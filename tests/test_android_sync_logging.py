import subprocess
from pathlib import Path
from unittest.mock import patch

from workers.sync import AndroidSyncConfig
from workers.sync import AndroidSyncItem
from workers.sync import _run_adb_command
from workers.sync import sync_items_to_android


def test_adb_command_logs_at_info_level():
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")

    with patch("workers.sync.log.info") as info:
        _run_adb_command(
            ["adb", "devices"],
            description="checking connected Android devices",
            timeout=15,
            runner=runner,
        )

    messages = [call.args[0] for call in info.call_args_list]
    assert any("%s starting" in message for message in messages)
    assert any("%s finished" in message for message in messages)


def test_android_sync_logs_request_before_early_exit():
    config = AndroidSyncConfig(enabled=False, destination="/sdcard/Movies/GetOffline")
    item = AndroidSyncItem(
        row_id=1,
        title="Episode",
        source_name="Source",
        file_path=Path("/tmp/missing.mp3"),
    )

    with patch("workers.sync.log.info") as info:
        result = sync_items_to_android([item], config)

    assert result.message == "disabled"
    rendered = [str(call.args[0]) for call in info.call_args_list]
    assert "Android transfer requested: enabled=%s mode=%s destination=%s max_items=%s" in rendered
    assert "Android transfer skipped: disabled" in rendered
