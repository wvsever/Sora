-- Debt securities issued (contract_debt_issued).
--   instrument_family: covered -> covered_bond; short_term_wholesale_paper -> certificate_of_deposit;
--   senior_unsecured -> senior_non_preferred when flagged, else senior_unsecured; subordinated; additional_tier1.
--   Floating instruments reset with the tenor of their index (EURIBOR_3M -> 3 months); the export has no reset
--   date, so the engine rolls the frequency from the issue date.
--   Retained amounts (own holdings, e.g. covered bonds kept as central bank collateral) are not a liability to
--   third parties: they are netted from the nominal, and the carrying amount is reduced pro rata.
--   The export contains forward-starting rollovers of wholesale paper (issue date after the reference date,
--   renewed_from_contract_id = a paper that is still outstanding): not issued yet, not mapped.
SELECT
    d.contract_id                                           AS debt_id,
    d.entity_id,
    CASE d.instrument_family
        WHEN 'covered'                    THEN 'covered_bond'
        WHEN 'short_term_wholesale_paper' THEN 'certificate_of_deposit'
        WHEN 'senior_unsecured'           THEN CASE WHEN coalesce(d.is_senior_non_preferred, false)
                                                    THEN 'senior_non_preferred' ELSE 'senior_unsecured' END
        WHEN 'subordinated'               THEN 'subordinated'
        WHEN 'additional_tier1'           THEN 'additional_tier1'
    END                                                     AS instrument_type,
    d.currency,
    CASE WHEN coalesce(CAST(d.retained_amount AS DECIMAL(18,2)), 0) = 0 THEN CAST(d.carrying_amount AS DECIMAL(18,2))
         ELSE CAST(round(CAST(d.carrying_amount AS DOUBLE)
                         * greatest(0, 1 - CAST(d.retained_amount AS DOUBLE) / CAST(d.nominal_amount AS DOUBLE)), 2)
                   AS DECIMAL(18,2)) END                    AS carrying_amount,
    greatest(CAST(d.nominal_amount AS DECIMAL(18,2)) - coalesce(CAST(d.retained_amount AS DECIMAL(18,2)), 0), 0) AS nominal_amount,
    CAST(d.accrued_interest AS DECIMAL(18,2))               AS accrued_interest,
    CAST(d.issue_date AS DATE)                              AS issue_date,
    CAST(d.contractual_maturity_date AS DATE)               AS maturity_date,
    CAST(d.first_call_date AS DATE)                         AS first_call_date,
    CASE WHEN d.interest_rate_index IS NOT NULL THEN 'floating' ELSE 'fixed' END AS interest_rate_type,
    CAST(d.coupon_rate AS DECIMAL(18,9))                    AS current_interest_rate,
    d.interest_rate_index                                   AS reference_rate,
    CAST(d.interest_rate_spread AS DECIMAL(18,9))           AS interest_spread,
    CAST(NULL AS DATE)                                      AS next_repricing_date,
    CASE WHEN d.interest_rate_index IS NOT NULL
         THEN CAST(regexp_extract(d.interest_rate_index, '_([0-9]+)M$', 1) AS BIGINT) END AS repricing_frequency_months,
    d.is_subordinated
FROM src.contract_debt_issued d
WHERE CAST(d.issue_date AS DATE) <= (SELECT reference_date FROM manifest)
