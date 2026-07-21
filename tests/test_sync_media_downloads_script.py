from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "sync-media-downloads.sh"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(0o755)


def _script_environment(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "rsync", "#!/bin/sh\ncp \"$3\" \"$4\"\n")
    _write_executable(bin_dir / "chown", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "ffprobe",
        '#!/bin/sh\nfor argument do media_path=$argument; done\n'
        'case "$(cat \"$media_path\" 2>/dev/null)" in GOOD*) echo audio;; *) exit 1;; esac\n',
    )
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _run_sync(tmp_path: Path, *options: str) -> subprocess.CompletedProcess[str]:
    downloads = tmp_path / "downloads" / "An Artist"
    destination = tmp_path / "sync"
    downloads.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=True)
    return subprocess.run(
        [str(SCRIPT), *options, str(downloads.parent), str(destination), "owner"],
        env=_script_environment(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )


def test_force_resync_replaces_an_up_to_date_destination(tmp_path: Path) -> None:
    source = tmp_path / "downloads" / "An Artist" / "track-converted.mp3"
    destination = tmp_path / "sync" / "An Artist - track-converted.mp3"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir()
    source.write_text("GOOD-new")
    destination.write_text("GOOD-old")
    source.touch()
    destination.touch()

    result = _run_sync(tmp_path, "--force")

    assert result.returncode == 0, result.stderr
    assert "copied=1 skipped=0 failed=0" in result.stdout
    assert destination.read_text() == "GOOD-new"


def test_invalid_source_does_not_replace_destination(tmp_path: Path) -> None:
    source = tmp_path / "downloads" / "An Artist" / "track-converted.mp3"
    destination = tmp_path / "sync" / "An Artist - track-converted.mp3"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir()
    source.write_text("BROKEN")
    destination.write_text("GOOD")

    result = _run_sync(tmp_path, "--force")

    assert result.returncode == 1
    assert "invalid media (ffprobe)" in result.stderr
    assert destination.read_text() == "GOOD"
