# Policy translation — review checklist

`example_bank_v3.2.yaml` was drafted with AI from the prose policy. The validator guarantees
that every article's source text exists verbatim in the policy and that every reference
resolves. It cannot judge whether the *interpretation* is right — that is this list.

Tick each item, then set `translation.reviewed_by` in the YAML.

## Interpretations of the policy text

- [ ] **2.2 age limit** — "fully repaid before the 76th birthday" → age at maturity < 76,
      maturity = decision date + term.
- [ ] **3.1 / D3 recency** — "three most recent months" is checked against the pay day seen on
      the payslips (25th): at an application date of 30/08, the August payslip already existed.
- [ ] **3.3 / counted income** — counted income = average evidenced payslip net. Non-professional
      income is excluded until the analyst confirms durability and household consistency.
- [ ] **3.4 exception band** — warning when DSTI > 40% and ≤ 45%; fail above 45%.
- [ ] **4.2 arrears** — a regularized arrear less than a year old is not covered by the policy
      text; it is flagged as a warning for the analyst rather than auto-declined.
- [ ] **5.3.d** — split into a deterministic part (amount ≤ amount found conform) and a judgment
      part (plausibility of the works).
- [ ] **5.4 total investment** — read from both the application and the conformity form. If they
      disagree, the rule is evaluated on every value and reported as unresolved when outcomes differ.

## Assumptions where the pack is silent

- [ ] **Application date** — the form has no date; defaults to the latest date in the dossier.
- [ ] **D4** — the authentic-source data sheet (tax data, income year 2024) is accepted as the
      most recent tax assessment.
- [ ] **D8** — presence is checked; three-month coverage is confirmed by the analyst in the pilot.
- [ ] **D9** — whether an electrical conformity certificate is required depends on regional
      regulation not in the pack → decided by the analyst.
- [ ] **Civil status** — a payslip may show a tax-household status ("Cohabitant") rather than the
      civil status; a mismatch is flagged, never concluded.

## Not in the policy — implementation choices (owner: example_systems)

These seven parameters are ours, not Example Bank's. They are listed by the validator and must be
confirmed in the mapping workshop: income change signal 20%, employment tenure signal 1 year,
credit cluster window 90 days / 2 credits, liability matching tolerance €1, floor-area tolerance
1 m², statutory employee social-security rate 13.07%.

## Severity of integrity checks

- [ ] **Fraud signals (escalate under 7.2):** national number checksum / birth date / sex, MRZ
      check digits and match, employer enterprise number, declared balances vs BECRIS, credit
      cluster before application.
- [ ] **Warnings:** IBAN validity, employee address = employer address, EPC date order, payslip
      social-security rate. **Info:** payslip net reconciliation.

Verified by hand while drafting: the eID MRZ check digits and both national numbers in the file
are valid; **the employer enterprise number on the payslips (BE 0366.618.186) fails its check
digit** (expected 31, printed 86).

## Labels the model reasons with

- [x] **tax.aggregate_taxable_income** (was `joint_taxable_income`). *Gezamenlijk belastbaar inkomen*
      is income taxed at the progressive rates, not a household total. The first judgment run took
      the wrong English label at face value and reasoned that "the applicant's own share is probably
      lower still". A wrong label produces confident wrong reasoning downstream: labels and
      descriptions are reviewed as carefully as thresholds.

## Reference labels (golden/)

- [ ] Spot-check `golden/sample_dossier.yaml` against the PDFs — at least the payslip
      amounts, the BECRIS credits and the dates. They were labelled by AI and are used to score
      model extraction, so they must be right. Then set `spot_checked_by`.
