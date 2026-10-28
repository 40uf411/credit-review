"""Build the search index from the corpus manifest.

    python -m assistant.ingest --dry-run      # download, check, chunk; print the report; touch nothing in Azure
    python -m assistant.ingest                # also scan with Prompt Shields, embed, and rebuild the index

For each document: download → sha256 check against the manifest (a pinned hash that no longer matches stops the run)
→ text → chunks of a few paragraphs, starting a new chunk at each article heading → Prompt Shields document scan
(a flagged chunk is quarantined and reported, never indexed) → embeddings → index.

A document that cannot be downloaded is reported and left out; the run fails only if no document is left. The report
is Markdown, so the workflow puts it in its job summary.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
import yaml

from assistant.models import Chunk
from assistant.shields import MAX_CHARS as SHIELDS_MAX_CHARS, MAX_DOCUMENTS as SHIELDS_MAX_DOCUMENTS

MANIFEST = Path(__file__).with_name("corpus") / "manifest.yaml"
CHUNK_CHARS = 1500
MIN_LINE_CHARS = 25          # shorter lines (menus, page furniture) are dropped from HTML pages
DIMENSIONS = 3072            # text-embedding-3-large
HEADING = re.compile(r"^(article|artikel|art\.|section|chapter|hoofdstuk|chapitre|titre)\s+\w+", re.IGNORECASE)


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    issuer: str
    country: str
    language: str
    effective: str
    url: str                      # where the document is published (provenance; fetched unless `file` is set)
    format: str
    licence: str
    sha256: str | None = None
    file: str | None = None       # a copy committed under corpus/files/, for sites that refuse automated downloads


@dataclass
class Report:
    rows: list[dict[str, Any]] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    indexed: int | None = None
    error: str = ""

    def markdown(self) -> str:
        lines = ["### Corpus ingestion", "", "| Document | Status | Chunks | sha256 |", "|---|---|---|---|"]
        for r in self.rows:
            lines.append(f"| `{r['id']}` | {r['status']} | {r.get('chunks', '—')} | `{r.get('sha256', '—')}` |")
        if self.quarantined:
            lines += ["", f"**Quarantined by Prompt Shields ({len(self.quarantined)}):** "
                      + ", ".join(f"`{q}`" for q in self.quarantined)]
        if self.indexed is not None:
            lines += ["", f"**Indexed {self.indexed} chunks.**"]
        if self.error:
            lines += ["", f"**Failed:** {self.error}"]
        unpinned = [r for r in self.rows if r.get("pinned") is False]
        if unpinned:
            lines += ["", "Pin these hashes in `assistant/corpus/manifest.yaml`: "
                      + ", ".join(f"`{r['id']}: {r['sha256']}`" for r in unpinned)]
        return "\n".join(lines)


def load_manifest(path: str | Path = MANIFEST) -> list[Document]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    files = Path(path).parent / "files"
    docs = [Document(**{**d, "effective": str(d["effective"]), "sha256": d.get("sha256") or None,
                        "file": str(files / d["file"]) if d.get("file") else None})
            for d in data["documents"]]
    ids = [d.id for d in docs]
    if len(ids) != len(set(ids)):
        raise ValueError("document ids in the manifest must be unique")
    for d in docs:
        if d.format not in ("pdf", "html") or not re.fullmatch(r"[a-z0-9-]+", d.id):
            raise ValueError(f"{d.id}: format must be pdf or html, and the id lowercase letters, digits and -")
    return docs


# ------------------------------------------------------------------ text

class _Text(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table",
              "td", "th", "dt", "dd", "blockquote", "pre"}
    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "form", "button", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    lines = (re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if len(line) >= MIN_LINE_CHARS or HEADING.match(line))


def pdf_text(data: bytes) -> str:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages = [page.get_text("text") for page in doc]  # pyright: ignore[reportAttributeAccessIssue]
    lines = (re.sub(r"\s+", " ", line).strip() for page in pages for line in page.splitlines())
    return "\n".join(line for line in lines if line)


def paragraphs(text: str) -> list[str]:
    """Lines joined into paragraphs; a heading always starts a new paragraph."""
    out: list[str] = []
    for line in text.splitlines():
        if HEADING.match(line) or not out or out[-1].endswith((".", ":", ";")) or len(out[-1]) > CHUNK_CHARS // 2:
            out.append(line)
        elif "\n" not in out[-1] and HEADING.match(out[-1]) and len(out[-1]) < 80:
            out[-1] += "\n" + line                       # a heading keeps its own line, above its first paragraph
        else:
            out[-1] += " " + line
    return [p for p in out if p.strip()]


def chunk(doc: Document, text: str, size: int = CHUNK_CHARS) -> list[Chunk]:
    pieces: list[str] = []
    current = ""
    for para in paragraphs(text):
        starts_section = bool(HEADING.match(para))
        if current and (starts_section or len(current) + len(para) + 1 > size):
            pieces.append(current)
            current = ""
        while len(para) > size:                         # a single very long paragraph: hard split
            pieces.append(para[:size])
            para = para[size:]
        current = f"{current}\n{para}" if current else para
    if current:
        pieces.append(current)
    return [Chunk(id=f"{doc.id}-{i:04d}", text=p.strip(), source=doc.id, country=doc.country, language=doc.language)
            for i, p in enumerate(pieces) if len(p.strip()) >= MIN_LINE_CHARS]


# ------------------------------------------------------------------ the run

def download(doc: Document, client: httpx.Client) -> bytes:
    if doc.file:
        return Path(doc.file).read_bytes()
    response = client.get(doc.url, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _failure(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"download failed (HTTP {exc.response.status_code})"
    if isinstance(exc, FileNotFoundError):
        return "file missing (add it under corpus/files/)"
    return f"download failed ({type(exc).__name__})"


def read_document(doc: Document, data: bytes) -> list[Chunk]:
    text = pdf_text(data) if doc.format == "pdf" else html_text(data.decode("utf-8", errors="replace"))
    return chunk(doc, text)


def scan(chunks: Sequence[Chunk], shields: Any) -> tuple[list[Chunk], list[str]]:
    """Prompt Shields document scan, in batches within the service's limits (5 documents, 10K characters).
    Flagged chunks are left out."""
    kept: list[Chunk] = []
    flagged: list[str] = []
    batch: list[Chunk] = []

    def flush() -> None:
        if batch:
            result = shields.analyze("Summarise these documents.", [c.text for c in batch])
            for c, attack in zip(batch, result.document_attacks, strict=True):
                (flagged.append(c.id) if attack else kept.append(c))
            batch.clear()

    for c in chunks:
        if batch and (len(batch) == SHIELDS_MAX_DOCUMENTS
                      or sum(len(b.text) for b in batch) + len(c.text) > SHIELDS_MAX_CHARS):
            flush()
        batch.append(c)
    flush()
    return kept, flagged


def build_index(endpoint: str, index_name: str, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]],
                titles: dict[str, str], index_client: Any = None, search_client: Any = None) -> None:
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )
    if index_client is None or search_client is None:
        from azure.identity import DefaultAzureCredential
        from azure.search.documents import SearchClient
        from azure.search.documents.indexes import SearchIndexClient
        credential = DefaultAzureCredential()
        index_client = SearchIndexClient(endpoint, credential)
        search_client = SearchClient(endpoint, index_name, credential)
    index = SearchIndex(
        name=index_name,
        fields=[
            SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
            SearchableField(name="text", type=SearchFieldDataType.String),
            SearchableField(name="title", type=SearchFieldDataType.String),
            SimpleField(name="source", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="country", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="language", type=SearchFieldDataType.String, filterable=True),
            SearchField(name="embedding", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                        searchable=True, vector_search_dimensions=DIMENSIONS, vector_search_profile_name="default"),
        ],
        vector_search=VectorSearch(algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
                                   profiles=[VectorSearchProfile(name="default", algorithm_configuration_name="hnsw")]),
        semantic_search=SemanticSearch(configurations=[SemanticConfiguration(
            name="default", prioritized_fields=SemanticPrioritizedFields(
                title_field=SemanticField(field_name="title"), content_fields=[SemanticField(field_name="text")]))]),
    )
    # Rebuilt from scratch: the corpus is small, and a rebuild leaves no chunk of a removed or changed document behind.
    if index_name in set(index_client.list_index_names()):
        index_client.delete_index(index_name)
    index_client.create_index(index)
    docs = [{"id": c.id, "text": c.text, "title": titles[c.source], "source": c.source, "country": c.country,
             "language": c.language, "embedding": list(v)} for c, v in zip(chunks, vectors, strict=True)]
    for i in range(0, len(docs), 100):
        search_client.upload_documents(documents=docs[i:i + 100])


def run(args: argparse.Namespace, client: httpx.Client | None = None, shields: Any = None, embedder: Any = None,
        indexer: Any = build_index) -> tuple[Report, int]:
    docs = load_manifest(args.manifest)
    report = Report()
    client = client or httpx.Client(timeout=60, headers={
        "User-Agent": "Mozilla/5.0 (compatible; credit-review-assistant-ingest/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "en,fr;q=0.8,nl;q=0.8"})
    chunks: list[Chunk] = []
    for doc in docs:
        try:
            data = download(doc, client)
        except Exception as exc:
            report.rows.append({"id": doc.id, "status": _failure(exc)})
            continue
        digest = hashlib.sha256(data).hexdigest()
        if doc.sha256 and doc.sha256 != digest:
            report.rows.append({"id": doc.id, "status": "**changed since it was pinned**", "sha256": digest})
            return report, 1
        try:
            found = read_document(doc, data)
        except Exception as exc:
            report.rows.append({"id": doc.id, "status": f"unreadable ({type(exc).__name__})"})
            continue
        if not found:            # an empty body or a challenge page: nothing to index, and no hash worth pinning
            report.rows.append({"id": doc.id, "status": f"no text ({len(data)} bytes received)"})
            continue
        chunks += found
        report.rows.append({"id": doc.id, "status": "ok" + (" (from file)" if doc.file else ""),
                            "chunks": len(found), "sha256": digest, "pinned": bool(doc.sha256)})
    if not chunks:
        return report, 1
    if args.dry_run:
        return report, 0

    from assistant.config import Settings
    settings = Settings.from_env()
    if settings.missing() and (shields is None or embedder is None):
        report.error = "missing settings: " + ", ".join(settings.missing())
        return report, 1
    if shields is None:
        from assistant.shields import PromptShields
        shields = PromptShields(settings.shields_endpoint)
    if embedder is None:
        from assistant.retriever import AzureEmbedder
        embedder = AzureEmbedder(settings.openai_endpoint, settings.embedding_deployment)
    try:
        kept, report.quarantined = scan(chunks, shields)
        vectors = embedder.embed([c.text for c in kept])
        indexer(settings.search_endpoint, settings.search_index, kept, vectors, {d.id: d.title for d in docs})
    except Exception as exc:          # the download report above is still worth printing
        report.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        return report, 1
    report.indexed = len(kept)
    return report, 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the assistant's search index from the corpus manifest.")
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    ap.add_argument("--dry-run", action="store_true", help="download, check and chunk only; no Azure call")
    args = ap.parse_args(argv)
    report, status = run(args)
    text = report.markdown()
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
