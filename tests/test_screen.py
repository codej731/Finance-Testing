from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from data.config import load_config
from data.ingest import StaticFx
from data.schema import FIELDS
from screen.factors import (assign_segments, base_period, compute_factors, estimate_factors,
                            insider_factors, market_cap, price_factors)
from screen.score import factor_percentiles, ranking, score
from screen.universe import apply_universe

CFG = load_config(Path(__file__).parent.parent / "config.yaml")
AS_OF = date(2026, 10, 17)


def fy_row(sid, end, ptype="FY", **fields):
    row = {"security_id": sid, "period_type": ptype, "period_end": date.fromisoformat(end),
           "fiscal_year": int(end[:4]), "fiscal_period": "FY", "currency": "CAD",
           "available_date": date.fromisoformat(end)}
    row.update({f: np.nan for f in FIELDS})
    row.update(fields)
    return row


PRODUCER = dict(revenue=1000.0, operating_income=200.0, depreciation_amortization=50.0,
                pretax_income=180.0, income_tax=45.0, net_income=135.0,
                operating_cash_flow=250.0, capex=100.0, total_assets=2000.0,
                total_liabilities=800.0, total_equity=1200.0, cash=100.0, total_debt=400.0,
                current_assets=500.0, current_liabilities=300.0, retained_earnings=600.0,
                interest_expense=20.0, shares_outstanding=100.0)


def panels(sid="X", **overrides):
    years = []
    for i, end in enumerate(["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31",
                             "2025-12-31"]):
        f = dict(PRODUCER, revenue=PRODUCER["revenue"] * (1 + 0.1 * i),
                 operating_income=PRODUCER["operating_income"] * (1 + 0.1 * i))
        if end == "2025-12-31":
            f.update(overrides)
        years.append(fy_row(sid, end, **f))
    fy = pd.DataFrame(years)
    empty = pd.DataFrame(columns=fy.columns)
    return fy, empty, empty


def factors_for(fy, q, ttm, mcap=1500.0, sid="X", ticker="X"):
    secs = pd.DataFrame([{"security_id": sid, "ticker": ticker, "country": "Canada"}])
    market = pd.DataFrame([{"security_id": sid, "market_cap": mcap, "price": 15.0,
                            "shares_outstanding": 100.0}])
    none = pd.DataFrame()
    return compute_factors(secs, market, none, none, none, fy, q, ttm, AS_OF, CFG).iloc[0]


# --- factors --------------------------------------------------------------

def test_producer_factors_follow_schema_formulas():
    f = factors_for(*panels())
    rev, ebit = 1400.0, 280.0
    assert f["tax_rate"] == pytest.approx(0.25)                 # 45 / 180
    assert f["operating_margin"] == pytest.approx(ebit / rev)
    ic = 400 + 1200 + 0 - 100                                    # MI missing → 0 (D-013)
    assert f["roic"] == pytest.approx(ebit * 0.75 / ic)          # same IC open and close
    assert f["enterprise_value"] == pytest.approx(1500 + 400 - 100)
    assert f["ev_ebit"] == pytest.approx(1800 / ebit)
    assert f["ev_ebitda"] == pytest.approx(1800 / (ebit + 50))
    assert f["fcf_yield"] == pytest.approx(150 / 1500)
    assert f["revenue_growth_3y"] == pytest.approx((1.4 / 1.1) ** (1 / 3) - 1)
    assert f["interest_coverage"] == pytest.approx(ebit / 20)
    assert f["cash_runway_years"] == np.inf                       # positive FCF
    x1, x2, x3, x4 = 200 / 2000, 600 / 2000, ebit / 2000, 1200 / 800
    assert f["altman_z2"] == pytest.approx(6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4)


def test_missing_debt_is_zero_when_balance_sheet_reported():
    f = factors_for(*panels(total_debt=np.nan))
    assert f["debt"] == 0 and f["enterprise_value"] == pytest.approx(1500 - 100)


def test_missing_cash_is_zero_when_balance_sheet_reported():
    f = factors_for(*panels(cash=np.nan))
    assert f["cash"] == 0 and f["enterprise_value"] == pytest.approx(1500 + 400)
    assert f["roic"] == pytest.approx(280 * 0.75 / np.mean([1600, 1500]))  # opening had cash


