-- Deposits received (contract_deposit).
--   deposit_type: current and vostro accounts -> current; savings -> savings; interbank_call -> call;
--                 notice -> notice; term and interbank_term -> term.
--   Floating deposits reset with the tenor of their index (EURIBOR_3M -> 3 months). The export has no reset
--   date for deposits: next_repricing_date stays NULL and the engine rolls the frequency from origination.
--   The export contains forward-starting renewals of term deposits (origination after the reference date,
--   renewed_from_contract_id = a deposit that is still outstanding): not on the balance sheet yet, not mapped.
SELECT
    d.contract_id                                           AS deposit_id,
    d.entity_id,
    d.counterparty_id,
    CASE d.deposit_type
        WHEN 'current'        THEN 'current'
        WHEN 'vostro'         THEN 'current'
        WHEN 'savings'        THEN 'savings'
        WHEN 'interbank_call' THEN 'call'
        WHEN 'notice'         THEN 'notice'
        WHEN 'term'           THEN 'term'
        WHEN 'interbank_term' THEN 'term'
    END                                                     AS deposit_type,
    d.product_code,
    d.currency,
    CAST(d.principal_outstanding AS DECIMAL(18,2))          AS amount,
    CAST(d.accrued_interest AS DECIMAL(18,2))               AS accrued_interest,
    CAST(d.origination_date AS DATE)                        AS origination_date,
    CAST(d.contractual_maturity_date AS DATE)               AS maturity_date,
    d.notice_period_days,
    d.interest_rate_type,
    CAST(d.current_interest_rate AS DECIMAL(18,9))          AS current_interest_rate,
    d.interest_rate_index                                   AS reference_rate,
    CAST(d.interest_rate_spread AS DECIMAL(18,9))           AS interest_spread,
    CAST(NULL AS DATE)                                      AS next_repricing_date,
    -- Central bank credit operations (CB_CREDIT_OP_3M) are floating without an index: the operation's tenor.
    CASE WHEN d.interest_rate_index IS NOT NULL
         THEN CAST(regexp_extract(d.interest_rate_index, '_([0-9]+)M$', 1) AS BIGINT)
         WHEN d.interest_rate_type = 'floating'
         THEN CAST(nullif(regexp_extract(d.product_code, '_([0-9]+)M$', 1), '') AS BIGINT) END AS repricing_frequency_months,
    d.is_intragroup,
    CAST(d.dgs_covered_amount AS DECIMAL(18,2))             AS dgs_covered_amount,
    d.is_operational_relationship                           AS is_operational,
    d.is_transactional_account                              AS is_transactional
FROM src.contract_deposit d
WHERE CAST(d.origination_date AS DATE) <= (SELECT reference_date FROM manifest)
