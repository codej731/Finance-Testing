# Methodology

How each stage turns raw data into its output. Field definitions and formulas
live in [`schema.md`](schema.md); this document covers the *procedures*.

---

## 1. Data stage (v1.0)

A run is `python -m data.pipeline <symbols>`. It ingests every source, writes
canonical rows to DuckDB, validates, and snapshots the run.

### 1.1 Sources and what each supplies

| Source | Supplies | Availability date |
|---|---|---|
| yahooquery `quoteSummary` | security master, market snapshot, estimates, insider trades | n/a |
| yahooquery `all_financial_data` | FY and quarterly statements (US + Canada) | estimated (schema §5) |
| yahooquery `history` | daily prices, splits | n/a |
| yfinance | statements, only for securities yahooquery returned nothing for | estimated |
| SEC EDGAR `companyfacts` | FY and quarterly statements for US filers with a CIK | actual filing date |

All sources are stored side by side in `fundamentals` (the `source` column is
part of the key). Priority is applied when reading, not when writing, so a
lower-priority value is never lost and conflicts stay auditable.

### 1.2 EDGAR normalization

The XBRL `companyfacts` feed needs several corrections before it matches the
canonical schema:

- **Original filing wins.** Each 10-K/10-Q repeats prior periods as
  comparatives, sometimes restated. For every period the value from the
  *earliest* filing is kept and its `filed` date becomes `available_date`.
  This is what an investor could have known at the time (point-in-time), at
  the cost of ignoring later restatements.
- **Only periodic reports.** Facts from 10-K, 10-Q, 20-F, 40-F (and
  amendments) are used; 8-Ks and other forms are ignored.
- **Year-to-date flows are differenced.** 10-Q cash-flow statements report
  six- and nine-month year-to-date figures, and no filing reports Q4 on its
  own. For currency-denominated flows, a missing discrete quarter is derived
  as `YTD(n) − YTD(n−1)` for durations sharing the same fiscal-year start
  (so Q4 = FY − 9M). Its availability date is the later of the two filings.
  Share-count averages are never differenced.
- **Tag priority is per period.** Issuers change tags over time (e.g.
  `SalesRevenueNet` → `RevenueFromContractWithCustomer…`). The first listed
  alternative that has a value *for that period* is used.
- **Summed alternatives need every required component.** `cash`,
  `total_debt` and `goodwill_intangibles` are sums of tags; if a required
  component is missing the alternative is skipped rather than treating the gap
  as zero (D-006). Components marked optional (`?` in `schema.py`, e.g.
  commercial paper, marketable securities) are added only when reported,
  because companies without them simply don't file the tag.
- **Balance-sheet instants** are kept only on report period ends, tagged `Q`
  for every period end and additionally `FY` on fiscal year-ends.
- **Cover-page shares** (`dei:EntityCommonStockSharesOutstanding`) are dated
  at the cover date, weeks after the period. They are attached to the report
  period of the filing they appear in.

### 1.3 Yahoo normalization

- Vendor TTM rows are dropped; TTM is always derived from quarters (§1.5).
- Fields stored as positive magnitudes (`capex`, `buybacks`,
  `dividends_paid`, `interest_expense`, `sbc`) are `abs()` of the vendor value.
- `available_date` = `period_end` + the configured lag for the listing
  exchange (`availability_lag_days`) and is flagged as estimated.
- Splits come from daily bars (exact dates) plus monthly bars over the full
  history (dated to the month), so share-count jumps in long EDGAR histories
  are explained.
- Yahoo's daily `close` is split-adjusted, not raw; `adj_close` is split- and
  dividend-adjusted.

### 1.4 Reading fundamentals: the panel

`Store.panel(period_type, as_of=…, currency=…, fx=…)` returns one row per
security and period with one column per canonical field. In order:

1. **Point-in-time filter:** drop rows with `available_date > as_of`.
2. **Currency conversion** (optional): currency fields are converted to the
   target currency at the period-end FX rate; a missing rate makes the value
   null. Share counts are not converted.
3. **Period alignment:** period ends within 7 days of each other across
   sources (52/53-week fiscal years) are one period, dated by the
   highest-priority source.
4. **Source resolution:** per field and period, the highest-priority non-null
   value (`edgar > yahooquery > yfinance`).
5. **Pivot** to wide; the row's `available_date` is the latest of its fields.

Because resolution is per field, one period can combine fields from more than
one source (D-012). The balance-identity and source-conflict checks are what
surface a bad combination.

### 1.5 TTM

