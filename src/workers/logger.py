import logging
import os


class YTDLPStyleAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"yt-dlp: [{self.extra['channel']}] {msg}", kwargs


log_path = os.path.expanduser("~/youtube/youtube_batch_dl.log")
os.makedirs(os.path.dirname(log_path), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

# Pika emits very chatty INFO logs for normal connection shutdown/reconnect cycles.
# Keep GetOffline worker lifecycle logs visible without burying transcript/subtitle progress.
logging.getLogger("pika").setLevel(logging.WARNING)


def get_logger(channel: str):
    return YTDLPStyleAdapter(logging.getLogger("getoffline"), {"channel": channel})
