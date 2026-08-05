"""Profile the SDK's multipart upload encoder with Scalene.

This benchmark exercises the same multipart encoder used by the production
frontend/API proxy, but consumes a synthetic upload locally instead of sending
it over the network. The synthetic file creates one chunk at a time, so the
benchmark can model multi-gigabyte uploads without allocating the entire test
file before profiling starts.

Example:

    scalene run --profile-all scripts/profile_multipart_upload.py --size-mb 2048
"""

from __future__ import annotations

import argparse
import sys
import time
import tracemalloc
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from packages.getoffline_sdk.transports import _encoded_body


@dataclass
class SyntheticUploadedFile:
    """Django-upload-like object that generates bounded chunks on demand."""

    name: str
    content_type: str
    size_bytes: int
    chunk_size: int
    chunks_emitted: int = 0

    def chunks(self) -> Iterator[bytes]:
        remaining = self.size_bytes
        while remaining:
            current_size = min(remaining, self.chunk_size)
            self.chunks_emitted += 1
            remaining -= current_size
            yield b"x" * current_size


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--size-mb",
        type=int,
        default=256,
        help="Synthetic upload size in MiB (default: 256).",
    )
    parser.add_argument(
        "--chunk-kb",
        type=int,
        default=1024,
        help="Synthetic upload chunk size in KiB (default: 1024).",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.size_mb <= 0 or args.chunk_kb <= 0:
        raise SystemExit("--size-mb and --chunk-kb must be positive")

    upload = SyntheticUploadedFile(
        name="scalene-large-video.mp4",
        content_type="video/mp4",
        size_bytes=args.size_mb * 1024 * 1024,
        chunk_size=args.chunk_kb * 1024,
    )

    tracemalloc.start()
    started = time.perf_counter()
    body, content_type = _encoded_body(
        {"title": "Scalene multipart benchmark", "file": upload}
    )
    if body is None or isinstance(body, bytes):
        raise AssertionError("multipart encoder returned an eager bytes body")

    encoded_bytes = 0
    body_parts = 0
    for part in body:
        encoded_bytes += len(part)
        body_parts += 1

    elapsed = time.perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    if upload.chunks_emitted == 0:
        raise AssertionError("synthetic upload was not consumed")

    print(f"content type: {content_type}")
    print(f"upload bytes: {upload.size_bytes:,}")
    print(f"encoded bytes: {encoded_bytes:,}")
    print(f"body parts: {body_parts:,}")
    print(f"upload chunks: {upload.chunks_emitted:,}")
    print(f"elapsed seconds: {elapsed:.3f}")
    print(f"throughput MiB/s: {upload.size_bytes / 1024 / 1024 / elapsed:.2f}")
    print(f"peak traced Python memory MiB: {peak_bytes / 1024 / 1024:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
