import argparse
from pathlib import Path
from typing import Dict, List

from config import load_bootstrap_config, load_config
from podcasts import download_podcasts
from webapp import AppState, VIDEO_EXTENSIONS, import_local_media_file, run_webapp
from youtube import download_youtube_items


def run_downloads() -> None:
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


def run_server(host: str, port: int) -> None:
    config = load_bootstrap_config()
    run_webapp(config=config, host=host, port=port)


def _path_sort_key(path: Path) -> str:
    return str(path).casefold()


def _directory_video_files(directory: Path, recursive: bool, excluded_root: Path) -> List[Path]:
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    files = []
    for candidate in iterator:
        resolved_candidate = candidate.expanduser().resolve()
        if not resolved_candidate.is_file():
            continue
        if resolved_candidate.suffix.lower().lstrip(".") not in VIDEO_EXTENSIONS:
            continue
        if resolved_candidate == excluded_root or excluded_root in resolved_candidate.parents:
            continue
        files.append(resolved_candidate)
    return sorted(files, key=_path_sort_key)


def _unused_update_runner(config: Dict, downloaded_items: List[str]) -> None:
    _ = config, downloaded_items


def run_directory_import(directory: str, recursive: bool = False) -> int:
    source_directory = Path(directory).expanduser().resolve()
    if not source_directory.is_dir():
        print(f"Import directory does not exist or is not a directory: {source_directory}")
        return 2

    config = load_config()
    output_root = Path(str(config["defaults"]["output_root"])).expanduser().resolve()
    database_path = Path(str(config["defaults"]["database_path"])).expanduser().resolve()
    state = AppState(
        output_root=output_root,
        database_path=database_path,
        config=config,
        update_runner=_unused_update_runner,
    )
    destination_root = output_root / "manual"
    video_files = _directory_video_files(source_directory, recursive, destination_root)
    imported_count = 0
    failed_count = 0
    for video_file in video_files:
        try:
            destination_path = import_local_media_file(state, video_file)
            outcome = "filtered" if not destination_path.exists() else "imported"
            print(f"{outcome}: {video_file}")
            imported_count += 1
        except Exception as exc:
            print(f"failed: {video_file}: {exc}")
            failed_count += 1

    print(
        f"Directory import complete: processed={imported_count} "
        f"failed={failed_count} candidates={len(video_files)}"
    )
    return 1 if failed_count else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GetOffline media downloader and browser player")
    parser.set_defaults(host="127.0.0.1", port=8080)
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("download", help="Run YouTube and podcast downloads")

    serve_parser = subparsers.add_parser("serve", help="Start local web UI to browse/play downloaded media")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8080)

    import_parser = subparsers.add_parser(
        "import-directory",
        help="Import video files from a directory using the browser drag-and-drop workflow",
    )
    import_parser.add_argument("directory", help="Directory containing video files to import")
    import_parser.add_argument("--recursive", action="store_true", help="Include videos in subdirectories")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "download":
        run_downloads()
        return

    if args.command == "import-directory":
        raise SystemExit(run_directory_import(args.directory, recursive=args.recursive))

    if args.command in (None, "serve"):
        run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
