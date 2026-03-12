import argparse

from config import load_config
from podcasts import download_podcasts
from webapp import run_webapp
from youtube import download_youtube_items


def run_downloads():
    config = load_config()
    downloaded_items = []

    download_youtube_items(config, downloaded_items)
    download_podcasts(config, downloaded_items)

    if downloaded_items:
        print("\nDownload Summary:")
        for item in downloaded_items:
            print(f" - {item}")
    else:
        print("\nNothing new was downloaded.")


def run_server(host: str, port: int):
    config = load_config()
    run_webapp(config=config, host=host, port=port)


def parse_args():
    parser = argparse.ArgumentParser(description="GetOffline media downloader and browser player")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("download", help="Run YouTube and podcast downloads")

    serve_parser = subparsers.add_parser("serve", help="Start local web UI to browse/play downloaded media")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8080)

    return parser.parse_args()


def main():
    args = parse_args()
    if args.command in (None, "download"):
        run_downloads()
        return

    if args.command == "serve":
        run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
