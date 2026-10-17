# Equity Research Engine *(working title)*

A personal research tool that screens North American equities for **quality businesses whose prices imply overly pessimistic expectations**, then values the shortlist with multiple methods and tracks entry timing.

> **Status:** v0 → v1 rebuild. The original notebook pipeline (yfinance/yahooquery sequential filters) is being restructured into a data layer, a scoring engine, and a valuation engine.

> **Not investment advice.** This is a personal research and learning project. Outputs are inputs to my own analysis, not recommendations.

---

## Thesis

Fundamentals tell you what a business is worth; price tells you what the market charges for it. An opportunity is the **gap between the two plus a reason for it to close**. The engine is built to answer, in order:

1. Is this a good business? *(quality)*
2. Is it cheap relative to its peers and its own history? *(value)*
3. **Why** is it cheap, and is the cause temporary or structural? *(diagnosis)*
4. What does the current price imply, and what is a reasonable value range? *(valuation)*
5. Has the selling stopped? *(timing — used for entry only, never to decide what is cheap)*

---

## Pipeline

```
 ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐
 │   DATA     │──▶│  UNIVERSE  │──▶│   SCORE    │──▶│  DIAGNOSE  │──▶│   VALUE    │──▶│  MONITOR   │
 │ ingest +   │   │ knockouts  │   │ industry-  │   │ why is it  │   │ relative,  │   │ watchlist  │
 │ normalize  │   │ only       │   │ relative   │   │ down?      │   │ reverse    │   │ states +   │
 │ + snapshot │   │            │   │ percentiles│   │            │   │ DCF, DCF   │   │ timing     │
 └────────────┘   └────────────┘   └────────────┘   └────────────┘   └────────────┘   └────────────┘
```

| Stage | Output | Notes |
|---|---|---|
| **Data** | Canonical fundamentals, prices, estimates, insider trades — saved as dated snapshots | See [`docs/schema.md`](docs/schema.md) |
| **Universe** | Investable list | Hard filters only: liquidity, size, data completeness, analyzable sector |
| **Score** | Factor percentiles + composite | Quality, value, expectations, price, risk — ranked **within industry** |
| **Diagnose** | Drawdown attribution + label | Market / sector / idiosyncratic decomposition × fundamental deterioration flags |
| **Value** | Value range + implied expectations | Relative (peers + own history), reverse DCF, scenario DCF |
| **Monitor** | Watchlist state | Candidate → Waiting → Stabilizing → Entry signal |

---

## Repository structure

```
data/
  ingest.py         # yfinance / yahooquery / SEC EDGAR XBRL fetchers + normalizers
  schema.py         # canonical field map + source mappings + table definitions
  transform.py      # point-in-time filter, source priority, FX, wide panel, TTM
  validate.py       # data-quality checks (nulls, currency, staleness, identities)
  store.py          # DuckDB + Parquet snapshot storage
  config.py         # loads config.yaml, hashes it for run_log
  pipeline.py       # one data-stage run: ingest → validate → store → snapshot
screen/
  universe.py       # knockout filters
  factors.py        # factor calculations
  score.py          # industry-relative percentile ranking + composite
  diagnose.py       # "why is it down" classifier
value/
  relative.py       # peer + own-history multiples
  reverse_dcf.py    # implied growth at current price
  dcf.py            # bear / base / bull scenarios
  sector/           # sector-specific models (banks, miners) — later
monitor/
  watchlist.py      # state machine + timing signals
reports/
  tearsheet.py      # one-page summary per ticker
notebooks/          # exploration only — no pipeline logic lives here
tests/
config.yaml         # every threshold and weight lives here
docs/
  schema.md
  methodology.md
legacy/             # v0 notebooks, scripts and CSV outputs, kept for reference
```

---

## Methodology summary

### Scoring
- Every factor is converted to a **percentile rank within industry** (not broad sector), using the full universe as the peer set.
- The composite is a weighted blend set in `config.yaml`. Component scores are always stored so a ranking can be explained.
- Only the universe stage uses hard cut-offs.

### Diagnosis
- Trailing returns are regressed on a broad market index and a sector ETF to split a drawdown into **market**, **sector**, and **idiosyncratic** components.
- Fundamental deterioration flags: decelerating revenue, compressing margins, falling ROIC, negative estimate revisions.
- Combined label: *macro selloff* · *sector-wide* · *temporary / company-specific* · *likely value trap*. The label is a first-pass flag, verified manually against filings.

