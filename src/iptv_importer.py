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
