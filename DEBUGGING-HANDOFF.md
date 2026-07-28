# GetOffline 500 Error and Profanity Censoring Handoff

You are continuing work in the GetOffline repository.

Repository:

```text
/home/rerotas/projects/get-offline/getoffline
```

Branch:

```text
296-audio-profanity-censoring-with-ffmpeg-and-whisper
```

The original workstation was at:

```text
HEAD 1354e81
```

Important:

- The fixes described below were uncommitted local changes on another workstation.
- Do not assume they exist on this workstation.
- Inspect the current branch and working tree before editing.
- Preserve unrelated local changes.
- Do not commit `.env`, `.venv`, `.coverage`, `.test-model-cache`, `__pycache__`, downloads, credentials, or other generated files.
- Read the repository's `AGENTS.md` before editing.
- Use `refs/heads/main` when comparing with main because this repository has both a branch and tag named `main`.
- Do not add `@csrf_exempt` to the login or logout endpoint. That was the initial theory, but it was disproven.
- Debug credentials have been supplied separately. Do not put them in source files, tests, logs, or commits.

## Objective

Recreate, review, test, and commit the fixes for:

1. The 500 response encountered immediately after login.
2. The incomplete profanity-censoring feature.
3. The FFmpeg mute/beep implementation.
4. Profanity job routing and CPU-slot scheduling.
5. Settings persistence and propagation.
6. Profanity status display.
7. Regression tests and quality checks.

Do not push until the implementation has been reviewed and all applicable tests pass.

## Root cause of the login 500

The login API was not the source of the 500.

What was initially suspected:

- A direct POST to the API login endpoint without a CSRF cookie returns:

  ```text
  403 "CSRF cookie not set"
  ```

- It was proposed that `@csrf_exempt` should be added to the API login view.

What live testing proved:

- A browser-style login request that first obtains a CSRF cookie succeeds.
- The login POST returns HTTP 302.
- The response sets a valid session cookie.
- The API library endpoint returns HTTP 200 for the authenticated session.
- The 500 occurs after the login redirect, while rendering the frontend library page.

The actual traceback was:

```text
django.template.exceptions.TemplateSyntaxError:
Could not parse the remainder: '=='unplayed''
```

The branch had reformatted valid Django template expressions into invalid expressions without spaces around comparison operators.

Broken example:

```django
{% if library_filter_mode=='unplayed' %}
```

Correct Django syntax:

```django
{% if library_filter_mode == 'unplayed' %}
```

The settings template had the same problem. Some template variables and `{% if %}` tags had also been split across lines in ways Django could not parse.

Therefore:

- Keep normal CSRF protection.
- Do not add `@csrf_exempt`.
- Fix the templates.
- Add a regression test that compiles the browser templates.

## Files changed

The original completed working tree modified these source/test files:

```text
src/api/services/dashboard_actions.py
src/api/services/library.py
src/frontend/routing.py
src/frontend/static/app/dashboard.css
src/frontend/static/app/settings.css
src/frontend/templates/app/library.html
src/frontend/templates/app/settings.html
src/models/domain.py
src/models/models.py
src/workers/censor.py
src/workers/content_filter.py
src/workers/handlers.py
src/workers/runner.py
src/workers/scheduler.py
tests/test_audio_censoring_integration.py
tests/test_censor.py
tests/test_coverage_worker_runtime.py
tests/test_frontend_proxy.py
tests/test_integration_pipeline_helpers.py
```

The approximate diff size was:

```text
19 files changed
529 insertions
181 deletions
```

Some changes are import cleanup or corrections to tests introduced by the feature branch. Do not omit them without checking the current state.

## 1. Fix Django template 500 errors

File:

```text
src/frontend/templates/app/library.html
```

Fix all Django comparison expressions so operators have spaces.

Examples:

```text
library_filter_mode == 'unplayed'
library_filter_mode == 'played'
library_filter_mode == 'favorites'
library_filter_mode == 'all'
```

Do not leave expressions like:

```text
library_filter_mode=='unplayed'
```

Keep template variable interpolation on one parseable line. For example:

```django
{{ item.title|default:'Untitled' }}
```

Do not split a template expression between lines after a filter argument.

Keep the table row formatting compatible with existing tests. The tests expect the `<tr` and `data-row-id` sequence to resemble:

```html
<tr
  data-row-id="{{ item.id }}"
```

The exact indentation matters to existing template assertions.