### Valuation
- **Relative:** current multiples vs. industry median and the company's own ~5-year median, with implied prices.
- **Reverse DCF:** solves for the growth rate the current enterprise value implies; compared against historical growth.
- **Scenario DCF:** explicit bear / base / bull assumptions; output is a range, not a point estimate.

---

## Design decisions log

Each entry records a methodology choice, why it was made, and what it costs. New entries go at the bottom.

### D-001 · Score instead of chaining hard filters — 2026-10-16
**Context:** v0 applied sequential AND-filters and inner-joined intermediate lists. Results depended on cell order, and stocks just below any threshold were discarded the same as clear failures.
**Decision:** Hard filters only for investability (liquidity, size, data completeness). Everything else becomes an industry-relative percentile feeding a composite score.
**Consequences:** Rankings are smoother and explainable; requires choosing weights, which are kept in config and revisited.

### D-002 · Industry-level peers from the full universe, median not mean — 2026-10-16
**Context:** v0 compared each stock with the *mean* multiple of other *Fortress-tier survivors* in the same *broad sector*. "Basic Materials" mixed royalty companies, gold miners, and fertilizer producers, so large "discounts" mostly reflected business-model differences. Survivor-only peer sets were also biased.
**Decision:** Peers = same industry, drawn from the full investable universe; use medians; require a minimum peer count, otherwise fall back to own-history comparison.
**Consequences:** Fewer spectacular "discounts", more meaningful ones.

### D-003 · Trend is a timing state, not an exclusion — 2026-10-16
**Context:** v0 required price > 200-day SMA, which removed exactly the beaten-down names the thesis targets.
**Decision:** Price/trend signals move a stock between watchlist states; they never remove it from the candidate set.
**Consequences:** The watchlist holds names that are still falling; entry discipline comes from the monitor stage.

### D-004 · Replace Altman Z with Z'' and, later, a structural PD model — 2026-10-16
**Context:** The original Z-score is calibrated for manufacturers, and its market-cap / liabilities term explodes for low-liability businesses (royalty companies scored >100).
**Decision:** Use Z'' (non-manufacturer version, book-equity based) as an interim distress flag; replace with a Merton-style probability-of-default model.
**Consequences:** Risk scores become comparable across business models.

### D-005 · ROIC on NOPAT and operating invested capital — 2026-10-16
**Context:** v0 used pre-tax EBIT / (total assets − current liabilities), which includes excess cash and flatters cash-rich firms. EBIT also fell back to pre-tax income.
**Decision:** ROIC = NOPAT / average invested capital (debt + equity + minority interest − cash). EBIT never falls back to pre-tax income.
**Consequences:** Requires a tax-rate rule (see schema); ROIC values drop for cash-heavy companies.

