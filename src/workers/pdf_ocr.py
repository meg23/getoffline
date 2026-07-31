"""PDF text extraction with OCR fallback for scanned pages."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

MIN_NATIVE_TEXT_LENGTH = 40
OCR_DPI = 200
OCR_PAGE_TIMEOUT_SECONDS = 180
MAX_SENTENCE_CHARS = 800

# Keep sentence boundaries inside the searchable text rather than returning
# one page-sized result. The lookahead keeps closing quotation marks with the
# following boundary and avoids splitting on punctuation followed by a word
# that does not look like a new sentence.
_SENTENCE_BOUNDARY_RE = re.compile(
    r'(?<=[.!?])(?=(?:["\'”’»)]*\s+)(?:["\'“‘«(\[]*[A-Z0-9]))'
)


@dataclass(frozen=True)
class PdfOcrPage:
    """Searchable text extracted from one PDF page."""

    page_number: int
    text: str
    used_ocr: bool


def _clean_text(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


_HARD_LINE_START_RE = re.compile(r"^(?:[-*•▪◦‣]|\d+[.)]|[A-Z][.)])\s+")


def split_sentences(text: str) -> list[str]:
    """Split extracted PDF text into useful, searchable sentence records."""
    cleaned_text = _clean_text(text)
    if not cleaned_text:
        return []

    lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]
    line_groups: list[str] = []
    current_lines: list[str] = []
    current_length = 0
    for line in lines:
        starts_hard_boundary = bool(_HARD_LINE_START_RE.match(line))
        would_exceed_limit = (
            current_lines and current_length + len(line) + 1 > MAX_SENTENCE_CHARS
        )
        if current_lines and (starts_hard_boundary or would_exceed_limit):
            line_groups.append(" ".join(current_lines))
            current_lines = []
            current_length = 0
        current_lines.append(line)
        current_length += len(line) + (1 if current_length else 0)
        if line.endswith((".", "!", "?")):
            line_groups.append(" ".join(current_lines))
            current_lines = []
            current_length = 0
    if current_lines:
        line_groups.append(" ".join(current_lines))

    sentences: list[str] = []
    for line_group in line_groups:
        sentences.extend(
            sentence.strip()
            for sentence in _SENTENCE_BOUNDARY_RE.split(line_group)
            if sentence.strip()
        )
    return sentences


def _ocr_page(pdf_path: Path, page_number: int) -> str:
    """Render one page and run Tesseract without invoking a shell."""
    with tempfile.TemporaryDirectory(prefix="getoffline-pdf-ocr-") as temp_dir:
        prefix = Path(temp_dir) / "page"
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-r",
                str(OCR_DPI),
                "-png",
                "-singlefile",
                str(pdf_path),
                str(prefix),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=OCR_PAGE_TIMEOUT_SECONDS,
        )
        image_path = prefix.with_suffix(".png")
        result = subprocess.run(
            ["tesseract", str(image_path), "stdout", "--psm", "3"],
            check=True,
            capture_output=True,
            text=True,
            timeout=OCR_PAGE_TIMEOUT_SECONDS,
        )
    return _clean_text(result.stdout)


def extract_pdf_pages(pdf_path: Path) -> list[PdfOcrPage]:
    """Extract native PDF text and OCR pages with little or no text."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - worker image dependency
        raise RuntimeError("PDF OCR requires the pypdf package") from exc

    reader = PdfReader(str(pdf_path), strict=False)
    pages: list[PdfOcrPage] = []
    for page_number, page in enumerate(reader.pages, start=1):
        native_text = _clean_text(page.extract_text())
        if len(native_text) >= MIN_NATIVE_TEXT_LENGTH:
            pages.append(PdfOcrPage(page_number, native_text, used_ocr=False))
            continue
        ocr_text = _ocr_page(pdf_path, page_number)
        text = ocr_text or native_text
        if text:
            pages.append(PdfOcrPage(page_number, text, used_ocr=bool(ocr_text)))
    return pages
