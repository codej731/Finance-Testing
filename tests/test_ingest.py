from datetime import date, datetime

import numpy as np

import pandas as pd
import pytest

from data.ingest import (RawCache, classify_insider_role, classify_insider_transaction,
                         normalize_edgar_companyfacts, normalize_yahoo_estimates,
                         normalize_yahoo_history, normalize_yahoo_insiders,
                         normalize_yahoo_market, normalize_yahoo_security,
                         normalize_yahoo_statements, normalize_yfinance_statements)
from tests import fixtures

LAGS = {"US": {"Q": 45, "FY": 90}, "TSX": {"Q": 45, "FY": 90}, "TSXV": {"Q": 60, "FY": 120}}
NOW = datetime(2025, 1, 31)


def _value(df, field, ptype, end):
    sel = df[(df["field"] == field) & (df["period_type"] == ptype)
             & (df["period_end"] == date.fromisoformat(end))]
    assert len(sel) == 1, (field, ptype, end, sel)
    return sel.iloc[0]


# --- EDGAR ----------------------------------------------------------------

@pytest.fixture(scope="module")
def edgar():
    return normalize_edgar_companyfacts(fixtures.edgar_companyfacts(), "XNAS:TST", NOW)


def test_edgar_uses_original_filing_and_filing_date(edgar):
    r = _value(edgar, "revenue", "FY", "2023-09-30")
    assert r["value"] == 400  # not the 405 restatement filed a year later
    assert r["available_date"] == date(2023, 11, 15)
    assert not r["available_date_estimated"]
    assert r["source"] == "edgar" and r["currency"] == "USD"


def test_edgar_ignores_non_periodic_forms(edgar):
    assert _value(edgar, "revenue", "FY", "2024-09-30")["value"] == 500


def test_edgar_tag_priority_is_per_period(edgar):
    assert _value(edgar, "revenue", "FY", "2022-09-30")["value"] == 350


def test_edgar_direct_quarters_and_derived_q4(edgar):
    assert _value(edgar, "revenue", "Q", "2024-03-31")["value"] == 120
    q4 = _value(edgar, "revenue", "Q", "2024-09-30")
    assert q4["value"] == 140  # 500 FY − 360 nine-month YTD
    assert q4["available_date"] == date(2024, 11, 15)


def test_edgar_ytd_cash_flow_is_differenced(edgar):
    got = [_value(edgar, "operating_cash_flow", "Q", e)["value"]
           for e in ("2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30")]
    assert got == [50, 40, 60, 60]


def test_edgar_share_averages_are_not_differenced(edgar):
    q = edgar[(edgar["field"] == "diluted_shares_avg") & (edgar["period_type"] == "Q")]
    assert q.empty


def test_edgar_instants_and_fy_tagging(edgar):
    assets = edgar[edgar["field"] == "total_assets"]
    assert set(assets.loc[assets["period_type"] == "FY", "period_end"]) == {
        date(2023, 9, 30), date(2024, 9, 30)}
    assert date(2024, 10, 15) not in set(assets["period_end"])  # not a period end


def test_edgar_summed_alternative_requires_all_components(edgar):
    debt = edgar[edgar["field"] == "total_debt"]
    assert set(debt["period_end"]) == {date(2024, 9, 30)}
    assert debt["value"].iat[0] == 250


def test_edgar_cash_adds_optional_marketable_securities(edgar):
    assert _value(edgar, "cash", "FY", "2024-09-30")["value"] == 140
    assert _value(edgar, "cash", "Q", "2023-12-31")["value"] == 90


def test_edgar_cover_page_shares_attach_to_report_period(edgar):
    fy = _value(edgar, "shares_outstanding", "FY", "2024-09-30")
    assert fy["value"] == 1_000_000 and fy["currency"] == "USD"
    assert _value(edgar, "shares_outstanding", "Q", "2024-06-30")["value"] == 990_000


def test_edgar_capex_is_positive(edgar):
    assert _value(edgar, "capex", "FY", "2024-09-30")["value"] == 30


# --- yahooquery -----------------------------------------------------------

