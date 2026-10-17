"""Synthetic vendor payloads shaped like the real responses (no network in tests)."""

from __future__ import annotations

import pandas as pd


def _fact(val, end, filed, form, accn, start=None):
    f = {"val": val, "end": end, "filed": filed, "form": form, "accn": accn,
         "fy": int(filed[:4]), "fp": "FY" if form.startswith("10-K") else "Q"}
    if start:
        f["start"] = start
    return f


def edgar_companyfacts() -> dict:
    """US filer with a September fiscal year-end.

    FY2023 (Oct-22..Sep-23) via 10-K filed 2023-11-15; FY2024 quarters via 10-Qs,
    FY2024 via 10-K filed 2024-11-15.
    """
    k23, q1, q2, q3, k24 = "A-23K", "A-24Q1", "A-24Q2", "A-24Q3", "A-24K"
    usd = lambda *facts: {"units": {"USD": list(facts)}}  # noqa: E731
    return {
        "cik": 1234,
        "entityName": "Test Corp",
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {"units": {"shares": [
                    _fact(1_000_000, "2024-11-01", "2024-11-15", "10-K", k24),
                    _fact(990_000, "2024-07-25", "2024-08-01", "10-Q", q3),
                ]}},
            },
            "us-gaap": {
                # Older years used SalesRevenueNet; issuer switched tags.
                "SalesRevenueNet": usd(
                    _fact(350, "2022-09-30", "2022-11-15", "10-K", "A-22K", "2021-10-01"),
                ),
                "Revenues": usd(
                    _fact(400, "2023-09-30", "2023-11-15", "10-K", k23, "2022-10-01"),
                    _fact(110, "2023-12-31", "2024-02-01", "10-Q", q1, "2023-10-01"),
                    _fact(120, "2024-03-31", "2024-05-01", "10-Q", q2, "2024-01-01"),
                    _fact(230, "2024-03-31", "2024-05-01", "10-Q", q2, "2023-10-01"),
                    _fact(130, "2024-06-30", "2024-08-01", "10-Q", q3, "2024-04-01"),
                    _fact(360, "2024-06-30", "2024-08-01", "10-Q", q3, "2023-10-01"),
                    _fact(500, "2024-09-30", "2024-11-15", "10-K", k24, "2023-10-01"),
                    # Comparative restated in the next 10-K: original must win.
                    _fact(405, "2023-09-30", "2024-11-15", "10-K", k24, "2022-10-01"),
                    # Not a periodic report.
                    _fact(999, "2024-09-30", "2024-10-20", "8-K", "A-8K", "2023-10-01"),
                ),
                # Cash flow is reported year-to-date only in 10-Qs.
                "NetCashProvidedByUsedInOperatingActivities": usd(
                    _fact(50, "2023-12-31", "2024-02-01", "10-Q", q1, "2023-10-01"),
                    _fact(90, "2024-03-31", "2024-05-01", "10-Q", q2, "2023-10-01"),
                    _fact(150, "2024-06-30", "2024-08-01", "10-Q", q3, "2023-10-01"),
                    _fact(210, "2024-09-30", "2024-11-15", "10-K", k24, "2023-10-01"),
                ),
                "PaymentsToAcquirePropertyPlantAndEquipment": usd(
                    _fact(30, "2024-09-30", "2024-11-15", "10-K", k24, "2023-10-01"),
                ),
                "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
                    _fact(1_010_000, "2024-09-30", "2024-11-15", "10-K", k24, "2023-10-01"),
                    _fact(1_005_000, "2024-06-30", "2024-08-01", "10-Q", q3, "2023-10-01"),
                ]}},
                "Assets": usd(
                    _fact(1000, "2023-09-30", "2023-11-15", "10-K", k23),
                    _fact(1010, "2023-12-31", "2024-02-01", "10-Q", q1),
                    _fact(1100, "2024-09-30", "2024-11-15", "10-K", k24),
                    _fact(1, "2024-10-15", "2024-11-15", "10-K", k24),  # odd date
                ),
                # LongTermDebtNoncurrent + DebtCurrent; the Dec-23 quarter lacks
                # DebtCurrent, so no complete alternative exists for it.
                "LongTermDebtNoncurrent": usd(
                    _fact(200, "2024-09-30", "2024-11-15", "10-K", k24),
                    _fact(210, "2023-12-31", "2024-02-01", "10-Q", q1),
                ),
                "DebtCurrent": usd(
                    _fact(50, "2024-09-30", "2024-11-15", "10-K", k24),
                ),
                # Cash and marketable securities filed as separate tags.
                "CashAndCashEquivalentsAtCarryingValue": usd(
                    _fact(100, "2024-09-30", "2024-11-15", "10-K", k24),
                    _fact(90, "2023-12-31", "2024-02-01", "10-Q", q1),
                ),
                "MarketableSecuritiesCurrent": usd(
                    _fact(40, "2024-09-30", "2024-11-15", "10-K", k24),
                ),
            },
        },
    }


