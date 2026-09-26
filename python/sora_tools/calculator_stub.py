"""Stub implementation of the Sora Regulatory Calculator API (schemas/calculator/openapi.yaml).

For tests and demos only. It is not a regulatory-grade calculator.

Modes:
  fixed    Deterministic constant results (engine and golden tests).
  formula  Simplified reference formulas: IRB risk-weight function (CRR Art. 153-154, CRR3 without the
           1.06 scaling factor), flat SA risk weights by class and CQS, output floor phase-in, and
           scenario multipliers for risk parameters.
  faults   Like `formula`, plus injected failures: the first call of every third idempotency key gets
           429 or 503, records whose id ends with "-REJECT" are rejected, and optional latency.

In any mode, ``reject`` (a regular expression, ``--reject``) rejects every record whose recordId it matches
(``re.search``), e.g. ``--reject '7\\|adverse\\|3$'`` for Sora's ``exposure_id|scenario|year`` record ids, and
``omit`` (parameter names, ``--omit lgd_s2,ccf``) leaves those parameters out of every /v1/parameters/credit
result (a calculator that does not model them), so the client's handling of missing values can be tested.

Risk parameters (/v1/parameters/credit) are deterministic in every mode: ``fixed`` returns the base value of each
parameter; ``formula`` scales it by the record's stage and ``eba_sector`` / ``is_sme`` attributes and, for
projection years, by a scenario multiplier (an attribute named like the parameter replaces the base value).

Run: ``sora-tools calculator-stub --mode formula --port 8080``
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from statistics import NormalDist
from typing import Any, Callable

API_VERSION = "0.1.0"
CALCULATOR = {"name": "sora-calculator-stub", "version": API_VERSION}
PARAM_SETS = ["EU_CRR3_2025-01-01"]
MAX_RECORDS = 10_000
MAX_SYNC_RECORDS = 5_000
_DEC = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_N = NormalDist()


class ProblemError(Exception):
    def __init__(self, status: int, title: str, detail: str = "", errors: list[dict] | None = None):
        super().__init__(detail or title)
        self.status, self.title, self.detail, self.errors = status, title, detail, errors or []

    def body(self) -> dict:
        return {"type": "about:blank", "title": self.title, "status": self.status, "detail": self.detail,
                "errors": self.errors}


# ---------------------------------------------------------------------------------------------
# Decimal helpers: all numbers cross the API as decimal strings.

def dec(value: Any, field: str) -> Decimal:
    if not isinstance(value, str) or not _DEC.match(value):
        raise ValueError(f"{field}: expected a decimal string, got {value!r}")
    return Decimal(value)


def fmt(x: float | Decimal, places: int = 9) -> str:
    q = Decimal(1).scaleb(-places)
    return format(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_EVEN), "f")   # never exponent notation ("0E-9")


def money(x: float | Decimal) -> str:
    return fmt(x, 2)


# ---------------------------------------------------------------------------------------------
# Calculations. Each takes one record dict and returns the result fields (without recordId/status).

def irb_formula(r: dict) -> dict:
    ead = dec(r["ead"], "ead")
    pd_in = float(dec(r["pd"], "pd"))
    lgd = float(dec(r["lgd"], "lgd"))
    m = float(dec(r["maturityYears"], "maturityYears"))
    cls = r["exposureClass"]
    if not 0 <= pd_in <= 1 or not 0 <= lgd <= 1:
        raise ValueError("pd and lgd must be in [0, 1]")
    if r.get("isDefaulted"):
        elbe = float(dec(r.get("elbe", r["lgd"]), "elbe"))
        k = max(0.0, lgd - elbe)
        return {"pdApplied": fmt(1), "lgdApplied": fmt(lgd), "capitalRequirementK": fmt(k),
                "riskWeight": fmt(k * 12.5), "rea": money(Decimal(str(k * 12.5)) * ead),
                "expectedLoss": money(Decimal(str(elbe)) * ead)}
    pd_ = pd_in if cls == "central_governments" else max(pd_in, 0.0005)   # CRR3 Art. 160(1) / 163(1) floor 0.05%
    pd_ = min(pd_, 0.999999)
    retail = cls.startswith("retail") or cls == "purchased_receivables_retail"
    if cls == "retail_residential_mortgage":
        rho = 0.15
    elif cls == "retail_qrre":
        rho = 0.04
    elif retail:
        f = (1 - math.exp(-35 * pd_)) / (1 - math.exp(-35))
        rho = 0.03 * f + 0.16 * (1 - f)
    else:
        f = (1 - math.exp(-50 * pd_)) / (1 - math.exp(-50))
        rho = 0.12 * f + 0.24 * (1 - f)
        s = r.get("annualTurnoverEurMillions")
        if s is not None and float(dec(s, "annualTurnoverEurMillions")) <= 50:
            s_ = min(max(float(Decimal(s)), 5.0), 50.0)
            rho -= 0.04 * (1 - (s_ - 5) / 45)                                  # Art. 153(4)
        if r.get("isLargeFinancialSectorEntity"):
            rho *= 1.25                                                         # Art. 153(2)
    k = lgd * _N.cdf((_N.inv_cdf(pd_) + math.sqrt(rho) * _N.inv_cdf(0.999)) / math.sqrt(1 - rho)) - pd_ * lgd
    ma = 1.0
    if not retail:
        b = (0.11852 - 0.05478 * math.log(pd_)) ** 2
        m_ = min(max(m, 1.0), 5.0)
        ma = (1 + (m_ - 2.5) * b) / (1 - 1.5 * b)
    k = max(k * ma, 0.0)
    rw = k * 12.5
    return {"pdApplied": fmt(pd_), "lgdApplied": fmt(lgd), "correlation": fmt(rho), "maturityAdjustment": fmt(ma),
            "capitalRequirementK": fmt(k), "riskWeight": fmt(rw), "rea": money(Decimal(str(rw)) * ead),
            "expectedLoss": money(Decimal(str(pd_ * lgd)) * ead)}


_SA_CQS = {  # risk weight by credit quality step 1..6, then unrated
    "central_governments": [0, 0.2, 0.5, 1.0, 1.0, 1.5, 1.0],
    "institutions": [0.2, 0.3, 0.5, 1.0, 1.0, 1.5, 0.4],
    "corporates": [0.2, 0.5, 0.75, 1.0, 1.5, 1.5, 1.0],
}
_SA_FLAT = {"retail": 0.75, "defaulted": 1.0, "subordinated_debt_equity": 1.5, "covered_bonds": 0.1,
            "mdb": 0.0, "international_organisations": 0.0, "adc_exposures": 1.5, "other_items": 1.0}
_CCF = {"full_risk": 1.0, "medium_risk": 0.5, "medium_low_risk": 0.2, "low_risk": 0.1, "other_commitment": 0.4}


def sa_formula(r: dict) -> dict:
    orig = dec(r["originalExposure"], "originalExposure")
    scra = dec(r.get("specificCreditRiskAdjustments", "0"), "specificCreditRiskAdjustments")
    ccf = _CCF.get(r.get("ccfCategory", "full_risk" if not r.get("offBalanceSheet") else "other_commitment"), 1.0)
    ev = max(orig - scra, Decimal(0)) * Decimal(str(ccf))
    cls = r["exposureClass"]
    cqs = r.get("creditQualityStep")
    ltv = None
    if cls in _SA_CQS:
        rw = _SA_CQS[cls][(cqs - 1) if cqs else 6]
        if cls == "corporates" and not cqs and r.get("isSme"):
            rw = 0.85
    elif cls == "secured_by_immovable_property":
        pv = r.get("propertyValue")
        ltv = float(orig / dec(pv, "propertyValue")) if pv and Decimal(pv) > 0 else None
        if r.get("propertyType") == "residential":
            rw = 0.2 if ltv is not None and ltv <= 0.55 else 0.35               # simplified loan-splitting
        else:
            rw = 0.6 if ltv is not None and ltv <= 0.55 else 1.0
    else:
        rw = _SA_FLAT.get(cls, 1.0)
    out = {"exposureValue": money(ev), "ccfApplied": fmt(ccf), "riskWeight": fmt(rw),
           "rea": money(ev * Decimal(str(rw)))}
    if ltv is not None:
        out["ltv"] = fmt(ltv)
    return out


_FLOOR = {2025: 0.5, 2026: 0.55, 2027: 0.6, 2028: 0.65, 2029: 0.7}  # CRR3 Art. 465(1) phase-in, then 72.5%


def floor_formula(r: dict, context: dict) -> dict:
    modelled = dec(r["reaModelled"], "reaModelled")
    sa = dec(r["reaStandardisedEquivalent"], "reaStandardisedEquivalent")
    other = dec(r.get("reaOther", "0"), "reaOther")
    year = int((r.get("applicationDate") or context["referenceDate"])[:4])
    x = Decimal(str(_FLOOR.get(year, 0.725 if year > 2029 else 0.5)))
    floored = max(modelled + other, x * sa + other)
    return {"floorFactor": fmt(x), "floorBinding": floored > modelled + other, "treaFloored": money(floored)}


_BASE = {"pd12m_s1": 0.005, "pd12m_s2": 0.08, "tr1_2": 0.05, "tr2_1": 0.25, "tr3_1": 0.01, "tr3_2": 0.05,
         "lgd_s1": 0.25, "lgd_s2": 0.28, "lgd_s3": 0.45, "lrlt_s2": 0.08, "ccf": 0.4, "pd_lifetime": 0.03,
         "pd_reg": 0.01, "lgd_reg": 0.3}
_STRESSABLE = {"pd12m_s1", "pd12m_s2", "tr1_2", "pd_lifetime", "pd_reg"}   # grow with stress; others constant


_SECTOR_FACTOR = {"household": 0.8, "non_financial_corporation": 1.2, "general_government": 0.5, "central_bank": 0.5,
                  "credit_institution": 0.7, "other_financial": 1.0}
_STAGE_FACTOR = {"stage1": 1.0, "stage2": 1.5, "stage3": 1.0, "poci": 1.0}


def _record_factor(r: dict, p: str) -> float:
    """Deterministic scaling of a base value by the record's attributes (PDs and transition rates only)."""
    if not p.startswith(("pd", "tr")):
        return 1.0
    attrs = r.get("attributes") or {}
    f = _SECTOR_FACTOR.get(attrs.get("eba_sector"), 1.0) * _STAGE_FACTOR.get(r.get("stage"), 1.0)
    return f * (1.1 if attrs.get("is_sme") is True else 1.0)


