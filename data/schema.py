"""Canonical field map, source mappings, and table definitions.

This is the only module that knows vendor field names. Everything downstream
of data/ reads canonical fields. See docs/schema.md for the specification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

SOURCES = ("edgar", "yahooquery", "yfinance")
PERIOD_TYPES = ("FY", "Q", "TTM")


# ---------------------------------------------------------------------------
# Canonical field dictionary (schema §3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldDef:
    name: str
    statement: Literal["income", "balance", "cashflow"]
    # flow: summed over quarters for TTM. stock: point-in-time balance.
    kind: Literal["flow", "stock"]
    # magnitude: stored as abs(value), direction is in the name (capex, buybacks…).
    # signed: stored as reported.
    sign: Literal["magnitude", "signed"] = "signed"
    unit: Literal["currency", "shares"] = "currency"
    # Source mappings: alternatives in priority order. Each alternative is a
    # tuple of components that are summed. Every component must be present
    # unless prefixed with "?" (optional: added when reported), and at least
    # one component must be present. A "-" prefix subtracts the component. yfinance uses the yahooquery names with
    # spaces.
    yahoo: tuple[tuple[str, ...], ...] = ()
    xbrl: tuple[tuple[str, ...], ...] = ()
    description: str = ""


def _alts(*tags: str | tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(t if isinstance(t, tuple) else (t,) for t in tags)


FIELDS: dict[str, FieldDef] = {f.name: f for f in [
    # Income statement
    FieldDef("revenue", "income", "flow", yahoo=_alts("TotalRevenue"),
             xbrl=_alts("us-gaap:Revenues",
                        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
                        "us-gaap:SalesRevenueNet"),
             description="Total revenue"),
    FieldDef("cost_of_revenue", "income", "flow", yahoo=_alts("CostOfRevenue"),
             xbrl=_alts("us-gaap:CostOfRevenue", "us-gaap:CostOfGoodsAndServicesSold")),
    FieldDef("gross_profit", "income", "flow", yahoo=_alts("GrossProfit"),
             xbrl=_alts("us-gaap:GrossProfit")),
    FieldDef("operating_income", "income", "flow", yahoo=_alts("OperatingIncome"),
             xbrl=_alts("us-gaap:OperatingIncomeLoss"),
             description="Reported operating income"),
    FieldDef("ebitda_reported", "income", "flow", yahoo=_alts("EBITDA", "NormalizedEBITDA"),
             description="Vendor EBITDA, reference only"),
    FieldDef("interest_expense", "income", "flow", sign="magnitude",
             yahoo=_alts("InterestExpense"),
             xbrl=_alts("us-gaap:InterestExpense", "us-gaap:InterestExpenseNonoperating")),
    FieldDef("pretax_income", "income", "flow", yahoo=_alts("PretaxIncome"),
             xbrl=_alts("us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                        "ExtraordinaryItemsNoncontrollingInterest")),
    FieldDef("income_tax", "income", "flow", yahoo=_alts("TaxProvision"),
             xbrl=_alts("us-gaap:IncomeTaxExpenseBenefit")),
    FieldDef("net_income", "income", "flow", yahoo=_alts("NetIncomeCommonStockholders"),
             xbrl=_alts("us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
                        "us-gaap:NetIncomeLoss"),
             description="To common shareholders"),
    FieldDef("diluted_shares_avg", "income", "flow", unit="shares",
             yahoo=_alts("DilutedAverageShares"),
             xbrl=_alts("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding")),

    # Balance sheet
    FieldDef("total_assets", "balance", "stock", yahoo=_alts("TotalAssets"),
             xbrl=_alts("us-gaap:Assets")),
    FieldDef("current_assets", "balance", "stock", yahoo=_alts("CurrentAssets"),
             xbrl=_alts("us-gaap:AssetsCurrent")),
    FieldDef("cash", "balance", "stock",
             yahoo=_alts("CashCashEquivalentsAndShortTermInvestments", "CashAndCashEquivalents"),
             xbrl=_alts("us-gaap:CashCashEquivalentsAndShortTermInvestments",
                        ("us-gaap:CashAndCashEquivalentsAtCarryingValue",
                         "?us-gaap:ShortTermInvestments",
                         "?us-gaap:MarketableSecuritiesCurrent",
                         "?us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
                        # Last resort, includes restricted cash (cash-flow statement total).
                        ("us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                         "?us-gaap:ShortTermInvestments",
                         "?us-gaap:MarketableSecuritiesCurrent")),
             description="Cash + short-term investments"),
    FieldDef("receivables", "balance", "stock", yahoo=_alts("AccountsReceivable"),
             xbrl=_alts("us-gaap:AccountsReceivableNetCurrent")),
    FieldDef("inventory", "balance", "stock", yahoo=_alts("Inventory"),
             xbrl=_alts("us-gaap:InventoryNet")),
    FieldDef("net_ppe", "balance", "stock", yahoo=_alts("NetPPE"),
             xbrl=_alts("us-gaap:PropertyPlantAndEquipmentNet")),
    FieldDef("goodwill_intangibles", "balance", "stock",
             yahoo=_alts("GoodwillAndOtherIntangibleAssets"),
             xbrl=_alts(("us-gaap:Goodwill", "us-gaap:IntangibleAssetsNetExcludingGoodwill"))),
    FieldDef("current_liabilities", "balance", "stock", yahoo=_alts("CurrentLiabilities"),
             xbrl=_alts("us-gaap:LiabilitiesCurrent")),
    FieldDef("payables", "balance", "stock", yahoo=_alts("AccountsPayable"),
             xbrl=_alts("us-gaap:AccountsPayableCurrent")),
    FieldDef("total_liabilities", "balance", "stock",
             yahoo=_alts("TotalLiabilitiesNetMinorityInterest"),
             xbrl=_alts("us-gaap:Liabilities")),
    # Excludes lease liabilities (config debt.include_leases; schema §8).
    # us-gaap LongTermDebt already includes the current portion, and DebtCurrent
    # already includes short-term borrowings.
    FieldDef("total_debt", "balance", "stock",
             yahoo=_alts(("LongTermDebt", "?CurrentDebt"), "TotalDebt"),
             xbrl=_alts(("us-gaap:LongTermDebtNoncurrent", "us-gaap:DebtCurrent"),
                        ("us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebtCurrent",
                         "?us-gaap:CommercialPaper", "?us-gaap:ShortTermBorrowings"),
                        ("us-gaap:LongTermDebt", "?us-gaap:CommercialPaper",
                         "?us-gaap:ShortTermBorrowings")),
             description="Short + long-term debt, excluding leases"),
    FieldDef("minority_interest", "balance", "stock", yahoo=_alts("MinorityInterest"),
             xbrl=_alts("us-gaap:MinorityInterest")),
    FieldDef("total_equity", "balance", "stock", yahoo=_alts("StockholdersEquity"),
             xbrl=_alts("us-gaap:StockholdersEquity",
                        ("us-gaap:StockholdersEquityIncludingPortionAttributableTo"
                         "NoncontrollingInterest", "?-us-gaap:MinorityInterest")),
             description="Common shareholders' equity"),
    FieldDef("retained_earnings", "balance", "stock", yahoo=_alts("RetainedEarnings"),
             xbrl=_alts("us-gaap:RetainedEarningsAccumulatedDeficit")),
    FieldDef("shares_outstanding", "balance", "stock", unit="shares",
             yahoo=_alts("OrdinarySharesNumber"),
             xbrl=_alts("dei:EntityCommonStockSharesOutstanding"),
             description="Period-end shares outstanding"),

    # Cash flow statement
    FieldDef("operating_cash_flow", "cashflow", "flow", yahoo=_alts("OperatingCashFlow"),
             xbrl=_alts("us-gaap:NetCashProvidedByUsedInOperatingActivities")),
    FieldDef("capex", "cashflow", "flow", sign="magnitude", yahoo=_alts("CapitalExpenditure"),
             xbrl=_alts("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment")),
    FieldDef("depreciation_amortization", "cashflow", "flow",
             # Depletion included: for miners and royalty companies it is most of
             # the charge (WPM 2025: D&A $1.3M, with depletion $305M).
             yahoo=_alts("DepreciationAmortizationDepletion", "DepreciationAndAmortization",
                         "ReconciledDepreciation"),
             xbrl=_alts("us-gaap:DepreciationDepletionAndAmortization")),
    FieldDef("sbc", "cashflow", "flow", sign="magnitude", yahoo=_alts("StockBasedCompensation"),
             xbrl=_alts("us-gaap:ShareBasedCompensation")),
    FieldDef("buybacks", "cashflow", "flow", sign="magnitude",
             yahoo=_alts("RepurchaseOfCapitalStock"),
             xbrl=_alts("us-gaap:PaymentsForRepurchaseOfCommonStock")),
    FieldDef("dividends_paid", "cashflow", "flow", sign="magnitude",
             yahoo=_alts("CashDividendsPaid"),
             xbrl=_alts("us-gaap:PaymentsOfDividends", "us-gaap:PaymentsOfDividendsCommonStock")),
]}

FLOW_FIELDS = frozenset(f.name for f in FIELDS.values() if f.kind == "flow")
STOCK_FIELDS = frozenset(f.name for f in FIELDS.values() if f.kind == "stock")
CURRENCY_FIELDS = frozenset(f.name for f in FIELDS.values() if f.unit == "currency")


def yfinance_name(yahoo_name: str) -> str:
    """yahooquery CamelCase → yfinance spaced name: NetPPE → 'Net PPE'."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", yahoo_name)


