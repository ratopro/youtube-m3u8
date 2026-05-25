import argparse
import os

from src.youtube_extractor import YouTubeExtractor
from src.web import create_app

def main():
    parser = argparse.ArgumentParser(description="Reemit a YouTube live stream as local HLS")
    parser.add_argument("url", nargs="?", help="YouTube live URL")
    parser.add_argument("--output-dir", default="output/hls", help="Directory for generated HLS files when using ffmpeg relay")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP server host")
    parser.add_argument("--port", default=5000, type=int, help="HTTP server port")

    args = parser.parse_args()
    url = args.url or os.environ.get("YOUTUBE_URL")

    hls_url = None
    if url:
        print(f"Comprobando stream HLS desde: {url}")
        hls_url = YouTubeExtractor(url).get_hls_url()

    app = create_app(args.output_dir, upstream_hls_url=hls_url)
    print(f"Abre el reproductor en: http://{args.host}:{args.port}")
    print(f"Playlist HLS local: http://{args.host}:{args.port}/live.m3u8")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
