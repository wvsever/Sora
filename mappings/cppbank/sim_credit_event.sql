-- Credit events from the credit event register, write-offs from the contract event log, and cures from
-- the default/cure register.
-- Default date: first month-end in stage 3. The register's unlikely-to-pay triggers are not used as the
-- default date: they fall on 9 fixed dates (2023-07-16 .. 2026-05-31), 880 of 4,317 predate the contract's
-- origination, and of the 2,539 stage-3 entries since 2023-08 that have one, 170 have it in the same or next
-- month (DS-042).
-- They are mapped as their own event types.
-- Cure: end of the probation period (`probationEnd`). The exposure stays in default during probation
-- (EBA/GL/2016/07 para. 71); `probationStart` coincides with the stage 3 -> stage 2 move in the stage history.
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
    SELECT contract_id, entity_id, CAST(event_date AS DATE), NULL, NULL, 'cure'
    FROM src.contract_default_cure_event
    WHERE event_type = 'probationEnd' AND CAST(event_date AS DATE) <= (SELECT reference_date FROM manifest)
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
