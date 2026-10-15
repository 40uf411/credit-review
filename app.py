"""Credit Review for Example Bank — the analyst's page.

    streamlit run app.py

Pick a policy and a dossier, confirm what each document is, run, and read the
findings with their evidence. Everything shown comes from credit_review.pipeline; this file
only arranges it.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

from credit_review.classify import classify_documents
from credit_review.llm import LLMClient, LLMConfig
from credit_review.pdf import PdfDocument, Span, list_dossier
from credit_review.pipeline import Assessment, assess, compare, precheck
from credit_review.policy import load_policy

ROOT = Path(__file__).resolve().parent

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
PROVIDERS = {"Anthropic API": "anthropic", "LM Studio (local)": "lmstudio", "Cached results only": "offline"}

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


def make_llm(provider: str, model: str) -> LLMClient:
    overrides: dict = {}
    if provider == "offline":
        overrides["offline"] = True
    else:
        import os
        os.environ["CREDIT_REVIEW_LLM_PROVIDER"] = provider
    if model:
        overrides["model"] = model
    return LLMClient(LLMConfig.from_env(ROOT / ".env", **overrides))


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
    dossier_path = st.text_input("Folder with the borrower's documents", "case_pack/dossier")
    st.subheader("Run")
    decision_date = st.date_input("Decision date", date.today(), format="DD/MM/YYYY")
    override_app = st.checkbox("Set the application date myself")
    application_date = st.date_input("Application date", date.today(), format="DD/MM/YYYY") if override_app else None
    run_judgment = st.toggle("Judgment checks (model)", value=True,
                             help="Articles 3.2, 3.3, 5.3.d and 7.1. The model can raise cited concerns, never pass a rule.")

    st.subheader("Model")
    env = LLMConfig.from_env(ROOT / ".env")
    default_provider = "Cached results only" if env.offline else ("LM Studio (local)" if env.label == "lmstudio" else "Anthropic API")
    provider_label = st.radio("Read documents with", list(PROVIDERS), index=list(PROVIDERS).index(default_provider))
    model = st.text_input("Model", env.model, help="Empty for LM Studio = the first loaded model.")
    llm = make_llm(PROVIDERS[provider_label], model)
    st.caption(llm.describe())

if not report.ok:
    st.stop()
policy = report.policy

# --------------------------------------------------------------------- header

st.title("Mortgage file assessment")
st.caption("Credit Review pilot for Example Bank. Credit Review reads the documents and applies the credit policy; "
           "the analyst decides.")
if policy.policy.status != "in_force":
    st.markdown(f'<div class="banner">Policy {policy.policy.id} v{policy.policy.version} is a <b>{policy.policy.status}</b>. '
                f'Results are for review, not for credit decisions.</div>', unsafe_allow_html=True)

folder = (ROOT / dossier_path) if not Path(dossier_path).is_absolute() else Path(dossier_path)
if not folder.is_dir():
    st.info("Enter the folder that holds the borrower's PDF documents in the sidebar.")
    st.stop()
docs = load_dossier_cached(str(folder), dossier_signature(folder))
by_name = {d.name: d for d in docs}
if not docs:
    st.info(f"No PDF documents in {dossier_path}.")
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
st.session_state.types = {r["Document"]: label_to_type.get(r["Type"]) for _, r in edited.iterrows()}

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
    st.caption(f"{len(untyped)} document(s) without a type will not be read: {', '.join(untyped)}.")
if detected and missing:
    st.caption("Some required documents are missing. You can still run: the affected rules will say so.")

if st.button("Run assessment", type="primary"):
    with st.status("Assessing the file…", expanded=True) as status:
        def say(msg: str) -> None:
            if get_script_run_ctx(suppress_warning=True) is not None:     # worker threads cannot draw
                status.write(msg)
        try:
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
st.markdown('<div class="headline">' + "<br>".join(a.summary["headline"]) + "</div>", unsafe_allow_html=True)
for w in a.warnings:
    st.warning(w)

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
            st.markdown(f'<span class="quote">“{e.quote}”</span><br><span class="where">{e.doc}, {where}'
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
                st.image(pdf.highlight_png(span) if e.bbox else pdf.page_png(e.page),
                         caption=f"{e.doc}, page {e.page}", width="stretch")


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
                st.markdown(c.reason)
                for w in c.working:
                    st.markdown(f'<div class="working">{w}</div>', unsafe_allow_html=True)
                for d in c.details:
                    st.markdown(f"- {d}")
                if c.judgment and c.judgment.get("concerns"):
                    facts = {f["id"]: f for f in c.judgment.get("facts", [])}
                    st.caption(f"Model concerns ({c.judgment.get('model')}), each with the facts it cites:")
                    for con in c.judgment["concerns"]:
                        st.markdown(f"- {con['text']}")
                        for fid in con["facts"]:
                            f = facts.get(fid, {})
                            st.markdown(f'<div class="where">&nbsp;&nbsp;&nbsp;{fid}: {f.get("text", "")[:160]} '
                                        f'— {f.get("docs", "")}</div>', unsafe_allow_html=True)
                    for r in c.judgment.get("rejected", []):
                        st.caption(f"Discarded, no valid citation: {r['text']}")
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
            st.markdown(f"- {f.description.split('. ')[0].rstrip('.')}: " + " vs ".join(
                f"{v.display} ({', '.join(v.docs)})" for v in f.candidates) + f". {f.note}")

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
        st.caption(info + (f". Not in this document: {', '.join(ex.not_found)}." if ex.not_found else "."))
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
            if st.button("Show what this change does to the file"):
                with st.spinner("Re-applying the policy…"):
                    b = assess(other_report.policy, other_report.sha256, docs, st.session_state.types, llm,
                               decision_date=a.run["decision_date"], application_date=a.run.get("application_date"),
                               run_judgment=a.run["judgment"] == "on")
                    st.session_state.comparison = (other, compare(a, b), {art.id: art.outcome for art in b.articles})
            if st.session_state.get("comparison") and st.session_state.comparison[0] == other:
                _, rows, after = st.session_state.comparison
                before = {art.id: art.outcome for art in a.articles}
                moved = [k for k in after if before.get(k) != after[k]]
                if not rows:
                    st.success("No finding on this file changes.")
                else:
                    if moved:
                        st.markdown("**Articles whose outcome changes:** " + "; ".join(
                            f"{k} {OUTCOMES.get(before.get(k), ('new',))[0]} → {OUTCOMES[after[k]][0]}" for k in moved))
                    for r in rows:
                        was = badge(r["before"]) if r["before"] != "—" else '<span class="where">new rule</span>'
                        st.markdown(f'<div class="change"><b>{r["check"]}</b> &nbsp;{was} → {badge(r["after"])}'
                                    f'<div class="why">{r["reason"]}</div></div>', unsafe_allow_html=True)
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
