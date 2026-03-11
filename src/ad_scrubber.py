import json
import re
import subprocess
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from logger import log

AD_PATTERNS = [
    r"\bwe(?:'|’)ll be right back\b",
    r"\bafter the break\b",
    r"\bnow a word from our sponsor\b",
    r"\bword from our sponsor\b",
    r"\bthis episode is sponsored by\b",
    r"\bthis show is sponsored by\b",
    r"\bsponsored by\b",
    r"\bthanks to our sponsor\b",
    r"\bbrought to you by\b",
    r"\bsupport for (?:this show|this podcast|today(?:'|’)s episode) comes from\b",
    r"\bwe(?:'|’)re sponsored by\b",
    r"\bour sponsor today is\b",
    r"\blet(?:'|’)s take a quick break\b",
    r"\btake a quick break\b",
    r"\bstay with us\b",
    r"\band now back to the show\b",
    r"\bback to the show\b",
    r"\bwe(?:'|’)re back\b",
    r"\bpromo code\b",
    r"\buse code\b",
    r"\buse offer code\b",
    r"\bvisit [a-z0-9\-]+\.(?:com|net|org)\b",
    r"\bdot com\b",
    r"\bfree trial\b",
    r"\blimited time\b",
    r"\bterms and conditions\b",
    r"\bridge wallet\b",
    r"\bfactor(?:\s+meals?|\s*75|\s*[_-]?\d+)?\b",
    r"\bhellofresh\b",
    r"\bgreen chef\b",
    r"\bhome chef\b",
    r"\bblue apron\b",
    r"\bworld of warships\b",
    r"\bwar thunder\b",
    r"\braid\s+shadow\s+legends\b",
    r"\bafk\s+journey\b",
    r"\bstate of survival\b",
    r"\bhero wars\b",
    r"\bmobile game(?:s)?\b",
    r"\bdownload (?:the|this) game\b",
    r"\bavailable on the app store\b",
    r"\bgoogle play(?: store)?\b",
    r"\bblue\s*chew\b",
    r"\bblu(?:e)?\s*chew(?:\.com)?\b",
    r"\bmood\s+gummies?\b",
    r"\bmood\.com\b",
    r"\bmicrodose\s+gummies?\b",
    r"\bviia\s+hemp\b",
    r"\bmagic\s+mind\b",
    r"\bbetterhelp\b",
    r"\btalkspace\b",
    r"\bpolicygenius\b",
    r"\bzip\s*recruiter\b",
    r"\bindeed\b",
    r"\bsquarespace\b",
    r"\bshopify\b",
    r"\bshipstation\b",
    r"\bstamps\.com\b",
    r"\brocket\s*mortgage\b",
    r"\bquickbooks\b",
    r"\bnetsuite\b",
    r"\bhims\b",
    r"\bhers\b",
    r"\bhims\s+and\s+hers\b",
    r"\broman\b",
    r"\bkeeps\b",
    r"\bnutrafol\b",
    r"\bag1\b",
    r"\bathletic\s+greens\b",
    r"\blmnt\b",
    r"\bliquid\s*i\.?v\b",
    r"\bseed\s+probiotics?\b",
    r"\bcare\/?of\b",
    r"\bcalm\b",
    r"\bheadspace\b",
    r"\bnoom\b",
    r"\bweight\s*watchers\b",
    r"\bmasterclass\b",
    r"\bskillshare\b",
    r"\baudible\b",
    r"\baudible\.com\b",
    r"\bamazon\s+music\b",
    r"\bspotify\s+premium\b",
    r"\bmanscaped\b",
    r"\bmeundies\b",
    r"\bbombas\b",
    r"\bshady\s*rays\b",
    r"\bwarby\s*parker\b",
    r"\braycon\b",
    r"\btheragun\b",
    r"\bwhoop\b",
    r"\bpeloton\b",
    r"\btonal\b",
    r"\bmirror\s+workout\b",
    r"\bair\s*doctor\b",
    r"\bmolekule\b",
    r"\bsimpli\s*safe\b",
    r"\bsimplesafe\b",
    r"\blink\s*home\s*security\b",
    r"\bvivint\b",
    r"\bstate\s*farm\b",
    r"\bprogressive\b",
    r"\bgeico\b",
    r"\ballstate\b",
    r"\bmint\s*mobile\b",
    r"\bvisible\s+wireless\b",
    r"\bverizon\b",
    r"\batt\b",
    r"\bt-?mobile\b",
    r"\bchime\b",
    r"\bsofi\b",
    r"\brobinhood\b",
    r"\bwealthfront\b",
    r"\bbetterment\b",
    r"\bupstart\b",
    r"\bcredit\s*karma\b",
    r"\bnerdwallet\b",
    r"\bexpressvpn\b",
    r"\bnordvpn\b",
    r"\bsurfshark\b",
    r"\bprivate\s+internet\s+access\b",
    r"\bdraftkings\b",
    r"\bfanduel\b",
    r"\bbetmgm\b",
    r"\bprizepicks\b",
    r"\bmybookie\b",
    r"\bcash\s*app\b",
    r"\bvenmo\b",
    r"\bpaypal\b",
    r"\bcoinbase\b",
    r"\bcrypto\.com\b",
    r"\bopensea\b",
    r"\bmeta\s*quest\b",
    r"\boculus\b",
    r"\bhello\s+tushy\b",
    r"\btushy\b",
    r"\bdisplate\b",
    r"\bdoor\s*dash\b",
    r"\buber\s*eats\b",
    r"\binstacart\b",
    r"\bpostmates\b",
    r"\bbetter\s*sleep\b",
    r"\bpublic\.com\b",
    r"\bm1\s+finance\b",
]
def _compile_patterns(patterns):
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            log.warning("Skipping invalid ad pattern %r: %s", pattern, exc)
    return compiled


