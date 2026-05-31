import json
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen


def _load_config():
    path = Path("data/config.json")
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def fetch_events() -> list[dict]:
    cfg = _load_config().get("rapidapi", {})
    key = cfg.get("key", "")
    host = cfg.get("host", "wosti-futbol-tv-spain.p.rapidapi.com")
    if not key:
        return []
    req = Request(
        f"https://{host}/api/Events",
        headers={
            "x-rapidapi-key": key,
            "x-rapidapi-host": host,
            "Content-Type": "application/json",
        },
    )
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


SPANISH_COMPETITIONS = {
    "LaLiga Hypermotion",
    "LaLiga EA Sports",
    "Copa del Rey",
    "Supercopa de España",
    "Primera Federación",
    "Segunda Federación",
}


def is_spanish_team(name: str) -> bool:
    return name in {
        "España", "Selección Española",
    }


def has_spanish_team(event: dict) -> bool:
    local = (event.get("LocalTeam") or {}).get("Name", "")
    away = (event.get("AwayTeam") or {}).get("Name", "")
    comp = (event.get("Competition") or {}).get("Name", "")
    return (
        comp in SPANISH_COMPETITIONS
        or is_spanish_team(local)
        or is_spanish_team(away)
    )


def get_today_matches() -> list[dict]:
    events = fetch_events()
    today = datetime.now().strftime("%Y-%m-%d")
    matches = []
    for ev in events:
        date_str = ev.get("Date", "")
        if not date_str.startswith(today):
            continue
        if not has_spanish_team(ev):
            continue
        local = (ev.get("LocalTeam") or {}).get("Name", "")
        away = (ev.get("AwayTeam") or {}).get("Name", "")
        comp = (ev.get("Competition") or {}).get("Name", "")
        channels = [c.get("Name", "") for c in ev.get("Channels") or []]
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        time_str = dt.strftime("%H:%M")
        matches.append({
            "id": ev.get("Id"),
            "local": local,
            "away": away,
            "competition": comp,
            "time": time_str,
            "date": today,
            "channels": channels,
            "title": f"{local} vs {away}",
        })
    matches.sort(key=lambda m: m["time"])
    return matches
