from ad_scrubber import generate_whisper_subtitles
from utils import create_audio_visualizer_video


def create_subtitles_and_optional_visualizer(
    media_file,
    scrubber_cfg: dict,
    subtitle_offset_seconds,
    entry_subtitles_enabled: bool,
    entry_visualize_enabled: bool,
    logger,
    context_name: str,
    context_label: str,
    skip_subtitles_after_scrub_failure: bool = False,
):
    if entry_subtitles_enabled and skip_subtitles_after_scrub_failure:
        logger.warning(
            "Skipping subtitle generation for %s because transcription failed during ad scrub",
            media_file,
        )
        return None

    if entry_subtitles_enabled and media_file.exists():
        try:
            subtitle_settings = dict(scrubber_cfg)
            if subtitle_offset_seconds is not None:
                subtitle_settings["subtitle_time_offset_seconds"] = float(subtitle_offset_seconds)
            subtitle_path = generate_whisper_subtitles(media_file, subtitle_settings)
            logger.info("Generated %s subtitles: %s", context_label, subtitle_path.name)

            if entry_visualize_enabled:
                try:
                    visualizer_path = create_audio_visualizer_video(media_file, subtitle_path)
                    logger.info("Generated %s visualizer: %s", context_label, visualizer_path.name)
                except Exception as viz_exc:
                    logger.warning("Visualizer generation failed for %s: %s", media_file, viz_exc)

            return subtitle_path
        except Exception as subtitle_exc:
            logger.warning("Subtitle generation failed for %s: %s", media_file, subtitle_exc)
            return None

    if entry_visualize_enabled:
        logger.info("Visualizer skipped for %s because subtitles are disabled", context_name)
    return None
