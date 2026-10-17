# Canonical Data Schema

**Purpose:** one internal representation of market and fundamental data, regardless of source (yahooquery, yfinance, SEC EDGAR XBRL). Every downstream module (scoring, diagnosis, valuation) reads only canonical fields; nothing outside `data/` should know a Yahoo field name.

---

## 1. Conventions

| Topic | Rule |
|---|---|
| **Names** | `snake_case`, singular, no abbreviations except standard ones (`ebit`, `ebitda`, `ppe`, `sbc`, `fcf`). |
| **Units** | Absolute units in the **reporting currency** (no thousands/millions). Shares in units. Ratios as decimals (0.15, not 15%). |
| **Currency** | Every fundamental row carries `currency`. Market data carries its own `currency`. They are reconciled before any market-vs-fundamental ratio is computed (see §6). |
| **Signs** | Flows with an obvious direction are stored as **positive magnitudes** and named by direction: `capex`, `interest_expense`, `dividends_paid`, `buybacks`, `sbc`. Items that can legitimately be either sign are stored **signed**: `net_income`, `operating_income`, `fcf`, `retained_earnings`. |
| **Missing data** | `NULL`, never 0. A derived metric with a null input is null. |
| **Periods** | `period_type` ∈ {`FY`, `Q`, `TTM`}. `period_end` is the fiscal period end date. |
| **Availability** | Every fundamental row has `available_date`: the first date the figure could have been known (see §5). |
| **Provenance** | Every row has `source` ∈ {`edgar`, `yahooquery`, `yfinance`} and `retrieved_at`. |

**Source priority** when several sources supply the same field: `edgar` > `yahooquery` > `yfinance`. Conflicts above a tolerance are logged, not silently overwritten.

---

## 2. Tables

### 2.1 `securities` — security master
| Column | Type | Notes |
|---|---|---|
| `security_id` | TEXT PK | Internal stable ID, e.g. `XTSE:RUS`, `XNAS:AAPL` |
| `ticker` | TEXT | Exchange-native symbol (`RUS`, `BAM.A`) |
| `yahoo_symbol` | TEXT | `RUS.TO`, `CCL-B.TO`, `AAPL` |
| `exchange` | TEXT | `TSX`, `TSXV`, `NASDAQ`, `NYSE`, … |
| `cik` | TEXT NULL | SEC CIK for EDGAR lookups (US filers) |
| `name` | TEXT | |
| `country` | TEXT | Country of domicile |
| `trading_currency` | TEXT | Currency of the quote |
| `reporting_currency` | TEXT | Currency of the financial statements |
| `sector` | TEXT | |
| `industry` | TEXT | **Peer grouping key** (D-002) |
| `is_active` | BOOL | |
| `first_seen`, `last_seen` | DATE | Preserves delisted names (survivorship) |

### 2.2 `prices_daily`
| Column | Type | Notes |
|---|---|---|
| `security_id` | TEXT | |
| `date` | DATE | |
| `open`, `high`, `low`, `close` | REAL | Unadjusted |
| `adj_close` | REAL | Split- and dividend-adjusted |
| `volume` | INTEGER | |
| `currency` | TEXT | |

PK: (`security_id`, `date`).

### 2.3 `fundamentals` — long format
| Column | Type | Notes |
|---|---|---|
| `security_id` | TEXT | |
| `field` | TEXT | Canonical field name (§3) |
| `period_type` | TEXT | `FY` / `Q` / `TTM` |
| `period_end` | DATE | |
| `fiscal_year` | INTEGER | |
| `fiscal_period` | TEXT | `FY`, `Q1`–`Q4` |
| `value` | REAL | |
| `currency` | TEXT | Reporting currency |
| `available_date` | DATE | Actual filing date or estimated (§5) |
| `available_date_estimated` | BOOL | |
| `source` | TEXT | |
| `retrieved_at` | TIMESTAMP | |

PK: (`security_id`, `field`, `period_type`, `period_end`, `source`). Long format makes merging sources and adding fields trivial; a wide view is derived for analysis.

### 2.4 `market_snapshot` — one row per security per run
| Column | Type | Notes |
|---|---|---|
| `run_id` | TEXT | FK → `run_log` |
| `security_id` | TEXT | |
| `as_of` | DATE | |
| `price` | REAL | Trading currency |
| `currency` | TEXT | Trading currency of `price` and `market_cap` |
| `shares_outstanding` | REAL | |
| `market_cap` | REAL | Trading currency |
| `beta` | REAL NULL | |
| `avg_volume_3m` | REAL | |
| `short_pct_float` | REAL NULL | |

