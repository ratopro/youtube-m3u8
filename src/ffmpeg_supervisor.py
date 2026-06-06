"""Monitor and recovery helpers for the ffmpeg pipelines we spawn.

The Flask app launches three ffmpeg processes per active stream (preview,
processed and presentation).  Each of them can silently die, hang, or get
orphaned after a container restart.  Without supervision, the player
will keep polling a stale playlist and show no image even though the
stream URL is still considered "connected".

This module provides:
- A supervisor thread that watches a process and a target HLS directory.
  It checks the modification time of the most recent segment and restarts
  the process if no fresh segment has been written in STALL_TIMEOUT_SEC.
  It also reads the process stderr in a background thread and logs the
  last few lines if the process dies.
- A kill helper that prefers SIGTERM and falls back to SIGKILL.
- A startup helper that kills any orphaned ffmpeg process left behind
  by a previous container instance and clears stale temp directories.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger("ffmpeg_supervisor")

STALL_TIMEOUT_SEC = 30.0
STARTUP_GRACE_SEC = 15.0
STDERR_TAIL_LINES = 20


def kill_process(proc: subprocess.Popen | None) -> bool:
    """Try to stop a process gracefully, then force kill if needed.

    Returns True if the process is no longer alive after the call.
    """
    if proc is None:
        return True
    if proc.poll() is not None:
        return True
    try:
        proc.terminate()
        for _ in range(20):  # ~1s
            if proc.poll() is not None:
                return True
            time.sleep(0.05)
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        return True
    except (ProcessLookupError, OSError):
        return False


def _read_stderr(proc: subprocess.Popen, buffer: list[str]) -> None:
    """Drain stderr into a bounded ring buffer until the process exits."""
    try:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                buffer.append(line)
                if len(buffer) > STDERR_TAIL_LINES:
                    buffer.pop(0)
                log.debug("ffmpeg[%s] %s", proc.pid, line)
    except Exception:  # pragma: no cover - defensive
        pass


@dataclass
class SupervisorConfig:
    name: str
    get_proc: Callable[[], "subprocess.Popen | None"]
    get_segment_dir: Callable[[], Path | None]
    restart: Callable[[], None]
    on_dead: Callable[[str], None] | None = None
    stall_timeout_sec: float = STALL_TIMEOUT_SEC
    startup_grace_sec: float = STARTUP_GRACE_SEC


class Supervisor(threading.Thread):
    """Watchdog thread: restarts ffmpeg when segments stop advancing."""

    daemon = True

    def __init__(self, cfg: SupervisorConfig):
        super().__init__(name=f"ffmpeg-supervisor-{cfg.name}")
        self._cfg = cfg
        self._stop_event = threading.Event()
        self._stderr_buffer: list[str] = []
        self._current_proc: subprocess.Popen | None = None
        self._last_segment_mtime: float = 0.0
        self._started_at: float = 0.0
        self._lock = threading.Lock()
        self._consecutive_failures: int = 0
        self._max_backoff_sec: float = 60.0

    def stop(self) -> None:
        self._stop_event.set()

    def _attach(self) -> None:
        proc = self._cfg.get_proc()
        if proc is None:
            return
        if proc is self._current_proc:
            return
        self._current_proc = proc
        self._stderr_buffer = []
        self._started_at = time.time()
        self._last_segment_mtime = self._scan_dir()
        threading.Thread(
            target=_read_stderr, args=(proc, self._stderr_buffer), daemon=True
        ).start()

    def _scan_dir(self) -> float:
        d = self._cfg.get_segment_dir()
        if d is None or not d.exists():
            return 0.0
        try:
            latest = max(
                ((p.stat().st_mtime, p.name) for p in d.iterdir() if p.is_file()),
                default=(0.0, ""),
            )
            return latest[0]
        except OSError:
            return 0.0

    def run(self) -> None:  # pragma: no cover - long running thread
        while not self._stop_event.is_set():
            with self._lock:
                self._attach()
                proc = self._current_proc
                if proc is None:
                    self._stop_event.wait(1.0)
                    continue

                # Process exited: log, restart.
                if proc.poll() is not None:
                    tail = "\n".join(self._stderr_buffer[-STDERR_TAIL_LINES:])
                    uptime = time.time() - self._started_at
                    if uptime < 5.0:
                        # Quick failure: apply exponential backoff to
                        # avoid a hot relaunch loop when ffmpeg keeps
                        # dying instantly (e.g. permission denied).
                        self._consecutive_failures += 1
                        backoff = min(
                            self._max_backoff_sec,
                            2 ** min(self._consecutive_failures, 6),
                        )
                        log.warning(
                            "ffmpeg[%s/%s] exited rc=%s after %.1fs. "
                            "Backing off %.0fs (consecutive failures=%d). Tail:\n%s",
                            self._cfg.name,
                            proc.pid,
                            proc.returncode,
                            uptime,
                            backoff,
                            self._consecutive_failures,
                            tail or "<no stderr>",
                        )
                        if self._consecutive_failures >= 3:
                            log.error(
                                "ffmpeg[%s] has failed %d times in a row; "
                                "giving up auto-restart until manually triggered",
                                self._cfg.name,
                                self._consecutive_failures,
                            )
                            self._current_proc = None
                            self._stop_event.wait(backoff)
                            continue
                        self._current_proc = None
                        try:
                            self._cfg.restart()
                        except Exception:  # pragma: no cover
                            log.exception("Restart callback failed for %s", self._cfg.name)
                        self._stop_event.wait(backoff)
                        continue
                    else:
                        log.warning(
                            "ffmpeg[%s/%s] exited rc=%s after %.0fs. Tail:\n%s",
                            self._cfg.name,
                            proc.pid,
                            proc.returncode,
                            uptime,
                            tail or "<no stderr>",
                        )
                        self._consecutive_failures = 0
                    if self._cfg.on_dead:
                        try:
                            self._cfg.on_dead(tail)
                        except Exception:  # pragma: no cover
                            log.exception("on_dead handler failed")
                    self._current_proc = None
                    try:
                        self._cfg.restart()
                    except Exception:  # pragma: no cover
                        log.exception("Restart callback failed for %s", self._cfg.name)
                    self._stop_event.wait(2.0)
                    continue

                # Stall detection: only after the startup grace window.
                elapsed = time.time() - self._started_at
                if elapsed > self._cfg.startup_grace_sec:
                    latest_mtime = self._scan_dir()
                    if latest_mtime and latest_mtime > self._last_segment_mtime:
                        self._last_segment_mtime = latest_mtime
                    elif (
                        self._last_segment_mtime
                        and (time.time() - self._last_segment_mtime)
                        > self._cfg.stall_timeout_sec
                    ):
                        log.warning(
                            "ffmpeg[%s] stalled for %.0fs, restarting",
                            self._cfg.name,
                            time.time() - self._last_segment_mtime,
                        )
                        kill_process(proc)
                        self._current_proc = None
                        self._consecutive_failures = 0
                        try:
                            self._cfg.restart()
                        except Exception:  # pragma: no cover
                            log.exception("Restart callback failed for %s", self._cfg.name)
                        self._stop_event.wait(2.0)
                        continue
                    elif not self._last_segment_mtime and latest_mtime:
                        self._last_segment_mtime = latest_mtime

            self._stop_event.wait(2.0)


def cleanup_orphans(extra_roots: Iterable[Path] = ()) -> int:
    """Kill any ffmpeg process left behind and clean stale temp dirs.

    Called once during app startup so a previous container instance
    cannot keep holding ports or temp files.  Returns the number of
    ffmpeg processes killed.
    """
    killed = 0
    try:
        result = subprocess.run(
            ["pgrep", "-f", "ffmpeg"],
            check=False,
            capture_output=True,
            text=True,
        )
        for raw in result.stdout.split():
            try:
                pid = int(raw)
            except ValueError:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                pass
    except FileNotFoundError:
        # pgrep not available; best effort only.
        pass

    for root in extra_roots:
        if not root or not root.exists():
            continue
        for entry in root.iterdir():
            try:
                if entry.is_file():
                    entry.unlink()
            except OSError:
                pass
    if killed:
        log.info("Cleaned up %d orphaned ffmpeg process(es)", killed)
    return killed
