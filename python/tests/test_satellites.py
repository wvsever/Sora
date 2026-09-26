"""Tests of sora_tools.satellites (sora-tools estimate-satellites).

* Recovery: rates generated from known coefficients (synthetic counts, no SIM) are recovered.
* Sign constraints: wrong-sign data never produce wrong-sign coefficients.
* On the reference SIM: determinism, the satellite file format, and an engine run with the estimated file
  (skipped if the engine is not built).
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import subprocess
from pathlib import Path

import pytest
import yaml

from sora_tools import satellites as sat
from sora_tools.cli import main

REPO = Path(__file__).resolve().parents[2]
SYNTHETIC = REPO / "tests" / "params" / "synthetic_satellites.csv"
SCENARIO = REPO / "tests" / "scenarios" / "test_eba2025.yaml"
ENGINE = Path(os.environ.get("SORA_ENGINE", REPO / "build" / "release" / "sora"))


# ----------------------------------------------------------------------------------------- synthetic data

def months(n: int, start_year: int = 2019) -> list[str]:
    return [f"{start_year + i // 12}-{i % 12 + 1:02d}" for i in range(n)]


def synthetic_drivers(ms: list[str]) -> dict:
    """Four drivers with different cycles, so that they are not collinear."""
    out = {}
    for i, m in enumerate(ms):
        out[m] = {
            "real_gdp": 1.5 + 2.5 * math.sin(2 * math.pi * i / 40),
            "unemployment_rate": 7.0 + 1.5 * math.cos(2 * math.pi * i / 27),
            "residential_property_prices": 3.0 + 6.0 * math.sin(2 * math.pi * i / 17 + 1.0),
            "commercial_property_prices": 2.0 + 7.0 * math.cos(2 * math.pi * i / 22 + 0.5),
        }
    return out


def synthetic_counts(ms, drivers, truth: dict, at_risk: int = 200_000, seed: int = 7,
                     trend: float = 0.0) -> dict:
    """{(portfolio, 'pd_perf', month): [events, at_risk]}: the monthly hazard h with 1 - (1 - h)^12 =
    expit(alpha + beta . x + trend * t), events drawn from a normal approximation of the binomial."""
    rng = random.Random(seed)
    counts = {}
    for p, (alpha, betas) in sorted(truth.items()):
        for t, m in enumerate(ms):
            z = alpha + trend * t / 12
            for c, b in betas.items():
                z += b * drivers[m][sat.driver_of(c, p)]
            p12 = 1 / (1 + math.exp(-z))
            h = 1 - (1 - p12) ** (1 / 12)
            k = round(at_risk * h + math.sqrt(at_risk * h * (1 - h)) * rng.gauss(0, 1))
            counts[(p, "pd_perf", m)] = [max(0, k), at_risk]
    return counts


TRUTH = {
    "HH_HOUSE": (-4.0, {"beta_gdp": -0.08, "beta_unemployment": 0.12, "beta_property": -0.015}),
    "NFC_SME_CRE": (-3.5, {"beta_gdp": -0.14, "beta_unemployment": 0.08, "beta_property": -0.02}),
    "HH_CONS": (-3.2, {"beta_gdp": -0.10, "beta_unemployment": 0.15}),
    "NFC_SME_OTHER": (-3.6, {"beta_gdp": -0.12, "beta_unemployment": 0.06}),
}


# ----------------------------------------------------------------------------------------- recovery

@pytest.mark.parametrize("trend", [0.0, 0.15])
def test_recovers_known_coefficients(trend):
    ms = months(84)
    drivers = synthetic_drivers(ms)
    counts = synthetic_counts(ms, drivers, TRUTH, trend=trend)
    res = sat.estimate(counts, drivers, portfolios=tuple(TRUTH), trend=True)
    for p, (_, betas) in TRUTH.items():
        eq = res["report"]["equations"][p]
        assert eq["estimated"] and eq["r2_within"] > 0.8, (p, eq["r2_within"])
        for c in sat.COEFFICIENTS:
            true = betas.get(c, 0.0)
            got = res["coefficients"][p][c]
            if c == "beta_property" and p not in sat.PROPERTY_PORTFOLIOS:
                assert got == 0.0 and eq["coefficients"][c]["source"] == "not_estimated"
                continue
            # window averaging of a logit-linear hazard is slightly non-linear: 15% (or 0.004) tolerance
            assert abs(got - true) <= max(0.15 * abs(true), 0.004), (p, c, got, true)
            d = eq["coefficients"][c]
            assert d["se"] > 0 and abs(d["estimate"] - true) <= 4 * d["se"] + 0.1 * abs(true), (p, c, d)


def test_single_driver_proxy_recovery():
    """Cycle-index proxy: only GDP is identified; beta_gdp = cycle slope / gdp_per_cycle."""
    ms = months(60)
    cycle = {m: 0.15 - (0.5 * (1 - math.cos(2 * math.pi * i / 30)) if 18 <= i < 48 else 0.0)
             for i, m in enumerate(ms)}
    drivers = {m: {"real_gdp": 1.5 + 5.0 * c} for m, c in cycle.items()}
    truth = {"HH_CONS": (-3.0, {"beta_gdp": -0.06}), "NFC_SME_OTHER": (-3.4, {"beta_gdp": -0.06})}
    res = sat.estimate(synthetic_counts(ms, drivers, truth, trend=0.1), drivers, portfolios=tuple(truth))
    assert res["report"]["drivers_available"] == ["real_gdp"]
    for p in truth:
        assert res["coefficients"][p]["beta_gdp"] == pytest.approx(-0.06, rel=0.15)
        assert res["coefficients"][p]["beta_unemployment"] == 0.0


def test_thin_portfolio_takes_pooled_slopes():
    ms = months(72)
    drivers = synthetic_drivers(ms)
    counts = synthetic_counts(ms, drivers, {p: TRUTH[p] for p in ("HH_CONS", "NFC_SME_OTHER")})
    counts.update(synthetic_counts(ms, drivers, {"OFC": (-9.0, {"beta_gdp": -0.1})}, at_risk=500, seed=3))
    res = sat.estimate(counts, drivers, portfolios=("HH_CONS", "NFC_SME_OTHER", "OFC", "CB"), min_events=30)
    pooled = res["report"]["pooled"]["coefficients"]
    for p in ("OFC", "CB"):
        eq = res["report"]["equations"][p]
        assert not eq["estimated"] and eq["default_events"] < 30
        assert res["coefficients"][p]["beta_gdp"] == pooled["beta_gdp"]["estimate"]
        assert eq["coefficients"]["beta_gdp"]["source"] == "pooled"


# ----------------------------------------------------------------------------------------- sign constraints

def test_sign_constraints_pooled_active_set():
    """Data where PDs fall when unemployment rises: the pooled unemployment slope is fixed at 0, and no
    portfolio coefficient comes out with the wrong sign."""
    ms = months(72)
    drivers = synthetic_drivers(ms)
    wrong = {p: (a, {**b, "beta_unemployment": -0.1}) for p, (a, b) in TRUTH.items()}
    res = sat.estimate(synthetic_counts(ms, drivers, wrong), drivers, portfolios=tuple(TRUTH))
    assert "beta_unemployment" in res["report"]["pooled"]["fixed_at_zero_by_sign_constraint"]
    for p, cs in res["coefficients"].items():
        for c, v in cs.items():
            assert v * sat.EXPECTED_SIGN[c] >= 0, (p, c, v)
        assert cs["beta_unemployment"] == 0.0
        d = res["report"]["equations"][p]["coefficients"]["beta_unemployment"]
        assert d["estimate"] < 0 and d["source"] in ("pooled_sign_fallback", "shrunk")   # shrunk fully onto 0
        assert cs["beta_gdp"] < 0


def test_sign_fallback_for_one_portfolio():
    ms = months(72)
    drivers = synthetic_drivers(ms)
    truth = dict(TRUTH)
    truth["HH_CONS"] = (-3.2, {"beta_gdp": 0.10, "beta_unemployment": 0.15})       # GDP up -> PD up: wrong sign
    res = sat.estimate(synthetic_counts(ms, drivers, truth), drivers, portfolios=tuple(truth))
    d = res["report"]["equations"]["HH_CONS"]["coefficients"]["beta_gdp"]
    assert d["estimate"] > 0 and d["source"] == "pooled_sign_fallback"
    assert res["coefficients"]["HH_CONS"]["beta_gdp"] == res["report"]["pooled"]["coefficients"]["beta_gdp"]["estimate"]
    assert res["coefficients"]["HH_CONS"]["beta_gdp"] < 0


# ----------------------------------------------------------------------------------------- inputs and format

def test_macro_history_periods(tmp_path):
    f = tmp_path / "hist.csv"
    f.write_text("scenario,variable,measure,unit,key,tenor,sector,year,value\n"
                 "historical,real_gdp,growth,pct,EU,,,2023,0.4\n"
                 "historical,real_gdp,growth,pct,BE,,,2023,9.9\n"
                 "adverse,real_gdp,growth,pct,EU,,,2023,-5\n"
                 "historical,real_gva,growth,pct,EU,,C,2023,7\n")
    d = sat.load_macro_history(f, "EU")
    assert len(d) == 12 and d["2023-01"] == {"real_gdp": 0.4} and d["2023-12"] == {"real_gdp": 0.4}
    g = tmp_path / "q.csv"
    g.write_text("variable,key,period,value\nunemployment_rate,EU,2024-Q2,6.1\nreal_gdp,EU,2024-05,1.2\n")
    d = sat.load_macro_history(g, "EU")
    assert sorted(d) == ["2024-04", "2024-05", "2024-06"]
    assert d["2024-05"] == {"unemployment_rate": 6.1, "real_gdp": 1.2}


def test_fmt():
    assert sat.fmt(-0.0000001) == "0" and sat.fmt(0.0) == "0" and sat.fmt(-0.1234567) == "-0.123457"
    assert sat.fmt(0.8) == "0.8"


@pytest.fixture(scope="module")
def estimated(reference_sim, testdata, tmp_path_factory):
    """Estimate on the reference SIM with the cycle-index proxy, twice (for the determinism test)."""
    runs = []
    for i in range(2):
        out = tmp_path_factory.mktemp(f"sat{i}")
        rc = main(["estimate-satellites", str(reference_sim), "--cycle-index",
                   str(testdata / "reference" / "macro_cycle_index.csv"), "--prior", str(SYNTHETIC),
                   "-o", str(out / "satellites.csv"), "--report", str(out / "report.json")])
        assert rc == 0
        runs.append(out)
    return runs


def test_deterministic(estimated):
    a, b = estimated
    for name in ("satellites.csv", "report.json"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_format_matches_synthetic_file(estimated):
    out = estimated[0] / "satellites.csv"
    with open(SYNTHETIC) as f, open(out) as g:
        syn, est = list(csv.DictReader(f)), list(csv.DictReader(g))
    assert out.read_text().splitlines()[0] == SYNTHETIC.read_text().splitlines()[0]
    assert [r["portfolio"] for r in est] == [r["portfolio"] for r in syn]
    for r, s in zip(est, syn):
        for c in sat.COEFFICIENTS:
            float(r[c])
        assert r["lgd_property_sensitivity"] == sat.fmt(float(s["lgd_property_sensitivity"]))   # from --prior


def test_reference_fit_is_sensible(estimated):
    rep = json.loads((estimated[0] / "report.json").read_text())
    assert rep["drivers"]["source"] == "cycle_index_proxy" and "PROXY" in rep["drivers"]["warning"]
    assert rep["drivers_available"] == ["real_gdp"]
    pooled = rep["pooled"]["coefficients"]["beta_gdp"]
    assert pooled["estimate"] < 0 and pooled["se"] > 0 and rep["pooled"]["fixed_at_zero_by_sign_constraint"] == []
    for p, eq in rep["equations"].items():
        assert rep["output"][p]["beta_gdp"] < 0
        assert rep["output"][p]["beta_unemployment"] == 0 and rep["output"][p]["beta_property"] == 0
        if eq["estimated"]:
            assert 0 <= eq["r2_within"] <= 1 and eq["n"] > 0


@pytest.mark.skipif(not ENGINE.exists(), reason="C++ engine not built")
def test_engine_runs_with_estimated_satellites(estimated, reference_sim, tmp_path):
    cfg = yaml.safe_load(SCENARIO.read_text())
    cfg["satellites"] = str(estimated[0] / "satellites.csv")
    scen = tmp_path / "scenario.yaml"
    scen.write_text(yaml.safe_dump(cfg, sort_keys=False))
    r = subprocess.run([str(ENGINE), "run", str(reference_sim), "--scenario", str(scen), "-o", str(tmp_path / "out"),
                        "--base", str(REPO)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    with open(tmp_path / "out" / "parameters.csv") as f:
        rows = list(csv.DictReader(f))
    by = {(r["key"], r["scenario"], r["year"]): r for r in rows}
    derived = [k for k in by if k[1] == "adverse" and by[k]["source"] == "derived"]
    assert derived
    # beta_gdp < 0, other slopes 0: the adverse GDP path is below the baseline in years 1-2 (year 3 of the EBA
    # 2025 adverse rebounds above the baseline in some countries), so adverse PDs are higher there
    early = [k for k in derived if k[2] in ("1", "2")]
    assert early and all(float(by[k]["pd12m_s1"]) >= float(by[(k[0], "baseline", k[2])]["pd12m_s1"]) for k in early)
    higher = sum(float(by[k]["pd12m_s1"]) > float(by[(k[0], "baseline", k[2])]["pd12m_s1"]) for k in early)
    assert higher > len(early) / 2
