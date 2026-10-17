"""Knockout filters: liquidity, size, data completeness, analyzable sector (v1.1, D-001, D-010).

Reads market_snapshot, securities and the run's validation issues; thresholds
come from config.yaml `universe`. This is the only stage that uses hard
cut-offs. Size and liquidity are compared in one currency (`universe.currency`)
so CAD and USD listings face the same bar.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from data.transform import FxProvider
from screen.factors import market_cap

COLUMNS = ["security_id", "passed", "reason", "market_cap_ref", "dollar_volume_ref"]


def apply_universe(securities: pd.DataFrame, market: pd.DataFrame,
                   validation_issues: pd.DataFrame, fx: FxProvider, as_of: date,
                   cfg) -> pd.DataFrame:
    """One row per security: passed, and the first knockout reason if not."""
    ref = cfg.get("universe.currency", "USD")
    excluded = set(cfg.get("universe.excluded_sectors", []))
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
            reason = f"market cap {mcap / 1e6:,.0f}M {ref} < minimum"
        elif not dollar_vol >= cfg.get("universe.min_avg_dollar_volume", 0):
            reason = f"avg daily value {dollar_vol / 1e3:,.0f}k {ref} < minimum"
        else:
            reason = None
        rows.append({"security_id": sid, "passed": reason is None, "reason": reason,
                     "market_cap_ref": mcap, "dollar_volume_ref": dollar_vol})
    return pd.DataFrame(rows, columns=COLUMNS)
