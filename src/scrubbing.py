from pathlib import Path
from typing import Tuple

from ad_scrubber import TranscriptionError, scrub_audio_file


def scrub_media_file(
    media_file: Path,
    scrubber_cfg: dict,
    scrubber_enabled: bool,
    entry_scrub_enabled: bool,
    logger,
    context_name: str,
    context_label: str,
) -> Tuple[Path, bool]:
    """Return playback media path and whether subtitles should be skipped after scrub failure."""
    playback_media = media_file
    skip_subtitles_after_scrub_failure = False

    if scrubber_enabled and entry_scrub_enabled:
        logger.info("Starting ad scrub for %s file: %s", context_label, media_file.name)
        try:
            scrubbed_output = scrub_audio_file(media_file, scrubber_cfg)
            if scrubbed_output:
                playback_media = scrubbed_output
                logger.info("Ad scrubbed %s file: %s", context_label, scrubbed_output.name)
            else:
                logger.info("Ad scrub made no changes for %s file: %s", context_label, media_file.name)
        except TranscriptionError as scrub_exc:
            skip_subtitles_after_scrub_failure = True
            logger.warning("Ad scrub failed for %s: %s", media_file, scrub_exc)
        except Exception as scrub_exc:
            logger.warning("Ad scrub failed for %s: %s", media_file, scrub_exc)
    else:
        logger.info(
            "Ad scrub disabled for %s (global=%s entry=%s)",
            context_name,
            scrubber_enabled,
            entry_scrub_enabled,
        )

    return playback_media, skip_subtitles_after_scrub_failure