def params_formula(r: dict, request: dict) -> dict:
    scenario = request["context"].get("scenario", "actual")
    values = []
    for year in request["years"]:
        for p in request["parameters"]:
            attrs = r.get("attributes") or {}
            base = float(attrs[p]) if p in attrs else _BASE[p] * _record_factor(r, p)
            mult = 1.0
            if year > 0 and p in _STRESSABLE:
                mult = 1 + (0.4 if scenario == "adverse" else 0.05) * year
            elif year > 0 and p.startswith(("lgd", "lrlt")) and scenario == "adverse":
                mult = 1 + 0.1 * year
            values.append({"year": year, "parameter": p, "value": fmt(min(base * mult, 1.0)), "source": "model"})
    return {"values": values}


def _fixed(calc: str, r: dict, request: dict) -> dict:
    if calc == "irb":
        ead = dec(r["ead"], "ead")
        return {"pdApplied": r["pd"], "lgdApplied": r["lgd"], "capitalRequirementK": fmt(0.08),
                "riskWeight": fmt(1), "rea": money(ead), "expectedLoss": money(ead * Decimal("0.01"))}
    if calc == "sa":
        ev = dec(r["originalExposure"], "originalExposure")
        return {"exposureValue": money(ev), "ccfApplied": fmt(1), "riskWeight": fmt(1), "rea": money(ev)}
    if calc == "output-floor":
        m = dec(r["reaModelled"], "reaModelled")
        return {"floorFactor": fmt(0.725), "floorBinding": False, "treaFloored": money(m)}
    return {"values": [{"year": y, "parameter": p, "value": fmt(_BASE[p]), "source": "model"}
                       for y in request["years"] for p in request["parameters"]]}


