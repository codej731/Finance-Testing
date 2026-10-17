"""Fetchers and normalizers for yahooquery, yfinance and SEC EDGAR XBRL.

Fetchers talk to the network through RawCache. Normalizers are pure functions
that turn a raw response into canonical rows (docs/schema.md) and are where
every vendor field name is resolved, via data.schema.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from data.schema import (FIELDS, TABLES, apply_sign, exchange_for, native_ticker,
                         parse_component, resolve_alternatives, yfinance_name)
from data.transform import estimated_available_date

FUNDAMENTAL_COLUMNS = TABLES["fundamentals"].column_names
YAHOO_MODULES = ("summaryProfile", "price", "summaryDetail", "defaultKeyStatistics",
                 "financialData", "earningsTrend", "insiderTransactions")


# ---------------------------------------------------------------------------
# Raw response cache (schema §7): an optimization, not the record
# ---------------------------------------------------------------------------

class RawCache:
    def __init__(self, root: str | Path, ttl_hours: dict[str, float]):
        self.root = Path(root)
        self.ttl_hours = ttl_hours

    def _path(self, source: str, key: str, kind: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._=-]", "_", key)
        return self.root / source / f"{safe}.{'parquet' if kind == 'frame' else 'json'}"

    def fetch(self, source: str, key: str, fetch: Callable[[], Any], kind: str = "json") -> Any:
        """Return the cached response if fresh, else call `fetch` and cache it."""
        path = self._path(source, key, kind)
        ttl = self.ttl_hours.get(source, 24) * 3600
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            return pd.read_parquet(path) if kind == "frame" else json.loads(path.read_text())
        data = fetch()
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "frame":
            data.to_parquet(path, index=False)
        else:
            path.write_text(json.dumps(data, default=str))
        return data


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    """Vendor number → float, treating {}, '', 'Infinity' and NaN as missing."""
    if isinstance(value, dict):
        value = value.get("raw")
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(out) or np.isinf(out) else out


def _date(value: Any) -> date | None:
    if isinstance(value, dict):
        value = value.get("fmt") or value.get("raw")
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    try:
        return pd.Timestamp(str(value)).date()
    except ValueError:
        return None


def _get(d: dict, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _empty_fundamentals() -> pd.DataFrame:
    return pd.DataFrame(columns=FUNDAMENTAL_COLUMNS)


# ---------------------------------------------------------------------------
# yahooquery normalizers
# ---------------------------------------------------------------------------

def normalize_yahoo_security(symbol: str, modules: dict, as_of: date) -> dict:
    """securities row from quoteSummary modules."""
    price = modules.get("price") or {}
    profile = modules.get("summaryProfile") or {}
    mic, exchange = exchange_for(symbol, price.get("exchange"))
    return {
        "security_id": f"{mic}:{native_ticker(symbol)}",
        "ticker": native_ticker(symbol),
        "yahoo_symbol": symbol,
        "exchange": exchange,
        "cik": None,
        "name": price.get("longName") or price.get("shortName"),
        "country": profile.get("country"),
        "trading_currency": price.get("currency"),
        "reporting_currency": _get(modules, "financialData", "financialCurrency"),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "is_active": True,
        "first_seen": as_of,
        "last_seen": as_of,
    }


def normalize_yahoo_market(security_id: str, modules: dict, run_id: str, as_of: date) -> dict:
    price = modules.get("price") or {}
    detail = modules.get("summaryDetail") or {}
    stats = modules.get("defaultKeyStatistics") or {}
    return {
        "run_id": run_id,
        "security_id": security_id,
        "as_of": _date(price.get("regularMarketTime")) or as_of,
        "price": _num(price.get("regularMarketPrice")),
        "currency": price.get("currency"),
        "shares_outstanding": _num(stats.get("sharesOutstanding")),
        "market_cap": _num(price.get("marketCap")),
        "beta": _num(detail.get("beta")) or _num(stats.get("beta")),
        "avg_volume_3m": (_num(detail.get("averageVolume"))
                          or _num(price.get("averageDailyVolume3Month"))),
        "short_pct_float": _num(stats.get("shortPercentOfFloat")),
    }


ESTIMATE_HORIZONS = ("0q", "+1q", "0y", "+1y")


def normalize_yahoo_estimates(security_id: str, modules: dict, run_id: str,
                              as_of: date) -> list[dict]:
    fin = modules.get("financialData") or {}
    rows = []
    for item in _get(modules, "earningsTrend", "trend") or []:
        horizon = item.get("period")
        if horizon not in ESTIMATE_HORIZONS:
            continue
        eps = item.get("earningsEstimate") or {}
        trend = item.get("epsTrend") or {}
        revisions = item.get("epsRevisions") or {}
        up, down = _num(revisions.get("upLast30days")), _num(revisions.get("downLast30days"))
        analysts = _num(eps.get("numberOfAnalysts"))
        rows.append({
            "run_id": run_id,
            "security_id": security_id,
            "as_of": as_of,
            "horizon": horizon,
            "eps_mean": _num(eps.get("avg")) if _num(eps.get("avg")) is not None
            else _num(trend.get("current")),
            "eps_mean_7d_ago": _num(trend.get("7daysAgo")),
            "eps_mean_30d_ago": _num(trend.get("30daysAgo")),
            "eps_mean_60d_ago": _num(trend.get("60daysAgo")),
            "eps_mean_90d_ago": _num(trend.get("90daysAgo")),
            "revisions_up_30d": int(up) if up is not None else None,
            "revisions_down_30d": int(down) if down is not None else None,
            "revenue_mean": _num(_get(item, "revenueEstimate", "avg")),
            "num_analysts": int(analysts) if analysts is not None else None,
            "rating_mean": _num(fin.get("recommendationMean")),
            "target_price_mean": _num(fin.get("targetMeanPrice")),
        })
    return rows


_ROLE_RULES = (
    ("ceo", ("chief executive", "ceo")),
    ("cfo", ("chief financial", "cfo")),
    ("10pct_owner", ("beneficial owner", "10%")),
    ("director", ("director",)),
    ("officer", ("officer", "president", "chief", "counsel", "treasurer", "secretary",
                 "controller")),
)
_TRANSACTION_RULES = (
    ("open_market_buy", ("purchase",)),
    ("open_market_sale", ("sale",)),
    ("award", ("award", "grant")),
    ("option_exercise", ("exercise", "conversion")),
    ("gift", ("gift",)),
)


def classify_insider_role(relation: str | None) -> str:
    text = (relation or "").lower()
    for role, needles in _ROLE_RULES:
        if any(n in text for n in needles):
            return role
    return "other"


def classify_insider_transaction(text: str | None) -> str:
    t = (text or "").lower()
    for kind, needles in _TRANSACTION_RULES:
        if any(n in t for n in needles):
            return kind
    return "other"


def normalize_yahoo_insiders(security_id: str, modules: dict) -> list[dict]:
    rows = []
    for tx in _get(modules, "insiderTransactions", "transactions") or []:
        shares = _num(tx.get("shares"))
        value = _num(tx.get("value"))
        text = tx.get("transactionText") or ""
        price = None
        m = re.search(r"at price ([\d,.]+)(?:\s*-\s*([\d,.]+))? per share", text)
        if m:
            lo = float(m.group(1).replace(",", ""))
            hi = float(m.group(2).replace(",", "")) if m.group(2) else lo
            price = (lo + hi) / 2 or None
        if value is None and shares is not None and price is not None:
            value = shares * price
        rows.append({
            "security_id": security_id,
            "trade_date": _date(tx.get("startDate")),
            "filing_date": None,  # Yahoo does not report the filing date
            "insider_name": tx.get("filerName"),
            "insider_role": classify_insider_role(tx.get("filerRelation")),
            "transaction_type": classify_insider_transaction(text),
            "shares": abs(shares) if shares is not None else None,
            "price": price,
            "value": abs(value) if value is not None else None,
            "source": "yahooquery",
        })
    return rows


_YAHOO_PERIOD_TYPES = {"12M": "FY", "3M": "Q"}


def normalize_yahoo_statements(frame: pd.DataFrame, security_ids: dict[str, str],
                               regions: dict[str, str], lags: dict,
                               retrieved_at: datetime,
                               source: str = "yahooquery") -> pd.DataFrame:
    """yahooquery all_financial_data (flattened: one row per symbol/asOfDate/periodType)
    → canonical long rows. Vendor TTM rows are dropped; TTM is derived from quarters."""
    rows = []
    for rec in frame.to_dict("records"):
        period_type = _YAHOO_PERIOD_TYPES.get(rec.get("periodType"))
        sid = security_ids.get(rec.get("symbol"))
        if period_type is None or sid is None:
            continue
        period_end = _date(rec.get("asOfDate"))
        available = estimated_available_date(period_end, period_type, regions[sid], lags)
        for name, f in FIELDS.items():
            value = resolve_alternatives(lambda y: _num(rec.get(y)), f.yahoo)
            if value is None:
                continue
            rows.append((sid, name, period_type, period_end, None, None,
                         apply_sign(name, value), rec.get("currencyCode"), available, True,
                         source, retrieved_at))
    if not rows:
        return _empty_fundamentals()
    return pd.DataFrame(rows, columns=FUNDAMENTAL_COLUMNS)


def normalize_yahoo_history(frame: pd.DataFrame, security_ids: dict[str, str],
                            currencies: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Flattened yahooquery history → (prices_daily rows, corporate_actions rows).

    Note: Yahoo's `close` is split-adjusted (not dividend-adjusted); `adj_close`
    is split- and dividend-adjusted.
    """
    if frame.empty:
        return (pd.DataFrame(columns=TABLES["prices_daily"].column_names),
                pd.DataFrame(columns=TABLES["corporate_actions"].column_names))
    df = frame[frame["symbol"].isin(security_ids)].copy()
    df["security_id"] = df["symbol"].map(security_ids)
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.date
    df["currency"] = df["symbol"].map(currencies)
    df = df.rename(columns={"adjclose": "adj_close"})
    for col in ("open", "high", "low", "close", "adj_close", "volume"):
        if col not in df.columns:
            df[col] = np.nan
    df = df.dropna(subset=["close"]).drop_duplicates(["security_id", "date"], keep="last")
    prices = df[TABLES["prices_daily"].column_names].copy()
    prices["volume"] = prices["volume"].astype("Int64")

    return prices.reset_index(drop=True), normalize_yahoo_splits(df, security_ids)


