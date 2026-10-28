"""Credit Review for Example Bank — the analyst's page.

    uv sync --extra ui && uv run streamlit run app.py

Pick a policy and a dossier, confirm what each document is, run, and read the
findings with their evidence. Everything shown comes from credit_review.pipeline; this file
only arranges it.

Hosted (CREDIT_REVIEW_UI_HOSTED=1, the ca-analyst Container App): the page asks for the
service's API key first and stays closed if none is configured. Locally and hosted, dossiers
come from the sample or an upload, never from a typed path. Text read from documents or written by a model
is escaped before it is shown, so a document cannot add markup, links or images to the page.

A second view asks the regulation assistant through its API (ASSISTANT_URL), with the server's key: the guards,
retrieval and memory stay in that service, and the browser never holds the key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from opentelemetry import trace
from streamlit.runtime.scriptrunner import get_script_run_ctx

from credit_review import telemetry
from credit_review.assistant_client import AssistantClient, AssistantError, ConversationExpired
from credit_review.classify import classify_documents
from credit_review.display import esc, md, safe_name
from credit_review.llm import LLMClient, LLMConfig
from credit_review.pdf import PdfDocument, Span, list_dossier
from credit_review.pipeline import Assessment, assess, compare, precheck
from credit_review.policy import load_policy
from credit_review.storage import store_from_env

ROOT = Path(__file__).resolve().parent
tracer = trace.get_tracer(__name__)
HOSTED = os.environ.get("CREDIT_REVIEW_UI_HOSTED") == "1"
SAMPLE = ROOT / "case_pack" / "dossier"
MAX_FILES, MAX_BYTES = 25, 15 * 1024 * 1024        # the same limits as the API

# outcome -> (label in the bank's words, strong colour, tint)
OUTCOMES = {
    "escalate":         ("Escalate to fraud desk", "#6927DA", "#F4EBFF"),
    "fail":             ("Not met",                "#B42318", "#FEF3F2"),
    "document_missing": ("Evidence missing",       "#475467", "#F2F4F7"),
    "unresolved":       ("Documents disagree",     "#0E7090", "#ECFDFF"),
    "warning":          ("Exception or clarify",   "#B54708", "#FFFAEB"),
    "analyst_review":   ("For the analyst",        "#1C3D5A", "#EEF4FA"),
    "pass":             ("Met",                    "#067647", "#ECFDF3"),
    "info":             ("Information",            "#667085", "#F9FAFB"),
    "not_applicable":   ("Not applicable",         "#98A2B3", "#FFFFFF"),
}
OFFLINE = "Cached results only"

@st.cache_resource(show_spinner=False)
def start_telemetry() -> bool:
    """Once per process (Streamlit reruns this script on every click): traces go where the API's go."""
    return telemetry.setup("analyst")


