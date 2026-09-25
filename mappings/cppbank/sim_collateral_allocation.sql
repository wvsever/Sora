-- Allocations to SIM exposures only (derivative and SFT margin allocations are excluded).
-- The export can hold several allocation rows per pair (effective_from history). The latest one on or before the reference date is kept.
WITH a AS (
    SELECT *,
        row_number() OVER (PARTITION BY contract_id, collateral_id
                           ORDER BY CAST(effective_from AS DATE) DESC NULLS LAST, row_seq DESC) AS rn
    FROM src.collateral_allocation
    WHERE effective_from IS NULL OR CAST(effective_from AS DATE) <= (SELECT reference_date FROM manifest)
)
SELECT
    a.contract_id                              AS exposure_id,
    a.collateral_id,
    CAST(a.allocated_amount AS DECIMAL(18,2))  AS allocated_amount,
    a.allocation_rank
FROM a
JOIN sim_exposure e ON e.exposure_id = a.contract_id
WHERE a.rn = 1
