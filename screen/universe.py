"""Knockout filters: liquidity, size, data completeness, analyzable sector (v1.1, D-001, D-010).

Reads market_snapshot, securities and the run's validation issues; thresholds
come from config.yaml `universe`. This is the only stage that uses hard
cut-offs. Size and liquidity are compared in one currency (`universe.currency`)
so CAD and USD listings face the same bar.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

import re

from data.schema import CANADIAN_EXCHANGES
from data.transform import FxProvider
from screen.factors import market_cap

COLUMNS = ["security_id", "passed", "reason", "market_cap_ref", "dollar_volume_ref"]


def apply_universe(securities: pd.DataFrame, market: pd.DataFrame,
                   validation_issues: pd.DataFrame, fx: FxProvider, as_of: date,
                   cfg) -> pd.DataFrame:
    """One row per security: passed, and the first knockout reason if not."""
    ref = cfg.get("universe.currency", "USD")
    excluded = set(cfg.get("universe.excluded_sectors", []))
    countries = set(cfg.get("universe.countries", []) or [])
    dropped = (validation_issues[validation_issues["severity"] == "drop"]
               .groupby("security_id")["check_name"].apply(lambda s: ", ".join(sorted(set(s)))))
    mkt = market.set_index("security_id")
    rows = []
    for _, sec in securities.iterrows():
        sid = sec["security_id"]
        m = mkt.loc[sid] if sid in mkt.index else None
        rate = fx.rate(m["currency"], ref, as_of) if m is not None and pd.notna(
            m["currency"]) else None
        mcap = market_cap(m) * rate if m is not None and rate else float("nan")
        dollar_vol = (m["avg_volume_3m"] * m["price"] * rate
                      if m is not None and rate else float("nan"))
        quote_type = sec.get("quote_type")
        if sid in dropped.index:
            reason = f"validation: {dropped[sid]}"
        elif isinstance(quote_type, str) and quote_type != "EQUITY":
            reason = f"not common equity: {quote_type}"
        elif sec["sector"] in excluded:
            reason = f"excluded sector: {sec['sector']}"
        elif countries and isinstance(sec.get("country"), str) and sec["country"] not in countries:
            reason = f"outside countries: {sec['country']}"
        elif m is None or pd.isna(m["price"]):
            reason = "no market data"
        elif (pd.notna(m["as_of"]) and (as_of - pd.Timestamp(m["as_of"]).date()).days
              > cfg.get("universe.max_quote_age_days", 7)):
            reason = f"stale quote: last trade {pd.Timestamp(m['as_of']).date()}"
        elif rate is None:
            reason = f"no FX rate {m['currency']}{ref}"
        elif m["price"] < cfg.get("universe.min_price", 0):
            reason = f"price {m['price']:.2f} < {cfg.get('universe.min_price')}"
        elif not mcap >= cfg.get("universe.min_market_cap", 0):
            reason = (f"market cap {mcap / 1e6:,.0f}M {ref} < minimum" if mcap == mcap
                      else "market cap unknown")
        elif not dollar_vol >= cfg.get("universe.min_avg_dollar_volume", 0):
            reason = (f"avg daily value {dollar_vol / 1e3:,.0f}k {ref} < minimum"
                      if dollar_vol == dollar_vol else "avg daily value unknown")
        else:
            reason = None
        rows.append({"security_id": sid, "passed": reason is None, "reason": reason,
                     "market_cap_ref": mcap, "dollar_volume_ref": dollar_vol})
    out = pd.DataFrame(rows, columns=COLUMNS)
    return drop_duplicate_listings(out, securities)


_NAME_NOISE = re.compile(r"[^a-z0-9 ]|\b(inc|corp|corporation|ltd|limited|plc|co|company|"
                         r"holdings?|group|the|sa|nv|ag|lp|llc)\b")


def _company_key(name: str | None) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return " ".join(_NAME_NOISE.sub(" ", name.lower()).split()) or None


def drop_duplicate_listings(universe: pd.DataFrame, securities: pd.DataFrame) -> pd.DataFrame:
    """One listing per company among the passed securities (interlisted names
    such as TELUS on TSX and NYSE). Prefer the listing in the company's home
    country, then the most traded."""
    if not {"name", "exchange"} <= set(securities.columns):
        return universe
    meta = securities.set_index("security_id")
    passed = universe[universe["passed"]].copy()
    passed["key"] = passed["security_id"].map(meta["name"]).map(_company_key)
    home = passed["security_id"].map(lambda sid: (
        (meta.at[sid, "exchange"] in CANADIAN_EXCHANGES) == (meta.at[sid, "country"] == "Canada")
        if "country" in meta.columns else True))
    passed["_rank"] = list(zip(~home, -passed["dollar_volume_ref"].fillna(0)))
    out = universe.copy()
    for _, group in passed.dropna(subset=["key"]).groupby("key"):
        if len(group) < 2:
            continue
        keep = group.sort_values("_rank").iloc[0]["security_id"]
        losers = group.index[group["security_id"] != keep]
        out.loc[losers, "passed"] = False
        out.loc[losers, "reason"] = f"duplicate listing of {keep}"
    return out


def knockout_summary(universe: pd.DataFrame) -> pd.Series:
    """Knockout counts by reason category ("price", "excluded sector: …")."""
    reasons = universe.loc[~universe["passed"], "reason"].dropna()
    category = reasons.str.replace(r"\s*[\d.,]+[MkB]?\s.*$|\s*<.*$", "", regex=True)
    category = category.str.replace(r"^(stale quote|duplicate listing of|market cap"
                                    r"|avg daily value|price).*$", r"\1", regex=True)
    category = category.str.replace("duplicate listing of", "duplicate listing", regex=False)
    category = category.str.replace(r"^outside countries:.*$", "outside countries", regex=True)
    return category.value_counts()
