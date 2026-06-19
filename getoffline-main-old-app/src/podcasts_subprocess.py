import json
import sys

from podcasts import _download_podcast_entry_in_subprocess

if __name__ == '__main__':
    payload = json.loads(sys.stdin.read())
    result = _download_podcast_entry_in_subprocess(payload)
    sys.stdout.write(json.dumps(result))