def normalize_yahoo_splits(frame: pd.DataFrame, security_ids: dict[str, str]) -> pd.DataFrame:
    """Split events from any yahooquery history frame → corporate_actions rows.
    With interval > 1d the date is the start of the bar containing the split."""
    columns = TABLES["corporate_actions"].column_names
    if frame.empty or "splits" not in frame.columns:
        return pd.DataFrame(columns=columns)
    splits = frame[(frame["splits"].fillna(0) > 0) & frame["symbol"].isin(security_ids)]
    return pd.DataFrame({
        "security_id": splits["symbol"].map(security_ids),
        "date": pd.to_datetime(splits["date"], utc=True).dt.date,
        "action_type": "split", "ratio": splits["splits"].astype(float),
        "source": "yahooquery",
    }, columns=columns).reset_index(drop=True)


# ---------------------------------------------------------------------------
# yfinance normalizer (fallback for fundamentals)
# ---------------------------------------------------------------------------

def normalize_yfinance_statements(statements: dict[str, list[pd.DataFrame]], security_id: str,
                                  currency: str | None, region: str, lags: dict,
                                  retrieved_at: datetime) -> pd.DataFrame:
    """yfinance frames (rows = spaced field names, columns = period ends), keyed
    by period type ("FY" / "Q"), → canonical long rows."""
    rows = []
    for period_type, frames in statements.items():
        merged = pd.concat([f for f in frames if f is not None and not f.empty])
        if merged.empty:
            continue
        merged = merged[~merged.index.duplicated(keep="first")]
        for col in merged.columns:
            period_end = _date(col)
            available = estimated_available_date(period_end, period_type, region, lags)
            def lookup(y: str, col=col) -> float | None:
                label = yfinance_name(y)
                return _num(merged.at[label, col]) if label in merged.index else None

            for name, f in FIELDS.items():
                value = resolve_alternatives(lookup, f.yahoo)
                if value is None:
                    continue
                rows.append((security_id, name, period_type, period_end, None, None,
                             apply_sign(name, value), currency, available, True,
                             "yfinance", retrieved_at))
    if not rows:
        return _empty_fundamentals()
    return pd.DataFrame(rows, columns=FUNDAMENTAL_COLUMNS)


