import hashlib
import hmac
import ipaddress
import logging
import math
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
from xml.sax.saxutils import escape

import requests
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for

log = logging.getLogger("youtube_hls.web")

from src.playout import PlayoutEngine
from src.youtube_extractor import StreamExtractor


def is_direct_stream_url(url: str) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return any(lowered.endswith(ext) for ext in (".m3u8", ".ts", ".mp4", ".mkv", ".mov", ".m4v"))


nvenc_available: bool | None = None
video_accel = "cpu"
processed_video_encoder = "libx264"
presentation_video_encoder = "libx264"


def _hls_url_for_source(url: str, source_type: str) -> str | None:
    if source_type in ("iptv", "hls") or is_direct_stream_url(url):
        return url
    try:
        return StreamExtractor(url).get_hls_url()
    except Exception:
        return None


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
    log.info(f"URL rotated for {source['name']}: refreshing from {source.get('url', '')[:60]} -> {current_url[:60]}")
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
        "-profile:v", "high",
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
    hls_path = Path(hls_dir).resolve()
    presentation_video = Path(__file__).resolve().parent.parent / "sofa.mp4"
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
    processed_segment_seconds = int(os.environ.get("PROCESSED_SEGMENT_SECONDS", "4"))
    processed_list_size = int(os.environ.get("PROCESSED_LIST_SIZE", "45"))
    processed_extra_segments = int(os.environ.get("PROCESSED_EXTRA_SEGMENTS", "8"))
    processed_video_bitrate = os.environ.get("PROCESSED_VIDEO_BITRATE", "6000k")
    processed_video_maxrate = os.environ.get("PROCESSED_VIDEO_MAXRATE", "7000k")
    processed_video_bufsize = os.environ.get("PROCESSED_VIDEO_BUFSIZE", "12000k")
    processed_audio_bitrate = os.environ.get("PROCESSED_AUDIO_BITRATE", "160k")
    processed_preset = os.environ.get("PROCESSED_PRESET", "veryfast")
    processed_height = int(os.environ.get("PROCESSED_HEIGHT", "1080"))
    processed_startup_wait_seconds = int(os.environ.get("PROCESSED_STARTUP_WAIT_SECONDS", "30"))
    proxy_signing_key = os.environ.get("PROXY_SIGNING_KEY") or secrets.token_urlsafe(32)
    allow_unsigned_proxy = os.environ.get("ALLOW_UNSIGNED_PROXY", "0") == "1"
    epg_store.init_db()
    processed_delay_segments = max(1, math.ceil(processed_delay_seconds / max(1, processed_segment_seconds)))
    processed_effective_list_size = max(processed_list_size, processed_delay_segments + processed_extra_segments)
    processed_delete_threshold = max(4, processed_extra_segments)
    global video_accel, processed_video_encoder, presentation_video_encoder, nvenc_available
    video_accel = os.environ.get("VIDEO_ACCEL", "cpu")
    processed_video_encoder = os.environ.get("PROCESSED_VIDEO_ENCODER", "libx264")
    presentation_video_encoder = os.environ.get("PRESENTATION_VIDEO_ENCODER", "libx264")
    nvenc_available = None
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
        "program_upstream_url": None,
        "processed_error": None,
        "iptv_channels": [],
        "iptv_url": None,
        "current_source_id": None,
        "current_reason": None,
        "current_calendar_id": None,
        "logs": {"preview": [], "processed": [], "presentation": [], "program": []},
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
            return True
        try:
            use_encoder = _resolve_encoder(processed_video_encoder)
            if use_encoder == "h264_nvenc":
                enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "cbr", "-b:v", "2000k", "-maxrate", "2500k", "-bufsize", "5000k", "-profile:v", "main", "-pix_fmt", "yuv420p"]
            else:
                enc = ["-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "main", "-pix_fmt", "yuv420p", "-b:v", "2000k", "-maxrate", "2500k", "-bufsize", "5000k"]
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=25:duration=30",
                "-vf", "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='%{localtime\:%H\\\:%M\\\:%S}':fontcolor=white:fontsize=48:x=w-tw-60:y=h-th-60:bordercolor=black:borderw=2",
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            ] + enc + ["-t", "30", str(program_fallback_video)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and program_fallback_video.exists():
                return True
        except Exception as exc:
            print(f"Fallback video generation failed: {exc}")
        return False

    def start_program_stream(input_url: str) -> None:
        stop_program_stream()
        program_hls_dir.mkdir(parents=True, exist_ok=True)
        for item in program_hls_dir.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
        if not program_fallback_video.exists():
            generate_fallback_video()
        use_encoder = _resolve_encoder(processed_video_encoder)
        encoder_args = _build_video_encoder_args(
            use_encoder, processed_video_bitrate, processed_video_maxrate, processed_video_bufsize,
            processed_preset,
        )
        output_playlist = program_hls_dir / "live.m3u8"
        segment_pattern = program_hls_dir / "seg_%06d.ts"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-fflags", "+genpts+discardcorrupt",
            "-err_detect", "ignore_err",
            "-rw_timeout", "15000000",
            "-re",
            "-stream_loop", "-1",
            "-i", input_url,
            "-map", "0:v:0", "-map", "0:a:0?",
        ] + encoder_args + [
            "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", processed_audio_bitrate, "-ar", "48000", "-ac", "2",
            "-f", "hls",
            "-hls_time", str(processed_segment_seconds),
            "-hls_list_size", str(processed_effective_list_size),
            "-hls_delete_threshold", str(processed_delete_threshold),
            "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments+program_date_time",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(segment_pattern),
            str(output_playlist),
        ]
        try:
            stream_state["program_proc"] = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        except Exception as exc:
            stream_state["program_error"] = f"No se pudo iniciar program stream: {exc}"
            return
        stream_state["program_stream_id"] = stream_state["stream_id"]
        stream_state["program_upstream_url"] = input_url
        stream_state["program_error"] = None
        stream_state["program_fallback"] = False
        threading.Thread(target=_drain_stderr, args=(stream_state["program_proc"], "program"), daemon=True).start()

    def start_program_fallback() -> None:
        if stream_state.get("program_fallback") and stream_state.get("program_proc") and stream_state["program_proc"].poll() is None:
            return
        stop_program_stream()
        program_hls_dir.mkdir(parents=True, exist_ok=True)
        for item in program_hls_dir.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
        if not program_fallback_video.exists():
            generate_fallback_video()
        if not program_fallback_video.exists():
            stream_state["program_error"] = "No se pudo generar fallback"
            return
        use_encoder = _resolve_encoder(processed_video_encoder)
        encoder_args = _build_video_encoder_args(
            use_encoder, processed_video_bitrate, processed_video_maxrate, processed_video_bufsize,
            processed_preset,
        )
        output_playlist = program_hls_dir / "live.m3u8"
        segment_pattern = program_hls_dir / "seg_%06d.ts"
        cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-re",
            "-stream_loop", "-1",
            "-i", str(program_fallback_video),
            "-map", "0:v:0", "-map", "0:a:0?",
        ] + encoder_args + [
            "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", processed_audio_bitrate, "-ar", "48000", "-ac", "2",
            "-f", "hls",
            "-hls_time", str(processed_segment_seconds),
            "-hls_list_size", str(processed_effective_list_size),
            "-hls_delete_threshold", str(processed_delete_threshold),
            "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments+program_date_time",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(segment_pattern),
            str(output_playlist),
        ]
        try:
            stream_state["program_proc"] = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        except Exception as exc:
            stream_state["program_error"] = f"No se pudo iniciar fallback: {exc}"
            return
        stream_state["program_stream_id"] = stream_state["stream_id"]
        stream_state["program_error"] = None
        stream_state["program_fallback"] = True
        stream_state["program_upstream_url"] = str(program_fallback_video)
        threading.Thread(target=_drain_stderr, args=(stream_state["program_proc"], "program"), daemon=True).start()

    def stop_program_stream() -> None:
        kill_process(stream_state.get("program_proc"))
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
        source_url = stream_state.get("source_url")
        if not source_url:
            return False

        current_mode = stream_state.get("mode", "")
        if current_mode in ("iptv", "hls") or is_direct_stream_url(source_url):
            stream_state["upstream_hls_url"] = source_url
            stream_state["stream_id"] += 1
            stream_state["error"] = None
            segment_cache.clear()
            return True

        try:
            stream_state["upstream_hls_url"] = StreamExtractor(source_url).get_hls_url()
            stream_state["stream_id"] += 1
            stream_state["error"] = None
            segment_cache.clear()
            return True
        except Exception as exc:
            stream_state["error"] = f"No se pudo refrescar la URL del stream: {exc}"
            stream_state["upstream_hls_url"] = None
            return False

    def emby_stream_url(max_quality: bool = False) -> str:
        return absolute_url("/emby/live-max.m3u8" if max_quality else "/emby/live.m3u8")

    def current_media_response(default_mimetype: str = "video/mp4"):
        upstream_hls_url = stream_state["upstream_hls_url"]
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
            if reason == stream_state.get("current_reason"):
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
        segment_cache.clear()

        _check_and_refresh_source_url(source)
        url = source["url"]
        source_type = source.get("type", "youtube")
        hls_url = _hls_url_for_source(url, source_type)
        if not hls_url:
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
        stream_state["mode"] = source_type if source_type in ("youtube", "iptv", "hls") else "youtube"
        stream_state["source_url"] = url
        stream_state["source_url_name"] = source.get("name", url)
        stream_state["upstream_hls_url"] = hls_url
        stream_state["error"] = None
        stream_state["current_source_id"] = source_id
        stream_state["current_reason"] = reason
        stream_state["current_calendar_id"] = calendar_id

        if hls_url and not StreamExtractor.is_hls_url(hls_url):
            try:
                start_preview_stream(hls_url)
            except Exception:
                pass
        if hls_url:
            try:
                start_program_stream(hls_url)
            except Exception as exc:
                stream_state["program_error"] = f"No se pudo iniciar program stream: {exc}"

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

    playout.set_callbacks({
        "activate_source": activate_source,
        "get_stream_state": get_stream_state,
    })
    playout.start()

    # Start program stream at startup if we have a saved upstream URL
    _initial_upstream = stream_state.get("upstream_hls_url")
    if _initial_upstream:
        try:
            start_program_stream(_initial_upstream)
        except Exception:
            pass
    else:
        # No saved source - start fallback so Emby always has a stream
        try:
            start_program_fallback()
        except Exception:
            pass

    def _daily_iptv_check():
        return daily_check_iptv(playout, get_providers())

    start_daily_scheduler(_daily_iptv_check)

    # Reap any ffmpeg process left over from a previous container
    # instance and clear stale HLS temp dirs.
    try:
        cleanup_orphans([preview_hls_dir, processed_hls_dir, presentation_hls_dir, program_hls_dir])
    except Exception:
        log.exception("ffmpeg orphan cleanup failed")

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
        lambda: start_program_fallback() if stream_state.get("program_fallback") else start_program_stream(stream_state.get("program_upstream_url") or str(program_fallback_video)),
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

        upstream_url = stream_state.get("upstream_hls_url")
        upstream_is_hls = bool(upstream_url and StreamExtractor.is_hls_url(upstream_url))
        upstream_is_direct = bool(upstream_url and not upstream_is_hls)
        emby_preview_url = "/emby/processed/live.m3u8" if processed_enabled else "/emby/live.m3u8"
        current_mode = stream_state["mode"]
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
                start_program_stream(hls_url)
            except Exception as exc:
                stream_state["program_error"] = f"No se pudo iniciar program stream: {exc}"
        return redirect(url_for("index"))

    @app.route("/presentation", methods=["POST"])
    def enable_presentation():
        if stream_state["mode"]:
            stream_state["error"] = "Desconecta la emision actual antes de activar el video presentacion."
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

        playlist_path = preview_hls_dir / "live.m3u8"
        if stream_state.get("preview_stream_id") != stream_state.get("stream_id") or stream_state.get("preview_proc") is None:
            try:
                start_preview_stream(upstream_hls_url)
            except Exception as exc:
                return no_store(Response(f"No se pudo iniciar preview HLS: {exc}\n", status=502))

        for _ in range(20):
            if playlist_path.exists() and playlist_path.stat().st_size > 0:
                break
            time.sleep(0.1)

        if not playlist_path.exists():
            return no_store(Response("Preview HLS aun iniciando.\n", status=503))

        raw = playlist_path.read_text(encoding="utf-8", errors="ignore")
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(f"/preview/live/{stripped}")
            else:
                lines.append(line)
        playlist = "\n".join(lines) + "\n"
        return no_store(Response(playlist, mimetype="application/vnd.apple.mpegurl"))

    @app.route("/preview/live/<path:filename>")
    def preview_live_file(filename: str):
        if filename.endswith(".ts"):
            return no_store(send_from_directory(preview_hls_dir, filename, mimetype="video/mp2t", conditional=True))
        if filename.endswith(".m3u8"):
            return no_store(send_from_directory(preview_hls_dir, filename, mimetype="application/vnd.apple.mpegurl", conditional=True))
        return no_store(send_from_directory(preview_hls_dir, filename, conditional=True))

    @app.route("/processed/live.m3u8")
    def processed_live_playlist():
        if not processed_enabled:
            return no_store(Response("Salida procesada desactivada.\n", status=404))

        if stream_state.get("mode") == "presentation":
            return presentation_m3u8()

        upstream_hls_url = stream_state.get("upstream_hls_url")
        if not upstream_hls_url:
            return no_store(Response("No hay ningun directo conectado.\n", status=404))

        playlist_path = program_hls_dir / "live.m3u8"
        if stream_state.get("program_proc") is None or stream_state.get("program_proc").poll() is not None:
            if upstream_hls_url:
                try:
                    start_program_stream(upstream_hls_url)
                except Exception as exc:
                    stream_state["program_error"] = str(exc)
                    return no_store(Response("No se pudo iniciar program stream: " + str(exc) + "\n", status=502))
            elif program_fallback_video.exists():
                try:
                    start_program_fallback()
                except Exception as exc:
                    stream_state["program_error"] = str(exc)
                    return no_store(Response("No se pudo iniciar fallback: " + str(exc) + "\n", status=502))

        wait_steps = max(1, int(processed_startup_wait_seconds / 0.2))
        for _ in range(wait_steps):
            if playlist_path.exists() and playlist_path.stat().st_size > 0:
                break
            time.sleep(0.2)

        if not playlist_path.exists():
            return no_store(Response("Procesado HLS aun iniciando.\n", status=503))

        raw = playlist_path.read_text(encoding="utf-8", errors="ignore")
        lines = raw.splitlines()

        header_lines = []
        segment_blocks: list[list[str]] = []
        current_block: list[str] = []
        pending_date_time: str | None = None
        pending_extinf: str | None = None

        for ln in lines:
            s = ln.strip()
            if s.startswith("#EXT-X-PROGRAM-DATE-TIME"):
                pending_date_time = ln
            elif s.startswith("#EXTINF:"):
                pending_extinf = ln
            elif s and not s.startswith("#"):
                block = []
                if pending_date_time:
                    block.append(pending_date_time)
                if pending_extinf:
                    block.append(pending_extinf)
                block.append(f"/program/live/{s}")
                segment_blocks.append(block)
                pending_date_time = None
                pending_extinf = None
            elif s.startswith("#EXTM3U") or s.startswith("#EXT-X-VERSION") or s.startswith("#EXT-X-TARGETDURATION") or s.startswith("#EXT-X-MEDIA-SEQUENCE") or s.startswith("#EXT-X-INDEPENDENT-SEGMENTS") or s.startswith("#EXT-X-DISCONTINUITY"):
                header_lines.append(ln)
            elif current_block or pending_extinf or pending_date_time:
                pass
            else:
                header_lines.append(ln)

        if not segment_blocks:
            return no_store(Response("Procesado HLS aun iniciando.\n", status=503))

        total_dur = sum(
            float(b[1].split(":", 1)[1].split(",", 1)[0]) if len(b) > 1 else processed_segment_seconds
            for b in segment_blocks
        )

        cut = len(segment_blocks)
        delayed = 0.0
        while cut > 0 and delayed < float(processed_delay_seconds):
            cut -= 1
            b = segment_blocks[cut]
            for ln in b:
                if ln.startswith("#EXTINF:"):
                    try:
                        delayed += float(ln.split(":", 1)[1].split(",", 1)[0])
                    except Exception:
                        delayed += float(processed_segment_seconds)
                    break

        if cut <= 0:
            return no_store(Response("Procesado acumulando buffer de diferido.\n", status=503))

        out = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:4",
               "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-INDEPENDENT-SEGMENTS"]
        for b in segment_blocks[:cut]:
            out.extend(b)

        playlist = "\n".join(out) + "\n"
        return no_store(Response(playlist, mimetype="application/vnd.apple.mpegurl"))

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
        })

    @app.route("/api/logs")
    def api_logs():
        preview_buf = stream_state.get("logs", {}).get("preview", [])
        processed_buf = stream_state.get("logs", {}).get("processed", [])
        presentation_buf = stream_state.get("logs", {}).get("presentation", [])
        preview_alive = stream_state.get("preview_proc") is not None and stream_state.get("preview_proc").poll() is None
        processed_alive = stream_state.get("processed_proc") is not None and stream_state.get("processed_proc").poll() is None
        presentation_alive = stream_state.get("presentation_proc") is not None and stream_state.get("presentation_proc").poll() is None
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
            "source_name": stream_state.get("source_url_name"),
        })

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
        return live_playlist()

    @app.route("/emby/live-max.m3u8")
    def emby_live_max_playlist():
        return live_max_playlist()

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
            return no_store(Response(
                direct_media_playlist("/emby/direct.mp4"),
                mimetype="application/vnd.apple.mpegurl",
            ))

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
        if stream_state["mode"] == "presentation":
            return presentation_m3u8()

        upstream_hls_url = stream_state["upstream_hls_url"]
        if not upstream_hls_url:
            if force_presentation("sin directo"):
                return presentation_m3u8()
            return Response("No hay ningun directo conectado.\n", status=404)

        if not StreamExtractor.is_hls_url(upstream_hls_url):
            return no_store(Response(
                direct_media_playlist("/current/stream.ts"),
                mimetype="application/vnd.apple.mpegurl",
            ))

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
        if stream_state["mode"] == "presentation":
            return presentation_m3u8()

        upstream_hls_url = stream_state["upstream_hls_url"]
        if not upstream_hls_url:
            if force_presentation("sin directo"):
                return presentation_m3u8()
            return Response("No hay ningun directo conectado.\n", status=404)

        if not StreamExtractor.is_hls_url(upstream_hls_url):
            return no_store(Response(
                direct_media_playlist("/current/stream.ts"),
                mimetype="application/vnd.apple.mpegurl",
            ))

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

    @app.route("/hls/<path:filename>")
    def hls_file(filename: str):
        return send_from_directory(hls_path, filename)

    # ----- API endpoints for TV playout -----

    @app.route("/api/sources", methods=["GET"])
    def api_sources():
        items = [public_source(s) for s in playout.get_sources()]
        for item in items:
            item.setdefault("group", _source_group_for(item))
        return jsonify(items)

    @app.route("/api/sources/add_youtube", methods=["POST"])
    def api_add_youtube():
        name = request.form.get("name", "").strip() or "YouTube"
        url = request.form.get("url", "").strip()
        if not url:
            return jsonify({"ok": False, "error": "URL requerida"})
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
        meta = probe_hls(url)
        src_id = playout.add_source({
            "name": name,
            "type": "hls",
            "url": url,
            **meta,
        })
        return jsonify({"ok": True, "source_id": src_id})

    @app.route("/api/load_iptv", methods=["POST"])
    def api_load_iptv():
        iptv_url = request.form.get("iptv_url", "").strip()
        if not iptv_url:
            return jsonify({"ok": False, "error": "URL requerida", "channels": []})
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
        ok = playout.delete_source(source_id)
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
        cal_id = playout.insert_calendar_entry({
            "date": date,
            "source_id": source_id,
            "title": title,
        }, insert_before)
        if cal_id:
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
        if start_mode != "after_previous" and not time_val:
            return jsonify({"ok": False, "error": "time requerido para programas con hora"})
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
        ok = force_presentation("stop manual")
        return jsonify({"ok": ok})

    @app.route("/api/auto/next", methods=["POST"])
    def api_auto_next():
        stop_preview_stream()
        stop_presentation_stream()
        segment_cache.clear()

        ap = playout.find_next_after_previous()
        if ap:
            source, cal_id = ap
            playout.set_calendar_played(cal_id)
            ok = activate_source(source, "after_previous", cal_id)
            return jsonify({"ok": ok, "source_name": source.get("name")})

        if not playout.is_auto_enabled():
            force_presentation("auto desactivado")
            return jsonify({"ok": False, "error": "Auto no activo"})

        source = playout.get_next_auto_source()
        if not source:
            force_presentation("sin fuentes auto")
            return jsonify({"ok": False, "error": "No hay fuentes auto disponibles"})

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
        url = source["url"]
        status = "ok"
        error_msg = None

        try:
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
            updates["url"] = url.strip()
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
            if source["type"] == "youtube":
                md = StreamExtractor(source["url"]).get_metadata()
                updates["duration_seconds"] = md["duration_seconds"]
                updates["duration_label"] = md["duration_label"]
                updates["is_live"] = md["is_live"]
            elif source["type"] in ("hls", "iptv") or ".m3u8" in source["url"]:
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
