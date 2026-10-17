"""Screen-stage run: universe → factors → industry-relative scores.

Scores a data-stage run in place: outputs carry that run's run_id and are added
to its snapshot. Run it straight after ingest so the inputs are the ones the
run fetched (python -m data.pipeline ... --score does both).

    python -m screen.pipeline                 # latest run
    python -m screen.pipeline --run-id 2026-10-17T01:04:36Z --top 25
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from data.config import Config, load_config
from data.store import Store
from data.transform import FxProvider
from data.validate import check_outliers
from screen.factors import assign_segments, compute_factors
from screen.score import ranking, score
from screen.universe import apply_universe, knockout_summary


@dataclass
class ScreenResult:
    run_id: str
    universe: pd.DataFrame
    factors: pd.DataFrame
    scores: pd.DataFrame
    ranking: pd.DataFrame


def latest_run_id(store: Store) -> str:
    row = store.con.execute("SELECT run_id FROM run_log WHERE finished_at IS NOT NULL "
                            "ORDER BY started_at DESC LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("no finished data-stage run in the store")
    return row[0]


class StaleRunError(RuntimeError):
    pass


def run_screen(store: Store, cfg: Config, fx: FxProvider, run_id: str | None = None,
               as_of: date | None = None, allow_stale: bool = False) -> ScreenResult:
    run_id = run_id or latest_run_id(store)
    started = store.con.execute("SELECT started_at FROM run_log WHERE run_id = ?",
                                [run_id]).fetchone()
    if started is None:
        raise ValueError(f"unknown run {run_id}")
    run_date = pd.Timestamp(started[0]).date()
    age = (date.today() - run_date).days
    if as_of is None and not allow_stale and age > cfg.get("screen.max_run_age_days", 3):
        raise StaleRunError(f"run {run_id} is {age} days old; re-run the data stage "
                            f"(or pass --allow-stale)")
    as_of = as_of or run_date

    market = store.read("market_snapshot", "run_id = ?", [run_id])
    sids = list(market["security_id"])
    in_run = "list_contains(?, security_id)"
    securities = store.read("securities", in_run, [sids])
    issues = store.read("validation_issues", "run_id = ?", [run_id])

    # Missing vendor share counts fall back to the latest reported balance sheet
    # (market cap = price × shares).
    missing = market["shares_outstanding"].isna()
    if missing.any():
        q = store.panel("Q", as_of=as_of, security_ids=list(market.loc[missing, "security_id"]))
        if "shares_outstanding" in q.columns and not q.empty:
            latest = (q.dropna(subset=["shares_outstanding"]).sort_values("period_end")
                      .groupby("security_id")["shares_outstanding"].last())
            market.loc[missing, "shares_outstanding"] = (
                market.loc[missing, "security_id"].map(latest))

    universe = apply_universe(securities, market, issues, fx, as_of, cfg)
    members = list(universe.loc[universe["passed"], "security_id"])
    secs = securities[securities["security_id"].isin(members)]

    trading = dict(zip(secs["security_id"], secs["trading_currency"]))
    panel = lambda kind: store.panel(kind, as_of=as_of, security_ids=members,  # noqa: E731
                                     currency=trading, fx=fx)
    prices = store.read("prices_daily", in_run, [members])
    prices = prices[pd.to_datetime(prices["date"]).dt.date <= as_of]
    factors = compute_factors(
        secs, market[market["security_id"].isin(members)],
        store.read("estimates_snapshot", f"run_id = ? AND {in_run}", [run_id, members]),
        prices, store.read("insider_transactions", in_run, [members]),
        panel("FY"), panel("Q"), panel("TTM"), as_of, cfg)
    if not factors.empty:
        factors["segment"] = assign_segments(factors, fx, as_of, cfg)
    scores = score(factors, secs, cfg) if not factors.empty else pd.DataFrame()

    # Outputs replace any earlier scoring of this run.
    for table in ("universe_membership", "factor_scores"):
        store.con.execute(f"DELETE FROM {table} WHERE run_id = ?", [run_id])
    store.con.execute("DELETE FROM validation_issues WHERE run_id = ? AND check_name = 'outlier'",
                      [run_id])
    store.write("universe_membership", universe.assign(run_id=run_id))
    store.write("factor_scores", scores.assign(run_id=run_id))
    outliers = check_outliers(factors.assign(period_type=factors.get("base_period_type"),
                                             period_end=factors.get("base_period_end")),
                              cfg.get("validation.outlier_bounds", {}))
    store.write("validation_issues", outliers.assign(run_id=run_id))
    store.snapshot(run_id, sids, tables=("universe_membership", "factor_scores",
                                         "validation_issues"))
    return ScreenResult(run_id, universe, factors, scores, ranking(scores, secs)
                        if not scores.empty else pd.DataFrame())


def print_result(result: ScreenResult, top: int) -> None:
    u = result.universe
    print(f"run {result.run_id}: {int(u['passed'].sum())}/{len(u)} in universe")
    if (~u["passed"]).any():
        print("\nknocked out (details in universe_membership):")
        print(knockout_summary(u).to_string())
    if not result.ranking.empty:
        cols = [c for c in ("name", "industry", "composite", "quality", "value",
                            "expectations", "price", "risk") if c in result.ranking.columns]
        # Composites are relative to each segment's own peers: one table each.
        with pd.option_context("display.width", 200, "display.max_colwidth", 30,
                               "display.float_format", "{:.2f}".format):
            for group, rows in result.ranking.groupby("group", sort=False):
                print(f"\n{group} — top {min(top, len(rows))} of {len(rows)}:")
                print(rows[cols].head(top).to_string())


def main(argv: list[str] | None = None) -> None:
    from data.pipeline import build_fx

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--allow-stale", action="store_true",
                        help="score a run older than screen.max_run_age_days")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    store = Store(cfg.resolve_path("paths.db"), cfg.resolve_path("paths.snapshots"))
    try:
        result = run_screen(store, cfg, build_fx(cfg), args.run_id,
                            allow_stale=args.allow_stale)
    except StaleRunError as e:
        raise SystemExit(str(e))
    print_result(result, args.top)


if __name__ == "__main__":
    main()