# ---------------------------------------------------------------------------
# SEC EDGAR companyfacts normalizer
# ---------------------------------------------------------------------------

EDGAR_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-Q", "10-Q/A",
                         "20-F", "20-F/A", "40-F", "40-F/A"})
QUARTER_DAYS = 91.31


def _quarters_in(days: int) -> int | None:
    """Duration in days → number of fiscal quarters (1–4), or None if irregular."""
    n = round(days / QUARTER_DAYS)
    return n if 1 <= n <= 4 and abs(days - n * QUARTER_DAYS) <= 20 else None


def _edgar_facts(companyfacts: dict, tag: str, unit_kind: str) -> pd.DataFrame:
    """All facts for one 'taxonomy:Tag' in the relevant unit, original filings only."""
    taxonomy, name = tag.split(":", 1)
    units = _get(companyfacts, "facts", taxonomy, name, "units") or {}
    if unit_kind == "shares":
        unit = "shares" if "shares" in units else None
    else:
        monetary = [u for u in units if "/" not in u and u != "shares" and u != "pure"]
        unit = "USD" if "USD" in monetary else (monetary[0] if monetary else None)
    if unit is None:
        return pd.DataFrame()
    df = pd.DataFrame(units[unit])
    if df.empty:
        return df
    # A filing cannot report actuals for a period ending after it was filed;
    # such facts are assumptions (e.g. a 2027 health-care cost trend rate).
    df = df[df["form"].isin(EDGAR_FORMS) & (df["end"] <= df["filed"])].copy()
    if "start" not in df.columns:
        df["start"] = None
    df["unit"] = unit
    df["end"] = pd.to_datetime(df["end"]).dt.date
    df["start"] = pd.to_datetime(df["start"]).dt.date
    df["filed"] = pd.to_datetime(df["filed"]).dt.date
    # Point-in-time: keep the first filing of each period (later filings
    # repeat it as a comparative, or restate it).
    df = df.sort_values("filed", kind="stable")
    return df.drop_duplicates(["start", "end"], keep="first").reset_index(drop=True)


