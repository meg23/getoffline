from config import load_config
from youtube import download_youtube_items
from podcasts import download_podcasts
from creators import download_creator_posts


def main():
    config = load_config()
    downloaded_items = []

    download_youtube_items(config, downloaded_items)
    download_podcasts(config, downloaded_items)
    download_creator_posts(config, downloaded_items)

    if downloaded_items:
        print("\n✅ Download Summary:")
        for item in downloaded_items:
            print(f" - {item}")
    else:
        print("\n📭 Nothing new was downloaded.")


if __name__ == "__main__":
    main()