def yahoo_modules(symbol: str, *, exchange: str, currency: str, financial_currency: str,
                  sector: str = "Industrials", industry: str = "Machinery",
                  country: str = "United States") -> dict:
    return {
        "summaryProfile": {"sector": sector, "industry": industry, "country": country},
        "price": {"exchange": exchange, "currency": currency, "longName": f"{symbol} Inc.",
                  "regularMarketPrice": 25.0, "marketCap": 2.5e9,
                  "regularMarketTime": "2025-01-31 16:00:00"},
        "summaryDetail": {"beta": 1.1, "averageVolume": 400_000},
        "defaultKeyStatistics": {"sharesOutstanding": 1e8, "shortPercentOfFloat": 0.02},
        "financialData": {"financialCurrency": financial_currency, "recommendationMean": 2.1,
                          "targetMeanPrice": 30.0},
        "earningsTrend": {"trend": [
            {"period": "0y", "earningsEstimate": {"avg": 2.0, "numberOfAnalysts": 8},
             "revenueEstimate": {"avg": 5e8},
             "epsTrend": {"current": 2.0, "7daysAgo": 2.0, "30daysAgo": 2.1,
                          "60daysAgo": 2.2, "90daysAgo": 2.5},
             "epsRevisions": {"upLast30days": 1, "downLast30days": 4}},
            {"period": "+5y", "earningsEstimate": {"avg": None}},
        ]},
        "insiderTransactions": {"transactions": [
            {"filerName": "DOE JANE", "filerRelation": "Chief Executive Officer",
             "transactionText": "Purchase at price 24.00 - 26.00 per share.",
             "shares": 10_000, "value": None, "startDate": "2025-01-10"},
            {"filerName": "ROE RICHARD", "filerRelation": "Director",
             "transactionText": "Stock Award(Grant) at price 0.00 per share.",
             "shares": 5_000, "value": 0, "startDate": "2024-12-01"},
        ]},
    }


def yahoo_financials(symbol: str, currency: str, fy_ends: list[str], q_ends: list[str],
                     scale: float = 1.0) -> pd.DataFrame:
    """Flattened all_financial_data output: one row per asOfDate/periodType."""
    rows = []
    for i, end in enumerate(fy_ends):
        rows.append(_yahoo_row(symbol, end, "12M", currency, 4 * scale * (1 + 0.1 * i)))
    for i, end in enumerate(q_ends):
        rows.append(_yahoo_row(symbol, end, "3M", currency, scale * (1 + 0.02 * i)))
    rows.append(_yahoo_row(symbol, (q_ends or fy_ends)[-1], "TTM", currency, 4 * scale))
    return pd.DataFrame(rows)


def _yahoo_row(symbol, end, period_type, currency, s):
    # Flows scale with `s`; balance-sheet items are constant and balance exactly.
    return {
        "symbol": symbol, "asOfDate": pd.Timestamp(end), "periodType": period_type,
        "currencyCode": currency,
        "TotalRevenue": 1e8 * s, "OperatingIncome": 2e7 * s,
        "NetIncomeCommonStockholders": 1.5e7 * s, "PretaxIncome": 1.9e7 * s,
        "TaxProvision": 4e6 * s, "InterestExpense": 1e6 * s,
        "OperatingCashFlow": 2.2e7 * s, "CapitalExpenditure": -5e6 * s,
        "TotalAssets": 4e8, "TotalLiabilitiesNetMinorityInterest": 1.5e8,
        "StockholdersEquity": 2.5e8, "CashAndCashEquivalents": 5e7,
        "CurrentAssets": 1.2e8, "CurrentLiabilities": 6e7, "TotalDebt": 8e7,
        "RetainedEarnings": 1e8, "OrdinarySharesNumber": 1e8,
    }


def yahoo_history(symbol: str, days: int = 10, split_on: int | None = None) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", periods=days)
    df = pd.DataFrame({
        "symbol": symbol, "date": dates, "open": 24.0, "high": 25.5, "low": 23.5,
        "close": 25.0, "volume": 400_000, "adjclose": 24.8, "dividends": 0.0, "splits": 0.0,
    })
    if split_on is not None:
        df.loc[split_on, "splits"] = 2.0
    return df