def test_missing_debt_stays_null_without_balance_sheet():
    f = factors_for(*panels(total_debt=np.nan, total_assets=np.nan))
    assert np.isnan(f["debt"]) and np.isnan(f["enterprise_value"])


def test_non_positive_denominators_are_null_not_infinite():
    explorer = dict(revenue=0.0, operating_income=-50.0, net_income=-60.0,
                    operating_cash_flow=-40.0, capex=10.0, depreciation_amortization=1.0,
                    interest_expense=np.nan, total_debt=0.0)
    f = factors_for(*panels(**explorer))
    for name in ("operating_margin", "ev_sales", "ev_ebit", "ev_ebitda", "fcf_conversion",
                 "net_debt_ebitda"):
        assert np.isnan(f[name]), name
    assert f["cash_runway_years"] == pytest.approx(100 / 50)    # cash / burn
    assert f["interest_coverage"] == np.inf                     # debt-free


def test_runway_counts_operating_burn_only():
    # Cash-generating operations funding a big project are self-funding...
    f = factors_for(*panels(operating_cash_flow=300.0, capex=5000.0))
    assert f["cash_runway_years"] == np.inf
    assert f["fcf_yield"] == pytest.approx(-4700 / 1500)       # FCF still counts the capex


def test_royalty_capex_is_an_acquisition():
    big_stream = dict(operating_cash_flow=300.0, capex=5000.0)
    f = factors_for(*panels(**big_stream), ticker="WPM")
    assert f["business_model"] == "royalty"
    assert f["fcf"] == 300 and f["fcf_yield"] == pytest.approx(300 / 1500)
    assert f["fcf_conversion"] == pytest.approx(300 / 135)
    # Ticker entries match on any exchange.
    f = factors_for(*panels(sid="XNYS:WPM", **big_stream), sid="XNYS:WPM", ticker="WPM")
    assert f["fcf"] == 300
    assert factors_for(*panels(**big_stream), ticker="ABX")["business_model"] == "operating"


def test_statutory_tax_when_pretax_loss():
    f = factors_for(*panels(pretax_income=-10.0, income_tax=1.0))
    assert f["tax_rate"] == CFG.get("tax.statutory_rates")["CA"]


def test_immaterial_interest_ranks_top():
    f = factors_for(*panels(interest_expense=0.5))             # < 0.1% of revenue
    assert f["interest_coverage"] == np.inf


def test_ttm_used_only_when_newer_and_complete():
    fy, q, _ = panels()
    ttm_row = fy_row("X", "2026-06-30", ptype="TTM", **PRODUCER)
    assert base_period(fy, pd.DataFrame([ttm_row]))["period_type"] == "TTM"
    ttm_row["capex"] = np.nan
    assert base_period(fy, pd.DataFrame([ttm_row]))["period_type"] == "FY"


def test_dilution_uses_balance_a_year_earlier():
    f = factors_for(*panels(shares_outstanding=130.0))
    assert f["dilution_1y"] == pytest.approx(0.3)


def test_market_cap_falls_back_to_price_times_shares():
    assert market_cap(pd.Series({"market_cap": np.nan, "price": 2.0,
                                 "shares_outstanding": 50.0})) == 100.0


def test_estimate_revisions_average_both_years():
    est = pd.DataFrame({"horizon": ["0y", "+1y", "0q"], "eps_mean": [1.1, 2.0, 5.0],
                        "eps_mean_90d_ago": [1.0, 2.5, 1.0]})
    assert estimate_factors(est)["eps_revision_90d"] == pytest.approx((0.1 - 0.2) / 2)


def test_price_factors():
    n = 300
    close = np.linspace(100, 50, n)
    p = pd.DataFrame({"date": pd.bdate_range("2025-01-01", periods=n), "close": close,
                      "high": close + 1, "adj_close": close})
    out = price_factors(p)
    assert out["drawdown_52w"] == pytest.approx(50 / (close[-252] + 1) - 1)
    assert out["momentum_12_1"] == pytest.approx(close[-22] / close[-253] - 1)