COMPILED_PATTERNS = _compile_patterns(AD_PATTERNS)

_WHISPER_MODEL_CACHE = {}
_TRANSCRIPTION_CACHE = {}


def _transcribe_with_whisper(input_file: Path, model_name: str, log_prefix: str):
    input_file = Path(input_file).resolve()
    cache_key = (str(input_file), input_file.stat().st_mtime_ns, model_name)
    cached = _TRANSCRIPTION_CACHE.get(cache_key)
    if cached is not None:
        log.info("⏭️ Reusing cached transcription for %s: %s (%s)", log_prefix, input_file.name, model_name)
        return cached

    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError("openai-whisper is required for transcription.") from exc

    model = _WHISPER_MODEL_CACHE.get(model_name)
    if model is None:
        model = whisper.load_model(model_name)
        _WHISPER_MODEL_CACHE[model_name] = model

    result = model.transcribe(str(input_file), fp16=False)
    _TRANSCRIPTION_CACHE[cache_key] = result
    return result


def scrubbed_output_path(input_file: Path) -> Path:
    input_file = Path(input_file)
    return input_file.with_name(f"{input_file.stem}.no_ads{input_file.suffix}")


def run(cmd):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr}"
        )
    return result.stdout.strip()


def ffprobe_duration(path: Path) -> float:
    duration = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    return float(duration)


def merge_ranges(ranges, gap=1.5):
    if not ranges:
        return []

    merged = [list(sorted(ranges)[0])]
    for start, end in sorted(ranges)[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + gap:
            merged[-1][1] = max(last_end, end)
        else:
            merged.append([start, end])

    return [(max(0.0, s), max(0.0, e)) for s, e in merged if e > s]


def clamp_ranges(ranges, total_duration, min_len=0.25):
    clamped = []
    for start, end in ranges:
        s = max(0.0, min(float(start), total_duration))
        e = max(0.0, min(float(end), total_duration))
        if e - s >= min_len:
            clamped.append((s, e))
    return clamped


def invert_ranges(total_duration, cut_ranges):
    keep = []
    cursor = 0.0
    for start, end in cut_ranges:
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total_duration:
        keep.append((cursor, total_duration))
    return [(s, e) for s, e in keep if e - s > 0.25]


def detect_ad_segments(transcript, pre_roll=2.0, post_roll=2.0, min_hits=1):
    candidates = []
    for seg in transcript.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        hits = sum(1 for pat in COMPILED_PATTERNS if pat.search(text))
        if hits >= min_hits:
            start = max(0.0, float(seg["start"]) - pre_roll)
            end = float(seg["end"]) + post_roll
            candidates.append((start, end, text, hits))

    merged = merge_ranges([(s, e) for s, e, _, _ in candidates], gap=3.0)
    return merged, candidates


def cut_and_concat(input_file: Path, keep_ranges, output_file: Path):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        wav_parts = []

        for i, (start, end) in enumerate(keep_ranges):
            duration = end - start
            if duration <= 0.35:
                continue

            part = tmpdir / f"part_{i:04d}.wav"
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{start:.3f}",
                    "-t",
                    f"{duration:.3f}",
                    "-i",
                    str(input_file),
                    "-vn",
                    "-ac",
                    "2",
                    "-ar",
                    "44100",
                    "-c:a",
                    "pcm_s16le",
                    str(part),
                ]
            )
            if part.exists() and part.stat().st_size > 0:
                wav_parts.append(part)

        if not wav_parts:
            raise RuntimeError("No valid audio parts were created.")

        concat_list = tmpdir / "concat.txt"
        with concat_list.open("w", encoding="utf-8") as f:
            for part in wav_parts:
                f.write(f"file '{part.as_posix()}'\n")

        joined_wav = tmpdir / "joined.wav"
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c:a", "pcm_s16le", str(joined_wav)])
        run(["ffmpeg", "-y", "-i", str(joined_wav), "-c:a", "libmp3lame", "-q:a", "2", str(output_file)])


