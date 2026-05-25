import hashlib
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urljoin
from xml.sax.saxutils import escape

import requests
from flask import Flask, Response, redirect, render_template, request, send_file, send_from_directory, url_for

from src.youtube_extractor import YouTubeExtractor


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


def direct_media_playlist(media_url: str, stream_id: int | None = None) -> str:
    media_proxy_url = proxied_url(media_url, stream_id, "media")
    return "\n".join([
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        "#EXTINF:0,YouTube media",
        media_proxy_url,
        "",
    ])


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
    segment_cache = SegmentCache(
        cache_dir=os.environ.get("CACHE_DIR", "/tmp/youtube-hls-cache"),
        ttl_seconds=int(os.environ.get("CACHE_TTL_SECONDS", "1800")),
        max_mb=int(os.environ.get("CACHE_MAX_MB", "512")),
        max_object_mb=int(os.environ.get("CACHE_MAX_OBJECT_MB", "32")),
    )
    live_window_segments = int(os.environ.get("LIVE_WINDOW_SEGMENTS", "30"))
    stream_state = {
        "source_url": None,
        "upstream_hls_url": upstream_hls_url,
        "stream_id": 1 if upstream_hls_url else 0,
        "error": None,
    }

    def emby_stream_url(max_quality: bool = False) -> str:
        upstream_hls_url = stream_state["upstream_hls_url"]
        if upstream_hls_url and not YouTubeExtractor.is_hls_url(upstream_hls_url):
            return proxied_url(upstream_hls_url, stream_state["stream_id"], "media")

        return absolute_url("/live-max.m3u8" if max_quality else "/live.m3u8")

    @app.route("/")
    def index():
        return render_template(
            "player.html",
            playlist_url="/live.m3u8" if stream_state["upstream_hls_url"] else None,
            playback_url=(
                f"/proxy?url={quote(stream_state['upstream_hls_url'], safe='')}"
                if stream_state["upstream_hls_url"] and not YouTubeExtractor.is_hls_url(stream_state["upstream_hls_url"])
                else "/live.m3u8"
            ),
            is_hls=(
                bool(stream_state["upstream_hls_url"])
                and YouTubeExtractor.is_hls_url(stream_state["upstream_hls_url"])
            ),
            source_url=stream_state["source_url"] or "",
            error=stream_state["error"],
            connected=bool(stream_state["upstream_hls_url"]),
        )

    @app.route("/connect", methods=["POST"])
    def connect():
        if stream_state["upstream_hls_url"]:
            stream_state["error"] = "Desconecta la emision actual antes de conectar una URL nueva."
            return redirect(url_for("index"))

        source_url = request.form.get("url", "").strip()
        if not source_url:
            stream_state["error"] = "Introduce una URL de YouTube valida."
            return redirect(url_for("index"))

        try:
            hls_url = YouTubeExtractor(source_url).get_hls_url()
        except Exception as exc:
            stream_state["error"] = f"No se pudo conectar con esa URL: {exc}"
            return redirect(url_for("index"))

        segment_cache.clear()
        stream_state["stream_id"] += 1
        stream_state["source_url"] = source_url
        stream_state["upstream_hls_url"] = hls_url
        stream_state["error"] = None
        return redirect(url_for("index"))

    @app.route("/disconnect", methods=["POST"])
    def disconnect():
        segment_cache.clear()
        stream_state["stream_id"] += 1
        stream_state["source_url"] = None
        stream_state["upstream_hls_url"] = None
        stream_state["error"] = None
        return redirect(url_for("index"))

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
        upstream_hls_url = stream_state["upstream_hls_url"]
        if not upstream_hls_url:
            return Response("No hay ningun directo conectado.\n", status=404)

        if not YouTubeExtractor.is_hls_url(upstream_hls_url):
            return no_store(Response(
                direct_media_playlist(upstream_hls_url, stream_state["stream_id"]),
                mimetype="application/vnd.apple.mpegurl",
            ))

        try:
            response = requests.get(upstream_hls_url, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            stream_state["error"] = f"El directo conectado fallo: {exc}"
            stream_state["upstream_hls_url"] = None
            return no_store(Response(stream_state["error"] + "\n", status=502))

        return no_store(Response(
            rewrite_playlist(response.text, upstream_hls_url, live_window_segments, stream_state["stream_id"]),
            mimetype="application/vnd.apple.mpegurl",
        ))

    @app.route("/live-max.m3u8")
    def live_max_playlist():
        upstream_hls_url = stream_state["upstream_hls_url"]
        if not upstream_hls_url:
            return Response("No hay ningun directo conectado.\n", status=404)

        if not YouTubeExtractor.is_hls_url(upstream_hls_url):
            return no_store(Response(
                direct_media_playlist(upstream_hls_url, stream_state["stream_id"]),
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
        except requests.RequestException as exc:
            stream_state["error"] = f"El directo conectado fallo: {exc}"
            stream_state["upstream_hls_url"] = None
            return no_store(Response(stream_state["error"] + "\n", status=502))

        return no_store(Response(
            rewrite_playlist(variant_response.text, variant_url, live_window_segments, stream_state["stream_id"]),
            mimetype="application/vnd.apple.mpegurl",
        ))

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

    return app

if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
