import hashlib
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin
from xml.sax.saxutils import escape

import requests
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for

from src.playout import PlayoutEngine
from src.youtube_extractor import StreamExtractor
from src.football_api import get_today_matches
from src.iptv_importer import (
    channels_for_category,
    check_url,
    get_providers,
    get_provider,
    xtream_live_url,
    start_daily_scheduler,
)


def no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def proxied_url(url: str, stream_id: int | None = None, kind: str = "segment") -> str:
    sid = f"&sid={stream_id}" if stream_id is not None else ""
    endpoint = {
        "playlist": "/p/playlist.m3u8",
        "media": "/p/media.mp4",
        "segment": "/p/segment.ts",
    }.get(kind, "/p/segment.ts")
    path = f"{endpoint}?url={quote(url, safe='')}{sid}"
    return absolute_url(path)


def rewrite_playlist(content: str, base_url: str, live_window_segments: int = 0, stream_id: int | None = None) -> str:
    if "#EXTINF" in content and live_window_segments > 0:
        return rewrite_media_playlist(content, base_url, live_window_segments, stream_id)

    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue

        absolute_url = urljoin(base_url, stripped)
        lines.append(proxied_url(absolute_url, stream_id, "playlist"))

    return "\n".join(lines) + "\n"


def rewrite_media_playlist(content: str, base_url: str, live_window_segments: int, stream_id: int | None) -> str:
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
        current_block.append(proxied_url(absolute_url, stream_id, "segment"))
        blocks.append(current_block)
        current_block = []

    if not blocks:
        return rewrite_playlist(content, base_url, live_window_segments=0)

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
    segment_cache = SegmentCache(
        cache_dir=os.environ.get("CACHE_DIR", "/tmp/youtube-hls-cache"),
        ttl_seconds=int(os.environ.get("CACHE_TTL_SECONDS", "1800")),
        max_mb=int(os.environ.get("CACHE_MAX_MB", "512")),
        max_object_mb=int(os.environ.get("CACHE_MAX_OBJECT_MB", "32")),
    )
    live_window_segments = int(os.environ.get("LIVE_WINDOW_SEGMENTS", "30"))
    presentation_loop_count = int(os.environ.get("PRESENTATION_LOOP_COUNT", "1000"))
    app_version = os.environ.get("APP_VERSION", "dev")
    auto_presentation_on_end = os.environ.get("AUTO_PRESENTATION_ON_END", "1") == "1"
    stream_state = {
        "mode": "youtube" if upstream_hls_url else None,
        "source_url": None,
        "source_url_name": None,
        "upstream_hls_url": upstream_hls_url,
        "stream_id": 1 if upstream_hls_url else 0,
        "error": None,
        "presentation_proc": None,
        "iptv_channels": [],
        "iptv_url": None,
        "current_source_id": None,
        "current_reason": None,
        "current_calendar_id": None,
    }

    def stop_presentation_stream() -> None:
        proc = stream_state.get("presentation_proc")
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        stream_state["presentation_proc"] = None

    def start_presentation_stream() -> None:
        stop_presentation_stream()
        presentation_hls_dir.mkdir(parents=True, exist_ok=True)
        for item in presentation_hls_dir.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)

        output_playlist = presentation_hls_dir / "live.m3u8"
        segment_pattern = presentation_hls_dir / "seg_%06d.ts"
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
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
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
            "8",
            "-hls_flags",
            "delete_segments+append_list+omit_endlist+independent_segments",
            "-hls_segment_filename",
            str(segment_pattern),
            str(output_playlist),
        ]
        stream_state["presentation_proc"] = subprocess.Popen(cmd)

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

    def refresh_stream_url_if_needed() -> bool:
        source_url = stream_state.get("source_url")
        if not source_url:
            return False

        if ".m3u8" in source_url:
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

    def activate_source(source: dict, reason: str, calendar_id: str | None = None) -> bool:
        source_id = source["id"]
        if source_id == stream_state.get("current_source_id") and stream_state.get("upstream_hls_url"):
            if reason == stream_state.get("current_reason"):
                return True

        if source["type"] == "presentation":
            if not presentation_video.exists():
                playout.add_history_entry({
                    "timestamp": datetime.now().isoformat(),
                    "source_id": source_id,
                    "source_name": source.get("name", "Video presentacion"),
                    "source_type": "presentation",
                    "reason": reason,
                    "calendar_id": calendar_id,
                    "success": False,
                    "error": "No se encuentra sofa.mp4",
                })
                return False
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
                    "timestamp": datetime.now().isoformat(),
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
                    "timestamp": datetime.now().isoformat(),
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
            stream_state["mode"] = None
            playout.add_history_entry({
                "timestamp": datetime.now().isoformat(),
                "source_id": source_id,
                "source_name": source.get("name", ""),
                "source_type": "football",
                "reason": reason,
                "calendar_id": calendar_id,
                "success": False,
                "error": "Evento informativo, sin stream",
            })
            return False

        stop_presentation_stream()
        segment_cache.clear()

        url = source["url"]
        source_type = source.get("type", "youtube")
        error_msg = None

        if source_type == "hls" or ".m3u8" in url:
            hls_url = url
        else:
            try:
                hls_url = StreamExtractor(url).get_hls_url()
            except Exception as exc:
                error_msg = f"No se pudo activar fuente: {exc}"
                stream_state["error"] = error_msg
                playout.add_history_entry({
                    "timestamp": datetime.now().isoformat(),
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

        playout.add_history_entry({
            "timestamp": datetime.now().isoformat(),
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

    def _daily_iptv_check():
        return daily_check_iptv(playout, get_providers())

    start_daily_scheduler(_daily_iptv_check)

    @app.route("/")
    def index():
        today = datetime.now().strftime("%Y-%m-%d")
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
        sources = sorted(state["sources"], key=lambda s: s["name"].lower())
        auto_enabled = state["auto_enabled"]

        source_map = {s["id"]: s["name"] for s in sources}

        response = no_store(Response(render_template(
            "player.html",
            app_version=app_version,
            playlist_url="/live.m3u8" if stream_state["mode"] else None,
            playback_url=(
                absolute_url(f"/presentation.mp4?sid={stream_state['stream_id']}")
                if stream_state["mode"] == "presentation"
                else
                proxied_url(stream_state["upstream_hls_url"], stream_state["stream_id"], "media")
                if stream_state["upstream_hls_url"] and not StreamExtractor.is_hls_url(stream_state["upstream_hls_url"])
                else "/live.m3u8"
            ),
            is_hls=(
                bool(stream_state["upstream_hls_url"])
                and StreamExtractor.is_hls_url(stream_state["upstream_hls_url"])
            ),
            mode=stream_state["mode"],
            source_url=stream_state["source_url"] or "",
            source_url_name=stream_state.get("source_url_name"),
            error=stream_state["error"],
            connected=bool(stream_state["mode"]),
            sources=sources,
            calendar_today=calendar_today,
            calendar_date=today,
            next_entry=next_entry,
            source_map=source_map,
            auto_enabled=auto_enabled,
            current_source_id=stream_state.get("current_source_id"),
            current_reason=stream_state.get("current_reason"),
            current_calendar_id=stream_state.get("current_calendar_id"),
            overlap_ids=overlap_ids,
        )))
        response.mimetype = "text/html"
        return response

    @app.route("/sources")
    def sources_page():
        state = playout.get_state()
        iptv_providers = get_providers()
        response = no_store(Response(render_template(
            "sources.html",
            app_version=app_version,
            sources=sorted(state["sources"], key=lambda s: s["name"].lower()),
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

        stop_presentation_stream()
        segment_cache.clear()
        stream_state["stream_id"] += 1
        stream_state["mode"] = "iptv"
        stream_state["source_url"] = channel_url
        stream_state["source_url_name"] = channel_name
        stream_state["upstream_hls_url"] = hls_url
        stream_state["error"] = None
        return redirect(url_for("index"))

    @app.route("/presentation", methods=["POST"])
    def enable_presentation():
        if stream_state["mode"]:
            stream_state["error"] = "Desconecta la emision actual antes de activar el video presentacion."
            return redirect(url_for("index"))

        if not presentation_video.exists():
            stream_state["error"] = "No se encontro el video de presentacion (sofa.mp4)."
            return redirect(url_for("index"))

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
        stop_presentation_stream()
        segment_cache.clear()
        stream_state["stream_id"] += 1
        stream_state["mode"] = None
        stream_state["source_url"] = None
        stream_state["source_url_name"] = None
        stream_state["upstream_hls_url"] = None
        stream_state["error"] = None
        stream_state["current_source_id"] = None
        stream_state["current_reason"] = None
        stream_state["current_calendar_id"] = None
        return redirect(url_for("index"))

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
            return no_store(Response("La presentacion continua aun no esta lista.\n", status=503))

        raw = playlist_path.read_text(encoding="utf-8", errors="ignore")
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(absolute_url(f"/presentation/live/{stripped}"))
            else:
                lines.append(line)
        playlist = "\n".join(lines) + "\n"
        return no_store(Response(playlist, mimetype="application/vnd.apple.mpegurl"))

    @app.route("/presentation/live/<path:filename>")
    def presentation_live_file(filename: str):
        if stream_state["mode"] != "presentation":
            return no_store(Response("No hay video de presentacion activo.\n", status=404))
        return no_store(send_from_directory(presentation_hls_dir, filename, conditional=True))

    @app.route("/health")
    def health():
        return no_store(Response("ok\n", mimetype="text/plain"))

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

    @app.route("/emby/live.m3u8")
    def emby_live_playlist():
        return live_playlist()

    @app.route("/emby/live-max.m3u8")
    def emby_live_max_playlist():
        return live_max_playlist()

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
            return no_store(Response("No hay ningun directo conectado.\n", status=404))

        if not StreamExtractor.is_hls_url(upstream_hls_url):
            return no_store(Response(
                direct_media_playlist(absolute_url("/emby/direct.mp4")),
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
            return Response("No hay ningun directo conectado.\n", status=404)

        if not StreamExtractor.is_hls_url(upstream_hls_url):
            return no_store(Response(
                direct_media_playlist(absolute_url("/current/media.mp4")),
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
            rewrite_playlist(response.text, upstream_hls_url, live_window_segments, stream_state["stream_id"]),
            mimetype="application/vnd.apple.mpegurl",
        ))

    @app.route("/live-max.m3u8")
    def live_max_playlist():
        if stream_state["mode"] == "presentation":
            return presentation_m3u8()

        upstream_hls_url = stream_state["upstream_hls_url"]
        if not upstream_hls_url:
            return Response("No hay ningun directo conectado.\n", status=404)

        if not StreamExtractor.is_hls_url(upstream_hls_url):
            return no_store(Response(
                direct_media_playlist(absolute_url("/current/media.mp4")),
                mimetype="application/vnd.apple.mpegurl",
            ))

        try:
            response = requests.get(upstream_hls_url, timeout=15)
            response.raise_for_status()
            variant_url = best_variant_url(response.text, upstream_hls_url)
            if not variant_url:
                return no_store(Response(
                    rewrite_playlist(response.text, upstream_hls_url, live_window_segments, stream_state["stream_id"]),
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
                        rewrite_playlist(response.text, refreshed_url, live_window_segments, stream_state["stream_id"]),
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
            rewrite_playlist(variant_response.text, variant_url, live_window_segments, stream_state["stream_id"]),
            mimetype="application/vnd.apple.mpegurl",
        ))

    @app.route("/current/media.mp4")
    def current_media_mp4():
        return current_media_response("video/mp4")

    def proxy_response(default_mimetype: str = "application/octet-stream"):
        target_url = request.args.get("url")
        if not target_url:
            return no_store(Response("Missing url parameter.\n", status=400))

        request_sid = request.args.get("sid", type=int)
        if request_sid is not None and request_sid != stream_state["stream_id"]:
            return no_store(Response("El canal cambio; descarta esta playlist antigua.\n", status=410))

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
            playlist = rewrite_playlist(upstream.text, target_url, live_window_segments, stream_state["stream_id"])
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
        return jsonify(playout.get_sources())

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

    @app.route("/api/calendar", methods=["POST"])
    def api_calendar():
        date = request.form.get("date", datetime.now().strftime("%Y-%m-%d"))
        entries = playout.get_calendar(date)
        state = playout.get_state()
        source_map = {s["id"]: s["name"] for s in state["sources"]}
        return jsonify({"entries": entries, "source_map": source_map})

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
        date = request.form.get("date", datetime.now().strftime("%Y-%m-%d"))
        entries = playout.get_calendar(date)
        last = entries[-1] if entries else None
        return jsonify({"last": last})

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
        stop_presentation_stream()
        segment_cache.clear()
        stream_state["stream_id"] += 1
        stream_state["mode"] = None
        stream_state["source_url"] = None
        stream_state["source_url_name"] = None
        stream_state["upstream_hls_url"] = None
        stream_state["error"] = None
        stream_state["current_source_id"] = None
        stream_state["current_reason"] = None
        stream_state["current_calendar_id"] = None
        return jsonify({"ok": True})

    @app.route("/api/auto/next", methods=["POST"])
    def api_auto_next():
        stop_presentation_stream()
        segment_cache.clear()
        stream_state["stream_id"] += 1
        stream_state["mode"] = None
        stream_state["source_url"] = None
        stream_state["source_url_name"] = None
        stream_state["upstream_hls_url"] = None
        stream_state["error"] = None
        stream_state["current_source_id"] = None
        stream_state["current_reason"] = None
        stream_state["current_calendar_id"] = None

        ap = playout.find_next_after_previous()
        if ap:
            source, cal_id = ap
            playout.set_calendar_played(cal_id)
            ok = activate_source(source, "after_previous", cal_id)
            return jsonify({"ok": ok, "source_name": source.get("name")})

        if not playout.is_auto_enabled():
            return jsonify({"ok": False, "error": "Auto no activo"})

        source = playout.get_next_auto_source()
        if not source:
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
        now_iso = datetime.now().isoformat()
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
                "last_checked_at": now_iso,
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
        provider_id = request.form.get("provider_id", "").strip() or None
        results = []
        total_imported = 0
        total_errors = 0
        for provider in get_providers():
            if provider_id and provider["id"] != provider_id:
                continue
            cats = provider.get("categories", {})
            for cid, group_label in sorted(cats.items(), key=lambda x: int(x[0])):
                try:
                    channels = channels_for_category(provider, cid)
                    imported = 0
                    skipped = 0
                    for ch in channels:
                        existing = playout.find_source_by_name(ch["name"])
                        if existing:
                            skipped += 1
                            continue
                        playout.add_source(ch)
                        imported += 1
                    total_imported += imported
                    results.append({
                        "category_id": cid,
                        "category_label": group_label,
                        "total": len(channels),
                        "imported": imported,
                        "skipped": skipped,
                        "error": None,
                    })
                except Exception as exc:
                    total_errors += 1
                    results.append({
                        "category_id": cid,
                        "category_label": group_label,
                        "total": 0,
                        "imported": 0,
                        "skipped": 0,
                        "error": str(exc),
                    })
        return jsonify({"ok": True, "results": results, "total_imported": total_imported, "total_errors": total_errors})

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