Replace the status container's inline style:

```html
style="display: flex; gap: 4px;"
```

with:

```html
class="profanity-status"
```

Keep both pills:

- The ordinary download status pill.
- The optional profanity status pill using:
  - `item.profanity_label`
  - `item.profanity_class`

File:

```text
src/frontend/templates/app/settings.html
```

Fix every malformed comparison, including:

```text
settings.manual_upload_censor_method == 'duck'
settings.manual_upload_censor_method == 'beep'
settings.audio_format == 'mp3'
settings.audio_format == 'm4a'
settings.audio_format == 'opus'
settings.video_format == 'mp4'
settings.video_format == 'mkv'
settings.video_codec == 'h264'
settings.video_codec == 'hevc'
settings.video_codec == 'copy'
source.media_type != 'video'
source.media_type == 'video'
source.manual_upload_censor_method == 'duck'
source.manual_upload_censor_method == 'beep'
```

Do not split `{% if source.manual_upload_censor_profanity %}` across lines.

Remove inline margin styles from the profanity settings hierarchy. Inline styles violate the application's Content Security Policy.

Use these classes:

```text
censor-option-group
censor-option-group-manual
censor-method-group
```

Apply them to:

- Manual-upload censor checkbox wrapper.
- Manual-upload censor-method wrapper.
- YouTube-source censor checkbox/method wrappers.
- Podcast-source censor checkbox/method wrappers.

File:

```text
src/frontend/static/app/dashboard.css
```

Add:

```css
.profanity-status {
  display: flex;
  gap: 4px;
}
```

File:

```text
src/frontend/static/app/settings.css
```

Add:

```css
.censor-option-group,
.censor-method-group {
  margin-left: 24px;
  margin-top: 8px;
}

.censor-option-group-manual {
  margin-top: -8px;
}
```

This replaces inline styles and prevents CSP console errors.

File:

```text
tests/test_frontend_proxy.py
```

Import:

```python
from django.template.loader import get_template
```

Add a unittest that compiles:

```text
app/library.html
app/settings.html
registration/login.html
```

Suggested structure:

```python
def test_browser_templates_compile(self):
    for template_name in (
        "app/library.html",
        "app/settings.html",
        "registration/login.html",
    ):
        with self.subTest(template_name=template_name):
            get_template(template_name)
```

This is the regression test for the actual 500.

## 2. Add a real censor job type and routing

File:

```text
src/models/domain.py
```

Add to `JobType`:

```python
CENSOR_PROFANITY = "censor_profanity"
```

File:

```text
src/frontend/routing.py
```

Route both transcoding and censoring to the FFmpeg queue:

```python
if parsed_job_type in {
    JobType.TRANSCODE_MEDIA,
    JobType.CENSOR_PROFANITY,
}:
    return FFMPEG_QUEUE
```

File:

```text
src/workers/runner.py
```

Allow the FFmpeg worker to claim both job types:

```python
"ffmpeg": {
    JobType.TRANSCODE_MEDIA,
    JobType.CENSOR_PROFANITY,
}
```

File:

```text
src/workers/scheduler.py
```

This step is easy to miss and is essential.

Add censor jobs to the shared heavy-work scheduler:

```python
HEAVY_JOB_TYPES = {
    JobType.TRANSCODE_MEDIA: HeavyJobKind.FFMPEG,
    JobType.CENSOR_PROFANITY: HeavyJobKind.FFMPEG,
    JobType.GENERATE_TRANSCRIPT: HeavyJobKind.TRANSCRIPT,
}
```

Without this mapping the job reaches the FFmpeg worker but bypasses the global CPU-slot scheduler.

File:

```text
tests/test_coverage_worker_runtime.py
```

Import `HEAVY_JOB_TYPES` and assert:

```python
self.assertEqual(
    HEAVY_JOB_TYPES[JobType.CENSOR_PROFANITY],
    HeavyJobKind.FFMPEG,
)
```

Keep Ruff's preferred import order: `HEAVY_JOB_TYPES` sorts before the class names in the grouped import.

## 3. Fix profanity match collection

File:

```text
src/workers/content_filter.py
```

The original `find_explicit_content()` returned only the first match. Censoring multiple portions of an audio file requires all profane sentences.

Refactor:

```python
def find_explicit_content(text: str) -> ExplicitContentMatch | None:
    matches = find_explicit_content_matches(text)
    return matches[0] if matches else None
```