def test_yahoo_statements():
    raw = fixtures.yahoo_financials("ABC.V", "CAD", ["2023-12-31"], ["2024-09-30"])
    df = normalize_yahoo_statements(raw, {"ABC.V": "XTSX:ABC"}, {"XTSX:ABC": "TSXV"}, LAGS, NOW)
    assert set(df["period_type"]) == {"FY", "Q"}  # vendor TTM dropped
    capex = _value(df, "capex", "FY", "2023-12-31")
    assert capex["value"] == 2e7  # Yahoo reports capex negative
    assert capex["available_date"] == date(2024, 4, 29)  # +120 days for TSX-V annual
    assert capex["available_date_estimated"]
    assert _value(df, "revenue", "Q", "2024-09-30")["available_date"] == date(2024, 11, 29)
    assert (df["currency"] == "CAD").all()


def test_yahoo_total_debt_excludes_leases():
    raw = fixtures.yahoo_financials("A", "USD", ["2023-12-31"], [])
    raw["CurrentDebt"], raw["LongTermDebt"], raw["TotalDebt"] = 2e7, 5e7, 9e7
    df = normalize_yahoo_statements(raw, {"A": "XNAS:A"}, {"XNAS:A": "US"}, LAGS, NOW)
    assert _value(df, "total_debt", "FY", "2023-12-31")["value"] == 7e7


def test_yahoo_depreciation_includes_depletion():
    raw = fixtures.yahoo_financials("A", "USD", ["2023-12-31"], [])
    raw["DepreciationAndAmortization"], raw["DepreciationAmortizationDepletion"] = 1e6, 3e8
    df = normalize_yahoo_statements(raw, {"A": "XNAS:A"}, {"XNAS:A": "US"}, LAGS, NOW)
    assert _value(df, "depreciation_amortization", "FY", "2023-12-31")["value"] == 3e8


def test_yahoo_total_debt_needs_long_term_debt():
    # Yahoo sometimes has only CurrentDebt for a year; that is not total debt.
    raw = fixtures.yahoo_financials("A", "USD", ["2023-12-31"], [])
    raw["CurrentDebt"], raw["LongTermDebt"], raw["TotalDebt"] = 2e7, np.nan, 9e7
    df = normalize_yahoo_statements(raw, {"A": "XNAS:A"}, {"XNAS:A": "US"}, LAGS, NOW)
    assert _value(df, "total_debt", "FY", "2023-12-31")["value"] == 9e7


def test_edgar_ignores_facts_dated_after_filing():
    facts = fixtures.edgar_companyfacts()
    facts["facts"]["us-gaap"]["SomeAssumption"] = {"units": {"pure": [
        {"val": 0.05, "start": "2027-01-01", "end": "2027-12-31", "filed": "2024-11-15",
         "form": "10-K", "accn": "A-24K"}]}}
    df = normalize_edgar_companyfacts(facts, "XNAS:TST", NOW)
    assert df["period_end"].max() == date(2024, 9, 30)
    assert _value(df, "shares_outstanding", "FY", "2024-09-30")["value"] == 1_000_000


def test_yahoo_statements_cash_falls_back_to_second_name():
    raw = fixtures.yahoo_financials("A", "USD", ["2023-12-31"], [])
    df = normalize_yahoo_statements(raw, {"A": "XNAS:A"}, {"XNAS:A": "US"}, LAGS, NOW)
    assert _value(df, "cash", "FY", "2023-12-31")["value"] == 5e7


