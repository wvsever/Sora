-- External issuer ratings. The latest rating per counterparty, source and scale, on or before the reference date.
-- CQS per the ECAI mapping (Implementing Regulation (EU) 2016/1799) for long-term scales.
WITH r AS (
    SELECT *,
        row_number() OVER (PARTITION BY counterparty_id, rating_source, rating_scale_id
                           ORDER BY CAST(rating_date AS DATE) DESC, row_seq DESC) AS rn
    FROM src.counterparty_rating
    WHERE CAST(rating_date AS DATE) <= (SELECT reference_date FROM manifest)
)
SELECT
    counterparty_id,
    CASE rating_source WHEN 'S&P' THEN 'sp' WHEN 'Moody''s' THEN 'moodys' WHEN 'Fitch' THEN 'fitch'
                       WHEN 'DBRS' THEN 'dbrs' ELSE 'other_ecai' END AS rating_source,
    rating_scale_id                                  AS rating_scale,
    rating_segment <> 'shortTerm'                    AS is_long_term,
    rating_grade,
    CASE WHEN rating_segment <> 'shortTerm' THEN
        CASE
            WHEN rating_grade IN ('AAA', 'AA', 'Aaa', 'Aa')  THEN 1
            WHEN rating_grade IN ('A')                       THEN 2
            WHEN rating_grade IN ('BBB', 'Baa')              THEN 3
            WHEN rating_grade IN ('BB', 'Ba')                THEN 4
            WHEN rating_grade IN ('B')                       THEN 5
            WHEN rating_grade IN ('CCC', 'Caa', 'CC', 'Ca', 'C', 'D', 'SD') THEN 6
        END
    END                                              AS credit_quality_step,
    CAST(rating_date AS DATE)                        AS rating_date
FROM r
WHERE rn = 1