Add:

```python
def find_explicit_content_matches(
    text: str,
) -> list[ExplicitContentMatch]:
```

Behavior:

- Normalize/strip input.
- Split into the same sentences as the existing implementation.
- Run `_predict_profanity(sentences)`.
- Build and return one `ExplicitContentMatch` for every profane prediction.
- Return an empty list for empty input or no matches.
- Preserve the legacy first-match behavior through `find_explicit_content()`.

Add:

```python
def screen_transcript_matches(
    subtitle_path: Path | None,
) -> list[ExplicitContentMatch]:
    if subtitle_path is None or not Path(subtitle_path).exists():
        return []
    return find_explicit_content_matches(
        transcript_text(Path(subtitle_path))
    )
```

Important compatibility detail:

- Existing tests patch `screen_transcript`.
- Continue calling `screen_transcript()` first in worker behavior.
- Only call `screen_transcript_matches()` when censoring is enabled and an explicit match was already found.
- Fall back to the original first match if the list function unexpectedly returns no entries.

## 4. Fix SRT parsing and FFmpeg filter generation

File:

```text
src/workers/censor.py
```

### A. Replace the regex-only SRT block parser

The branch parser could mishandle empty cues and subsequent valid cues.

Implement `_extract_srt_blocks()` as a small state/line parser returning:

```python
list[tuple[str, str, str, str]]
```

Each tuple is:

- Index.
- Start timestamp.
- End timestamp.
- Normalized text.

Required behavior:

- Normalize CRLF and CR to LF.
- Recognize numeric cue indices.
- Recognize timestamps like:
  - `HH:MM:SS,mmm --> HH:MM:SS,mmm`
  - `HH:MM:SS.mmm --> HH:MM:SS.mmm`
- Permit optional trailing SRT cue settings after the end timestamp.
- Skip empty cues.
- Do not allow an empty cue to consume the next valid cue.
- Join multiline cue text with spaces.

Only catch `OSError` around reading the subtitle file. The parser itself should not be wrapped in a broad exception handler.

### B. Improve sentence-to-cue matching

The old implementation required the profane sentence and cue text to be exactly equal.

Normalize whitespace and lowercase both values, then treat a match as:

```python
normalized_profane_text in normalized_block_text
```

This permits a detected profane sentence or phrase to match a larger subtitle cue.

### C. Replace the invalid beep graph

The feature branch used an `atone` concept that is not a valid FFmpeg source filter and referenced a `[beep]` stream that was never correctly generated.

Build the beep filter using FFmpeg's `sine` source.

For merged profanity segments, generate an enable expression such as:

```text
between(t,1.000,2.000)+between(t,4.000,5.000)
```

Return a complete filter graph shaped like:

```text
[0:a]volume=0:enable='<conditions>'[muted];
sine=frequency=<frequency>:sample_rate=48000,
volume=<amplitude>:enable='<conditions>'[beep];
[muted][beep]amix=inputs=2:duration=first:normalize=0[outa]
```

The string should be contiguous when passed to FFmpeg.

Defaults remain:

- Frequency: 1000 Hz.
- Amplitude: existing function default.
- Output audio label: `[outa]`.

For the duck/mute method, preserve the existing simple audio filter output. The handler will wrap it as:

```text
[0:a]<duck-filter>[outa]
```

Update tests in `tests/test_censor.py` to assert:

```text
"sine=frequency=1000"
"sine=frequency=2000"
"volume=0.8"
"amix"
```

Do not assert `atone` or `a=...`.

## 5. Save and propagate censor settings

File:

```text
src/api/services/dashboard_actions.py
```

### A. Extend `SourceFormData`

Add:

```python
censor_profanity: bool
censor_method: str
```

### B. Add profile defaults

Add to `PROFILE_DEFAULTS`:

```python
"manual_upload_censor_profanity": "0"
"manual_upload_censor_method": "duck"
```

### C. Validate the method

Add to `CONFIG_ENUM_RULES`:

```python
"manual_upload_censor_method": {"duck", "beep"}
```

In source-form validation, reject methods outside `duck`/`beep`.

The previous implementation's validation message was:

```text
"manual_upload_censor_method is invalid"
```

### D. Parse source form values

The existing branch templates use these form names for each source:

```text
source_<id>__manual_upload_censor_profanity
source_<id>__manual_upload_censor_method
```