def parse_component(component: str) -> tuple[str, bool, int]:
    """'?-us-gaap:MinorityInterest' → ('us-gaap:MinorityInterest', optional=True, sign=-1).
    "?" marks an optional component, "-" one that is subtracted."""
    optional = component.startswith("?")
    component = component.lstrip("?")
    sign = -1 if component.startswith("-") else 1
    return component.lstrip("-"), optional, sign


def resolve_alternatives(lookup, alternatives: tuple[tuple[str, ...], ...]) -> float | None:
    """First alternative that resolves, given lookup(name) -> float | None."""
    for alternative in alternatives:
        total, found = 0.0, False
        for component in alternative:
            name, optional, sign = parse_component(component)
            value = lookup(name)
            if value is None:
                if optional:
                    continue
                break
            total += sign * value
            found = True
        else:
            if found:
                return total
    return None


def apply_sign(field: str, value: float | None) -> float | None:
    if value is None:
        return None
    return abs(value) if FIELDS[field].sign == "magnitude" else value


# ---------------------------------------------------------------------------
# Security identifiers
# ---------------------------------------------------------------------------

# Yahoo exchange code → (MIC, exchange label)
YAHOO_EXCHANGES = {
    "NMS": ("XNAS", "NASDAQ"), "NGM": ("XNAS", "NASDAQ"), "NCM": ("XNAS", "NASDAQ"),
    "NAS": ("XNAS", "NASDAQ"), "NYQ": ("XNYS", "NYSE"), "ASE": ("XASE", "NYSEAMERICAN"),
    "PCX": ("ARCX", "NYSEARCA"), "BTS": ("BATS", "CBOE"),
    "TOR": ("XTSE", "TSX"), "VAN": ("XTSX", "TSXV"), "NEO": ("NEOE", "CBOE_CA"),
    "CNQ": ("XCNQ", "CSE"),
}
# Fallback by Yahoo symbol suffix when the exchange code is unknown.
YAHOO_SUFFIXES = {
    ".TO": ("XTSE", "TSX"), ".V": ("XTSX", "TSXV"),
    ".NE": ("NEOE", "CBOE_CA"), ".CN": ("XCNQ", "CSE"),
}
CANADIAN_EXCHANGES = frozenset({"TSX", "TSXV", "CBOE_CA", "CSE"})


