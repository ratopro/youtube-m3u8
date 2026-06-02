import json
import os
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path


def _add_time(time_str: str, duration_seconds: int | None) -> str | None:
    if not time_str or not duration_seconds:
        return None
    parts = time_str.split(":")
    total = int(parts[0]) * 3600 + int(parts[1]) * 60 + duration_seconds
    hours = (total // 3600) % 24
    minutes = (total % 3600) // 60
    return f"{hours:02d}:{minutes:02d}"


def _epg_end_to_hm(epg_end: str | None) -> str | None:
    if not epg_end or not isinstance(epg_end, str):
        return None
    digits = re.sub(r"[^0-9]", "", epg_end)
    if len(digits) < 12:
        return None
    return f"{digits[8:10]}:{digits[10:12]}"


def default_state():
    return {
        "version": 2,
        "sources": [],
        "calendar": [],
        "auto_enabled": False,
    }


class PlayoutEngine:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.lock = threading.RLock()
        self.callbacks = {}
        self._state = None
        self._running = False
        self._thread = None
        self._load()

    def set_callbacks(self, callbacks: dict):
        self.callbacks = callbacks

    def _load(self):
        path = Path(self.state_file)
        if path.exists():
            try:
                with open(path) as f:
                    self._state = json.load(f)
                changed = False
                for s in self._state.get("sources", []):
                    if "emit_enabled" not in s:
                        s["emit_enabled"] = False
                        changed = True
                if changed:
                    self._save()
                return
            except (json.JSONDecodeError, Exception):
                pass
        self._state = default_state()
        self._save()

    def _save(self):
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self._state, f, indent=2)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            try:
                self._check()
            except Exception:
                pass
            time.sleep(5)

    def _get_auto_candidates(self) -> list[dict]:
        return [s for s in self._state["sources"] if s.get("auto_enabled", True)]

    def ensure_presentation_source(self, video_exists: bool) -> bool:
        with self.lock:
            if not video_exists:
                return False
            for s in self._state["sources"]:
                if s["type"] == "presentation":
                    return True
            existing_ids = {s["id"] for s in self._state["sources"]}
            src_id = "src_1"
            n = 1
            while src_id in existing_ids:
                n += 1
                src_id = f"src_{n}"
            self._state["sources"].append({
                "id": src_id,
                "name": "Video presentacion",
                "type": "presentation",
                "url": "",
                "auto_enabled": True,
                "created_at": datetime.now().isoformat(),
                "duration_label": "Directo",
                "is_live": True,
            })
            self._save()
            return True

    def _find_source(self, source_id: str) -> dict | None:
        for s in self._state["sources"]:
            if s["id"] == source_id:
                return s
        return None

    def find_source_by_name(self, name: str) -> dict | None:
        with self.lock:
            for s in self._state["sources"]:
                if s["name"] == name:
                    return s
        return None

    def _check(self):
        source_to_activate = None
        reason = None
        cal_id = None

        with self.lock:
            today = datetime.now().strftime("%Y-%m-%d")
            now_time = datetime.now().strftime("%H:%M")

            # Sweep past time-based entries that are still pending
            for entry in self._state["calendar"]:
                entry_time = entry.get("time", "")
                if (
                    entry["date"] == today
                    and entry.get("start_mode", "time") == "time"
                    and entry_time
                    and entry_time < now_time
                    and entry.get("status") != "played"
                    and entry.get("enabled", True)
                ):
                    entry["status"] = "played"
                    source = self._find_source(entry.get("source_id", ""))
                    self.add_history_entry({
                        "timestamp": datetime.now().isoformat(),
                        "source_id": entry.get("source_id", ""),
                        "source_name": entry.get("title", ""),
                        "source_type": source["type"] if source else "calendar",
                        "reason": "time_passed",
                        "calendar_id": entry["id"],
                        "success": False,
                        "error": "Horario vencido",
                    })
            self._save()

            current = self.callbacks.get("get_stream_state", lambda: {})()
            current_cal_id = current.get("current_calendar_id")
            current_mode = current.get("mode")

            # Cut current program if its end_time has passed
            if current_cal_id and current_mode:
                for entry in self._state["calendar"]:
                    if entry["id"] == current_cal_id and entry.get("end_time") and entry["end_time"] <= now_time:
                        entry["status"] = "played"
                        self._save()
                        source_to_activate = None
                        reason = None
                        cal_id = None
                        current_cal_id = None
                        break

            if not source_to_activate and current_cal_id is not None:
                source_to_activate = None

            # Time-based calendar
            if not source_to_activate:
                for entry in self._state["calendar"]:
                    if (
                        entry["date"] == today
                        and entry.get("start_mode", "time") == "time"
                        and entry["time"] <= now_time
                        and entry.get("enabled", True)
                        and entry.get("status") != "played"
                    ):
                        source = self._find_source(entry["source_id"])
                        if source:
                            source_to_activate = source
                            reason = "calendar"
                            cal_id = entry["id"]
                            entry["status"] = "played"
                            self._save()
                        break

        if source_to_activate:
            activate = self.callbacks.get("activate_source")
            if activate:
                activate(source_to_activate, reason, cal_id)
            return

        with self.lock:
            current = self.callbacks.get("get_stream_state", lambda: {})()
            mode = current.get("mode")

            if mode is None or mode == "presentation":
                for entry in self._state["calendar"]:
                    if (
                        entry["date"] == today
                        and entry.get("start_mode") == "after_previous"
                        and entry.get("enabled", True)
                        and entry.get("status") != "played"
                    ):
                        source = self._find_source(entry["source_id"])
                        if source:
                            source_to_activate = source
                            reason = "after_previous"
                            cal_id = entry["id"]
                            entry["status"] = "played"
                            self._save()
                        break

        if source_to_activate:
            activate = self.callbacks.get("activate_source")
            if activate:
                activate(source_to_activate, reason, cal_id)
            return

        with self.lock:
            current = self.callbacks.get("get_stream_state", lambda: {})()
            # Auto fills gaps: emit random source between scheduled programs
            if self._state.get("auto_enabled") and not current.get("mode"):
                # Check if there's a future scheduled program; if so, auto fills until then
                next_scheduled = None
                today = datetime.now().strftime("%Y-%m-%d")
                now_time = datetime.now().strftime("%H:%M")
                for e in self._state["calendar"]:
                    if e["date"] == today and e.get("start_mode", "time") == "time" and e["time"] > now_time and e.get("enabled", True) and e.get("status") != "played":
                        next_scheduled = e["time"]
                        break
                candidates = self._get_auto_candidates()
                if candidates:
                    last_few = [h["source_id"] for h in self._state.get("history", [])[-5:] if h.get("success")]
                    if len(candidates) > 1:
                        remaining = [c for c in candidates if c["id"] not in last_few]
                        if remaining:
                            candidates = remaining
                    last_id = current.get("current_source_id")
                    if len(candidates) > 1 and last_id:
                        remaining = [c for c in candidates if c["id"] != last_id]
                        if remaining:
                            candidates = remaining
                    source_to_activate = random.choice(candidates)
                    reason = "auto_gap" if next_scheduled else "auto"

        if source_to_activate:
            activate = self.callbacks.get("activate_source")
            if activate:
                activate(source_to_activate, reason, None)

    def get_state(self) -> dict:
        with self.lock:
            return dict(self._state)

    def get_sources(self) -> list:
        with self.lock:
            return list(self._state["sources"])

    def get_calendar(self, date: str | None = None) -> list:
        with self.lock:
            if date:
                entries = [e for e in self._state["calendar"] if e["date"] == date]
            else:
                entries = list(self._state["calendar"])
            entries.sort(key=lambda e: e.get("time") or "99:99")
            return entries

    def recalculate_calendar_day(self, date: str):
        with self.lock:
            entries = [e for e in self._state["calendar"] if e["date"] == date]
            last_end = None
            for entry in entries:
                epg_end_hm = _epg_end_to_hm(entry.get("epg_end"))
                if epg_end_hm:
                    entry["end_time"] = epg_end_hm
                    last_end = epg_end_hm
                    continue
                dur = entry.get("duration_seconds")
                if entry.get("start_mode") == "time":
                    if not entry.get("time_locked") and last_end:
                        entry["time"] = last_end
                    if entry.get("time"):
                        end = _add_time(entry["time"], dur)
                        entry["end_time"] = end
                        last_end = end if end else None
                    else:
                        last_end = None
                else:
                    if last_end:
                        entry["time"] = last_end
                        end = _add_time(last_end, dur)
                        entry["end_time"] = end
                        last_end = end if end else None
                    else:
                        entry["time"] = ""
                        entry["end_time"] = None
                        last_end = None
            self._save()

    def reorder_calendar(self, date: str, cal_ids: list[str]) -> bool:
        with self.lock:
            today_entries = [e for e in self._state["calendar"] if e["date"] == date]
            other_entries = [e for e in self._state["calendar"] if e["date"] != date]
            id_map = {e["id"]: e for e in today_entries}
            reordered = []
            for cid in cal_ids:
                if cid in id_map:
                    reordered.append(id_map.pop(cid))
            reordered.extend(id_map.values())
            self._state["calendar"] = other_entries + reordered
            self._save()
        self.recalculate_calendar_day(date)
        return True

    def add_source(self, source: dict) -> str | None:
        with self.lock:
            existing_ids = {s["id"] for s in self._state["sources"]}
            src_id = "src_1"
            n = 1
            while src_id in existing_ids:
                n += 1
                src_id = f"src_{n}"
            source["id"] = src_id
            source["created_at"] = datetime.now().isoformat()
            source.setdefault("auto_enabled", True)
            source.setdefault("emit_enabled", False)
            self._state["sources"].append(source)
            self._save()
            return src_id

    def delete_source(self, source_id: str) -> bool:
        with self.lock:
            before = len(self._state["sources"])
            self._state["sources"] = [
                s for s in self._state["sources"] if s["id"] != source_id
            ]
            if before == len(self._state["sources"]):
                return False
            self._state["calendar"] = [
                e for e in self._state["calendar"] if e["source_id"] != source_id
            ]
            self._save()
            return True

    def delete_sources_by_provider(self, provider_id: str) -> int:
        with self.lock:
            source_ids = {
                s["id"] for s in self._state["sources"]
                if s.get("iptv_provider") == provider_id
            }
            before = len(self._state["sources"])
            self._state["sources"] = [
                s for s in self._state["sources"]
                if s.get("iptv_provider") != provider_id
            ]
            removed = before - len(self._state["sources"])
            self._state["calendar"] = [
                e for e in self._state["calendar"]
                if e.get("source_id") not in source_ids
            ]
            self._save()
            return removed

    def toggle_source_auto(self, source_id: str) -> bool:
        with self.lock:
            for s in self._state["sources"]:
                if s["id"] == source_id:
                    s["auto_enabled"] = not s.get("auto_enabled", True)
                    self._save()
                    return True
            return False

    def add_calendar_entry(self, entry: dict) -> str | None:
        with self.lock:
            cal_id = self._generate_cal_id()
            self._prepare_calendar_entry(entry, cal_id)
            self._state["calendar"].append(entry)
            self._save()
        if entry["date"]:
            self.recalculate_calendar_day(entry["date"])
        return cal_id

    def insert_calendar_entry(self, entry: dict, insert_before: str | None = None) -> str | None:
        with self.lock:
            cal_id = self._generate_cal_id()
            self._prepare_calendar_entry(entry, cal_id)
            if insert_before:
                idx = -1
                for i, e in enumerate(self._state["calendar"]):
                    if e["id"] == insert_before:
                        idx = i
                        break
                if idx >= 0:
                    self._state["calendar"].insert(idx, entry)
                else:
                    self._state["calendar"].append(entry)
            else:
                self._state["calendar"].append(entry)
            self._save()
        if entry["date"]:
            self.recalculate_calendar_day(entry["date"])
        return cal_id

    def _generate_cal_id(self) -> str:
        existing_ids = {e["id"] for e in self._state["calendar"]}
        cal_id = "cal_1"
        n = 1
        while cal_id in existing_ids:
            n += 1
            cal_id = f"cal_{n}"
        return cal_id

    def _prepare_calendar_entry(self, entry: dict, cal_id: str):
        if not entry.get("title", "").strip():
            src = self._find_source(entry.get("source_id", ""))
            entry["title"] = src["name"] if src else "Programa"
        src = self._find_source(entry.get("source_id", ""))
        if src:
            entry.setdefault("duration_seconds", src.get("duration_seconds"))
            entry.setdefault("duration_label", src.get("duration_label"))
            entry.setdefault("is_live", src.get("is_live", True))
        entry.setdefault("start_mode", "time")
        entry.setdefault("time", "")
        entry.setdefault("end_time", None)
        entry["time_locked"] = entry.get("start_mode") == "time" and bool(entry.get("time"))
        entry["id"] = cal_id
        entry["enabled"] = entry.get("enabled", True)
        entry["status"] = "pending"

    def delete_calendar_entry(self, cal_id: str) -> bool:
        with self.lock:
            entry = None
            for e in self._state["calendar"]:
                if e["id"] == cal_id:
                    entry = e
                    break
            if not entry:
                return False
            date = entry.get("date", "")
            self._state["calendar"] = [e for e in self._state["calendar"] if e["id"] != cal_id]
            self._save()
        if date:
            self.recalculate_calendar_day(date)
        return True

    def toggle_calendar_entry(self, cal_id: str) -> bool:
        with self.lock:
            for e in self._state["calendar"]:
                if e["id"] == cal_id:
                    e["enabled"] = not e.get("enabled", True)
                    date = e.get("date", "")
                    self._save()
                    if date:
                        self.recalculate_calendar_day(date)
                    return True
            return False

    def set_calendar_played(self, cal_id: str) -> bool:
        with self.lock:
            for e in self._state["calendar"]:
                if e["id"] == cal_id:
                    e["status"] = "played"
                    self._save()
                    return True
            return False

    def find_calendar_entry(self, cal_id: str) -> dict | None:
        with self.lock:
            for e in self._state["calendar"]:
                if e["id"] == cal_id:
                    return dict(e)
            return None

    def update_calendar_entry(self, cal_id: str, updates: dict) -> bool:
        with self.lock:
            for e in self._state["calendar"]:
                if e["id"] == cal_id:
                    for key, value in updates.items():
                        if key not in ("id", "date"):
                            e[key] = value
                    if updates.get("time") is not None:
                        e["time_locked"] = bool(updates.get("time"))
                    self._save()
                    break
            else:
                return False
        if updates.get("time") is not None or updates.get("duration_seconds") is not None:
            for e in self._state["calendar"]:
                if e["id"] == cal_id:
                    self.recalculate_calendar_day(e.get("date", ""))
                    break
        return True

    def reset_calendar_status(self, cal_id: str) -> bool:
        with self.lock:
            for e in self._state["calendar"]:
                if e["id"] == cal_id:
                    e["status"] = "pending"
                    self._save()
                    return True
            return False

    def set_auto_enabled(self, enabled: bool):
        with self.lock:
            self._state["auto_enabled"] = enabled
            self._save()

    def is_auto_enabled(self) -> bool:
        with self.lock:
            return self._state.get("auto_enabled", False)

    def find_source_by_id(self, source_id: str) -> dict | None:
        with self.lock:
            return self._find_source(source_id)

    def find_next_after_previous(self) -> tuple[dict, str] | None:
        with self.lock:
            today = datetime.now().strftime("%Y-%m-%d")
            for entry in self._state["calendar"]:
                if (
                    entry["date"] == today
                    and entry.get("start_mode") == "after_previous"
                    and entry.get("enabled", True)
                    and entry.get("status") != "played"
                ):
                    source = self._find_source(entry["source_id"])
                    if source:
                        return (source, entry["id"])
        return None

    def has_upcoming_calendar(self) -> bool:
        with self.lock:
            today = datetime.now().strftime("%Y-%m-%d")
            now_time = datetime.now().strftime("%H:%M")
            for e in self._state["calendar"]:
                if e["date"] == today and e.get("start_mode", "time") == "time" and e["time"] > now_time and e.get("enabled", True) and e.get("status") != "played":
                    return True
            return False

    def get_last_played_sources(self, count: int = 5) -> list[str]:
        history = self._state.get("history", [])
        return [h["source_id"] for h in history[-count:] if h.get("success")]

    def get_next_auto_source(self) -> dict | None:
        with self.lock:
            current = self.callbacks.get("get_stream_state", lambda: {})()
            current_id = current.get("current_source_id")
            candidates = self._get_auto_candidates()
            if not candidates:
                return None
            last_few = [h["source_id"] for h in self._state.get("history", [])[-5:] if h.get("success")]
            if len(candidates) > 1:
                remaining = [c for c in candidates if c["id"] not in last_few]
                if remaining:
                    candidates = remaining
            if len(candidates) > 1 and current_id:
                remaining = [c for c in candidates if c["id"] != current_id]
                if remaining:
                    candidates = remaining
            return candidates[0] if candidates else None

    def add_history_entry(self, entry: dict):
        with self.lock:
            if "history" not in self._state:
                self._state["history"] = []
            existing_ids = {h["id"] for h in self._state["history"]}
            hid = "hist_1"
            n = 1
            while hid in existing_ids:
                n += 1
                hid = f"hist_{n}"
            entry["id"] = hid
            self._state["history"].append(entry)
            if len(self._state["history"]) > 500:
                self._state["history"] = self._state["history"][-500:]
            self._save()

    def get_history(self, limit: int = 100) -> list:
        with self.lock:
            return list(self._state.get("history", []))[-limit:]

    def update_source(self, source_id: str, updates: dict) -> bool:
        with self.lock:
            for s in self._state["sources"]:
                if s["id"] == source_id:
                    for key, value in updates.items():
                        if key != "id":
                            s[key] = value
                    self._save()
                    return True
            return False

    def set_source_validation(self, source_id: str, status: str, error: str | None = None):
        with self.lock:
            for s in self._state["sources"]:
                if s["id"] == source_id:
                    s["validation"] = {
                        "status": status,
                        "error": error,
                        "checked_at": datetime.now().isoformat(),
                    }
                    self._save()
                    return True
            return False
