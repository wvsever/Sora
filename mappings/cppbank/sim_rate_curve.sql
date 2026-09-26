-- Interest rate curves at the reference date: reference/risk_free_curve (one curve per currency, tenors in years)
-- and reference/credit_spread_curve (sector sovereign / financial / other, rating band IG / HY, seniority).
WITH ref AS (SELECT reference_date AS d FROM manifest)
SELECT
    'RF-' || currency                                            AS curve_id,
    'risk_free'                                                  AS curve_type,
    currency,
    CAST(round(CAST(tenor AS DOUBLE) * 12) AS BIGINT)            AS tenor_months,
    CAST(rate AS DECIMAL(18,9))                                  AS rate,
    NULL                                                         AS sector,
    NULL                                                         AS rating_band,
    NULL                                                         AS seniority
FROM src.risk_free_curve
WHERE CAST(observation_date AS DATE) = (SELECT d FROM ref)
UNION ALL
SELECT
    'CS-' || currency || '-' || sector || '-' || rating_band || '-' || seniority,
    'credit_spread',
    currency,
    CAST(round(CAST(tenor AS DOUBLE) * 12) AS BIGINT),
    CAST(spread_rate AS DECIMAL(18,9)),
    CASE sector WHEN 'other' THEN 'corporate' ELSE sector END,
    CASE rating_band WHEN 'IG' THEN 'investment_grade' WHEN 'HY' THEN 'high_yield' END,
    CASE seniority WHEN 'Senior' THEN 'senior' WHEN 'NonSenior' THEN 'non_senior' END
FROM src.credit_spread_curve
WHERE CAST(observation_date AS DATE) = (SELECT d FROM ref)
