import os
import re
from pathlib import Path

def sanitize(name):
    return re.sub(r"[^\w.-]", "_", name)

def ensure_dir(path):
    Path(path).expanduser().mkdir(parents=True, exist_ok=True)