### 2.5 `estimates_snapshot`
Captures consensus *and its history* so revisions can be measured (D-008).

| Column | Type | Notes |
|---|---|---|
| `run_id`, `security_id`, `as_of` | | |
| `horizon` | TEXT | `0q`, `+1q`, `0y`, `+1y` |
| `eps_mean` | REAL | Current consensus |
| `eps_mean_7d_ago`, `eps_mean_30d_ago`, `eps_mean_60d_ago`, `eps_mean_90d_ago` | REAL | |
| `revisions_up_30d`, `revisions_down_30d` | INTEGER | |
| `revenue_mean` | REAL NULL | |
| `num_analysts` | INTEGER | |
| `rating_mean` | REAL NULL | Displayed only, not scored |
| `target_price_mean` | REAL NULL | Displayed only |

### 2.6 `insider_transactions`
| Column | Type | Notes |
|---|---|---|
| `security_id` | TEXT | |
| `trade_date`, `filing_date` | DATE | |
| `insider_name` | TEXT | |
| `insider_role` | TEXT | `ceo`, `cfo`, `director`, `officer`, `10pct_owner`, `other` |
| `transaction_type` | TEXT | `open_market_buy`, `open_market_sale`, `award`, `option_exercise`, `gift`, `other` |
| `shares` | REAL | Positive magnitude |
| `price` | REAL NULL | |
| `value` | REAL NULL | `shares × price` |
| `source` | TEXT | |

Only `open_market_buy` / `open_market_sale` feed the insider factor; awards and exercises are kept for context.

### 2.6a `corporate_actions`
| Column | Type | Notes |
|---|---|---|
| `security_id`, `date` | | |
| `action_type` | TEXT | `split` (more types later) |
| `ratio` | REAL | New shares per old share |
| `source` | TEXT | |

The "corporate-action record" the share-count-jump check (§6) looks for.

### 2.6b `validation_issues`
`run_id`, `security_id`, `check_name`, `severity` (`drop` / `warn` / `info`), `field`, `period_type`, `period_end`, `detail`. One row per failed check per run (§6).

### 2.7 Output tables
**`factor_scores`** — `run_id`, `security_id`, `factor`, `raw_value`, `peer_group`, `peer_count`, `percentile`, plus one `composite` row per security.

**`valuations`** — `run_id`, `security_id`, `method` (`relative_peer`, `relative_history`, `reverse_dcf`, `dcf_bear`, `dcf_base`, `dcf_bull`), `value_per_share`, `implied_growth` (reverse DCF), `assumptions` (JSON), `currency`.

**`diagnosis`** — `run_id`, `security_id`, `window`, `total_return`, `market_component`, `sector_component`, `idiosyncratic_component`, `deterioration_flags` (JSON), `label`.

**`watchlist`** — `security_id`, `state`, `entered_state_at`, `notes`.

### 2.8 `run_log`
| Column | Notes |
|---|---|
| `run_id` | UTC timestamp, e.g. `2026-10-16T22:00:00Z`; snapshot folders drop the colons |
| `config_hash` | Hash of `config.yaml` used |
| `code_version` | Git commit SHA |
| `universe_size`, `notes` | Securities that passed validation; source errors are appended to `notes` |
| `started_at`, `finished_at` | UTC |

Every output row traces back to the exact data, config, and code that produced it.

---

## 3. Canonical field dictionary

yfinance uses the same names as yahooquery with spaces (`TotalRevenue` → `Total Revenue`). XBRL tags are the common `us-gaap` candidates. `→` separates alternatives in priority order (resolved per period); `+` sums components, which must all be present unless marked `?` (optional: added when reported). Cash and debt mappings were checked against AAPL and MSFT 10-K figures on the first live run (2026-10-17): `us-gaap:LongTermDebt` already includes the current portion and `DebtCurrent` already includes short-term borrowings, so they are never summed together, and Yahoo's `TotalDebt` includes finance-lease liabilities for some issuers, so `CurrentDebt + LongTermDebt` is preferred.

