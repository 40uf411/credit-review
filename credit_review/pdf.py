"""PDF ingestion: hash, text layer as visual lines with positions, page images.

Text is rebuilt line by line from word positions (like `pdftotext -layout`), so a
label and its value on the same visual line stay together. Every line keeps its
page, line number and bounding box: that is what evidence points to.

Documents without a text layer (scans) are rendered to images for a vision model.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import cached_property
from pathlib import Path
from typing import Literal

import pymupdf

MIN_WORDS_FOR_TEXT_LAYER = 20
RENDER_DPI = 150
MAX_IMAGE_PAGES = 4
MODEL_IMAGE_MAX_PX = 1800      # long edge sent to vision models (API limits; small text stays legible)

Located = Literal["exact", "fuzzy", "image", "not_found", "none"]


@dataclass
class Line:
    page: int                     # 1-based
    number: int                   # 1-based within the page
    text: str
    bbox: tuple[float, float, float, float]
    words: list[tuple[float, float, float, float, str]]


@dataclass
class Span:
    """Where a quote was found."""
    located: Located
    page: int | None = None
    lines: list[int] = field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None
    score: float | None = None
    matched_text: str = ""        # the document text the quote was matched to (normalised)


@dataclass
class PdfDocument:
    path: Path
    sha256: str
    page_count: int
    page_sizes: list[tuple[float, float]]
    lines: list[Line]

    @property
    def has_text_layer(self) -> bool:
        return sum(len(line.words) for line in self.lines) >= MIN_WORDS_FOR_TEXT_LAYER

    @property
    def name(self) -> str:
        return self.path.name

    def page_text(self, page: int) -> str:
        return "\n".join(line.text for line in self.lines if line.page == page)

    def text_for_model(self) -> str:
        """Text sent to the model: page markers, no line numbers (quotes must stay verbatim)."""
        parts = []
        for page in range(1, self.page_count + 1):
            parts.append(f"=== Page {page} ===")
            parts.append(self.page_text(page))
        return "\n".join(parts)

    def first_page_text(self, max_chars: int = 2500) -> str:
        return self.page_text(1)[:max_chars]

    # -- images -------------------------------------------------------------
    @staticmethod
    def _zoom(page: "pymupdf.Page", dpi: int, max_px: int | None) -> "pymupdf.Matrix":
        zoom = dpi / 72
        if max_px:
            zoom = min(zoom, max_px / max(page.rect.width, page.rect.height))
        return pymupdf.Matrix(zoom, zoom)

    def page_png(self, page: int, dpi: int = RENDER_DPI, max_px: int | None = 2000) -> bytes:
        with pymupdf.open(self.path) as doc:
            p = doc[page - 1]
            return p.get_pixmap(matrix=self._zoom(p, dpi, max_px)).tobytes("png")

    def images_for_model(self, max_px: int = MODEL_IMAGE_MAX_PX) -> list[bytes]:
        """JPEG page images, long edge capped, deterministic for a given PyMuPDF version."""
        out = []
        with pymupdf.open(self.path) as doc:
            for p in list(doc)[:MAX_IMAGE_PAGES]:
                pix = p.get_pixmap(matrix=self._zoom(p, 200, max_px))
                out.append(pix.tobytes("jpg", jpg_quality=85))
        return out

    def highlight_png(self, span: Span, dpi: int = RENDER_DPI) -> bytes | None:
        """Render the page of a span with the evidence box highlighted."""
        if not span.page:
            return None
        with pymupdf.open(self.path) as doc:
            page = doc[span.page - 1]
            if span.bbox:
                annot = page.add_highlight_annot(pymupdf.Rect(*span.bbox))
                annot.update()
            return page.get_pixmap(matrix=self._zoom(page, dpi, 2000)).tobytes("png")

    # -- quote location -------------------------------------------------------
    @cached_property
    def _index(self) -> list[tuple[int, str, list[int]]]:
        """Per page: normalised text and, for each character, the index of its line."""
        out = []
        for page in range(1, self.page_count + 1):
            chars: list[str] = []
            owner: list[int] = []
            for idx, line in enumerate(self.lines):
                if line.page != page:
                    continue
                norm = normalise_for_search(line.text)
                if not norm:
                    continue
                if chars:
                    chars.append(" ")
                    owner.append(idx)
                chars.extend(norm)
                owner.extend([idx] * len(norm))
            out.append((page, "".join(chars), owner))
        return out

    def locate(self, quote: str | None, page_hint: int | None = None,
               fuzzy_threshold: float = 0.85) -> Span:
        """Find a model-supplied quote in the text layer. Exact first, then fuzzy."""
        if not quote or not quote.strip():
            return Span(located="none")
        if not self.has_text_layer:
            return Span(located="image", page=page_hint if page_hint else None)
        needle = normalise_for_search(quote)
        pages = sorted(self._index, key=lambda e: e[0] != page_hint)
        for page, hay, owner in pages:
            pos = hay.find(needle)
            if pos >= 0:
                return self._span(page, owner, pos, pos + len(needle), "exact", 1.0)
        best: tuple[float, int, int, int, list[int]] | None = None
        for page, hay, owner in pages:
            score, start, end = _best_window(needle, hay)
            if best is None or score > best[0]:
                best = (score, page, start, end, owner)
        if best and best[0] >= fuzzy_threshold:
            score, page, start, end, owner = best
            return self._span(page, owner, start, end, "fuzzy", round(score, 3))
        return Span(located="not_found", score=round(best[0], 3) if best else None)

    def _span(self, page: int, owner: list[int], start: int, end: int,
              located: Located, score: float) -> Span:
        idxs = sorted(set(owner[start:max(end, start + 1)]))
        lines = [self.lines[i] for i in idxs]
        x0 = min(line.bbox[0] for line in lines)
        y0 = min(line.bbox[1] for line in lines)
        x1 = max(line.bbox[2] for line in lines)
        y1 = max(line.bbox[3] for line in lines)
        hay = next(h for p, h, _ in self._index if p == page)
        return Span(located=located, page=page, lines=[line.number for line in lines],
                    bbox=(x0, y0, x1, y1), score=score, matched_text=hay[start:end])


def normalise_for_search(text: str) -> str:
    text = text.replace(" ", " ").casefold()
    text = re.sub(r"\s*-\s*", "-", text)          # "PROCES -VERBAAL" == "proces-verbaal"
    return re.sub(r"\s+", " ", text).strip()


def _best_window(needle: str, hay: str) -> tuple[float, int, int]:
    """Best fuzzy match of needle against same-length windows of hay."""
    n = len(needle)
    if n == 0 or not hay:
        return 0.0, 0, 0
    best = (0.0, 0, 0)
    step = max(1, n // 8)
    for start in range(0, max(1, len(hay) - n + 1), step):
        window = hay[start:start + n]
        score = SequenceMatcher(None, needle, window, autojunk=False).ratio()
        if score > best[0]:
            best = (score, start, start + n)
    return best


def _visual_lines(page: pymupdf.Page, page_no: int) -> list[Line]:
    words = [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words") if w[4].strip()]
    if not words:
        return []
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows: list[list[tuple[float, float, float, float, str]]] = []
    for w in words:
        centre, height = (w[1] + w[3]) / 2, max(w[3] - w[1], 1.0)
        if rows:
            last = rows[-1]
            last_centre = sum((x[1] + x[3]) / 2 for x in last) / len(last)
            if abs(centre - last_centre) <= 0.45 * height:
                last.append(w)
                continue
        rows.append([w])
    lines = []
    for number, row in enumerate(rows, start=1):
        row.sort(key=lambda w: w[0])
        text = " ".join(w[4] for w in row)
        bbox = (min(w[0] for w in row), min(w[1] for w in row),
                max(w[2] for w in row), max(w[3] for w in row))
        lines.append(Line(page=page_no, number=number, text=text, bbox=bbox, words=row))
    return lines


def load_pdf(path: str | Path) -> PdfDocument:
    path = Path(path)
    data = path.read_bytes()
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        lines: list[Line] = []
        for i, page in enumerate(doc, start=1):
            lines.extend(_visual_lines(page, i))
        sizes = [(p.rect.width, p.rect.height) for p in doc]
        return PdfDocument(path=path, sha256=hashlib.sha256(data).hexdigest(),
                           page_count=doc.page_count, page_sizes=sizes, lines=lines)


def list_dossier(folder: str | Path) -> list[PdfDocument]:
    folder = Path(folder)
    return [load_pdf(p) for p in sorted(folder.iterdir())
            if p.suffix.lower() == ".pdf" and not p.name.startswith(".")]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