From four consecutive quarters (the first and last quarter ends 250–290 days
apart): currency flows are summed, share-count averages are averaged,
balance-sheet items come from the latest quarter. Any missing quarter makes
the flow null.

### 1.6 Fiscal labels

`fiscal_year` is the calendar year in which the fiscal year ends; the year-end
month/day comes from the security's latest FY row (December if none).
Quarters are numbered by their distance from the next fiscal year-end.

### 1.7 Validation

Every check in schema §6 runs on each ingest against *everything stored* for
the run's securities, not only the rows fetched in this run. Results go to
`validation_issues` with severity `drop`, `warn` or `info`; securities with
any `drop` are excluded from the run's universe but stay in the snapshot so
the exclusion is reproducible. Currency mismatches are `info` when an FX rate
exists and `drop` when it does not. The outlier check runs once factors exist
(v1.1).

### 1.8 Runs and snapshots

`run_log` records the run ID (UTC timestamp), the hash of `config.yaml`, and
the git commit (suffixed `-dirty` with uncommitted changes).
`snapshots/{run_id}/` holds Parquet copies of the run-scoped tables and of
every security-scoped table for the run's securities; `load_snapshot()` reads
them back. The raw-response cache under `cache/raw/` only saves API calls.

---

## 2. Universe and scoring (v1.1)

`python -m screen.pipeline` (or `data.pipeline --score`) scores a data-stage
run in place: outputs carry its `run_id` and are added to its snapshot.

### 2.1 Universe (`screen/universe.py`)

Knockouts, in order; the first failing one is stored in `universe_membership`:
validation `drop` → excluded sector (D-010) → no market data → stale quote →
no FX rate → price → market cap → average daily traded value. Market cap and
traded value are converted to `universe.currency` at the latest rate (D-015).
A missing vendor share count falls back to the latest reported balance sheet.

### 2.2 Factors (`screen/factors.py`)

Fundamentals are converted to the trading currency at each period-end rate,
point-in-time as of the run date.

- **Base period:** the latest TTM row when it is newer than the latest FY and
  revenue, operating income, operating cash flow and capex are all present;
  otherwise the latest FY. Flows and closing balances come from that one row.
  Opening balances (average invested capital, dilution) come from the FY or
  quarter ending within 20 days of one year earlier.
- **Formulas:** schema §4. Growth and margin stability use fiscal years only.
- **Exceptions:** missing debt / minority interest = 0 when a balance sheet
  exists (D-013); ratios with a denominator ≤ 0 are null (D-014).
- **Deliberate infinities:** interest coverage when interest is below
  `factors.immaterial_interest_pct_revenue` of revenue or there is no debt;
  cash runway when FCF ≥ 0. They rank at the top.
- **Insider buying:** open-market buys minus sales by officers and directors
  over `factors.insider_window_days`, as a share of market cap; null when the
  vendor has no insider records for the company.

| Category | Factors (direction) |
|---|---|
| Quality | ROIC ↑, operating margin ↑, margin stability ↓ (std. dev.), FCF conversion ↑, 3-yr revenue CAGR ↑ |
| Value | EV/EBIT ↓, EV/EBITDA ↓, EV/sales ↓, FCF yield ↑ |
| Expectations | 90-day EPS revision (0y, +1y) ↑, insider net buying ↑ |
| Price | 52-week drawdown ↓ (deeper = more pessimism priced in) |
| Risk | Altman Z'' ↑, interest coverage ↑, net debt/EBITDA ↓, cash runway ↑, 1-yr dilution ↓ |
| Not scored | 12-1 momentum (timing only, D-003) |

### 2.3 Scores (`screen/score.py`)

- Each factor is ranked within the industry when at least
  `peers.min_peer_count` securities have a value, else the sector, else the
  universe (D-016). Percentile = (rank − 1)/(n − 1), 1.0 = best, ties
  averaged; nulls are not ranked and stay null.
- Category score = mean of its factor percentiles, provided at least
  `scoring.min_factor_share` of the category's factors are present.
- Composite = weighted mean of category scores (`scoring.category_weights`),
  renormalized over the categories present; null with fewer than
  `scoring.min_categories`.
- `factor_scores` stores every factor (raw value, peer group, peer count,
  percentile), every `category:<name>` score and the `composite`, so any
  ranking can be traced to its inputs. The outlier check (schema §6) runs on
  the factor values and logs to `validation_issues`.

## 3. Diagnosis (v1.4)

*To be written with `screen/diagnose.py`.*

## 4. Valuation (v1.2, v2.0)

*To be written with `value/`.*
