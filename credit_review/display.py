"""Showing text that came from a document or a model on a page: as text, never as markup, links or images."""

from __future__ import annotations

import html
import re
from pathlib import Path


def esc(text: object) -> str:
    """For HTML a page builds itself: no markup gets through, and no blank line can end the HTML block."""
    return html.escape(str(text)).replace("\n", "<br>")


def md(text: object) -> str:
    """For Markdown: every character that could start a link, an image, a heading or emphasis is escaped."""
    return re.sub(r"([\\`*_{}\[\]()<>#+!|~-])", r"\\\1", str(text))


def safe_name(name: str) -> str:
    """An uploaded file's name, reduced to a plain PDF file name: no folders, no unusual characters."""
    stem = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name).name).strip(" .")[:100]
    return stem if stem.lower().endswith(".pdf") else (stem or "document") + ".pdf"
