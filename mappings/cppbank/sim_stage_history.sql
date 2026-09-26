-- Month-end stage and allowance history from the impairment allowance ledger (all periods in the export).
--
-- principal_outstanding (optional SIM column): the ledger has gross carrying amounts for every loan only at
-- 2025-09, 2026-03 and 2026-06 (DS-046); in the other months only for part of them (mostly credit cards and
-- working-capital lines). The quarterly rate/principal snapshot (contract_rate_principal_history, loans, 2025-09 ..
-- 2026-06, dated within the month, e.g. 2025-12-30) has the principal of the others (at 2025-12 exactly the loans
-- without a ledger amount). It is mapped to the month end of its date. A loan first
-- drawn after that month's snapshot and by the month end (80 loans originated on 2025-12-31) has no snapshot row
-- yet: its outstanding principal is the original principal. Sora uses the column as a proxy for the gross
-- carrying amount of the prior-year Actual rows (CR_SCEN, CR_SECTOR) only where gross_carrying_amount is NULL.
WITH principal AS (
    SELECT contract_id,
           last_day(CAST(as_of_date AS DATE)) AS period_end,
           CAST(as_of_date AS DATE)           AS as_of,
           CAST(principal_outstanding AS DECIMAL(18,2)) AS principal
    FROM src.contract_rate_principal_history
    QUALIFY row_number() OVER (PARTITION BY contract_id, last_day(CAST(as_of_date AS DATE))
                               ORDER BY CAST(as_of_date AS DATE) DESC) = 1
),
snapshot AS (   -- snapshot date per month end
    SELECT period_end, max(as_of) AS as_of FROM principal GROUP BY period_end
),
drawn_after_snapshot AS (
    SELECT l.contract_id, s.period_end, CAST(l.original_principal AS DECIMAL(18,2)) AS principal
    FROM src.contract_loan l
    JOIN snapshot s
      ON CAST(coalesce(l.first_drawdown_date, l.origination_date) AS DATE) > s.as_of
     AND CAST(coalesce(l.first_drawdown_date, l.origination_date) AS DATE) <= s.period_end
)
SELECT
    i.contract_id                                        AS exposure_id,
    i.entity_id,
    last_day(CAST(i.accounting_period || '-01' AS DATE)) AS period_end,
    i.declared_stage_at_period_end                       AS stage,
    i.currency,
    CASE WHEN i.contract_table <> 'contract_commitment' THEN CAST(i.gross_carrying_amount AS DECIMAL(18,2)) END AS gross_carrying_amount,
    CASE WHEN i.contract_table = 'contract_commitment'  THEN CAST(i.gross_carrying_amount AS DECIMAL(18,2)) END AS off_balance_amount,
    CAST(i.closing_balance AS DECIMAL(18,2))             AS loss_allowance,
    CAST(i.utilisation_write_off AS DECIMAL(18,2))       AS write_off_in_period,
    CASE WHEN i.contract_table = 'contract_loan' THEN coalesce(p.principal, d.principal) END AS principal_outstanding
FROM src.impairment_allowance i
LEFT JOIN principal p
  ON p.contract_id = i.contract_id AND p.period_end = last_day(CAST(i.accounting_period || '-01' AS DATE))
LEFT JOIN drawn_after_snapshot d
  ON d.contract_id = i.contract_id AND d.period_end = last_day(CAST(i.accounting_period || '-01' AS DATE))
