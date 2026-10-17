from datetime import date

import numpy as np
import pandas as pd

from data.ingest import StaticFx
from data.schema import FIELDS
from data.validate import (check_balance_identity, check_completeness, check_currency,
                           check_outliers, check_share_jumps, check_sign_sanity,
                           check_source_conflicts, check_staleness)
from tests.test_schema_transform import frame, row


def panel(*rows):
    """Wide rows: dicts with security_id, period_type, period_end + fields."""
    df = pd.DataFrame(list(rows))
    for f in FIELDS:
        if f not in df.columns:
            df[f] = np.nan
    df["period_end"] = pd.to_datetime(df["period_end"]).dt.date
    if "available_date" not in df.columns:
        df["available_date"] = df["period_end"]
    return df


def test_currency_mismatch_converted_or_dropped():
    secs = pd.DataFrame({"security_id": ["XTSE:RUS"], "trading_currency": ["CAD"]})
    long = frame(row("XTSE:RUS", "revenue", "FY", "2024-12-31", 1, ccy="USD"))
    ok = check_currency(secs, long, StaticFx({"USDCAD": 1.37}))
    assert list(ok["severity"]) == ["info"]
    missing = check_currency(secs, long, StaticFx({}))
    assert list(missing["severity"]) == ["drop"]


def test_staleness():
    fy = panel({"security_id": "A", "period_type": "FY", "period_end": "2023-06-30"},
               {"security_id": "B", "period_type": "FY", "period_end": "2024-12-31"})
    out = check_staleness(fy, ["A", "B", "C"], date(2025, 1, 31), 15)
    assert set(out["security_id"]) == {"A", "C"} and (out["severity"] == "drop").all()


def test_staleness_by_region():
    fy = panel({"security_id": "V", "period_type": "FY", "period_end": "2025-06-30"},
               {"security_id": "T", "period_type": "FY", "period_end": "2025-06-30"})
    limits = {"US": 15, "TSX": 15, "TSXV": 16}
    out = check_staleness(fy, ["V", "T"], date(2026, 10, 17), limits, {"V": "TSXV", "T": "TSX"})
    assert list(out["security_id"]) == ["T"]  # TSX-V FY2026 not due until Oct 28


def test_balance_identity_warns():
    p = panel({"security_id": "A", "period_type": "FY", "period_end": "2024-12-31",
               "total_assets": 100, "total_liabilities": 40, "total_equity": 50,
               "minority_interest": 5},
              {"security_id": "B", "period_type": "FY", "period_end": "2024-12-31",
               "total_assets": 100, "total_liabilities": 40, "total_equity": 59})
    out = check_balance_identity(p, 0.02)
    assert list(out["security_id"]) == ["A"] and out["severity"].iat[0] == "warn"


def test_sign_sanity_drops_latest_and_warns_history():
    p = panel({"security_id": "A", "period_type": "FY", "period_end": "2023-12-31",
               "revenue": -1, "total_assets": 10},
              {"security_id": "A", "period_type": "FY", "period_end": "2024-12-31",
               "revenue": 5, "total_assets": 0})
    out = check_sign_sanity(p).set_index("field")
    assert out.loc["revenue", "severity"] == "warn"
    assert out.loc["total_assets", "severity"] == "drop"


def test_share_jump_explained_by_split():
    p = panel({"security_id": "A", "period_type": "Q", "period_end": "2024-06-30",
               "shares_outstanding": 100},
              {"security_id": "A", "period_type": "Q", "period_end": "2024-09-30",
               "shares_outstanding": 200})
    assert len(check_share_jumps(p, 0.5)) == 1
    split = pd.DataFrame({"security_id": ["A"], "date": [date(2024, 8, 1)]})
    assert check_share_jumps(p, 0.5, split).empty


def test_share_jump_explained_by_split_before_filing():
    p = panel({"security_id": "A", "period_type": "FY", "period_end": "2009-12-31",
               "available_date": date(2010, 2, 20), "shares_outstanding": 100},
              {"security_id": "A", "period_type": "FY", "period_end": "2010-12-31",
               "available_date": date(2011, 2, 25), "shares_outstanding": 200})
    split = pd.DataFrame({"security_id": ["A"], "date": [date(2011, 2, 1)]})
    assert check_share_jumps(p, 0.5, split).empty


def test_source_conflict_logged_with_kept_source():
    long = frame(row("A", "revenue", "FY", "2024-09-28", 100, "edgar"),
                 row("A", "revenue", "FY", "2024-09-30", 110, "yahooquery"),
                 row("A", "cash", "FY", "2024-09-30", 10, "yahooquery"),
                 row("A", "cash", "FY", "2024-09-30", 10.2, "yfinance"))
    out = check_source_conflicts(long, 0.05)
    assert list(out["field"]) == ["revenue"]
    assert "kept edgar" in out["detail"].iat[0]


def test_completeness():
    fy = panel({"security_id": "A", "period_type": "FY", "period_end": "2024-12-31",
                "revenue": 1, "net_income": 1},
               {"security_id": "B", "period_type": "FY", "period_end": "2024-12-31",
                "revenue": 1, "net_income": 1, "cash": 1})
    out = check_completeness(fy, ["A", "B"], ["revenue", "net_income", "cash"], 3)
    assert list(out["security_id"]) == ["A"] and "missing cash" in out["detail"].iat[0]


def test_outliers():
    m = pd.DataFrame({"security_id": ["A", "B"], "roic": [0.15, 3.5]})
    out = check_outliers(m, {"roic": [-2.0, 2.0], "not_computed": [0, 1]})
    assert list(out["security_id"]) == ["B"]