def _edgar_calendar(companyfacts: dict) -> tuple[set[date], set[date], dict[str, date]]:
    """FY period ends, all report period ends, and accession → report period end."""
    fy_ends: set[date] = set()
    period_ends: set[date] = set()
    accn_end: dict[str, date] = {}
    for name, body in (_get(companyfacts, "facts", "us-gaap") or {}).items():
        for facts in (body.get("units") or {}).values():
            for f in facts:
                if (f.get("form") not in EDGAR_FORMS or not f.get("start")
                        or f["end"] > f.get("filed", "")):
                    continue
                start, end = _date(f["start"]), _date(f["end"])
                n = _quarters_in((end - start).days)
                if n is None:
                    continue
                period_ends.add(end)
                if n == 4:
                    fy_ends.add(end)
                accn = f.get("accn")
                if accn and (accn not in accn_end or end > accn_end[accn]):
                    accn_end[accn] = end
    return fy_ends, period_ends, accn_end


def _edgar_tag_series(companyfacts: dict, tag: str, field: str, fy_ends: set[date],
                      period_ends: set[date], accn_end: dict[str, date]) -> pd.DataFrame:
    """One tag → rows of (period_type, period_end, value, filed, unit)."""
    f = FIELDS[field]
    facts = _edgar_facts(companyfacts, tag, f.unit)
    out = []
    if facts.empty:
        return pd.DataFrame(columns=["period_type", "period_end", "value", "filed", "unit"])

    if tag.startswith("dei:"):
        # Cover-page values are dated at the cover date; attach to the report period.
        for r in facts.itertuples():
            pend = accn_end.get(r.accn)
            if pend is None:
                continue
            out.append(("Q", pend, r.val, r.filed, r.unit))
            if pend in fy_ends:
                out.append(("FY", pend, r.val, r.filed, r.unit))
    elif f.kind == "stock":
        for r in facts[facts["start"].isna()].itertuples():
            if r.end not in period_ends:
                continue
            out.append(("Q", r.end, r.val, r.filed, r.unit))
            if r.end in fy_ends:
                out.append(("FY", r.end, r.val, r.filed, r.unit))
    else:
        durations = facts[facts["start"].notna()].copy()
        durations["n"] = [_quarters_in((e - s).days) for s, e in
                          zip(durations["start"], durations["end"])]
        durations = durations[durations["n"].notna()]
        direct_q = set()
        for r in durations.itertuples():
            if r.n == 1:
                out.append(("Q", r.end, r.val, r.filed, r.unit))
                direct_q.add(r.end)
            elif r.n == 4:
                out.append(("FY", r.end, r.val, r.filed, r.unit))
        # 10-Q cash flows (and Q4 everywhere) are only reported year-to-date:
        # derive the discrete quarter as the difference of consecutive YTD values.
        # Averages (share counts) cannot be differenced.
        if f.unit == "currency":
            for _, ytd in durations.groupby("start"):
                by_n = {int(r.n): r for r in ytd.itertuples()}
                for n in (2, 3, 4):
                    cur, prev = by_n.get(n), by_n.get(n - 1)
                    if cur is None or prev is None or cur.end in direct_q:
                        continue
                    out.append(("Q", cur.end, cur.val - prev.val,
                                max(cur.filed, prev.filed), cur.unit))
                    direct_q.add(cur.end)
    return (pd.DataFrame(out, columns=["period_type", "period_end", "value", "filed", "unit"])
            .drop_duplicates(["period_type", "period_end"], keep="first"))


