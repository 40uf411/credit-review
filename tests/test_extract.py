"""Classification and extraction on the real case dossier, replaying the hand-labelled
reference as the model. Proves the plumbing: schema, prompt, quote location, parsing,
guardrails. Model accuracy itself is measured with `python -m credit_review.golden`."""

from credit_review.classify import classify_documents
from credit_review.extract import build_schema, extract_dossier, fields_for_type, make_observation
from credit_review.golden import evaluate, score


def test_keywords_classify_every_text_document_without_a_model(docs, policy, golden):
    proposals = {p.doc: p for p in classify_documents(docs, policy, llm=None)}
    for name, label in golden["documents"].items():
        p = proposals[name]
        if p.has_text_layer:
            assert p.method == "keywords" and p.proposed_type == label["doc_type"], name
        else:
            assert p.proposed_type is None and p.method == "none"     # scans: analyst or model decides


def test_works_conformity_is_not_mistaken_for_electrical_conformity(doc, policy):
    p = classify_documents([doc("conformity.pdf")], policy, llm=None)[0]
    assert p.proposed_type == "works_conformity"


def test_scanned_payslips_are_classified_by_the_model(docs, policy, golden_llm):
    proposals = classify_documents(docs, policy, golden_llm)
    payslips = [p for p in proposals if not p.has_text_layer]
    assert len(payslips) == 3 and all(p.proposed_type == "payslip" and p.method == "llm" for p in payslips)


def test_policy_decides_which_fields_are_requested(policy):
    eid = fields_for_type(policy, "identity_eid")
    assert {"applicant.rrn", "identity.valid_until", "identity.mrz"} <= set(eid)
    assert "credit.amount" not in eid
    schema = build_schema(fields_for_type(policy, "becris_extract"))
    credits = schema["properties"]["fields"]["properties"]["becris__credits"]
    assert "contract_amount" in credits["properties"]["items"]["items"]["properties"]


def test_schema_has_no_optional_or_union_types(policy):
    """Keeps strict structured output within provider limits (all required, no type arrays)."""
    def walk(node):
        if isinstance(node, dict):
            assert not isinstance(node.get("type"), list)
            if node.get("type") == "object":
                assert set(node.get("required", [])) == set(node.get("properties", {}))
                assert node.get("additionalProperties") is False
            for v in node.values():
                walk(v)
    for doc_type in policy.document_types:
        walk(build_schema(fields_for_type(policy, doc_type)))


def test_full_dossier_extraction_with_replayed_reference(docs, policy, golden, golden_llm):
    types = {name: d["doc_type"] for name, d in golden["documents"].items()}
    results = extract_dossier(docs, types, policy, golden_llm, workers=4)
    assert len(results) == 10 and not any(r.error for r in results)
    assert score(evaluate(results, golden))["accuracy"] == 1.0
    for r in results:
        for o in r.observations:
            if r.method == "text" and o.evidence.quote:
                assert o.evidence.located == "exact", (r.doc, o.field, o.sub)
                assert o.evidence.page and o.evidence.bbox
            assert o.flags == [], (r.doc, o.path, o.flags)


def test_scanned_payslip_values(docs, policy, golden_llm):
    types = {"payslip_july2026.pdf": "payslip"}
    (july,) = extract_dossier(docs, types, policy, golden_llm)
    got = {o.path: o for o in july.observations}
    assert july.method == "vision"
    assert got["payslips.net_pay"].value == 2678.77
    assert got["payslips.salary_advance"].value == 250.0
    assert got["payslips.period"].value == "2026-07"
    assert got["payslips.net_pay"].evidence.located == "image"
    assert got["applicant.civil_status"].value == "cohabiting"
    assert "payslips.seizure" in july.not_found          # absent from the document: not an observation


def test_observations_are_self_contained_and_not_found_is_listed(docs, policy, golden_llm):
    types = {"application_form.pdf": "application_form"}
    (form,) = extract_dossier(docs, types, policy, golden_llm)
    assert {"credit.offered_rate", "applicant.first_time_buyer", "application.signature"} <= set(form.not_found)
    o = next(o for o in form.observations if o.path == "liabilities.declared[2].monthly_payment")
    assert o.value == 1851.73 and o.evidence.doc == form.doc and o.evidence.page == 4


# --- guardrails on model answers -------------------------------------------------

def _obs(doc, policy, field, reply, name="credit_registry.pdf", doc_type="becris_extract"):
    return make_observation(field, None, None, policy.fields[field], reply, doc(name), doc_type, vision=False)


def test_code_overrules_a_wrongly_normalised_number(doc, policy):
    o = _obs(doc, policy, "becris.consultation_date",
             {"found": True, "raw": "30/8/2026", "value": "2026-30-08", "quote": "Datum: 30/8/2026", "page": 1})
    assert o.value == "2026-08-30" and o.parse == "code"


def test_decimal_comma_mistake_is_caught(doc, policy):
    reply = {"found": True, "raw": "1.851,73", "value": "1.85173", "quote": "Termijnbedrag 1.851,73", "page": 2}
    o = make_observation("becris.credits", "instalment", 2, policy.fields["becris.credits"].items["instalment"],
                         reply, doc("credit_registry.pdf"), "becris_extract", vision=False)
    assert o.value == 1851.73 and "raw_value_mismatch" in o.flags


def test_invented_quote_is_flagged(doc, policy):
    o = _obs(doc, policy, "becris.consultation_date",
             {"found": True, "raw": "15/9/2026", "value": "2026-09-15",
              "quote": "Raadpleging op 15 september 2026", "page": 1})
    assert "quote_not_found" in o.flags


def test_value_absent_from_a_near_miss_quote_is_flagged(doc, policy):
    o = _obs(doc, policy, "applicant.rrn",
             {"found": True, "raw": "75030163262", "value": "75030163262",
              "quote": "Identificatiecode 75030163262", "page": 1})
    assert o.evidence.located == "fuzzy" and "raw_not_in_document" in o.flags


def test_enum_outside_the_policy_vocabulary_is_rejected(doc, policy):
    o = make_observation("applicant.civil_status", None, None, policy.fields["applicant.civil_status"],
                         {"found": True, "raw": "Weduwe", "value": "widow", "quote": "Weduwe", "page": 1},
                         doc("authoritative_data.pdf"), "authentic_data", vision=False)
    assert o.value is None and "invalid_enum" in o.flags


def test_not_found_is_not_a_value(doc, policy):
    assert _obs(doc, policy, "applicant.rrn", {"found": False, "raw": "", "value": "", "quote": "", "page": 0}) is None
