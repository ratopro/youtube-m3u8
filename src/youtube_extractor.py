import logging
from typing import Any
from urllib.parse import quote

import yt_dlp

logger = logging.getLogger("youtube_extractor")


class StreamExtractor:
    def __init__(self, url: str):
        self.url = url

    def _extract_info(self) -> dict[str, Any]:
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extractor_args": {"generic": ["impersonate"]},
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(self.url, download=False)

    def get_hls_url(self) -> str:
        """Return the best stream URL, preferring HLS manifests when available."""
        info = self._extract_info()

        manifest_url = info.get("manifest_url")
        if manifest_url and ".m3u8" in manifest_url:
            return manifest_url

        formats = info.get("formats") or []
        hls_formats = [
            item for item in formats
            if item.get("url") and (
                item.get("protocol") in {"m3u8", "m3u8_native"}
                or ".m3u8" in item.get("url", "")
            )
        ]

        def quality_score(item: dict[str, Any]) -> int:
            return int(item.get("height") or 0) * 10000 + int(item.get("tbr") or 0)

        if not hls_formats:
            direct_formats = [
                item for item in formats
                if item.get("url") and item.get("vcodec") != "none" and item.get("acodec") != "none"
            ]
            if not direct_formats:
                raise RuntimeError("No se encontro un stream reproducible para este enlace.")

            return max(direct_formats, key=quality_score)["url"]

        return max(hls_formats, key=quality_score)["url"]

    @staticmethod
    def is_hls_url(url: str) -> bool:
        return ".m3u8" in url or "manifest/hls" in url

    def get_metadata(self) -> dict[str, Any]:
        info = self._extract_info()
        duration = info.get("duration")
        is_live = info.get("is_live", False) or info.get("live_status") == "is_live"
        title = info.get("title", "Sin titulo")

        duration_label = None
        if duration and not is_live:
            hours = int(duration // 3600)
            minutes = int((duration % 3600) // 60)
            seconds = int(duration % 60)
            duration_label = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours > 0 else f"{minutes:02d}:{seconds:02d}"

        return {
            "title": title,
            "duration_seconds": duration if not is_live else None,
            "duration_label": duration_label if not is_live else "Directo",
            "is_live": is_live,
        }

    def extract_streams(self) -> list[dict[str, Any]]:
        return [{
            "title": "Stream HLS",
            "resolution": "Auto",
            "url": self.get_hls_url(),
            "type": "hls",
        }]


# Backward compatibility alias
YouTubeExtractor = StreamExtractor