def _edgar_combine(parts: list[tuple[pd.DataFrame, bool]]) -> pd.DataFrame | None:
    """Sum tag series period by period. Required parts must all be present for a
    period; optional parts are added where reported."""
    required = [p for p, optional in parts if not optional]
    if any(p.empty for p in required):
        return None
    if required:
        index = required[0].index
        for p in required[1:]:
            index = index.intersection(p.index)
    else:
        index = parts[0][0].index
        for p, _ in parts[1:]:
            index = index.union(p.index)
    if index.empty:
        return None
    frames = [p.reindex(index) for p, _ in parts]
    return pd.DataFrame({
        "value": pd.concat([f["value"] for f in frames], axis=1).sum(axis=1, min_count=1),
        "filed": pd.concat([pd.to_datetime(f["filed"]) for f in frames], axis=1)
                   .max(axis=1).dt.date,
        "unit": pd.concat([f["unit"] for f in frames], axis=1).bfill(axis=1).iloc[:, 0],
    }, index=index)


def normalize_edgar_companyfacts(companyfacts: dict, security_id: str,
                                 retrieved_at: datetime) -> pd.DataFrame:
    """companyfacts JSON → canonical long rows with actual filing dates."""
    fy_ends, period_ends, accn_end = _edgar_calendar(companyfacts)
    frames = []
    for name, f in FIELDS.items():
        alt_frames = []
        for rank, alternative in enumerate(f.xbrl):
            combined = _edgar_combine(
                [(_edgar_tag_series(companyfacts, tag, name, fy_ends, period_ends, accn_end)
                  .set_index(["period_type", "period_end"]), optional)
                 for tag, optional in map(parse_component, alternative)])
            if combined is None:
                continue
            combined["rank"] = rank
            alt_frames.append(combined.reset_index())
        if not alt_frames:
            continue
        # Issuers switch tags over time, so priority is resolved per period.
        best = (pd.concat(alt_frames).sort_values("rank", kind="stable")
                .drop_duplicates(["period_type", "period_end"], keep="first"))
        best["field"] = name
        frames.append(best)
    if not frames:
        return _empty_fundamentals()
    df = pd.concat(frames, ignore_index=True)
    reporting_ccy = (df.loc[df["unit"] != "shares", "unit"].mode().iat[0]
                     if (df["unit"] != "shares").any() else None)
    return pd.DataFrame({
        "security_id": security_id,
        "field": df["field"],
        "period_type": df["period_type"],
        "period_end": df["period_end"],
        "fiscal_year": None,
        "fiscal_period": None,
        "value": [apply_sign(f, v) for f, v in zip(df["field"], df["value"].astype(float))],
        "currency": df["unit"].where(df["unit"] != "shares", reporting_ccy),
        "available_date": df["filed"],
        "available_date_estimated": False,
        "source": "edgar",
        "retrieved_at": retrieved_at,
    }, columns=FUNDAMENTAL_COLUMNS)