def test_insider_net_buying_officers_only():
    ins = pd.DataFrame({
        "trade_date": [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2025, 1, 1)],
        "insider_role": ["ceo", "director", "10pct_owner", "cfo"],
        "transaction_type": ["open_market_buy", "open_market_sale", "open_market_buy",
                             "open_market_buy"],
        "value": [100.0, 30.0, 1000.0, 500.0]})
    assert insider_factors(ins, AS_OF, 182, 1000.0)["insider_net_buy_pct"] == pytest.approx(0.07)
    assert np.isnan(insider_factors(pd.DataFrame(), AS_OF, 182, 1000.0)["insider_net_buy_pct"])


# --- universe -------------------------------------------------------------

def test_universe_knockouts():
    secs = pd.DataFrame({"security_id": list("ABCDEFG"),
                         "sector": ["Basic Materials"] * 6 + ["Real Estate"]})
    market = pd.DataFrame({
        "security_id": list("ABCDEFG"), "currency": "CAD",
        "as_of": [AS_OF] * 5 + [date(2026, 8, 1), AS_OF],
        "price": [10.0, 0.5, 10.0, 10.0, 10.0, 10.0, 10.0],
        "market_cap": [5e8, 5e8, 5e7, 5e8, 5e8, 5e8, 5e8],
        "shares_outstanding": [5e7] * 7,
        "avg_volume_3m": [1e5, 1e6, 1e5, 1e3, 1e5, 1e5, 1e5]})
    issues = pd.DataFrame({"security_id": ["E"], "check_name": ["staleness"],
                           "severity": ["drop"]})
    out = apply_universe(secs, market, issues, StaticFx({"USDCAD": 1.4}), AS_OF,
                         CFG).set_index("security_id")
    assert out.loc["A", "passed"]
    assert out.loc["A", "market_cap_ref"] == pytest.approx(5e8 / 1.4)  # CAD → USD
    assert out.loc["B", "reason"].startswith("price")
    assert out.loc["C", "reason"].startswith("market cap")
    assert out.loc["D", "reason"].startswith("avg daily value")
    assert out.loc["E", "reason"] == "validation: staleness"
    assert out.loc["F", "reason"].startswith("stale quote")
    assert out.loc["G", "reason"].startswith("excluded sector")


# --- scoring --------------------------------------------------------------

def _secs(n_gold=5, n_silver=2):
    rows = [{"security_id": f"G{i}", "industry": "Gold", "sector": "Basic Materials",
             "name": f"G{i}", "exchange": "TSX"} for i in range(n_gold)]
    rows += [{"security_id": f"S{i}", "industry": "Silver", "sector": "Basic Materials",
              "name": f"S{i}", "exchange": "TSX"} for i in range(n_silver)]
    return pd.DataFrame(rows)


def test_percentiles_within_industry_with_sector_fallback():
    secs = _secs()
    f = pd.DataFrame({"security_id": secs["security_id"],
                      "roic": [0.1, 0.2, 0.3, 0.4, 0.5, 0.9, np.nan]})
    out = factor_percentiles(f, secs, "roic", "high", 5).set_index("security_id")
    assert out.loc["G0", "percentile"] == 0 and out.loc["G4", "percentile"] == 1
    assert out.loc["G0", "peer_group"] == "industry:Gold" and out.loc["G0", "peer_count"] == 5
    # Silver has one non-null value: ranked within the sector instead.
    assert out.loc["S0", "peer_group"] == "sector:Basic Materials"
    assert out.loc["S0", "percentile"] == 1.0 and out.loc["S0", "peer_count"] == 6
    assert np.isnan(out.loc["S1", "percentile"])            # null stays null (D-006)


def test_lower_is_better_and_infinite_best():
    secs = _secs(n_gold=5, n_silver=0)
    f = pd.DataFrame({"security_id": secs["security_id"],
                      "ev_ebit": [5.0, 10.0, 15.0, 20.0, 25.0],
                      "interest_coverage": [1.0, 5.0, np.inf, 3.0, 2.0]})
    ev = factor_percentiles(f, secs, "ev_ebit", "low", 5).set_index("security_id")
    assert ev.loc["G0", "percentile"] == 1.0                # cheapest is best
    cov = factor_percentiles(f, secs, "interest_coverage", "high", 5).set_index("security_id")
    assert cov.loc["G2", "percentile"] == 1.0