### Income statement
| Field | Definition | yahooquery | XBRL (us-gaap) |
|---|---|---|---|
| `revenue` | Total revenue | `TotalRevenue` | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet` |
| `cost_of_revenue` | | `CostOfRevenue` | `CostOfRevenue`, `CostOfGoodsAndServicesSold` |
| `gross_profit` | | `GrossProfit` | `GrossProfit` |
| `operating_income` | Reported operating income | `OperatingIncome` | `OperatingIncomeLoss` |
| `ebitda_reported` | Vendor EBITDA (reference only) | `EBITDA`, `NormalizedEBITDA` | — |
| `interest_expense` | Positive magnitude | `InterestExpense` | `InterestExpense`, `InterestExpenseNonoperating` |
| `pretax_income` | | `PretaxIncome` | `IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest` |
| `income_tax` | | `TaxProvision` | `IncomeTaxExpenseBenefit` |
| `net_income` | To common shareholders | `NetIncomeCommonStockholders` | `NetIncomeLossAvailableToCommonStockholdersBasic`, `NetIncomeLoss` |
| `diluted_shares_avg` | | `DilutedAverageShares` | `WeightedAverageNumberOfDilutedSharesOutstanding` |

### Balance sheet
| Field | Definition | yahooquery | XBRL (us-gaap) |
|---|---|---|---|
| `total_assets` | | `TotalAssets` | `Assets` |
| `current_assets` | | `CurrentAssets` | `AssetsCurrent` |
| `cash` | Cash + short-term investments | `CashCashEquivalentsAndShortTermInvestments` → `CashAndCashEquivalents` | `CashCashEquivalentsAndShortTermInvestments` → `CashAndCashEquivalentsAtCarryingValue` + ?`ShortTermInvestments` + ?`MarketableSecuritiesCurrent` + ?`AvailableForSaleSecuritiesDebtSecuritiesCurrent` |
| `receivables` | | `AccountsReceivable` | `AccountsReceivableNetCurrent` |
| `inventory` | | `Inventory` | `InventoryNet` |
| `net_ppe` | | `NetPPE` | `PropertyPlantAndEquipmentNet` |
| `goodwill_intangibles` | | `GoodwillAndOtherIntangibleAssets` | `Goodwill` + `IntangibleAssetsNetExcludingGoodwill` |
| `current_liabilities` | | `CurrentLiabilities` | `LiabilitiesCurrent` |
| `payables` | | `AccountsPayable` | `AccountsPayableCurrent` |
| `total_liabilities` | | `TotalLiabilitiesNetMinorityInterest` | `Liabilities` |
| `total_debt` | Short + long-term debt, **excluding leases** (§8) | ?`CurrentDebt` + ?`LongTermDebt` → `TotalDebt` | `LongTermDebtNoncurrent` + `DebtCurrent` → `LongTermDebtNoncurrent` + `LongTermDebtCurrent` + ?`CommercialPaper` + ?`ShortTermBorrowings` → `LongTermDebt` + ?`CommercialPaper` + ?`ShortTermBorrowings` |
| `minority_interest` | | `MinorityInterest` | `MinorityInterest` |
| `total_equity` | Common shareholders' equity | `StockholdersEquity` | `StockholdersEquity` |
| `retained_earnings` | Signed | `RetainedEarnings` | `RetainedEarningsAccumulatedDeficit` |
| `shares_outstanding` | Period-end | `OrdinarySharesNumber` | `dei:EntityCommonStockSharesOutstanding` |

### Cash flow statement
| Field | Definition | yahooquery | XBRL (us-gaap) |
|---|---|---|---|
| `operating_cash_flow` | | `OperatingCashFlow` | `NetCashProvidedByUsedInOperatingActivities` |
| `capex` | Positive magnitude | `CapitalExpenditure` (negate) | `PaymentsToAcquirePropertyPlantAndEquipment` |
| `depreciation_amortization` | | `DepreciationAndAmortization`, `ReconciledDepreciation` | `DepreciationDepletionAndAmortization` |
| `sbc` | Stock-based compensation | `StockBasedCompensation` | `ShareBasedCompensation` |
| `buybacks` | Positive magnitude | `RepurchaseOfCapitalStock` (negate) | `PaymentsForRepurchaseOfCommonStock` |
| `dividends_paid` | Positive magnitude | `CashDividendsPaid` (negate) | `PaymentsOfDividends`, `PaymentsOfDividendsCommonStock` |

---

## 4. Derived metrics

Computed in one place (`screen/factors.py`), never fetched from a vendor. Formulas are fixed here so they can't drift between modules.

| Metric | Formula | Notes |
|---|---|---|
| `ebit` | `operating_income` | **Never** falls back to pre-tax income (D-005) |
| `ebitda` | `ebit + depreciation_amortization` | Vendor EBITDA kept only for comparison |
| `tax_rate` | `income_tax / pretax_income`, clipped to [0, 0.40] | If pretax ≤ 0 or null → statutory fallback by country (config) |
| `nopat` | `ebit × (1 − tax_rate)` | |
| `invested_capital` | `total_debt + total_equity + minority_interest − cash` | Average of opening and closing balances |
| `roic` | `nopat / invested_capital` | Null if invested capital ≤ 0 |
| `fcf` | `operating_cash_flow − capex` | Optional SBC-adjusted variant: `fcf − sbc` |
| `fcf_conversion` | `fcf / net_income` | Null if net income ≤ 0 |
| `operating_margin` | `ebit / revenue` | |
| `margin_stability` | Std. dev. of `operating_margin` over up to 5 FY | Lower = better; null if < 3 years |
| `revenue_growth_3y` | 3-year CAGR of `revenue` | |
| `interest_coverage` | `ebit / interest_expense` | Null if interest expense is immaterial; scored as top percentile, **not** set to 100 |
| `net_debt` | `total_debt − cash` | |
| `enterprise_value` | `market_cap + total_debt + minority_interest − cash` | After currency reconciliation (§6) |
| `ev_ebit`, `ev_ebitda`, `ev_sales` | | Null if denominator ≤ 0 |
| `fcf_yield` | `fcf / market_cap` | Levered; unlevered variant uses EV |
| `altman_z2` | `6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4` | X1 = WC/TA, X2 = RE/TA, X3 = EBIT/TA, X4 = book equity / total liabilities (D-004) |
| `eps_revision_90d` | `(eps_mean − eps_mean_90d_ago) / abs(eps_mean_90d_ago)` | Horizon `0y` and `+1y` |
| `drawdown_52w` | `price / max(high, 252d) − 1` | |
| `momentum_12_1` | Return from t−252 to t−21 trading days | Skips the most recent month |
| `insider_net_buy_value_6m` | Σ open-market buy value − Σ open-market sale value, last 6 months | Officers and directors only |

TTM values are the sum of the last four quarters for flow items and the latest quarter for balance-sheet items.

---

## 5. Point-in-time rules

A fundamental value may be used on date *D* only if `available_date ≤ D`.

| Source | `available_date` |
|---|---|
| EDGAR | Actual filing date (`filed`) |
| Yahoo, US filer | `period_end` + 45 days (quarterly) / + 90 days (annual) — conservative estimate |
| Yahoo, Canadian filer | `period_end` + 45 / + 90 days (TSX); + 60 / + 120 days (TSX-V) |

Estimated dates are flagged (`available_date_estimated = TRUE`) and can be replaced when an actual filing date is obtained.

---

## 6. Validation checks (`data/validate.py`)

Run on every ingest; failures are logged to the run and, if severe, drop the security from that run's universe.

| Check | Rule | Severity |
|---|---|---|
| **Currency mismatch** | `trading_currency ≠ reporting_currency` → convert fundamentals at the period-end FX rate before computing EV or multiples (common for TSX miners reporting in USD) | Must handle |
| **Staleness** | Latest FY `period_end` older than 15 months (US, TSX) / 16 months (TSX-V, whose annual filings are due 120 days after year-end) | Drop |
| **Balance identity** | `abs(total_assets − total_liabilities − total_equity − minority_interest) / total_assets > 2%` | Warn |
| **Sign sanity** | `revenue < 0`, `total_assets ≤ 0`, `shares_outstanding ≤ 0` | Drop |
| **Share count jump** | Shares change > 50% between periods without a corporate-action record | Warn |
| **Source conflict** | Same field/period differs > 5% across sources | Warn, keep higher-priority source |
| **Completeness** | Fewer than N required fields present (N in config) | Drop at universe stage |
| **Outliers** | Any ratio beyond configured bounds (e.g. \|ROIC\| > 200%) | Warn, flag for review |

---

## 7. Storage

- **DuckDB** single-file database for all tables (fast analytical queries, zero server setup).
- Each run also writes **Parquet snapshots** to `snapshots/{run_id}/`, so any past run can be reproduced exactly.
- Raw API responses are cached separately under `cache/raw/{source}/` with a TTL from config. The cache is an optimization; the snapshot is the record.

---

## 8. Open decisions

- Whether `total_debt` includes lease liabilities (IFRS 16 / ASC 842); this affects EV and ROIC comparability between Canadian and US filers.
- FX rate source for currency reconciliation.
- Minimum peer count before falling back to own-history comparison.
- Statutory tax-rate fallbacks by country.
