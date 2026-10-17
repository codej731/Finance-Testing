"""Pure transforms on canonical long-format fundamentals.

Long format (schema §2.3) is the storage shape. Analysis reads a wide panel:
one row per (security_id, period_type, period_end), one column per field,
after point-in-time filtering, source-priority resolution and, if requested,
currency conversion. TTM rows are derived from quarters here (schema §4).
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Protocol

import numpy as np
import pandas as pd

from data.schema import CURRENCY_FIELDS, FIELDS, SOURCES

# Period ends from different sources within this many days are the same period
# (52/53-week fiscal years make Yahoo and EDGAR dates differ by a few days).
PERIOD_MATCH_DAYS = 7

META_COLUMNS = ["security_id", "period_type", "period_end", "fiscal_year",
                "fiscal_period", "currency", "available_date"]


class FxProvider(Protocol):
    def rate(self, from_ccy: str, to_ccy: str, on: date) -> float | None:
        """Units of to_ccy per one from_ccy on (or just before) `on`."""


# ---------------------------------------------------------------------------
# Availability and fiscal labels
# ---------------------------------------------------------------------------

def estimated_available_date(period_end: date, period_type: str, region: str,
                             lags: dict) -> date:
    """Conservative availability estimate for sources without filing dates (§5)."""
    return period_end + timedelta(days=int(lags[region][period_type]))


def _fy_end_on_or_after(month: int, day: int, d: date) -> date:
    for year in (d.year, d.year + 1):
        candidate = date(year, month, min(day, calendar.monthrange(year, month)[1]))
        if candidate >= d - timedelta(days=PERIOD_MATCH_DAYS):
            return candidate
    raise AssertionError("unreachable")


def infer_fiscal_labels(long: pd.DataFrame) -> pd.DataFrame:
    """Fill fiscal_year / fiscal_period from period_end and the fiscal year-end.

    The fiscal year-end month/day is taken from each security's latest FY row
    (December if there are none). fiscal_year is the calendar year in which the
    fiscal year ends.
    """
    out = long.copy()
    out["period_end"] = pd.to_datetime(out["period_end"]).dt.date
    fy_anchor: dict[str, tuple[int, int]] = {}
    fy_rows = out[out["period_type"] == "FY"]
    for sid, ends in fy_rows.groupby("security_id")["period_end"]:
        latest = max(ends)
        fy_anchor[sid] = (latest.month, latest.day)

    labels: dict[tuple, tuple[int, str | None]] = {}
    keys = out[["security_id", "period_type", "period_end"]].drop_duplicates()
    for sid, ptype, pend in keys.itertuples(index=False):
        month, day = fy_anchor.get(sid, (12, 31))
        fy_end = _fy_end_on_or_after(month, day, pend)
        if ptype == "FY":
            labels[(sid, ptype, pend)] = (fy_end.year, "FY")
            continue
        q = 4 - round((fy_end - pend).days / 91.31)
        labels[(sid, ptype, pend)] = (fy_end.year, f"Q{q}" if 1 <= q <= 4 else None)
    looked_up = [labels[k] for k in
                 zip(out["security_id"], out["period_type"], out["period_end"])]
    out["fiscal_year"] = [y for y, _ in looked_up]
    out["fiscal_period"] = [p for _, p in looked_up]
    return out


# ---------------------------------------------------------------------------
# Point-in-time, source priority, currency
# ---------------------------------------------------------------------------

def point_in_time(long: pd.DataFrame, as_of: date | None) -> pd.DataFrame:
    """Keep only rows that could have been known on `as_of` (§5)."""
    if as_of is None:
        return long
    avail = pd.to_datetime(long["available_date"]).dt.date
    return long[avail <= as_of]


def align_period_ends(long: pd.DataFrame, priority: tuple[str, ...] = SOURCES) -> pd.DataFrame:
    """Snap near-identical period ends across sources onto one date.

    Within each (security_id, period_type), dates within PERIOD_MATCH_DAYS of
    each other are one period; the date reported by the highest-priority source
    wins.
    """
    if long.empty:
        return long
    out = long.copy()
    out["period_end"] = pd.to_datetime(out["period_end"]).dt.date
    rank = {s: i for i, s in enumerate(priority)}
    out["_rank"] = out["source"].map(rank).fillna(len(priority))
    mapping: dict[tuple, date] = {}
    keys = out.groupby(["security_id", "period_type", "period_end"])["_rank"].min().reset_index()
    for (sid, ptype), grp in keys.groupby(["security_id", "period_type"]):
        grp = grp.sort_values("period_end")
        cluster: list[tuple[date, float]] = []
        for pend, r in zip(grp["period_end"], grp["_rank"]):
            if cluster and (pend - cluster[-1][0]).days > PERIOD_MATCH_DAYS:
                _assign_cluster(mapping, sid, ptype, cluster)
                cluster = []
            cluster.append((pend, r))
        _assign_cluster(mapping, sid, ptype, cluster)
    out["period_end"] = [mapping[(s, t, p)] for s, t, p in
                         zip(out["security_id"], out["period_type"], out["period_end"])]
    return out.drop(columns="_rank")


def _assign_cluster(mapping: dict, sid: str, ptype: str, cluster: list) -> None:
    canonical = min(cluster, key=lambda c: (c[1], c[0]))[0]
    for pend, _ in cluster:
        mapping[(sid, ptype, pend)] = canonical


def resolve_sources(long: pd.DataFrame, priority: tuple[str, ...] = SOURCES) -> pd.DataFrame:
    """One row per (security, field, period_type, period_end): highest-priority source.

    A null from a higher-priority source does not mask a value from a lower one.
    """
    if long.empty:
        return long
    rank = {s: i for i, s in enumerate(priority)}
    work = long[long["value"].notna()].copy()
    work["_rank"] = work["source"].map(rank).fillna(len(priority))
    work = work.sort_values("_rank", kind="stable")
    key = ["security_id", "field", "period_type", "period_end"]
    return work.drop_duplicates(key, keep="first").drop(columns="_rank").reset_index(drop=True)


def convert_currency(long: pd.DataFrame, target: dict[str, str] | str,
                     fx: FxProvider) -> pd.DataFrame:
    """Convert currency-denominated fields at the period-end rate (§6).

    `target` is a currency code, or a {security_id: currency} map (normally the
    trading currency). Rows whose rate is unavailable become null.
    """
    out = long.copy()
    targets = (out["security_id"].map(target) if isinstance(target, dict)
               else pd.Series(target, index=out.index))
    needs = (out["field"].isin(CURRENCY_FIELDS) & targets.notna()
             & out["currency"].notna() & (out["currency"] != targets))
    if not needs.any():
        return out
    ends = pd.to_datetime(out.loc[needs, "period_end"]).dt.date
    pairs = list(zip(out.loc[needs, "currency"], targets[needs], ends))
    rates = {p: fx.rate(*p) for p in set(pairs)}
    factor = pd.Series([rates[p] for p in pairs], index=out.index[needs], dtype=float)
    out.loc[needs, "value"] = out.loc[needs, "value"] * factor  # missing rate → NaN
    out.loc[needs, "currency"] = targets[needs]
    return out


# ---------------------------------------------------------------------------
# Wide panel and TTM
# ---------------------------------------------------------------------------

def to_wide(long: pd.DataFrame) -> pd.DataFrame:
    """Pivot resolved long rows to one row per (security, period_type, period_end).

    available_date is the latest availability across the row's fields: the
    date the whole row was known.
    """
    if long.empty:
        return pd.DataFrame(columns=META_COLUMNS + list(FIELDS))
    index = ["security_id", "period_type", "period_end"]
    values = long.pivot_table(index=index, columns="field", values="value", aggfunc="first")
    values.columns.name = None
    meta = long.groupby(index).agg(
        fiscal_year=("fiscal_year", "first"),
        fiscal_period=("fiscal_period", "first"),
        currency=("currency", "first"),
        available_date=("available_date", "max"),
    )
    wide = meta.join(values).reset_index()
    for f in FIELDS:
        if f not in wide.columns:
            wide[f] = np.nan
    return wide[META_COLUMNS + list(FIELDS)].sort_values(index).reset_index(drop=True)


def build_ttm(wide_q: pd.DataFrame) -> pd.DataFrame:
    """TTM rows from four consecutive quarters (§4).

    Currency flows are summed, share-count flows averaged, balance-sheet items
    taken from the latest quarter. A missing quarter makes the flow null.
    """
    rows = []
    for sid, grp in wide_q[wide_q["period_type"] == "Q"].groupby("security_id"):
        grp = grp.sort_values("period_end").reset_index(drop=True)
        ends = pd.to_datetime(grp["period_end"])
        for i in range(3, len(grp)):
            span = (ends[i] - ends[i - 3]).days
            if not 250 <= span <= 290:  # four consecutive quarters
                continue
            window = grp.iloc[i - 3:i + 1]
            latest = grp.iloc[i]
            row = {"security_id": sid, "period_type": "TTM",
                   "period_end": latest["period_end"],
                   "fiscal_year": latest["fiscal_year"],
                   "fiscal_period": latest["fiscal_period"],
                   "currency": latest["currency"],
                   "available_date": window["available_date"].max()}
            for name, f in FIELDS.items():
                col = window[name]
                if f.kind == "stock":
                    row[name] = latest[name]
                elif col.isna().any():
                    row[name] = np.nan
                else:
                    row[name] = col.mean() if f.unit == "shares" else col.sum()
            rows.append(row)
    return pd.DataFrame(rows, columns=META_COLUMNS + list(FIELDS))


def fundamentals_panel(long: pd.DataFrame, period_type: str = "FY", *,
                       as_of: date | None = None,
                       priority: tuple[str, ...] = SOURCES,
                       currency: dict[str, str] | str | None = None,
                       fx: FxProvider | None = None) -> pd.DataFrame:
    """Analysis-ready wide panel for FY, Q or TTM."""
    work = point_in_time(long, as_of)
    if currency is not None:
        if fx is None:
            raise ValueError("currency conversion requested without an FX provider")
        work = convert_currency(work, currency, fx)
    source_type = "Q" if period_type == "TTM" else period_type
    work = work[work["period_type"] == source_type]
    work = resolve_sources(align_period_ends(work, priority), priority)
    wide = to_wide(work)
    return build_ttm(wide) if period_type == "TTM" else wide
