"""Function catalogue owned by Example Systems.

The policy YAML may only reference functions listed here. Each derived
function returns one value; each integrity function returns a pass/fail
result with the values it compared. Implementations live in derive.py and
integrity.py; a test asserts every contract below is implemented.
"""

DERIVED_FUNCTIONS: dict[str, str] = {
    "age_in_years": "Whole years between a birth date and a reference date.",
    "age_at_maturity": "Age at the reference date plus the credit term in years.",
    "years_between": "Years (decimal) between two dates.",
    "date_on_or_after": "True when the first date is on or after the second.",
    "payslips_cover_recent_months": "Payslips cover N consecutive months ending at the most recent month already paid at the reference date.",
    "payslips_have_advance_or_seizure": "Any payslip shows a salary advance or a seizure.",
    "average_net_pay": "Average net pay over the N most recent payslips.",
    "relative_deviation": "abs(a - b) / b.",
    "sum_non_professional_income": "Sum of declared income items that are not professional.",
    "sum_child_benefit": "Sum of declared child-benefit items.",
    "child_benefit_consistent_with_household": "Declared child benefit requires at least one dependent child.",
    "counted_income": "Income counted for affordability: evidenced professional net income.",
    "tax_income_ratio": "Monthly taxable income from tax data / average taxable pay on payslips.",
    "max_relative_change": "Largest month-to-month relative change in gross pay.",
    "existing_obligations": "Monthly instalments of BECRIS credits plus declared credits absent from BECRIS.",
    "annuity_payment": "Monthly annuity payment at the offered rate, or the reference rate when none is offered.",
    "sum_amounts": "Sum of amounts.",
    "ratio": "a / b.",
    "difference": "a - b.",
    "required_free_income": "Minimum free income plus an amount per dependent.",
    "within_months": "True when the first date is within N calendar months before the second.",
    "has_unregularized_arrear": "A registered arrear without regularization.",
    "has_regularized_arrear": "A regularized arrear is registered.",
    "all_register_credits_declared": "Every BECRIS credit matches a declared liability within tolerance.",
    "applicant_owns_property": "Buyer on the deed is the applicant.",
    "includes_any_category": "At least one list item has a category from the allowed list.",
    "required_own_funds": "max(absolute minimum, share x total investment).",
    "offer_blocking_documents_present": "All requirements marked blocks_offer are satisfied.",
}

INTEGRITY_FUNCTIONS: dict[str, str] = {
    "rrn_checksum_valid": "Belgian national register number mod-97 check (pre- and post-2000 births).",
    "rrn_matches_birth_date": "First six digits of the national number equal YYMMDD of the birth date.",
    "rrn_matches_sex": "Sequence digits odd for male, even for female.",
    "mrz_check_digits_valid": "ICAO 9303 check digits of the identity document MRZ.",
    "mrz_matches_identity": "MRZ birth date, sex and expiry equal the printed values.",
    "enterprise_number_valid": "Belgian enterprise number mod-97 check on each payslip employer.",
    "declared_balances_consistent_with_register": "Declared outstanding balance is possible given BECRIS contract dates and instalments.",
    "no_credit_cluster_before_application": "Fewer than N credits contracted within the window before the application.",
    "iban_valid": "IBAN format and mod-97 checksum.",
    "employee_address_differs_from_employer": "Employee address on the payslip differs from the employer address.",
    "epc_dates_chronological": "Permit application, end of works and construction year are in order.",
    "payslip_social_security_rate": "Employee social security equals the statutory rate times gross pay.",
    "payslip_net_reconciles": "Net pay equals taxable pay minus withholding tax minus special contribution.",
}
