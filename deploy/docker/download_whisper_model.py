import os

from faster_whisper.utils import download_model

model = os.environ.get("WHISPER_MODEL", "base")
download_model(model, output_dir=os.environ["GETOFFLINE_MODEL_CACHE_DIR"])