# ---------------------------------------------------------------------------------------------

REQUIRED = {
    "irb": ["recordId", "exposureClass", "approach", "ead", "pd", "lgd", "maturityYears"],
    "sa": ["recordId", "exposureClass", "originalExposure"],
    "output-floor": ["recordId", "reaModelled", "reaStandardisedEquivalent"],
    "parameters-credit": ["recordId", "level"],
}
PATHS = {"/v1/credit-risk/irb": "irb", "/v1/credit-risk/sa": "sa",
         "/v1/credit-risk/output-floor": "output-floor", "/v1/parameters/credit": "parameters-credit"}


class CalculatorStub:
    def __init__(self, mode: str = "formula", latency: float = 0.0, reject: str | None = None,
                 omit: str | list[str] | None = None):
        if mode not in ("fixed", "formula", "faults"):
            raise ValueError(mode)
        self.mode, self.latency = mode, latency
        self.reject = re.compile(reject) if reject else None
        if isinstance(omit, str):
            omit = [x.strip() for x in omit.split(",") if x.strip()]
        self.omit = set(omit or [])
        self.lock = threading.Lock()
        self.responses: dict[str, tuple[int, dict]] = {}    # idempotency cache
        self.fault_seen: set[str] = set()
        self.jobs: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []             # (path, idempotency key), for tests

    def capabilities(self) -> dict:
        return {"calculator": CALCULATOR, "apiVersion": API_VERSION,
                "calculations": ["irb", "sa", "output-floor", "parameters-credit"], "paramSets": PARAM_SETS,
                "formats": ["application/json"],
                "limits": {"maxRecordsPerRequest": MAX_RECORDS, "maxRecordsPerSyncRequest": MAX_SYNC_RECORDS,
                           "maxConcurrentRequests": 8}}

    def calculate(self, calc: str, request: dict) -> dict:
        ctx = request.get("context") or {}
        for f in ("requestId", "runId", "referenceDate", "paramSet", "reportingCurrency"):
            if f not in ctx:
                raise ProblemError(400, "Invalid request", f"context.{f} is required")
        if ctx["paramSet"] not in PARAM_SETS:
            raise ProblemError(422, "Unsupported parameter set", ctx["paramSet"])
        records = request.get("records")
        if not isinstance(records, list):
            raise ProblemError(400, "Invalid request", "records must be an array")
        if len(records) > MAX_RECORDS:
            raise ProblemError(413, "Too many records", f"max {MAX_RECORDS}")
        if calc == "parameters-credit":
            for f in ("parameters", "years"):
                if not request.get(f):
                    raise ProblemError(400, "Invalid request", f"{f} is required")
            unknown = [p for p in request["parameters"] if p not in _BASE]
            if unknown:
                raise ProblemError(422, "Unknown parameters", ", ".join(unknown))
        results = []
        for i, r in enumerate(records):
            rid = r.get("recordId", f"#{i}")
            missing = [f for f in REQUIRED[calc] if f not in r]
            try:
                if missing:
                    raise ValueError(f"missing fields: {', '.join(missing)}")
                if self.mode == "faults" and str(rid).endswith("-REJECT"):
                    raise ValueError("rejected by fault injection")
                if self.reject and self.reject.search(str(rid)):
                    raise ValueError("rejected by the --reject pattern")
                if self.mode == "fixed":
                    out = _fixed(calc, r, request)
                elif calc == "irb":
                    out = irb_formula(r)
                elif calc == "sa":
                    out = sa_formula(r)
                elif calc == "output-floor":
                    out = floor_formula(r, ctx)
                else:
                    out = params_formula(r, request)
                if calc == "parameters-credit" and self.omit:
                    out = {**out, "values": [v for v in out["values"] if v["parameter"] not in self.omit]}
                results.append({"recordId": rid, "status": "ok", **out})
            except (ValueError, KeyError, InvalidOperation) as e:
                results.append({"recordId": rid, "status": "rejected",
                                "errors": [{"code": "STUB-INVALID", "message": str(e)}]})
        return {"meta": {"requestId": ctx["requestId"], "calculator": CALCULATOR, "paramSet": ctx["paramSet"]},
                "results": results}

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict]:
        if self.latency:
            time.sleep(self.latency)
        if method == "GET" and path == "/v1/health":
            return 200, {"status": "ok"}
        if method == "GET" and path == "/v1/capabilities":
            return 200, self.capabilities()
        m = re.match(r"^/v1/jobs/([^/]+)(/result)?$", path)
        if m:
            job = self.jobs.get(m.group(1))
            if not job:
                raise ProblemError(404, "Job not found", m.group(1))
            if method == "DELETE":
                job["status"] = "cancelled"
                return 204, {}
            if m.group(2):
                if job["status"] != "succeeded":
                    raise ProblemError(409, "Job not finished", job["status"])
                return 200, job["result"]
            return 200, {k: v for k, v in job.items() if k != "result"}
        if method != "POST" or (path not in PATHS and path != "/v1/jobs"):
            raise ProblemError(404, "Not found", path)

        key = headers.get("idempotency-key")
        if not key:
            raise ProblemError(400, "Missing Idempotency-Key header")
        with self.lock:
            self.calls.append((path, key))
            if key in self.responses:
                return self.responses[key]
            if self.mode == "faults" and key not in self.fault_seen and int(hashlib.sha256(key.encode()).hexdigest(), 16) % 3 == 0:
                self.fault_seen.add(key)
                code = 429 if int(hashlib.sha256(key.encode()).hexdigest(), 16) % 2 else 503
                raise ProblemError(code, "Injected fault", "retry with the same Idempotency-Key")
        try:
            request = json.loads(body or b"{}")
        except json.JSONDecodeError as e:
            raise ProblemError(400, "Invalid JSON", str(e)) from e
        if path == "/v1/jobs":
            calc = request.get("calculation")
            if calc not in REQUIRED:
                raise ProblemError(400, "Invalid calculation", str(calc))
            job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
            result = self.calculate(calc, request.get("request") or {})
            job = {"jobId": job_id, "calculation": calc, "status": "succeeded", "progress": 1.0, "result": result}
            self.jobs[job_id] = job
            response = (202, {k: v for k, v in job.items() if k != "result"})
        else:
            calc = PATHS[path]
            req_records = request.get("records") or []
            if len(req_records) > MAX_SYNC_RECORDS:
                raise ProblemError(413, "Too many records for a synchronous call", f"max {MAX_SYNC_RECORDS}; use /v1/jobs")
            response = (200, self.calculate(calc, request))
        with self.lock:
            self.responses[key] = response
        return response


