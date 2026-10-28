"""The analyst page (app.py): closed without the key when hosted, document text shown as text, the sample loads."""

import re

import pytest

from credit_review.display import esc, md, safe_name
from tests.conftest import ROOT

testing = pytest.importorskip("streamlit.testing.v1", reason="the analyst page needs the ui extra")
APP = str(ROOT / "app.py")


@pytest.fixture
def page(monkeypatch, tmp_path):
    monkeypatch.setenv("CREDIT_REVIEW_LLM_OFFLINE", "1")          # never a model call from a test
    monkeypatch.setenv("CREDIT_REVIEW_CACHE_DIR", str(tmp_path))
    for name in ("CREDIT_REVIEW_UI_HOSTED", "CREDIT_API_KEY", "CREDIT_REVIEW_STORAGE_URL", "ASSISTANT_URL"):
        monkeypatch.delenv(name, raising=False)

    def run(**env: str):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return testing.AppTest.from_file(APP, default_timeout=120).run()
    return run


def test_hosted_without_a_configured_key_the_page_stays_closed(page):
    at = page(CREDIT_REVIEW_UI_HOSTED="1")
    assert "stays closed" in at.error[0].value and not at.sidebar.radio


def test_hosted_the_page_opens_only_with_the_key(page):
    at = page(CREDIT_REVIEW_UI_HOSTED="1", CREDIT_API_KEY="right")
    assert not at.sidebar.radio
    at.text_input[0].input("wrong").run()
    assert "not accepted" in at.error[0].value and not at.sidebar.radio
    at.text_input[0].input("right").run()
    assert at.sidebar.radio[0].options == ["Sample dossier", "Upload PDFs"]


def test_locally_the_sample_dossier_loads_without_a_model(page):
    at = page()
    assert not at.exception
    assert [t.label for t in at.tabs][:2] == ["Dossier assessment", "Regulation assistant"]     # one page, two tabs
    assert at.sidebar.radio[0].options == ["Sample dossier", "Upload PDFs"]     # never a typed server path
    assert any(h.value == "1. Documents" for h in at.header)


def test_document_text_cannot_become_markup_links_or_images():
    assert esc('<img src=x onerror="alert(1)">\n\nnext') == \
        "&lt;img src=x onerror=&quot;alert&#40;1&#41;&quot;&gt;<br><br>next"
    assert "](" not in esc("[x](https://evil.example)") and "![" not in esc("![x](y)")
    assert md("![x](https://evil.example/?q=secret) **b**") == \
        r"\!\[x\]\(https://evil.example/?q=secret\) \*\*b\*\*"


@pytest.mark.parametrize(("name", "safe"), [("payslip.pdf", "payslip.pdf"), ("../../etc/passwd", "passwd.pdf"),
                                            ("a<script>.PDF", "a_script_.PDF"), ("", "document.pdf")])
def test_uploaded_file_names_are_reduced_to_plain_names(name, safe):
    assert safe_name(name) == safe


# ------------------------------------------------------------------ the assistant view (its API, faked)

@pytest.fixture
def assistant(monkeypatch):
    """A fake assistant API behind httpx.MockTransport; records every request the page sends."""
    import httpx

    import credit_review.assistant_client as client
    seen: list[httpx.Request] = []
    replies = {"answer": {"answer": "In the Netherlands: at most 100% of the home's value. <img src=x onerror=alert(1)>",
                          "refused": False, "code": "ok", "remaining_messages": 4,
                          "citations": [{"chunk_id": "nl-thk-0015", "source": "nl-thk",
                                         "title": "Tijdelijke regeling hypothecair krediet",
                                         "quote": "bedraagt honderd procent ![x](https://evil.example/?q=1)"}]}}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/conversations":
            return httpx.Response(201, json={"id": "c" * 24, "remaining_messages": 5})
        text = request.read().decode()
        if "poem" in text:
            return httpx.Response(200, json={"answer": "I can only answer questions about mortgage-lending rules.",
                                             "refused": True, "code": "off_topic", "citations": [],
                                             "remaining_messages": 4})
        return httpx.Response(200, json=replies["answer"])

    monkeypatch.setattr(client, "TRANSPORT", httpx.MockTransport(handle))
    return seen


def open_assistant(page, **env):
    return page(ASSISTANT_URL="https://assistant.example", CREDIT_API_KEY="the-key", **env)


def test_the_assistant_view_answers_with_its_citations(page, assistant):
    at = open_assistant(page)
    assert not at.exception and "Regulation assistant" in [t.value for t in at.title]
    assert len(at.button) >= 3                                          # the example questions
    at.chat_input[0].set_value("What is the maximum LTV in the Netherlands?").run()
    assert not at.exception
    page_text = " ".join(m.value for m in at.markdown)
    assert "Tijdelijke regeling hypothecair krediet" in page_text and "nl-thk-0015" in page_text
    assert re.search(r"(?<!\\)<img", page_text) is None                     # escaped: shown as text, not markup
    assert "](https://evil.example" not in page_text                  # no link or image from a quote
    assert "4 questions left" in " ".join(c.value for c in at.caption)
    sent = assistant[-1]
    assert sent.headers["X-API-Key"] == "the-key" and sent.headers["X-User-Id"].startswith("analyst-")
    assert all("the-key" not in m.value for m in at.markdown)          # the key never reaches the page


def test_a_refusal_is_shown_with_its_code(page, assistant):
    at = open_assistant(page)
    at.chat_input[0].set_value("Write me a poem about the sea.").run()
    assert md("off_topic") in " ".join(c.value for c in at.caption) and at.warning


def test_without_an_assistant_url_the_view_says_so(page):
    at = page(CREDIT_API_KEY="the-key")
    assert any("not configured" in i.value for i in at.info)
