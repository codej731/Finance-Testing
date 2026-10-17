from datetime import date

import numpy as np
import pandas as pd
import pytest

from data.ingest import StaticFx
from data.schema import (FIELDS, TABLES, apply_sign, availability_region, native_ticker,
                         resolve_alternatives, security_id_for, yfinance_name)
from data.transform import (align_period_ends, build_ttm, convert_currency,
                            fundamentals_panel, infer_fiscal_labels, point_in_time,
                            resolve_sources, to_wide)

COLS = TABLES["fundamentals"].column_names


def row(sid, field, ptype, end, value, source="yahooquery", avail=None, ccy="USD"):
    end = date.fromisoformat(end)
    return dict(security_id=sid, field=field, period_type=ptype, period_end=end,
                fiscal_year=None, fiscal_period=None, value=value, currency=ccy,
                available_date=date.fromisoformat(avail) if avail else end,
                available_date_estimated=source != "edgar", source=source,
                retrieved_at=pd.Timestamp("2025-01-01"))


def frame(*rows):
    return pd.DataFrame(list(rows), columns=COLS)


# --- schema ---------------------------------------------------------------

@pytest.mark.parametrize("yq,yf", [
    ("TotalRevenue", "Total Revenue"), ("NetPPE", "Net PPE"), ("EBITDA", "EBITDA"),
    ("CashCashEquivalentsAndShortTermInvestments",
     "Cash Cash Equivalents And Short Term Investments"),
    ("TotalLiabilitiesNetMinorityInterest", "Total Liabilities Net Minority Interest"),
])
def test_yfinance_names(yq, yf):
    assert yfinance_name(yq) == yf


@pytest.mark.parametrize("symbol,code,expected", [
    ("AAPL", "NMS", "XNAS:AAPL"), ("RUS.TO", "TOR", "XTSE:RUS"),
    ("BAM-A.TO", None, "XTSE:BAM.A"), ("ABC.V", None, "XTSX:ABC"), ("BRK-B", "NYQ", "XNYS:BRK.B"),
])
def test_security_ids(symbol, code, expected):
    assert security_id_for(symbol, code) == expected


def test_native_ticker_and_region():
    assert native_ticker("BAM-A.TO") == "BAM.A"
    assert availability_region("TSXV") == "TSXV"
    assert availability_region("TSX") == "TSX"
    assert availability_region("NASDAQ") == "US"


def test_magnitude_fields_are_positive_and_signed_fields_kept():
    assert apply_sign("capex", -5.0) == 5.0
    assert apply_sign("buybacks", -1.0) == 1.0
    assert apply_sign("net_income", -3.0) == -3.0
    assert apply_sign("capex", None) is None


@pytest.mark.parametrize("values,expected", [
    ({"A": 1, "B": 2, "C": 4}, 3),        # first alternative, optional C not in it
    ({"A": 1}, 1),                         # B missing → second alternative: A + ?C
    ({"A": 1, "C": 4}, 5),
    ({"C": 4}, None),                      # A required in both
    ({}, None),
])
def test_resolve_alternatives(values, expected):
    alts = (("A", "B"), ("A", "?C"))
    assert resolve_alternatives(values.get, alts) == expected


def test_all_optional_alternative_needs_one_value():
    assert resolve_alternatives({}.get, (("?A", "?B"),)) is None
    assert resolve_alternatives({"B": 2}.get, (("?A", "?B"),)) == 2


def test_every_field_has_a_source_mapping():
    for f in FIELDS.values():
        assert f.yahoo, f.name
        assert f.xbrl or f.name == "ebitda_reported", f.name


# --- transform ------------------------------------------------------------

def test_fiscal_labels_for_september_year_end():
    df = infer_fiscal_labels(frame(
        row("X", "revenue", "FY", "2024-09-30", 1),
        row("X", "revenue", "Q", "2023-12-31", 1),
        row("X", "revenue", "Q", "2024-06-30", 1),
        row("X", "revenue", "Q", "2024-09-28", 1),  # 52/53-week year
        row("X", "revenue", "Q", "2024-12-31", 1),  # after the latest FY
    ))
    labels = list(zip(df["fiscal_year"], df["fiscal_period"]))
    assert labels == [(2024, "FY"), (2024, "Q1"), (2024, "Q3"), (2024, "Q4"), (2025, "Q1")]


