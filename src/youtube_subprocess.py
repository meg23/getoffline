import json, sys
from pathlib import Path
from youtube import _download_youtube_entry_in_subprocess

if __name__ == '__main__':
    payload = json.loads(sys.stdin.read())
    result = _download_youtube_entry_in_subprocess(payload)
    sys.stdout.write(json.dumps(result))
