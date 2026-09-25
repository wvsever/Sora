-- Credit exposures from four source tables:
--   contract_loan              -> loan
--   contract_commitment        -> loan_commitment / financial_guarantee / other_commitment
--   contract_lease (lessor)    -> finance_lease (operating leases and lessee leases are not credit exposures)
--   contract_security_position -> debt_security (equities and fund units are out of scope)
WITH ref AS (SELECT reference_date AS d FROM manifest),
-- Days past due: oldest unpaid instalment at the reference date.
dpd AS (
    SELECT contract_id,
           max((SELECT d FROM ref) - CAST(due_date AS DATE)) AS days_past_due
    FROM src.contract_arrears
    WHERE CAST(due_amount AS DECIMAL(18,2)) > coalesce(CAST(paid_amount AS DECIMAL(18,2)), 0)
      AND CAST(due_date AS DATE) <= (SELECT d FROM ref)
    GROUP BY contract_id
),
loans AS (
    SELECT
        l.contract_id                                    AS exposure_id,
        l.entity_id,
        l.counterparty_id,
        'loan'                                           AS exposure_type,
        l.product_code,
        l.portfolio_id,
        l.currency,
        l.measurement_category                           AS src_measurement,
        l.declared_stage                                 AS src_stage,
        CAST(l.origination_date AS DATE)                 AS origination_date,
        CAST(l.contractual_maturity_date AS DATE)        AS maturity_date,
        CAST(l.gross_carrying_amount AS DECIMAL(18,2))   AS gross_carrying_amount,
        CAST(l.accrued_interest AS DECIMAL(18,2))        AS accrued_interest,
        nullif(CAST(l.undrawn_amount AS DECIMAL(18,2)), 0) AS off_balance_amount,
        CAST(l.committed_limit AS DECIMAL(18,2))         AS committed_amount,
        CAST(l.impairment_allowance AS DECIMAL(18,2))    AS loss_allowance,
        CAST(l.write_off_cumulative AS DECIMAL(18,2))    AS accumulated_write_off,
        l.interest_rate_type,
        CAST(l.current_interest_rate AS DECIMAL(18,9))   AS current_interest_rate,
        l.interest_rate_index                            AS reference_rate,
        CAST(l.interest_spread AS DECIMAL(18,9))         AS interest_spread,
        CAST(l.next_repricing_date AS DATE)              AS next_repricing_date,
        l.amortisation_type,
        l.is_credit_impaired,
        l.concession_type IS NOT NULL                    AS is_forborne,
        l.watchlist_flag                                 AS is_watchlist,
        l.is_revolving,
        l.is_unconditionally_cancellable,
        l.is_intragroup,
        l.purpose_code,
        l.financed_property_type
    FROM src.contract_loan l
),
commitments AS (
    SELECT
        c.contract_id,
        c.entity_id,
        c.counterparty_id,
        CASE c.commitment_type
            WHEN 'loan_commitment'           THEN 'loan_commitment'
            WHEN 'financial_guarantee_given' THEN 'financial_guarantee'
            ELSE 'other_commitment'
        END,
        c.product_code,
        c.portfolio_id,
        c.currency,
        CASE WHEN c.fair_value_option_elected THEN 'designated_fvtpl' ELSE 'amortised_cost' END,
        c.declared_stage,
        CAST(c.start_date AS DATE),
        CAST(coalesce(c.contractual_maturity_date, c.expiry_date) AS DATE),
        nullif(CAST(c.gross_carrying_amount AS DECIMAL(18,2)), 0),
        CAST(c.accrued_interest AS DECIMAL(18,2)),
        CAST(c.undrawn_amount AS DECIMAL(18,2)),
        CAST(c.committed_amount AS DECIMAL(18,2)),
        CAST(c.provision_balance AS DECIMAL(18,2)),
        NULL,
        c.interest_rate_type,
        CAST(c.current_interest_rate AS DECIMAL(18,9)),
        c.interest_rate_index,
        NULL,
        NULL,
        NULL,
        c.declared_stage IN ('stage3', 'poci'),
        NULL,
        NULL,
        NULL,
        c.is_unconditionally_cancellable,
        c.is_intragroup,
        NULL,
        NULL
    FROM src.contract_commitment c
),
leases AS (
    SELECT
        s.contract_id, s.entity_id, s.counterparty_id, 'finance_lease', 'FINANCE_LEASE_' || upper(s.underlying_asset_class),
        s.portfolio_id, s.currency, 'amortised_cost', s.declared_stage,
        CAST(s.commencement_date AS DATE), CAST(s.contractual_maturity_date AS DATE),
        CAST(s.gross_carrying_amount AS DECIMAL(18,2)), CAST(s.accrued_interest AS DECIMAL(18,2)),
        NULL, NULL, CAST(s.impairment_allowance AS DECIMAL(18,2)), NULL,
        'fixed', CAST(s.discount_rate AS DECIMAL(18,9)), NULL, NULL, NULL, NULL,
        s.declared_stage IN ('stage3', 'poci'), NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM src.contract_lease s
    WHERE s.role = 'lessor' AND s.is_finance_lease
),
securities AS (
    SELECT
        s.contract_id, s.entity_id, s.issuer_counterparty_id, 'debt_security', s.product_code,
        s.portfolio_id, s.currency, s.measurement_category, s.declared_stage,
        CAST(s.issue_date AS DATE), CAST(s.contractual_maturity_date AS DATE),
        CAST(s.gross_carrying_amount AS DECIMAL(18,2)), CAST(s.accrued_interest AS DECIMAL(18,2)),
        NULL, NULL, CAST(s.impairment_allowance AS DECIMAL(18,2)), CAST(s.write_off_cumulative AS DECIMAL(18,2)),
        s.interest_rate_type, CAST(s.coupon_rate AS DECIMAL(18,9)), NULL, NULL, NULL, NULL,
        s.declared_stage IN ('stage3', 'poci'), NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM src.contract_security_position s
    WHERE s.instrument_class = 'debt' AND NOT coalesce(s.is_short_position, false)
),
unioned AS (
    SELECT * FROM loans
    UNION ALL SELECT * FROM commitments
    UNION ALL SELECT * FROM leases
    UNION ALL SELECT * FROM securities
),
x AS (
    SELECT u.*,
        CASE u.src_measurement
            WHEN 'amortised_cost'    THEN 'amortised_cost'
            WHEN 'fvoci'             THEN 'fvoci'
            WHEN 'mandatorily_fvtpl' THEN 'fvtpl_mandatory'
            WHEN 'designated_fvtpl'  THEN 'fvtpl_designated'
            WHEN 'held_for_trading'  THEN 'held_for_trading'
        END AS measurement_category,
        c.eba_sector
    FROM unioned u
    LEFT JOIN sim_counterparty c USING (counterparty_id)
)
SELECT
    exposure_id,
    entity_id,
    counterparty_id,
    exposure_type,
    product_code,
    portfolio_id,
    currency,
    measurement_category,
    CASE WHEN measurement_category IN ('amortised_cost', 'fvoci') THEN coalesce(src_stage, 'stage1')
         ELSE 'not_applicable' END                                   AS stage,
    origination_date,
    maturity_date,
    gross_carrying_amount,
    accrued_interest,
    off_balance_amount,
    committed_amount,
    CASE WHEN measurement_category IN ('amortised_cost', 'fvoci') THEN coalesce(loss_allowance, 0) END AS loss_allowance,
    accumulated_write_off,
    interest_rate_type,
    current_interest_rate,
    reference_rate,
    interest_spread,
    next_repricing_date,
    amortisation_type,
    coalesce(d.days_past_due, 0)                                     AS days_past_due,
    -- Default proxy: stage 3 or more than 90 days past due (the export has no default flag).
    (src_stage = 'stage3' OR coalesce(d.days_past_due, 0) > 90)      AS is_defaulted,
    is_credit_impaired,
    is_forborne,
    is_watchlist,
    is_revolving,
    is_unconditionally_cancellable,
    is_intragroup,
    CASE WHEN eba_sector = 'household' AND exposure_type = 'loan' THEN
        CASE
            WHEN product_code = 'RESI_MTG'                         THEN 'house_purchase'
            WHEN product_code IN ('CONSUMER', 'CREDIT_CARD')       THEN 'consumption'
            WHEN product_code = 'PURCHASED_RECEIVABLES'
                 AND (purpose_code LIKE '%consumer%' OR purpose_code LIKE '%point_of_sale%'
                      OR purpose_code LIKE '%vehicle%')           THEN 'consumption'
            ELSE 'other'
        END
    END                                                              AS household_purpose,
    CASE WHEN eba_sector = 'non_financial_corporation' AND exposure_type = 'loan' THEN
        product_code = 'CRE' OR coalesce(financed_property_type = 'commercial', false)
    END                                                              AS is_cre,
    NULL                                                             AS country_of_risk
FROM x
LEFT JOIN dpd d ON d.contract_id = x.exposure_id
