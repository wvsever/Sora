-- Closing rates to EUR (the export quotes 1 unit of from_currency in to_currency).
-- Only the reference date is needed by the engine. History is kept for converting stage history.
SELECT
    from_currency                         AS currency,
    CAST(rate_date AS DATE)               AS rate_date,
    CAST(rate AS DECIMAL(18,9))           AS rate_to_reporting
FROM src.fx_rate
WHERE to_currency = 'EUR'
  AND rate_type = 'closing'
  AND CAST(rate_date AS DATE) >= (SELECT reference_date - INTERVAL 3 YEAR FROM manifest)