def native_ticker(yahoo_symbol: str) -> str:
    """RUS.TO → RUS, CCL-B.TO → CCL.B, BRK-B → BRK.B."""
    base = yahoo_symbol
    for suffix in YAHOO_SUFFIXES:
        if base.upper().endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.replace("-", ".")


def exchange_for(yahoo_symbol: str, yahoo_exchange_code: str | None) -> tuple[str, str]:
    """Return (MIC, exchange label)."""
    if yahoo_exchange_code and yahoo_exchange_code in YAHOO_EXCHANGES:
        return YAHOO_EXCHANGES[yahoo_exchange_code]
    for suffix, value in YAHOO_SUFFIXES.items():
        if yahoo_symbol.upper().endswith(suffix):
            return value
    return ("XXXX", "UNKNOWN")


def security_id_for(yahoo_symbol: str, yahoo_exchange_code: str | None = None) -> str:
    mic, _ = exchange_for(yahoo_symbol, yahoo_exchange_code)
    return f"{mic}:{native_ticker(yahoo_symbol)}"


def availability_region(exchange: str) -> str:
    """Key into config availability_lag_days."""
    if exchange == "TSXV":
        return "TSXV"
    if exchange in CANADIAN_EXCHANGES:
        return "TSX"
    return "US"


# ---------------------------------------------------------------------------
# Tables (schema §2)
# ---------------------------------------------------------------------------