def test_yahoo_security_market_estimates_insiders():
    m = fixtures.yahoo_modules("RUS.TO", exchange="TOR", currency="CAD",
                               financial_currency="USD", country="Canada")
    sec = normalize_yahoo_security("RUS.TO", m, date(2025, 1, 31))
    assert sec["security_id"] == "XTSE:RUS" and sec["exchange"] == "TSX"
    assert (sec["trading_currency"], sec["reporting_currency"]) == ("CAD", "USD")

    mkt = normalize_yahoo_market("XTSE:RUS", m, "run", date(2025, 1, 31))
    assert mkt["price"] == 25.0 and mkt["avg_volume_3m"] == 400_000
    assert mkt["as_of"] == date(2025, 1, 31)

    est = normalize_yahoo_estimates("XTSE:RUS", m, "run", date(2025, 1, 31))
    assert [e["horizon"] for e in est] == ["0y"]
    assert est[0]["eps_mean_90d_ago"] == 2.5 and est[0]["revisions_down_30d"] == 4

    ins = normalize_yahoo_insiders("XTSE:RUS", m)
    buy = ins[0]
    assert (buy["insider_role"], buy["transaction_type"]) == ("ceo", "open_market_buy")
    assert buy["price"] == 25.0 and buy["value"] == 250_000
    assert ins[1]["transaction_type"] == "award"


def test_missing_market_fields_are_null_not_zero():
    m = fixtures.yahoo_modules("X", exchange="NMS", currency="USD", financial_currency="USD")
    m["defaultKeyStatistics"] = {}
    mkt = normalize_yahoo_market("XNAS:X", m, "run", date(2025, 1, 31))
    assert mkt["short_pct_float"] is None and mkt["shares_outstanding"] is None


@pytest.mark.parametrize("text,kind", [
    ("Sale at price 227.50 - 229.30 per share.", "open_market_sale"),
    ("Conversion of Exercise of derivative security at price 21.34 per share.",
     "option_exercise"),
    ("Stock Gift at price 0.00 per share.", "gift"),
    ("", "other"),
])
def test_insider_transaction_types(text, kind):
    assert classify_insider_transaction(text) == kind


@pytest.mark.parametrize("relation,role", [
    ("Chief Financial Officer", "cfo"), ("Director", "director"),
    ("Beneficial Owner (10% or more)", "10pct_owner"), ("General Counsel", "officer"),
    (None, "other"),
])
def test_insider_roles(relation, role):
    assert classify_insider_role(relation) == role


def test_history_and_splits():
    prices, actions = normalize_yahoo_history(fixtures.yahoo_history("A", split_on=3),
                                              {"A": "XNAS:A"}, {"A": "USD"})
    assert len(prices) == 10 and prices["currency"].eq("USD").all()
    assert list(actions["ratio"]) == [2.0] and actions["action_type"].iat[0] == "split"


def test_history_without_splits_column():
    raw = fixtures.yahoo_history("A").drop(columns="splits")
    prices, actions = normalize_yahoo_history(raw, {"A": "XNAS:A"}, {"A": "USD"})
    assert len(prices) == 10 and actions.empty


# --- yfinance -------------------------------------------------------------

def test_yfinance_statements():
    fin = pd.DataFrame({"2024-12-31": [100.0, 20.0], "2023-12-31": [90.0, None]},
                       index=["Total Revenue", "Operating Income"])
    cf = pd.DataFrame({"2024-12-31": [-7.0]}, index=["Capital Expenditure"])
    df = normalize_yfinance_statements({"FY": [fin, pd.DataFrame(), cf]}, "XNYS:Z", "USD",
                                       "US", LAGS, NOW)
    assert _value(df, "revenue", "FY", "2023-12-31")["value"] == 90
    assert _value(df, "capex", "FY", "2024-12-31")["value"] == 7
    assert df[(df["field"] == "operating_income")]["period_end"].tolist() == [date(2024, 12, 31)]
    assert (df["source"] == "yfinance").all()


# --- cache ----------------------------------------------------------------

def test_raw_cache_reuses_fresh_entries(tmp_path):
    cache = RawCache(tmp_path, {"yahooquery": 1})
    calls = []
    fetch = lambda: calls.append(1) or {"a": 1}  # noqa: E731
    assert cache.fetch("yahooquery", "k", fetch) == {"a": 1}
    assert cache.fetch("yahooquery", "k", fetch) == {"a": 1}
    assert len(calls) == 1
    frame = cache.fetch("yahooquery", "f", lambda: pd.DataFrame({"x": [1]}), kind="frame")
    assert frame["x"].tolist() == [1]
    assert (tmp_path / "yahooquery" / "f.parquet").exists()