# ---------------------------------------------------------------------------
# Network clients
# ---------------------------------------------------------------------------

def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class YahooQueryClient:
    def __init__(self, cache: RawCache, batch_size: int = 100):
        self.cache = cache
        self.batch_size = batch_size

    def modules(self, symbols: list[str]) -> dict[str, dict]:
        from yahooquery import Ticker
        out: dict[str, dict] = {}
        for batch in _chunks(symbols, self.batch_size):
            data = self.cache.fetch("yahooquery", f"modules_{_digest(batch)}", lambda b=batch: Ticker(
                b, asynchronous=True).get_modules(list(YAHOO_MODULES)))
            out.update({s: d for s, d in data.items() if isinstance(d, dict)})
        return out

    def financials(self, symbols: list[str], frequency: str) -> pd.DataFrame:
        from yahooquery import Ticker

        def fetch(batch: list[str]) -> pd.DataFrame:
            df = Ticker(batch, asynchronous=True).all_financial_data(frequency=frequency)
            return df.reset_index() if isinstance(df, pd.DataFrame) else pd.DataFrame()

        frames = [self.cache.fetch("yahooquery", f"financials_{frequency}_{_digest(b)}",
                                   lambda b=b: fetch(b), kind="frame")
                  for b in _chunks(symbols, self.batch_size)]
        frames = [f for f in frames if not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def history(self, symbols: list[str], period: str, interval: str = "1d") -> pd.DataFrame:
        from yahooquery import Ticker

        def fetch(batch: list[str]) -> pd.DataFrame:
            df = Ticker(batch, asynchronous=True).history(period=period, interval=interval)
            if not isinstance(df, pd.DataFrame) or df.empty:
                return pd.DataFrame()
            df = df.reset_index()
            df["date"] = pd.to_datetime(df["date"].astype(str).str[:10])
            return df

        frames = [self.cache.fetch("yahooquery", f"history_{period}_{interval}_{_digest(b)}",
                                   lambda b=b: fetch(b), kind="frame")
                  for b in _chunks(symbols, self.batch_size)]
        frames = [f for f in frames if not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


class YFinanceClient:
    def __init__(self, cache: RawCache):
        self.cache = cache

    def statements(self, symbol: str) -> tuple[dict[str, list[pd.DataFrame]], str | None]:
        import yfinance as yf
        t = yf.Ticker(symbol)
        attrs = {"FY": ("financials", "balance_sheet", "cashflow"),
                 "Q": ("quarterly_financials", "quarterly_balance_sheet", "quarterly_cashflow")}
        out: dict[str, list[pd.DataFrame]] = {}
        for period_type, names in attrs.items():
            frames = []
            for name in names:
                def fetch(name=name) -> pd.DataFrame:
                    df = getattr(t, name)
                    df = df.copy()
                    df.columns = [str(pd.Timestamp(c).date()) for c in df.columns]
                    return df.reset_index(names="label")
                cached = self.cache.fetch("yfinance", f"{symbol}_{name}", fetch, kind="frame")
                frames.append(cached.set_index("label") if not cached.empty else cached)
            out[period_type] = frames
        info = self.cache.fetch("yfinance", f"{symbol}_currency",
                                lambda: {"financialCurrency": t.info.get("financialCurrency")})
        return out, info.get("financialCurrency")


class EdgarClient:
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

    def __init__(self, cache: RawCache, user_agent: str, request_interval: float = 0.15):
        if not user_agent:
            raise ValueError("SEC requires a User-Agent ('Name email'); set sec.user_agent")
        self.cache = cache
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self.request_interval = request_interval
        self._last_request = 0.0

    def _get_json(self, url: str) -> dict:
        import requests
        wait = self.request_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        resp = requests.get(url, headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def cik_map(self) -> dict[str, str]:
        """Exchange-native ticker (BRK.B) → zero-padded CIK."""
        data = self.cache.fetch("edgar", "company_tickers",
                                lambda: self._get_json(self.TICKERS_URL))
        return {row["ticker"].replace("-", ".").upper(): f"{int(row['cik_str']):010d}"
                for row in data.values()}

    def companyfacts(self, cik: str) -> dict:
        return self.cache.fetch("edgar", f"companyfacts_{cik}",
                                lambda: self._get_json(self.FACTS_URL.format(cik=cik)))


def _digest(items: list[str]) -> str:
    return hashlib.sha1(",".join(sorted(items)).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# FX providers (schema §6 currency reconciliation)
# ---------------------------------------------------------------------------

class StaticFx:
    """Fixed rates from config, e.g. {"USDCAD": 1.37}. Inverses are derived."""

    def __init__(self, rates: dict[str, float]):
        self.rates = {k.upper(): float(v) for k, v in (rates or {}).items()}

    def rate(self, from_ccy: str, to_ccy: str, on: date) -> float | None:
        if from_ccy == to_ccy:
            return 1.0
        if f"{from_ccy}{to_ccy}" in self.rates:
            return self.rates[f"{from_ccy}{to_ccy}"]
        if f"{to_ccy}{from_ccy}" in self.rates:
            return 1.0 / self.rates[f"{to_ccy}{from_ccy}"]
        return None


class YahooFx:
    """Daily closes of Yahoo FX pairs (USDCAD=X); last rate on or before the date."""

    MAX_STALENESS_DAYS = 7

    def __init__(self, cache: RawCache, fallback: StaticFx | None = None):
        self.cache = cache
        self.fallback = fallback
        self._series: dict[str, pd.Series] = {}

    def _pair(self, pair: str) -> pd.Series:
        if pair not in self._series:
            from yahooquery import Ticker

            def fetch() -> pd.DataFrame:
                df = Ticker(f"{pair}=X").history(period="max", interval="1d")
                if not isinstance(df, pd.DataFrame) or df.empty:
                    return pd.DataFrame(columns=["date", "close"])
                df = df.reset_index()
                df["date"] = pd.to_datetime(df["date"].astype(str).str[:10])
                return df[["date", "close"]]

            df = self.cache.fetch("yahooquery", f"fx_{pair}", fetch, kind="frame")
            self._series[pair] = (df.set_index(pd.to_datetime(df["date"]))["close"]
                                  .dropna().sort_index())
        return self._series[pair]

    def _lookup(self, pair: str, on: date) -> float | None:
        s = self._pair(pair)
        s = s[s.index <= pd.Timestamp(on)]
        if s.empty or (pd.Timestamp(on) - s.index[-1]).days > self.MAX_STALENESS_DAYS:
            return None
        return float(s.iloc[-1])

    def rate(self, from_ccy: str, to_ccy: str, on: date) -> float | None:
        if from_ccy == to_ccy:
            return 1.0
        direct = self._lookup(f"{from_ccy}{to_ccy}", on)
        if direct is not None:
            return direct
        inverse = self._lookup(f"{to_ccy}{from_ccy}", on)
        if inverse:
            return 1.0 / inverse
        return self.fallback.rate(from_ccy, to_ccy, on) if self.fallback else None


# ---------------------------------------------------------------------------
# Listings (input to the universe stage)
# ---------------------------------------------------------------------------

def list_us_symbols() -> list[str]:
    """Common stocks from NASDAQ Trader's nasdaqtraded.txt, as Yahoo symbols."""
    import requests
    resp = requests.get("https://www.nasdaqtrader.com/dynamic/symdir/nasdaqtraded.txt",
                        timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), sep="|")
    df = df[(df["Test Issue"] == "N") & (df["ETF"] == "N")].dropna(subset=["Symbol"])
    symbols = df["Symbol"].astype(str)
    return sorted(s.replace(".", "-") for s in symbols if "$" not in s)  # $ = preferreds


def list_tsx_symbols() -> list[str]:
    """TSX / TSX-V symbols from the TMX MOC-eligible list, as Yahoo symbols."""
    import requests
    resp = requests.get("https://www.tsx.com/files/trading/moc-eligible-stocks.txt",
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    out = []
    for line in resp.text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        if parts[0] in ("TSX", "TSXV"):
            exchange, symbol = parts[0], parts[1]
        elif parts[-1] in ("TSX", "TSXV"):
            exchange, symbol = parts[-1], parts[0]
        else:
            continue
        out.append(f"{symbol.replace('.', '-')}{'.TO' if exchange == 'TSX' else '.V'}")
    return sorted(set(out))

