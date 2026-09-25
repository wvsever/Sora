-- Collateral items. Margin collateral for derivatives (initial/variation margin) is kept. It is
-- allocated to derivative contracts, which are not SIM exposures, so the engine ignores it.
SELECT
    collateral_id,
    entity_id,
    CASE collateral_form
        WHEN 'immovable_property_residential' THEN 'residential_property'
        WHEN 'immovable_property_commercial'  THEN 'commercial_property'
        WHEN 'physical_other'                 THEN 'other_physical'
        ELSE collateral_form   -- cash, debt_security, equity_security, fund_unit, gold, life_policy, receivables, other
    END                                               AS collateral_type,
    currency,
    CAST(market_or_appraised_value AS DECIMAL(18,2))  AS market_value,
    CAST(valuation_date AS DATE)                      AS valuation_date,
    valuation_method,
    CAST(value_at_origination AS DECIMAL(18,2))       AS value_at_origination,
    CASE WHEN collateral_form LIKE 'immovable_property%' THEN property_country END AS property_country,
    lien_rank,
    CAST(prior_ranking_claims_not_held AS DECIMAL(18,2)) AS prior_ranking_claims,
    issuer_counterparty_id,
    provider_counterparty_id
FROM src.collateral
