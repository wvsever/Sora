"""Contract tests for the Sora Regulatory Calculator API (schemas/calculator/openapi.yaml).

By default they run against the built-in stub. To check a real calculator implementation:

    SORA_CALCULATOR_URL=https://calculator.internal:8443 pytest python/tests/contract -m "not stub_only"

Tests marked `stub_only` check the stub's reference formulas and fault injection.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC = yaml.safe_load((Path(__file__).resolve().parents[3] / "schemas" / "calculator" / "openapi.yaml").read_text())
REGISTRY = Registry().with_resource("urn:sora-calculator", Resource(SPEC, specification=DRAFT202012))


def assert_schema(instance, schema_name: str):
    v = Draft202012Validator({"$ref": f"urn:sora-calculator#/components/schemas/{schema_name}"}, registry=REGISTRY)
    errors = sorted(v.iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors[:10])


@pytest.fixture(scope="module")
def base_url():
    url = os.environ.get("SORA_CALCULATOR_URL")
    if url:
        yield url.rstrip("/")
        return
    from sora_tools.calculator_stub import start_background
    server = start_background("formula")
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def call(base, method, path, body=None, key: str | None = "auto"):
    headers = {"Content-Type": "application/json"}
    if key == "auto":
        key = str(uuid.uuid4())
    if key:
        headers["Idempotency-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None), dict(e.headers)


def context(**kw):
    return {"requestId": str(uuid.uuid4()), "runId": "contract-test", "scenario": "actual", "projectionYear": 0,
            "referenceDate": "2026-06-30", "paramSet": "EU_CRR3_2025-01-01", "reportingCurrency": "EUR", **kw}


IRB_RECORDS = [
    {"recordId": "CORP-1", "exposureClass": "corporates_general", "approach": "airb", "ead": "1000000.00",
     "pd": "0.01", "lgd": "0.45", "maturityYears": "2.5"},
    {"recordId": "SME-1", "exposureClass": "corporates_sme", "approach": "firb", "ead": "250000.00",
     "pd": "0.02", "lgd": "0.40", "maturityYears": "3", "annualTurnoverEurMillions": "12.5"},
    {"recordId": "MTG-1", "exposureClass": "retail_residential_mortgage", "approach": "airb", "ead": "300000.00",
     "pd": "0.005", "lgd": "0.15", "maturityYears": "20"},
    {"recordId": "DEF-1", "exposureClass": "corporates_general", "approach": "airb", "ead": "100000.00",
     "pd": "1", "lgd": "0.60", "maturityYears": "1", "isDefaulted": True, "elbe": "0.50"},
]


def test_health_and_capabilities(base_url):
    status, body, _ = call(base_url, "GET", "/v1/health", key=None)
    assert status == 200 and body["status"] in ("ok", "degraded")
    status, caps, _ = call(base_url, "GET", "/v1/capabilities", key=None)
    assert status == 200
    assert_schema(caps, "Capabilities")
    assert "EU_CRR3_2025-01-01" in caps["paramSets"]
    assert caps["limits"]["maxRecordsPerSyncRequest"] <= caps["limits"]["maxRecordsPerRequest"]


def test_irb_response_contract(base_url):
    req = {"context": context(), "records": IRB_RECORDS}
    status, body, _ = call(base_url, "POST", "/v1/credit-risk/irb", req)
    assert status == 200, body
    assert_schema(body, "IrbResponse")
    assert body["meta"]["requestId"] == req["context"]["requestId"]
    assert [r["recordId"] for r in body["results"]] == [r["recordId"] for r in IRB_RECORDS]   # one per record, in order
    assert all(r["status"] == "ok" for r in body["results"])


def test_record_level_rejection(base_url):
    bad = {**IRB_RECORDS[0], "recordId": "BAD-1", "pd": 0.01}          # number instead of decimal string
    status, body, _ = call(base_url, "POST", "/v1/credit-risk/irb", {"context": context(), "records": [IRB_RECORDS[0], bad]})
    assert status == 200
    assert_schema(body, "IrbResponse")
    assert [r["status"] for r in body["results"]] == ["ok", "rejected"]
    assert body["results"][1]["errors"]


def test_zero_results_are_plain_decimals(base_url):
    """Zero values must still match the Decimal pattern (no exponent notation such as "0E-9")."""
    rec = {**IRB_RECORDS[3], "recordId": "DEF-ZERO", "lgd": "0.50", "elbe": "0.50"}      # K = max(0, LGD - ELBE) = 0
    status, body, _ = call(base_url, "POST", "/v1/credit-risk/irb", {"context": context(), "records": [rec]})
    assert status == 200
    assert_schema(body, "IrbResponse")
    assert body["results"][0]["status"] == "ok"


def test_deterministic(base_url):
    req = {"context": context(), "records": IRB_RECORDS}
    a = call(base_url, "POST", "/v1/credit-risk/irb", req)[1]
    b = call(base_url, "POST", "/v1/credit-risk/irb", {**req, "context": {**req["context"], "requestId": str(uuid.uuid4())}})[1]
    assert a["results"] == b["results"]
    assert a["meta"]["calculator"] == b["meta"]["calculator"]


def test_idempotent(base_url):
    key = str(uuid.uuid4())
    req = {"context": context(requestId=key), "records": IRB_RECORDS}
    a = call(base_url, "POST", "/v1/credit-risk/irb", req, key=key)
    b = call(base_url, "POST", "/v1/credit-risk/irb", req, key=key)
    assert a[0] == b[0] == 200 and a[1] == b[1]


def test_missing_idempotency_key_is_a_problem(base_url):
    status, body, headers = call(base_url, "POST", "/v1/credit-risk/irb", {"context": context(), "records": []}, key=None)
    assert status == 400
    assert headers.get("Content-Type", "").startswith("application/problem+json")
    assert_schema(body, "Problem")


def test_unsupported_param_set(base_url):
    status, body, _ = call(base_url, "POST", "/v1/credit-risk/irb",
                           {"context": context(paramSet="XX_UNKNOWN"), "records": IRB_RECORDS[:1]})
    assert status == 422
    assert_schema(body, "Problem")


def test_sa_contract(base_url):
    records = [
        {"recordId": "SA-CORP", "exposureClass": "corporates", "originalExposure": "1000.00", "creditQualityStep": 3},
        {"recordId": "SA-RRE", "exposureClass": "secured_by_immovable_property", "originalExposure": "500.00",
         "propertyType": "residential", "propertyValue": "1000.00"},
        {"recordId": "SA-OFF", "exposureClass": "retail", "originalExposure": "200.00", "offBalanceSheet": True,
         "ccfCategory": "medium_low_risk"},
    ]
    status, body, _ = call(base_url, "POST", "/v1/credit-risk/sa", {"context": context(), "records": records})
    assert status == 200
    assert_schema(body, "SaResponse")
    assert all(r["status"] == "ok" for r in body["results"])


def test_output_floor_contract(base_url):
    records = [{"recordId": "BANK-CPP-001", "reaModelled": "600.00", "reaStandardisedEquivalent": "1000.00",
                "reaOther": "100.00", "applicationDate": "2027-12-31"}]
    status, body, _ = call(base_url, "POST", "/v1/credit-risk/output-floor", {"context": context(), "records": records})
    assert status == 200
    assert_schema(body, "OutputFloorResponse")


def test_parameters_contract(base_url):
    req = {"context": context(scenario="adverse"), "parameters": ["pd12m_s1", "tr1_2", "lgd_s1"], "years": [0, 1, 2, 3],
           "macroPath": [{"variable": "real_gdp_growth", "country": "BE", "year": 1, "value": "-2.1"}],
           "records": [{"recordId": "LOANS|NFC|SME_CRE|BE", "level": "segment", "stage": "stage1",
                        "attributes": {"product_code": "CRE"}}]}
    status, body, _ = call(base_url, "POST", "/v1/parameters/credit", req)
    assert status == 200
    assert_schema(body, "ParameterResponse")
    values = body["results"][0]["values"]
    assert {(v["year"], v["parameter"]) for v in values} == {(y, p) for y in req["years"] for p in req["parameters"]}


def test_async_job(base_url):
    key = str(uuid.uuid4())
    status, job, headers = call(base_url, "POST", "/v1/jobs",
                                {"calculation": "irb", "request": {"context": context(), "records": IRB_RECORDS}}, key=key)
    assert status == 202
    assert_schema(job, "Job")
    status, job, _ = call(base_url, "GET", f"/v1/jobs/{job['jobId']}", key=None)
    assert status == 200 and job["status"] in ("queued", "running", "succeeded")
    if job["status"] == "succeeded":
        status, result, _ = call(base_url, "GET", f"/v1/jobs/{job['jobId']}/result", key=None)
        assert status == 200
        assert_schema(result, "IrbResponse")


# ------------------------------------------------------------------ stub-only (reference formulas)

@pytest.mark.stub_only
def test_stub_irb_reference_values(base_url):
    if os.environ.get("SORA_CALCULATOR_URL"):
        pytest.skip("stub only")
    body = call(base_url, "POST", "/v1/credit-risk/irb", {"context": context(), "records": IRB_RECORDS})[1]
    res = {r["recordId"]: r for r in body["results"]}
    # Basel IRB curve, corporate PD 1%, LGD 45%, M 2.5: RW 92.32% (CRR3: no 1.06 scaling).
    assert abs(float(res["CORP-1"]["riskWeight"]) - 0.9232) < 0.0005
    assert abs(float(res["CORP-1"]["rea"]) - float(res["CORP-1"]["riskWeight"]) * 1_000_000) < 0.01
    assert float(res["SME-1"]["correlation"]) < float(res["CORP-1"]["correlation"])     # SME adjustment
    assert res["MTG-1"]["correlation"] == "0.150000000" and res["MTG-1"]["maturityAdjustment"] == "1.000000000"
    assert res["DEF-1"]["riskWeight"] == "1.250000000"                                   # (LGD - ELBE) * 12.5
    assert res["CORP-1"]["expectedLoss"] == "4500.00"


@pytest.mark.stub_only
def test_stub_output_floor_phase_in(base_url):
    if os.environ.get("SORA_CALCULATOR_URL"):
        pytest.skip("stub only")
    records = [{"recordId": "E1", "reaModelled": "600.00", "reaStandardisedEquivalent": "1000.00",
                "applicationDate": "2027-12-31"}]
    r = call(base_url, "POST", "/v1/credit-risk/output-floor", {"context": context(), "records": records})[1]["results"][0]
    assert r["floorFactor"] == "0.600000000" and r["treaFloored"] == "600.00" and r["floorBinding"] is False


def test_stub_fault_injection():
    from sora_tools.calculator_stub import start_background
    server = start_background("faults")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        seen_fault = False
        for _ in range(12):
            key = str(uuid.uuid4())
            req = {"context": context(requestId=key), "records": [*IRB_RECORDS[:1], {**IRB_RECORDS[0], "recordId": "X-REJECT"}]}
            status, body, headers = call(base, "POST", "/v1/credit-risk/irb", req, key=key)
            if status in (429, 503):
                seen_fault = True
                assert "Retry-After" in headers
                status, body, _ = call(base, "POST", "/v1/credit-risk/irb", req, key=key)   # retry, same key
            assert status == 200
            assert [r["status"] for r in body["results"]] == ["ok", "rejected"]
        assert seen_fault
    finally:
        server.shutdown()