def scrub_audio_file(input_file: Path, settings: dict):
    input_file = Path(input_file)
    output_file = scrubbed_output_path(input_file)
    marker = output_file.with_name(f".{output_file.name}.adscrubbed.json")
    removed_text_report = output_file.with_suffix(f"{output_file.suffix}.removed_text.txt")

    if marker.exists() and output_file.exists() and output_file.stat().st_mtime >= input_file.stat().st_mtime:
        log.info("⏭️ Ad scrub skipped (already processed): %s -> %s", input_file.name, output_file.name)
        return output_file

    model_name = settings.get("model", "base")
    pre_roll = float(settings.get("pre_roll", 2.0))
    post_roll = float(settings.get("post_roll", 2.0))
    min_hits = int(settings.get("min_hits", 1))

    log.info("🧠 Transcribing for ad-scrub: %s (%s)", input_file.name, model_name)
    result = _transcribe_with_whisper(input_file, model_name, "ad-scrub")

    cut_ranges, raw_hits = detect_ad_segments(
        result,
        pre_roll=pre_roll,
        post_roll=post_roll,
        min_hits=min_hits,
    )

    total_duration = ffprobe_duration(input_file)
    min_ad_seconds = float(settings.get("min_ad_seconds", 8.0))
    cut_ranges = clamp_ranges(cut_ranges, total_duration, min_len=min_ad_seconds)

    log.info(
        "🔎 Ad scrub analysis for %s: %d matched transcript segments, %d cut range(s) after filtering",
        input_file.name,
        len(raw_hits),
        len(cut_ranges),
    )

    if not cut_ranges:
        marker.write_text(
            json.dumps(
                {
                    "input": str(input_file),
                    "output": str(output_file),
                    "cut_ranges": [],
                    "removed_seconds": 0.0,
                    "removed_percent": 0.0,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        log.info("✅ No ad ranges removed for %s (0.00s removed)", input_file.name)
        return None

    keep_ranges = invert_ranges(total_duration, cut_ranges)
    keep_ranges = clamp_ranges(keep_ranges, total_duration, min_len=0.35)

    removed_seconds = sum((end - start) for start, end in cut_ranges)
    removed_pct = (removed_seconds / total_duration * 100.0) if total_duration > 0 else 0.0

    for idx, (start, end) in enumerate(cut_ranges, start=1):
        log.info(
            "✂️ Cut range %d/%d for %s: %.2fs -> %.2fs (%.2fs)",
            idx,
            len(cut_ranges),
            input_file.name,
            start,
            end,
            end - start,
        )

    with tempfile.NamedTemporaryFile(suffix=input_file.suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        cut_and_concat(input_file, keep_ranges, tmp_path)
        tmp_path.replace(output_file)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    marker.write_text(
        json.dumps(
            {
                "input": str(input_file),
                "output": str(output_file),
                "duration": total_duration,
                "cut_ranges": cut_ranges,
                "keep_ranges": keep_ranges,
                "removed_seconds": removed_seconds,
                "removed_percent": removed_pct,
                "matched_segments": [
                    {"start": s, "end": e, "text": txt, "hits": hits}
                    for s, e, txt, hits in raw_hits
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    removed_segments = []
    for seg in result.get("segments", []):
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", 0.0))
        seg_text = (seg.get("text") or "").strip()
        if not seg_text:
            continue

        for cut_start, cut_end in cut_ranges:
            overlaps = max(seg_start, cut_start) < min(seg_end, cut_end)
            if overlaps:
                removed_segments.append((seg_start, seg_end, seg_text))
                break

    with removed_text_report.open("w", encoding="utf-8") as report_file:
        report_file.write(f"Input: {input_file}\n")
        report_file.write(f"Output: {output_file}\n\n")
        report_file.write("Removed ranges:\n")
        for idx, (start, end) in enumerate(cut_ranges, start=1):
            report_file.write(f"{idx:02d}. {start:.2f}s - {end:.2f}s ({end - start:.2f}s)\n")

        report_file.write("\nRemoved transcript segments (overlapping removed ranges):\n")
        if removed_segments:
            for seg_start, seg_end, seg_text in removed_segments:
                report_file.write(f"- [{seg_start:.2f}s - {seg_end:.2f}s] {seg_text}\n")
        else:
            report_file.write("- No transcript segments overlapped removed ranges.\n")

    log.info(
        "✅ Ad scrub complete for %s -> %s: removed %.2fs (%.2f%%) across %d range(s)",
        input_file.name,
        output_file.name,
        removed_seconds,
        removed_pct,
        len(cut_ranges),
    )
    return output_file




def _parse_srt_timestamp(value: str) -> float:
    hours, minutes, seconds_millis = value.split(":")
    seconds, millis = seconds_millis.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000.0
    )


def _format_srt_timestamp(value: float) -> str:
    value = max(0.0, value)
    hours = int(value // 3600)
    value -= hours * 3600
    minutes = int(value // 60)
    value -= minutes * 60
    seconds = int(value)
    millis = int(round((value - seconds) * 1000))

    if millis == 1000:
        millis = 0
        seconds += 1
    if seconds == 60:
        seconds = 0
        minutes += 1
    if minutes == 60:
        minutes = 0
        hours += 1

    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _shift_srt_timestamps(srt_path: Path, offset_seconds: float):
    if abs(offset_seconds) < 1e-6:
        return

    lines = srt_path.read_text(encoding="utf-8", errors="replace").splitlines()
    shifted = []
    timestamp_re = re.compile(
        r"^(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})(.*)$"
    )

    for line in lines:
        match = timestamp_re.match(line)
        if not match:
            shifted.append(line)
            continue

        start_raw, end_raw, tail = match.groups()
        start = _parse_srt_timestamp(start_raw) + offset_seconds
        end = _parse_srt_timestamp(end_raw) + offset_seconds

        start = max(0.0, start)
        end = max(start + 0.01, end)

        shifted.append(
            f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}{tail}"
        )

    srt_path.write_text("\n".join(shifted) + "\n", encoding="utf-8")

def generate_whisper_subtitles(input_file: Path, settings: dict, subtitle_path: Optional[Path] = None):
    input_file = Path(input_file)
    subtitle_path = Path(subtitle_path) if subtitle_path else input_file.with_suffix(".srt")

    if subtitle_path.exists() and subtitle_path.stat().st_mtime >= input_file.stat().st_mtime:
        log.info("⏭️ Subtitle generation skipped (already up to date): %s", subtitle_path.name)
        return subtitle_path

    try:
        from whisper.utils import get_writer
    except ImportError as exc:
        raise RuntimeError("openai-whisper is required for subtitle generation.") from exc

    model_name = settings.get("subtitle_model", settings.get("model", "base"))
    log.info("📝 Generating subtitles: %s (%s)", input_file.name, model_name)
    result = _transcribe_with_whisper(input_file, model_name, "subtitle-generation")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        temp_stem = "subtitle_output"
        writer = get_writer("srt", str(tmp_dir_path))
        writer(result, temp_stem)

        generated_subtitle_path = tmp_dir_path / f"{temp_stem}.srt"
        if not generated_subtitle_path.exists():
            srt_candidates = sorted(tmp_dir_path.glob("*.srt"))
            if srt_candidates:
                generated_subtitle_path = srt_candidates[0]
            else:
                raise RuntimeError(f"Whisper did not produce subtitle file in {tmp_dir_path}")

        subtitle_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(generated_subtitle_path, subtitle_path)

    if not subtitle_path.exists():
        raise RuntimeError(f"Subtitle output file was not created: {subtitle_path}")

    subtitle_offset = float(settings.get("subtitle_time_offset_seconds", 0.0))
    _shift_srt_timestamps(subtitle_path, subtitle_offset)

    log.info("✅ Subtitles generated: %s (offset: %.3fs)", subtitle_path.name, subtitle_offset)
    return subtitle_path
