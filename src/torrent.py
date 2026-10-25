"""BitTorrent source management.

Torrents are downloaded with libtorrent and exposed as a local HTTP byte
stream.  Playback reads sequentially: the selected video file is prioritised,
the first and last pieces get short deadlines (so containers with the index
atom at the end can be probed immediately) and every read blocks until the
pieces it needs are on disk.

The module has no Flask dependencies; ``src/web.py`` wires the HTTP endpoint,
source metadata and player integration on top of :class:`TorrentManager`.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import libtorrent as lt

log = logging.getLogger("youtube_hls.torrent")

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts", ".webm", ".mpg", ".mpeg",
}
ALLOWED_TORRENT_EXTENSIONS = {".torrent"}

READ_CHUNK_SIZE = 512 * 1024
PIECE_LOOKAHEAD = 24
FIRST_PIECE_DEADLINES = 8
LAST_PIECE_DEADLINES = 8
RESUME_INTERVAL_SECONDS = 120.0


class TorrentError(RuntimeError):
    """Base error for torrent operations."""


class TorrentNotReady(TorrentError):
    """Raised when metadata or the selected video file is not ready yet."""


class TorrentStopped(TorrentError):
    """Raised when a stream is stopped because its source was removed."""


def inspect_torrent_data(data: bytes) -> dict:
    """Validate a .torrent payload and return basic metadata."""
    if not data:
        raise TorrentError("El archivo .torrent está vacío")
    try:
        info = lt.torrent_info(data)
    except Exception as exc:
        raise TorrentError(f"Archivo .torrent inválido: {exc}") from exc
    layout = info.layout()
    return {
        "name": info.name() or "Torrent",
        "info_hash": str(info.info_hash()),
        "total_size": int(layout.total_size()),
        "num_files": int(layout.num_files()),
    }


def inspect_magnet(magnet: str) -> dict:
    """Validate a magnet URI and return its display name / info hash."""
    magnet = (magnet or "").strip()
    if not magnet.lower().startswith("magnet:?"):
        raise TorrentError("Enlace magnet inválido")
    try:
        params = lt.parse_magnet_uri(magnet)
    except Exception as exc:
        raise TorrentError(f"Enlace magnet inválido: {exc}") from exc
    try:
        info_hash = str(params.info_hash)
    except Exception:
        info_hash = ""
    return {
        "name": params.name or "Torrent",
        "info_hash": info_hash,
    }


@dataclass
class _Entry:
    source_id: str
    handle: object
    save_path: Path
    torrent_path: Path
    resume_path: Path
    magnet: str | None = None
    name: str = ""
    info: object | None = None
    file_index: int | None = None
    file_path: str | None = None
    file_size: int = 0
    extension: str = ""
    error: str | None = None
    removed: bool = False
    prepared: bool = False
    last_prioritized: int = -1
    last_priority_at: float = 0.0
    last_resume_save: float = 0.0
    resume_saved_progress: float = -1.0
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def absolute_path(self) -> Path:
        if not self.file_path:
            return self.save_path
        return self.save_path / self.file_path


class TorrentManager:
    """Owns one libtorrent session and all torrent sources."""

    def __init__(
        self,
        root: str | Path,
        *,
        on_ready=None,
        on_error=None,
        listen_port: int = 6881,
        piece_timeout: float = 90.0,
        metadata_timeout: float = 60.0,
    ):
        self.root = Path(root)
        self.meta_dir = self.root / "meta"
        self.data_dir = self.root / "data"
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.on_ready = on_ready
        self.on_error = on_error
        self.listen_port = listen_port
        self.piece_timeout = piece_timeout
        self.metadata_timeout = metadata_timeout
        self._session: lt.session | None = None
        self._session_lock = threading.RLock()
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._stop = threading.Event()
        self._monitor = threading.Thread(target=self._monitor_loop, name="torrent-monitor", daemon=True)
        self._monitor.start()

    # ── session ────────────────────────────────────────────────────────────────

    def _ensure_session(self) -> lt.session:
        with self._session_lock:
            if self._session is not None:
                return self._session
            settings = {
                "listen_interfaces": f"0.0.0.0:{self.listen_port},[::]:{self.listen_port}",
                "enable_dht": True,
                "enable_lsd": True,
                "enable_upnp": False,
                "enable_natpmp": False,
                "alert_mask": int(
                    lt.alert_category.status
                    | lt.alert_category.storage
                    | lt.alert_category.error
                ),
                "active_downloads": 4,
                "active_seeds": 4,
                "active_limit": 12,
                "allow_multiple_connections_per_ip": True,
            }
            self._session = lt.session(settings)
            log.info("libtorrent session started (listen port %s)", self.listen_port)
            return self._session

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            self._save_resume(entry)
        with self._session_lock:
            session = self._session
            self._session = None
        if session is not None:
            try:
                session.pause()
            except Exception:
                pass
        with self._lock:
            self._entries.clear()

    # ── helpers ────────────────────────────────────────────────────────────────

    def meta_path(self, source_id: str) -> Path:
        return self.meta_dir / f"{source_id}.torrent"

    def fastresume_path(self, source_id: str) -> Path:
        return self.meta_dir / f"{source_id}.fastresume"

    def data_path(self, source_id: str) -> Path:
        return self.data_dir / source_id

    @staticmethod
    def _hash_keys(obj) -> set[str]:
        """Return the set of info-hash strings (v1 and/or v2) for a handle/params."""
        keys: set[str] = set()
        try:
            info = getattr(obj, "ti", None)
            if info is not None:
                return TorrentManager._hash_keys(info)
        except Exception:
            pass
        hashes = None
        try:
            attr = getattr(obj, "info_hashes", None)
            hashes = attr() if callable(attr) else attr
        except Exception:
            hashes = None
        if hashes is not None:
            for name in ("has_v1", "has_v2"):
                try:
                    if not getattr(hashes, name)():
                        continue
                except Exception:
                    continue
                try:
                    keys.add(str(getattr(hashes, name.replace("has_", ""))))
                except Exception:
                    pass
        if not keys:
            try:
                attr = getattr(obj, "info_hash", None)
                value = attr() if callable(attr) else attr
                if value:
                    keys.add(str(value))
            except Exception:
                pass
        return keys

    @staticmethod
    def _bencode_torrent(info) -> bytes:
        """Serialise a ``torrent_info`` back into a .torrent file."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            info_dict = lt.bdecode(info.metadata())
            trackers = list(info.trackers())
        payload: dict = {"info": info_dict}
        if trackers:
            payload["announce"] = trackers[0].url
            payload["announce-list"] = [[tracker.url] for tracker in trackers]
        return lt.bencode(payload)

    def _write_torrent(self, entry: _Entry) -> None:
        if entry.info is None or entry.torrent_path.exists():
            return
        try:
            data = self._bencode_torrent(entry.info)
            tmp = entry.torrent_path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(entry.torrent_path)
        except Exception as exc:
            log.warning("No se pudo guardar el .torrent de %s: %s", entry.source_id, exc)

    def _call_ready(self, entry: _Entry) -> None:
        if not self.on_ready:
            return
        try:
            self.on_ready(entry.source_id, self.descriptor(entry.source_id) or {})
        except Exception:
            log.exception("on_ready callback failed for %s", entry.source_id)

    def _call_error(self, entry: _Entry) -> None:
        if not self.on_error:
            return
        try:
            self.on_error(entry.source_id, entry.error or "Error de torrent")
        except Exception:
            log.exception("on_error callback failed for %s", entry.source_id)

    # ── adding / metadata ──────────────────────────────────────────────────────

    def add_torrent_data(self, source_id: str, data: bytes, name: str | None = None) -> dict:
        """Add a torrent from the raw contents of a .torrent file."""
        if not data:
            raise TorrentError("El archivo .torrent está vacío")
        try:
            info = lt.torrent_info(data)
        except Exception as exc:
            raise TorrentError(f"Archivo .torrent inválido: {exc}") from exc
        torrent_path = self.meta_path(source_id)
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = torrent_path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(torrent_path)
        return self._add(source_id, info=info, name=name or info.name(), torrent_data=data)

    def add_magnet(self, source_id: str, magnet: str, name: str | None = None) -> dict:
        """Add a torrent from a magnet URI (metadata arrives asynchronously)."""
        magnet = (magnet or "").strip()
        if not magnet.lower().startswith("magnet:?"):
            raise TorrentError("Enlace magnet inválido")
        return self._add(source_id, magnet=magnet, name=name)

    def _add(
        self,
        source_id: str,
        *,
        info=None,
        magnet: str | None = None,
        name: str | None = None,
        torrent_data: bytes | None = None,
    ) -> dict:
        session = self._ensure_session()
        with self._lock:
            existing = self._entries.get(source_id)
            if existing is not None:
                return self.descriptor(source_id) or {}

        save_path = self.data_path(source_id)
        save_path.mkdir(parents=True, exist_ok=True)
        torrent_path = self.meta_path(source_id)
        resume_path = self.fastresume_path(source_id)

        params = None
        if resume_path.exists():
            try:
                params = lt.read_resume_data(resume_path.read_bytes())
            except Exception as exc:
                log.warning("Fast resume inválido para %s: %s", source_id, exc)
                params = None

        if params is not None and info is not None:
            try:
                resume_info = params.ti
                if resume_info is None or resume_info.info_hash() != info.info_hash():
                    params = None
            except Exception:
                params = None

        if params is None:
            params = lt.add_torrent_params()
            if info is not None:
                params.ti = info
            else:
                params = lt.parse_magnet_uri(magnet or "")
                params.ti = None

        params.save_path = str(save_path)
        params.flags |= lt.torrent_flags.auto_managed
        params.flags |= lt.torrent_flags.sequential_download
        params.flags &= ~lt.torrent_flags.paused

        # libtorrent returns the existing handle for a duplicate info hash and
        # ignores save_path, which would make the new source point at another
        # source's files. Reject the duplicate instead.
        new_keys = self._hash_keys(params)
        if new_keys:
            with self._lock:
                for other in self._entries.values():
                    if new_keys & self._hash_keys(other.handle):
                        raise TorrentError(
                            f"Este torrent ya está añadido como «{other.name}»"
                        )

        try:
            handle = session.add_torrent(params)
        except Exception as exc:
            raise TorrentError(f"No se pudo añadir el torrent: {exc}") from exc

        entry = _Entry(
            source_id=source_id,
            handle=handle,
            save_path=save_path,
            torrent_path=torrent_path,
            resume_path=resume_path,
            magnet=magnet,
            name=name or (info.name() if info is not None else "") or source_id,
            info=info,
        )
        if torrent_data is not None and not torrent_path.exists():
            try:
                torrent_path.write_bytes(torrent_data)
            except Exception:
                pass

        with self._lock:
            self._entries[source_id] = entry

        if info is not None:
            self._prepare(entry, info)
        else:
            log.info("Torrent %s añadido; esperando metadatos", source_id)
        return self.descriptor(source_id) or {}

    def _prepare(self, entry: _Entry, info) -> None:
        """Select the video file, set priorities and announce readiness."""
        with entry.lock:
            if entry.removed:
                return
            layout = info.layout()
            best_index = None
            best_size = -1
            for index in range(layout.num_files()):
                flags = layout.file_flags(index)
                if flags & lt.file_storage.flag_pad_file:
                    continue
                rel_path = layout.file_path(index)
                if not rel_path or "/.pad/" in f"/{rel_path}" or "__MACOSX" in rel_path:
                    continue
                if Path(rel_path).suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                size = layout.file_size(index)
                if size > best_size:
                    best_index = index
                    best_size = size

            entry.info = info
            entry.name = entry.name or info.name()
            if best_index is None:
                entry.error = "El torrent no contiene ningún archivo de vídeo compatible"
                entry.file_index = None
                entry.file_size = 0
            else:
                rel_path = layout.file_path(best_index)
                resolved = (entry.save_path / rel_path).resolve()
                try:
                    root = entry.save_path.resolve()
                    if resolved != root and root not in resolved.parents:
                        raise ValueError("ruta fuera del directorio de descarga")
                except OSError as exc:
                    entry.error = f"Ruta de archivo inválida: {exc}"
                else:
                    entry.file_index = best_index
                    entry.file_path = rel_path
                    entry.file_size = best_size
                    entry.extension = Path(rel_path).suffix.lower() or ".mp4"
                    entry.error = None
                    priorities = [0] * layout.num_files()
                    priorities[best_index] = 7
                    try:
                        entry.handle.prioritize_files(priorities)
                    except Exception as exc:
                        log.warning("No se pudieron fijar prioridades en %s: %s", entry.source_id, exc)
                    self._set_initial_deadlines(entry)
                    self._write_torrent(entry)
                    entry.prepared = True

        if entry.error:
            log.warning("Torrent %s: %s", entry.source_id, entry.error)
            self._call_error(entry)
        else:
            log.info(
                "Torrent %s listo: %s (%s bytes)",
                entry.source_id, entry.file_path, entry.file_size,
            )
            self._call_ready(entry)

    def _set_initial_deadlines(self, entry: _Entry) -> None:
        if entry.info is None:
            return
        num_pieces = entry.info.num_pieces()
        if num_pieces <= 0:
            return
        first = min(FIRST_PIECE_DEADLINES, num_pieces)
        for index in range(first):
            self._set_deadline(entry, index, 800 + index * 250)
        last_start = max(first, num_pieces - LAST_PIECE_DEADLINES)
        for index in range(last_start, num_pieces):
            self._set_deadline(entry, index, 5000 + (index - last_start) * 250)

    def _set_deadline(self, entry: _Entry, piece: int, milliseconds: int) -> None:
        try:
            entry.handle.set_piece_deadline(piece, milliseconds)
        except Exception:
            pass

    def _prioritize_around(self, entry: _Entry, piece: int) -> None:
        now = time.time()
        with entry.lock:
            if piece == entry.last_prioritized and now - entry.last_priority_at < 3.0:
                return
            entry.last_prioritized = piece
            entry.last_priority_at = now
        if entry.info is None:
            return
        last = min(entry.info.num_pieces(), piece + PIECE_LOOKAHEAD)
        for index in range(piece, last):
            self._set_deadline(entry, index, 1000 + (index - piece) * 300)

    # ── resume data ────────────────────────────────────────────────────────────

    def _save_resume(self, entry: _Entry) -> None:
        session = self._session
        if session is None or entry.removed:
            return
        try:
            status = entry.handle.status()
            if not status.has_metadata:
                return
            entry.handle.save_resume_data(lt.save_resume_flags_t.only_if_modified)
        except Exception:
            return
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                session.wait_for_alert(200)
                alerts = session.pop_alerts()
            except Exception:
                return
            done = False
            for alert in alerts:
                if isinstance(alert, lt.save_resume_data_alert):
                    try:
                        if alert.handle.info_hash() != entry.handle.info_hash():
                            continue
                        data = lt.write_resume_data_buf(alert.params)
                        tmp = entry.resume_path.with_suffix(".tmp")
                        tmp.write_bytes(data)
                        tmp.replace(entry.resume_path)
                        entry.last_resume_save = time.time()
                        entry.resume_saved_progress = float(status.progress)
                        done = True
                    except Exception as exc:
                        log.debug("No se pudo guardar fast resume %s: %s", entry.source_id, exc)
                elif isinstance(alert, lt.save_resume_data_failed_alert):
                    done = True
            if done:
                return

    # ── status ─────────────────────────────────────────────────────────────────

    def get(self, source_id: str) -> _Entry | None:
        with self._lock:
            return self._entries.get(source_id)

    def descriptor(self, source_id: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(source_id)
        if entry is None:
            return None
        return self._descriptor(entry)

    def _descriptor(self, entry: _Entry) -> dict:
        result = {
            "id": entry.source_id,
            "name": entry.name,
            "ready": bool(entry.prepared and not entry.error),
            "metadata": entry.info is not None,
            "file": entry.file_path,
            "size_bytes": entry.file_size,
            "extension": entry.extension,
            "error": entry.error,
            "progress": 0.0,
            "download_rate": 0,
            "upload_rate": 0,
            "peers": 0,
            "seeds": 0,
            "state": "unknown",
            "total_done": 0,
            "total": 0,
        }
        try:
            status = entry.handle.status()
            result.update({
                "progress": float(status.progress),
                "download_rate": int(status.download_rate),
                "upload_rate": int(status.upload_rate),
                "peers": int(status.num_peers),
                "seeds": int(status.num_seeds),
                "state": str(status.state),
                "total_done": int(status.total_done),
                "total": int(status.total_wanted),
            })
            errc = getattr(status, "errc", None)
            if errc is not None and not entry.error:
                try:
                    if int(errc.value()) != 0:
                        result["error"] = errc.message()
                except Exception:
                    pass
        except Exception:
            pass
        return result

    def statuses(self) -> list[dict]:
        with self._lock:
            source_ids = list(self._entries.keys())
        return [self.descriptor(source_id) or {} for source_id in source_ids]

    # ── streaming ──────────────────────────────────────────────────────────────

    def wait_ready(self, source_id: str, timeout: float | None = None) -> _Entry:
        timeout = self.metadata_timeout if timeout is None else timeout
        deadline = time.time() + max(0.1, timeout)
        while True:
            entry = self.get(source_id)
            if entry is None:
                raise TorrentNotReady("Torrent no encontrado")
            if entry.removed:
                raise TorrentStopped("Torrent eliminado")
            if entry.error:
                raise TorrentError(entry.error)
            if entry.prepared and entry.file_index is not None:
                return entry
            if time.time() >= deadline:
                raise TorrentNotReady("Esperando metadatos del torrent")
            time.sleep(0.2)

    def stream_size(self, source_id: str, timeout: float | None = None) -> int:
        entry = self.wait_ready(source_id, timeout)
        return int(entry.file_size)

    def iter_stream(
        self,
        source_id: str,
        offset: int = 0,
        length: int | None = None,
        *,
        should_stop=None,
        timeout: float | None = None,
    ):
        """Yield bytes for a byte range, blocking until pieces are on disk."""
        entry = self.wait_ready(source_id, timeout)
        total = int(entry.file_size)
        start = max(0, int(offset))
        end = total if length is None else min(total, start + max(0, int(length)))
        timeout = self.piece_timeout if timeout is None else timeout
        path = entry.absolute_path
        handle = None
        try:
            while start < end:
                if entry.removed:
                    raise TorrentStopped("Torrent eliminado")
                if should_stop and should_stop():
                    return
                info = entry.info
                if info is None:
                    raise TorrentNotReady("Metadatos no disponibles")
                request = info.map_file(entry.file_index, start, 1)
                piece = int(request.piece)
                piece_offset = int(request.start)
                piece_length = int(info.piece_length())
                chunk_len = min(READ_CHUNK_SIZE, end - start, piece_length - piece_offset)
                if chunk_len <= 0:
                    raise TorrentError("Rango de lectura inválido")
                self._wait_piece(entry, piece, timeout, should_stop)
                if handle is None:
                    handle = open(path, "rb")
                handle.seek(start)
                data = handle.read(chunk_len)
                if not data:
                    raise TorrentError("No se pudieron leer datos del archivo descargado")
                start += len(data)
                yield data
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    def _wait_piece(self, entry: _Entry, piece: int, timeout: float, should_stop) -> None:
        if entry.removed:
            raise TorrentStopped("Torrent eliminado")
        if entry.handle.is_valid() and entry.handle.have_piece(piece):
            self._prioritize_around(entry, piece)
            return
        self._prioritize_around(entry, piece)
        deadline = time.time() + max(0.5, timeout)
        while True:
            if entry.removed:
                raise TorrentStopped("Torrent eliminado")
            if should_stop and should_stop():
                raise TorrentStopped("Cambio de fuente")
            try:
                if entry.handle.is_valid() and entry.handle.have_piece(piece):
                    return
            except Exception as exc:
                raise TorrentError(f"Torrent no disponible: {exc}") from exc
            if time.time() >= deadline:
                raise TorrentError(
                    f"Tiempo de espera agotado descargando la pieza {piece}"
                )
            time.sleep(0.1)

    # ── removal / resume ───────────────────────────────────────────────────────

    def remove(self, source_id: str, delete_files: bool = True) -> bool:
        with self._lock:
            entry = self._entries.pop(source_id, None)
        if entry is None:
            self._remove_paths(source_id, delete_files)
            return False
        entry.removed = True
        try:
            if entry.handle.is_valid():
                flags = lt.session.delete_files if delete_files else 0
                self._ensure_session().remove_torrent(entry.handle, flags)
        except Exception as exc:
            log.warning("No se pudo eliminar el torrent %s: %s", source_id, exc)
        self._remove_paths(source_id, delete_files)
        return True

    def _remove_paths(self, source_id: str, delete_files: bool) -> None:
        try:
            self.meta_path(source_id).unlink(missing_ok=True)
            self.fastresume_path(source_id).unlink(missing_ok=True)
        except OSError:
            pass
        if delete_files:
            shutil.rmtree(self.data_path(source_id), ignore_errors=True)

    def resume(self, sources: list[dict]) -> int:
        """Re-add every torrent source found in the persisted playout state."""
        count = 0
        for source in sources:
            if source.get("type") != "torrent":
                continue
            source_id = source.get("id")
            if not source_id:
                continue
            try:
                torrent_path = source.get("torrent_file") or str(self.meta_path(source_id))
                if torrent_path and Path(torrent_path).is_file():
                    self.add_torrent_data(source_id, Path(torrent_path).read_bytes(), source.get("name"))
                elif source.get("magnet"):
                    self.add_magnet(source_id, source["magnet"], source.get("name"))
                else:
                    log.warning("Torrent %s sin .torrent ni magnet; no se puede reanudar", source_id)
                    continue
                count += 1
            except Exception as exc:
                log.warning("No se pudo reanudar el torrent %s: %s", source_id, exc)
        return count

    # ── monitor ────────────────────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                entries = list(self._entries.values())
            for entry in entries:
                if entry.removed:
                    continue
                try:
                    if entry.info is None:
                        status = entry.handle.status()
                        if status.has_metadata:
                            info = entry.handle.torrent_file()
                            if info is not None:
                                self._prepare(entry, info)
                    elif (
                        entry.prepared
                        and now - entry.last_resume_save > RESUME_INTERVAL_SECONDS
                    ):
                        self._save_resume(entry)
                except Exception:
                    log.debug("Monitor torrent %s: error", entry.source_id, exc_info=True)
            self._stop.wait(2.0)


__all__ = [
    "ALLOWED_TORRENT_EXTENSIONS",
    "TorrentError",
    "TorrentManager",
    "TorrentNotReady",
    "TorrentStopped",
    "VIDEO_EXTENSIONS",
]