Parse them into the new `SourceFormData` fields.

Normalize method with:

- Default `"duck"`.
- `.strip()`.
- `.lower()`.

### E. Save source fields

Include these model fields in update/create behavior:

```text
manual_upload_censor_profanity
manual_upload_censor_method
```

Add them to:

- `_source_update_fields()`.
- `_apply_source_form_data()`.
- `add_source()`.

### F. Propagate manual-upload settings

At the beginning of `manual_upload()`, load:

```python
settings = _profile_settings(profile_id)
```

When creating the transcript job, include:

```python
"delete_explicit_content":
    _checked(settings, "manual_upload_delete_explicit_content")

"censor_profanity":
    _checked(settings, "manual_upload_censor_profanity")

"censor_method":
    settings.get("manual_upload_censor_method", "duck")
```

This ensures the UI settings affect actual manual-upload jobs.

## 6. Propagate settings through the worker pipeline

File:

```text
src/workers/handlers.py
```

Clean up duplicate imports first. The branch had repeated imports for `DownloadStatus`, `JobStatus`, `SourceType`, and `ProfanityStatus`.

Use one grouped domain import and add:

```python
from workers.censor import (
    AudioSegment,
    build_censor_filter,
    extract_profanity_segments,
)
```

Also import:

```python
screen_transcript_matches
```

Propagate these payload fields through every relevant stage:

```text
"censor_profanity"
"censor_method"
```

Required propagation points include:

1. `check_for_episodes()`
   - Read from:
     - `source.manual_upload_censor_profanity`
     - `source.manual_upload_censor_method`
   - Put both into discovered download job payloads.

2. `download_episode()`
   - Preserve both fields in transcode and transcript child payloads.

3. `_download_with_yt_dlp()`
   - Preserve both fields in `transcode_payload`.
   - Do not create a video `Download` row before screening when either `delete_explicit_content` or `censor_profanity` is true.
   - For compatible video files that do not need transcoding, call the deferred screening path when either flag is true.

4. `_postprocess_download_with_ffmpeg()`
   - For deferred video insertion, trigger transcript/screening when either flag is true.
   - Propagate method and censor flag.

5. `transcode_media()`
   - Propagate both fields to the generated transcript job.

6. Any manual-upload transcript job
   - Already receives values from `dashboard_actions.py`.

## 7. Implement the durable censor worker

File:

```text
src/workers/handlers.py
```

Add the following helpers.

### A. `_should_censor_profanity()`

Signature:

```python
def _should_censor_profanity(
    *,
    source_type: str,
    source_name: str,
    profile_id: str,
) -> tuple[bool, str]:
```

Query `SourceConfig` for the matching:

- `profile_id`
- `source_type`
- `name`

Read:

- `manual_upload_censor_profanity`
- `manual_upload_censor_method`

Return:

- `(False, "duck")` if there is no matching source.
- Enabled as a boolean.
- Method normalized to `duck` or `beep`.
- Fall back to `duck` for invalid stored values.

This helper is primarily retained for the branch's feature tests.

### B. `_queue_audio_censoring_job()`

Inputs:

```text
profile_id
media_path
subtitle_path
profane_sentences
censor_method
optional download_id
optional download_lookup
optional download_defaults
```

Behavior:

1. Call `extract_profanity_segments()`.
2. Normalize method to `duck` or `beep`, defaulting to `duck`.
3. Call `build_censor_filter()` as an early validation.
4. Return `None` if there are no segments or no usable filter.
5. Create a `censor_profanity` job with payload containing:
   - `download_id`
   - `media_path`
   - `subtitle_path`
   - `censor_method`
   - `censor_filter`
   - `censored_segments`
6. Serialize segments with `dataclasses.asdict()`.
7. Include deferred `download_lookup` and `download_defaults` when supplied.
8. Use an idempotency key:

   ```text
   censor_profanity:<profile_id>:download:<download_id>
   ```

   when a download exists.

9. For deferred media without a Download ID, create a deterministic identity from SHA-1 of the media path:

   ```python
   hashlib.sha1(
       str(media_path).encode("utf-8"),
       usedforsecurity=False,
   ).hexdigest()
   ```

Security note:

- The serialized `censor_filter` can remain in the job payload for diagnostics.
- The executing handler must not trust or execute that serialized filter.
- Rebuild the FFmpeg filter from validated serialized segments.

### C. `_censor_segments_from_payload()`

