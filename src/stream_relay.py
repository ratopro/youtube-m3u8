import shutil
import subprocess
import sys
import time
from pathlib import Path


class StreamRelay:
    """Runs ffmpeg to restream an input HLS URL into local HLS files."""

    def __init__(self, output_dir: str = "output/hls"):
        self.output_dir = Path(output_dir)
        self.playlist_path = self.output_dir / "index.m3u8"
        self.process: subprocess.Popen | None = None
        self.downloader_process: subprocess.Popen | None = None

    def prepare_output(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for item in self.output_dir.glob("*"):
            if item.is_file():
                item.unlink()

    def start(self, source_url: str) -> subprocess.Popen:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg no esta instalado o no esta en el PATH.")

        self.prepare_output()

        downloader_command = [
            sys.executable,
            "-m", "yt_dlp",
            "--quiet",
            "--no-warnings",
            "--no-part",
            "-f", "best[protocol*=m3u8]/best",
            "-o", "-",
            source_url,
        ]

        self.downloader_process = subprocess.Popen(
            downloader_command,
            stdout=subprocess.PIPE,
        )

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "warning",
            "-i", "pipe:0",
            "-c", "copy",
            "-f", "hls",
            "-hls_time", "4",
            "-hls_list_size", "6",
            "-hls_flags", "delete_segments+append_list+omit_endlist",
            "-hls_segment_filename", str(self.output_dir / "segment_%05d.ts"),
            str(self.playlist_path),
        ]

        self.process = subprocess.Popen(command, stdin=self.downloader_process.stdout)
        if self.downloader_process.stdout:
            self.downloader_process.stdout.close()
        return self.process

    def wait_until_ready(self, timeout: int = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                return False
            if self.playlist_path.exists() and any(self.output_dir.glob("segment_*.ts")):
                return True
            time.sleep(1)
        return False

    def stop(self) -> None:
        processes = [self.process, self.downloader_process]

        for process in processes:
            if not process or process.poll() is not None:
                continue

            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
