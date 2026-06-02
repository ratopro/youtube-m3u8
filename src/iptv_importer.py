import difflib
import re
import xml.etree.ElementTree as ET
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import json

CONFIG_PATH = Path("data/config.json")


def load_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=True)


def slugify_provider_id(text: str) -> str:
    allowed = []
    for ch in text.strip().lower():
        if ch.isalnum():
            allowed.append(ch)
        elif ch in (" ", "-", "_"):
            allowed.append("-")
    slug = "".join(allowed).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "proveedor"


def add_provider(provider: dict) -> dict:
    cfg = load_config() or {}
    providers = cfg.get("iptv_providers", [])
    pid = provider["id"]
    for p in providers:
        if p.get("id") == pid:
            raise ValueError(f"Ya existe un proveedor con id '{pid}'")
    providers.append(provider)
    cfg["iptv_providers"] = providers
    save_config(cfg)
    return provider


def delete_provider(provider_id: str) -> bool:
    cfg = load_config()
    if not cfg:
        return False
    providers = cfg.get("iptv_providers", [])
    before = len(providers)
    cfg["iptv_providers"] = [p for p in providers if p.get("id") != provider_id]
    if len(cfg["iptv_providers"]) == before:
        return False
    save_config(cfg)
    return True


def get_providers() -> list[dict]:
    cfg = load_config()
    if not cfg:
        return []
    return cfg.get("iptv_providers", [])


def get_provider(provider_id: str) -> dict | None:
    for p in get_providers():
        if p["id"] == provider_id:
            return p
    return None


def xtream_live_url(provider: dict, stream_id: int) -> str:
    return f"{provider['dns']}/live/{provider['username']}/{provider['password']}/{stream_id}.ts"


def fetch_categories(provider: dict) -> list[dict]:
    params = f"username={provider['username']}&password={provider['password']}&action=get_live_categories"
    url = f"{provider['dns']}/player_api.php?{params}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_channels(provider: dict, category_id: str) -> list[dict]:
    params = f"username={provider['username']}&password={provider['password']}&action=get_live_streams&category_id={category_id}"
    url = f"{provider['dns']}/player_api.php?{params}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode())


def fetch_short_epg(provider: dict, stream_id: int | str, limit: int = 12) -> list[dict]:
    params = (
        f"username={provider['username']}&password={provider['password']}"
        f"&action=get_short_epg&stream_id={stream_id}&limit={int(limit)}"
    )
    url = f"{provider['dns']}/player_api.php?{params}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if isinstance(data, dict):
        listings = data.get("epg_listings")
        if isinstance(listings, list):
            return listings
    return []


def parse_epg_full(raw: bytes) -> dict:
    root = ET.fromstring(raw)
    channels: dict[str, dict] = {}
    for ch_el in root.findall("channel"):
        cid = ch_el.get("id", "")
        if not cid:
            continue
        name_el = ch_el.find("display-name")
        icon_el = ch_el.find("icon")
        channels[cid] = {
            "id": cid,
            "name": (name_el.text or cid).strip() if name_el is not None and name_el.text else cid,
            "icon": icon_el.get("src") if icon_el is not None and icon_el.get("src") else "",
        }
    programmes: dict[str, list[dict]] = {}
    for prog in root.findall("programme"):
        ch = prog.get("channel", "")
        if not ch:
            continue
        title_el = prog.find("title")
        desc_el = prog.find("desc")
        title = title_el.text.strip() if title_el is not None and title_el.text else "Programa"
        desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
        programmes.setdefault(ch, []).append({
            "title": title,
            "description": desc,
            "start": prog.get("start", ""),
            "end": prog.get("stop", ""),
            "channel_id": ch,
        })
    for ch_id in programmes:
        programmes[ch_id].sort(key=lambda x: x.get("start", ""))
    return {"channels": channels, "programmes": programmes}


EPG_DIR = Path("data/epg")


