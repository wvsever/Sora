-- One row per legal entity of the CPPBank group.
SELECT
    entity_id,
    parent_entity_id,
    lei,
    country,
    functional_currency,
    entity_type,
    accounting_consolidation_method       AS consolidation_method,
    CAST(ownership_pct_held_by_parent AS DECIMAL(18,9)) AS ownership_pct
FROM src.entity