Convert `payload["censored_segments"]` into `AudioSegment` instances.

Validation:

- Payload must be a list.
- Each item must be a dictionary.
- Start/end must parse as floats.
- Start must be nonnegative.
- End must be greater than start.
- Invalid entries are skipped.
- Text is converted to a string.

### D. `_censor_audio_codec_args()`

Return codec arguments based on the existing file suffix.

For `.mp3`:

```text
-c:a libmp3lame -q:a <profile audio_quality>
```

For `.opus` or `.webm`:

```text
-c:a libopus -b:a 96k
```

For other formats:

```text
-c:a aac -b:a 192k
```

### E. `censor_profanity(job)`

This is the actual FFmpeg worker handler.

Required behavior:

1. Read a dictionary payload safely.

2. Load the Download by:
   - Primary key from `download_id`.
   - Matching `job.profile_id`.

3. Resolve `media_path`.

4. Resolve the profile output root with `_download_output_root()`.

5. Reject media outside the profile output root using `Path.relative_to()`.

   Raise:

   ```text
   ValueError("Censor input is outside the profile output root")
   ```

6. Require an existing file.

7. Normalize and validate `censor_method`.

   Only allow:
   - `duck`
   - `beep`

8. Rebuild segments with `_censor_segments_from_payload()`.

9. Rebuild the filter with `build_censor_filter()`.

10. Reject jobs with no valid segments/filter.

11. Write to a hidden same-directory temporary file:

    ```text
    .<stem>.censoring<suffix>
    ```

    Example:

    ```text
    song.mp3
    .song.censoring.mp3
    ```

    This ensures the final rename is atomic on the same filesystem.

12. Build the FFmpeg command approximately as:

    ```text
    ffmpeg
    -y
    -i <media_path>
    -filter_complex <filter>
    -map 0:v:0?
    -map [outa]
    -map 0:s?
    -c:v copy
    -c:s mov_text
    ...audio codec args...
    <temporary_path>
    ```

    For non-MP4 subtitles, use:

    ```text
    -c:s copy
    ```

13. For duck mode, wrap the simple filter as:

    ```text
    [0:a]<duck-filter>[outa]
    ```

    For beep mode, the graph already produces `[outa]`.

14. Call:

    ```python
    _touch_active_job(job, stage="profanity_censor")
    ```

15. Execute with:

    ```python
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    ```

16. Verify the temporary output exists and has nonzero size.

17. Atomically replace the original:

    ```python
    temporary_path.replace(media_path)
    ```

18. On any exception:
    - Delete the temporary output with `missing_ok=True`.
    - Re-raise the exception.
    - Do not remove or replace the original input.

19. For an existing Download:
    - Update `file_size_bytes`.
    - Set `profanity_status = ProfanityStatus.CENSORED`.
    - Update `last_seen_at`.
    - Save only those fields.

20. For a deferred video without an existing Download:
    - Require dictionary `download_lookup`.
    - Require dictionary `download_defaults`.
    - Create/update the Download after successful censoring.
    - Set:
      - `file_path`
      - `file_size_bytes`
      - `file_ext`
      - `download_status = DownloadStatus.DOWNLOADED`
      - `profanity_status = ProfanityStatus.CENSORED`
      - `completed_at`
      - `last_seen_at`
    - Create `TranscriptSegment` rows from the subtitle file.

21. Register the handler:

    ```python
    HANDLERS["censor_profanity"] = censor_profanity
    ```

## 8. Connect transcript screening to censoring

File:

```text
src/workers/handlers.py
```

Update both normal and deferred transcript paths.

### A. Screening conditions

Run explicit-content screening when either is true:

```text
delete_explicit_content
censor_profanity
```

Do not screen only on the delete flag.

### B. Existing Download path in `generate_transcript()`

When a profane match is found and censoring is enabled:

1. Load all matches with `screen_transcript_matches()`.
2. Fall back to the original match when needed.
3. Queue a censor job using:
   - Profile ID.
   - Media path.
   - Subtitle path.
   - All nonempty matched sentences.
   - Download ID.
   - Selected method.
4. If a censor job was created:
   - Set the Download's interim status to `ProfanityStatus.UNCENSORED`.
   - Update `last_seen_at`.
   - Publish the child job.
   - Return without deleting media.

If no censor job can be created, retain the existing delete/filter behavior.

When deleting explicit media:

