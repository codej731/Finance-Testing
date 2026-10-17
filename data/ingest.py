"""Fetchers and normalizers for yahooquery, yfinance and SEC EDGAR XBRL.

Fetchers talk to the network through RawCache. Normalizers are pure functions
that turn a raw response into canonical rows (docs/schema.md) and are where
every vendor field name is resolved, via data.schema.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
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

log = logging.getLogger(__name__)

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
        # {source: {"cached": n, "fetched": n}} for the run's freshness summary
        self.stats: dict[str, dict[str, int]] = {}

    def _count(self, source: str, outcome: str, n: int = 1) -> None:
        counts = self.stats.setdefault(source, {"cached": 0, "fetched": 0})
        counts[outcome] += n

    def _path(self, source: str, key: str, kind: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._=-]", "_", key)
        return self.root / source / f"{safe}.{'parquet' if kind == 'frame' else 'json.gz'}"

    def get(self, source: str, key: str, kind: str = "json") -> Any:
        """Fresh cached value, or None."""
        path = self._path(source, key, kind)
        if not path.exists() or time.time() - path.stat().st_mtime >= (
                self.ttl_hours.get(source, 24) * 3600):
            return None
        if kind == "frame":
            return pd.read_parquet(path)
        with gzip.open(path, "rt") as f:
            return json.load(f)

    def put(self, source: str, key: str, data: Any, kind: str = "json") -> None:
        path = self._path(source, key, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "frame":
            data.to_parquet(path, index=False)
        else:
            with gzip.open(path, "wt") as f:
                json.dump(data, f, default=str)

    def fetch(self, source: str, key: str, fetch: Callable[[], Any], kind: str = "json",
              cacheable: Callable[[Any], bool] | None = None) -> Any:
        """Return the cached response if fresh, else call `fetch` and cache it.
        JSON is gzip-compressed (SEC companyfacts are several MB each). A response
        failing `cacheable` (e.g. still holding transient errors) is not stored."""
        cached = self.get(source, key, kind)
        if cached is not None:
            self._count(source, "cached")
            return cached
        data = fetch()
        self._count(source, "fetched")
        if cacheable is None or cacheable(data):
            self.put(source, key, data, kind)
        return data


class Pacing:
    """Spacing and retries for remote calls: at least `delay` seconds between
    calls, and up to `max_retries` retries with exponential backoff."""

    def __init__(self, delay: float = 0.0, max_retries: int = 3, backoff: float = 10.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.delay, self.max_retries, self.backoff, self.sleep = delay, max_retries, backoff, sleep
        self._last = 0.0

    def wait(self) -> None:
        remaining = self.delay - (time.monotonic() - self._last)
        if self._last and remaining > 0:
            self.sleep(remaining)
        self._last = time.monotonic()

    def call(self, fn: Callable[[], Any], what: str) -> Any:
        for attempt in range(self.max_retries + 1):
            self.wait()
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 - network errors come in many types
                if attempt == self.max_retries:
                    raise
                pause = self.backoff * 2 ** attempt
                log.warning("%s failed (%s); retry %d/%d in %.0fs", what, e, attempt + 1,
                            self.max_retries, pause)
                self.sleep(pause)
        raise AssertionError("unreachable")


def is_transient_error(message: Any) -> bool:
    """yahooquery reports per-symbol failures as strings; "not found" is permanent,
    anything else (crumb, rate limit, timeout) is worth retrying."""
    return isinstance(message, str) and "not found" not in message.lower()


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
        "quote_type": price.get("quoteType"),
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


def _iso(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _edgar_facts(companyfacts: dict, tag: str, unit_kind: str) -> list[dict]:
    """All facts for one 'taxonomy:Tag' in the relevant unit, original filings only.

    Plain Python rather than pandas: there are thousands of small lists per
    filer and DataFrame overhead dominated the run time."""
    taxonomy, name = tag.split(":", 1)
    units = _get(companyfacts, "facts", taxonomy, name, "units") or {}
    if unit_kind == "shares":
        unit = "shares" if "shares" in units else None
    else:
        monetary = [u for u in units if "/" not in u and u != "shares" and u != "pure"]
        unit = "USD" if "USD" in monetary else (monetary[0] if monetary else None)
    if unit is None:
        return []
    # A filing cannot report actuals for a period ending after it was filed;
    # such facts are assumptions (e.g. a 2027 health-care cost trend rate).
    rows = [f for f in units[unit] if f.get("form") in EDGAR_FORMS
            and f["end"] <= f.get("filed", "")]
    # Point-in-time: keep the first filing of each period (later filings repeat
    # it as a comparative, or restate it). sort() is stable, like the original.
    rows.sort(key=lambda f: f["filed"])
    seen: set = set()
    out = []
    for f in rows:
        key = (f.get("start"), f["end"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"start": _iso(f.get("start")), "end": _iso(f["end"]), "val": f["val"],
                    "filed": _iso(f["filed"]), "accn": f.get("accn"), "unit": unit})
    return out


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
                start, end = _iso(f["start"]), _iso(f["end"])
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


Series = dict  # (period_type, period_end) -> (value, filed, unit)


def _edgar_tag_series(companyfacts: dict, tag: str, field: str, fy_ends: set[date],
                      period_ends: set[date], accn_end: dict[str, date]) -> Series:
    """One tag → {(period_type, period_end): (value, filed, unit)}; first entry wins."""
    f = FIELDS[field]
    facts = _edgar_facts(companyfacts, tag, f.unit)
    out: Series = {}

    def add(ptype: str, end: date, value: float, filed: date, unit: str) -> None:
        out.setdefault((ptype, end), (value, filed, unit))

    if tag.startswith("dei:"):
        # Cover-page values are dated at the cover date; attach to the report period.
        for r in facts:
            pend = accn_end.get(r["accn"])
            if pend is None:
                continue
            add("Q", pend, r["val"], r["filed"], r["unit"])
            if pend in fy_ends:
                add("FY", pend, r["val"], r["filed"], r["unit"])
    elif f.kind == "stock":
        for r in facts:
            if r["start"] is None and r["end"] in period_ends:
                add("Q", r["end"], r["val"], r["filed"], r["unit"])
                if r["end"] in fy_ends:
                    add("FY", r["end"], r["val"], r["filed"], r["unit"])
    else:
        durations = []
        for r in facts:
            if r["start"] is not None:
                n = _quarters_in((r["end"] - r["start"]).days)
                if n is not None:
                    durations.append((n, r))
        direct_q = set()
        for n, r in durations:
            if n == 1:
                add("Q", r["end"], r["val"], r["filed"], r["unit"])
                direct_q.add(r["end"])
            elif n == 4:
                add("FY", r["end"], r["val"], r["filed"], r["unit"])
        # 10-Q cash flows (and Q4 everywhere) are only reported year-to-date:
        # derive the discrete quarter as the difference of consecutive YTD values.
        # Averages (share counts) cannot be differenced.
        if f.unit == "currency":
            by_start: dict[date, dict[int, dict]] = {}
            for n, r in durations:
                by_start.setdefault(r["start"], {})[n] = r
            for by_n in by_start.values():
                for n in (2, 3, 4):
                    cur, prev = by_n.get(n), by_n.get(n - 1)
                    if cur is None or prev is None or cur["end"] in direct_q:
                        continue
                    add("Q", cur["end"], cur["val"] - prev["val"],
                        max(cur["filed"], prev["filed"]), cur["unit"])
                    direct_q.add(cur["end"])
    return out


def _edgar_combine(parts: list[tuple[Series, bool, int]]) -> Series | None:
    """Sum (or subtract, sign −1) tag series period by period. Required parts must
    all be present for a period; optional parts are applied where reported."""
    required = [p for p, optional, _ in parts if not optional]
    if any(not p for p in required):
        return None
    if required:
        keys = set(required[0])
        for p in required[1:]:
            keys &= set(p)
    else:
        keys = set().union(*(p for p, _, _ in parts))
    out: Series = {}
    for key in keys:
        total, filed, unit = 0.0, None, None
        for series, _, sign in parts:
            hit = series.get(key)
            if hit is None:
                continue
            total += sign * hit[0]
            filed = hit[1] if filed is None else max(filed, hit[1])
            unit = unit or hit[2]
        out[key] = (total, filed, unit)
    return out or None


def normalize_edgar_companyfacts(companyfacts: dict, security_id: str,
                                 retrieved_at: datetime) -> pd.DataFrame:
    """companyfacts JSON → canonical long rows with actual filing dates."""
    fy_ends, period_ends, accn_end = _edgar_calendar(companyfacts)
    rows = []
    for name, f in FIELDS.items():
        best: Series = {}
        # Issuers switch tags over time, so priority is resolved per period:
        # an earlier alternative wins wherever it has a value.
        for alternative in f.xbrl:
            combined = _edgar_combine(
                [(_edgar_tag_series(companyfacts, tag, name, fy_ends, period_ends, accn_end),
                  optional, sign) for tag, optional, sign in map(parse_component, alternative)])
            for key, hit in (combined or {}).items():
                # A zero in a higher-priority tag does not block a real value in a
                # later one (some filers tag Revenues = 0 beside their actual
                # RevenueFromContractWithCustomer… figure).
                if key not in best or (best[key][0] == 0 and hit[0] != 0):
                    best[key] = hit
        rows.extend((name, ptype, pend, value, filed, unit)
                    for (ptype, pend), (value, filed, unit) in best.items())
    if not rows:
        return _empty_fundamentals()
    df = pd.DataFrame(rows, columns=["field", "period_type", "period_end", "value", "filed",
                                     "unit"])
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
    def __init__(self, cache: RawCache, batch_size: int = 50, pacing: Pacing | None = None,
                 ticker_options: dict | None = None):
        self.cache = cache
        self.batch_size = batch_size
        self.pacing = pacing or Pacing()
        # Passed to yahooquery.Ticker: timeout, max_workers, retry, backoff_factor…
        self.ticker_options = {"asynchronous": True, **(ticker_options or {})}

    def _ticker(self, symbols: list[str]):
        from yahooquery import Ticker
        return Ticker(symbols, **self.ticker_options)

    def _per_symbol(self, symbols: list[str], key: str, what: str, kind: str,
                    fetch_batch: Callable[[list[str]], dict[str, Any]]) -> dict[str, Any]:
        """Cache per symbol, fetch only the misses in batches. `fetch_batch`
        returns {symbol: value} holding only values worth caching; a symbol it
        leaves out (failed batch, transient error) is fetched again next run."""
        out: dict[str, Any] = {}
        misses = []
        for symbol in symbols:
            hit = self.cache.get("yahooquery", f"{key}/{symbol}", kind)
            if hit is None:
                misses.append(symbol)
            else:
                out[symbol] = hit
        self.cache._count("yahooquery", "cached", len(symbols) - len(misses))
        batches = list(_chunks(misses, self.batch_size))
        if misses:
            log.info("%s: %d cached, fetching %d in %d batches", what,
                     len(symbols) - len(misses), len(misses), len(batches))
        for i, batch in enumerate(batches):
            if i and i % 20 == 0:
                log.info("%s: batch %d/%d", what, i + 1, len(batches))
            try:
                got = self.pacing.call(lambda b=batch: fetch_batch(b),
                                       f"{what} for {len(batch)} symbols")
            except Exception as e:  # noqa: BLE001 - keep going; these are refetched next run
                log.warning("%s: batch of %d failed after retries: %s", what, len(batch), e)
                continue
            self.cache._count("yahooquery", "fetched", len(got))
            for symbol, value in got.items():
                self.cache.put("yahooquery", f"{key}/{symbol}", value, kind)
                out[symbol] = value
        return out

    def _modules_batch(self, batch: list[str]) -> dict:
        """Quotes for a batch, retrying symbols that failed transiently. Returns
        modules (dict) or permanent errors ("not found") only."""
        pending, out = list(batch), {}
        for attempt in range(self.pacing.max_retries + 1):
            if attempt:
                self.pacing.sleep(self.pacing.backoff * 2 ** (attempt - 1))
                self.pacing.wait()
            data = self._ticker(pending).get_modules(list(YAHOO_MODULES))
            data = data if isinstance(data, dict) else {}
            for symbol in pending:
                value = data.get(symbol)
                if isinstance(value, dict) or (value is not None
                                               and not is_transient_error(value)):
                    out[symbol] = value
            pending = [s for s in pending if s not in out]
            if not pending:
                break
        if pending:
            log.warning("quotes: %d symbols still failing after retries (%s…); refetched "
                        "next run", len(pending), ", ".join(pending[:5]))
        return out

    def modules(self, symbols: list[str]) -> dict[str, dict]:
        data = self._per_symbol(symbols, "modules", "quotes", "json", self._modules_batch)
        return {s: d for s, d in data.items() if isinstance(d, dict)}

    def _frames(self, symbols: list[str], key: str, call: Callable, what: str) -> pd.DataFrame:
        def fetch_batch(batch: list[str]) -> dict[str, pd.DataFrame]:
            df = call(batch)
            # Only symbols present in the response are cached: one missing from a
            # partial response is fetched again next run.
            return {s: g.reset_index(drop=True) for s, g in df.groupby("symbol")}

        frames = [f for f in self._per_symbol(symbols, key, what, "frame",
                                              fetch_batch).values() if not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def financials(self, symbols: list[str], frequency: str) -> pd.DataFrame:
        def call(batch: list[str]) -> pd.DataFrame:
            df = self._ticker(batch).all_financial_data(frequency=frequency)
            if not isinstance(df, pd.DataFrame):  # error text for the whole batch
                raise RuntimeError(str(df)[:200])
            return df.reset_index()
        return self._frames(symbols, f"financials_{frequency}", call, f"financials ({frequency})")

    def history(self, symbols: list[str], period: str, interval: str = "1d") -> pd.DataFrame:
        def call(batch: list[str]) -> pd.DataFrame:
            df = self._ticker(batch).history(period=period, interval=interval)
            if not isinstance(df, pd.DataFrame) or df.empty:
                raise RuntimeError(f"no price history for {len(batch)} symbols")
            df = df.reset_index()
            df["date"] = pd.to_datetime(df["date"].astype(str).str[:10])
            return df
        return self._frames(symbols, f"history_{period}_{interval}", call,
                            f"history ({period}, {interval})")


class YFinanceClient:
    def __init__(self, cache: RawCache, pacing: Pacing | None = None):
        self.cache = cache
        self.pacing = pacing or Pacing()

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
                    df = self.pacing.call(lambda: getattr(t, name), f"yfinance {symbol} {name}")
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

    def __init__(self, cache: RawCache, user_agent: str, request_interval: float = 0.15,
                 max_retries: int = 3, backoff: float = 10.0, timeout: float = 30.0):
        if not user_agent:
            raise ValueError("SEC requires a User-Agent ('Name email'); set sec.user_agent")
        self.cache = cache
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self.pacing = Pacing(request_interval, max_retries, backoff)
        self.timeout = timeout

    def _get_json(self, url: str) -> dict:
        import requests

        def get() -> dict:
            resp = requests.get(url, headers=self.headers, timeout=self.timeout)
            if resp.status_code == 404:
                return {}  # no XBRL facts for this filer: permanent, not retried
            resp.raise_for_status()
            return resp.json()
        return self.pacing.call(get, f"SEC {url.rsplit('/', 1)[-1]}")

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

LISTING_COLUMNS = ["yahoo_symbol", "exchange", "name"]
NASDAQ_EXCHANGES = {"Q": "NASDAQ", "N": "NYSE", "A": "NYSEAMERICAN", "P": "NYSEARCA",
                    "Z": "CBOE", "V": "IEX"}
# Not common equity. Yahoo's quoteType (must be EQUITY) is the final check;
# these name patterns only avoid fetching obvious non-equities.
US_EXCLUDE = re.compile(r"warrant|\bunits?\b|\brights?\b|preferred|\bnotes?\b|debenture"
                        r"|\bfund\b|\bETN\b|beneficial (?:interest|ownership)"
                        r"|acquisition corp|\bphysical\b", re.I)
ADR_PATTERN = re.compile(r"depositary|\bADR\b|\bADS\b", re.I)
TMX_EXCLUDE = re.compile(r"\bETFs?\b|\bfunds?\b|\bfd\b|index|portfolio|\bbonds?\b|\bbnd\b"
                         r"|covered call|\bcov\b|split corp|bitcoin|ether|\bpr\b|pref|\bCDR\b"
                         r"|depositary receipt|\bETP\b|acquisition corp|\bphysical\b", re.I)
# TMX suffixes kept: share classes and trust units. Dropped: .U (USD-quoted
# duplicate line), .PR.* preferreds, .DB debentures, .WT warrants, .P capital
# pool shells (TSX-V), and the letter series used by ETFs (.F, .T, …).
TMX_KEEP_SUFFIXES = {"", "A", "B", "C", "D", "E", "UN"}


def parse_us_listing(text: str, exclude_adrs: bool = True) -> pd.DataFrame:
    """NASDAQ Trader nasdaqtraded.txt → common stocks as Yahoo symbols."""
    df = pd.read_csv(io.StringIO(text), sep="|", dtype=str).dropna(subset=["Symbol"])
    df = df[(df["Test Issue"] == "N") & (df["ETF"] == "N")
            & (df.get("NextShares", "N").fillna("N") == "N")]
    df = df[~df["Symbol"].str.contains(r"\$", regex=True)]          # $ = preferred series
    names = df["Security Name"].fillna("")
    keep = ~names.str.contains(US_EXCLUDE)
    if exclude_adrs:
        keep &= ~names.str.contains(ADR_PATTERN)
    df = df[keep]
    return pd.DataFrame({
        "yahoo_symbol": df["Symbol"].str.replace(".", "-", regex=False),
        "exchange": df["Listing Exchange"].map(NASDAQ_EXCHANGES).fillna("US"),
        "name": names[keep].str.strip(),
    }, columns=LISTING_COLUMNS).drop_duplicates("yahoo_symbol").reset_index(drop=True)


def parse_tmx_listing(directory: dict, exchange: str) -> pd.DataFrame:
    """TMX company directory JSON → equities as Yahoo symbols (.TO / .V)."""
    yahoo_suffix = {"TSX": ".TO", "TSXV": ".V"}[exchange]
    rows = []
    for company in directory.get("results", []):
        for inst in company.get("instruments", []):
            symbol, name = inst.get("symbol", ""), inst.get("name") or company.get("name", "")
            base, _, suffix = symbol.partition(".")
            company_name = company.get("name", "")
            if (suffix not in TMX_KEEP_SUFFIXES or TMX_EXCLUDE.search(name)
                    or TMX_EXCLUDE.search(company_name)):
                continue
            yahoo = base + (f"-{suffix}" if suffix else "") + yahoo_suffix
            rows.append((yahoo, exchange, company.get("name") or name))
    return (pd.DataFrame(rows, columns=LISTING_COLUMNS).drop_duplicates("yahoo_symbol")
            .reset_index(drop=True))


class ListingsClient:
    US_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqtraded.txt"
    TMX_URL = "https://www.tsx.com/json/company-directory/search/{market}/%5E*"

    def __init__(self, cache: RawCache, pacing: Pacing | None = None):
        self.cache = cache
        self.pacing = pacing or Pacing()

    def _get(self, url: str, as_json: bool) -> Any:
        import requests

        def get():
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        return self.pacing.call(get, f"listing {url}")

    def listings(self, markets: list[str], exclude_adrs: bool = True) -> pd.DataFrame:
        """markets: any of "us", "tsx", "tsxv"."""
        frames = []
        for market in markets:
            if market == "us":
                text = self.cache.fetch("listings", "us",
                                        lambda: {"text": self._get(self.US_URL, False)})["text"]
                frames.append(parse_us_listing(text, exclude_adrs))
            elif market in ("tsx", "tsxv"):
                data = self.cache.fetch("listings", market, lambda m=market: self._get(
                    self.TMX_URL.format(market=m), True))
                frames.append(parse_tmx_listing(data, market.upper()))
            else:
                raise ValueError(f"unknown market {market!r} (use us, tsx, tsxv)")
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=LISTING_COLUMNS)
