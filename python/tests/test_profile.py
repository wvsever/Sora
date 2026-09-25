import yaml

from sora_tools.profile import profile_export


def test_profile_lists_codes_but_not_sensitive_or_numeric_values(tmp_path):
    export = tmp_path / "export"
    (export / "loans" / "entity_id=E1").mkdir(parents=True)
    (export / "loans" / "entity_id=E1" / "part-0.csv").write_text(
        "contract_id,stage,legal_name,amount,flag\n"
        "L1,stage1,Alice Smith,100.00,true\n"
        "L2,stage2,Bob Jones,250.50,false\n"
        "L3,stage1,Carol Diaz,100.00,\n")
    (export / "ref").mkdir()
    (export / "ref" / "country.csv").write_text("code,label\nBE,Belgium\nDE,Germany\n")
    out = tmp_path / "dict.yaml"
    doc = profile_export(export, out)
    assert set(doc["tables"]) == {"loans", "country"}
    loans = yaml.safe_load(out.read_text())["tables"]["loans"]
    assert loans["rows"] == 3 and loans["path"] == "loans/**/*.csv"
    cols = loans["columns"]
    assert set(cols["stage"]["codes"]) == {"stage1", "stage2"}
    assert "codes" not in cols["legal_name"]            # sensitive name: never listed
    assert "codes" not in cols["amount"]                # numeric: never listed
    assert cols["flag"]["null_rate"] == round(1 / 3, 4)
    assert "Alice" not in out.read_text() and "250.50" not in out.read_text()


def test_profile_no_codes(tmp_path):
    export = tmp_path / "export"
    (export / "t").mkdir(parents=True)
    (export / "t" / "part-0000.csv").write_text("stage\nstage1\n")
    doc = profile_export(export, tmp_path / "d.yaml", list_codes=False)
    assert "codes" not in doc["tables"]["t"]["columns"]["stage"]