def epg_cache_path(provider_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", provider_id)
    return EPG_DIR / f"{safe}.xml"


def fetch_xtream_xmltv(provider: dict) -> dict:
    dns_candidates = [provider["dns"]]
    if provider.get("dns_alt"):
        dns_candidates.append(provider["dns_alt"])
    last_error = None
    for dns in dns_candidates:
        url = (
            f"{dns}/xmltv.php"
            f"?username={provider['username']}&password={provider['password']}"
        )
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(req, timeout=60) as r:
                raw = r.read()
            data = parse_epg_full(raw)
            try:
                EPG_DIR.mkdir(parents=True, exist_ok=True)
                epg_cache_path(provider["id"]).write_bytes(raw)
            except Exception:
                pass
            data["source_url"] = url
            data["cached"] = False
            return data
        except Exception as exc:
            last_error = exc
            continue
    cached = epg_cache_path(provider["id"])
    if cached.exists():
        try:
            data = parse_epg_full(cached.read_bytes())
            data["cached"] = True
            data["source_url"] = "cache"
            return data
        except Exception:
            pass
    raise RuntimeError(f"No se pudo descargar XMLTV del proveedor: {last_error}")


def _epg_match_key(name: str) -> str:
    return _normalize_name(name)


def build_xmltv_index(epg_data: dict) -> dict[str, str]:
    index: dict[str, str] = {}
    for cid, meta in epg_data.get("channels", {}).items():
        key = _epg_match_key(meta.get("name", ""))
        if key:
            index.setdefault(key, cid)
    return index


def resolve_channel_xmltv_id(stream: dict, epg_index: dict[str, str]) -> str | None:
    name = stream.get("name", "")
    key = _epg_match_key(name)
    if key in epg_index:
        return epg_index[key]
    if len(key) < 4:
        return None
    best = None
    best_score = 0.82
    for epg_name, cid in epg_index.items():
        score = difflib.SequenceMatcher(None, key, epg_name).ratio()
        if score > best_score:
            best_score = score
            best = cid
    return best


def channel_now_next(epg_data: dict, xmltv_id: str, now: datetime | None = None) -> dict:
    progs = epg_data.get("programmes", {}).get(xmltv_id, [])
    if not progs:
        return {"now": None, "next": None}
    now = now or datetime.utcnow()
    now_str = now.strftime("%Y%m%d%H%M%S")
    current = None
    nxt = None
    for p in progs:
        start = p.get("start", "")
        end = p.get("end", "")
        if start and start <= now_str and (not end or now_str < end):
            current = p
            continue
        if start and start > now_str and not nxt:
            nxt = p
            break
    return {"now": current, "next": nxt}


GROUP_MAP = {
    "86": "Entretenimiento",
    "87": "Documentales",
    "88": "General",
    "89": "Cine",
    "90": "Deportes",
    "600": "Infantil",
    "601": "Musica",
    "410": "24/7",
}


def channels_for_category(provider: dict, category_id: str) -> list[dict]:
    group = GROUP_MAP.get(category_id, "Otros")
    channels = fetch_channels(provider, category_id)
    result = []
    for ch in channels:
        stream_id = ch.get("stream_id")
        name = ch.get("name", "").strip()
        if not name or not stream_id:
            continue
        result.append({
            "name": name,
            "type": "iptv",
            "url": xtream_live_url(provider, stream_id),
            "auto_enabled": False,
            "duration_label": "Directo",
            "is_live": True,
            "iptv_provider": provider["id"],
            "iptv_stream_id": stream_id,
            "iptv_category_id": category_id,
            "iptv_group": group,
        })
    return result


def check_url(url: str, timeout: int = 8) -> str:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-0"})
        with urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "") or ""
            if "video" in ct or "octet-stream" in ct or "mp2t" in ct:
                return "ok"
            if r.status == 200 or r.status == 206:
                return "ok"
            return f"bad_type:{ct}"
    except URLError as e:
        return f"dns_error:{e.reason}"
    except Exception as e:
        return f"error:{e}"


def _normalize_name(name: str) -> str:
    text = name.lower().strip()
    text = re.sub(r'^[a-z]{2}\s*\|\s*', '', text)
    text = re.sub(r'\s*(HD|FHD|UHD|4K|SD|HEVC|H264)\s*$', '', text, flags=re.IGNORECASE)
    result = []
    for ch in text:
        if ch.isalnum():
            result.append(ch)
        elif ch in (" ", "-", "_", "·", "&", "+", "/"):
            result.append(ch)
    return "".join(result).strip()


_started = False


def start_daily_scheduler(check_callback):
    global _started
    if _started:
        return
    _started = True
    thread = threading.Thread(target=_daily_scheduler_loop, args=(check_callback,), daemon=True)
    thread.start()


def _daily_scheduler_loop(check_callback):
    last_check_day = None
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if today != last_check_day and now.hour == 6 and now.minute == 0:
                check_callback()
                last_check_day = today
        except Exception:
            pass
        time.sleep(60)