def test_composite_and_category_coverage():
    secs = _secs(n_gold=5, n_silver=0)
    f = pd.DataFrame({"security_id": secs["security_id"]})
    rng = np.random.default_rng(0)
    for name in CFG.get("scoring.factors"):
        f[name] = rng.normal(size=len(f))
    # G0 is an explorer: value has only fcf_yield (1 of 4 factors).
    f.loc[0, ["ev_ebit", "ev_ebitda", "ev_sales"]] = np.nan
    s = score(f, secs, CFG)
    cats = s[s["factor"].str.startswith("category:")].pivot(
        index="security_id", columns="factor", values="percentile")
    assert np.isnan(cats.loc["G0", "category:value"])
    assert not np.isnan(cats.loc["G1", "category:value"])
    comp = s[s["factor"] == "composite"].set_index("security_id")["percentile"]
    w = pd.Series(CFG.get("scoring.category_weights"))
    g1 = cats.loc["G1"].rename(lambda c: c.replace("category:", ""))
    assert comp["G1"] == pytest.approx((g1 * w).sum() / w.sum())
    g0 = cats.loc["G0"].rename(lambda c: c.replace("category:", "")).dropna()
    assert comp["G0"] == pytest.approx((g0 * w[g0.index]).sum() / w[g0.index].sum())
    # momentum is computed but not scored (D-003)
    assert "momentum_12_1" in set(s["factor"])
    r = ranking(s, secs)
    assert list(r.index) == list(comp.sort_values(ascending=False).index)


def test_composite_needs_min_categories():
    secs = _secs(n_gold=5, n_silver=0)
    f = pd.DataFrame({"security_id": secs["security_id"], "roic": [1.0, 2, 3, 4, 5],
                      "operating_margin": [1.0, 2, 3, 4, 5],
                      "margin_stability": [1.0, 2, 3, 4, 5]})
    s = score(f, secs, CFG)
    assert s.loc[s["factor"] == "composite", "percentile"].isna().all()


# --- segments (D-018) -----------------------------------------------------

def test_assign_segments():
    f = pd.DataFrame({
        "security_id": ["P", "E", "N", "R"],
        "business_model": ["operating", "operating", "operating", "royalty"],
        "revenue": [5e8, 2e6, np.nan, 1e6], "currency": ["CAD"] * 4})
    seg = assign_segments(f, StaticFx({"USDCAD": 1.4}), AS_OF, CFG)
    assert list(seg) == ["producing", "pre_revenue", "pre_revenue", "royalty"]


def test_segment_peers_fall_back_to_whole_segment():
    secs = pd.DataFrame({
        "security_id": ["G1", "G2", "G3", "G4", "G5", "R1", "R2", "R3"],
        "industry": ["Gold"] * 5 + ["Gold", "Gold", "Silver"],
        "sector": ["Basic Materials"] * 8})
    f = pd.DataFrame({"security_id": secs["security_id"],
                      "segment": ["producing"] * 5 + ["royalty"] * 3,
                      "ev_ebitda": [5.0, 6.0, 7.0, 8.0, 9.0, 20.0, 25.0, 30.0]})
    out = factor_percentiles(f, secs, "ev_ebitda", "low", 5, 3).set_index("security_id")
    assert out.loc["G1", "peer_group"] == "industry:Gold|producing"
    assert out.loc["G1", "peer_count"] == 5 and out.loc["G1", "percentile"] == 1.0
    # Royalty names are not ranked against cheaper miners: 20x is best among royalties.
    assert out.loc["R1", "peer_group"] == "segment:royalty"
    assert out.loc["R1", "percentile"] == 1.0 and out.loc["R3", "percentile"] == 0.0


def test_ranking_groups_by_segment():
    secs = _secs(n_gold=5, n_silver=0).assign(segment=["producing"] * 3 + ["pre_revenue"] * 2)
    f = pd.DataFrame({"security_id": secs["security_id"], "segment": secs["segment"]})
    for name in CFG.get("scoring.factors"):
        f[name] = np.arange(5, dtype=float)
    s = score(f, secs.drop(columns="segment"), CFG)
    comp = s[s["factor"] == "composite"].set_index("security_id")
    assert comp.loc["G0", "peer_group"] == "segment:producing" and comp.loc["G0", "peer_count"] == 3
    r = ranking(s, secs)
    assert list(r["segment"]) == ["producing"] * 3 + ["pre_revenue"] * 2