start_telemetry()
st.set_page_config(page_title="Credit Review — Example Bank pilot", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&display=swap');
html, body, [class*="css"], .stMarkdown, .stButton button, .stTextInput input, .stSelectbox, .stTabs button {
  font-family: 'Public Sans', system-ui, sans-serif; }
h1, h2, h3 { font-family: 'Public Sans', system-ui, sans-serif; letter-spacing: -0.01em; color: #101828; }
h1 { font-weight: 700; font-size: 1.9rem !important; }
.src { font-family: 'Source Serif 4', Georgia, serif; font-size: 1.02rem; line-height: 1.6;
       color: #344054; border-left: 3px solid #D0D5DD; padding: 0.2rem 0 0.2rem 0.9rem; margin: 0.3rem 0 0.8rem; max-width: 72ch; }
.quote { font-family: 'Source Serif 4', Georgia, serif; font-style: italic; color: #1D2939; }
.where { color: #667085; font-size: 0.85rem; }
.working { font-variant-numeric: tabular-nums; color: #344054; font-size: 0.92rem; margin: 0.1rem 0; }
.badge { display: inline-block; padding: 0.08rem 0.5rem; border-radius: 3px; font-size: 0.8rem;
         font-weight: 600; white-space: nowrap; }
.strip { display: flex; flex-wrap: wrap; gap: 6px; margin: 0.4rem 0 0.3rem; }
.tile { width: 74px; padding: 6px 7px 7px; border-radius: 4px; border-left: 5px solid; }
.tile b { display: block; font-size: 1.15rem; font-variant-numeric: tabular-nums; color: #101828; }
.tile span { display: block; font-size: 0.68rem; line-height: 1.15; color: #344054; }
.legend { color: #475467; font-size: 0.8rem; margin-bottom: 0.8rem; }
.legend i { display: inline-block; width: 10px; height: 10px; margin: 0 4px 0 12px; border-radius: 2px; }
.headline { font-size: 1.05rem; line-height: 1.7; color: #101828; max-width: 80ch; }
.change { border-top: 1px solid #EAECF0; padding: 0.6rem 0 0.5rem; }
.change .why { color: #344054; font-size: 0.92rem; margin-top: 0.25rem; max-width: 90ch; }
.banner { background: #FFFAEB; border-left: 4px solid #B54708; padding: 0.5rem 0.9rem; color: #7A2E0E; margin-bottom: 0.8rem; }
</style>
""", unsafe_allow_html=True)

if HOSTED:                                    # the same key as the API; no key configured means closed
    expected = os.environ.get("CREDIT_API_KEY", "")
    if not expected:
        st.error("This page has no API key configured, so it stays closed.")
        st.stop()
    if not st.session_state.get("unlocked"):
        st.title("Mortgage file assessment")
        key = st.text_input("API key", type="password", help="The key the service was deployed with.")
        if key and hmac.compare_digest(key.encode(), expected.encode()):
            st.session_state.unlocked = True
            st.rerun()
        if key:
            st.error("That key was not accepted.")
        st.stop()


def badge(outcome: str) -> str:
    label, strong, tint = OUTCOMES.get(outcome, (outcome, "#667085", "#F2F4F7"))
    return f'<span class="badge" style="color:{strong};background:{tint};border:1px solid {strong}33">{label}</span>'


# --------------------------------------------------------------------- loading

@st.cache_resource(show_spinner=False)
def load_policy_cached(path: str, mtime: float):
    return load_policy(path)


@st.cache_resource(show_spinner="Reading the PDF files…")
def load_dossier_cached(folder: str, signature: tuple) -> list[PdfDocument]:
    return list_dossier(folder)


def dossier_signature(folder: Path) -> tuple:
    return tuple(sorted((p.name, p.stat().st_mtime) for p in folder.glob("*.pdf")))


@st.cache_resource(show_spinner=False)
def make_llm(offline: bool) -> LLMClient:
    """The provider and model come from the environment (.env locally, the Container App's settings when hosted).
    Replies are cached where the API caches them (Blob when configured), so a dossier the API has read is free here."""
    config = LLMConfig.from_env(ROOT / ".env", **({"offline": True} if offline else {}))
    return LLMClient(config, store=store_from_env("llm-cache", config.cache_dir))


def save_upload(files) -> tuple[Path | None, str]:
    """Uploaded PDFs go to a folder named by their content, so the same upload is the same dossier."""
    if len(files) > MAX_FILES:
        return None, f"At most {MAX_FILES} files per dossier."
    documents: dict[str, bytes] = {}
    for f in files:
        data = f.getvalue()
        if len(data) > MAX_BYTES or not data.startswith(b"%PDF-"):
            return None, f"{safe_name(f.name)} is not a PDF of at most {MAX_BYTES // 2**20} MB."
        documents[safe_name(f.name)] = data
    digest = hashlib.sha256(b"".join(hashlib.sha256(d).digest() for _, d in sorted(documents.items()))).hexdigest()
    folder = Path(tempfile.gettempdir()) / "credit-review-uploads" / digest[:24]
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in documents.items():
        (folder / name).write_bytes(data)
    return folder, ""


# --------------------------------------------------------------------- the regulation assistant

EXAMPLES = [
    "What is the maximum loan-to-value for a mortgage in the Netherlands?",
    "Quel est le taux d'effort maximal pour un crédit immobilier en France ?",
    "How do Belgium and the Netherlands limit loan-to-value?",
]


def render_turn(turn: dict) -> None:
    """Everything the service returns is shown as text: an answer or a quote cannot add markup, links or images."""
    with st.chat_message(turn["role"]):
        if turn["role"] == "user":
            st.markdown(md(turn["text"]))
            return
        if turn.get("refused"):
            st.warning(md(turn.get("answer", "")))
            st.caption(f"refused: {md(turn.get('code', ''))}")
            return
        st.markdown(md(turn.get("answer", "")))
        for c in turn.get("citations", []):
            st.markdown(f'<div class="where"><b>{esc(c.get("title") or c.get("source") or "Source")}</b> '
                        f'&nbsp;{esc(c.get("chunk_id", ""))}</div>'
                        f'<div class="src"><span class="quote">“{esc(c.get("quote", ""))}”</span></div>',
                        unsafe_allow_html=True)


def assistant_view() -> None:
    """The regulation assistant, through its own API: the guards, retrieval and memory stay in that service."""
    st.title("Regulation assistant")
    st.caption("Mortgage-lending rules in the EU, Belgium, France and the Netherlands, answered from the sources with "
               "verbatim quotes. Five questions per conversation.")
    url, key = os.environ.get("ASSISTANT_URL", ""), os.environ.get("CREDIT_API_KEY", "")
    if not url or not key:
        st.info("The assistant is not configured for this page (ASSISTANT_URL and CREDIT_API_KEY).")
        return
    state = st.session_state
    user = state.setdefault("assistant_user", "analyst-" + secrets.token_hex(8))    # one per browser session
    client = AssistantClient(url, key, user)
    if st.button("New conversation"):
        state.pop("assistant_conversation", None)
    try:
        if "assistant_conversation" not in state:
            created = client.start()
            state.update(assistant_conversation=created["id"], assistant_left=created["remaining_messages"],
                         assistant_turns=[])
    except AssistantError as exc:
        st.error(str(exc))
        return

    for turn in state.assistant_turns:
        render_turn(turn)
    left = state.assistant_left
    clicked = None
    if not state.assistant_turns:
        st.caption("Try:")
        for i, example in enumerate(EXAMPLES):
            if st.button(example, key=f"example_{i}"):
                clicked = example
    st.caption("No questions left in this conversation: start a new one in the sidebar." if left == 0
               else f"{left} question{'s' if left != 1 else ''} left in this conversation.")
    question = st.chat_input("Ask about a mortgage-lending rule…", disabled=left == 0) or clicked
    if not question:
        return
    try:
        with st.spinner("Checking the question and searching the sources…"):
            reply = client.ask(state.assistant_conversation, question)
    except ConversationExpired as exc:
        state.pop("assistant_conversation", None)
        st.warning(str(exc))
        return
    except AssistantError as exc:
        st.error(str(exc))
        return
    state.assistant_turns += [{"role": "user", "text": question}, {"role": "assistant", **reply}]
    state.assistant_left = reply.get("remaining_messages", 0)
    st.rerun()


# One web page for both: the dossier assessment and the regulation assistant, as two tabs. The assistant's tab is drawn
# first (the dossier tab stops early until a dossier is loaded), and the open tab survives reruns.
tab_dossier, tab_assistant = st.tabs(["Dossier assessment", "Regulation assistant"], key="page_tab",
                                     on_change="rerun")
with tab_assistant:
    assistant_view()

with tab_dossier:
    # --------------------------------------------------------------------- sidebar

    with st.sidebar:
        st.subheader("Policy")
        policies = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "policy").glob("*.yaml"))
        policy_path = st.selectbox("Policy configuration", policies,
                                   index=policies.index("policy/example_bank_v3.2.yaml") if "policy/example_bank_v3.2.yaml" in policies else 0)
        report = load_policy_cached(str(ROOT / policy_path), (ROOT / policy_path).stat().st_mtime)
        if report.ok:
            meta = report.policy.policy
            st.caption(f"{meta.id} version {meta.version}, status {meta.status}. "
                       f"Reviewed by {meta.translation.get('reviewed_by') or 'nobody yet'}.")
        else:
            st.error("This policy file is not valid and cannot be used:\n\n" + "\n".join(f"- {e}" for e in report.errors))
        with st.expander("Validation details"):
            for w in report.warnings:
                st.warning(w)
            for n in report.notes:
                st.caption(n)
            st.caption(f"SHA-256 {report.sha256[:16]}…")

        st.subheader("Dossier")
        # No typed folder paths, locally or hosted: a dossier is the sample or what the analyst uploads.
        source = st.radio("Documents", ["Sample dossier", "Upload PDFs"])
        folder, dossier_note = SAMPLE, ""
        if source == "Upload PDFs":
            uploads = st.file_uploader("The borrower's documents", type="pdf", accept_multiple_files=True)
            folder, dossier_note = save_upload(uploads) if uploads else (None, "Upload the borrower's PDF documents.")
        st.subheader("Run")
        decision_date = st.date_input("Decision date", date.today(), format="DD/MM/YYYY")
        override_app = st.checkbox("Set the application date myself")
        application_date = st.date_input("Application date", date.today(), format="DD/MM/YYYY") if override_app else None
        run_judgment = st.toggle("Judgment checks (model)", value=True,
                                 help="Articles 3.2, 3.3, 5.3.d and 7.1. The model can raise cited concerns, never pass a rule.")

        st.subheader("Model")
        env = LLMConfig.from_env(ROOT / ".env")
        configured = f"{env.label}: {env.model}" if env.model else env.label
        choice = st.radio("Read documents with", [configured, OFFLINE], index=1 if env.offline else 0,
                          help="Cached results only never calls a model: every reply comes from the cache or the "
                               "document is reported as not read.")
        llm = make_llm(choice == OFFLINE)
        st.caption(llm.describe())

    policy = report.policy
    if not report.ok or policy is None:
        st.stop()
        raise SystemExit         # st.stop() never returns; this tells the type checker so

    # --------------------------------------------------------------------- header

    st.title("Mortgage file assessment")
    st.caption("Credit Review pilot for Example Bank. Credit Review reads the documents and applies the credit policy; "
               "the analyst decides.")
    if policy.policy.status != "in_force":
        st.markdown(f'<div class="banner">Policy {policy.policy.id} v{policy.policy.version} is a <b>{policy.policy.status}</b>. '
                    f'Results are for review, not for credit decisions.</div>', unsafe_allow_html=True)

    if folder is None or not folder.is_dir():
        st.info(dossier_note or "Choose the sample dossier or upload the borrower's PDF documents in the sidebar.")
        st.stop()
    docs = load_dossier_cached(str(folder), dossier_signature(folder))
    by_name = {d.name: d for d in docs}
    if not docs:
        st.info("No PDF documents in this dossier.")
        st.stop()

    if st.session_state.get("dossier") != str(folder):          # new dossier: start over
        st.session_state.update(dossier=str(folder), types={d.name: None for d in docs},
                                proposals={}, assessment=None, comparison=None)

    # --------------------------------------------------------------------- 1. documents

    st.header("1. Documents")
    labels = {k: v.label for k, v in policy.document_types.items()}
    label_to_type = {v: k for k, v in labels.items()}
    UNSET = "Not set"

    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("Detect document types", type="secondary", width="stretch"):
            with st.spinner("Reading the first page of each document…"):
                for p in classify_documents(docs, policy, llm):
                    st.session_state.proposals[p.doc] = p
                    if p.proposed_type:
                        st.session_state.types[p.doc] = p.proposed_type
    with c2:
        st.caption("Text documents are recognised from the policy's own keywords; scans are read by the model. "
                   "Check each type before running: you can change any of them.")

    rows = []
    for d in docs:
        p = st.session_state.proposals.get(d.name)
        t = st.session_state.types.get(d.name)
        rows.append({
            "Document": d.name,
            "Type": labels.get(t, UNSET) if t else UNSET,
            "Detected by": ({"keywords": "Keywords", "llm": "Model", "none": "—"}[p.method] if p else ""),
            "Confidence": p.confidence if p else "",
            "Pages": d.page_count,
            "Text layer": "yes" if d.has_text_layer else "scan",
        })
    edited = st.data_editor(
        pd.DataFrame(rows), hide_index=True, width="stretch", key=f"types_{len(st.session_state.proposals)}",
        disabled=["Document", "Detected by", "Confidence", "Pages", "Text layer"],
        column_config={"Type": st.column_config.SelectboxColumn("Type", options=[UNSET] + list(labels.values()), required=True)},
    )
    st.session_state.types = {str(r["Document"]): label_to_type.get(str(r["Type"])) for _, r in edited.iterrows()}

    checklist = precheck(policy, docs, st.session_state.types)
    detected = bool(st.session_state.proposals) or any(st.session_state.types.values())
    problems = [r for r in checklist if r.status in ("missing", "too_few", "duplicate", "too_many")]
    missing = [r for r in problems if r.applies == "yes"]            # conditional ones are settled after reading
    depends = [r for r in problems if r.applies == "unknown"]
    untyped = [n for n, t in st.session_state.types.items() if not t]


    def checklist_status(r) -> str:
        if r.ok:
            return "in order" if r.status == "satisfied" else "not needed"
        if r.status == "analyst":
            return "analyst decides"
        word = r.status.replace("_", " ")
        return f"{word}, if it applies" if r.applies == "unknown" else word


    summary = f"{sum(r.ok for r in checklist)} of {len(checklist)} in order"
    if missing:
        summary += f", {len(missing)} missing"
    if depends:
        summary += f", {len(depends)} depend on values read from the documents"
    with st.expander(f"Required documents: {summary}", expanded=detected and bool(missing)):
        st.dataframe(pd.DataFrame([{
            "Requirement": f"{r.id} {r.source}", "Status": checklist_status(r),
            "Documents": ", ".join(r.docs), "Note": r.detail,
        } for r in checklist]), hide_index=True, width="stretch")
        if depends:
            st.caption("Whether a conditional requirement applies is decided after the documents are read, "
                       "for example by the employment type on the application.")
    if detected and missing and st.session_state.get("toasted") != tuple(r.id for r in missing):
        for r in missing:
            st.toast(f"{r.id} {r.source}: {r.status.replace('_', ' ')}", icon="⚠️")
        st.session_state.toasted = tuple(r.id for r in missing)

    # --------------------------------------------------------------------- 2. run

    st.header("2. Assessment")
    if not detected:
        st.caption("Detect the document types first, or set them by hand in the table above.")
    elif untyped:
        st.caption(f"{len(untyped)} document(s) without a type will not be read: {md(', '.join(untyped))}.")
    if detected and missing:
        st.caption("Some required documents are missing. You can still run: the affected rules will say so.")

    if st.button("Run assessment", type="primary"):
        with st.status("Assessing the file…", expanded=True) as status:
            def say(msg: str) -> None:
                if get_script_run_ctx(suppress_warning=True) is not None:     # worker threads cannot draw
                    status.write(msg)
            try:
                with tracer.start_as_current_span("assess") as span:     # the same span the API writes
                    span.set_attributes({"credit_review.documents": len(docs),
                                         "credit_review.policy_version": str(policy.policy.version)})
                    st.session_state.assessment = assess(
                        policy, report.sha256, docs, st.session_state.types, llm,
                        decision_date=decision_date.isoformat(),
                        application_date=application_date.isoformat() if application_date else None,
                        run_judgment=run_judgment, progress=say)
                st.session_state.comparison = None
                status.update(label="Assessment complete", state="complete", expanded=False)
            except Exception as exc:
                status.update(label="Assessment failed", state="error")
                st.exception(exc)

    a: Assessment | None = st.session_state.get("assessment")
    if a is None:
        st.stop()

    # --------------------------------------------------------------------- results

    rank = {k: v.rank for k, v in policy.outcomes.items()}
    tiles = "".join(
        f'<div class="tile" style="background:{OUTCOMES[art.outcome][2]};border-color:{OUTCOMES[art.outcome][1]}" '
        f'title="{art.section_title}: {OUTCOMES[art.outcome][0]}"><b>{art.id}</b><span>{OUTCOMES[art.outcome][0]}</span></div>'
        for art in a.articles)
    legend = "".join(f'<i style="background:{c}"></i>{l}' for k, (l, c, _) in OUTCOMES.items()
                     if any(art.outcome == k for art in a.articles))
    st.markdown(f'<div class="strip">{tiles}</div><div class="legend">{legend}</div>', unsafe_allow_html=True)
    st.markdown('<div class="headline">' + "<br>".join(esc(h) for h in a.summary["headline"]) + "</div>",
                unsafe_allow_html=True)
    for w in a.warnings:
        st.warning(md(w))

    tab_findings, tab_disagree, tab_integrity, tab_docs, tab_change, tab_audit = st.tabs(
        ["Findings", "Where documents disagree", "Integrity checks", "Documents and values", "Policy change", "Audit trail"],
        key="results_tab", on_change="rerun")          # keep the open tab when a button inside it reruns the page


    def evidence_block(c, key: str) -> None:
        """Quotes grouped by document; one page rendered on request, with the value highlighted."""
        unique = {}
        for e in c.evidence:
            unique.setdefault((e.doc, e.page, e.quote), e)
        if not unique:
            return
        with st.expander(f"Evidence: {len(unique)} quote(s) from {len({k[0] for k in unique})} document(s)"):
            items = list(unique.values())
            for e in items[:40]:
                where = f"page {e.page}" + (f", lines {e.lines[0]}–{e.lines[-1]}" if e.lines else "") if e.page else ""
                how = {"image": "read from the scanned page", "fuzzy": "approximate match", "exact": "",
                       "not_found": "quote not found in the document", "none": ""}[e.located]
                st.markdown(f'<span class="quote">“{esc(e.quote)}”</span><br><span class="where">{esc(e.doc)}, {where}'
                            f'{" — " + how if how else ""}</span>', unsafe_allow_html=True)
            if len(items) > 40:
                st.caption(f"…and {len(items) - 40} more.")
            options = ["—"] + [f"{i}: {e.doc}, page {e.page}" for i, e in enumerate(items) if e.page]
            pick = st.selectbox("Show on the page", options, key=f"ev_{key}")
            if pick != "—":
                e = items[int(pick.split(":")[0])]
                pdf = by_name.get(e.doc)
                if pdf:
                    span = Span(located=e.located, page=e.page, lines=e.lines,
                                bbox=tuple(e.bbox) if e.bbox else None)
                    png = (pdf.highlight_png(span) if e.bbox else None) or pdf.page_png(e.page)
                    st.image(png, caption=f"{e.doc}, page {e.page}", width="stretch")


    with tab_findings:
        show = st.multiselect("Show articles that are", [OUTCOMES[k][0] for k in OUTCOMES],
                              default=[OUTCOMES[k][0] for k in OUTCOMES if k not in ("pass", "not_applicable", "info")])
        shown_keys = {k for k, v in OUTCOMES.items() if v[0] in show}
        current = None
        for art in a.articles:
            if art.outcome not in shown_keys:
                continue
            if art.section != current:
                current = art.section
                st.subheader(f"{art.section} {art.section_title}")
            first = next((c for c in art.checks if c.outcome == art.outcome), art.checks[0])
            with st.expander(f"{art.id}  ·  {OUTCOMES[art.outcome][0]}  ·  {first.description}"):
                st.markdown(f'<div class="src">{art.source}</div>', unsafe_allow_html=True)
                for c in art.checks:
                    st.markdown(f"{badge(c.outcome)} &nbsp;<b>{c.id}</b> {c.description}", unsafe_allow_html=True)
                    st.markdown(md(c.reason))
                    for w in c.working:
                        st.markdown(f'<div class="working">{esc(w)}</div>', unsafe_allow_html=True)
                    for d in c.details:
                        st.markdown(f"- {md(d)}")
                    if c.judgment and c.judgment.get("concerns"):
                        facts = {f["id"]: f for f in c.judgment.get("facts", [])}
                        st.caption(f"Model concerns ({c.judgment.get('model')}), each with the facts it cites:")
                        for con in c.judgment["concerns"]:
                            st.markdown(f"- {md(con['text'])}")
                            for fid in con["facts"]:
                                f = facts.get(fid, {})
                                st.markdown(f'<div class="where">&nbsp;&nbsp;&nbsp;{esc(fid)}: {esc(f.get("text", "")[:160])} '
                                            f'— {esc(f.get("docs", ""))}</div>', unsafe_allow_html=True)
                        for r in c.judgment.get("rejected", []):
                            st.caption(f"Discarded, no valid citation: {md(r['text'])}")
                    evidence_block(c, f"{art.id}_{c.id}")

    with tab_disagree:
        conflicts = [f for f in a.facts if f.status == "conflict"]
        compatible = [f for f in a.facts if f.status == "compatible"]
        if not conflicts:
            st.success("No value differs between documents.")
        for f in conflicts:
            st.markdown(f"**{f.description.split('. ')[0].rstrip('.')}**")
            st.dataframe(pd.DataFrame([{
                "Value": v.display, "Documents": ", ".join(v.docs),
                "As printed": " / ".join(dict.fromkeys(v.raws)), "Quote": v.evidence[0].quote if v.evidence else "",
            } for v in f.candidates]), hide_index=True, width="stretch")
        if compatible:
            st.caption("Different precision only, not a conflict:")
            for f in compatible:
                st.markdown(f"- {f.description.split('. ')[0].rstrip('.')}: " + md(" vs ".join(
                    f"{v.display} ({', '.join(v.docs)})" for v in f.candidates) + f". {f.note}"))

    with tab_integrity:
        sev = {"fraud_signal": "Fraud signal", "warning": "Warning", "info": "Information"}
        res = {"pass": "Passed", "fail": "Failed", "not_evaluable": "Not evaluable"}
        st.dataframe(pd.DataFrame([{"Check": f"{r.id} {r.description}", "Severity": sev[r.severity],
                                    "Result": res[r.status], "Detail": r.detail} for r in a.integrity]),
                     hide_index=True, width="stretch")
        st.caption("Deterministic checks (check digits, encoded dates, arithmetic, date order). No model involved. "
                   "A failed fraud signal is escalated under 7.2 and never resolved by one analyst.")

    with tab_docs:
        st.dataframe(pd.DataFrame([{"Requirement": f"{r.id} {r.source}", "Status": r.status.replace("_", " "),
                                    "Documents": ", ".join(r.docs), "Note": r.detail} for r in a.requirements]),
                     hide_index=True, width="stretch")
        names = [e.doc for e in a.extractions]
        if names:
            pick = st.selectbox("Values read from", names)
            ex = next(e for e in a.extractions if e.doc == pick)
            info = f"{ex.doc_type.replace('_', ' ')}, read from {'the text layer' if ex.method == 'text' else 'page images'}"
            if ex.llm:
                info += f" by {ex.llm.model} ({ex.llm.mode}{', cached' if ex.llm.cached else ''})"
            st.caption(md(info + (f". Not in this document: {', '.join(ex.not_found)}." if ex.not_found else ".")))
            st.dataframe(pd.DataFrame([{
                "Field": o.path, "Value": (json.dumps(o.value, ensure_ascii=False) if isinstance(o.value, (dict, list))
                                         else "" if o.value is None else str(o.value)),
                "As printed": o.raw, "Quote": o.evidence.quote, "Page": o.evidence.page,
                "Located": o.evidence.located, "Checks": ", ".join(o.flags),
            } for o in ex.observations]), hide_index=True, width="stretch")

    with tab_change:
        st.markdown("Run the same file under another version of the policy and see exactly which findings change. "
                    "Documents already read are not read again.")
        others = [p for p in policies if p != policy_path]
        if not others:
            st.info("Add a second policy file to the policy folder (for example a copy of v3.2 with one rule changed).")
        else:
            other = st.selectbox("Compare with", others)
            other_report = load_policy_cached(str(ROOT / other), (ROOT / other).stat().st_mtime)
            if not other_report.ok:
                st.error("That policy file is not valid, so it cannot go live:\n\n" + "\n".join(f"- {e}" for e in other_report.errors))
            else:
                old_p, new_p = policy.parameters, other_report.policy.parameters

                def shown(prm) -> str:
                    if prm is None:
                        return "—"
                    if isinstance(prm.value, list):
                        return ", ".join(prm.value)
                    return f"{prm.value * 100:g}%" if prm.unit == "ratio" else f"{prm.value:g} {prm.unit}"

                pchanges = [{"Parameter": k, "Owner": (new_p.get(k) or old_p.get(k)).owner,
                             "Before": shown(old_p.get(k)), "After": shown(new_p.get(k))}
                            for k in sorted(set(old_p) | set(new_p))
                            if k not in old_p or k not in new_p or old_p[k].value != new_p[k].value]
                old_ids = {c.id for _, _, c in policy.checks()}
                new_ids = {c.id for _, _, c in other_report.policy.checks()}
                st.caption(f"{other_report.policy.policy.id} v{other_report.policy.policy.version} "
                           f"({other_report.policy.policy.status}). {len(pchanges)} parameter(s) changed; "
                           f"rules added: {', '.join(sorted(new_ids - old_ids)) or 'none'}; "
                           f"removed: {', '.join(sorted(old_ids - new_ids)) or 'none'}.")
                if pchanges:
                    st.dataframe(pd.DataFrame(pchanges), hide_index=True, width="stretch")
                if other_report.policy is not None and st.button("Show what this change does to the file"):
                    with st.spinner("Re-applying the policy…"):
                        b = assess(other_report.policy, other_report.sha256, docs, st.session_state.types, llm,
                                   decision_date=a.run["decision_date"], application_date=a.run.get("application_date"),
                                   run_judgment=a.run["judgment"] == "on")
                        st.session_state.comparison = (other, compare(a, b), {art.id: art.outcome for art in b.articles})
                comparison = st.session_state.get("comparison")
                if comparison and comparison[0] == other:
                    _, rows, after = comparison
                    before = {art.id: art.outcome for art in a.articles}
                    moved = [k for k in after if before.get(k) != after[k]]
                    if not rows:
                        st.success("No finding on this file changes.")
                    else:
                        if moved:
                            st.markdown("**Articles whose outcome changes:** " + "; ".join(
                                f"{k} {OUTCOMES.get(before.get(k) or '', ('new',))[0]} → {OUTCOMES[after[k]][0]}" for k in moved))
                        for r in rows:
                            was = badge(r["before"]) if r["before"] != "—" else '<span class="where">new rule</span>'
                            st.markdown(f'<div class="change"><b>{esc(r["check"])}</b> &nbsp;{was} → {badge(r["after"])}'
                                        f'<div class="why">{esc(r["reason"])}</div></div>', unsafe_allow_html=True)
                        st.caption("Every other finding on this file is identical under both versions.")

    with tab_audit:
        st.markdown(f"**Policy** {a.policy['id']} v{a.policy['version']} ({a.policy['status']}), "
                    f"reviewed by {a.policy.get('reviewed_by') or 'nobody yet'}, SHA-256 `{a.policy['sha256'][:16]}…`")
        st.markdown("**Dates**  \n" + "  \n".join(a.run["dates"]))
        st.markdown("**Models**  \n" + ("  \n".join(a.run["models_used"]) or "none") + f"  \nJudgment checks: {a.run['judgment']}")
        st.dataframe(pd.DataFrame([{"Document": d.doc, "Type": d.doc_type, "SHA-256": d.sha256} for d in a.documents]),
                     hide_index=True, width="stretch")
        st.caption("Every model call is cached by its full input, so this run can be reproduced exactly from this record.")
        st.download_button("Download the assessment (JSON)", a.model_dump_json(indent=2),
                           file_name=f"assessment_{a.run['decision_date']}.json", mime="application/json")
