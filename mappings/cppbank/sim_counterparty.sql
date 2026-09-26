-- Counterparties with EBA sector, NACE and SME indicator.
-- Financials are converted to EUR at the reference-date closing rate. No personal data is mapped.
WITH fx AS (
    SELECT currency, rate_to_reporting AS eur
    FROM sim_fx_rate WHERE rate_date = (SELECT reference_date FROM manifest)
    UNION SELECT 'EUR', 1  -- no-op if the export already has EUR->EUR
),
c AS (
    SELECT
        cp.*,
        CAST(annual_turnover AS DECIMAL(18,2)) * fx.eur AS turnover_eur,
        CAST(coalesce(balance_sheet_total, total_assets) AS DECIMAL(18,2)) * fx.eur AS assets_eur
    FROM src.counterparty cp
    LEFT JOIN fx ON fx.currency = coalesce(cp.turnover_currency, 'EUR')
)
SELECT
    counterparty_id,
    country_of_residence,
    esa2010_sector,
    CASE
        WHEN esa2010_sector = 'S.121'                THEN 'central_bank'
        WHEN esa2010_sector LIKE 'S.13%'             THEN 'general_government'
        WHEN esa2010_sector = 'S.122'                THEN 'credit_institution'
        WHEN esa2010_sector BETWEEN 'S.123' AND 'S.129' THEN 'other_financial'
        WHEN esa2010_sector = 'S.11'                 THEN 'non_financial_corporation'
        WHEN esa2010_sector IN ('S.14', 'S.15')      THEN 'household'
    END                                               AS eba_sector,
    -- NACE is only meaningful for non-financial corporations.
    CASE WHEN esa2010_sector = 'S.11' THEN nace_code END AS nace_code,
    is_natural_person,
    -- EU SME definition (2003/361/EC) on the counterparty's own figures.
    CASE WHEN esa2010_sector = 'S.11' THEN
        number_of_employees < 250 AND (turnover_eur <= 50000000 OR assets_eur <= 43000000)
    END                                               AS is_sme,
    CAST(round(turnover_eur, 2) AS DECIMAL(18,2))     AS annual_turnover_eur,
    CAST(round(assets_eur, 2) AS DECIMAL(18,2))       AS total_assets_eur,
    number_of_employees,
    coalesce(ultimate_parent_counterparty_id, counterparty_id) AS group_id,
    lei
FROM c
