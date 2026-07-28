#!/usr/bin/env python3
"""Sync validated media files into a flat directory for a host cron job."""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


MEDIA_SUFFIXES = {".mp3", ".mp4"}


def sanitize_component(value: str) -> str:
    value = value.replace("\n", " ").replace("\r", " ")
    for character in '/\\:*?"<>|':
        value = value.replace(character, "-")
    value = " ".join(value.split()).strip(" .-")
    return value or "Unknown Artist"


def is_media_file(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_SUFFIXES


def has_media_stream(path: Path) -> bool:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and any(
        line.strip() in {"audio", "video"} for line in result.stdout.splitlines()
    )


def _numeric_or_lookup(value: str, *, user: bool) -> int:
    try:
        return int(value)
    except ValueError:
        if user:
            return pwd.getpwnam(value).pw_uid
        return grp.getgrnam(value).gr_gid


def resolve_owner(value: str) -> tuple[int, int]:
    user_value, separator, group_value = value.partition(":")
    if not user_value and not group_value:
        raise ValueError("owner must be <user[:group]> or <uid[:gid]>")

    if user_value:
        uid = _numeric_or_lookup(user_value, user=True)
        default_gid = pwd.getpwuid(uid).pw_gid
    else:
        uid = -1
        default_gid = -1

    if separator and group_value:
        gid = _numeric_or_lookup(group_value, user=False)
    elif separator:
        raise ValueError("owner group cannot be empty when a colon is provided")
    else:
        gid = default_gid
    return uid, gid


def chown_tree(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            chown_tree(child, uid, gid)


def copy_media(source: Path, destination: Path, uid: int, gid: int) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".sync-media.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        shutil.copy2(source, temporary_path)
        if not has_media_stream(temporary_path):
            raise ValueError("copied file failed ffprobe validation")
        os.replace(temporary_path, destination)
        temporary_path = None
        os.chown(destination, uid, gid)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_media_downloads.py",
        description="Copy validated MP3 and MP4 files into a flat sync directory.",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="copy files even when the destination is up to date",
    )
    parser.add_argument("downloads_dir", help="directory containing downloaded media")
    parser.add_argument(
        "sync_dir", help="destination directory, including a mounted path"
    )
    parser.add_argument("owner", help="owner in user[:group] or uid[:gid] form")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    downloads_dir = Path(args.downloads_dir)
    sync_dir = Path(args.sync_dir)
    force_resync = args.force or os.environ.get("FORCE_RESYNC") == "1"
    dry_run = os.environ.get("DRY_RUN") == "1"
    verbose = os.environ.get("VERBOSE") == "1"

    if not downloads_dir.is_dir():
        print(f"downloads directory does not exist: {downloads_dir}", file=sys.stderr)
        return 66
    if shutil.which("ffprobe") is None:
        print("ffprobe is required but was not found on PATH", file=sys.stderr)
        return 69

    try:
        uid, gid = resolve_owner(args.owner)
    except (KeyError, ValueError) as exc:
        print(f"invalid owner {args.owner!r}: {exc}", file=sys.stderr)
        return 64

    sync_dir.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        chown_tree(sync_dir, uid, gid)

    copied = 0
    skipped = 0
    failed = 0
    for source_path in sorted(
        (path for path in downloads_dir.rglob("*") if path.is_file()),
        key=lambda path: str(path),
    ):
        if not is_media_file(source_path):
            continue

        artist = sanitize_component(source_path.parent.name)
        filename = sanitize_component(source_path.name)
        destination = sync_dir / f"{artist} - {filename}"

        if not has_media_stream(source_path):
            failed += 1
            print(f"invalid media (ffprobe): {source_path}", file=sys.stderr)
            continue

        if (
            not force_resync
            and destination.exists()
            and source_path.stat().st_mtime <= destination.stat().st_mtime
            and has_media_stream(destination)
        ):
            skipped += 1
            if verbose:
                print(f"skip: {destination}")
            continue

        print(f"copy: {source_path} -> {destination}")
        if dry_run:
            copied += 1
            continue

        try:
            copy_media(source_path, destination, uid, gid)
            copied += 1
        except (OSError, ValueError) as exc:
            failed += 1
            print(f"failed: {source_path}: {exc}", file=sys.stderr)

    if not dry_run:
        chown_tree(sync_dir, uid, gid)

    print(
        f"sync complete: copied={copied} skipped={skipped} "
        f"failed={failed} destination={sync_dir}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
