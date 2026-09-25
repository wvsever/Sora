-- Unfunded credit protection received. Exposure-specific protection is kept only if the exposure is in SIM.
SELECT
    g.guarantee_id,
    g.entity_id,
    g.protection_form                           AS protection_type,
    g.guarantor_counterparty_id,
    e.exposure_id                               AS protected_exposure_id,
    CASE WHEN e.exposure_id IS NULL THEN g.protected_counterparty_id END AS protected_counterparty_id,
    g.currency,
    CAST(g.guaranteed_amount AS DECIMAL(18,2))  AS protected_amount,
    CAST(g.start_date AS DATE)                  AS start_date,
    CAST(g.end_date AS DATE)                    AS end_date
FROM src.guarantee_received g
LEFT JOIN sim_exposure e ON e.exposure_id = g.protected_contract_id
WHERE g.protected_contract_id IS NULL OR e.exposure_id IS NOT NULL
