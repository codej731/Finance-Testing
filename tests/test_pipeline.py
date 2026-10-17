"""End-to-end data-stage run against fake vendor clients and a temp DuckDB."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from data.config import load_config
from data.ingest import StaticFx
from data.pipeline import Clients, run_ingest
from data.store import Store, load_snapshot
from tests import fixtures

AS_OF = date(2025, 1, 31)
QUARTERS = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]


class FakeYahoo:
    def __init__(self):
        self.modules_data = {
            "TST": fixtures.yahoo_modules("TST", exchange="NMS", currency="USD",
                                          financial_currency="USD"),
            "MINR.TO": fixtures.yahoo_modules("MINR.TO", exchange="TOR", currency="CAD",
                                              financial_currency="USD", sector="Basic Materials",
                                              industry="Gold", country="Canada"),
            "OLD.V": fixtures.yahoo_modules("OLD.V", exchange="VAN", currency="CAD",
                                            financial_currency="CAD"),
        }

    def modules(self, symbols):
        return {s: self.modules_data[s] for s in symbols if s in self.modules_data}

    def financials(self, symbols, frequency):
        frames = {
            "TST": fixtures.yahoo_financials("TST", "USD", ["2023-09-30", "2024-09-30"],
                                             ["2024-06-30", "2024-09-30"]),
            "MINR.TO": fixtures.yahoo_financials("MINR.TO", "USD", ["2022-12-31", "2023-12-31"],
                                                 QUARTERS),
            "OLD.V": fixtures.yahoo_financials("OLD.V", "CAD", ["2022-06-30"], []),
        }
        want = "12M" if frequency == "a" else "3M"
        df = pd.concat([frames[s] for s in symbols if s in frames])
        return df[df["periodType"].isin([want, "TTM"])]

    def history(self, symbols, period, interval="1d"):
        if interval != "1d":  # long-range monthly bars: one old split for TST
            df = fixtures.yahoo_history("TST", days=3)
            df["date"] = pd.to_datetime(["2014-06-01", "2014-07-01", "2014-08-01"])
            df.loc[0, "splits"] = 7.0
            return df
        return pd.concat([fixtures.yahoo_history(s, split_on=2 if s == "MINR.TO" else None)
                          for s in symbols])


class FakeEdgar:
    def cik_map(self):
        return {"TST": "0000001234"}

    def companyfacts(self, cik):
        assert cik == "0000001234"
        return fixtures.edgar_companyfacts()


@pytest.fixture()
def cfg(tmp_path: Path):
    raw = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
    raw["paths"] = {"db": "engine.duckdb", "snapshots": "snapshots", "cache": "cache"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_config(path)


def _run(cfg, store, notes=None):
    clients = Clients(yahoo=FakeYahoo(), edgar=FakeEdgar(), fx=StaticFx({"USDCAD": 1.37}))
    return run_ingest(["TST", "MINR.TO", "OLD.V", "GONE"], cfg, store=store, clients=clients,
                      as_of=AS_OF, notes=notes)


def test_full_run(cfg, tmp_path):
    store = Store(cfg.resolve_path("paths.db"), cfg.resolve_path("paths.snapshots"))
    result = _run(cfg, store, notes="test")

    assert sorted(result.security_ids) == ["XNAS:TST", "XTSE:MINR", "XTSX:OLD"]
    assert "XTSX:OLD" in result.report.dropped  # stale FY
    assert sorted(result.universe) == ["XNAS:TST", "XTSE:MINR"]
    assert any("GONE" in e for e in result.errors)

    secs = store.read("securities").set_index("security_id")
    assert secs.loc["XNAS:TST", "cik"] == "0000001234"
    assert pd.isna(secs.loc["XTSE:MINR", "cik"])

    issues = result.report.issues
    minr = issues[issues["security_id"] == "XTSE:MINR"]
    assert ("currency_mismatch", "info") in set(zip(minr["check_name"], minr["severity"]))

    # EDGAR outranks Yahoo for the same period; Yahoo fills what EDGAR lacks.
    fy = store.panel("FY", as_of=AS_OF, security_ids=["XNAS:TST"]).set_index("period_end")
    assert fy.loc[date(2024, 9, 30), "revenue"] == 500
    assert fy.loc[date(2024, 9, 30), "operating_income"] == pytest.approx(2e7 * 4.4)
    assert fy.loc[date(2024, 9, 30), "fiscal_year"] == 2024

    # Fundamentals in USD, quote in CAD: panel converts at period-end FX.
    cad = store.panel("FY", as_of=AS_OF, security_ids=["XTSE:MINR"], currency="CAD",
                      fx=StaticFx({"USDCAD": 1.37}))
    assert (cad["currency"] == "CAD").all()
    assert cad["revenue"].iat[-1] == pytest.approx(1e8 * 4.4 * 1.37)

    ttm = store.panel("TTM", as_of=AS_OF, security_ids=["XTSE:MINR"])
    assert len(ttm) == 0  # Q4 2024 not yet available on AS_OF (+45 days)
    ttm_later = store.panel("TTM", as_of=date(2025, 3, 1), security_ids=["XTSE:MINR"])
    assert len(ttm_later) == 1

    assert len(store.read("prices_daily")) == 30
    actions = store.read("corporate_actions").sort_values("date")
    assert list(zip(actions["security_id"], actions["ratio"])) == [("XNAS:TST", 7.0),
                                                                    ("XTSE:MINR", 2.0)]
    assert len(store.read("market_snapshot", "run_id = ?", [result.run_id])) == 3
    assert len(store.read("estimates_snapshot")) == 3

    log = store.read("run_log").iloc[0]
    assert log["config_hash"] == cfg.hash and log["universe_size"] == 2
    assert log["finished_at"] is not None and "GONE" in log["notes"]

    snap = load_snapshot(cfg.resolve_path("paths.snapshots"), result.run_id)
    assert {"run_log", "fundamentals", "market_snapshot", "validation_issues",
            "securities", "prices_daily"} <= set(snap)
    assert len(snap["validation_issues"]) == len(issues)
    assert ":" not in result.snapshot_path.name


def test_rerun_upserts_without_duplicates(cfg):
    store = Store(cfg.resolve_path("paths.db"), cfg.resolve_path("paths.snapshots"))
    first = _run(cfg, store)
    n_fund = len(store.read("fundamentals"))
    n_ins = len(store.read("insider_transactions"))
    second = _run(cfg, store, notes="again")
    assert len(store.read("fundamentals")) == n_fund
    assert len(store.read("insider_transactions")) == n_ins
    assert len(store.read("securities")) == 3
    assert first.run_id != second.run_id
    assert len(store.read("run_log")) == 2


def test_screen_after_ingest(cfg):
    from screen.pipeline import run_screen

    store = Store(cfg.resolve_path("paths.db"), cfg.resolve_path("paths.snapshots"))
    ingest = _run(cfg, store)
    fx = StaticFx({"USDCAD": 1.37})
    result = run_screen(store, cfg, fx, ingest.run_id, as_of=AS_OF)

    members = result.universe.set_index("security_id")
    assert members.loc["XTSX:OLD", "reason"] == "validation: staleness"
    assert set(members.index[members["passed"]]) == {"XNAS:TST", "XTSE:MINR"}

    stored = store.read("factor_scores", "run_id = ?", [ingest.run_id])
    assert {"composite", "roic", "category:quality"} <= set(stored["factor"])
    assert set(stored["security_id"]) == {"XNAS:TST", "XTSE:MINR"}
    # Fundamentals were converted to the trading currency before EV (MINR: USD → CAD).
    minr = result.factors.set_index("security_id").loc["XTSE:MINR"]
    assert minr["currency"] == "CAD"

    snap = load_snapshot(cfg.resolve_path("paths.snapshots"), ingest.run_id)
    assert len(snap["factor_scores"]) == len(stored)
    assert len(snap["universe_membership"]) == 3

    # Re-scoring the same run replaces, not appends.
    run_screen(store, cfg, fx, ingest.run_id, as_of=AS_OF)
    assert len(store.read("factor_scores", "run_id = ?", [ingest.run_id])) == len(stored)


def test_screen_refuses_stale_runs(cfg):
    from screen.pipeline import StaleRunError, run_screen

    store = Store(cfg.resolve_path("paths.db"), cfg.resolve_path("paths.snapshots"))
    ingest = _run(cfg, store)
    store.con.execute("UPDATE run_log SET started_at = started_at - INTERVAL 10 DAY "
                      "WHERE run_id = ?", [ingest.run_id])
    fx = StaticFx({"USDCAD": 1.37})
    with pytest.raises(StaleRunError):
        run_screen(store, cfg, fx, ingest.run_id)
    run_screen(store, cfg, fx, ingest.run_id, allow_stale=True)  # explicit override
