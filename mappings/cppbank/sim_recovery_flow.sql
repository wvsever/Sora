-- Post-default cash flows: recoveries and workout costs from the recovery ledger, write-offs from the event log.
WITH f AS (
    SELECT contract_id, entity_id, CAST(cash_flow_date AS DATE) AS flow_date,
           CASE cash_flow_type WHEN 'recovery' THEN 'recovery' WHEN 'workoutCost' THEN 'workout_cost' END AS flow_type,
           currency, abs(CAST(amount AS DECIMAL(18,2))) AS amount
    FROM src.contract_recovery_cashflow
    UNION ALL
    SELECT contract_id, entity_id, CAST(event_date AS DATE), 'write_off', currency, abs(CAST(amount AS DECIMAL(18,2)))
    FROM src.contract_event WHERE event_type = 'write_off'
)
SELECT contract_id AS exposure_id, entity_id, flow_date, flow_type, any_value(currency) AS currency, sum(amount) AS amount
FROM f
GROUP BY contract_id, entity_id, flow_date, flow_type
