from config import load_config
from logger import log
from youtube import download_youtube_items
from podcast import download_podcasts

def main():
    config = load_config()
    downloaded_items = []

    youtube(config, downloaded_items)
    podcasts(config, downloaded_items)

    if downloaded_items:
        print("\n✅ Download Summary:")
        for item in downloaded_items:
            print(f" - {item}")
    else:
        print("\n📭 Nothing new was downloaded.")

if __name__ == "__main__":
    main()