### D-006 · Missing data is null, never zero — 2026-10-16
**Context:** v0 defaulted missing fields to 0 (or 100 for interest coverage), so failures silently passed or failed filters.
**Decision:** Missing → null. Factors with null inputs are null; scoring handles nulls explicitly (excluded from that factor's ranking).
**Consequences:** More visible data gaps, which become a data-completeness knockout.

### D-007 · Store dated snapshots from day one — 2026-10-16
**Context:** yfinance only returns current fundamentals, so v0 results cannot be backtested without look-ahead and survivorship bias.
**Decision:** Every run writes a dated snapshot of inputs and outputs, and fundamentals carry an availability date.
**Consequences:** A point-in-time dataset accumulates for future validation.

### D-008 · Estimate revisions over consensus ratings — 2026-10-16
**Context:** The analyst "Buy" filter passed ~75% of quality names, and battered stocks tend to have recent downgrades, so the filter worked against the thesis.
**Decision:** Use the direction of EPS estimate revisions (current vs. 30/90 days ago) as the expectations factor. Ratings are displayed, not filtered on.

### D-009 · LLMs summarize sources; they do not produce numbers or verdicts — 2026-10-16
**Context:** The v0 Gemini sentiment report produced unverifiable figures and described working from a "simulated" date.
**Decision:** LLM use is limited to summarizing documents passed in (MD&A, risk factors, transcripts), with citations back to the source text.

### D-010 · Financials and REITs excluded until sector models exist — 2026-10-16
**Context:** EV-based multiples, ROIC, and current ratio are not meaningful for banks, insurers, or REITs.
**Decision:** Exclude at the universe stage; add P/B-vs-ROE (banks) and P/FFO (REITs) models later. Miners get a NAV-based model on the same roadmap.

### D-011 · EDGAR values are taken as originally filed — 2026-10-16
**Context:** Each filing repeats earlier periods as comparatives, sometimes restated. Using the latest value would put information into the past that was not known then.
**Decision:** For each period keep the value from the earliest filing, with that filing date as `available_date`. Discrete quarters missing from filings (Q4, 10-Q cash flows) are derived from year-to-date differences.
**Consequences:** Point-in-time correct; restatements are not reflected until a dedicated restatement field is added.

### D-012 · Source priority is resolved per field, at read time — 2026-10-16
**Context:** EDGAR coverage of individual tags is uneven, and Yahoo often has the field EDGAR lacks.
**Decision:** All sources are stored; when reading, each field of each period takes the highest-priority non-null value.
**Consequences:** Nothing is lost and conflicts are auditable, but a single period can mix sources; the balance-identity and source-conflict checks flag bad combinations.

### D-013 · Missing debt, minority interest and cash count as zero when a balance sheet exists — 2026-10-17
**Context:** Debt-free companies (Franco-Nevada, most explorers) have no debt line, and some (Tourmaline) have no cash line, so under D-006 their enterprise value, ROIC and leverage were null.
**Decision:** Exception to D-006: when `total_assets` is reported, a missing `total_debt`, `minority_interest` or `cash` is 0. Without a balance sheet they stay null. Configurable (`factors.missing_balance_is_zero`).
**Consequences:** EV-based factors exist for debt-free companies; a vendor that simply omits a real debt line would understate EV, which the source-conflict check can surface for US names.

### D-014 · Ratios with a zero or negative denominator are null — 2026-10-17
**Context:** Pre-revenue explorers produced −∞ operating margins and meaningless EV/EBIT.
**Decision:** Any ratio whose denominator is ≤ 0 is null. The only infinities are deliberate best-case values: interest coverage when interest is immaterial or there is no debt, and cash runway when free cash flow is positive. A category score needs at least half its factors (`scoring.min_factor_share`), so explorers are scored on expectations, price and risk (runway, dilution, Z'') rather than on a lone FCF yield.
**Consequences:** Explorers and producers share one composite built from different evidence; compare them with that in mind.

### D-015 · Universe thresholds in USD and on traded value — 2026-10-17
**Context:** v0 thresholds were per listing currency and on share volume; 50k shares/day means very different things for a $0.40 explorer and a $400 stock.
**Decision:** Market cap and liquidity are converted to `universe.currency` (USD) at the latest FX rate; liquidity is 3-month average volume × price. Quotes older than `universe.max_quote_age_days` are knocked out (halted or delisted names).
**Consequences:** Thresholds are comparable across TSX, TSX-V and US listings.

### D-016 · Peer fallback goes to the sector, then the universe — 2026-10-17
**Context:** D-002 calls for own-history comparison when an industry has too few peers, which needs a history of factor values per company that does not exist yet.
**Decision:** Interim: with fewer than `peers.min_peer_count` non-null values in the industry, a factor is ranked against the whole sector, then the universe. The peer group used is stored with each score. Own-history comparison arrives with relative valuation (`value/relative.py`).
**Consequences:** Thin industries (e.g. Silver, Uranium) are ranked against broader peers until the universe grows.

### D-017 · Cash runway measures operating burn; royalty stream purchases are acquisitions — 2026-10-17
**Context:** Wheaton spent $4.5B on new streams in one quarter, recorded as capex. Trailing FCF went to −$2.9B and cash runway to 0.03 years, as if the company were about to run out of cash.
**Decision:** Cash runway is infinite when operating cash flow is positive; otherwise cash ÷ (capex − operating cash flow). Royalty and streaming companies listed in `factors.royalty_companies` (tickers or security IDs; Yahoo labels them by metal) use free cash flow = operating cash flow, because their capex is buying new streams.
**Consequences:** Runway flags only companies whose operations burn cash (explorers, developers). The royalty list is maintained by hand.

### D-018 · Peers share industry and segment — 2026-10-17
**Context:** Royalty companies (~22–24× EBITDA) were ranked against gold miners (5–9×) in the same "Gold" industry, and pre-revenue explorers against producers, so the ranking mostly reflected business model rather than cheapness.
**Decision:** Each company gets a segment: `royalty` (config list), `pre_revenue` (revenue below `peers.pre_revenue_max` in USD) or `producing`. A factor is ranked within industry + segment, else sector + segment, else the whole segment across industries (`peers.min_segment_peers`), else the universe. Producers and pre-revenue companies are listed per sector (Technology, Consumer Defensive, Industrials, …), royalty companies as one group. This extends D-002/D-016.
**Consequences:** Fairer comparisons; small segments have coarse percentiles until the universe is the full listing.

### D-019 · Freshness is enforced, not assumed — 2026-10-17
**Context:** Peers are compared from data stored in the database, so scores are only as current as the last data run.
**Decision:** Raw API responses (including EDGAR) are reused for at most 24 hours. Each run prints how many responses were fetched versus taken from the cache, and its quote dates. The screen refuses to score a run older than `screen.max_run_age_days` (3) unless `--allow-stale` is passed. Quotes older than 7 days, and fiscal years older than 15/16 months, already knock a company out.
**Consequences:** Scores cannot silently rest on old prices; scoring old runs on purpose (e.g. for backtests) needs the explicit flag.

### D-020 · Universe from full exchange listings, fetched in two phases — 2026-10-17
**Context:** Peer groups are drawn only from the symbols ingested; a hand-picked list gave groups of 3–5 and coarse percentiles.
**Decision:** `--universe us,tsx,tsxv` takes every listed common stock (NASDAQ Trader for NYSE/NASDAQ/NYSE American; the TMX company directory for TSX and TSX-V), dropping ETFs, preferreds, debentures, warrants, USD duplicate lines, capital pool shells, CDRs and ADRs; Yahoo's `quoteType` must be `EQUITY`. Quotes are fetched for everything; statements, price history and EDGAR only for securities passing the market-data knockouts. Requests are paced: batches of 50, 2 s between batches, 30 s timeouts, up to 3 retries with 15/30/60 s backoff; transient failures are never cached.
**Consequences:** A full run takes tens of minutes and survivors of the market knockouts form the peer universe. Everything is still recorded for the knocked-out names (quote snapshot), preserving survivorship history.

---

## Known limitations
- Yahoo data quality varies, especially for TSX / TSX-V names; EDGAR is preferred for US fundamentals.
- Yahoo does not provide filing dates, so availability dates are estimated with conservative lags.
- Many TSX-listed companies report in USD while trading in CAD; currency handling is enforced in validation.
- No backtest exists yet; any claims about performance wait until enough snapshots accumulate.

## Roadmap
- [ ] **v1.0** Data layer: canonical schema, validation, snapshot storage, config file *(implemented; first live run 2026-10-17 on AAPL, MSFT, RUS.TO, BAM.TO)*
- [ ] **v1.1** Universe knockouts + factor scoring + composite *(implemented; first run 2026-10-17 on 54 TSX / TSX-V / US names)*
- [ ] **v1.2** Reverse DCF
- [ ] **v1.3** One-page tear sheet per ticker
- [ ] **v1.4** Diagnosis module
- [ ] **v2.0** Scenario DCF, watchlist state machine
- [ ] **v2.1** Structural PD model replaces Z''
- [ ] **v2.2** Sector models (banks, REITs, miners)

## Setup

Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                             # offline tests, no network needed
```

Set `sec.user_agent` in `config.yaml` to `"Your Name your@email.com"` (SEC fair-access policy). EDGAR ingestion is skipped while it is empty.

Run the data stage:

```bash
python -m data.pipeline AAPL MSFT RUS.TO CCL-B.TO
python -m data.pipeline --file symbols.txt --notes "weekly refresh"
python -m data.pipeline --file symbols.txt --score   # + universe, factors, scores
python -m data.pipeline --universe us,tsx,tsxv --score             # full listings (tens of minutes)
python -m data.pipeline --universe tsx,tsxv --limit 300 --score    # quick random sample
python -m screen.pipeline --top 25                   # re-score the latest run
```

This writes `store/engine.duckdb`, `snapshots/{run_id}/*.parquet` and `cache/raw/` (all git-ignored). Reading fundamentals:

```python
from datetime import date

from data.config import load_config
from data.store import Store

cfg = load_config()
store = Store(cfg.resolve_path("paths.db"), cfg.resolve_path("paths.snapshots"))
fy  = store.panel("FY",  as_of=date(2026, 10, 16))   # point-in-time, source-resolved
ttm = store.panel("TTM", as_of=date(2026, 10, 16))
issues = store.read("validation_issues", "run_id = ?", [run_id])
```
