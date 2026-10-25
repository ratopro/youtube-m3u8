import hashlib
import hmac
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import shutil
import select
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
from xml.sax.saxutils import escape

import requests
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for

log = logging.getLogger("youtube_hls.web")

from src.playout import PlayoutEngine
from src.torrent import (
    ALLOWED_TORRENT_EXTENSIONS,
    TorrentError,
    TorrentManager,
    TorrentNotReady,
    TorrentStopped,
    inspect_magnet,
    inspect_torrent_data,
)
from src.youtube_extractor import StreamExtractor


def is_direct_stream_url(url: str) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return any(lowered.endswith(ext) for ext in (".m3u8", ".ts", ".mp4", ".mkv", ".avi", ".mov", ".m4v"))


ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi"}
TS_COMPATIBLE_VIDEO_CODECS = {"h264", "hevc", "mpeg2video", "mpeg1video"}
TS_COMPATIBLE_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "mp2"}


def format_duration_label(seconds: float | int | None) -> str:
    if not seconds or seconds <= 0:
        return "Directo"
    total = int(round(float(seconds)))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_byte_range(header_value: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single HTTP Range header. Return (start, end) or None if absent."""
    if not header_value:
        return None
    match = re.match(r"^\s*bytes\s*=\s*(\d*)-(\d*)\s*$", header_value)
    if not match:
        raise ValueError("Rango no soportado")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("Rango vacío")
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    else:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("Rango vacío")
        start = max(0, size - suffix)
        end = size - 1
    if size <= 0 or start < 0 or start >= size or end < start:
        raise ValueError("Rango fuera del archivo")
    return start, min(end, size - 1)


def probe_video_file(path: Path) -> dict:
    """Read duration/codecs from a local video file using ffprobe."""
    return probe_video_target(str(path), timeout=30)


def probe_video_url(url: str, timeout: int = 120) -> dict:
    """Read duration/codecs from an HTTP media URL using ffprobe."""
    return probe_video_target(url, timeout=timeout, rw_timeout_us=timeout * 1_000_000)


def probe_video_target(target: str, timeout: int = 30, rw_timeout_us: int | None = None) -> dict:
    """Run ffprobe on a local file or URL and normalise the metadata."""
    cmd = ["ffprobe", "-v", "error"]
    if rw_timeout_us:
        cmd.extend(["-rw_timeout", str(rw_timeout_us)])
    cmd.extend([
        "-print_format", "json",
        "-show_format", "-show_streams",
        target,
    ])
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise ValueError(detail[-1] if detail else "ffprobe no pudo leer el archivo")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"ffprobe devolvio datos invalidos: {exc}") from exc

    streams = data.get("streams") or []
    video = next(
        (
            s for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if not video:
        raise ValueError("El archivo no contiene ninguna pista de video")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        try:
            duration = float(video.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0

    return {
        "duration_seconds": int(duration) if duration > 0 else None,
        "duration_label": format_duration_label(duration),
        "is_live": False,
        "video_codec": (video.get("codec_name") or "").lower() or None,
        "audio_codec": ((audio or {}).get("codec_name") or "").lower() or None,
        "width": video.get("width"),
        "height": video.get("height"),
        "audio_probed": True,
    }


def build_file_pump_command(path: Path, video_codec: str | None, audio_codec: str | None) -> list[str]:
    """Build an ffmpeg command that loops a local file as MPEG-TS on stdout.

    The program pipeline already re-encodes the stream, so compatible codecs are
    copied and only unsupported ones (common in AVI/MKV) are transcoded.  Files
    without an audio track get silent audio so the program pipeline keeps a
    stable stream layout even when it restarts on a video-only source.
    """
    silent_audio = audio_codec is None
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-re", "-stream_loop", "-1",
        "-i", str(path),
    ]
    if silent_audio:
        cmd.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
    cmd.extend(["-map", "0:v:0", "-map", "1:a:0" if silent_audio else "0:a:0?"])
    if (video_codec or "").lower() in TS_COMPATIBLE_VIDEO_CODECS:
        cmd.extend(["-c:v", "copy"])
    else:
        cmd.extend([
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", "50",
        ])
    if silent_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"])
    elif (audio_codec or "").lower() in TS_COMPATIBLE_AUDIO_CODECS:
        cmd.extend(["-c:a", "copy"])
    else:
        cmd.extend(["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"])
    cmd.extend([
        "-avoid_negative_ts", "make_zero",
        "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-",
    ])
    return cmd


youtube_hls_cache: dict[str, tuple[float, str]] = {}
youtube_hls_ttl_seconds = int(os.environ.get("YOUTUBE_HLS_TTL_SECONDS", "900"))


nvenc_available: bool | None = None
video_accel = "cpu"
processed_video_encoder = "libx264"
presentation_video_encoder = "libx264"


def _hls_url_for_source(url: str, source_type: str) -> str | None:
    if source_type == "file":
        return url or None
    if source_type in ("iptv", "hls") or is_direct_stream_url(url):
        return url
    if source_type == "youtube":
        now = time.time()
        cached = youtube_hls_cache.get(url)
        if cached and now - cached[0] < youtube_hls_ttl_seconds:
            return cached[1]
        try:
            hls_url = StreamExtractor(url).get_hls_url()
        except Exception:
            return None
        youtube_hls_cache[url] = (now, hls_url)
        return hls_url
    try:
        return StreamExtractor(url).get_hls_url()
    except Exception:
        return None


def warm_youtube_hls(source: dict) -> None:
    if source.get("type") != "youtube":
        return
    url = source.get("url", "")
    if not url or is_direct_stream_url(url):
        return

    def _warm():
        _hls_url_for_source(url, "youtube")

    threading.Thread(target=_warm, daemon=True).start()


class GStreamerEngine:
    def __init__(self, exe: str, hls_dir: Path, env: dict | None = None):
        self.exe = exe
        self.hls_dir = hls_dir
        self.env = env or {}
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            self.hls_dir.mkdir(parents=True, exist_ok=True)
            child_env = os.environ.copy()
            child_env.update(self.env)
            child_env.update({
                "GSTREAMER_HLS_DIR": str(self.hls_dir),
                "PROGRAM_HLS_DIR": str(self.hls_dir),
            })
            self.proc = subprocess.Popen(
                [self.exe],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=child_env,
            )
            threading.Thread(target=self._drain_stderr, daemon=True).start()

    def command(self, cmd: str, timeout: float = 5.0) -> str:
        with self._lock:
            if not self.proc or self.proc.poll() is not None:
                self.start()
            assert self.proc and self.proc.stdin and self.proc.stdout
            try:
                self.proc.stdin.write(cmd + "\n")
                self.proc.stdin.flush()
                return self.proc.stdout.readline().strip()
            except Exception as exc:
                return f'{{"status":"error","error":"{str(exc)}"}}'

    def set_black(self) -> str:
        return self.command("BLACK")

    def switch(self, playlist_name: str) -> str:
        return self.command("SWITCH " + playlist_name)

    def status(self) -> str:
        return self.command("STATUS")

    def stop(self) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.write("STOP\n")
                    self.proc.stdin.flush()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    kill_process(self.proc)
            self.proc = None

    def _drain_stderr(self) -> None:
        try:
            assert self.proc and self.proc.stderr
            for line in self.proc.stderr:
                line = line.strip()
                if line:
                    log.info("gstreamer_engine: %s", line)
        except Exception:
            pass


def _check_and_refresh_source_url(source: dict) -> bool:
    provider_id = source.get("iptv_provider")
    stream_id = source.get("iptv_stream_id")
    if not provider_id or not stream_id:
        return False
    provider = get_provider(provider_id)
    if not provider:
        return False
    current_url = xtream_live_url(provider, int(stream_id))
    if current_url == source.get("url"):
        return False
    log.info(
        "URL rotated for %s: refreshing from %s -> %s",
        source["name"],
        redact_url(source.get("url", ""))[:80],
        redact_url(current_url)[:80],
    )
    playout.update_source(source["id"], {"url": current_url})
    return True


def _check_nvenc() -> bool:
    global nvenc_available
    if nvenc_available is not None:
        return nvenc_available
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        nvenc_available = "h264_nvenc" in r.stdout
    except Exception:
        nvenc_available = False
    return nvenc_available


def _gpu_status() -> dict:
    has_cuda_devices = Path("/dev/nvidia0").exists()
    has_nvenc = _check_nvenc()
    nvidia_smi = None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            nvidia_smi = r.stdout.strip()
    except Exception:
        pass
    active_processed = _resolve_encoder(processed_video_encoder)
    active_presentation = _resolve_encoder(presentation_video_encoder)
    return {
        "video_accel": video_accel,
        "has_cuda_devices": has_cuda_devices,
        "nvenc_available": has_nvenc,
        "nvidia_smi": nvidia_smi,
        "processed_encoder": processed_video_encoder,
        "presentation_encoder": presentation_video_encoder,
        "active_processed_encoder": active_processed,
        "active_presentation_encoder": active_presentation,
        "encoder_mode": "hw" if active_processed == "h264_nvenc" else "sw",
    }


def _resolve_encoder(requested_encoder: str) -> str:
    if requested_encoder == "h264_nvenc" and _check_nvenc() and Path("/dev/nvidia0").exists():
        return "h264_nvenc"
    return "libx264"


def _build_video_encoder_args(encoder: str, bitrate: str, maxrate: str, bufsize: str, preset: str) -> list[str]:
    if encoder == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-rc", "cbr",
            "-b:v", bitrate,
            "-maxrate", maxrate,
            "-bufsize", bufsize,
            "-profile:v", "high",
            "-pix_fmt", "yuv420p",
        ]
    return [
        "-c:v", "libx264",
        "-preset", preset,
        "-profile:v", "main",
        "-level", "4.1",
        "-pix_fmt", "yuv420p",
        "-b:v", bitrate,
        "-maxrate", maxrate,
        "-bufsize", bufsize,
    ]


from src.football_api import get_today_matches
from src.iptv_importer import (
    add_provider,
    channels_for_category,
    check_url,
    delete_provider,
    ensure_xtream_xmltv_cache,
    parse_epg_file,
    build_xmltv_index,
    resolve_channel_xmltv_id,
    channel_now_next,
    get_providers,
    get_provider,
    slugify_provider_id,
    xtream_live_url,
    start_daily_scheduler,
)
from src import epg_store
from src.timeutils import now, now_date, now_hm, now_hms, now_iso
from src.ffmpeg_supervisor import (
    STALL_TIMEOUT_SEC,
    STARTUP_GRACE_SEC,
    Supervisor,
    SupervisorConfig,
    cleanup_orphans,
    kill_process,
)


def _cleanup_orphan_segments(hls_dir: Path, max_age_minutes: int = 10) -> None:
    cutoff = time.time() - max_age_minutes * 60
    for f in hls_dir.glob("seg_*.ts"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except OSError:
            pass

def _next_segment_number(hls_dir: Path) -> int:
    last_num = 0
    for f in hls_dir.glob("seg_*.ts"):
        try:
            num = int(f.stem.split("_")[1])
            if num > last_num:
                last_num = num
        except (IndexError, ValueError):
            pass
    return last_num + 1

def no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _source_group_for(src: dict) -> str:
    group = (src.get("iptv_group") or "").strip()
    if group:
        return group
    src_type = (src.get("type") or "otros").lower()
    if src_type == "iptv":
        return "Otros IPTV"
    if src_type == "hls":
        return "HLS directo"
    if src_type == "youtube":
        return "YouTube"
    if src_type == "presentation":
        return "Presentacion"
    if src_type == "football":
        return "Futbol"
    if src_type == "file":
        return "Archivos"
    if src_type == "torrent":
        return "Torrents"
    return "Otros"


def proxied_url(url: str, stream_id: int | None = None, kind: str = "segment", signing_key: str | None = None) -> str:
    sid = f"&sid={stream_id}" if stream_id is not None else ""
    endpoint = {
        "playlist": "/p/playlist.m3u8",
        "media": "/p/media.mp4",
        "segment": "/p/segment.ts",
    }.get(kind, "/p/segment.ts")
    sig = ""
    if signing_key:
        payload = f"{stream_id or ''}\n{kind}\n{url}"
        digest = hmac.new(signing_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        sig = f"&sig={digest}"
    path = f"{endpoint}?url={quote(url, safe='')}{sid}{sig}"
    return absolute_url(path)


def rewrite_playlist(content: str, base_url: str, live_window_segments: int = 0, stream_id: int | None = None, signing_key: str | None = None) -> str:
    if "#EXTINF" in content and live_window_segments > 0:
        return rewrite_media_playlist(content, base_url, live_window_segments, stream_id, signing_key)

    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue

        absolute_url = urljoin(base_url, stripped)
        lines.append(proxied_url(absolute_url, stream_id, "playlist", signing_key))

    return "\n".join(lines) + "\n"


def rewrite_media_playlist(content: str, base_url: str, live_window_segments: int, stream_id: int | None, signing_key: str | None = None) -> str:
    header = []
    blocks = []
    current_block = []
    seen_extinf = False
    media_sequence_index = None
    media_sequence_value = None

    for line in content.splitlines():
        stripped = line.strip()

        if not seen_extinf and not stripped.startswith("#EXTINF"):
            if stripped.startswith("#EXT-X-PROGRAM-DATE-TIME"):
                current_block.append(line)
                continue

            if stripped.startswith("#EXT-X-MEDIA-SEQUENCE"):
                media_sequence_index = len(header)
                try:
                    media_sequence_value = int(stripped.split(":", 1)[1])
                except (IndexError, ValueError):
                    media_sequence_value = None
            header.append(line)
            continue

        if stripped.startswith("#EXTINF"):
            seen_extinf = True
            current_block.append(line)
            continue

        if not seen_extinf:
            header.append(line)
            continue

        if stripped.startswith("#") or not stripped:
            current_block.append(line)
            continue

        absolute_url = urljoin(base_url, stripped)
        current_block.append(proxied_url(absolute_url, stream_id, "segment", signing_key))
        blocks.append(current_block)
        current_block = []

    if not blocks:
        return rewrite_playlist(content, base_url, live_window_segments=0, stream_id=stream_id, signing_key=signing_key)

    removed_blocks = max(0, len(blocks) - live_window_segments)
    blocks = blocks[-live_window_segments:]

    if media_sequence_index is not None and media_sequence_value is not None:
        header[media_sequence_index] = f"#EXT-X-MEDIA-SEQUENCE:{media_sequence_value + removed_blocks}"

    lines = header
    for block in blocks:
        lines.extend(block)

    return "\n".join(lines) + "\n"


def best_variant_url(content: str, base_url: str) -> str | None:
    variants = []
    pending_info = None

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-STREAM-INF"):
            bandwidth_match = re.search(r"BANDWIDTH=(\d+)", stripped)
            resolution_match = re.search(r"RESOLUTION=(\d+)x(\d+)", stripped)
            pending_info = {
                "bandwidth": int(bandwidth_match.group(1)) if bandwidth_match else 0,
                "height": int(resolution_match.group(2)) if resolution_match else 0,
            }
            continue

        if pending_info and stripped and not stripped.startswith("#"):
            variants.append({
                **pending_info,
                "url": urljoin(base_url, stripped),
            })
            pending_info = None

    if not variants:
        return None

    best = max(variants, key=lambda item: (item["height"], item["bandwidth"]))
    return best["url"]


def direct_media_playlist(media_url: str) -> str:
    return "\n".join([
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:4",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXTINF:4.0,YouTube media",
        media_url,
        "",
    ])


def direct_file_playlist(file_url: str) -> str:
    return "\n".join([
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        "#EXTINF:0,Presentation media",
        file_url,
        "",
    ])


def presentation_loop_playlist(file_url: str, loop_count: int) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for _ in range(max(1, loop_count)):
        lines.append("#EXTINF:0,Presentation loop")
        lines.append(file_url)
    lines.append("#EXT-X-ENDLIST")
    lines.append("")
    return "\n".join(lines)


def absolute_url(path: str) -> str:
    return urljoin(request.host_url, path.lstrip("/"))


def proxy_signature(url: str, stream_id: int | None, kind: str, signing_key: str) -> str:
    payload = f"{stream_id or ''}\n{kind}\n{url}"
    return hmac.new(signing_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def is_public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        host = info[4][0]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def private_source_urls_allowed() -> bool:
    return os.environ.get("ALLOW_PRIVATE_SOURCE_URLS", "0").lower() in {"1", "true", "yes", "on"}


def is_allowed_source_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return private_source_urls_allowed() or is_public_http_url(url)


def redact_url(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"(/live/)([^/]+)(/)([^/]+)(/)", r"\1***\3***\5", value)


def public_source(source: dict) -> dict:
    item = dict(source)
    if "url" in item:
        item["url"] = redact_url(item.get("url"))
    return item


def public_provider(provider: dict) -> dict:
    item = dict(provider)
    if "username" in item:
        item["username"] = "***"
    if "password" in item:
        item["password"] = "***"
    return item


class SegmentCache:
    def __init__(self, cache_dir: str, ttl_seconds: int, max_mb: int, max_object_mb: int):
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_mb * 1024 * 1024
        self.max_object_bytes = max_object_mb * 1024 * 1024
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, url: str) -> Path:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / key

    def get(self, url: str) -> Path | None:
        path = self.path_for(url)
        if not path.exists():
            return None

        age = int(os.path.getmtime(path))
        if self.ttl_seconds > 0 and (int(time.time()) - age) > self.ttl_seconds:
            path.unlink(missing_ok=True)
            return None

        return path

    def should_cache(self, content_length: str | None) -> bool:
        if not content_length:
            return True

        try:
            return int(content_length) <= self.max_object_bytes
        except ValueError:
            return True

    def put(self, url: str, upstream: requests.Response) -> Path:
        self.cleanup()
        path = self.path_for(url)
        temp_path = path.with_suffix(".tmp")

        written = 0
        with temp_path.open("wb") as handle:
            for chunk in upstream.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue

                written += len(chunk)
                if written > self.max_object_bytes:
                    handle.close()
                    temp_path.unlink(missing_ok=True)
                    raise ValueError("Segmento demasiado grande para cachear")

                handle.write(chunk)

        temp_path.replace(path)
        return path

    def cleanup(self) -> None:
        files = [item for item in self.cache_dir.iterdir() if item.is_file() and not item.name.endswith(".tmp")]
        total = sum(item.stat().st_size for item in files)
        if total <= self.max_bytes:
            return

        for item in sorted(files, key=lambda file: file.stat().st_mtime):
            total -= item.stat().st_size
            item.unlink(missing_ok=True)
            if total <= self.max_bytes:
                break

    def clear(self) -> None:
        for item in self.cache_dir.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)


def create_app(hls_dir: str = "output/hls", upstream_hls_url: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="../templates")
    max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "8192"))
    app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 1024 * 1024

    # ── Auth helpers ───────────────────────────────────────────────────────────
    ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

    def admin_token_enabled() -> bool:
        return bool(ADMIN_TOKEN)

    PUBLIC_PATH_PREFIXES = (
        "/emby/",
        "/program/live/",
        "/processed/live/",
        "/preview/live/",
        "/presentation/live/",
        "/channels",
        "/guide",
        "/static",
    )

    PUBLIC_PATHS = {
        "/health",
        "/api/version",
        "/api/gpu/status",
    }

    SENSITIVE_GET_PATHS = {
        "/api/logs",
        "/api/sources",
        "/api/torrents/status",
        "/api/playout/status",
        "/api/history",
        "/api/iptv/providers",
    }

    def is_public_path(path: str) -> bool:
        if path in PUBLIC_PATHS:
            return True
        for prefix in PUBLIC_PATH_PREFIXES:
            if path.startswith(prefix):
                return True
        return False

    def request_needs_auth() -> bool:
        if not admin_token_enabled():
            return False
        if is_public_path(request.path):
            return False
        if request.method == "GET" and request.path not in SENSITIVE_GET_PATHS:
            return False
        return True

    def provided_admin_token() -> str:
        return request.headers.get("X-Admin-Token", "") or request.cookies.get("admin_token", "")

    @app.before_request
    def require_admin_token():
        if not request_needs_auth():
            return None
        if not secrets.compare_digest(provided_admin_token(), ADMIN_TOKEN):
            resp = jsonify({"ok": False, "error": "No autorizado"})
            resp.status_code = 401
            return resp

    @app.after_request
    def add_security_headers(response):
        response.headers.set("X-Content-Type-Options", "nosniff")
        response.headers.set("Referrer-Policy", "no-referrer")
        response.headers.set("X-Frame-Options", "SAMEORIGIN")
        response.headers.set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        no_store(response)
        return response

    @app.errorhandler(413)
    def upload_too_large(_error):
        return jsonify({
            "ok": False,
            "error": f"El archivo supera el limite de {max_upload_mb} MB",
        }), 413

    @app.route("/login", methods=["POST"])
    def login():
        token = request.form.get("token", "").strip()
        if not admin_token_enabled():
            return jsonify({"ok": False, "error": "Auth no configurada"}), 400
        if not secrets.compare_digest(token, ADMIN_TOKEN):
            return jsonify({"ok": False, "error": "Token invalido"}), 401
        resp = jsonify({"ok": True})
        resp.set_cookie(
            "admin_token",
            token,
            httponly=True,
            samesite="Lax",
            max_age=86400 * 7,
        )
        return resp

    @app.route("/logout", methods=["POST"])
    def logout():
        resp = jsonify({"ok": True})
        resp.delete_cookie("admin_token")
        return resp

    # ── End auth helpers ────────────────────────────────────────────────────────

    presentation_video = Path(__file__).resolve().parent.parent / "sofa.mp4"
    media_dir = Path(os.environ.get("MEDIA_DIR", "data/media"))
    torrent_dir = Path(os.environ.get("TORRENT_DIR", "data/torrents"))
    torrent_listen_port = int(os.environ.get("TORRENT_LISTEN_PORT", "6881"))
    torrent_piece_timeout = float(os.environ.get("TORRENT_PIECE_TIMEOUT", "90"))
    torrent_metadata_timeout = float(os.environ.get("TORRENT_METADATA_TIMEOUT", "60"))
    internal_base_url = os.environ.get("INTERNAL_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
    presentation_hls_dir = Path(os.environ.get("PRESENTATION_HLS_DIR", "/tmp/presentation-hls"))
    preview_hls_dir = Path(os.environ.get("PREVIEW_HLS_DIR", "/tmp/preview-hls"))
    processed_hls_dir = Path(os.environ.get("PROCESSED_HLS_DIR", "/tmp/processed-hls"))
    program_hls_dir = Path(os.environ.get("PROGRAM_HLS_DIR", "/tmp/program-hls"))
    program_fallback_video = Path(os.environ.get("PROGRAM_FALLBACK_VIDEO", "/app/data/fallback.mp4"))
    segment_cache = SegmentCache(
        cache_dir=os.environ.get("CACHE_DIR", "/tmp/youtube-hls-cache"),
        ttl_seconds=int(os.environ.get("CACHE_TTL_SECONDS", "1800")),
        max_mb=int(os.environ.get("CACHE_MAX_MB", "512")),
        max_object_mb=int(os.environ.get("CACHE_MAX_OBJECT_MB", "32")),
    )
    live_window_segments = int(os.environ.get("LIVE_WINDOW_SEGMENTS", "30"))
    presentation_loop_count = int(os.environ.get("PRESENTATION_LOOP_COUNT", "1000"))
    presentation_hls_list_size = int(os.environ.get("PRESENTATION_HLS_LIST_SIZE", "24"))
    presentation_hls_delete_threshold = int(os.environ.get("PRESENTATION_HLS_DELETE_THRESHOLD", "12"))
    app_version = os.environ.get("APP_VERSION", "dev")
    build_commit = os.environ.get("APP_COMMIT", "")
    build_date = os.environ.get("APP_BUILD_DATE", "")
    auto_presentation_on_end = os.environ.get("AUTO_PRESENTATION_ON_END", "1") == "1"
    processed_enabled = os.environ.get("PROCESSED_ENABLED", "1") == "1"
    processed_delay_seconds = int(os.environ.get("PROCESSED_DELAY_SECONDS", "60"))
    processed_segment_seconds = int(os.environ.get("PROCESSED_SEGMENT_SECONDS", "2"))
    use_gstreamer_engine = os.environ.get("USE_GSTREAMER_ENGINE", "0") == "1"
    gstreamer_engine: GStreamerEngine | None = None
    processed_list_size = int(os.environ.get("PROCESSED_LIST_SIZE", "45"))
    processed_extra_segments = int(os.environ.get("PROCESSED_EXTRA_SEGMENTS", "8"))
    processed_video_bitrate = os.environ.get("PROCESSED_VIDEO_BITRATE", "6000k")
    processed_video_maxrate = os.environ.get("PROCESSED_VIDEO_MAXRATE", "7000k")
    processed_video_bufsize = os.environ.get("PROCESSED_VIDEO_BUFSIZE", "12000k")
    processed_audio_bitrate = os.environ.get("PROCESSED_AUDIO_BITRATE", "160k")
    processed_preset = os.environ.get("PROCESSED_PRESET", "veryfast")
    processed_height = int(os.environ.get("PROCESSED_HEIGHT", "1080"))
    processed_width = int(os.environ.get("PROCESSED_WIDTH", str(processed_height * 16 // 9)))
    processed_startup_wait_seconds = int(os.environ.get("PROCESSED_STARTUP_WAIT_SECONDS", "30"))
    proxy_signing_key = os.environ.get("PROXY_SIGNING_KEY") or secrets.token_urlsafe(32)
    allow_unsigned_proxy = os.environ.get("ALLOW_UNSIGNED_PROXY", "0") == "1"
    epg_store.init_db()
    processed_delay_segments = max(1, math.ceil(processed_delay_seconds / max(1, processed_segment_seconds)))
    processed_effective_list_size = max(processed_list_size, processed_delay_segments + processed_extra_segments)
    processed_delete_threshold = max(4, processed_extra_segments)
    program_hls_list_size = max(processed_effective_list_size, 45)
    program_hls_delete_threshold = max(processed_delete_threshold, 8)
    global video_accel, processed_video_encoder, presentation_video_encoder, nvenc_available
    video_accel = os.environ.get("VIDEO_ACCEL", "cpu")
    processed_video_encoder = os.environ.get("PROCESSED_VIDEO_ENCODER", "libx264")
    presentation_video_encoder = os.environ.get("PRESENTATION_VIDEO_ENCODER", "libx264")
    nvenc_available = None
    program_lock = threading.RLock()
    file_pump_lock = threading.RLock()
    fallback_pump_lock = threading.RLock()
    stream_state = {
        "mode": "youtube" if upstream_hls_url else None,
        "source_url": None,
        "source_url_name": None,
        "upstream_hls_url": upstream_hls_url,
        "stream_id": 1 if upstream_hls_url else 0,
        "error": None,
        "presentation_proc": None,
        "preview_proc": None,
        "preview_stream_id": None,
        "processed_proc": None,
        "processed_stream_id": None,
        "program_proc": None,
        "program_stream_id": None,
        "program_error": None,
        "program_fallback": False,
        "program_source_proc": None,
        "program_source_url": None,
        "program_source_error": None,
        "program_upstream_url": None,
        "processed_error": None,
        "iptv_channels": [],
        "iptv_url": None,
        "current_source_id": None,
        "current_reason": None,
        "current_calendar_id": None,
        "pending_source": None,
        "last_previewed_source_id": None,
        "logs": {"preview": [], "processed": [], "presentation": [], "program": []},
        "relay_source_version": 0,
        "relay_source_config": None,
        "_fallback_pump": None,
        "_file_pump": None,
        "_file_pump_key": None,
        "_file_pump_started": 0.0,
    }

    MAX_LOG_LINES = 100

    def _drain_stderr(proc: subprocess.Popen, key: str) -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    buf = stream_state["logs"][key]
                    buf.append(line)
                    if len(buf) > MAX_LOG_LINES:
                        buf.pop(0)
        except Exception:
            pass

    def stop_presentation_stream() -> None:
        kill_process(stream_state.get("presentation_proc"))
        stream_state["presentation_proc"] = None

    def stop_preview_stream() -> None:
        kill_process(stream_state.get("preview_proc"))
        stream_state["preview_proc"] = None
        stream_state["preview_stream_id"] = None

    def start_preview_stream(input_url: str) -> None:
        if use_gstreamer_engine:
            return
        proc = stream_state.get("preview_proc")
        current_url = stream_state.get("preview_url")
        if current_url == input_url and proc is not None and proc.poll() is None:
            return
        stop_preview_stream()
        preview_hls_dir.mkdir(parents=True, exist_ok=True)

        output_playlist = preview_hls_dir / "live.m3u8"
        segment_pattern = preview_hls_dir / "seg_%06d.ts"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts",
            "-rw_timeout",
            "15000000",
            "-i",
            input_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "8",
            "-hls_flags",
            "delete_segments+append_list+omit_endlist+independent_segments",
            "-hls_segment_filename",
            str(segment_pattern),
            str(output_playlist),
        ]
        stream_state["preview_proc"] = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        stream_state["preview_stream_id"] = stream_state["stream_id"]
        stream_state["preview_url"] = input_url
        threading.Thread(target=_drain_stderr, args=(stream_state["preview_proc"], "preview"), daemon=True).start()

    def stop_processed_stream() -> None:
        kill_process(stream_state.get("processed_proc"))
        stream_state["processed_proc"] = None
        stream_state["processed_stream_id"] = None

    def start_processed_stream(input_url: str) -> None:
        if not processed_enabled:
            return
        stop_processed_stream()
        processed_hls_dir.mkdir(parents=True, exist_ok=True)
        for item in processed_hls_dir.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)

        output_playlist = processed_hls_dir / "live.m3u8"
        segment_pattern = processed_hls_dir / "seg_%06d.ts"
        scale_filter = f"scale=w=-2:h={processed_height}:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2"
        use_encoder = _resolve_encoder(processed_video_encoder)
        encoder_args = _build_video_encoder_args(
            use_encoder, processed_video_bitrate, processed_video_maxrate, processed_video_bufsize,
            processed_preset,
        )
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-rw_timeout",
            "15000000",
            "-i",
            input_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            scale_filter,
        ]
        cmd.extend(encoder_args)
        if use_encoder != "h264_nvenc":
            cmd.extend(["-r", "25"])
        cmd.extend([
            "-g",
            "50",
            "-keyint_min",
            "50",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            processed_audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "hls",
            "-hls_time",
            str(processed_segment_seconds),
            "-hls_list_size",
            str(processed_effective_list_size),
            "-hls_delete_threshold",
            str(processed_delete_threshold),
            "-hls_flags",
            "delete_segments+append_list+omit_endlist+independent_segments+program_date_time",
            "-hls_segment_type",
            "mpegts",
            "-hls_segment_filename",
            str(segment_pattern),
            str(output_playlist),
        ])
        try:
            stream_state["processed_proc"] = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        except Exception as exc:
            stream_state["processed_error"] = f"No se pudo iniciar ffmpeg: {exc}"
            return
        stream_state["processed_stream_id"] = stream_state["stream_id"]
        stream_state["processed_url"] = input_url
        stream_state["processed_error"] = None
        threading.Thread(target=_drain_stderr, args=(stream_state["processed_proc"], "processed"), daemon=True).start()

    def generate_fallback_video() -> bool:
        if not program_fallback_video.parent.exists():
            program_fallback_video.parent.mkdir(parents=True, exist_ok=True)
        if program_fallback_video.exists():
            import subprocess as _sub
            probe = _sub.run(
                ["ffprobe", "-hide_banner", str(program_fallback_video)],
                capture_output=True, timeout=10,
            )
            if probe.returncode == 0:
                return True
            program_fallback_video.unlink()
        try:
            import subprocess as _sub
            use_encoder = _resolve_encoder(processed_video_encoder)
            if use_encoder == "h264_nvenc":
                enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "cbr", "-b:v", "2000k", "-maxrate", "2500k", "-bufsize", "5000k", "-profile:v", "main", "-pix_fmt", "yuv420p"]
            else:
                enc = ["-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "main", "-pix_fmt", "yuv420p", "-b:v", "2000k", "-maxrate", "2500k", "-bufsize", "5000k"]
            font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            clock_filter = ("drawtext=fontfile=" + font_path + ":"
                           "text='%{localtime}':"
                           "fontcolor=white:fontsize=80:x=w-tw-80:y=h-th-80:"
                           "box=1:boxcolor=black@0.5:boxborderw=12:bordercolor=black:borderw=2")
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=size=1920x1080:rate=25:color=black:duration=30",
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000:duration=30",
                "-vf", clock_filter,
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            ] + enc + ["-t", "30", str(program_fallback_video)
            ]
            result = _sub.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and program_fallback_video.exists():
                return True
            else:
                print("ffmpeg stderr:", result.stderr[:500])
        except Exception as exc:
            print(f"Fallback video generation failed: {exc}")
        return False

    def _start_fallback_pump() -> subprocess.Popen | None:
        """Run ffmpeg that continuously loops fallback.mp4 to stdout (mpegts).
        The relay reads from this pump when no channel source is active."""
        _kill_fallback_pump()
        if not program_fallback_video.exists():
            generate_fallback_video()
        if not program_fallback_video.exists():
            stream_state["_fallback_pump"] = None
            return None
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-re",
            "-stream_loop", "-1",
            "-i", str(program_fallback_video),
            "-c", "copy",
            "-f", "mpegts",
            "-",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        stream_state["_fallback_pump"] = proc
        return proc

    def _kill_fallback_pump() -> None:
        with fallback_pump_lock:
            old = stream_state.get("_fallback_pump")
            if old and old.poll() is None:
                old.kill()
                try:
                    old.wait(timeout=5)
                except Exception:
                    pass
            stream_state["_fallback_pump"] = None

    def _ensure_fallback_pump() -> None:
        with fallback_pump_lock:
            pump = stream_state.get("_fallback_pump")
            if pump and pump.poll() is None:
                return
            _start_fallback_pump()

    def _kill_file_pump() -> None:
        with file_pump_lock:
            old = stream_state.get("_file_pump")
            if old and old.poll() is None:
                old.kill()
                try:
                    old.wait(timeout=3)
                except Exception:
                    pass
            stream_state["_file_pump"] = None
            stream_state["_file_pump_key"] = None

    def _ensure_file_pump(
        path: str,
        config: dict | None = None,
        version: int | None = None,
        remote: bool = False,
    ) -> subprocess.Popen | None:
        """Start (or reuse) an ffmpeg pump that loops a video source as MPEG-TS.

        ``remote`` sources (torrents) are HTTP URLs and cannot be probed with
        ffprobe here; their metadata comes from the source configuration.
        """
        config = config or {}
        with file_pump_lock:
            # A switch may have happened while this generator was being
            # created; never start a pump for a stale relay version.
            if version is not None and stream_state["relay_source_version"] != version:
                return None
            video_codec = config.get("video_codec")
            audio_codec = config.get("audio_codec")
            if remote:
                probed = bool(config.get("audio_probed"))
                if not probed:
                    video_codec = "unknown"
                    audio_codec = ""
                elif video_codec is None:
                    video_codec = "unknown"
            key = f"{path}|{video_codec or ''}|{audio_codec if audio_codec is not None else 'silent'}"
            pump = stream_state.get("_file_pump")
            if pump and pump.poll() is None and stream_state.get("_file_pump_key") == key:
                return pump

            last_started = float(stream_state.get("_file_pump_started") or 0)
            if pump is not None and stream_state.get("_file_pump_key") == key and time.time() - last_started < 2:
                # Avoid a tight restart loop when ffmpeg fails immediately.
                return None

            _kill_file_pump()
            if remote:
                if not path:
                    stream_state["program_source_error"] = "Fuente remota sin URL"
                    return None
                media_path = path
            else:
                media_path = Path(path)
                if not media_path.is_file():
                    stream_state["program_source_error"] = f"Archivo no encontrado: {media_path}"
                    return None
                if not video_codec or audio_codec is None:
                    try:
                        meta = probe_video_file(media_path)
                        video_codec = video_codec or meta.get("video_codec")
                        audio_codec = audio_codec or meta.get("audio_codec")
                    except Exception as exc:
                        stream_state["program_source_error"] = f"No se pudo leer el archivo: {exc}"
                        return None
            cmd = build_file_pump_command(media_path, video_codec, audio_codec)
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            except Exception as exc:
                stream_state["program_source_error"] = f"No se pudo iniciar el reproductor del archivo: {exc}"
                return None
            stream_state["_file_pump"] = proc
            stream_state["_file_pump_key"] = key
            stream_state["_file_pump_started"] = time.time()
            return proc

    def _file_chunks(version: int, path: str, config: dict | None = None):
        if stream_state["relay_source_version"] != version:
            return
        pump = _ensure_file_pump(path, config, version)
        if not pump or not pump.stdout or pump.poll() is not None:
            return
        try:
            while stream_state["relay_source_version"] == version:
                ready, _, _ = select.select([pump.stdout], [], [], 0.5)
                if not ready:
                    continue
                chunk = pump.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            with file_pump_lock:
                if stream_state["relay_source_version"] != version and stream_state.get("_file_pump") is pump:
                    _kill_file_pump()

    def _torrent_chunks(version: int, source_id: str, config: dict | None = None):
        """Relay a torrent through an ffmpeg pump so the output is loopable MPEG-TS."""
        if stream_state["relay_source_version"] != version:
            return
        config = config or {}
        try:
            size = torrent_manager.stream_size(source_id, timeout=min(15.0, torrent_metadata_timeout))
        except TorrentError as exc:
            stream_state["program_source_error"] = f"Torrent no disponible: {exc}"
            yield from _fallback_chunks(version)
            return
        if size <= 0:
            stream_state["program_source_error"] = "El torrent no tiene un archivo de video válido"
            yield from _fallback_chunks(version)
            return
        stream_url = config.get("_hls_url") or config.get("stream_url") or _torrent_stream_url(
            source_id, config.get("extension")
        )
        if not config.get("audio_probed"):
            # The background probe reads codecs from the HTTP stream; wait a
            # little so the pump can copy instead of transcoding blind.
            deadline = time.time() + 8
            source = playout.find_source_by_id(source_id)
            while (
                source is not None
                and not source.get("audio_probed")
                and time.time() < deadline
                and stream_state["relay_source_version"] == version
            ):
                time.sleep(0.25)
                source = playout.find_source_by_id(source_id)
            if source is not None and source.get("audio_probed"):
                config = {
                    **config,
                    "video_codec": source.get("video_codec"),
                    "audio_codec": source.get("audio_codec"),
                    "audio_probed": True,
                }
        _kill_fallback_pump()
        pump = _ensure_file_pump(stream_url, config, version, remote=True)
        if not pump or not pump.stdout or pump.poll() is not None:
            if stream_state["relay_source_version"] == version:
                yield from _fallback_chunks(version, max_seconds=3)
            return
        try:
            while stream_state["relay_source_version"] == version:
                ready, _, _ = select.select([pump.stdout], [], [], 0.5)
                if not ready:
                    continue
                chunk = pump.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            with file_pump_lock:
                if stream_state["relay_source_version"] != version and stream_state.get("_file_pump") is pump:
                    _kill_file_pump()

    def _set_relay_source(source: dict | None, hls_url: str | None = None) -> None:
        """Change relay source without restarting program ffmpeg.
        source=None → fallback (black+clock); source=dict → channel source.
        relay_feed handles killing the pump when IPTV is confirmed ready."""
        stream_state["relay_source_version"] += 1
        _kill_file_pump()
        if source:
            stream_state["relay_source_config"] = {**source, "_hls_url": hls_url}
        else:
            _ensure_fallback_pump()
            stream_state["relay_source_config"] = None

    def _resolve_relay_hls(config: dict) -> str | None:
        hls_url = None
        source_type = config.get("type", "iptv")
        source_id = config.get("id")
        cached_hls = config.get("_hls_url")
        if cached_hls:
            return cached_hls
        if source_type == "file":
            return config.get("path") or config.get("url") or None
        if source_type == "torrent":
            stream_url = config.get("stream_url")
            if stream_url:
                return stream_url
            if source_id:
                return _torrent_stream_url(source_id, config.get("extension"))
            return None
        if source_type in ("iptv", "hls") and source_id and playout:
            s = playout.find_source_by_id(source_id)
            if s:
                _check_and_refresh_source_url(s)
                hls_url = s["url"]
        if not hls_url:
            hls_url = _hls_url_for_source(config.get("source_url", config.get("url", "")), source_type)
        return hls_url

    def _gstreamer_source_ready() -> bool:
        switching = stream_state.get("_source_switching")
        if switching and (time.time() - stream_state.get("_source_switch_start", 0) > 90):
            switching = False
            stream_state["_source_switching"] = False
        new_playlist = program_hls_dir / "source_new.m3u8"
        if new_playlist.exists() and new_playlist.stat().st_size > 0:
            switch_start = stream_state.get("_source_switch_start", 0)
            text = new_playlist.read_text(encoding="utf-8", errors="ignore")
            seg_lines = [l.strip() for l in text.splitlines() if l.strip().endswith(".ts")]
            new_segments = [l for l in seg_lines if (program_hls_dir / l).exists() and (program_hls_dir / l).stat().st_mtime >= switch_start]
            if len(new_segments) >= 3:
                return True
        playlist_path = program_hls_dir / "source.m3u8"
        try:
            if not playlist_path.exists() or playlist_path.stat().st_size == 0:
                return False
            text = playlist_path.read_text(encoding="utf-8", errors="ignore")
            if not any(line.strip().endswith(".ts") for line in text.splitlines()):
                return False
            for segment in program_hls_dir.glob("source_*.ts"):
                if segment.stat().st_size > 0:
                    return True
        except OSError:
            return False
        return False

    def _wait_for_gstreamer_source(timeout_seconds: float) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            proc = stream_state.get("program_source_proc")
            if proc is not None and proc.poll() is not None:
                with open("/tmp/wfg_debug.txt", "w") as f:
                    f.write(f"proc died at {time.time()}\n")
                return False
            if _gstreamer_source_ready():
                with open("/tmp/wfg_debug.txt", "w") as f:
                    f.write(f"source ready at {time.time()}\n")
                return True
            time.sleep(0.2)
        with open("/tmp/wfg_debug.txt", "w") as f:
            f.write(f"timeout at {time.time()}\n")
        return False

    def _stop_gstreamer_source_stream() -> None:
        stream_state["program_source_run_id"] = int(stream_state.get("program_source_run_id") or 0) + 1
        stream_state["_source_discontinuity"] = True
        stream_state["_source_switching"] = True
        stream_state["_source_switch_start"] = time.time()
        old_proc = stream_state.get("program_source_proc")
        kill_process(old_proc)
        stream_state["program_source_proc"] = None
        stream_state["program_source_url"] = None
        stream_state["program_source_error"] = None
        stream_state["_using_source_new"] = False
        with open("/tmp/wfg_debug.txt", "a") as f:
            f.write(f"_stop_gstreamer_source_stream called, proc={old_proc}, new_switch_start={stream_state['_source_switch_start']}\n")

    def _clean_source_hls_output(force: bool = True) -> None:
        cutoff = time.time() - 10 * 60
        if not force:
            cutoff = 0
        for item in program_hls_dir.glob("source_*"):
            try:
                if item.name == "source.m3u8":
                    continue
                if item.name == "source_new.m3u8":
                    item.unlink()
                elif item.is_file() and item.suffix == ".ts" and item.stat().st_mtime >= cutoff:
                    continue
                elif item.is_file():
                    item.unlink()
            except OSError:
                pass

    def _next_source_segment_number() -> int:
        playlist_path = program_hls_dir / "source.m3u8"
        try:
            text = playlist_path.read_text(encoding="utf-8", errors="ignore")
            seq_match = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
            seq = int(seq_match.group(1)) if seq_match else 0
            segments = [line.strip() for line in text.splitlines() if line.strip().endswith(".ts")]
            if segments:
                return seq + len(segments)
            if seq:
                return seq + 1
        except OSError:
            pass
        last_num = 0
        for f in program_hls_dir.glob("source_*.ts"):
            try:
                num = int(f.stem.split("_")[1])
                if num > last_num:
                    last_num = num
            except (IndexError, ValueError):
                pass
        return last_num + 1

    def _watch_gstreamer_source_proc(proc: subprocess.Popen, run_id: int) -> None:
        proc.wait()
        with program_lock:
            if stream_state.get("program_source_run_id") != run_id:
                return
            if stream_state.get("mode") not in ("youtube", "iptv", "hls", "file", "torrent"):
                return
            stream_state["program_source_proc"] = None
            stream_state["program_source_url"] = None
            stream_state["program_source_error"] = "ffmpeg fuente termino"
            stream_state["program_error"] = stream_state["program_source_error"]
            stream_state["program_fallback"] = True
            if use_gstreamer_engine and gstreamer_engine is not None and gstreamer_engine.proc and gstreamer_engine.proc.poll() is None:
                try:
                    gstreamer_engine.switch("black.m3u8")
                except Exception as exc:
                    stream_state["program_error"] = f"ffmpeg fuente termino; no se pudo volver a negro: {exc}"

    def _start_gstreamer_source_stream(hls_url: str, loop: bool = False, silent_audio: bool = False) -> bool:
        with program_lock:
            proc = stream_state.get("program_source_proc")
            if proc is not None and proc.poll() is None and stream_state.get("program_source_url") == hls_url:
                stream_state["program_source_error"] = None
                ready = _wait_for_gstreamer_source(processed_startup_wait_seconds)
                if not ready:
                    stream_state["program_source_error"] = "No se genero source.m3u8 en el tiempo esperado"
                    stream_state["program_error"] = stream_state["program_source_error"]
                    stream_state["program_source_run_id"] = int(stream_state.get("program_source_run_id") or 0) + 1
                    kill_process(proc)
                    stream_state["program_source_proc"] = None
                return ready

            _stop_gstreamer_source_stream()
            program_hls_dir.mkdir(parents=True, exist_ok=True)
            old_seq = 0
            playlist_path = program_hls_dir / "source.m3u8"
            try:
                text = playlist_path.read_text(encoding="utf-8", errors="ignore")
                seq_match = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
                seq = int(seq_match.group(1)) if seq_match else 0
                segments = [l.strip() for l in text.splitlines() if l.strip().endswith(".ts")]
                old_seq = seq + len(segments)
            except OSError:
                pass
            _clean_source_hls_output(force=False)
            start_number = old_seq
            (program_hls_dir / "source_new.m3u8").unlink(missing_ok=True)
            output_playlist = program_hls_dir / "source_new.m3u8"
            segment_pattern = program_hls_dir / "source_%05d.ts"
            scale_filter = f"scale=w=-2:h={processed_height}:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2"
            use_encoder = _resolve_encoder(processed_video_encoder)
            encoder_args = _build_video_encoder_args(
                use_encoder, processed_video_bitrate, processed_video_maxrate, processed_video_bufsize,
                processed_preset,
            )
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel", "error",
                "-fflags", "+genpts+discardcorrupt",
                "-err_detect", "ignore_err",
            ]
            if loop:
                # Local video file: play at real time and restart at EOF.
                cmd.extend(["-re", "-stream_loop", "-1"])
            else:
                cmd.extend([
                    "-rw_timeout", "15000000",
                    "-reconnect", "1",
                    "-reconnect_at_eof", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "2",
                ])
            cmd.extend([
                "-i", hls_url,
            ])
            if silent_audio:
                cmd.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
            cmd.extend([
                "-map", "0:v:0",
                "-map", "1:a:0" if silent_audio else "0:a:0?",
                "-vf", scale_filter,
            ])
            cmd.extend(encoder_args)
            if use_encoder != "h264_nvenc":
                cmd.extend(["-r", "25"])
            cmd.extend([
                "-g", str(25 * processed_segment_seconds),
                "-keyint_min", str(25 * processed_segment_seconds),
                "-sc_threshold", "0",
                "-c:a", "aac",
                "-b:a", processed_audio_bitrate,
                "-ar", "48000",
                "-ac", "2",
                "-f", "hls",
                "-hls_time", str(processed_segment_seconds),
                "-hls_list_size", str(program_hls_list_size),
                "-hls_delete_threshold", str(program_hls_delete_threshold),
                "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments+program_date_time",
                "-hls_segment_type", "mpegts",
                "-hls_segment_filename", str(segment_pattern),
                "-start_number", str(start_number),
                str(output_playlist),
            ])
            try:
                proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
            except Exception as exc:
                stream_state["program_source_error"] = f"No se pudo iniciar ffmpeg fuente: {exc}"
                stream_state["program_error"] = stream_state["program_source_error"]
                return False

            run_id = int(time.time() * 1000)
            stream_state["program_source_run_id"] = run_id
            stream_state["program_source_proc"] = proc
            stream_state["program_source_url"] = hls_url
            stream_state["program_source_error"] = None
            stream_state["program_error"] = None
            threading.Thread(target=_drain_stderr, args=(proc, "program_source"), daemon=True).start()
            threading.Thread(target=_watch_gstreamer_source_proc, args=(proc, run_id), daemon=True).start()

            if not _wait_for_gstreamer_source(processed_startup_wait_seconds):
                stream_state["program_source_error"] = "No se genero source.m3u8 en el tiempo esperado"
                stream_state["program_error"] = stream_state["program_source_error"]
                stream_state["program_source_run_id"] = int(stream_state.get("program_source_run_id") or 0) + 1
                kill_process(proc)
                stream_state["program_source_proc"] = None
                return False
            try:
                new_playlist = program_hls_dir / "source_new.m3u8"
                if new_playlist.exists():
                    with open("/tmp/wfg_debug.txt", "a") as f:
                        f.write(f"switch step at {time.time()}, new_size={new_playlist.stat().st_size}\n")
                    switch_result = gstreamer_engine.switch("source_new.m3u8")
                    with open("/tmp/wfg_debug.txt", "a") as f:
                        f.write(f"switch result: {switch_result[:50] if switch_result else None} at {time.time()}\n")
                else:
                    with open("/tmp/wfg_debug.txt", "a") as f:
                        f.write(f"switch skipped: new_playlist not found at {time.time()}\n")
            except Exception as e:
                with open("/tmp/wfg_debug.txt", "a") as f:
                    f.write(f"switch failed: {type(e).__name__}: {e} at {time.time()}\n")
            stream_state["_source_switching"] = False
            return True

    def _restart_gstreamer_source_if_active() -> None:
        if not use_gstreamer_engine or gstreamer_engine is None:
            return
        if stream_state.get("program_fallback") is not False:
            return
        hls_url = stream_state.get("upstream_hls_url")
        if not hls_url:
            return
        current_source = playout.find_source_by_id(stream_state.get("current_source_id") or "")
        current_type = (current_source or {}).get("type")
        silent_audio = bool(
            current_source
            and (
                (current_type == "file" and not current_source.get("audio_codec"))
                or (
                    current_type == "torrent"
                    and current_source.get("audio_probed")
                    and not current_source.get("audio_codec")
                )
            )
        )
        if _start_gstreamer_source_stream(
            hls_url,
            loop=stream_state.get("mode") in ("file", "torrent"),
            silent_audio=silent_audio,
        ):
            stream_state["_using_source_new"] = True
            try:
                result = gstreamer_engine.switch("source_new.m3u8")
                if '"ok"' in result or '"status":"ok"' in result:
                    stream_state["program_fallback"] = False
                    stream_state["program_error"] = None
                    return
                stream_state["program_error"] = result
            except Exception as exc:
                stream_state["program_error"] = f"No se pudo cambiar a source: {exc}"
        stream_state["_using_source_new"] = False
        stream_state["program_fallback"] = True
        try:
            if gstreamer_engine.proc and gstreamer_engine.proc.poll() is None:
                gstreamer_engine.switch("black.m3u8")
        except Exception:
            pass

    def _fallback_chunks(version: int, max_seconds: float | None = None):
        deadline = None if max_seconds is None else time.time() + max_seconds
        while stream_state["relay_source_version"] == version and (
            deadline is None or time.time() < deadline
        ):
            pump = stream_state.get("_fallback_pump")
            if not pump or pump.poll() is not None or not pump.stdout:
                _ensure_fallback_pump()
                pump = stream_state.get("_fallback_pump")
            if not pump or pump.poll() is not None or not pump.stdout:
                return
            while stream_state["relay_source_version"] == version and (deadline is None or time.time() < deadline):
                r, _, _ = select.select([pump.stdout], [], [], 0.5)
                if not r:
                    continue
                chunk = pump.stdout.read(65536)
                if not chunk:
                    break
                yield chunk

    @app.route("/_relay/feed")
    def relay_feed():
        def generate():
            while True:
                version = stream_state["relay_source_version"]
                config = stream_state.get("relay_source_config")
                if config:
                    if config.get("type") == "file":
                        path = config.get("_hls_url") or config.get("path") or config.get("url")
                        if path:
                            _kill_fallback_pump()
                            yield from _file_chunks(version, path, config)
                        else:
                            stream_state["program_source_error"] = "Fuente de archivo sin ruta"
                            _ensure_fallback_pump()
                            yield from _fallback_chunks(version)
                        continue
                    if config.get("type") == "torrent":
                        source_id = config.get("id")
                        if source_id:
                            yield from _torrent_chunks(version, source_id, config)
                        else:
                            stream_state["program_source_error"] = "Fuente torrent sin identificador"
                            _ensure_fallback_pump()
                            yield from _fallback_chunks(version)
                        continue
                    hls_url = _resolve_relay_hls(config)
                    if hls_url:
                        try:
                            resp = requests.get(hls_url, stream=True, timeout=(2, 3), headers={"User-Agent": "Lavf/ffmpeg 7.1.4"})
                            for chunk in resp.iter_content(chunk_size=65536):
                                if stream_state["relay_source_version"] != version:
                                    break
                                if chunk:
                                    _kill_fallback_pump()
                                    yield chunk
                        except Exception:
                            for chunk in _fallback_chunks(version):
                                yield chunk
                else:
                    _ensure_fallback_pump()
                    pump = stream_state.get("_fallback_pump")
                    if pump and pump.poll() is None:
                        try:
                            while True:
                                cur_version = stream_state["relay_source_version"]
                                if cur_version != version:
                                    new_config = stream_state.get("relay_source_config")
                                    if new_config is not None:
                                        if new_config.get("type") in ("file", "torrent"):
                                            break
                                        hls_url = _resolve_relay_hls(new_config)
                                        if hls_url:
                                            try:
                                                resp = requests.get(hls_url, stream=True, timeout=(2, 3), headers={"User-Agent": "Lavf/ffmpeg 7.1.4"})
                                                for chunk in resp.iter_content(chunk_size=65536):
                                                    if stream_state["relay_source_version"] != cur_version:
                                                        break
                                                    if chunk:
                                                        _kill_fallback_pump()
                                                        yield chunk
                                            except Exception:
                                                pass
                                    break
                                r, _, _ = select.select([pump.stdout], [], [], 0.5)
                                if r:
                                    chunk = pump.stdout.read(65536)
                                    if not chunk:
                                        break
                                    yield chunk
                        except Exception:
                            pass

        return Response(generate(), mimetype="video/mp2t")

    def _start_persistent_program_stream() -> None:
        """Start ONE program stream that reads from the relay endpoint.
        This is called once at init. Never call again unless it crashes."""
        nonlocal gstreamer_engine
        with program_lock:
            stop_program_stream()
        program_hls_dir.mkdir(parents=True, exist_ok=True)
        if use_gstreamer_engine:
            if gstreamer_engine is None:
                gstreamer_engine = GStreamerEngine(
                    os.environ.get("GSTREAMER_ENGINE_BIN", "/usr/local/bin/gstreamer_engine"),
                    program_hls_dir,
                    {
                        "GSTREAMER_TARGET_DURATION": str(processed_segment_seconds),
                        "GSTREAMER_MAX_FILES": str(program_hls_list_size),
                        "GSTREAMER_WIDTH": str(processed_width),
                        "GSTREAMER_HEIGHT": str(processed_height),
                    },
                )
            gstreamer_engine.start()
            result = gstreamer_engine.set_black()
            if '"ok"' not in result and '"status":"ok"' not in result:
                stream_state["program_error"] = result
            else:
                stream_state["program_error"] = None
                stream_state["program_source_error"] = None
                if stream_state.get("program_fallback") is False and _gstreamer_source_ready() and not stream_state.get("_using_source_new"):
                    gstreamer_engine.switch("source.m3u8")
            stream_state["program_proc"] = gstreamer_engine.proc
            stream_state["program_stream_id"] = stream_state["stream_id"]
            stream_state["program_fallback"] = True
            return
        if not program_fallback_video.exists():
            generate_fallback_video()
        use_encoder = _resolve_encoder(processed_video_encoder)
        encoder_args = _build_video_encoder_args(
            use_encoder, processed_video_bitrate, processed_video_maxrate, processed_video_bufsize,
            processed_preset,
        )
        output_playlist = program_hls_dir / "live.m3u8"
        segment_pattern = program_hls_dir / "seg_%06d.ts"
        clock_filter = ("drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
                       "text='%{localtime}':"
                       "fontcolor=white:fontsize=64:x=w-tw-80:y=h-th-80:"
                       "box=1:boxcolor=black@0.5:boxborderw=12:bordercolor=black:borderw=2")
        include_clock = stream_state.get("include_clock", False)
        vf_arg = ["-vf", clock_filter] if include_clock else []
        relay_url = f"{internal_base_url}/_relay/feed"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-fflags", "+genpts+discardcorrupt",
            "-err_detect", "ignore_err",
            "-rw_timeout", "60000000",
            "-re",
            "-reconnect", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "2",
            "-i", relay_url,
            "-map", "0:v:0", "-map", "0:a:0?",
        ] + vf_arg + encoder_args + [
            "-g", str(25 * processed_segment_seconds), "-keyint_min", str(25 * processed_segment_seconds), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", processed_audio_bitrate, "-ar", "48000", "-ac", "2",
            "-f", "hls",
            "-hls_time", str(processed_segment_seconds),
            "-hls_list_size", str(program_hls_list_size),
            "-hls_delete_threshold", str(program_hls_delete_threshold),
            "-hls_flags", "append_list+omit_endlist+independent_segments+program_date_time",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(segment_pattern),
            "-start_number", str(_next_segment_number(program_hls_dir)),
            str(output_playlist),
        ]
        try:
            stream_state["program_proc"] = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        except Exception as exc:
            stream_state["program_error"] = f"No se pudo iniciar program stream: {exc}"
            return
        stream_state["program_stream_id"] = stream_state["stream_id"]
        stream_state["program_error"] = None
        stream_state["program_fallback"] = True
        threading.Thread(target=_drain_stderr, args=(stream_state["program_proc"], "program"), daemon=True).start()
        _cleanup_orphan_segments(program_hls_dir)

    def stop_program_stream() -> None:
        nonlocal gstreamer_engine
        if use_gstreamer_engine and gstreamer_engine is not None:
            gstreamer_engine.stop()
            gstreamer_engine = None
        else:
            kill_process(stream_state.get("program_proc"))
        _stop_gstreamer_source_stream()
        stream_state["program_proc"] = None
        stream_state["program_stream_id"] = None

    def start_presentation_stream() -> None:
        stop_preview_stream()
        stop_processed_stream()
        stop_presentation_stream()
        presentation_hls_dir.mkdir(parents=True, exist_ok=True)
        for item in presentation_hls_dir.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)

        output_playlist = presentation_hls_dir / "live.m3u8"
        segment_pattern = presentation_hls_dir / "seg_%06d.ts"
        use_encoder = _resolve_encoder(presentation_video_encoder)
        pres_encoder_args = _build_video_encoder_args(use_encoder, "4000k", "5000k", "8000k", "fast")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts",
            "-stream_loop",
            "-1",
            "-re",
            "-i",
            str(presentation_video),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
        ]
        cmd.extend(pres_encoder_args)
        if use_encoder != "h264_nvenc":
            cmd.extend(["-preset", "veryfast", "-tune", "zerolatency"])
        cmd.extend([
            "-g",
            "48",
            "-keyint_min",
            "48",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-f",
            "hls",
            "-hls_time",
            "4",
            "-hls_list_size",
            str(presentation_hls_list_size),
            "-hls_delete_threshold",
            str(presentation_hls_delete_threshold),
            "-hls_flags",
            "delete_segments+append_list+omit_endlist+independent_segments",
            "-hls_segment_filename",
            str(segment_pattern),
            str(output_playlist),
        ])
        stream_state["presentation_proc"] = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        threading.Thread(target=_drain_stderr, args=(stream_state["presentation_proc"], "presentation"), daemon=True).start()

    def switch_to_presentation(reason: str) -> bool:
        ap = playout.find_next_after_previous()
        if ap:
            source, cal_id = ap
            playout.set_calendar_played(cal_id)
            activate_source(source, "after_previous", cal_id)
            return True

        if not auto_presentation_on_end:
            return False
        if stream_state.get("mode") == "presentation":
            return True
        if not presentation_video.exists():
            return False

        try:
            stop_preview_stream()
            if stream_state.get("mode") in ("file", "torrent"):
                _set_relay_source(None)
            if use_gstreamer_engine and gstreamer_engine is not None:
                _stop_gstreamer_source_stream()
                result = gstreamer_engine.switch("black.m3u8")
                if '"ok"' in result or '"status":"ok"' in result:
                    stream_state["program_error"] = None
                    stream_state["program_fallback"] = True
                else:
                    stream_state["program_error"] = result
            start_presentation_stream()
        except Exception as exc:
            stream_state["error"] = f"No se pudo activar presentacion automatica: {exc}"
            return False

        segment_cache.clear()
        stream_state["stream_id"] += 1
        stream_state["mode"] = "presentation"
        stream_state["source_url"] = "Video presentacion (auto)"
        stream_state["upstream_hls_url"] = None
        stream_state["error"] = f"El directo termino o fallo. Activada presentacion automatica: {reason}"
        return True

    def force_presentation(reason: str) -> bool:
        if stream_state.get("mode") == "presentation":
            return True
        if not presentation_video.exists():
            stream_state["error"] = "No se encontro sofa.mp4 para fallback continuo"
            return False
        try:
            stop_preview_stream()
            if stream_state.get("mode") in ("file", "torrent"):
                _set_relay_source(None)
            if use_gstreamer_engine and gstreamer_engine is not None:
                _stop_gstreamer_source_stream()
                result = gstreamer_engine.switch("black.m3u8")
                if '"ok"' in result or '"status":"ok"' in result:
                    stream_state["program_error"] = None
                    stream_state["program_fallback"] = True
                else:
                    stream_state["program_error"] = result
            start_presentation_stream()
            segment_cache.clear()
            stream_state["stream_id"] += 1
            stream_state["mode"] = "presentation"
            stream_state["source_url"] = "Video presentacion"
            stream_state["source_url_name"] = "Video presentacion"
            stream_state["upstream_hls_url"] = None
            stream_state["current_source_id"] = None
            stream_state["current_reason"] = "fallback"
            stream_state["current_calendar_id"] = None
            stream_state["error"] = f"Salida continua en presentacion: {reason}"
            return True
        except Exception as exc:
            stream_state["error"] = f"No se pudo activar presentacion continua: {exc}"
            return False

    def refresh_stream_url_if_needed() -> bool:
        current_mode = stream_state.get("mode", "")
        if current_mode == "torrent":
            return bool(stream_state.get("upstream_hls_url"))

        source_url = stream_state.get("source_url")
        if not source_url:
            return False

        if current_mode in ("iptv", "hls", "file") or is_direct_stream_url(source_url):
            stream_state["upstream_hls_url"] = source_url
            stream_state["stream_id"] += 1
            stream_state["error"] = None
            segment_cache.clear()
            _restart_gstreamer_source_if_active()
            return True

        try:
            stream_state["upstream_hls_url"] = StreamExtractor(source_url).get_hls_url()
            stream_state["stream_id"] += 1
            stream_state["error"] = None
            segment_cache.clear()
            _restart_gstreamer_source_if_active()
            return True
        except Exception as exc:
            stream_state["error"] = f"No se pudo refrescar la URL del stream: {exc}"
            stream_state["upstream_hls_url"] = None
            _stop_gstreamer_source_stream()
            if use_gstreamer_engine and gstreamer_engine is not None:
                gstreamer_engine.switch("black.m3u8")
                stream_state["program_fallback"] = True
            return False

    def emby_stream_url(max_quality: bool = False) -> str:
        return absolute_url("/emby/live-max.m3u8" if max_quality else "/emby/live.m3u8")

    def _torrent_media_response(source_id: str | None):
        if not source_id:
            return no_store(Response("No hay ningun torrent activo.\n", status=404))
        source = playout.find_source_by_id(source_id)
        if not source or source.get("type") != "torrent":
            return no_store(Response("Fuente torrent no encontrada.\n", status=404))
        if torrent_manager.get(source_id) is None:
            _ensure_torrent_entry(source)
        try:
            size = torrent_manager.stream_size(source_id, timeout=torrent_metadata_timeout)
        except TorrentError as exc:
            return no_store(Response(f"Torrent no disponible: {exc}\n", status=503))
        if size <= 0:
            return no_store(Response("El torrent no tiene video disponible.\n", status=503))

        try:
            byte_range = parse_byte_range(request.headers.get("Range"), size)
        except ValueError:
            return no_store(Response(
                "Rango no válido.\n",
                status=416,
                headers={"Content-Range": f"bytes */{size}"},
            ))

        if byte_range is None:
            start, end, status_code = 0, size - 1, 200
        else:
            start, end = byte_range
            status_code = 206
        length = end - start + 1

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        mimetype = (
            mimetypes.guess_type(f"stream{source.get('extension') or '.mp4'}")[0]
            or "application/octet-stream"
        )
        if request.method == "HEAD":
            return no_store(Response(b"", status=status_code, headers=headers, mimetype=mimetype))

        def generate():
            try:
                yield from torrent_manager.iter_stream(source_id, start, length)
            except TorrentStopped:
                return
            except Exception as exc:
                log.warning("Torrent stream %s interrumpido: %s", source_id, exc)
                return

        return no_store(Response(
            generate(),
            status=status_code,
            headers=headers,
            mimetype=mimetype,
        ))

    def current_media_response(default_mimetype: str = "video/mp4"):
        upstream_hls_url = stream_state["upstream_hls_url"]
        if stream_state["mode"] == "torrent":
            return _torrent_media_response(stream_state.get("current_source_id"))
        if stream_state["mode"] == "file":
            media_path = Path(upstream_hls_url or "")
            if media_path.is_file():
                mimetype = mimetypes.guess_type(media_path.name)[0] or default_mimetype
                return no_store(send_file(media_path, mimetype=mimetype, conditional=True))
            return no_store(Response("Archivo de video no encontrado.\n", status=404))
        if stream_state["mode"] not in ("youtube", "iptv", "hls") or not upstream_hls_url:
            return no_store(Response("No hay ningun directo conectado.\n", status=404))

        range_header = request.headers.get("Range")
        cached_path = None
        if not range_header:
            cached_path = segment_cache.get(upstream_hls_url)
            if cached_path:
                return no_store(send_file(cached_path, mimetype=default_mimetype))

        try:
            headers = {"Range": range_header} if range_header else None
            upstream = requests.get(upstream_hls_url, stream=True, timeout=20, headers=headers)
            upstream.raise_for_status()
        except requests.RequestException as exc:
            stream_state["error"] = f"Fallo el proxy del directo: {exc}"
            stream_state["upstream_hls_url"] = None
            if switch_to_presentation(str(exc)):
                return no_store(send_file(presentation_video, mimetype="video/mp4", conditional=True))
            return no_store(Response(stream_state["error"] + "\n", status=502))

        content_type = upstream.headers.get("content-type", "")

        if not range_header and segment_cache.should_cache(upstream.headers.get("content-length")):
            try:
                cached_path = segment_cache.put(upstream_hls_url, upstream)
                return no_store(send_file(cached_path, mimetype=content_type or default_mimetype))
            except ValueError:
                pass

        def generate():
            for chunk in upstream.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk

        response_headers = {}
        for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if header in upstream.headers:
                response_headers[header] = upstream.headers[header]

        return no_store(Response(
            generate(),
            status=upstream.status_code,
            headers=response_headers,
            mimetype=content_type or default_mimetype,
        ))

    def parse_m3u(content: str) -> list[dict]:
        channels = []
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("#EXTINF"):
                name_match = re.search(r'tvg-name="([^"]*)"', line)
                if name_match:
                    name = name_match.group(1)
                else:
                    name = line.rsplit(",", 1)[-1].strip() if "," in line else "Canal"
                channels.append({"name": name, "url": ""})
            elif channels and not line.startswith("#") and line:
                channels[-1]["url"] = line
        return [ch for ch in channels if ch["url"]]

    playout = PlayoutEngine(state_file=os.environ.get("STATE_FILE", "data/state.json"))

    torrent_probe_lock = threading.Lock()
    torrent_probe_pending: set[str] = set()

    def _torrent_stream_url(source_id: str, extension: str | None) -> str:
        ext = (extension or ".mp4").lower()
        if not ext.startswith("."):
            ext = f".{ext}"
        return f"{internal_base_url}/torrent/{source_id}/stream{ext}"

    def _mark_torrent_probe(source_id: str) -> None:
        with torrent_probe_lock:
            torrent_probe_pending.add(source_id)

    def _consume_torrent_probe(source_id: str) -> bool:
        with torrent_probe_lock:
            if source_id not in torrent_probe_pending:
                return False
            torrent_probe_pending.discard(source_id)
            return True

    def _probe_torrent_metadata(source_id: str) -> dict | None:
        source = playout.find_source_by_id(source_id)
        if not source:
            return None
        stream_url = source.get("stream_url") or _torrent_stream_url(
            source_id, source.get("extension") or ".mp4"
        )
        try:
            meta = probe_video_url(stream_url, timeout=max(60, int(torrent_piece_timeout) + 30))
        except Exception as exc:
            log.warning("No se pudo analizar el video del torrent %s: %s", source_id, exc)
            playout.set_source_validation(source_id, "error", str(exc))
            return None
        playout.update_source(source_id, meta)
        playout.set_source_validation(source_id, "ok", None)
        return meta

    def _on_torrent_ready(source_id: str, desc: dict) -> None:
        source = playout.find_source_by_id(source_id)
        if not source:
            return
        extension = desc.get("extension") or ".mp4"
        updates = {
            "torrent_file": str(torrent_manager.meta_path(source_id)),
            "save_path": str(torrent_manager.data_path(source_id)),
            "file_path": desc.get("file"),
            "size_bytes": desc.get("size_bytes"),
            "extension": extension,
            "stream_url": _torrent_stream_url(source_id, extension),
        }
        current_name = (source.get("name") or "").strip()
        if desc.get("name") and (not current_name or current_name.lower() == "torrent"):
            updates["name"] = desc["name"]
        playout.update_source(source_id, updates)
        playout.set_source_validation(source_id, "ok", None)
        if _consume_torrent_probe(source_id):
            threading.Thread(
                target=_probe_torrent_metadata,
                args=(source_id,),
                daemon=True,
            ).start()

    def _on_torrent_error(source_id: str, message: str) -> None:
        log.warning("Torrent %s: %s", source_id, message)
        playout.set_source_validation(source_id, "error", message)

    def _ensure_torrent_entry(source: dict):
        source_id = source.get("id")
        if not source_id:
            return None
        entry = torrent_manager.get(source_id)
        if entry is not None:
            return entry
        torrent_path = source.get("torrent_file") or str(torrent_manager.meta_path(source_id))
        try:
            if torrent_path and Path(torrent_path).is_file():
                torrent_manager.add_torrent_data(
                    source_id, Path(torrent_path).read_bytes(), source.get("name")
                )
            elif source.get("magnet"):
                torrent_manager.add_magnet(source_id, source["magnet"], source.get("name"))
        except Exception as exc:
            log.warning("No se pudo cargar el torrent %s: %s", source_id, exc)
        return torrent_manager.get(source_id)

    torrent_manager = TorrentManager(
        torrent_dir,
        on_ready=_on_torrent_ready,
        on_error=_on_torrent_error,
        listen_port=torrent_listen_port,
        piece_timeout=torrent_piece_timeout,
        metadata_timeout=torrent_metadata_timeout,
    )

    def _warm_provider_epg(provider_id: str) -> None:
        try:
            data = _get_provider_epg(provider_id, include_programmes=False)
            if data.get("error"):
                return
            if epg_store.channel_count(provider_id) == 0:
                _sync_epg_store(provider_id, _get_provider_epg(provider_id, include_programmes=True))
        except Exception:
            pass

    threading.Thread(
        target=lambda: [_warm_provider_epg(p["id"]) for p in get_providers()],
        daemon=True,
    ).start()

    def activate_source(source: dict, reason: str, calendar_id: str | None = None) -> bool:
        source_id = source["id"]
        if source_id == stream_state.get("current_source_id") and stream_state.get("upstream_hls_url"):
            if reason == stream_state.get("current_reason") and (not use_gstreamer_engine or stream_state.get("program_fallback") is False):
                return True

        if source["type"] == "presentation":
            if not presentation_video.exists():
                playout.add_history_entry({
                    "timestamp": now_iso(),
                    "source_id": source_id,
                    "source_name": source.get("name", "Video presentacion"),
                    "source_type": "presentation",
                    "reason": reason,
                    "calendar_id": calendar_id,
                    "success": False,
                    "error": "No se encuentra sofa.mp4",
                })
                return False
            stop_preview_stream()
            stop_presentation_stream()
            segment_cache.clear()
            if use_gstreamer_engine and gstreamer_engine is not None:
                _stop_gstreamer_source_stream()
                result = gstreamer_engine.switch("black.m3u8")
                if '"ok"' in result or '"status":"ok"' in result:
                    stream_state["program_error"] = None
                    stream_state["program_fallback"] = True
                else:
                    stream_state["program_error"] = result
            stream_state["stream_id"] += 1
            stream_state["mode"] = "presentation"
            stream_state["source_url"] = "Video presentacion"
            stream_state["source_url_name"] = source.get("name", "Video presentacion")
            stream_state["upstream_hls_url"] = None
            stream_state["error"] = None
            stream_state["current_source_id"] = source_id
            stream_state["current_reason"] = reason
            stream_state["current_calendar_id"] = calendar_id
            try:
                start_presentation_stream()
                playout.add_history_entry({
                    "timestamp": now_iso(),
                    "source_id": source_id,
                    "source_name": source.get("name", "Video presentacion"),
                    "source_type": "presentation",
                    "reason": reason,
                    "calendar_id": calendar_id,
                    "success": True,
                    "error": None,
                })
                return True
            except Exception as exc:
                stream_state["mode"] = None
                stream_state["current_source_id"] = None
                stream_state["current_reason"] = None
                stream_state["current_calendar_id"] = None
                stream_state["error"] = f"No se pudo iniciar presentacion: {exc}"
                playout.add_history_entry({
                    "timestamp": now_iso(),
                    "source_id": source_id,
                    "source_name": source.get("name", "Video presentacion"),
                    "source_type": "presentation",
                    "reason": reason,
                    "calendar_id": calendar_id,
                    "success": False,
                    "error": str(exc),
                })
                return False

        if source["type"] == "football":
            stop_preview_stream()
            if use_gstreamer_engine and gstreamer_engine is not None:
                _stop_gstreamer_source_stream()
                result = gstreamer_engine.switch("black.m3u8")
                if '"ok"' not in result and '"status":"ok"' not in result:
                    stream_state["program_error"] = result
                else:
                    stream_state["program_error"] = None
                    stream_state["program_fallback"] = True
            playout.add_history_entry({
                "timestamp": now_iso(),
                "source_id": source_id,
                "source_name": source.get("name", ""),
                "source_type": "football",
                "reason": reason,
                "calendar_id": calendar_id,
                "success": False,
                "error": "Evento informativo, sin stream",
            })
            force_presentation("evento sin stream")
            return False

        stop_presentation_stream()
        stop_preview_stream()
        segment_cache.clear()

        source_type = source.get("type", "youtube")
        if source_type == "torrent":
            if _ensure_torrent_entry(source) is None:
                _stop_gstreamer_source_stream()
                if use_gstreamer_engine and gstreamer_engine is not None:
                    gstreamer_engine.switch("black.m3u8")
                    stream_state["program_fallback"] = True
                error_msg = f"No se pudo cargar el torrent: {source.get('name', source_id)}"
                stream_state["error"] = error_msg
                playout.add_history_entry({
                    "timestamp": now_iso(),
                    "source_id": source_id,
                    "source_name": source.get("name", source_id),
                    "source_type": source_type,
                    "reason": reason,
                    "calendar_id": calendar_id,
                    "success": False,
                    "error": error_msg,
                })
                return False
            url = source.get("magnet") or source.get("torrent_file") or source.get("name", "")
            hls_url = source.get("stream_url") or _torrent_stream_url(
                source_id, source.get("extension")
            )
        else:
            if source_type == "file":
                url = source.get("path") or source.get("url") or ""
            else:
                _check_and_refresh_source_url(source)
                url = source.get("url", "")
        if source_type == "file" and (not url or not Path(url).is_file()):
            _stop_gstreamer_source_stream()
            if use_gstreamer_engine and gstreamer_engine is not None:
                gstreamer_engine.switch("black.m3u8")
                stream_state["program_fallback"] = True
            error_msg = f"Archivo de video no encontrado: {source.get('name', source_id)}"
            stream_state["error"] = error_msg
            playout.add_history_entry({
                "timestamp": now_iso(),
                "source_id": source_id,
                "source_name": source.get("name", url),
                "source_type": source_type,
                "reason": reason,
                "calendar_id": calendar_id,
                "success": False,
                "error": error_msg,
            })
            return False
        if source_type != "torrent":
            hls_url = _hls_url_for_source(url, source_type)
        if not hls_url:
            _stop_gstreamer_source_stream()
            if use_gstreamer_engine and gstreamer_engine is not None:
                gstreamer_engine.switch("black.m3u8")
                stream_state["program_fallback"] = True
            exc = stream_state.get("error") or Exception("URL no resolved")
            error_msg = f"No se pudo activar fuente: {exc}"
            stream_state["error"] = error_msg
            playout.add_history_entry({
                "timestamp": now_iso(),
                "source_id": source_id,
                "source_name": source.get("name", url),
                "source_type": source_type,
                "reason": reason,
                "calendar_id": calendar_id,
                "success": False,
                "error": str(exc),
            })
            return False

        stream_state["stream_id"] += 1
        stream_state["mode"] = source_type if source_type in ("youtube", "iptv", "hls", "file", "torrent") else "youtube"
        stream_state["source_url"] = url
        stream_state["source_url_name"] = source.get("name", url)
        stream_state["upstream_hls_url"] = hls_url
        stream_state["error"] = None
        stream_state["current_source_id"] = source_id
        stream_state["current_reason"] = reason
        stream_state["current_calendar_id"] = calendar_id

        if use_gstreamer_engine and gstreamer_engine is None:
            _start_persistent_program_stream()

        if hls_url:
            try:
                _set_relay_source(source, hls_url=hls_url)
                if source_type == "torrent" and not source.get("audio_probed"):
                    threading.Thread(
                        target=_probe_torrent_metadata,
                        args=(source_id,),
                        daemon=True,
                    ).start()
                if use_gstreamer_engine and gstreamer_engine is not None:
                    silent_audio = bool(
                        (source_type == "file" and not source.get("audio_codec"))
                        or (
                            source_type == "torrent"
                            and source.get("audio_probed")
                            and not source.get("audio_codec")
                        )
                    )
                    if not _start_gstreamer_source_stream(
                        hls_url,
                        loop=(source_type in ("file", "torrent")),
                        silent_audio=silent_audio,
                    ):
                        stream_state["program_fallback"] = True
                        if gstreamer_engine is not None and gstreamer_engine.proc and gstreamer_engine.proc.poll() is None:
                            gstreamer_engine.switch("black.m3u8")
                        return False
                    stream_state["_using_source_new"] = True
                    result = gstreamer_engine.switch("source_new.m3u8")
                    if '"ok"' not in result and '"status":"ok"' not in result:
                        stream_state["program_error"] = result
                        _stop_gstreamer_source_stream()
                        if gstreamer_engine is not None and gstreamer_engine.proc and gstreamer_engine.proc.poll() is None:
                            gstreamer_engine.switch("black.m3u8")
                        stream_state["program_fallback"] = True
                        return False
                    stream_state["program_error"] = None
                    stream_state["program_fallback"] = False
            except Exception as exc:
                stream_state["program_error"] = f"No se pudo cambiar source: {exc}"
                _stop_gstreamer_source_stream()
                if use_gstreamer_engine and gstreamer_engine is not None and gstreamer_engine.proc and gstreamer_engine.proc.poll() is None:
                    gstreamer_engine.switch("black.m3u8")
                stream_state["program_fallback"] = True
                return False
        if hls_url and source_type not in ("file", "torrent") and not StreamExtractor.is_hls_url(hls_url):
            try:
                start_preview_stream(hls_url)
            except Exception:
                pass

        if stream_state.get("include_clock", False):
            stream_state["include_clock"] = False
            _start_persistent_program_stream()

        playout.add_history_entry({
            "timestamp": now_iso(),
            "source_id": source_id,
            "source_name": source.get("name", url),
            "source_type": source_type,
            "reason": reason,
            "calendar_id": calendar_id,
            "success": True,
            "error": None,
        })
        return True

    def get_stream_state() -> dict:
        return stream_state

    playout.ensure_presentation_source(presentation_video.exists())

    try:
        resumed = torrent_manager.resume(playout.get_sources())
        if resumed:
            log.info("Reanudados %s torrent(s) desde el estado guardado", resumed)
    except Exception:
        log.exception("No se pudieron reanudar los torrents guardados")

    playout.set_callbacks({
        "activate_source": activate_source,
        "get_stream_state": get_stream_state,
    })
    playout.start()

    # Reap any ffmpeg process left over from a previous container
    # instance and clear stale HLS temp dirs.  Must run BEFORE any
    # start_program_* call so we don't wipe just-created segments.
    try:
        cleanup_orphans([preview_hls_dir, processed_hls_dir, presentation_hls_dir, program_hls_dir])
    except Exception:
        log.exception("ffmpeg orphan cleanup failed")

    # Generate fallback video at startup so it's ready
    try:
        generate_fallback_video()
    except Exception:
        pass

    # Start persistent relay architecture:
    # 1. Fallback pump runs continuously looping fallback.mp4
    # 2. Program ffmpeg reads from local relay endpoint, never restarts
    # 3. Source changes just update what the relay serves
    try:
        _ensure_fallback_pump()
    except Exception:
        pass
    try:
        _start_persistent_program_stream()
    except Exception:
        pass
    # Relay starts with fallback; playout engine will switch to the
    # appropriate source via activate_source -> _set_relay_source

    def _daily_iptv_check():
        return daily_check_iptv(playout, get_providers())

    start_daily_scheduler(_daily_iptv_check)

    # Spin up watchdog threads that restart each ffmpeg pipeline if it
    # stops emitting HLS segments.  Adapted from jvdillon/netv's session
    # supervisor: same heartbeat idea, but a simpler in-process version
    # tailored to our three singletons.
    _supervisors: list[Supervisor] = []

    def _start_supervisor(name: str, get_proc, get_dir, restart) -> None:
        cfg = SupervisorConfig(
            name=name,
            get_proc=get_proc,
            get_segment_dir=get_dir,
            restart=restart,
            stall_timeout_sec=float(os.environ.get("FFMPEG_STALL_TIMEOUT", str(STALL_TIMEOUT_SEC))),
            startup_grace_sec=float(os.environ.get("FFMPEG_STARTUP_GRACE_SEC", str(STARTUP_GRACE_SEC))),
        )
        sup = Supervisor(cfg)
        sup.start()
        _supervisors.append(sup)

    _start_supervisor(
        "presentation",
        lambda: stream_state.get("presentation_proc"),
        lambda: presentation_hls_dir,
        lambda: start_presentation_stream(),
    )

    _start_supervisor(
        "program",
        lambda: stream_state.get("program_proc"),
        lambda: program_hls_dir,
        lambda: _start_persistent_program_stream(),
    )

    def _restart_preview():
        url = stream_state.get("preview_url")
        if url:
            start_preview_stream(url)

    def _restart_processed():
        url = stream_state.get("processed_url")
        if url and processed_enabled:
            start_processed_stream(url)

    _start_supervisor(
        "preview",
        lambda: stream_state.get("preview_proc"),
        lambda: preview_hls_dir,
        _restart_preview,
    )
    _start_supervisor(
        "processed",
        lambda: stream_state.get("processed_proc"),
        lambda: processed_hls_dir,
        _restart_processed,
    )

    @app.route("/")
    def index():
        today = now_date()
        calendar_today = playout.get_calendar(today)
        next_entry = None
        for e in calendar_today:
            if e.get("status") != "played" and e.get("enabled", True) and e.get("start_mode", "time") == "time":
                next_entry = e
                break

        overlap_ids = set()
        for i in range(len(calendar_today) - 1):
            cur = calendar_today[i]
            nxt = calendar_today[i + 1]
            if cur.get("end_time") and nxt.get("time") and cur["end_time"] > nxt["time"]:
                overlap_ids.add(cur["id"])

        state = playout.get_state()
        internal_sources = sorted(state["sources"], key=lambda s: s["name"].lower())

        for s in internal_sources:
            s.setdefault("group", _source_group_for(s))
        sources = [public_source(s) for s in internal_sources]
        auto_enabled = state["auto_enabled"]
        calendar_counts: dict[str, int] = {}
        calendar_programs: dict[str, list[dict]] = {}
        for entry in state.get("calendar", []):
            date_key = str(entry.get("date") or "")
            if not date_key:
                continue
            calendar_counts[date_key] = calendar_counts.get(date_key, 0) + 1
            calendar_programs.setdefault(date_key, []).append({
                "id": entry.get("id"),
                "time": entry.get("time"),
                "title": entry.get("title"),
                "enabled": entry.get("enabled", True),
                "status": entry.get("status"),
            })

        source_map = {s["id"]: s["name"] for s in internal_sources}

        current_mode = stream_state["mode"]
        upstream_url = stream_state.get("upstream_hls_url")
        upstream_is_hls = bool(upstream_url and StreamExtractor.is_hls_url(upstream_url))
        upstream_is_direct = bool(upstream_url and not upstream_is_hls and current_mode not in ("file", "torrent"))
        emby_preview_url = "/emby/processed/live.m3u8" if processed_enabled else "/emby/live.m3u8"
        playback_url = None
        is_hls_flag = False
        is_direct_mp4 = False
        if current_mode == "presentation":
            playback_url = "/presentation.mp4"
            is_direct_mp4 = True
        elif current_mode:
            playback_url = emby_preview_url
            is_hls_flag = True

        response = no_store(Response(render_template(
            "player.html",
            app_version=app_version,
            build_commit=build_commit,
            build_date=build_date,
            playlist_url="/live.m3u8" if current_mode else None,
            playback_url=playback_url,
            is_hls=is_hls_flag,
            is_direct_ts=upstream_is_direct,
            is_direct_mp4=is_direct_mp4,
            mode=current_mode,
            source_url=redact_url(stream_state["source_url"]),
            source_url_name=stream_state.get("source_url_name"),
            error=stream_state["error"],
            connected=bool(stream_state["mode"]),
            sources=sources,
            calendar_today=calendar_today,
            calendar_date=today,
            next_entry=next_entry,
            current_entry=playout.find_calendar_entry(stream_state.get("current_calendar_id")),
            source_map=source_map,
            auto_enabled=auto_enabled,
            current_source_id=stream_state.get("current_source_id"),
            current_reason=stream_state.get("current_reason"),
            current_calendar_id=stream_state.get("current_calendar_id"),
            pending_source=stream_state.get("pending_source"),
            overlap_ids=overlap_ids,
            processed_enabled=processed_enabled,
            processed_error=stream_state.get("processed_error"),
            calendar_counts=calendar_counts,
            calendar_programs=calendar_programs,
        )))
        response.mimetype = "text/html"
        return response

    def _entry_summary(entry: dict | None, source_map: dict[str, str]) -> dict | None:
        if not entry:
            return None
        return {
            "id": entry.get("id"),
            "title": entry.get("title") or entry.get("epg_title") or "Programa",
            "time": entry.get("time"),
            "end_time": entry.get("end_time") or entry.get("epg_end_time"),
            "source_id": entry.get("source_id"),
            "source_name": source_map.get(entry.get("source_id", ""), entry.get("source_id", "") or ""),
            "epg_title": entry.get("epg_title"),
            "epg_description": entry.get("epg_description"),
        }

    def _seconds_between(hms_a: str, hms_b: str) -> int:
        def _to_sec(s: str) -> int | None:
            if not s:
                return None
            parts = s.split(":")
            if len(parts) < 2:
                return None
            try:
                h = int(parts[0])
                m = int(parts[1])
                sec = int(parts[2]) if len(parts) > 2 else 0
                return h * 3600 + m * 60 + sec
            except (ValueError, IndexError):
                return None
        a = _to_sec(hms_a)
        b = _to_sec(hms_b)
        if a is None or b is None:
            return 0
        diff = a - b
        return max(0, diff)

    @app.route("/api/playout/status", methods=["GET"])
    def api_playout_status():
        today = now_date()
        calendar_today = playout.get_calendar(today)
        local_now_hm = now_hm()
        local_now_hms = now_hms()
        state = playout.get_state()
        source_map = {s["id"]: s["name"] for s in state["sources"]}

        # Current entry: prefer the one in stream_state, fall back to in-progress today
        current_cal_id = stream_state.get("current_calendar_id")
        current_entry = playout.find_calendar_entry(current_cal_id) if current_cal_id else None
        if not current_entry:
            for e in calendar_today:
                if (
                    e.get("start_mode", "time") == "time"
                    and e.get("enabled", True)
                    and e.get("time")
                    and e["time"] <= local_now_hm
                    and (not e.get("end_time") or e["end_time"] > local_now_hm)
                ):
                    current_entry = e
                    break

        # Next entry: first pending/future entry today with time > now
        next_entry = None
        for e in calendar_today:
            if e.get("enabled", True) and e.get("start_mode", "time") == "time" and e.get("time"):
                if e.get("end_time") and e["end_time"] <= local_now_hm:
                    continue
                if e.get("status") == "played" and (not e.get("end_time") or e["end_time"] <= local_now_hm):
                    continue
                if e is current_entry:
                    continue
                if e["time"] > local_now_hm:
                    next_entry = e
                    break

        current_summary = _entry_summary(current_entry, source_map)
        next_summary = _entry_summary(next_entry, source_map)
        if current_summary:
            current_summary["remaining_seconds"] = _seconds_between(current_summary.get("end_time"), local_now_hms)
        if next_summary:
            next_summary["starts_in_seconds"] = _seconds_between(next_summary.get("time"), local_now_hms)

        return jsonify({
            "now": local_now_hms,
            "today": today,
            "connected": bool(stream_state["mode"]),
            "current_source_id": stream_state.get("current_source_id"),
            "current_reason": stream_state.get("current_reason"),
            "current_source_name": stream_state.get("source_url_name"),
            "current_entry": current_summary,
            "next_entry": next_summary,
            "pending_source": {
                "name": stream_state.get("pending_source", {}).get("source", {}).get("name")
            } if stream_state.get("pending_source") else None,
        })

    @app.route("/sources")
    def sources_page():
        state = playout.get_state()
        iptv_providers = [public_provider(p) for p in get_providers()]
        sources_for_page = [public_source(s) for s in sorted(state["sources"], key=lambda s: s["name"].lower())]
        for s in sources_for_page:
            s.setdefault("group", _source_group_for(s))
        response = no_store(Response(render_template(
            "sources.html",
            app_version=app_version,
            build_commit=build_commit,
            build_date=build_date,
            sources=sources_for_page,
            iptv_providers=iptv_providers,
        )))
        response.mimetype = "text/html"
        return response

    @app.route("/connect", methods=["POST"])
    def connect():
        if stream_state["mode"]:
            stream_state["error"] = "Desconecta la emision actual antes de conectar una URL nueva."
            return redirect(url_for("index"))

        source_url = request.form.get("url", "").strip()
        if not source_url:
            stream_state["error"] = "Introduce una URL valida."
            return redirect(url_for("index"))
        if not is_allowed_source_url(source_url):
            stream_state["error"] = "URL no permitida. Usa http/https con host publico."
            return redirect(url_for("index"))

        try:
            hls_url = StreamExtractor(source_url).get_hls_url()
        except Exception as exc:
            stream_state["error"] = f"No se pudo conectar con esa URL: {exc}"
            return redirect(url_for("index"))

        stop_presentation_stream()
        segment_cache.clear()
        stream_state["stream_id"] += 1
        stream_state["mode"] = "youtube"
        stream_state["source_url"] = source_url
        stream_state["source_url_name"] = None
        stream_state["upstream_hls_url"] = hls_url
        stream_state["error"] = None
        try:
            start_processed_stream(hls_url)
        except Exception as exc:
            stream_state["processed_error"] = f"No se pudo iniciar procesado: {exc}"
        return redirect(url_for("index"))

    @app.route("/load_iptv", methods=["POST"])
    def load_iptv():
        if stream_state["mode"]:
            stream_state["error"] = "Desconecta la emision actual antes de cargar una lista."
            return redirect(url_for("index"))

        iptv_url = request.form.get("iptv_url", "").strip()
        if not iptv_url:
            stream_state["error"] = "Introduce una URL de lista IPTV."
            return redirect(url_for("index"))
        if not is_allowed_source_url(iptv_url):
            stream_state["error"] = "URL IPTV no permitida. Usa http/https con host publico."
            return redirect(url_for("index"))

        try:
            r = requests.get(iptv_url, timeout=30)
            r.raise_for_status()
            channels = parse_m3u(r.text)
        except requests.RequestException as exc:
            stream_state["error"] = f"No se pudo descargar la lista: {exc}"
            return redirect(url_for("index"))

        if not channels:
            stream_state["error"] = "No se encontraron canales en la lista."
            return redirect(url_for("index"))

        stream_state["iptv_channels"] = channels
        stream_state["iptv_url"] = iptv_url
        stream_state["error"] = None
        return redirect(url_for("index"))

    @app.route("/connect_iptv", methods=["POST"])
    def connect_iptv():
        if stream_state["mode"]:
            stream_state["error"] = "Desconecta la emision actual antes de emitir un canal."
            return redirect(url_for("index"))

        channel_url = request.form.get("channel_url", "").strip()
        if not channel_url:
            stream_state["error"] = "Selecciona un canal."
            return redirect(url_for("index"))
        if not is_allowed_source_url(channel_url):
            stream_state["error"] = "URL de canal no permitida."
            return redirect(url_for("index"))

        channel_name = channel_url
        for ch in stream_state.get("iptv_channels", []):
            if ch["url"] == channel_url:
                channel_name = ch["name"]
                break

        if channel_url.endswith(".m3u8") or "m3u8" in channel_url:
            hls_url = channel_url
        else:
            try:
                hls_url = StreamExtractor(channel_url).get_hls_url()
            except Exception as exc:
                stream_state["error"] = f"No se pudo extraer el stream del canal: {exc}"
                return redirect(url_for("index"))

        stop_preview_stream()
        stop_presentation_stream()
        segment_cache.clear()
        stream_state["stream_id"] += 1
        stream_state["mode"] = "iptv"
        stream_state["source_url"] = channel_url
        stream_state["source_url_name"] = channel_name
        stream_state["upstream_hls_url"] = hls_url
        stream_state["error"] = None
        if hls_url and not StreamExtractor.is_hls_url(hls_url):
            try:
                start_preview_stream(hls_url)
            except Exception:
                pass
        if hls_url:
            try:
                _set_relay_source({
                    "id": None,
                    "url": channel_url,
                    "type": "iptv",
                    "name": channel_name,
                })
            except Exception as exc:
                stream_state["program_error"] = f"No se pudo cambiar source: {exc}"
        return redirect(url_for("index"))

    @app.route("/presentation", methods=["POST"])
    def enable_presentation():
        if stream_state["mode"]:
            stream_state["error"] = "Desconecta la emision actual antes de emitir un canal."
            return redirect(url_for("index"))

        if not presentation_video.exists():
            stream_state["error"] = "No se encontro el video de presentacion (sofa.mp4)."
            return redirect(url_for("index"))

        stop_preview_stream()
        stop_processed_stream()
        segment_cache.clear()
        stream_state["stream_id"] += 1
        stream_state["mode"] = "presentation"
        stream_state["source_url"] = "Video presentacion"
        stream_state["upstream_hls_url"] = None
        stream_state["error"] = None
        try:
            start_presentation_stream()
        except Exception as exc:
            stream_state["mode"] = None
            stream_state["source_url"] = None
            stream_state["error"] = f"No se pudo iniciar presentacion continua: {exc}"
        return redirect(url_for("index"))

    @app.route("/disconnect", methods=["POST"])
    def disconnect():
        force_presentation("desconexion manual")
        return redirect(url_for("index"))

    @app.route("/api/presentation/start", methods=["POST"])
    def api_presentation_start():
        ok = force_presentation("emitir ahora")
        return jsonify({"ok": ok, "error": None if ok else stream_state.get("error")})

    @app.route("/presentation.mp4")
    def presentation_mp4():
        request_sid = request.args.get("sid", type=int)
        if request_sid is not None and request_sid != stream_state["stream_id"]:
            return no_store(Response("La sesion cambio; recarga la lista.\n", status=410))

        if stream_state["mode"] != "presentation":
            return no_store(Response("No hay video de presentacion activo.\n", status=404))

        if not presentation_video.exists():
            return no_store(Response("No se encontro sofa.mp4.\n", status=404))

        return no_store(send_file(presentation_video, mimetype="video/mp4", conditional=True))

    @app.route("/presentation.m3u8")
    def presentation_m3u8():
        request_sid = request.args.get("sid", type=int)
        if request_sid is not None and request_sid != stream_state["stream_id"]:
            return no_store(Response("La sesion cambio; recarga la lista.\n", status=410))

        if stream_state["mode"] != "presentation":
            return no_store(Response("No hay video de presentacion activo.\n", status=404))

        playlist_path = presentation_hls_dir / "live.m3u8"
        if not playlist_path.exists():
            return no_store(Response(
                direct_file_playlist(f"/presentation.mp4?sid={stream_state['stream_id']}"),
                mimetype="application/vnd.apple.mpegurl",
            ))

        raw = playlist_path.read_text(encoding="utf-8", errors="ignore")
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(f"/presentation/live/{stripped}")
            else:
                lines.append(line)
        playlist = "\n".join(lines) + "\n"
        return no_store(Response(playlist, mimetype="application/vnd.apple.mpegurl"))

    @app.route("/presentation/live/<path:filename>")
    def presentation_live_file(filename: str):
        if stream_state["mode"] != "presentation":
            return no_store(Response("No hay video de presentacion activo.\n", status=404))
        if filename.endswith(".ts"):
            return no_store(send_from_directory(presentation_hls_dir, filename, mimetype="video/mp2t", conditional=True))
        if filename.endswith(".m3u8"):
            return no_store(send_from_directory(presentation_hls_dir, filename, mimetype="application/vnd.apple.mpegurl", conditional=True))
        return no_store(send_from_directory(presentation_hls_dir, filename, conditional=True))

    @app.route("/preview/live.m3u8")
    def preview_live_playlist():
        if stream_state["mode"] == "presentation":
            return presentation_m3u8()

        upstream_hls_url = stream_state.get("upstream_hls_url")
        if not upstream_hls_url:
            return no_store(Response("No hay ningun directo conectado.\n", status=404))

        if StreamExtractor.is_hls_url(upstream_hls_url):
            return live_playlist()

        if use_gstreamer_engine and stream_state.get("program_fallback") is not False:
            return no_store(Response("Programa en fallback. Esperando fuente...\n", status=503))

        program_playlist_path = program_hls_dir / "live.m3u8"
        for _ in range(20):
            if program_playlist_path.exists() and program_playlist_path.stat().st_size > 0:
                break
            time.sleep(0.1)

        if not program_playlist_path.exists():
            return no_store(Response("Programa aun iniciando.\n", status=503))

        raw = program_playlist_path.read_text(encoding="utf-8", errors="ignore")
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(f"/program/live/{stripped}")
            else:
                lines.append(line)
        playlist = "\n".join(lines) + "\n"
        return no_store(Response(playlist, mimetype="application/vnd.apple.mpegurl"))

    @app.route("/preview/live/<path:filename>")
    def preview_live_file(filename: str):
        if filename.endswith(".ts"):
            return no_store(send_from_directory(program_hls_dir, filename, mimetype="video/mp2t", conditional=True))
        if filename.endswith(".m3u8"):
            return no_store(send_from_directory(program_hls_dir, filename, mimetype="application/vnd.apple.mpegurl", conditional=True))
        return no_store(send_from_directory(program_hls_dir, filename, conditional=True))

    def _program_segment_url(segment: str, absolute_urls: bool = False) -> str:
        path = f"/program/live/{segment}"
        if use_gstreamer_engine and stream_state.get("program_fallback") is False:
            query = []
            stream_id = stream_state.get("stream_id")
            source_gen = stream_state.get("program_source_run_id")
            if stream_id is not None:
                query.append(f"sid={int(stream_id)}")
            if source_gen is not None:
                query.append(f"gen={int(source_gen)}")
            if query:
                path += "?" + "&".join(query)
        return absolute_url(path) if absolute_urls else path

    def _wait_for_program_playlist(absolute_urls: bool = False):
        playlist_path = program_hls_dir / "live.m3u8"
        if use_gstreamer_engine and stream_state.get("program_fallback") is False and gstreamer_engine is not None:
            try:
                if _gstreamer_source_ready() and not stream_state.get("_using_source_new"):
                    gstreamer_engine.switch("source.m3u8")
            except Exception:
                pass
        for _ in range(max(1, int(processed_startup_wait_seconds / 0.2))):
            if playlist_path.exists() and playlist_path.stat().st_size > 0:
                break
            time.sleep(0.2)
        if not playlist_path.exists():
            return no_store(Response("Programa aun iniciando.\n", status=503))
        raw = playlist_path.read_text(encoding="utf-8", errors="ignore")
        lines = []
        has_allow_cache = False
        discontinuity_pending = stream_state.pop("_source_discontinuity", False)
        first_segment_idx = None
        for idx, ln in enumerate(raw.splitlines()):
            s = ln.strip()
            if s == "#EXT-X-ALLOW-CACHE:NO":
                has_allow_cache = True
            if s and not s.startswith("#") and s.endswith(".ts"):
                seg_url = _program_segment_url(s, absolute_urls)
                lines.append(seg_url)
                if first_segment_idx is None:
                    first_segment_idx = len(lines) - 1
            else:
                lines.append(ln)
        if discontinuity_pending and first_segment_idx is not None:
            needs_discontinuity = True
            for i in range(first_segment_idx):
                if lines[i].strip() == "#EXT-X-DISCONTINUITY":
                    needs_discontinuity = False
                    break
            if needs_discontinuity:
                extinf_idx = None
                for i in range(first_segment_idx - 1, -1, -1):
                    if lines[i].strip().startswith("#EXTINF:"):
                        extinf_idx = i
                        break
                insert_idx = extinf_idx if extinf_idx is not None else first_segment_idx
                lines.insert(insert_idx, "#EXT-X-DISCONTINUITY")
        if not has_allow_cache:
            insert_at = 1 if lines and lines[0].startswith("#EXTM3U") else 0
            lines.insert(insert_at, "#EXT-X-ALLOW-CACHE:NO")
        return no_store(Response("\n".join(lines) + "\n", mimetype="application/vnd.apple.mpegurl"))

    @app.route("/processed/live.m3u8")
    def processed_live_playlist():
        if not processed_enabled:
            return no_store(Response("Salida procesada desactivada.\n", status=404))
        program_proc = stream_state.get("program_proc")
        program_dead = program_proc is None or program_proc.poll() is not None
        if program_dead:
            try:
                _start_persistent_program_stream()
            except Exception as exc:
                stream_state["program_error"] = str(exc)
        return _wait_for_program_playlist()

    @app.route("/processed/live/<path:filename>")
    def processed_live_file(filename: str):
        if filename.endswith(".ts"):
            return no_store(send_from_directory(processed_hls_dir, filename, mimetype="video/mp2t", conditional=True))
        if filename.endswith(".m3u8"):
            return no_store(send_from_directory(processed_hls_dir, filename, mimetype="application/vnd.apple.mpegurl", conditional=True))
        return no_store(send_from_directory(processed_hls_dir, filename, conditional=True))

    @app.route("/program/live/<path:filename>")
    def program_live_file(filename: str):
        if filename.endswith(".ts"):
            return no_store(send_from_directory(program_hls_dir, filename, mimetype="video/mp2t", conditional=True))
        if filename.endswith(".m3u8"):
            return no_store(send_from_directory(program_hls_dir, filename, mimetype="application/vnd.apple.mpegurl", conditional=True))
        return no_store(send_from_directory(program_hls_dir, filename, conditional=True))

    @app.route("/health")
    def health():
        return no_store(Response("ok\n", mimetype="text/plain"))

    @app.route("/api/version")
    def api_version():
        import platform
        return jsonify({
            "app": "youtube-m3u8",
            "version": app_version,
            "commit": build_commit or None,
            "build_date": build_date or None,
            "python": platform.python_version(),
            "tz": os.environ.get("TZ", "UTC"),
            "auth_required": admin_token_enabled(),
        })

    @app.route("/api/logs")
    def api_logs():
        preview_buf = stream_state.get("logs", {}).get("preview", [])
        processed_buf = stream_state.get("logs", {}).get("processed", [])
        presentation_buf = stream_state.get("logs", {}).get("presentation", [])
        preview_alive = stream_state.get("preview_proc") is not None and stream_state.get("preview_proc").poll() is None
        processed_alive = stream_state.get("processed_proc") is not None and stream_state.get("processed_proc").poll() is None
        presentation_alive = stream_state.get("presentation_proc") is not None and stream_state.get("presentation_proc").poll() is None
        program_proc = stream_state.get("program_proc")
        program_alive = program_proc is not None and program_proc.poll() is None
        return jsonify({
            "preview": list(preview_buf),
            "processed": list(processed_buf),
            "presentation": list(presentation_buf),
            "processed_error": stream_state.get("processed_error"),
            "upstream_error": stream_state.get("error"),
            "mode": stream_state.get("mode"),
            "preview_alive": preview_alive,
            "processed_alive": processed_alive,
            "presentation_alive": presentation_alive,
            "program_alive": program_alive,
            "program_error": stream_state.get("program_error"),
            "program_source_error": stream_state.get("program_source_error"),
            "program_fallback": stream_state.get("program_fallback"),
            "source_name": stream_state.get("source_url_name"),
        })

    @app.route("/api/gstreamer/status")
    def api_gstreamer_status():
        if not use_gstreamer_engine or gstreamer_engine is None:
            return jsonify({"ok": False, "error": "GStreamer engine disabled"})
        return Response(gstreamer_engine.status() + "\n", mimetype="application/json")

    @app.route("/api/gpu/status")
    def api_gpu_status():
        return jsonify(_gpu_status())

    @app.route("/channels.m3u")
    def channels_m3u():
        channel_name = "YouTube Live"
        playlist = "\n".join([
            "#EXTM3U x-tvg-url=\"{}\"".format(absolute_url("/guide.xml")),
            "#EXTINF:-1 tvg-id=\"youtube-live\" tvg-name=\"{}\" group-title=\"YouTube\",{}".format(channel_name, channel_name),
            emby_stream_url(False),
            "",
        ])
        return no_store(Response(playlist, mimetype="application/x-mpegURL"))

    @app.route("/channels-processed.m3u")
    def channels_processed_m3u():
        channel_name = "YouTube Live Procesado"
        playlist = "\n".join([
            "#EXTM3U x-tvg-url=\"{}\"".format(absolute_url("/guide.xml")),
            "#EXTINF:-1 tvg-id=\"youtube-live\" tvg-name=\"{}\" group-title=\"YouTube\",{}".format(channel_name, channel_name),
            absolute_url("/emby/processed/live.m3u8"),
            "",
        ])
        return no_store(Response(playlist, mimetype="application/x-mpegURL"))

    @app.route("/emby/live.m3u8")
    def emby_live_playlist():
        program_proc = stream_state.get("program_proc")
        program_dead = program_proc is None or program_proc.poll() is not None
        if program_dead:
            try:
                _start_persistent_program_stream()
            except Exception as exc:
                stream_state["program_error"] = str(exc)
        return _wait_for_program_playlist(absolute_urls=True)

    @app.route("/emby/live-max.m3u8")
    def emby_live_max_playlist():
        program_proc = stream_state.get("program_proc")
        program_dead = program_proc is None or program_proc.poll() is not None
        if program_dead:
            try:
                _start_persistent_program_stream()
            except Exception as exc:
                stream_state["program_error"] = str(exc)
        return _wait_for_program_playlist(absolute_urls=True)

    @app.route("/emby/processed/live.m3u8")
    def emby_processed_live_playlist():
        return processed_live_playlist()

    @app.route("/emby/direct.mp4")
    def emby_direct_mp4():
        if stream_state["mode"] == "presentation":
            if not presentation_video.exists():
                return no_store(Response("No se encontro sofa.mp4.\n", status=404))
            return no_store(send_file(presentation_video, mimetype="video/mp4", conditional=True))

        return current_media_response("video/mp4")

    @app.route("/channels-max.m3u")
    def channels_max_m3u():
        channel_name = "YouTube Live Max"
        playlist = "\n".join([
            "#EXTM3U x-tvg-url=\"{}\"".format(absolute_url("/guide.xml")),
            "#EXTINF:-1 tvg-id=\"youtube-live-max\" tvg-name=\"{}\" group-title=\"YouTube\",{}".format(channel_name, channel_name),
            emby_stream_url(True),
            "",
        ])
        return no_store(Response(playlist, mimetype="application/x-mpegURL"))

    @app.route("/channels-emby-direct.m3u")
    def channels_emby_direct_m3u():
        channel_name = "YouTube Live Direct"
        playlist = "\n".join([
            "#EXTM3U x-tvg-url=\"{}\"".format(absolute_url("/guide.xml")),
            "#EXTINF:-1 tvg-id=\"youtube-live\" tvg-name=\"{}\" group-title=\"YouTube\",{}".format(channel_name, channel_name),
            absolute_url("/emby/direct.m3u8"),
            "",
        ])
        return no_store(Response(playlist, mimetype="application/x-mpegURL"))

    @app.route("/emby/direct.m3u8")
    def emby_direct_playlist():
        if stream_state["mode"] == "presentation":
            return presentation_m3u8()

        upstream_hls_url = stream_state["upstream_hls_url"]
        if not upstream_hls_url:
            if force_presentation("sin directo para emby direct"):
                return presentation_m3u8()
            return no_store(Response("No hay ningun directo conectado.\n", status=404))

        if not StreamExtractor.is_hls_url(upstream_hls_url):
            program_proc = stream_state.get("program_proc")
            if program_proc is None or program_proc.poll() is not None:
                try:
                    _start_persistent_program_stream()
                except Exception as exc:
                    stream_state["program_error"] = str(exc)
            return _wait_for_program_playlist(absolute_urls=True)

        return live_playlist()

    @app.route("/guide.xml")
    def guide_xml():
        channel_name = escape("YouTube Live")
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="youtube-m3u8">
  <channel id="youtube-live">
    <display-name>{channel_name}</display-name>
  </channel>
  <channel id="youtube-live-max">
    <display-name>{channel_name} Max</display-name>
  </channel>
</tv>
""".format(channel_name=channel_name)
        return no_store(Response(xml, mimetype="application/xml"))

    @app.route("/live.m3u8")
    def live_playlist():
        if stream_state["mode"] == "black":
            program_proc = stream_state.get("program_proc")
            if program_proc is None or program_proc.poll() is not None:
                try:
                    _start_persistent_program_stream()
                except Exception as exc:
                    stream_state["program_error"] = str(exc)
            return _wait_for_program_playlist(absolute_urls=True)
        if stream_state["mode"] == "presentation":
            return presentation_m3u8()

        upstream_hls_url = stream_state["upstream_hls_url"]
        if not upstream_hls_url:
            if force_presentation("sin directo"):
                return presentation_m3u8()
            return Response("No hay ningun directo conectado.\n", status=404)

        if not StreamExtractor.is_hls_url(upstream_hls_url):
            program_proc = stream_state.get("program_proc")
            if program_proc is None or program_proc.poll() is not None:
                try:
                    _start_persistent_program_stream()
                except Exception as exc:
                    stream_state["program_error"] = str(exc)
            return _wait_for_program_playlist(absolute_urls=True)

        try:
            response = requests.get(upstream_hls_url, timeout=15)
            response.raise_for_status()
        except requests.RequestException:
            if not refresh_stream_url_if_needed():
                if switch_to_presentation(stream_state["error"] or "stream no disponible"):
                    return presentation_m3u8()
                return no_store(Response(stream_state["error"] + "\n", status=502))

            try:
                response = requests.get(stream_state["upstream_hls_url"], timeout=15)
                response.raise_for_status()
            except requests.RequestException as exc2:
                stream_state["error"] = f"El directo conectado fallo: {exc2}"
                if switch_to_presentation(str(exc2)):
                    return presentation_m3u8()
                return no_store(Response(stream_state["error"] + "\n", status=502))

        return no_store(Response(
            rewrite_playlist(response.text, upstream_hls_url, live_window_segments, stream_state["stream_id"], proxy_signing_key),
            mimetype="application/vnd.apple.mpegurl",
        ))

    @app.route("/live-max.m3u8")
    def live_max_playlist():
        if stream_state["mode"] == "black":
            program_proc = stream_state.get("program_proc")
            if program_proc is None or program_proc.poll() is not None:
                try:
                    _start_persistent_program_stream()
                except Exception as exc:
                    stream_state["program_error"] = str(exc)
            return _wait_for_program_playlist(absolute_urls=True)
        if stream_state["mode"] == "presentation":
            return presentation_m3u8()

        upstream_hls_url = stream_state["upstream_hls_url"]
        if not upstream_hls_url:
            if force_presentation("sin directo"):
                return presentation_m3u8()
            return Response("No hay ningun directo conectado.\n", status=404)

        if not StreamExtractor.is_hls_url(upstream_hls_url):
            program_proc = stream_state.get("program_proc")
            if program_proc is None or program_proc.poll() is not None:
                try:
                    _start_persistent_program_stream()
                except Exception as exc:
                    stream_state["program_error"] = str(exc)
            return _wait_for_program_playlist(absolute_urls=True)

        try:
            response = requests.get(upstream_hls_url, timeout=15)
            response.raise_for_status()
            variant_url = best_variant_url(response.text, upstream_hls_url)
            if not variant_url:
                return no_store(Response(
                    rewrite_playlist(response.text, upstream_hls_url, live_window_segments, stream_state["stream_id"], proxy_signing_key),
                    mimetype="application/vnd.apple.mpegurl",
                ))

            variant_response = requests.get(variant_url, timeout=15)
            variant_response.raise_for_status()
        except requests.RequestException:
            if not refresh_stream_url_if_needed():
                if switch_to_presentation(stream_state["error"] or "stream no disponible"):
                    return presentation_m3u8()
                return no_store(Response(stream_state["error"] + "\n", status=502))

            try:
                refreshed_url = stream_state["upstream_hls_url"]
                response = requests.get(refreshed_url, timeout=15)
                response.raise_for_status()
                variant_url = best_variant_url(response.text, refreshed_url)
                if not variant_url:
                    return no_store(Response(
                        rewrite_playlist(response.text, refreshed_url, live_window_segments, stream_state["stream_id"], proxy_signing_key),
                        mimetype="application/vnd.apple.mpegurl",
                    ))

                variant_response = requests.get(variant_url, timeout=15)
                variant_response.raise_for_status()
            except requests.RequestException as exc2:
                stream_state["error"] = f"El directo conectado fallo: {exc2}"
                if switch_to_presentation(str(exc2)):
                    return presentation_m3u8()
                return no_store(Response(stream_state["error"] + "\n", status=502))

        return no_store(Response(
            rewrite_playlist(variant_response.text, variant_url, live_window_segments, stream_state["stream_id"], proxy_signing_key),
            mimetype="application/vnd.apple.mpegurl",
        ))

    @app.route("/torrent/<source_id>/stream.<ext>", methods=["GET", "HEAD"])
    def torrent_stream(source_id: str, ext: str):
        return _torrent_media_response(source_id)

    @app.route("/current/media.mp4")
    def current_media_mp4():
        return current_media_response("video/mp4")

    @app.route("/current/stream.ts")
    def current_media_ts():
        return current_media_response("video/mp2t")

    def proxy_response(default_mimetype: str = "application/octet-stream"):
        target_url = request.args.get("url")
        if not target_url:
            return no_store(Response("Missing url parameter.\n", status=400))

        request_sid = request.args.get("sid", type=int)
        if request_sid is not None and request_sid != stream_state["stream_id"]:
            return no_store(Response("El canal cambio; descarta esta playlist antigua.\n", status=410))

        kind = {
            "proxy_playlist": "playlist",
            "proxy_media": "media",
        }.get(request.endpoint or "", "segment")
        supplied_sig = request.args.get("sig", "")
        expected_sig = proxy_signature(target_url, request_sid, kind, proxy_signing_key)
        if not allow_unsigned_proxy and not hmac.compare_digest(supplied_sig, expected_sig):
            return no_store(Response("Firma de proxy invalida.\n", status=403))
        if not is_public_http_url(target_url):
            return no_store(Response("Destino de proxy no permitido.\n", status=403))

        is_playlist = "manifest/hls" in target_url or target_url.endswith(".m3u8")
        range_header = request.headers.get("Range")
        if not is_playlist and not range_header:
            cached_path = segment_cache.get(target_url)
            if cached_path:
                return no_store(send_file(cached_path, mimetype=default_mimetype))

        try:
            headers = {"Range": range_header} if range_header else None
            upstream = requests.get(target_url, stream=True, timeout=20, headers=headers)
            upstream.raise_for_status()
        except requests.RequestException as exc:
            stream_state["error"] = f"Fallo el proxy del directo: {exc}"
            stream_state["upstream_hls_url"] = None
            switch_to_presentation(str(exc))
            return no_store(Response(stream_state["error"] + "\n", status=502))

        content_type = upstream.headers.get("content-type", "")
        if "mpegurl" in content_type or is_playlist:
            playlist = rewrite_playlist(upstream.text, target_url, live_window_segments, stream_state["stream_id"], proxy_signing_key)
            return no_store(Response(playlist, mimetype="application/vnd.apple.mpegurl"))

        if not range_header and segment_cache.should_cache(upstream.headers.get("content-length")):
            try:
                cached_path = segment_cache.put(target_url, upstream)
                return no_store(send_file(cached_path, mimetype=content_type or default_mimetype))
            except ValueError:
                pass

        def generate():
            for chunk in upstream.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk

        response_headers = {}
        for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if header in upstream.headers:
                response_headers[header] = upstream.headers[header]

        return no_store(Response(
            generate(),
            status=upstream.status_code,
            headers=response_headers,
            mimetype=content_type or default_mimetype,
        ))

    @app.route("/proxy")
    def proxy():
        return proxy_response()

    @app.route("/p/playlist.m3u8")
    def proxy_playlist():
        return proxy_response("application/vnd.apple.mpegurl")

    @app.route("/p/segment.ts")
    def proxy_segment():
        return proxy_response("video/MP2T")

    @app.route("/p/media.mp4")
    def proxy_media():
        return proxy_response("video/mp4")

    # ----- API endpoints for TV playout -----

    @app.route("/api/sources", methods=["GET"])
    def api_sources():
        items = [public_source(s) for s in playout.get_sources()]
        for item in items:
            item.setdefault("group", _source_group_for(item))
        return jsonify(items)

    @app.route("/api/torrents/status", methods=["GET"])
    def api_torrents_status():
        torrents = [
            item for item in torrent_manager.statuses()
            if playout.find_source_by_id(item.get("id", "")) is not None
        ]
        return jsonify({"ok": True, "torrents": torrents})

    @app.route("/api/sources/add_youtube", methods=["POST"])
    def api_add_youtube():
        name = request.form.get("name", "").strip() or "YouTube"
        url = request.form.get("url", "").strip()
        if not url:
            return jsonify({"ok": False, "error": "URL requerida"})
        if not is_allowed_source_url(url):
            return jsonify({"ok": False, "error": "URL no permitida"})
        meta = {}
        try:
            md = StreamExtractor(url).get_metadata()
            meta["duration_seconds"] = md["duration_seconds"]
            meta["duration_label"] = md["duration_label"]
            meta["is_live"] = md["is_live"]
            if not name or name == "YouTube":
                name = md["title"]
        except Exception:
            pass
        src_id = playout.add_source({
            "name": name,
            "type": "youtube",
            "url": url,
            **meta,
        })
        return jsonify({"ok": True, "source_id": src_id})

    def probe_hls(url: str) -> dict:
        if not is_allowed_source_url(url):
            raise ValueError("URL no permitida. Usa http/https con host publico.")
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException:
            return {"duration_seconds": None, "duration_label": "Directo", "is_live": True}
        content = resp.text
        if "#EXT-X-ENDLIST" not in content:
            return {"duration_seconds": None, "duration_label": "Directo", "is_live": True}
        total = 0.0
        for line in content.splitlines():
            if line.startswith("#EXTINF:"):
                try:
                    total += float(line.split(":")[1].split(",")[0])
                except (ValueError, IndexError):
                    pass
        hours = int(total // 3600)
        minutes = int((total % 3600) // 60)
        seconds = int(total % 60)
        label = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours > 0 else f"{minutes:02d}:{seconds:02d}"
        return {"duration_seconds": int(total), "duration_label": label, "is_live": False}

    @app.route("/api/sources/add_hls", methods=["POST"])
    def api_add_hls():
        name = request.form.get("name", "").strip() or "HLS Directo"
        url = request.form.get("url", "").strip()
        if not url:
            return jsonify({"ok": False, "error": "URL requerida"})
        if not is_allowed_source_url(url):
            return jsonify({"ok": False, "error": "URL no permitida"})
        meta = probe_hls(url)
        src_id = playout.add_source({
            "name": name,
            "type": "hls",
            "url": url,
            **meta,
        })
        return jsonify({"ok": True, "source_id": src_id})

    @app.route("/api/sources/add_file", methods=["POST"])
    def api_add_file():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"ok": False, "error": "Selecciona un archivo de video"}), 400

        original_filename = os.path.basename(upload.filename.replace("\\", "/"))
        extension = Path(original_filename).suffix.lower()
        if extension not in ALLOWED_VIDEO_EXTENSIONS:
            allowed = ", ".join(sorted(ext.lstrip(".").upper() for ext in ALLOWED_VIDEO_EXTENSIONS))
            return jsonify({"ok": False, "error": f"Formato no permitido. Usa {allowed}"}), 400

        name = request.form.get("name", "").strip() or Path(original_filename).stem
        media_dir.mkdir(parents=True, exist_ok=True)
        stored_path = media_dir / f"{uuid.uuid4().hex}{extension}"
        try:
            upload.save(stored_path)
            if stored_path.stat().st_size <= 0:
                raise ValueError("El archivo esta vacio")
            meta = probe_video_file(stored_path)
        except Exception as exc:
            try:
                stored_path.unlink(missing_ok=True)
            except OSError:
                pass
            return jsonify({"ok": False, "error": f"No se pudo procesar el video: {exc}"}), 400

        resolved_path = str(stored_path.resolve())
        src_id = playout.add_source({
            "name": name,
            "type": "file",
            "url": resolved_path,
            "path": resolved_path,
            "original_filename": original_filename,
            "size_bytes": stored_path.stat().st_size,
            "emit_enabled": True,
            **meta,
        })
        return jsonify({
            "ok": True,
            "source_id": src_id,
            "name": name,
            "duration_label": meta.get("duration_label"),
            "size_bytes": stored_path.stat().st_size,
        })

    @app.route("/api/sources/add_torrent", methods=["POST"])
    def api_add_torrent():
        name = request.form.get("name", "").strip()
        magnet = request.form.get("magnet", "").strip()
        upload = request.files.get("file")
        trying_file = bool(upload and upload.filename)

        if trying_file:
            original_filename = os.path.basename(upload.filename.replace("\\", "/"))
            extension = Path(original_filename).suffix.lower()
            if extension not in ALLOWED_TORRENT_EXTENSIONS:
                return jsonify({
                    "ok": False,
                    "error": "Formato no permitido. Usa un archivo .torrent",
                }), 400
            try:
                data = upload.read()
                meta = inspect_torrent_data(data)
            except TorrentError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            display_name = name or meta.get("name") or Path(original_filename).stem
            source = {
                "name": display_name,
                "type": "torrent",
                "url": "",
                "original_filename": original_filename,
                "size_bytes": meta.get("total_size"),
                "emit_enabled": True,
            }
            src_id = playout.add_source(source)
            _mark_torrent_probe(src_id)
            try:
                desc = torrent_manager.add_torrent_data(src_id, data, display_name)
            except TorrentError as exc:
                _consume_torrent_probe(src_id)
                torrent_manager.remove(src_id, delete_files=True)
                playout.delete_source(src_id)
                return jsonify({"ok": False, "error": str(exc)}), 400
            return jsonify({
                "ok": True,
                "source_id": src_id,
                "name": display_name,
                "metadata_ready": bool(desc.get("metadata")),
                "size_bytes": desc.get("size_bytes"),
                "duration_label": None,
            })

        if magnet:
            try:
                meta = inspect_magnet(magnet)
            except TorrentError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            display_name = name or meta.get("name") or "Torrent"
            source = {
                "name": display_name,
                "type": "torrent",
                "url": magnet,
                "magnet": magnet,
                "emit_enabled": True,
            }
            src_id = playout.add_source(source)
            _mark_torrent_probe(src_id)
            try:
                desc = torrent_manager.add_magnet(src_id, magnet, display_name)
            except TorrentError as exc:
                _consume_torrent_probe(src_id)
                torrent_manager.remove(src_id, delete_files=True)
                playout.delete_source(src_id)
                return jsonify({"ok": False, "error": str(exc)}), 400
            return jsonify({
                "ok": True,
                "source_id": src_id,
                "name": display_name,
                "metadata_ready": bool(desc.get("metadata")),
                "size_bytes": desc.get("size_bytes"),
                "duration_label": None,
            })

        return jsonify({
            "ok": False,
            "error": "Selecciona un archivo .torrent o pega un enlace magnet",
        }), 400

    def _remove_source_media_file(source: dict | None) -> None:
        if not source:
            return
        if source.get("type") == "torrent":
            torrent_manager.remove(source.get("id", ""), delete_files=True)
            return
        if source.get("type") != "file":
            return
        raw_path = source.get("path") or source.get("url") or ""
        if not raw_path:
            return
        try:
            resolved = Path(raw_path).resolve()
            media_root = media_dir.resolve()
            if resolved != media_root and media_root not in resolved.parents:
                return
        except OSError:
            return
        for other in playout.get_sources():
            if other.get("id") == source.get("id"):
                continue
            other_path = other.get("path") or other.get("url")
            if not other_path:
                continue
            try:
                if Path(other_path).resolve() == resolved:
                    return
            except OSError:
                continue
        try:
            resolved.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("No se pudo borrar el archivo %s: %s", resolved, exc)

    @app.route("/api/load_iptv", methods=["POST"])
    def api_load_iptv():
        iptv_url = request.form.get("iptv_url", "").strip()
        if not iptv_url:
            return jsonify({"ok": False, "error": "URL requerida", "channels": []})
        if not is_allowed_source_url(iptv_url):
            return jsonify({"ok": False, "error": "URL no permitida", "channels": []})
        try:
            r = requests.get(iptv_url, timeout=30)
            r.raise_for_status()
            channels = parse_m3u(r.text)
        except requests.RequestException as exc:
            return jsonify({"ok": False, "error": f"No se pudo descargar: {exc}", "channels": []})
        return jsonify({"ok": True, "channels": channels, "iptv_url": iptv_url})

    @app.route("/api/sources/add_iptv", methods=["POST"])
    def api_add_iptv():
        name = request.form.get("name", "").strip() or "Canal IPTV"
        url = request.form.get("url", "").strip()
        if not url:
            return jsonify({"ok": False, "error": "URL requerida"})
        if not is_allowed_source_url(url):
            return jsonify({"ok": False, "error": "URL no permitida"})
        src_id = playout.add_source({
            "name": name,
            "type": "iptv",
            "url": url,
        })
        return jsonify({"ok": True, "source_id": src_id})

    @app.route("/api/sources/delete", methods=["POST"])
    def api_delete_source():
        source_id = request.form.get("source_id", "").strip()
        if not source_id:
            return jsonify({"ok": False, "error": "source_id requerido"})
        source = playout.find_source_by_id(source_id)
        was_current = bool(source and stream_state.get("current_source_id") == source_id)
        ok = playout.delete_source(source_id)
        if ok:
            if was_current and source and source.get("type") == "torrent":
                _set_relay_source(None)
                _stop_gstreamer_source_stream()
                force_presentation("fuente torrent eliminada")
            _remove_source_media_file(source)
        return jsonify({"ok": ok})

    @app.route("/api/sources/toggle_auto", methods=["POST"])
    def api_toggle_source_auto():
        source_id = request.form.get("source_id", "").strip()
        if not source_id:
            return jsonify({"ok": False, "error": "source_id requerido"})
        ok = playout.toggle_source_auto(source_id)
        return jsonify({"ok": ok})

    @app.route("/api/sources/<source_id>/play", methods=["POST"])
    def api_play_source(source_id: str):
        source = playout.find_source_by_id(source_id)
        if not source:
            return jsonify({"ok": False, "error": "Fuente no encontrada"})
        ok = activate_source(source, "manual", None)
        return jsonify({"ok": ok, "source_name": source.get("name", source_id)})

    @app.route("/api/sources/epg", methods=["POST"])
    def api_source_epg():
        source_id = request.form.get("source_id", "").strip()
        if not source_id:
            return jsonify({"ok": False, "error": "source_id requerido"})
        source = playout.find_source_by_id(source_id)
        if not source:
            return jsonify({"ok": False, "error": "Fuente no encontrada"})

        provider_id = source.get("iptv_provider")
        if not provider_id:
            return jsonify({"ok": False, "error": "La fuente no es IPTV Xtream"})

        epg_channels = _get_provider_epg(provider_id, include_programmes=False)
        epg_index = build_xmltv_index(epg_channels)
        xmltv_id = resolve_channel_xmltv_id(source, epg_index)
        if not xmltv_id:
            return jsonify({"ok": False, "error": "Canal no encontrado en el EPG del proveedor"})
        epg_data = _get_provider_epg(provider_id, channel_ids={xmltv_id})
        raw = epg_data.get("programmes", {}).get(xmltv_id, [])

        def _decode_text(value):
            if value is None:
                return ""
            text = str(value)
            try:
                import base64
                decoded = base64.b64decode(text).decode("utf-8", errors="ignore").strip()
                if decoded:
                    return decoded
            except Exception:
                pass
            return text

        entries = []
        for item in raw:
            title = _decode_text(item.get("title") or item.get("name") or "Programa")
            desc = _decode_text(item.get("description") or "")
            start_iso = item.get("start") or item.get("start_timestamp") or item.get("start_time") or ""
            end_iso = item.get("end") or item.get("end_timestamp") or item.get("end_time") or ""
            start_time = ""
            if isinstance(start_iso, str):
                if len(start_iso) >= 16 and start_iso[4] == "-":
                    start_time = start_iso[11:16]
                elif len(start_iso) >= 12:
                    start_time = start_iso[8:12]
                    if len(start_time) == 4:
                        start_time = start_time[:2] + ":" + start_time[2:]
                    else:
                        start_time = ""
            if not start_time and item.get("start_timestamp"):
                try:
                    start_time = datetime.fromtimestamp(int(item.get("start_timestamp"))).strftime("%H:%M")
                except Exception:
                    start_time = ""
            entries.append({
                "title": title or "Programa",
                "description": desc,
                "start": str(start_iso or ""),
                "end": str(end_iso or ""),
                "time": start_time,
            })

        entries.sort(key=lambda x: x.get("start", ""))
        return jsonify({"ok": True, "source": {"id": source_id, "name": source.get("name", source_id), "xmltv_id": xmltv_id}, "entries": entries})

    _xmltv_cache: dict[str, dict] = {}
    _xmltv_cache_lock = threading.Lock()
    _XMLTV_TTL_SECONDS = 6 * 3600

    def _sync_epg_store(provider_id: str, data: dict) -> None:
        try:
            epg_store.upsert_epg(provider_id, data)
        except Exception as exc:
            print(f"[epg_store] upsert failed for {provider_id}: {exc}")

    def _get_provider_epg(provider_id: str, channel_ids: set[str] | None = None, include_programmes: bool = True) -> dict:
        provider = get_provider(provider_id)
        if not provider:
            return {"channels": {}, "programmes": {}, "error": "proveedor no encontrado"}
        if not include_programmes:
            with _xmltv_cache_lock:
                cached = _xmltv_cache.get(provider_id)
                if cached and (time.time() - cached.get("ts", 0)) < _XMLTV_TTL_SECONDS:
                    return cached["data"]
        try:
            if epg_store.channel_count(provider_id) > 0:
                channels = epg_store.list_channels(provider_id)
                if include_programmes:
                    programmes = epg_store.channels_with_programmes(provider_id, channel_ids)
                else:
                    programmes = {}
                data = {"channels": channels, "programmes": programmes, "cached": True, "source_url": "sqlite"}
                if not include_programmes:
                    with _xmltv_cache_lock:
                        _xmltv_cache[provider_id] = {"ts": time.time(), "data": data}
                return data
        except Exception:
            pass
        try:
            path = ensure_xtream_xmltv_cache(provider)
            data = parse_epg_file(path, channel_ids=None, include_programmes=True)
        except Exception as exc:
            return {"channels": {}, "programmes": {}, "error": str(exc)}
        _sync_epg_store(provider_id, data)
        if not include_programmes:
            channels_only = {
                cid: {"id": cid, "name": meta.get("name", cid), "icon": meta.get("icon", "")}
                for cid, meta in data.get("channels", {}).items()
            }
            data = {"channels": channels_only, "programmes": {}, "cached": True, "source_url": "sqlite-imported"}
            with _xmltv_cache_lock:
                _xmltv_cache[provider_id] = {"ts": time.time(), "data": data}
            return data
        programmes = data.get("programmes", {})
        if channel_ids:
            programmes = {cid: progs for cid, progs in programmes.items() if cid in channel_ids}
        data["programmes"] = programmes
        return data

    @app.route("/api/sources/<source_id>/epg_channels", methods=["GET"])
    def api_source_epg_channels(source_id: str):
        source = playout.find_source_by_id(source_id)
        if not source:
            return jsonify({"ok": False, "error": "Fuente no encontrada"})
        provider_id = source.get("iptv_provider")
        stream_id = source.get("iptv_stream_id")
        category_id = source.get("iptv_category_id")
        if not provider_id or not category_id:
            return jsonify({"ok": False, "error": "La fuente no es IPTV Xtream"})

        provider = get_provider(provider_id)
        if not provider:
            return jsonify({"ok": False, "error": "Proveedor Xtream no encontrado"})

        try:
            streams = channels_for_category(provider, category_id)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Error listando canales: {exc}"})

        epg_channels = _get_provider_epg(provider_id, include_programmes=False)
        epg_index = build_xmltv_index(epg_channels)
        now = datetime.utcnow()
        channels = []
        xmltv_ids = set()
        resolved_streams = []
        for s in streams:
            xmltv_id = resolve_channel_xmltv_id(s, epg_index)
            if xmltv_id:
                xmltv_ids.add(xmltv_id)
            resolved_streams.append((s, xmltv_id))
        epg_data = _get_provider_epg(provider_id, channel_ids=xmltv_ids) if xmltv_ids else {"programmes": {}}
        for s, xmltv_id in resolved_streams:
            nn = channel_now_next(epg_data, xmltv_id, now=now) if xmltv_id else {"now": None, "next": None}
            channels.append({
                "stream_id": s.get("iptv_stream_id"),
                "name": s.get("name", ""),
                "group": s.get("iptv_group", ""),
                "xmltv_id": xmltv_id,
                "now": nn.get("now"),
                "next": nn.get("next"),
            })
        channels.sort(key=lambda c: (c.get("group", ""), c.get("name", "")))
        return jsonify({
            "ok": True,
            "provider_id": provider_id,
            "provider_name": provider.get("name", provider_id),
            "category_id": category_id,
            "group": source.get("iptv_group", ""),
            "current_stream_id": stream_id,
            "epg_error": epg_channels.get("error") or epg_data.get("error"),
            "epg_cached": epg_channels.get("cached", False),
            "channels": channels,
        })

    @app.route("/api/calendar/add_from_epg", methods=["POST"])
    def api_add_calendar_from_epg():
        date = request.form.get("date", "").strip()
        source_id = request.form.get("source_id", "").strip()
        insert_before = request.form.get("insert_before", "").strip() or None
        title = request.form.get("title", "").strip() or "Programa"
        start_iso = request.form.get("start", "").strip()
        end_iso = request.form.get("end", "").strip()
        epg_channel_id = request.form.get("epg_channel_id", "").strip()
        epg_description = request.form.get("epg_description", "").strip()
        epg_channel_name = request.form.get("epg_channel_name", "").strip()
        if not date or not source_id or not start_iso:
            return jsonify({"ok": False, "error": "date, source_id y start requeridos"})

        def _to_hm(value: str) -> str:
            if not value:
                return ""
            digits = re.sub(r"[^0-9]", "", value)
            if len(digits) >= 12:
                return f"{digits[8:10]}:{digits[10:12]}"
            return ""

        time_val = _to_hm(start_iso)
        end_time_val = _to_hm(end_iso)
        if not time_val:
            return jsonify({"ok": False, "error": "start no tiene formato XMLTV reconocible"})

        cal_id = playout.insert_calendar_entry({
            "date": date,
            "time": time_val,
            "source_id": source_id,
            "title": title,
            "start_mode": "time",
            "end_time": end_time_val,
            "epg_title": title,
            "epg_description": epg_description,
            "epg_channel_id": epg_channel_id,
            "epg_channel_name": epg_channel_name,
            "epg_start": start_iso,
            "epg_end": end_iso,
        }, insert_before)
        entries = playout.get_calendar(date)
        state = playout.get_state()
        source_map = {s["id"]: s["name"] for s in state["sources"]}
        return jsonify({"ok": bool(cal_id), "cal_id": cal_id, "entries": entries, "source_map": source_map})

    @app.route("/epg/<provider_id>.xml", methods=["GET"])
    def api_epg_provider_xml(provider_id: str):
        path = Path("data/epg") / (re.sub(r"[^a-zA-Z0-9_-]", "_", provider_id) + ".xml")
        if not path.exists():
            return "EPG no disponible para este proveedor", 404
        return send_file(str(path), mimetype="application/xml")

    def _enrich_entry(entry: dict, source_map: dict[str, str]) -> dict:
        if not entry:
            return entry
        out = dict(entry)
        out["source_name"] = source_map.get(entry.get("source_id", ""), entry.get("source_id", "") or "")
        return out

    @app.route("/api/calendar", methods=["POST"])
    def api_calendar():
        date = request.form.get("date", now_date())
        entries = playout.get_calendar(date)
        state = playout.get_state()
        source_map = {s["id"]: s["name"] for s in state["sources"]}
        enriched = [_enrich_entry(e, source_map) for e in entries]
        return jsonify({"entries": enriched, "source_map": source_map})

    @app.route("/api/calendar/reorder", methods=["POST"])
    def api_calendar_reorder():
        date = request.form.get("date", "").strip()
        ids_str = request.form.get("ids", "").strip()
        if not date or not ids_str:
            return jsonify({"ok": False, "error": "date y ids requeridos"})
        cal_ids = ids_str.split(",")
        ok = playout.reorder_calendar(date, cal_ids)
        entries = playout.get_calendar(date)
        state = playout.get_state()
        source_map = {s["id"]: s["name"] for s in state["sources"]}
        return jsonify({"ok": ok, "entries": entries, "source_map": source_map})

    @app.route("/api/calendar/last", methods=["POST"])
    def api_calendar_last():
        date = request.form.get("date", now_date())
        entries = playout.get_calendar(date)
        last = entries[-1] if entries else None
        return jsonify({"last": last})

    def _enrich_calendar_with_epg(cal_id: str, source: dict):
        try:
            time.sleep(0.5)
            entry = playout.find_calendar_entry(cal_id)
            if not entry:
                return
            time_str = entry.get("time", "")
            if not time_str or ":" not in time_str:
                time_str = now_hm()
            provider_id = source.get("iptv_provider")
            if not provider_id:
                return
            epg_channels = _get_provider_epg(provider_id, include_programmes=False)
            epg_index = build_xmltv_index(epg_channels)
            xmltv_id = resolve_channel_xmltv_id(source, epg_index)
            if not xmltv_id:
                return
            epg_data = _get_provider_epg(provider_id, channel_ids={xmltv_id})
            entries = epg_data.get("programmes", {}).get(xmltv_id, [])
            if not entries:
                return
            date_str = entry.get("date", "")
            target_dt = date_str.replace("-", "") + time_str.replace(":", "") + "00"
            best = None
            for epg in entries:
                start = epg.get("start", "")
                end = epg.get("end", "") or epg.get("stop", "")
                if start and start <= target_dt and (not end or target_dt < end):
                    best = epg
                    break
            if not best and entries:
                best = entries[0]
            if best:
                title = best.get("title") or ""
                desc = best.get("description") or ""
                end_raw = best.get("end") or best.get("stop") or ""
                epg_end = ""
                if isinstance(end_raw, str) and len(end_raw) >= 12:
                    hm = end_raw[8:12]
                    if len(hm) == 4:
                        epg_end = hm[:2] + ":" + hm[2:]
                playout.update_calendar_entry(entry["id"], {
                    "epg_title": title,
                    "epg_description": desc,
                    "epg_channel_name": xmltv_id,
                    "epg_end_time": epg_end,
                })
        except Exception:
            pass

    @app.route("/api/calendar/add_from_source", methods=["POST"])
    def api_add_from_source():
        date = request.form.get("date", "").strip()
        source_id = request.form.get("source_id", "").strip()
        insert_before = request.form.get("insert_before", "").strip() or None
        if not date or not source_id:
            return jsonify({"ok": False, "error": "date y source_id requeridos"})
        source = playout.find_source_by_id(source_id)
        if not source:
            return jsonify({"ok": False, "error": "Fuente no encontrada"})
        title = source.get("name", "Programa")
        has_duration = bool(source.get("duration_seconds"))
        existing = playout.get_calendar(date)
        is_first = not existing
        start_mode = "after_previous" if (is_first or not has_duration) else "time"
        cal_id = playout.insert_calendar_entry({
            "date": date,
            "source_id": source_id,
            "title": title,
            "start_mode": start_mode,
        }, insert_before)
        if cal_id:
            warm_youtube_hls(source)
            thread = threading.Thread(target=_enrich_calendar_with_epg, args=(cal_id, source), daemon=True)
            thread.start()
        entries = playout.get_calendar(date)
        state = playout.get_state()
        source_map = {s["id"]: s["name"] for s in state["sources"]}
        return jsonify({"ok": True, "cal_id": cal_id, "entries": entries, "source_map": source_map})

    @app.route("/api/calendar/add", methods=["POST"])
    def api_add_calendar():
        date = request.form.get("date", "").strip()
        time_val = request.form.get("time", "").strip()
        source_id = request.form.get("source_id", "").strip()
        title = request.form.get("title", "").strip() or "Programa"
        start_mode = request.form.get("start_mode", "time")
        if not date or not source_id:
            return jsonify({"ok": False, "error": "date y source_id requeridos"})
        source = playout.find_source_by_id(source_id)
        if source and not source.get("duration_seconds"):
            start_mode = "after_previous"
        if start_mode != "after_previous" and not time_val:
            return jsonify({"ok": False, "error": "time requerido para programas con hora"})
        if source and source.get("type") == "youtube":
            warm_youtube_hls(source)
        cal_id = playout.add_calendar_entry({
            "date": date,
            "time": time_val,
            "source_id": source_id,
            "title": title,
            "start_mode": start_mode,
        })
        return jsonify({"ok": True, "cal_id": cal_id})

    @app.route("/api/calendar/enrich_epg", methods=["POST"])
    def api_calendar_enrich_epg():
        cal_id = request.form.get("cal_id", "").strip()
        if not cal_id:
            return jsonify({"ok": False, "error": "cal_id requerido"})
        entry = playout.find_calendar_entry(cal_id)
        if not entry:
            return jsonify({"ok": False, "error": "Entrada no encontrada"})
        source_id = entry.get("source_id", "")
        source = playout.find_source_by_id(source_id)
        if not source:
            return jsonify({"ok": False, "error": "Fuente no encontrada"})
        _enrich_calendar_with_epg(cal_id, source)
        updated = playout.find_calendar_entry(cal_id)
        return jsonify({"ok": True, "entry": updated})

    @app.route("/api/calendar/delete", methods=["POST"])
    def api_delete_calendar():
        cal_id = request.form.get("cal_id", "").strip()
        if not cal_id:
            return jsonify({"ok": False, "error": "cal_id requerido"})
        ok = playout.delete_calendar_entry(cal_id)
        return jsonify({"ok": ok})

    @app.route("/api/calendar/update", methods=["POST"])
    def api_update_calendar():
        cal_id = request.form.get("cal_id", "").strip()
        if not cal_id:
            return jsonify({"ok": False, "error": "cal_id requerido"})
        updates = {}
        time_val = request.form.get("time", "").strip()
        title = request.form.get("title", "").strip()
        source_id = request.form.get("source_id", "").strip()
        start_mode = request.form.get("start_mode", "").strip()
        if title:
            updates["title"] = title
        if source_id:
            updates["source_id"] = source_id
        if start_mode:
            updates["start_mode"] = start_mode
        if start_mode == "after_previous":
            updates["time"] = ""
            updates["time_locked"] = False
        elif time_val:
            updates["time"] = time_val
        ok = playout.update_calendar_entry(cal_id, updates)
        return jsonify({"ok": ok})

    @app.route("/api/calendar/toggle", methods=["POST"])
    def api_toggle_calendar():
        cal_id = request.form.get("cal_id", "").strip()
        if not cal_id:
            return jsonify({"ok": False, "error": "cal_id requerido"})
        ok = playout.toggle_calendar_entry(cal_id)
        return jsonify({"ok": ok})

    @app.route("/api/calendar/play_now", methods=["POST"])
    def api_play_now():
        cal_id = request.form.get("cal_id", "").strip()
        if not cal_id:
            return jsonify({"ok": False, "error": "cal_id requerido"})
        entry = None
        for e in playout.get_calendar():
            if e["id"] == cal_id:
                entry = e
                break
        if not entry:
            return jsonify({"ok": False, "error": "Programa no encontrado"})
        source = playout.find_source_by_id(entry["source_id"])
        if not source:
            return jsonify({"ok": False, "error": "Fuente no encontrada"})
        playout.set_calendar_played(cal_id)
        ok = activate_source(source, "calendar", cal_id)
        return jsonify({"ok": ok})

    @app.route("/api/auto/toggle", methods=["POST"])
    def api_toggle_auto():
        playout.set_auto_enabled(not playout.is_auto_enabled())
        return jsonify({"ok": True, "auto_enabled": playout.is_auto_enabled()})

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        ap = playout.find_next_after_previous()
        if ap:
            source, cal_id = ap
            playout.set_calendar_played(cal_id)
            if activate_source(source, "after_previous", cal_id):
                return jsonify({"ok": True, "after_previous": True})
        try:
            _set_relay_source(None)
            stream_state["last_previewed_source_id"] = None
            stream_state["stream_id"] += 1
            stream_state["mode"] = "black"
            stream_state["source_url"] = "Pantalla negra con reloj"
            stream_state["source_url_name"] = "Negro + reloj"
            stream_state["upstream_hls_url"] = None
            stream_state["current_source_id"] = None
            stream_state["current_reason"] = "black"
            stream_state["current_calendar_id"] = None
            stream_state["error"] = "Mostrando pantalla negra con reloj."
            stream_state["include_clock"] = True
            _start_persistent_program_stream()
            return jsonify({"ok": True, "black_clock": True})
        except Exception as exc:
            stream_state["error"] = f"No se pudo detener a negro: {exc}"
            return jsonify({"ok": False, "error": str(exc)})

    @app.route("/api/auto/next", methods=["POST"])
    def api_auto_next():
        ap = playout.find_next_after_previous()
        if ap:
            source, cal_id = ap
            stream_state["pending_source"] = {
                "source": source,
                "calendar_id": cal_id,
                "reason": "after_previous",
            }
        else:
            if not playout.is_auto_enabled():
                return jsonify({"ok": False, "error": "Auto no activo"})
            source = playout.get_next_preview_source(
                prev_id=stream_state.get("last_previewed_source_id"),
                current_id=stream_state.get("current_source_id"),
            )
            if not source:
                return jsonify({"ok": False, "error": "No hay fuentes auto disponibles"})
            stream_state["pending_source"] = {
                "source": source,
                "calendar_id": None,
                "reason": "auto",
            }

        stream_state["last_previewed_source_id"] = source.get("id")

        try:
            source_type = source.get("type", "youtube")
            if source_type not in ("file", "torrent"):
                url = source["url"]
                hls_url = _hls_url_for_source(url, source_type)
                if hls_url:
                    start_preview_stream(hls_url)
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "source_name": source.get("name"),
            "pending": True,
        })

    @app.route("/api/emit_now", methods=["POST"])
    def api_emit_now():
        mode = request.args.get("mode", "")
        calendar_date = request.args.get("date", "").strip() or None
        pending = stream_state.get("pending_source")
        if mode != "program" and pending:
            source = pending["source"]
            cal_id = pending.get("calendar_id")
            reason = pending.get("reason", "manual")
            stream_state["pending_source"] = None
            stream_state["last_previewed_source_id"] = None
            if cal_id:
                playout.set_calendar_played(cal_id)
            ok = activate_source(source, reason, cal_id)
            return jsonify({
                "ok": ok,
                "source_name": source.get("name"),
                "error": None if ok else stream_state.get("error"),
            })

        ap = playout.find_first_calendar_entry(calendar_date) if mode == "program" else playout.find_next_after_previous()
        if not ap and mode == "program":
            ap = playout.find_first_calendar_entry(calendar_date, include_played=True)
        if ap:
            source, cal_id = ap
            playout.set_calendar_played(cal_id)
            stream_state["last_previewed_source_id"] = None
            ok = activate_source(source, "program", cal_id)
            return jsonify({"ok": ok, "source_name": source.get("name")})

        if mode == "program":
            return jsonify({"ok": False, "error": "No hay programas pendientes"})

        if not playout.is_auto_enabled():
            return jsonify({"ok": False, "error": "Auto no activo"})

        source = playout.get_next_auto_source()
        if not source:
            return jsonify({"ok": False, "error": "No hay fuentes auto disponibles"})

        stream_state["last_previewed_source_id"] = None
        ok = activate_source(source, "auto", None)
        return jsonify({"ok": ok, "source_name": source.get("name")})

    @app.route("/api/history", methods=["GET", "POST"])
    def api_history():
        limit = 100
        if request.method == "POST":
            try:
                limit = int(request.form.get("limit", 100))
            except (ValueError, TypeError):
                pass
        history = playout.get_history(limit)
        return jsonify({"history": list(reversed(history))})

    @app.route("/api/sources/validate", methods=["POST"])
    def api_validate_source():
        source_id = request.form.get("source_id", "").strip()
        if not source_id:
            return jsonify({"ok": False, "error": "source_id requerido"})
        source = playout.find_source_by_id(source_id)
        if not source:
            return jsonify({"ok": False, "error": "Fuente no encontrada"})

        source_type = source.get("type", "youtube")
        url = source.get("url", "")
        status = "ok"
        error_msg = None

        try:
            if source_type == "torrent":
                if torrent_manager.get(source_id) is None:
                    _ensure_torrent_entry(source)
                desc = torrent_manager.descriptor(source_id)
                if desc is None:
                    raise ValueError("Torrent no cargado")
                if desc.get("error"):
                    raise ValueError(desc["error"])
                if not desc.get("metadata"):
                    status = "pending"
                    error_msg = "Esperando metadatos y peers del torrent"
                elif not desc.get("ready"):
                    raise ValueError("Todavía no hay un archivo de vídeo disponible")
            elif source_type == "file":
                media_path = Path(source.get("path") or url or "")
                if not media_path.is_file():
                    raise ValueError("Archivo de video no encontrado")
                probe_video_file(media_path)
            else:
                if not is_allowed_source_url(url):
                    raise ValueError("URL no permitida")
                if source_type == "hls" or ".m3u8" in url:
                    resp = requests.head(url, timeout=10, allow_redirects=True)
                    resp.raise_for_status()
                else:
                    hls_url = StreamExtractor(url).get_hls_url()
                    requests.head(hls_url, timeout=10, allow_redirects=True)
        except Exception as exc:
            status = "error"
            error_msg = str(exc)

        playout.set_source_validation(source_id, status, error_msg)
        return jsonify({"ok": True, "status": status, "error": error_msg})

    @app.route("/api/sources/update", methods=["POST"])
    def api_update_source():
        source_id = request.form.get("source_id", "").strip()
        if not source_id:
            return jsonify({"ok": False, "error": "source_id requerido"})
        updates = {}
        name = request.form.get("name")
        if name is not None:
            updates["name"] = name.strip()
        url = request.form.get("url")
        if url is not None:
            url = url.strip()
            if not is_allowed_source_url(url):
                return jsonify({"ok": False, "error": "URL no permitida"})
            updates["url"] = url
            source_type = request.form.get("type") or playout.find_source_by_id(source_id).get("type", "youtube")
            if source_type == "youtube":
                try:
                    md = StreamExtractor(url).get_metadata()
                    updates["duration_seconds"] = md["duration_seconds"]
                    updates["duration_label"] = md["duration_label"]
                    updates["is_live"] = md["is_live"]
                except Exception:
                    pass
            elif source_type == "hls" or ".m3u8" in url:
                meta = probe_hls(url)
                updates.update(meta)
        emit_enabled = request.form.get("emit_enabled")
        if emit_enabled is not None:
            updates["emit_enabled"] = emit_enabled in ("1", "true", "True", "on")
        if not updates:
            return jsonify({"ok": False, "error": "Sin cambios"})
        ok = playout.update_source(source_id, updates)
        return jsonify({"ok": ok})

    @app.route("/api/sources/probe", methods=["POST"])
    def api_probe_source():
        source_id = request.form.get("source_id", "").strip()
        if not source_id:
            return jsonify({"ok": False, "error": "source_id requerido"})
        source = playout.find_source_by_id(source_id)
        if not source:
            return jsonify({"ok": False, "error": "Fuente no encontrada"})

        updates = {}
        status = "ok"
        error_msg = None

        try:
            if source.get("type") == "torrent":
                if torrent_manager.get(source_id) is None:
                    _ensure_torrent_entry(source)
                if torrent_manager.get(source_id) is None:
                    raise ValueError("Torrent no cargado")
                # Fail fast if metadata still is not available.
                torrent_manager.stream_size(source_id, timeout=min(15.0, torrent_metadata_timeout))
                meta = _probe_torrent_metadata(source_id)
                if meta is None:
                    raise ValueError("No se pudo analizar el video del torrent")
                updates.update(meta)
            elif source["type"] == "file":
                media_path = Path(source.get("path") or source.get("url") or "")
                if not media_path.is_file():
                    raise ValueError("Archivo de video no encontrado")
                updates.update(probe_video_file(media_path))
            elif source["type"] == "youtube":
                md = StreamExtractor(source["url"]).get_metadata()
                updates["duration_seconds"] = md["duration_seconds"]
                updates["duration_label"] = md["duration_label"]
                updates["is_live"] = md["is_live"]
            elif source["type"] in ("hls", "iptv") or ".m3u8" in source.get("url", ""):
                meta = probe_hls(source["url"])
                updates.update(meta)
            elif source["type"] == "presentation":
                updates["duration_label"] = "Directo"
                updates["is_live"] = True
        except Exception as exc:
            status = "error"
            error_msg = str(exc)

        if updates:
            playout.update_source(source_id, updates)
        playout.set_source_validation(source_id, status, error_msg)
        return jsonify({"ok": True, "status": status, "error": error_msg, "updates": updates})

    @app.route("/api/football/today", methods=["GET"])
    def api_football_today():
        try:
            matches = get_today_matches()
            return jsonify({"ok": True, "matches": matches})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @app.route("/api/football/add_today", methods=["POST"])
    def api_football_add_today():
        try:
            matches = get_today_matches()
            existing_cal = {(e["date"], e["title"]) for e in playout.get_calendar()}
            added = 0
            for m in matches:
                source_name = f"Fútbol: {m['title']}"
                existing = playout.find_source_by_name(source_name)
                if existing:
                    src_id = existing["id"]
                else:
                    src_id = playout.add_source({
                        "name": source_name,
                        "type": "football",
                        "url": "",
                        "auto_enabled": False,
                    })
                if not src_id:
                    continue
                if (m["date"], m["title"]) in existing_cal:
                    continue
                existing_cal.add((m["date"], m["title"]))
                playout.add_calendar_entry({
                    "date": m["date"],
                    "time": m["time"],
                    "source_id": src_id,
                    "title": m["title"],
                    "start_mode": "time",
                    "duration_seconds": 5400,
                    "duration_label": "90 min",
                    "is_live": False,
                })
                added += 1
            return jsonify({"ok": True, "added": added, "total": len(matches)})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    def daily_check_iptv(playout_engine, providers: list[dict], on_done=None) -> int:
        provider_map = {p["id"]: p for p in providers}
        state = playout_engine.get_state()
        local_now_iso = now_iso()
        checked = 0
        for src in state.get("sources", []):
            if src.get("type") != "iptv":
                continue
            sid = src.get("iptv_stream_id")
            if not sid:
                continue
            provider = provider_map.get(src.get("iptv_provider", ""))
            if not provider:
                continue
            url = xtream_live_url(provider, sid)
            status = check_url(url, timeout=4)
            updates = {
                "last_checked_at": local_now_iso,
                "last_check_status": status if status == "ok" else "offline",
                "last_check_error": None if status == "ok" else status,
            }
            if status == "ok" and src.get("url") != url:
                updates["url"] = url
                updates["last_check_status"] = "changed_url"
            playout_engine.update_source(src["id"], updates)
            checked += 1
        if on_done:
            on_done(checked)
        return checked

    @app.route("/api/iptv/import", methods=["POST"])
    def api_iptv_import():
        from src.iptv_importer import import_provider_channels
        provider_id = request.form.get("provider_id", "").strip() or None
        results = []
        total_imported = 0
        total_updated = 0
        total_errors = 0
        for provider in get_providers():
            if provider_id and provider["id"] != provider_id:
                continue
            try:
                summary = import_provider_channels(provider, playout)
                total_imported += summary["imported"]
                total_updated += summary["updated"]
                results.append({
                    "provider_id": provider["id"],
                    "category_id": "*",
                    "category_label": "Todos los grupos",
                    "total": summary["total"],
                    "imported": summary["imported"],
                    "updated": summary["updated"],
                    "skipped": summary["skipped"],
                    "category_count": summary["category_count"],
                    "error": None,
                })
            except Exception as exc:
                total_errors += 1
                results.append({
                    "provider_id": provider["id"],
                    "category_id": "*",
                    "category_label": "Todos los grupos",
                    "total": 0,
                    "imported": 0,
                    "updated": 0,
                    "skipped": 0,
                    "error": str(exc),
                })
        return jsonify({
            "ok": True,
            "results": results,
            "total_imported": total_imported,
            "total_updated": total_updated,
            "total_errors": total_errors,
        })

    @app.route("/api/iptv/providers/add", methods=["POST"])
    def api_iptv_add_provider():
        name = request.form.get("name", "").strip()
        dns = request.form.get("dns", "").strip()
        dns_alt = request.form.get("dns_alt", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        provider_id = request.form.get("provider_id", "").strip()

        if not name:
            return jsonify({"ok": False, "error": "Falta el nombre del proveedor"})
        if not dns:
            return jsonify({"ok": False, "error": "Falta el DNS del panel Xtream"})
        if not username or not password:
            return jsonify({"ok": False, "error": "Faltan usuario o password"})
        if not dns.startswith("http://") and not dns.startswith("https://"):
            return jsonify({"ok": False, "error": "El DNS debe empezar por http:// o https://"})

        pid = slugify_provider_id(provider_id or name)
        categories = {
            "86": "Entretenimiento",
            "87": "Documentales",
            "88": "General",
            "89": "Cine",
            "90": "Deportes",
            "600": "Infantil",
            "601": "Musica",
            "410": "24/7",
        }

        provider = {
            "id": pid,
            "name": name,
            "dns": dns,
            "username": username,
            "password": password,
            "categories": categories,
        }
        if dns_alt:
            provider["dns_alt"] = dns_alt

        try:
            add_provider(provider)
            return jsonify({"ok": True, "provider": public_provider(provider)})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @app.route("/api/iptv/providers/delete", methods=["POST"])
    def api_iptv_delete_provider():
        provider_id = request.form.get("provider_id", "").strip()
        if not provider_id:
            return jsonify({"ok": False, "error": "provider_id requerido"})

        ok = delete_provider(provider_id)
        if not ok:
            return jsonify({"ok": False, "error": "Proveedor no encontrado"})

        removed_sources = playout.delete_sources_by_provider(provider_id)
        return jsonify({"ok": True, "removed_sources": removed_sources})

    @app.route("/api/iptv/check", methods=["POST"])
    def api_iptv_check():
        try:
            thread = threading.Thread(
                target=daily_check_iptv, args=(playout, get_providers()),
                daemon=True
            )
            thread.start()
            return jsonify({"ok": True, "message": "Verificacion iniciada en segundo plano"})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    return app

if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
