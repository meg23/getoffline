import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

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
]
COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in AD_PATTERNS]


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
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
    marker = input_file.with_suffix(f"{input_file.suffix}.adscrubbed.json")
    if marker.exists() and marker.stat().st_mtime >= input_file.stat().st_mtime:
        log.info(f"⏭️ Ad scrub skipped (already processed): {input_file.name}")
        return False

    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError("openai-whisper is required for ad scrubbing.") from exc

    model_name = settings.get("model", "base")
    pre_roll = float(settings.get("pre_roll", 2.0))
    post_roll = float(settings.get("post_roll", 2.0))
    min_hits = int(settings.get("min_hits", 1))

    log.info(f"🧠 Transcribing for ad-scrub: {input_file.name} ({model_name})")
    model = whisper.load_model(model_name)
    result = model.transcribe(str(input_file), fp16=False)

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
            json.dumps({"input": str(input_file), "cut_ranges": []}, indent=2),
            encoding="utf-8",
        )
        log.info(f"✅ No ad ranges removed for {input_file.name} (0.00s removed)")
        return False

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
        shutil.move(str(tmp_path), str(input_file))
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    marker.write_text(
        json.dumps(
            {
                "input": str(input_file),
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

    log.info(
        "✅ Ad scrub complete for %s: removed %.2fs (%.2f%%) across %d range(s)",
        input_file.name,
        removed_seconds,
        removed_pct,
        len(cut_ranges),
    )
    return True
