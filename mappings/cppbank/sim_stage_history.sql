-- Month-end stage and allowance history from the impairment allowance ledger (all periods in the export).
SELECT
    i.contract_id                                        AS exposure_id,
    i.entity_id,
    last_day(CAST(i.accounting_period || '-01' AS DATE)) AS period_end,
    i.declared_stage_at_period_end                       AS stage,
    i.currency,
    CASE WHEN i.contract_table <> 'contract_commitment' THEN CAST(i.gross_carrying_amount AS DECIMAL(18,2)) END AS gross_carrying_amount,
    CASE WHEN i.contract_table = 'contract_commitment'  THEN CAST(i.gross_carrying_amount AS DECIMAL(18,2)) END AS off_balance_amount,
    CAST(i.closing_balance AS DECIMAL(18,2))             AS loss_allowance,
    CAST(i.utilisation_write_off AS DECIMAL(18,2))       AS write_off_in_period
FROM src.impairment_allowance i