- Continue setting `download_status = DownloadStatus.FILTERED`.
- Also set `profanity_status = ProfanityStatus.UNCENSORED`.

When screening succeeds with no profanity:

- Set `profanity_status = ProfanityStatus.CLEAN`.

### C. Deferred video path

For a profane match and censoring enabled:

1. Build deferred `download_defaults`.
2. Include:
   - `subtitle_path`
   - `profanity_status = ProfanityStatus.UNCENSORED`
3. Queue a censor job with:
   - `download_lookup`
   - `download_defaults`
4. Publish the censor child.
5. Return without inserting the Download row yet.

The censor worker creates the row only after FFmpeg succeeds.

### D. Transcript-generation failure

If transcript generation fails before required screening and either deletion or censoring was enabled:

- Retain the safety behavior of deleting the media.
- Set:
  - `download_status = DownloadStatus.FILTERED`
  - `profanity_status = ProfanityStatus.UNCENSORED`

This avoids retaining unscreened content when screening was explicitly required.

## 9. Model and import cleanup

File:

```text
src/models/models.py
```

The feature branch introduced duplicate domain imports.

Use one import:

```python
from .domain import DownloadStatus, JobStatus, ProfanityStatus
```

File:

```text
src/api/services/library.py
```

This was only Ruff import ordering:

```python
from models.domain import DownloadStatus, ProfanityStatus, parse_str_enum
```

Do not make behavioral changes there unless current code differs.

## 10. Test corrections

File:

```text
tests/test_audio_censoring_integration.py
```

The branch test was not set up as a valid Django test module and referenced the wrong enum.

Required setup before importing models/handlers:

```python
os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")

import django

django.setup()
```

Use:

```python
from models.domain import ProfanityStatus
```

Do not use a nonexistent:

```python
DownloadStatus.CENSORED
```

Assert:

```python
ProfanityStatus.CENSORED == "censored"
ProfanityStatus.UNCENSORED == "uncensored"
```

Remove stale or unused imports such as:

- `Download`
- `SourceConfig`
- `ExplicitContentMatch`
- Unnecessary patch objects.

Update the `_queue_audio_censoring_job` test so it patches `create_job` but not a nonexistent/unneeded `_profile_setting` interaction.

File:

```text
tests/test_censor.py
```

Update beep expectations from the invalid `atone` syntax to:

```text
sine=frequency=
volume=
amix
```

Clean up unused variables/imports as required by Ruff.

File:

```text
tests/test_coverage_worker_runtime.py
```

Add the heavy-job scheduler assertion described above.

File:

```text
tests/test_frontend_proxy.py
```

Add the template compilation regression described above.

File:

```text
tests/test_integration_pipeline_helpers.py
```

The current Compose configuration uses an absolute API entrypoint command.

Change the expected value from:

```text
api-entrypoint.sh
```

to:

```text
/usr/local/bin/api-entrypoint.sh
```

This was an existing branch-test mismatch exposed by the full suite.

## 11. Verification already performed on the original workstation

### A. Login diagnosis

Observed before the fix:

- Login page GET provided CSRF cookie/token.
- Browser-style login POST returned HTTP 302 and session cookie.
- Authenticated API library request returned HTTP 200.
- Redirected library page returned HTTP 500.
- Container-side template compilation produced:

  ```text
  TemplateSyntaxError:
  Could not parse the remainder: '=='unplayed''
  ```

Observed after the template fix:

- `/` returned HTTP 200.
- `/settings/` returned HTTP 200.
- Login remained CSRF protected.

### B. Playwright/browser verification

Playwright was run against the local Compose frontend.

Final result:

```text
loginStatus: 200
finalUrl: http://127.0.0.1:8080/settings/
libraryStatus: 200
settingsStatus: 200
manual profanity checkbox count: 1
manual censor method count: 1
selected manual method: duck
consoleErrors: []
```

An earlier Playwright pass found two Content Security Policy errors caused by inline styles. Replacing inline styles with CSS classes removed both errors.

The local profile had no YouTube sources, so source-specific YouTube controls could not be visually counted in that profile. The template/tests still cover their rendering syntax.

### C. Real FFmpeg verification

Real FFmpeg was used, not only mocked tests.

A three-second input file was generated.

Both graphs produced valid audio:

- Duck/mute graph.
- Beep graph using the `sine` source.

The exact handler-shaped MP3 commands were also tested, including:

