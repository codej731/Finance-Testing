"""Factor calculations (v1.1).

Every derived metric in docs/schema.md §4 is computed here and nowhere else.
Fundamentals arrive as wide panels already converted to each security's
trading currency, so market-vs-fundamental ratios never mix currencies.

Two rules apply throughout:
- D-013: a missing debt, minority-interest or cash line counts as 0 when the
  balance sheet was reported (debt-free or cash-free companies would otherwise
  get no EV or ROIC).
- D-014: a ratio whose denominator is zero or negative is null, never ±inf.
  The only infinities are deliberate "best possible" values: interest coverage
  when interest is immaterial and cash runway when operations generate cash.
- D-017: cash runway measures operating burn only, and royalty / streaming
  companies (factors.royalty_companies) treat stream purchases as
  acquisitions, so their free cash flow is operating cash flow.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

YEAR_MATCH_DAYS = 20  # tolerance when looking for "the same date a year earlier"
INSIDER_ROLES = ("ceo", "cfo", "director", "officer")
COUNTRY_TAX_KEYS = {"United States": "US", "Canada": "CA"}


def _div(num, den) -> float:
    """num / den, null if either is missing or den <= 0 (D-014)."""
    if num is None or den is None or pd.isna(num) or pd.isna(den) or den <= 0:
        return np.nan
    return num / den


def _val(row: pd.Series | None, field: str) -> float:
    if row is None or field not in row.index:
        return np.nan
    v = row[field]
    return np.nan if pd.isna(v) else float(v)


def _row_near(rows: pd.DataFrame, target: date) -> pd.Series | None:
    """Row whose period_end is within YEAR_MATCH_DAYS of `target`."""
    if rows.empty:
        return None
    gaps = (pd.to_datetime(rows["period_end"]) - pd.Timestamp(target)).abs()
    i = gaps.idxmin()
    return rows.loc[i] if gaps[i] <= pd.Timedelta(days=YEAR_MATCH_DAYS) else None


# ---------------------------------------------------------------------------
# Base period
# ---------------------------------------------------------------------------

TTM_REQUIRED = ("revenue", "operating_income", "operating_cash_flow", "capex")


def base_period(fy: pd.DataFrame, ttm: pd.DataFrame) -> pd.Series | None:
    """Latest TTM row if it is newer than the latest FY and its core flows are
    complete; otherwise the latest FY. Flows and closing balances both come
    from this row, so a metric never mixes periods."""
    latest_fy = fy.sort_values("period_end").iloc[-1] if not fy.empty else None
    latest_ttm = ttm.sort_values("period_end").iloc[-1] if not ttm.empty else None
    if latest_ttm is not None and all(pd.notna(latest_ttm[f]) for f in TTM_REQUIRED):
        if latest_fy is None or latest_ttm["period_end"] > latest_fy["period_end"]:
            return latest_ttm
    return latest_fy


def _capital(row: pd.Series | None, missing_is_zero: bool) -> dict[str, float]:
    """Debt, minority interest, cash and invested capital for one balance-sheet row."""
    debt, mi, cash = (_val(row, "total_debt"), _val(row, "minority_interest"),
                      _val(row, "cash"))
    if missing_is_zero and not np.isnan(_val(row, "total_assets")):  # D-013
        debt, mi, cash = (0.0 if np.isnan(v) else v for v in (debt, mi, cash))
    ic = debt + _val(row, "total_equity") + mi - cash
    return {"debt": debt, "minority_interest": mi, "cash": cash, "invested_capital": ic}


# ---------------------------------------------------------------------------
# Per-security factors
# ---------------------------------------------------------------------------

def is_royalty(security: pd.Series, cfg) -> bool:
    """Listed in factors.royalty_companies, by security_id (XTSE:FNV) or ticker (FNV)."""
    listed = set(cfg.get("factors.royalty_companies", []) or [])
    return security.get("security_id") in listed or security.get("ticker") in listed


def fundamental_factors(fy: pd.DataFrame, q: pd.DataFrame, ttm: pd.DataFrame,
                        country: str | None, cfg, royalty: bool = False) -> dict[str, float]:
    """Quality, risk and fundamental inputs for one security."""
    base = base_period(fy, ttm)
    if base is None:
        return {}
    zero_debt = cfg.get("factors.missing_balance_is_zero", True)
    lo, hi = cfg.get("tax.clip", [0.0, 0.40])
    rates = cfg.get("tax.statutory_rates", {})
    statutory = rates.get(COUNTRY_TAX_KEYS.get(country, ""), rates.get("default", 0.25))

    revenue = _val(base, "revenue")
    ebit = _val(base, "operating_income")  # never pre-tax income (D-005)
    ebitda = ebit + _val(base, "depreciation_amortization")
    pretax, tax = _val(base, "pretax_income"), _val(base, "income_tax")
    tax_rate = (min(max(tax / pretax, lo), hi)
                if pretax > 0 and not np.isnan(tax) else statutory)
    nopat = ebit * (1 - tax_rate)

    close = _capital(base, zero_debt)
    balances = pd.concat([fy, q], ignore_index=True)
    opening_row = _row_near(balances, base["period_end"] - timedelta(days=365))
    opening = _capital(opening_row, zero_debt)
    ics = [v for v in (close["invested_capital"], opening["invested_capital"])
           if not np.isnan(v)]
    avg_ic = float(np.mean(ics)) if ics else np.nan

    ocf, capex = _val(base, "operating_cash_flow"), _val(base, "capex")
    # Royalty / streaming "capex" is buying new streams: an acquisition, not upkeep.
    fcf = ocf if royalty else ocf - capex
    net_income = _val(base, "net_income")
    cash = close["cash"]
    interest = _val(base, "interest_expense")
    immaterial = cfg.get("factors.immaterial_interest_pct_revenue", 0.001)

    # Margin history and growth from fiscal years only.
    hist = fy.sort_values("period_end").tail(5)
    margins = [_div(r["operating_income"], r["revenue"]) for _, r in hist.iterrows()]
    margins = [m for m in margins if not np.isnan(m)]
    latest_fy = fy.sort_values("period_end").iloc[-1] if not fy.empty else None
    fy_3y_ago = (_row_near(fy, latest_fy["period_end"] - timedelta(days=3 * 365))
                 if latest_fy is not None else None)
    rev_now, rev_then = _val(latest_fy, "revenue"), _val(fy_3y_ago, "revenue")

    if revenue > 0 and not np.isnan(interest) and interest < immaterial * revenue:
        coverage = np.inf  # immaterial interest: ranked top, not computed
    elif np.isnan(interest) and close["debt"] == 0:
        coverage = np.inf  # no debt, no interest line
    else:
        coverage = _div(ebit, interest)
    # Runway measures operating burn: a company whose operations generate cash is
    # self-funding even while it spends heavily on growth (D-017).
    if np.isnan(ocf):
        runway = np.nan
    elif ocf >= 0:
        runway = np.inf
    else:
        runway = _div(cash, capex - ocf)

    shares_now, shares_then = (_val(base, "shares_outstanding"),
                               _val(opening_row, "shares_outstanding"))
    ta, tl = _val(base, "total_assets"), _val(base, "total_liabilities")
    if ta > 0 and tl > 0:
        x1 = (_val(base, "current_assets") - _val(base, "current_liabilities")) / ta
        z2 = (6.56 * x1 + 3.26 * _val(base, "retained_earnings") / ta
              + 6.72 * ebit / ta + 1.05 * _val(base, "total_equity") / tl)
    else:
        z2 = np.nan

    return {
        "business_model": "royalty" if royalty else "operating",
        "base_period_type": base["period_type"], "base_period_end": base["period_end"],
        "currency": base["currency"],
        "revenue": revenue, "ebit": ebit, "ebitda": ebitda, "tax_rate": tax_rate,
        "nopat": nopat, "fcf": fcf, **close, "avg_invested_capital": avg_ic,
        "net_debt": close["debt"] - cash,
        "roic": _div(nopat, avg_ic),
        "operating_margin": _div(ebit, revenue),
        "margin_stability": float(np.std(margins, ddof=1)) if len(margins) >= 3 else np.nan,
        "fcf_conversion": _div(fcf, net_income),
        "revenue_growth_3y": ((rev_now / rev_then) ** (1 / 3) - 1
                              if rev_now > 0 and rev_then > 0 else np.nan),
        "interest_coverage": coverage,
        "net_debt_ebitda": _div(close["debt"] - cash, ebitda),
        "altman_z2": z2,
        "cash_runway_years": runway,
        "dilution_1y": _div(shares_now, shares_then) - 1,
    }


def market_cap(market: pd.Series) -> float:
    """Vendor market cap, else price × shares outstanding."""
    mcap = _val(market, "market_cap")
    return mcap if mcap > 0 else _val(market, "price") * _val(market, "shares_outstanding")


def valuation_factors(f: dict, mcap: float) -> dict[str, float]:
    if not f or np.isnan(mcap) or mcap <= 0:
        return {"market_cap": mcap}
    ev = mcap + f["debt"] + f["minority_interest"] - f["cash"]
    return {
        "market_cap": mcap, "enterprise_value": ev,
        "ev_ebit": _div(ev, f["ebit"]),
        "ev_ebitda": _div(ev, f["ebitda"]),
        "ev_sales": _div(ev, f["revenue"]),
        "fcf_yield": f["fcf"] / mcap,
    }


def estimate_factors(estimates: pd.DataFrame) -> dict[str, float]:
    """Mean 90-day EPS revision over the current and next fiscal year (D-008)."""
    revisions = []
    for _, r in estimates[estimates["horizon"].isin(["0y", "+1y"])].iterrows():
        old = r["eps_mean_90d_ago"]
        if pd.notna(r["eps_mean"]) and pd.notna(old) and old != 0:
            revisions.append((r["eps_mean"] - old) / abs(old))
    return {"eps_revision_90d": float(np.mean(revisions)) if revisions else np.nan}


def price_factors(prices: pd.DataFrame) -> dict[str, float]:
    p = prices.sort_values("date")
    out = {"drawdown_52w": np.nan, "momentum_12_1": np.nan}
    if len(p) >= 2:
        window = p.tail(252)
        out["drawdown_52w"] = _div(p["close"].iloc[-1], window["high"].max()) - 1
    if len(p) >= 253:
        adj = p["adj_close"].to_numpy()
        out["momentum_12_1"] = _div(adj[-22], adj[-253]) - 1
    return out


def insider_factors(insiders: pd.DataFrame, as_of: date, window_days: int,
                    mcap: float) -> dict[str, float]:
    """Officers' and directors' open-market buys minus sales over the window, as
    a share of market cap. Null when the vendor has no insider data at all."""
    if insiders.empty or np.isnan(mcap) or mcap <= 0:
        return {"insider_net_buy_pct": np.nan}
    recent = insiders[(pd.to_datetime(insiders["trade_date"]) > pd.Timestamp(
        as_of - timedelta(days=window_days)))
        & insiders["insider_role"].isin(INSIDER_ROLES)]
    buys = recent.loc[recent["transaction_type"] == "open_market_buy", "value"].sum()
    sales = recent.loc[recent["transaction_type"] == "open_market_sale", "value"].sum()
    return {"insider_net_buy_pct": (buys - sales) / mcap}


def compute_factors(securities: pd.DataFrame, market: pd.DataFrame,
                    estimates: pd.DataFrame, prices: pd.DataFrame,
                    insiders: pd.DataFrame, fy: pd.DataFrame, q: pd.DataFrame,
                    ttm: pd.DataFrame, as_of: date, cfg) -> pd.DataFrame:
    """One row per security: every factor plus the inputs that explain it."""
    window = cfg.get("factors.insider_window_days", 182)
    by = lambda df: dict(tuple(df.groupby("security_id"))) if not df.empty else {}  # noqa: E731
    fy_g, q_g, ttm_g = by(fy), by(q), by(ttm)
    est_g, px_g, ins_g = by(estimates), by(prices), by(insiders)
    mkt = market.set_index("security_id")
    empty = pd.DataFrame(columns=fy.columns)
    rows = []
    for _, sec in securities.iterrows():
        sid = sec["security_id"]
        f = fundamental_factors(fy_g.get(sid, empty), q_g.get(sid, empty),
                                ttm_g.get(sid, empty), sec.get("country"), cfg,
                                royalty=is_royalty(sec, cfg))
        mcap = market_cap(mkt.loc[sid]) if sid in mkt.index else np.nan
        rows.append({
            "security_id": sid, **f, **valuation_factors(f, mcap),
            **estimate_factors(est_g.get(sid, pd.DataFrame(columns=["horizon"]))),
            **price_factors(px_g.get(sid, pd.DataFrame(columns=["date"]))),
            **insider_factors(ins_g.get(sid, pd.DataFrame()), as_of, window, mcap),
        })
    return pd.DataFrame(rows)


def assign_segments(factors: pd.DataFrame, fx, as_of: date, cfg) -> pd.Series:
    """Peer segment per security (D-018): "royalty" (factors.royalty_companies),
    "pre_revenue" (base-period revenue below peers.pre_revenue_max, in the
    universe currency, or no revenue reported) or "producing"."""
    ref = cfg.get("universe.currency", "USD")
    limit = cfg.get("peers.pre_revenue_max", 0)

    def segment(r: pd.Series) -> str:
        if r.get("business_model") == "royalty":
            return "royalty"
        rate = fx.rate(r["currency"], ref, as_of) if isinstance(r.get("currency"), str) else None
        revenue = r.get("revenue", np.nan)
        if pd.isna(revenue) or (rate is not None and revenue * rate < limit):
            return "pre_revenue"
        return "producing"

    return factors.apply(segment, axis=1) if not factors.empty else pd.Series(dtype=object)