def quoted(columns) -> str:
    """Comma-separated, double-quoted identifiers (some names, e.g. window, are reserved)."""
    return ", ".join(f'"{c}"' for c in columns)


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[tuple[str, str], ...]
    primary_key: tuple[str, ...] = ()

    @property
    def column_names(self) -> list[str]:
        return [c for c, _ in self.columns]

    def ddl(self) -> str:
        cols = [f'"{name}" {dtype}' for name, dtype in self.columns]
        if self.primary_key:
            cols.append(f"PRIMARY KEY ({quoted(self.primary_key)})")
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n  " + ",\n  ".join(cols) + "\n)"


TABLES: dict[str, Table] = {t.name: t for t in [
    Table("securities", (
        ("security_id", "VARCHAR"), ("ticker", "VARCHAR"), ("yahoo_symbol", "VARCHAR"),
        ("exchange", "VARCHAR"), ("cik", "VARCHAR"), ("name", "VARCHAR"),
        ("country", "VARCHAR"), ("trading_currency", "VARCHAR"),
        ("reporting_currency", "VARCHAR"), ("sector", "VARCHAR"), ("industry", "VARCHAR"),
        ("is_active", "BOOLEAN"), ("first_seen", "DATE"), ("last_seen", "DATE"),
        ("quote_type", "VARCHAR"),  # Yahoo quoteType: EQUITY, ETF, MUTUALFUND…
    ), ("security_id",)),
    Table("prices_daily", (
        ("security_id", "VARCHAR"), ("date", "DATE"), ("open", "DOUBLE"), ("high", "DOUBLE"),
        ("low", "DOUBLE"), ("close", "DOUBLE"), ("adj_close", "DOUBLE"),
        ("volume", "BIGINT"), ("currency", "VARCHAR"),
    ), ("security_id", "date")),
    Table("fundamentals", (
        ("security_id", "VARCHAR"), ("field", "VARCHAR"), ("period_type", "VARCHAR"),
        ("period_end", "DATE"), ("fiscal_year", "INTEGER"), ("fiscal_period", "VARCHAR"),
        ("value", "DOUBLE"), ("currency", "VARCHAR"), ("available_date", "DATE"),
        ("available_date_estimated", "BOOLEAN"), ("source", "VARCHAR"),
        ("retrieved_at", "TIMESTAMP"),
    ), ("security_id", "field", "period_type", "period_end", "source")),
    Table("market_snapshot", (
        ("run_id", "VARCHAR"), ("security_id", "VARCHAR"), ("as_of", "DATE"),
        ("price", "DOUBLE"), ("currency", "VARCHAR"), ("shares_outstanding", "DOUBLE"),
        ("market_cap", "DOUBLE"), ("beta", "DOUBLE"), ("avg_volume_3m", "DOUBLE"),
        ("short_pct_float", "DOUBLE"),
    ), ("run_id", "security_id")),
    Table("estimates_snapshot", (
        ("run_id", "VARCHAR"), ("security_id", "VARCHAR"), ("as_of", "DATE"),
        ("horizon", "VARCHAR"), ("eps_mean", "DOUBLE"), ("eps_mean_7d_ago", "DOUBLE"),
        ("eps_mean_30d_ago", "DOUBLE"), ("eps_mean_60d_ago", "DOUBLE"),
        ("eps_mean_90d_ago", "DOUBLE"), ("revisions_up_30d", "INTEGER"),
        ("revisions_down_30d", "INTEGER"), ("revenue_mean", "DOUBLE"),
        ("num_analysts", "INTEGER"), ("rating_mean", "DOUBLE"),
        ("target_price_mean", "DOUBLE"),
    ), ("run_id", "security_id", "horizon")),
    Table("insider_transactions", (
        ("security_id", "VARCHAR"), ("trade_date", "DATE"), ("filing_date", "DATE"),
        ("insider_name", "VARCHAR"), ("insider_role", "VARCHAR"),
        ("transaction_type", "VARCHAR"), ("shares", "DOUBLE"), ("price", "DOUBLE"),
        ("value", "DOUBLE"), ("source", "VARCHAR"),
    ), ("security_id", "trade_date", "insider_name", "transaction_type", "shares", "source")),
    # Splits and similar events, so share-count jumps can be explained (schema §6).
    Table("corporate_actions", (
        ("security_id", "VARCHAR"), ("date", "DATE"), ("action_type", "VARCHAR"),
        ("ratio", "DOUBLE"), ("source", "VARCHAR"),
    ), ("security_id", "date", "action_type")),
    Table("validation_issues", (
        ("run_id", "VARCHAR"), ("security_id", "VARCHAR"), ("check_name", "VARCHAR"),
        ("severity", "VARCHAR"), ("field", "VARCHAR"), ("period_type", "VARCHAR"),
        ("period_end", "DATE"), ("detail", "VARCHAR"),
    )),
    Table("universe_membership", (
        ("run_id", "VARCHAR"), ("security_id", "VARCHAR"), ("passed", "BOOLEAN"),
        ("reason", "VARCHAR"), ("market_cap_ref", "DOUBLE"), ("dollar_volume_ref", "DOUBLE"),
    ), ("run_id", "security_id")),
    Table("factor_scores", (
        ("run_id", "VARCHAR"), ("security_id", "VARCHAR"), ("factor", "VARCHAR"),
        ("raw_value", "DOUBLE"), ("peer_group", "VARCHAR"), ("peer_count", "INTEGER"),
        ("percentile", "DOUBLE"),
    ), ("run_id", "security_id", "factor")),
    Table("valuations", (
        ("run_id", "VARCHAR"), ("security_id", "VARCHAR"), ("method", "VARCHAR"),
        ("value_per_share", "DOUBLE"), ("implied_growth", "DOUBLE"),
        ("assumptions", "JSON"), ("currency", "VARCHAR"),
    ), ("run_id", "security_id", "method")),
    Table("diagnosis", (
        ("run_id", "VARCHAR"), ("security_id", "VARCHAR"), ("window", "VARCHAR"),
        ("total_return", "DOUBLE"), ("market_component", "DOUBLE"),
        ("sector_component", "DOUBLE"), ("idiosyncratic_component", "DOUBLE"),
        ("deterioration_flags", "JSON"), ("label", "VARCHAR"),
    ), ("run_id", "security_id", "window")),
    Table("watchlist", (
        ("security_id", "VARCHAR"), ("state", "VARCHAR"),
        ("entered_state_at", "TIMESTAMP"), ("notes", "VARCHAR"),
    ), ("security_id",)),
    Table("run_log", (
        ("run_id", "VARCHAR"), ("config_hash", "VARCHAR"), ("code_version", "VARCHAR"),
        ("universe_size", "INTEGER"), ("notes", "VARCHAR"),
        ("started_at", "TIMESTAMP"), ("finished_at", "TIMESTAMP"),
    ), ("run_id",)),
]}

# Tables whose rows belong to a single run and go into that run's snapshot as-is.
RUN_SCOPED_TABLES = ("market_snapshot", "estimates_snapshot", "validation_issues",
                     "universe_membership", "factor_scores", "valuations", "diagnosis")
