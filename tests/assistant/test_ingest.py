"""Corpus ingestion: manifest, text, chunks, hash pinning, the Prompt Shields quarantine."""

import argparse
import hashlib

import httpx
import pytest
import yaml

from assistant.ingest import Document, chunk, html_text, load_manifest, run, scan
from assistant.models import Chunk
from tests.assistant.fakes import Shields

DOC = Document("nl-thk", "Regeling", "MinFin", "NL", "nl", "2013-01-01", "https://example.org/r", "html", "public")

HTML = """<html><head><script>var x = 1;</script><style>p{}</style></head><body>
<nav>Home | Over ons | Contact en meer links hier</nav>
<h2>Artikel 3</h2><p>Het hypothecair krediet bedraagt ten hoogste 100 procent van de waarde van de woning.</p>
<h2>Artikel 4</h2><p>De financieringslast is ten hoogste het bedrag dat volgt uit de tabel in de bijlage.</p>
<footer>Copyright notice for the whole site footer</footer></body></html>"""


def test_html_keeps_the_text_and_drops_scripts_menus_and_footers():
    text = html_text(HTML)
    assert "ten hoogste 100 procent" in text and "Artikel 3" in text
    assert "var x" not in text and "Over ons" not in text and "Copyright" not in text


def test_each_article_starts_a_new_chunk():
    chunks = chunk(DOC, html_text(HTML))
    assert [c.text.splitlines()[0] for c in chunks] == ["Artikel 3", "Artikel 4"]
    assert [c.id for c in chunks] == ["nl-thk-0000", "nl-thk-0001"]
    assert all(c.country == "NL" and c.source == "nl-thk" for c in chunks)


def test_long_text_is_split_under_the_size():
    text = "\n".join(f"Sentence number {i} about loan-to-value limits and affordability." for i in range(200))
    chunks = chunk(DOC, text, size=500)
    assert len(chunks) > 5 and all(len(c.text) <= 500 for c in chunks)


def test_flagged_chunks_are_quarantined_and_batches_stay_under_the_limit():
    chunks = [Chunk(f"d-{i}", ("x" * 3000) + (" IGNORE PREVIOUS" if i == 2 else ""), "d", "EU", "en") for i in range(6)]
    shields = Shields(flagged=["IGNORE PREVIOUS"])
    kept, flagged = scan(chunks, shields)
    assert flagged == ["d-2"] and len(kept) == 5 and shields.calls >= 2


def test_the_manifest_in_the_repository_is_valid():
    docs = load_manifest()
    assert len(docs) == 5 and {d.country for d in docs} == {"EU", "BE", "FR", "NL"}


def write_manifest(tmp_path, sha256=None):
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"documents": [{**DOC.__dict__, "sha256": sha256}]}))
    return path


def client(body: bytes = HTML.encode(), status: int = 200):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, content=body)))


def test_a_dry_run_reports_the_hash_to_pin(tmp_path):
    report, status = run(argparse.Namespace(manifest=write_manifest(tmp_path), dry_run=True), client=client())
    assert status == 0 and report.rows[0]["chunks"] == 2
    assert hashlib.sha256(HTML.encode()).hexdigest() in report.markdown() and "Pin these hashes" in report.markdown()


def test_a_document_that_changed_since_it_was_pinned_stops_the_run(tmp_path):
    report, status = run(argparse.Namespace(manifest=write_manifest(tmp_path, "0" * 64), dry_run=True), client=client())
    assert status == 1 and "changed since it was pinned" in report.markdown()


def test_a_failed_download_is_reported_and_no_document_left_fails(tmp_path):
    report, status = run(argparse.Namespace(manifest=write_manifest(tmp_path), dry_run=True), client=client(status=404))
    assert status == 1 and "download failed" in report.markdown()


def test_a_full_run_scans_embeds_and_indexes(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_SEARCH_ENDPOINT", "https://s.search.windows.net")
    indexed = {}

    class Embedder:
        def embed(self, texts):
            return [[0.0] * 4 for _ in texts]

    def indexer(endpoint, index, chunks, vectors, titles):
        indexed.update(endpoint=endpoint, index=index, ids=[c.id for c in chunks], titles=titles)

    report, status = run(argparse.Namespace(manifest=write_manifest(tmp_path), dry_run=False), client=client(),
                         shields=Shields(flagged=["financieringslast"]), embedder=Embedder(), indexer=indexer)
    assert status == 0 and indexed["ids"] == ["nl-thk-0000"] and report.quarantined == ["nl-thk-0001"]
    assert indexed["index"] == "regulations" and indexed["titles"] == {"nl-thk": "Regeling"}


@pytest.mark.parametrize("bad", [{"format": "docx"}, {"id": "Bad ID"}])
def test_manifest_entries_are_checked(tmp_path, bad):
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump({"documents": [{**DOC.__dict__, **bad}]}))
    with pytest.raises(ValueError):
        load_manifest(path)


def test_missing_settings_stop_before_any_azure_call_and_keep_the_report(tmp_path, monkeypatch):
    for name in ("ASSISTANT_SEARCH_ENDPOINT", "ASSISTANT_SHIELDS_ENDPOINT", "CREDIT_REVIEW_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    report, status = run(argparse.Namespace(manifest=write_manifest(tmp_path), dry_run=False), client=client())
    text = report.markdown()
    assert status == 1 and "missing settings: ASSISTANT_SEARCH_ENDPOINT" in text and "| `nl-thk` | ok | 2 |" in text


def test_an_azure_failure_keeps_the_download_report(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_SEARCH_ENDPOINT", "https://s.search.windows.net")

    class Broken:
        def analyze(self, user_prompt, documents=()):
            raise RuntimeError("403")

    report, status = run(argparse.Namespace(manifest=write_manifest(tmp_path), dry_run=False), client=client(),
                         shields=Broken(), embedder=object())
    assert status == 1 and "Failed:** RuntimeError: 403" in report.markdown() and report.rows[0]["chunks"] == 2


def test_a_committed_copy_is_read_instead_of_downloaded(tmp_path):
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "nl.html").write_text(HTML)
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"documents": [{**DOC.__dict__, "file": "nl.html", "sha256": None}]}))
    report, status = run(argparse.Namespace(manifest=path, dry_run=True), client=client(status=500))
    assert status == 0 and report.rows[0]["status"] == "ok (from file)" and report.rows[0]["chunks"] == 2


def test_a_missing_copy_is_reported(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"documents": [{**DOC.__dict__, "file": "absent.pdf", "sha256": None}]}))
    report, _ = run(argparse.Namespace(manifest=path, dry_run=True), client=client())
    assert "file missing" in report.markdown()


def test_an_empty_page_is_not_ok_and_offers_no_hash_to_pin(tmp_path):
    report, status = run(argparse.Namespace(manifest=write_manifest(tmp_path), dry_run=True), client=client(body=b""))
    assert status == 1 and report.rows[0]["status"].startswith("no text")
    assert "e3b0c442" not in report.markdown() and "Pin these hashes" not in report.markdown()


def test_http_errors_name_the_status(tmp_path):
    report, _ = run(argparse.Namespace(manifest=write_manifest(tmp_path), dry_run=True), client=client(status=403))
    assert "download failed (HTTP 403)" in report.markdown()
