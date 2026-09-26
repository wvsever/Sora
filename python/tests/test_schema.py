from sora_tools import docs
from sora_tools.schema import lint, load_schema


def test_schema_is_clean():
    assert lint(load_schema()) == []


def test_every_column_is_documented():
    schema = load_schema()
    for t in schema.tables.values():
        for c in t.columns.values():
            assert len(c.description) >= 10, f"{t.name}.{c.name}"
            assert c.example is not None, f"{t.name}.{c.name}"


def test_generated_reference_is_up_to_date(repo):
    """schemas/sim/SIM_REFERENCE.md is generated. Regenerate with `sora-tools schema markdown -o ...`."""
    committed = (repo / "schemas" / "sim" / "SIM_REFERENCE.md").read_text(encoding="utf-8")
    assert committed == docs.markdown(load_schema())


def test_llm_description_is_complete():
    schema = load_schema()
    text = docs.llm(schema)
    for t in schema.tables.values():
        assert f"TABLE {t.name}" in text
        for c in t.columns.values():
            assert f"- {c.name} [" in text
    for e in schema.enums:
        assert f"- {e}:" in text


def test_ddl_has_all_tables():
    schema = load_schema()
    ddl = docs.ddl(schema)
    assert ddl.count("CREATE TABLE") == len(schema.tables)