- Optional video/subtitle mappings.
- `[outa]` mapping.
- Audio codec selection.
- Same-directory hidden temporary naming.

`ffprobe` confirmed valid outputs.

Observed examples:

```text
duck:
  duration 3.030204
  size 21296 bytes

beep:
  duration 3.030204
  size 27713 bytes
```

### D. Container verification

Successfully built:

- `frontend`
- `api`
- `worker-ffmpeg`
- `worker-transcripts`

Successfully recreated and observed running/healthy:

- `frontend`
- `api`
- Three FFmpeg workers.
- Three transcript workers.

### E. Test results

Focused feature/runtime tests:

- 53 focused tests passed during implementation.
- The final scheduler-focused module ran 12 tests successfully.

Full requested checks:

```text
make test-coverage
  203 tests passed
  overall coverage: 80%

make test-mypy
  Success: no issues found in 92 source files

make test-ruff
  All checks passed

make test-compile
  Passed

git diff --check
  Passed
```

## 12. What was not fully verified

Be precise in the final report.

The following were not run as one complete production-shaped end-to-end flow:

- The full `make integration-test`.
- The network-dependent podcast integration scenario.
- A complete media upload/download -> real Whisper inference -> profanity detection -> RabbitMQ censor job -> FFmpeg replacement workflow.
- The authenticated Wapiti scan included in `make test`.

Why:

- The podcast integration requires network access.
- Real Whisper inference/model loading is comparatively expensive.
- The user explicitly requested coverage and mypy; those were completed.
- The critical boundaries were validated independently:
  - Settings and template rendering.
  - Payload propagation tests.
  - Profanity segment extraction tests.
  - Job routing.
  - CPU-slot scheduling.
  - Worker handler construction.
  - Real FFmpeg execution.
  - Browser authentication and CSP behavior.

Do not claim that the full Whisper/Compose pipeline passed unless you run it on this workstation.

## 13. Required workstation procedure

1. Read `AGENTS.md`.

2. Inspect state:

   ```bash
   git branch --show-current
   git rev-parse --short HEAD
   git status --short
   ```

3. Confirm the intended branch:

   ```text
   296-audio-profanity-censoring-with-ffmpeg-and-whisper
   ```

4. Compare carefully with the remote/current branch and with:

   ```text
   refs/heads/main
   ```

5. Determine which fixes above are absent.

6. Implement only the absent fixes.

7. Do not add `@csrf_exempt`.

8. Run the smallest focused tests while editing.

9. Run:

   ```bash
   make test-compile
   make test-ruff
   make test-mypy
   make test-coverage
   git diff --check
   ```

10. Rebuild the affected Compose services:

    - `frontend`
    - `api`
    - `worker-ffmpeg`
    - `worker-transcripts`

11. Verify:

    - Login succeeds.
    - Library returns 200.
    - Settings returns 200.
    - Browser console has no CSP/template errors.
    - Manual profanity checkbox and censor-method dropdown render.
    - If sources exist, verify YouTube/podcast source censor controls.
    - FFmpeg duck and beep graphs generate probeable output.
    - Censor jobs route to the FFmpeg queue.
    - Censor jobs use `HeavyJobKind.FFMPEG`.

12. If resources permit, run the YouTube-shaped deterministic Compose integration test. Only run the podcast integration if network access is available and appropriate.

13. Review:

    ```bash
    git diff --check
    git diff --stat
    git diff
    git status --short
    ```

14. Ensure no secret/generated files are staged.

15. Prepare a focused commit. The commit should explain:

    - The 500 was caused by invalid Django template comparisons.
    - CSRF protection was retained.
    - Censor settings now propagate through the worker pipeline.
    - FFmpeg beep/mute processing now uses a valid filter graph.
    - Censor work is routed and CPU-slot scheduled.
    - Atomic replacement protects existing media.
    - Regression tests were added/fixed.

16. Do not push until explicitly authorized by the user.

## Expected final report

Report:

- Current branch and starting commit.
- Exact files changed.
- Confirmation that no `csrf_exempt` was added.
- Login/library/settings browser results.
- Whether Playwright reported console errors.
- Whether a real FFmpeg output was generated and probed.
- Focused and full test results.
- Whether full Compose integration/Whisper inference was run.
- Any remaining risks.
- Commit hash, if a commit was authorized and created.
- Confirmation that no secrets/generated artifacts were committed.
