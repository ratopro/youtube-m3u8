import difflib
import io
import re
import urllib.parse
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
    base = (provider.get("dns") or "").rstrip("/")
    user = urllib.parse.quote(str(provider.get("username", "")), safe="")
    pwd = urllib.parse.quote(str(provider.get("password", "")), safe="")
    return f"{base}/live/{user}/{pwd}/{stream_id}.ts"


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


def _parse_epg_source(source, channel_ids: set[str] | None = None, include_programmes: bool = True) -> dict:
    channels: dict[str, dict] = {}
    programmes: dict[str, list[dict]] = {}

    for _event, elem in ET.iterparse(source, events=("end",)):
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "channel":
            cid = elem.get("id", "")
            if cid:
                name_el = elem.find("display-name")
                if name_el is None:
                    name_el = elem.find("{*}display-name")
                icon_el = elem.find("icon")
                if icon_el is None:
                    icon_el = elem.find("{*}icon")
                channels[cid] = {
                    "id": cid,
                    "name": (name_el.text or cid).strip() if name_el is not None and name_el.text else cid,
                    "icon": icon_el.get("src") if icon_el is not None and icon_el.get("src") else "",
                }
            elem.clear()
        elif tag == "programme":
            if not include_programmes:
                elem.clear()
                continue
            ch = elem.get("channel", "")
            if ch and (channel_ids is None or ch in channel_ids):
                title_el = elem.find("title")
                if title_el is None:
                    title_el = elem.find("{*}title")
                desc_el = elem.find("desc")
                if desc_el is None:
                    desc_el = elem.find("{*}desc")
                title = title_el.text.strip() if title_el is not None and title_el.text else "Programa"
                desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
                programmes.setdefault(ch, []).append({
                    "title": title,
                    "description": desc,
                    "start": elem.get("start", ""),
                    "end": elem.get("stop", ""),
                    "channel_id": ch,
                })
            elem.clear()

    for ch_id in programmes:
        programmes[ch_id].sort(key=lambda x: x.get("start", ""))
    return {"channels": channels, "programmes": programmes}


def parse_epg_full(raw: bytes) -> dict:
    return _parse_epg_source(io.BytesIO(raw))


def parse_epg_file(path: Path, channel_ids: set[str] | None = None, include_programmes: bool = True) -> dict:
    return _parse_epg_source(path, channel_ids=channel_ids, include_programmes=include_programmes)


def ensure_xtream_xmltv_cache(provider: dict, force: bool = False) -> Path:
    cached = epg_cache_path(provider["id"])
    if cached.exists() and not force:
        return cached

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
        temp_path = None
        try:
            EPG_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = cached.with_suffix(".xml.tmp")
            with urlopen(req, timeout=60) as r:
                with temp_path.open("wb") as f:
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
            temp_path.replace(cached)
            return cached
        except Exception as exc:
            last_error = exc
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            continue

    raise RuntimeError(f"No se pudo descargar XMLTV del proveedor: {last_error}")


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
        try:
            cached = ensure_xtream_xmltv_cache({**provider, "dns": dns, "dns_alt": None}, force=True)
            data = parse_epg_file(cached)
            data["source_url"] = "xtream"
            data["cached"] = False
            return data
        except Exception as exc:
            last_error = exc
            continue
    cached = epg_cache_path(provider["id"])
    if cached.exists():
        try:
            data = parse_epg_file(cached)
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


def _clean_group_name(raw: str) -> str:
    if not raw:
        return "Otros"
    cleaned = re.sub(r"^[A-Z]{2}\s*\|\s*", "", raw.strip(), flags=re.IGNORECASE)
    return cleaned or raw.strip()


def fetch_provider_categories(provider: dict) -> dict[str, str]:
    """Return a real category_id -> cleaned name map for the provider.

    Falls back to the provider's locally-configured categories when the
    Xtream API is unreachable.
    """
    try:
        cats = fetch_categories(provider)
    except Exception:
        return {str(cid): name for cid, name in (provider.get("categories") or {}).items()}

    if not isinstance(cats, list):
        return {str(cid): name for cid, name in (provider.get("categories") or {}).items()}

    result: dict[str, str] = {}
    for c in cats:
        cid = str(c.get("category_id", "")).strip()
        name = (c.get("category_name") or "").strip()
        if cid:
            result[cid] = _clean_group_name(name)
    if not result:
        return {str(cid): name for cid, name in (provider.get("categories") or {}).items()}
    return result


def fetch_provider_streams(provider: dict) -> list[dict]:
    """Return all live streams for the provider as raw Xtream dicts."""
    params = f"username={provider['username']}&password={provider['password']}&action=get_live_streams"
    url = f"{provider['dns'].rstrip('/')}/player_api.php?{params}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode())
    return data if isinstance(data, list) else []


def channels_for_category(provider: dict, category_id: str) -> list[dict]:
    group = GROUP_MAP.get(category_id) or _clean_group_name(category_id)
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
            "emit_enabled": False,
            "duration_label": "Directo",
            "is_live": True,
            "iptv_provider": provider["id"],
            "iptv_stream_id": stream_id,
            "iptv_category_id": category_id,
            "iptv_group": group,
        })
    return result


def import_provider_channels(
    provider: dict,
    playout_engine,
) -> dict:
    """Import every live stream from an Xtream provider like netv does.

    Matches existing sources by (provider_id, stream_id) so re-imports don't
    create duplicates; refreshes URL/name/group and marks new channels as
    hidden (emit_enabled=False, auto_enabled=False) so they show up in
    /sources but do not pollute the main player until the user opts-in.
    """
    category_map = fetch_provider_categories(provider)
    streams = fetch_provider_streams(provider)
    imported = 0
    updated = 0
    skipped = 0
    errors: list[str] = []

    for ch in streams:
        stream_id = ch.get("stream_id")
        name = (ch.get("name") or "").strip()
        if not stream_id or not name:
            skipped += 1
            continue
        category_ids = ch.get("category_ids") or []
        primary_cat_id = str(category_ids[0]) if category_ids else ""
        group_name = category_map.get(primary_cat_id)
        if not group_name:
            if primary_cat_id in GROUP_MAP:
                group_name = GROUP_MAP[primary_cat_id]
            else:
                group_name = "Otros"
        url = xtream_live_url(provider, stream_id)

        existing = playout_engine.find_source_by_provider_stream(provider["id"], stream_id)
        if existing:
            updates = {
                "name": name,
                "url": url,
                "iptv_category_id": primary_cat_id,
                "iptv_group": group_name,
            }
            playout_engine.update_source(existing["id"], updates)
            updated += 1
            continue

        new_source = {
            "name": name,
            "type": "iptv",
            "url": url,
            "auto_enabled": False,
            "emit_enabled": False,
            "duration_label": "Directo",
            "is_live": True,
            "iptv_provider": provider["id"],
            "iptv_stream_id": stream_id,
            "iptv_category_id": primary_cat_id,
            "iptv_group": group_name,
        }
        playout_engine.add_source(new_source)
        imported += 1

    return {
        "provider_id": provider["id"],
        "total": len(streams),
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "category_count": len(category_map),
    }


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
