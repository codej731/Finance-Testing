"""Data-stage run: ingest → normalize → validate → store → snapshot.

    python -m data.pipeline AAPL MSFT RUS.TO CCL-B.TO
    python -m data.pipeline --file symbols.txt --notes "weekly refresh"
    python -m data.pipeline --file symbols.txt --score     # then universe + scores
    python -m data.pipeline --universe us,tsx,tsxv --score # full listings
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from typing import Callable
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from data.config import Config, load_config
from data.ingest import (EdgarClient, ListingsClient, Pacing, RawCache, StaticFx, YahooFx,
                         YahooQueryClient,
                         YFinanceClient, normalize_edgar_companyfacts, normalize_yahoo_estimates,
                         normalize_yahoo_history, normalize_yahoo_insiders,
                         normalize_yahoo_splits,
                         normalize_yahoo_market, normalize_yahoo_security,
                         normalize_yahoo_statements, normalize_yfinance_statements)
from data.schema import CANADIAN_EXCHANGES, availability_region
from data.store import Store
from data.transform import infer_fiscal_labels
from data.validate import ValidationReport, validate

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    run_id: str
    security_ids: list[str]
    universe: list[str]
    report: ValidationReport
    errors: list[str] = field(default_factory=list)
    snapshot_path: Path | None = None
    fetched: list[str] = field(default_factory=list)  # got statements and history


@dataclass
class Clients:
    yahoo: YahooQueryClient
    yfinance: YFinanceClient | None = None
    edgar: EdgarClient | None = None
    fx: object | None = None


def build_fx(cfg: Config, cache: RawCache | None = None):
    cache = cache or RawCache(cfg.resolve_path("paths.cache"), cfg.get("cache_ttl_hours", {}))
    static = StaticFx(cfg.get("fx.static_rates", {}))
    if cfg.get("fx.provider") == "static":
        return static
    return YahooFx(cache, fallback=static if cfg.get("fx.allow_static_fallback") else None)


def build_clients(cfg: Config) -> Clients:
    cache = RawCache(cfg.resolve_path("paths.cache"), cfg.get("cache_ttl_hours", {}))
    fx = build_fx(cfg, cache)
    retries = cfg.get("ingest.max_retries", 3)
    backoff = cfg.get("ingest.retry_backoff_seconds", 15)
    timeout = cfg.get("ingest.request_timeout_seconds", 30)
    user_agent = cfg.get("sec.user_agent") or ""
    edgar = (EdgarClient(cache, user_agent, cfg.get("sec.request_interval", 0.15),
                         retries, backoff, timeout) if user_agent else None)
    if edgar is None:
        log.warning("sec.user_agent is empty: EDGAR ingestion skipped")
    yahoo = YahooQueryClient(
        cache, cfg.get("ingest.batch_size", 50),
        Pacing(cfg.get("ingest.batch_delay_seconds", 2.0), retries, backoff),
        {"timeout": timeout, "max_workers": cfg.get("ingest.max_workers", 4)})
    yf_client = (YFinanceClient(cache, Pacing(cfg.get("ingest.yfinance_delay_seconds", 0.5),
                                              retries, backoff))
                 if cfg.get("ingest.use_yfinance_fallback") else None)
    return Clients(yahoo=yahoo, yfinance=yf_client, edgar=edgar, fx=fx)


Prefilter = Callable[[pd.DataFrame, pd.DataFrame], set]


def run_ingest(symbols: list[str], cfg: Config, *, store: Store | None = None,
               clients: Clients | None = None, as_of: date | None = None,
               notes: str | None = None, prefilter: Prefilter | None = None) -> IngestResult:
    """Ingest `symbols`. Quotes are fetched for all of them; with `prefilter`
    (securities, market snapshot) → security IDs, only those IDs get statements,
    price history and EDGAR (phase 2). The others keep their quote record."""
    as_of = as_of or date.today()
    store = store or Store(cfg.resolve_path("paths.db"), cfg.resolve_path("paths.snapshots"))
    clients = clients or build_clients(cfg)
    lags = cfg.get("availability_lag_days")
    retrieved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    errors: list[str] = []

    run_id = store.start_run(cfg.hash, notes)
    log.info("run %s: %d symbols", run_id, len(symbols))

    # Security master + market, estimates, insiders (one quoteSummary call per batch)
    try:
        modules = clients.yahoo.modules(symbols)
    except Exception as e:
        store.finish_run(run_id, 0, f"aborted: Yahoo quote data unavailable: {e}"[:500])
        raise
    for missing in sorted(set(symbols) - set(modules)):
        errors.append(f"{missing}: no Yahoo quote data")
    securities = pd.DataFrame([normalize_yahoo_security(s, m, as_of)
                               for s, m in modules.items()])
    if securities.empty:
        store.finish_run(run_id, 0, "no securities resolved")
        return IngestResult(run_id, [], [], ValidationReport(), errors)

    sid_by_symbol = dict(zip(securities["yahoo_symbol"], securities["security_id"]))
    region = {sid: availability_region(ex)
              for sid, ex in zip(securities["security_id"], securities["exchange"])}

    if clients.edgar is not None:
        try:
            ciks = clients.edgar.cik_map()
            us = ~securities["exchange"].isin(CANADIAN_EXCHANGES)
            securities.loc[us, "cik"] = securities.loc[us, "ticker"].str.upper().map(ciks)
        except Exception as e:  # noqa: BLE001 - one source failing must not end the run
            errors.append(f"edgar cik map: {e}")
    store.write_securities(securities)

    market, estimates, insiders = [], [], []
    for symbol, m in modules.items():
        sid = sid_by_symbol[symbol]
        market.append(normalize_yahoo_market(sid, m, run_id, as_of))
        estimates.extend(normalize_yahoo_estimates(sid, m, run_id, as_of))
        insiders.extend(normalize_yahoo_insiders(sid, m))
    market = pd.DataFrame(market)
    store.write("market_snapshot", market)
    store.write("estimates_snapshot", pd.DataFrame(estimates))
    store.write("insider_transactions", pd.DataFrame(insiders))

    # Phase 2 only for securities that can pass the market-data knockouts.
    all_sids = list(securities["security_id"])
    if prefilter is not None:
        keep = prefilter(securities, market)
        log.info("prefilter: %d of %d securities get statements and history",
                 len(keep), len(all_sids))
        securities = securities[securities["security_id"].isin(keep)]
        sid_by_symbol = {sym: sid for sym, sid in sid_by_symbol.items() if sid in keep}

    # Fundamentals from every available source; priority is resolved on read.
    fundamentals = []
    for freq in ("a", "q"):
        try:
            raw = clients.yahoo.financials(list(sid_by_symbol), freq)
            fundamentals.append(normalize_yahoo_statements(raw, sid_by_symbol, region, lags,
                                                           retrieved_at))
        except Exception as e:  # noqa: BLE001
            errors.append(f"yahooquery financials ({freq}): {e}")
    have_yahoo = set(pd.concat(fundamentals)["security_id"]) if fundamentals else set()

    if clients.yfinance is not None:
        missing = [(sym, sid) for sym, sid in sid_by_symbol.items() if sid not in have_yahoo]
        if missing:
            log.info("yfinance fallback: %d securities without yahooquery statements",
                     len(missing))
        for symbol, sid in missing:
            try:
                statements, ccy = clients.yfinance.statements(symbol)
                fundamentals.append(normalize_yfinance_statements(
                    statements, sid, ccy, region[sid], lags, retrieved_at))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{symbol}: yfinance statements: {e}")

    if clients.edgar is not None:
        filers = [(sid, cik) for sid, cik in zip(securities["security_id"], securities["cik"])
                  if isinstance(cik, str)]
        for i, (sid, cik) in enumerate(filers):
            if i % 200 == 0:
                log.info("edgar: filer %d/%d", i + 1, len(filers))
            try:
                fundamentals.append(normalize_edgar_companyfacts(
                    clients.edgar.companyfacts(cik), sid, retrieved_at))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{sid}: edgar companyfacts: {e}")

    fundamentals = [f for f in fundamentals if not f.empty]
    if fundamentals:
        long = infer_fiscal_labels(pd.concat(fundamentals, ignore_index=True))
        store.write("fundamentals", long)

    # Prices and splits. Daily bars give exact split dates; monthly bars over the
    # full history add older splits (dated to the month) so share-count jumps in
    # long EDGAR histories can be explained.
    currencies = dict(zip(securities["yahoo_symbol"], securities["trading_currency"]))
    prices = None
    try:
        history = clients.yahoo.history(list(sid_by_symbol), cfg.get("ingest.price_history", "5y"))
        prices, actions = normalize_yahoo_history(history, sid_by_symbol, currencies)
        store.write("prices_daily", prices)
        store.write("corporate_actions", actions)
    except Exception as e:  # noqa: BLE001
        errors.append(f"yahooquery history: {e}")
    try:
        monthly = clients.yahoo.history(list(sid_by_symbol), "max", interval="1mo")
        older = normalize_yahoo_splits(monthly, sid_by_symbol)
        if prices is not None and not prices.empty:
            first_daily = older["security_id"].map(prices.groupby("security_id")["date"].min())
            older = older[first_daily.isna() | (older["date"] < first_daily)]
        store.write("corporate_actions", older)
    except Exception as e:  # noqa: BLE001
        errors.append(f"yahooquery split history: {e}")

    # Validate against everything stored for these securities, not just this batch.
    sids = list(securities["security_id"])
    if not sids:
        store.finish_run(run_id, 0, "no securities passed the prefilter")
        return IngestResult(run_id, all_sids, [], ValidationReport(), errors,
                            store.snapshot(run_id, all_sids), [])
    report = validate(
        store.read("securities", "list_contains(?, security_id)", [sids]),
        store.fundamentals(sids), as_of, cfg, fx=clients.fx,
        corporate_actions=store.read("corporate_actions", "list_contains(?, security_id)",
                                     [sids]),
    )
    store.write("validation_issues", report.issues.assign(run_id=run_id))

    universe = [s for s in sids if s not in report.dropped]
    store.finish_run(run_id, len(universe), "; ".join([notes or ""] + errors[:20]).strip("; ")
                     or None)
    snapshot_path = store.snapshot(run_id, all_sids)
    return IngestResult(run_id, all_sids, universe, report, errors, snapshot_path, sids)


def freshness_summary(store: Store, run_id: str, cache: RawCache) -> str:
    """How current this run's inputs are: network vs cache, and quote dates."""
    lines = ["freshness:"]
    for source, counts in sorted(cache.stats.items()):
        lines.append(f"  {source}: {counts['fetched']} fetched, {counts['cached']} from cache "
                     f"(TTL {cache.ttl_hours.get(source, 24)}h)")
    q = store.con.execute("SELECT min(as_of), max(as_of) FROM market_snapshot WHERE run_id = ?",
                          [run_id]).fetchone()
    if q and q[0] is not None:
        lines.append(f"  quotes: {q[0]:%Y-%m-%d} to {q[1]:%Y-%m-%d}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("symbols", nargs="*", help="Yahoo symbols (AAPL, RUS.TO, CCL-B.TO)")
    parser.add_argument("--file", type=Path, help="file with one Yahoo symbol per line")
    parser.add_argument("--universe", default=None,
                        help="full exchange listings: comma-separated us, tsx, tsxv")
    parser.add_argument("--limit", type=int, default=None,
                        help="random sample of this many symbols (trial runs)")
    parser.add_argument("--no-prefilter", action="store_true",
                        help="fetch statements and history for every symbol")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--notes", default=None)
    parser.add_argument("--score", action="store_true",
                        help="run the screen stage (universe, factors, scores) afterwards")
    parser.add_argument("--top", type=int, default=20, help="rows to print with --score")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    cfg = load_config(args.config)
    store = Store(cfg.resolve_path("paths.db"), cfg.resolve_path("paths.snapshots"))
    clients = build_clients(cfg)

    symbols = list(args.symbols)
    if args.file:
        symbols += [ln.strip() for ln in args.file.read_text().splitlines()
                    if ln.strip() and not ln.startswith("#")]
    if args.universe:
        markets = [m.strip().lower() for m in args.universe.split(",") if m.strip()]
        listings = ListingsClient(clients.yahoo.cache, clients.yahoo.pacing).listings(
            markets, cfg.get("listings.exclude_adrs", True))
        log.info("listings: %s", listings["exchange"].value_counts().to_dict())
        symbols += list(listings["yahoo_symbol"])
    symbols = sorted(set(symbols))
    if args.limit and len(symbols) > args.limit:
        symbols = sorted(pd.Series(symbols).sample(args.limit, random_state=0))
    if not symbols:
        parser.error("no symbols given (pass symbols, --file or --universe)")

    prefilter = None
    if args.universe and cfg.get("ingest.prefilter", True) and not args.no_prefilter:
        from screen.universe import apply_universe

        def prefilter(securities: pd.DataFrame, market: pd.DataFrame) -> set:
            members = apply_universe(securities, market, pd.DataFrame(
                columns=["security_id", "check_name", "severity"]), clients.fx,
                date.today(), cfg)
            return set(members.loc[members["passed"], "security_id"])

    result = run_ingest(symbols, cfg, store=store, clients=clients, notes=args.notes,
                        prefilter=prefilter)
    print(f"run {result.run_id}: {len(result.security_ids)} securities quoted, "
          f"{len(result.fetched)} fetched in full, {len(result.universe)} passed validation")
    if not result.report.issues.empty:
        summary = result.report.issues.groupby(["check_name", "severity"]).size()
        print(summary.to_string())
    for err in result.errors:
        print(f"error: {err}")
    print(freshness_summary(store, result.run_id, clients.yahoo.cache))
    print(f"snapshot: {result.snapshot_path}")
    if args.score and result.security_ids:
        from screen.pipeline import print_result, run_screen
        print()
        print_result(run_screen(store, cfg, clients.fx, result.run_id), args.top)


if __name__ == "__main__":
    main()
