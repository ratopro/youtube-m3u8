import json
import logging
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

import requests

logger = logging.getLogger("drm_engine")


class DrmCfgParser:
    def __init__(self, cfg_path: str):
        self.cfg_path = Path(cfg_path)
        self.data = {}

    def parse(self) -> list[dict]:
        with open(self.cfg_path, encoding="utf-8") as f:
            self.data = json.load(f)
        return self.data.get("Channels", [])

    def get_name(self) -> str:
        return self.data.get("Name", self.cfg_path.stem)

    @staticmethod
    def find_all(base_dir: str = "drm") -> list[dict]:
        base = Path(base_dir)
        if not base.exists():
            return []
        sources = []
        for f in sorted(base.glob("*.cfg")):
            parser = DrmCfgParser(str(f))
            channels = parser.parse()
            sources.append({
                "file": str(f),
                "name": parser.get_name(),
                "channels": [
                    {"name": ch.get("Name", ch.get("Id", "?")), "id": ch.get("Id", "?")}
                    for ch in channels
                ],
                "channel_count": len(channels),
            })
        return sources


class DrmChannelStreamer:
    def __init__(self, channel_config: dict, hls_dir: str):
        self.config = channel_config
        self.hls_dir = Path(hls_dir)
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        self.playlist_path = self.hls_dir / "live.m3u8"
        self.segment_pattern = str(self.hls_dir / "seg_%06d.ts")

        self.running = False
        self.error = None
        self._thread: threading.Thread | None = None
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._segment_count = 0
        self._init_data: bytes | None = None
        self._mpd_base_url = ""
        self._rep_id = ""
        self._media_template = ""
        self._init_template = ""
        self._start_number = 1
        self._timescale = 1
        self._duration = 0
        self._key_hex = ""

    def _get_key(self) -> str | None:
        keys = self.config.get("Keys", [])
        if not keys:
            logger.warning("No keys found in channel config")
            return None
        first = keys[0]
        parts = first.split(":")
        return parts[1] if len(parts) == 2 else parts[0]

    def _parse_mpd(self, mpd_text: str) -> bool:
        root = ET.fromstring(mpd_text)
        tag = root.tag
        ns = ""
        if tag.startswith("{"):
            ns = tag[:tag.index("}") + 1]

        rep_root = root.find(f".//{ns}AdaptationSet")
        rep = rep_root.find(f"{ns}Representation") if rep_root is not None else root.find(f".//{ns}Representation")
        if rep is None:
            rep = root.find(f".//Representation")
            if rep is None:
                logger.error("No Representation found in MPD (ns=%s)", ns)
                return False

        seg_template = rep.find(f"{ns}SegmentTemplate")
        if seg_template is None:
            seg_template = rep.find("SegmentTemplate")
            if seg_template is None:
                logger.error("No SegmentTemplate found in MPD (ns=%s)", ns)
                return False

        base_url = ""
        for elem in [root, root.find(f".//{ns}Period") if ns else root.find(".//Period"), rep_root or rep, rep]:
            if elem is not None:
                bu = elem.findtext(f"{ns}BaseURL", "")
                if not bu and not ns:
                    bu = elem.findtext("BaseURL", "")
                if bu:
                    base_url = bu
                    break

        self._mpd_base_url = base_url
        self._rep_id = rep.get("id", "1")
        self._init_template = seg_template.get("initialization", "")
        self._media_template = seg_template.get("media", "")
        self._start_number = int(seg_template.get("startNumber", "1"))
        self._timescale = int(seg_template.get("timescale", "1"))
        self._duration = int(seg_template.get("duration", "0"))

        logger.info("Parsed MPD: rep=%s, base=%s, dur=%d, timescale=%d",
                     self._rep_id, base_url, self._duration, self._timescale)
        return True

    def _resolve_url(self, template: str, number: int = 0) -> str:
        url = template.replace("$RepresentationID$", self._rep_id)
        url = url.replace("$Number$", str(number))
        url = url.replace("$Number%05d$", f"{number:05d}")
        url = url.replace("$Time$", str(number * self._duration))
        if self._mpd_base_url:
            return urljoin(self._mpd_base_url, url)
        return url

    def _decrypt_segment(self, key_hex: str, init_data: bytes, seg_data: bytes) -> bytes | None:
        combined = init_data + seg_data
        logger.debug("Decrypting segment: key_hex=%s, combined_size=%d", key_hex[:8] + "...", len(combined))
        proc = subprocess.Popen(
            [
                "ffmpeg",
                "-decryption_key", key_hex,
                "-f", "mp4",
                "-i", "pipe:0",
                "-c", "copy",
                "-f", "mpegts",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = proc.communicate(input=combined, timeout=60)
        if proc.returncode != 0 or not out:
            logger.error("ffmpeg decrypt failed (rc=%d): %s", proc.returncode, err[:500].decode("utf-8", errors="replace"))
            return None
        return out

    def _fetch_mpd(self, manifest_url: str) -> str | None:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            r = requests.get(manifest_url, timeout=15, headers=headers)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            status = getattr(e.response, 'status_code', 'connection')
            logger.error("Failed to fetch MPD (HTTP %s): %s", status, manifest_url)
            return None

    def _fetch_segment(self, url: str) -> bytes | None:
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            logger.warning("Failed to fetch segment %s: %s", url, e)
            return None

    def _write_playlist(self, seg_paths: list[str]):
        duration = max(1, self._duration / self._timescale) if self._duration and self._timescale else 4
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:" + str(int(duration) + 1),
            "#EXT-X-MEDIA-SEQUENCE:" + str(max(0, self._segment_count - len(seg_paths))),
        ]
        for sp in seg_paths:
            lines.append(f"#EXTINF:{duration:.3f},")
            lines.append(sp)
        lines.append("")
        self.playlist_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Playlist written with %d segments", len(seg_paths))

    def stop(self):
        self.running = False
        if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
            self._ffmpeg_proc.terminate()
            try:
                self._ffmpeg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._ffmpeg_proc.kill()
        self._ffmpeg_proc = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def start(self):
        if self.running:
            return
        manifest_url = self.config.get("Manifest", "")
        if not manifest_url:
            self.error = "No manifest URL in config"
            logger.error(self.error)
            return

        key_hex = self._get_key()
        if not key_hex:
            self.error = "No key found in config"
            logger.error(self.error)
            return
        self._key_hex = key_hex

        logger.info("Starting DRM streamer: manifest=%s", manifest_url)
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        for item in self.hls_dir.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)

        manifest_url = self.config.get("Manifest", "")
        mpd_text = self._fetch_mpd(manifest_url)
        if not mpd_text:
            self.error = "Could not fetch MPD"
            self.running = False
            return

        if not self._parse_mpd(mpd_text):
            self.error = "Could not parse MPD"
            self.running = False
            return

        init_url = self._resolve_url(self._init_template)
        logger.info("Fetching init segment: %s", init_url)
        init_data = self._fetch_segment(init_url)
        if not init_data:
            self.error = f"Could not fetch init segment: {init_url}"
            self.running = False
            return
        self._init_data = init_data
        logger.info("Init segment downloaded: %d bytes", len(init_data))

        next_seg = self._start_number
        pending_segs: list[str] = []
        max_pending = 6

        while self.running:
            try:
                mpd_text = self._fetch_mpd(manifest_url)
                if mpd_text:
                    self._parse_mpd(mpd_text)

                seg_url = self._resolve_url(self._media_template, next_seg)
                logger.debug("Fetching segment %d: %s", next_seg, seg_url)
                seg_data = self._fetch_segment(seg_url)
                if seg_data:
                    logger.debug("Segment %d downloaded: %d bytes", next_seg, len(seg_data))
                    ts_data = self._decrypt_segment(self._key_hex, init_data, seg_data)
                    if ts_data:
                        seg_path = self.hls_dir / f"seg_{self._segment_count:06d}.ts"
                        seg_path.write_bytes(ts_data)
                        pending_segs.append(seg_path.name)
                        self._segment_count += 1
                        logger.info("Segment %d decrypted and saved: %s (%d bytes)", next_seg, seg_path.name, len(ts_data))

                        if len(pending_segs) > max_pending:
                            old = pending_segs.pop(0)
                            (self.hls_dir / old).unlink(missing_ok=True)

                        self._write_playlist(pending_segs)
                    else:
                        logger.error("Failed to decrypt segment %d", next_seg)
                else:
                    logger.warning("Segment %d not available (empty response)", next_seg)

                next_seg += 1
                sleep_time = max(1, int(self._duration / self._timescale) - 1) if self._duration and self._timescale else 3
                time.sleep(sleep_time)

            except Exception as e:
                logger.error("Error in DRM stream loop: %s", e, exc_info=True)
                self.error = str(e)
                time.sleep(5)