def test_fiscal_labels_default_to_calendar_year():
    df = infer_fiscal_labels(frame(row("X", "revenue", "Q", "2024-06-30", 1)))
    assert (df["fiscal_year"].iat[0], df["fiscal_period"].iat[0]) == (2024, "Q2")


def test_point_in_time_excludes_unfiled_rows():
    df = frame(row("X", "revenue", "FY", "2024-12-31", 1, avail="2025-03-01"),
               row("X", "revenue", "FY", "2023-12-31", 1, avail="2024-03-01"))
    assert len(point_in_time(df, date(2025, 2, 1))) == 1


def test_source_priority_and_null_does_not_mask():
    df = frame(
        row("X", "revenue", "FY", "2024-12-31", 100, "yahooquery"),
        row("X", "revenue", "FY", "2024-12-31", 101, "edgar"),
        row("X", "cash", "FY", "2024-12-31", np.nan, "edgar"),
        row("X", "cash", "FY", "2024-12-31", 7, "yfinance"),
    )
    out = resolve_sources(df).set_index("field")
    assert out.loc["revenue", "value"] == 101 and out.loc["revenue", "source"] == "edgar"
    assert out.loc["cash", "value"] == 7


def test_align_period_ends_snaps_to_priority_source():
    df = frame(row("X", "revenue", "FY", "2024-09-28", 1, "edgar"),
               row("X", "revenue", "FY", "2024-09-30", 1, "yahooquery"),
               row("X", "revenue", "FY", "2023-09-30", 1, "yahooquery"))
    ends = sorted(set(align_period_ends(df)["period_end"]))
    assert ends == [date(2023, 9, 30), date(2024, 9, 28)]


def _quarters(values, fields=("revenue",), start="2024-03-31"):
    ends = pd.date_range(start, periods=len(values), freq="QE").date
    rows = []
    for end, v in zip(ends, values):
        for f in fields:
            rows.append(row("X", f, "Q", str(end), v))
        rows.append(row("X", "total_assets", "Q", str(end), 1000 + (v or 0)))
    return frame(*rows)


def test_ttm_sums_flows_and_takes_latest_balance():
    wide = to_wide(_quarters([10, 20, 30, 40, 50]))
    ttm = build_ttm(wide)
    assert list(ttm["revenue"]) == [100, 140]
    assert list(ttm["total_assets"]) == [1040, 1050]


def test_ttm_null_when_a_quarter_is_missing_or_gapped():
    ttm = build_ttm(to_wide(_quarters([10, np.nan, 30, 40])))
    assert ttm["revenue"].isna().all()
    gapped = _quarters([10, 20, 30, 40, 50])
    gapped = gapped[gapped["period_end"] != date(2024, 9, 30)]
    assert build_ttm(to_wide(gapped)).empty


def test_ttm_averages_share_counts():
    ttm = build_ttm(to_wide(_quarters([100, 100, 100, 104], fields=("diluted_shares_avg",))))
    assert ttm["diluted_shares_avg"].iat[0] == 101


def test_currency_conversion_skips_share_fields():
    df = frame(row("X", "revenue", "FY", "2024-12-31", 100, ccy="USD"),
               row("X", "shares_outstanding", "FY", "2024-12-31", 50, ccy="USD"))
    out = convert_currency(df, {"X": "CAD"}, StaticFx({"USDCAD": 1.4})).set_index("field")
    assert out.loc["revenue", "value"] == pytest.approx(140)
    assert out.loc["revenue", "currency"] == "CAD"
    assert out.loc["shares_outstanding", "value"] == 50


def test_currency_conversion_without_rate_is_null():
    df = frame(row("X", "revenue", "FY", "2024-12-31", 100, ccy="EUR"))
    out = convert_currency(df, "CAD", StaticFx({}))
    assert out["value"].isna().all()


def test_panel_requires_fx_for_conversion():
    with pytest.raises(ValueError):
        fundamentals_panel(frame(row("X", "revenue", "FY", "2024-12-31", 1)), currency="CAD")
