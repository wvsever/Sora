-- Credit events from the credit event register, plus write-offs from the contract event log.
-- Default dates: the export has no explicit default event, so the first month-end in stage 3 is used.
WITH ev AS (
    SELECT contract_id, entity_id, CAST(event_date AS DATE) AS event_date, currency,
           CAST(amount AS DECIMAL(18,2)) AS amount,
           CASE event_type
               WHEN 'nonAccruedStatus'            THEN 'non_accrual'
               WHEN 'distressedForbearance'       THEN 'forbearance'
               WHEN 'specificCreditAdjustment'    THEN 'specific_credit_adjustment'
               WHEN 'obligorBankruptcyProtection' THEN 'bankruptcy'
               WHEN 'institutionFiledBankruptcy'  THEN 'bankruptcy'
               WHEN 'saleAtMaterialCreditLoss'    THEN 'distressed_sale'
           END AS event_type
    FROM src.contract_credit_event
    UNION ALL
    SELECT contract_id, entity_id, CAST(event_date AS DATE), currency, CAST(amount AS DECIMAL(18,2)), 'write_off'
    FROM src.contract_event WHERE event_type = 'write_off'
    UNION ALL
    SELECT exposure_id, entity_id, min(period_end), NULL, NULL, 'default'
    FROM sim_stage_history WHERE stage = 'stage3' GROUP BY ALL
)
SELECT
    contract_id AS exposure_id,
    entity_id,
    event_type,
    event_date,
    any_value(currency) AS currency,
    sum(amount)         AS amount
FROM ev
GROUP BY contract_id, entity_id, event_type, event_date
