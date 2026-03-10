from config import load_config
from podcasts import download_podcasts
from youtube import download_youtube_items


def main():
    config = load_config()
    downloaded_items = []

    download_youtube_items(config, downloaded_items)
    download_podcasts(config, downloaded_items)

    if downloaded_items:
        print("\n✅ Download Summary:")
        for item in downloaded_items:
            print(f" - {item}")
    else:
        print("\n📭 Nothing new was downloaded.")


if __name__ == "__main__":
    main()
