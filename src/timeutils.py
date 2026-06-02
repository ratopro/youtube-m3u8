import os
import time
from datetime import datetime

DEFAULT_TZ = os.environ.get("APP_TZ", "Europe/Madrid")


def configure_app_timezone() -> None:
    tz = os.environ.get("APP_TZ") or DEFAULT_TZ
    if not tz:
        return
    os.environ["TZ"] = tz
    try:
        time.tzset()
    except Exception:
        pass


def now() -> datetime:
    return datetime.now()


def now_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def now_iso() -> str:
    return datetime.now().isoformat()