def make_handler(stub: CalculatorStub) -> Callable:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # quiet
            pass

        def _serve(self, method: str):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                status, payload = stub.handle(method, self.path.split("?")[0], headers, body)
                ctype = "application/json"
            except ProblemError as e:
                status, payload, ctype = e.status, e.body(), "application/problem+json"
            data = b"" if status == 204 else json.dumps(payload).encode()
            self.send_response(status)
            if status == 202 and "jobId" in payload:
                self.send_header("Location", f"/v1/jobs/{payload['jobId']}")
            if status in (429, 503):
                self.send_header("Retry-After", "0")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def do_DELETE(self):
            self._serve("DELETE")

    return Handler


def serve(mode: str = "formula", host: str = "127.0.0.1", port: int = 8080, latency: float = 0.0,
          reject: str | None = None, omit: str | list[str] | None = None):
    """Start the stub server (blocking unless used via :func:`start_background`)."""
    stub = CalculatorStub(mode, latency, reject, omit)
    server = ThreadingHTTPServer((host, port), make_handler(stub))
    server.stub = stub
    return server


def start_background(mode: str = "formula", latency: float = 0.0, reject: str | None = None,
                     port: int = 0, omit: str | list[str] | None = None) -> ThreadingHTTPServer:
    server = serve(mode, port=port, latency=latency, reject=reject, omit=omit)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
