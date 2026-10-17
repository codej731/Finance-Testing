"""Data-quality checks run on every ingest (schema §6).

Each check returns issue rows; `validate` runs them all and reports which
securities must be dropped from the run's universe. Nothing is silently fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from data.schema import SOURCES, TABLES, availability_region
from data.transform import FxProvider, align_period_ends, fundamentals_panel

ISSUE_COLUMNS = [c for c in TABLES["validation_issues"].column_names if c != "run_id"]
DROP, WARN, INFO = "drop", "warn", "info"


def _issue(security_id: str, check: str, severity: str, detail: str, *,
           field: str | None = None, period_type: str | None = None,
           period_end: date | None = None) -> dict:
    return {"security_id": security_id, "check_name": check, "severity": severity,
            "field": field, "period_type": period_type, "period_end": period_end,
            "detail": detail}


def _frame(issues: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(issues, columns=ISSUE_COLUMNS)


@dataclass
class ValidationReport:
    issues: pd.DataFrame = field(default_factory=lambda: _frame([]))

    @property
    def dropped(self) -> set[str]:
        return set(self.issues.loc[self.issues["severity"] == DROP, "security_id"])

    def for_security(self, security_id: str) -> pd.DataFrame:
        return self.issues[self.issues["security_id"] == security_id]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_currency(securities: pd.DataFrame, long: pd.DataFrame,
                   fx: FxProvider | None) -> pd.DataFrame:
    """Fundamentals in a currency other than the quote must be convertible."""
    issues = []
    trading = securities.set_index("security_id")["trading_currency"].to_dict()
    for (sid, ccy), grp in long.dropna(subset=["currency"]).groupby(["security_id", "currency"]):
        quote_ccy = trading.get(sid)
        if quote_ccy is None or ccy == quote_ccy:
            continue
        latest = pd.Timestamp(grp["period_end"].max()).date()
        rate = fx.rate(ccy, quote_ccy, latest) if fx is not None else None
        if rate is None:
            issues.append(_issue(sid, "currency_mismatch", DROP,
                                 f"reports in {ccy}, trades in {quote_ccy}; no FX rate",
                                 period_end=latest))
        else:
            issues.append(_issue(sid, "currency_mismatch", INFO,
                                 f"reports in {ccy}, trades in {quote_ccy}; converted at "
                                 f"period-end FX ({ccy}{quote_ccy} {rate:.4f} on {latest})",
                                 period_end=latest))
    return _frame(issues)


def check_staleness(fy: pd.DataFrame, security_ids: list[str], as_of: date,
                    months: int | dict[str, int],
                    regions: dict[str, str] | None = None) -> pd.DataFrame:
    """Latest FY older than `months`. `months` may be a {region: months} map, since
    filing deadlines differ (TSX-V annual statements are due 120 days after year-end)."""
    latest = fy.groupby("security_id")["period_end"].max()
    issues = []
    for sid in security_ids:
        limit = (months.get((regions or {}).get(sid, "US"), max(months.values()))
                 if isinstance(months, dict) else months)
        cutoff = (pd.Timestamp(as_of) - pd.DateOffset(months=limit)).date()
        if sid not in latest.index:
            issues.append(_issue(sid, "staleness", DROP, "no FY fundamentals"))
        elif pd.Timestamp(latest[sid]).date() < cutoff:
            issues.append(_issue(sid, "staleness", DROP,
                                 f"latest FY {latest[sid]} older than {limit} months",
                                 period_type="FY", period_end=latest[sid]))
    return _frame(issues)


def check_balance_identity(panel: pd.DataFrame, tolerance: float) -> pd.DataFrame:
    """assets − liabilities − equity − minority interest ≈ 0."""
    issues = []
    p = panel.dropna(subset=["total_assets", "total_liabilities", "total_equity"])
    p = p[p["total_assets"] > 0]
    gap = (p["total_assets"] - p["total_liabilities"] - p["total_equity"]
           - p["minority_interest"].fillna(0)).abs() / p["total_assets"]
    for idx in gap[gap > tolerance].index:
        r = p.loc[idx]
        issues.append(_issue(r["security_id"], "balance_identity", WARN,
                             f"identity off by {gap[idx]:.1%} of total assets",
                             period_type=r["period_type"], period_end=r["period_end"]))
    return _frame(issues)


SIGN_RULES = (
    ("revenue", lambda v: v < 0, "revenue < 0"),
    ("total_assets", lambda v: v <= 0, "total_assets <= 0"),
    ("shares_outstanding", lambda v: v <= 0, "shares_outstanding <= 0"),
)


def check_sign_sanity(panel: pd.DataFrame) -> pd.DataFrame:
    """Drop if the latest period of a type fails; warn for older periods."""
    issues = []
    for (sid, ptype), grp in panel.groupby(["security_id", "period_type"]):
        latest_end = grp["period_end"].max()
        for _, r in grp.iterrows():
            for fname, bad, label in SIGN_RULES:
                v = r[fname]
                if pd.notna(v) and bad(v):
                    severity = DROP if r["period_end"] == latest_end else WARN
                    issues.append(_issue(sid, "sign_sanity", severity, label, field=fname,
                                         period_type=ptype, period_end=r["period_end"]))
    return _frame(issues)


def check_share_jumps(panel: pd.DataFrame, threshold: float,
                      corporate_actions: pd.DataFrame | None = None) -> pd.DataFrame:
    """Share count moving more than `threshold` between consecutive periods with
    no recorded corporate action in between. An action up to the period's
    availability date counts: cover-page share counts are dated at filing."""
    issues = []
    actions = corporate_actions if corporate_actions is not None else pd.DataFrame(
        columns=["security_id", "date"])
    for (sid, ptype), grp in panel.dropna(subset=["shares_outstanding"]).groupby(
            ["security_id", "period_type"]):
        grp = grp.sort_values("period_end")
        dates = list(pd.to_datetime(grp["period_end"]))
        known = list(pd.to_datetime(grp["available_date"]).fillna(pd.Series(dates, index=grp.index)))
        shares = list(grp["shares_outstanding"])
        sid_actions = pd.to_datetime(actions.loc[actions["security_id"] == sid, "date"])
        for i in range(1, len(grp)):
            prev, cur = shares[i - 1], shares[i]
            if prev <= 0:
                continue
            change = cur / prev - 1
            if abs(change) <= threshold:
                continue
            window_end = max(dates[i], known[i])
            explained = ((sid_actions > dates[i - 1]) & (sid_actions <= window_end)).any()
            if not explained:
                issues.append(_issue(sid, "share_count_jump", WARN,
                                     f"shares changed {change:+.0%} with no corporate action",
                                     field="shares_outstanding", period_type=ptype,
                                     period_end=dates[i].date()))
    return _frame(issues)


def check_source_conflicts(long: pd.DataFrame, tolerance: float,
                           priority: tuple[str, ...] = SOURCES) -> pd.DataFrame:
    """Same field/period differing by more than `tolerance` across sources.
    The higher-priority source is kept by transform.resolve_sources."""
    issues = []
    work = align_period_ends(long.dropna(subset=["value"]), priority)
    rank = {s: i for i, s in enumerate(priority)}
    key = ["security_id", "field", "period_type", "period_end"]
    multi = work[work.duplicated(key, keep=False)]
    for (sid, fname, ptype, pend), grp in multi.groupby(key):
        grp = grp.assign(_rank=grp["source"].map(rank)).sort_values("_rank")
        kept = grp.iloc[0]
        for _, other in grp.iloc[1:].iterrows():
            scale = max(abs(kept["value"]), abs(other["value"]))
            if scale == 0:
                continue
            diff = abs(kept["value"] - other["value"]) / scale
            if diff > tolerance:
                issues.append(_issue(
                    sid, "source_conflict", WARN,
                    f"{kept['source']}={kept['value']:.6g} vs {other['source']}="
                    f"{other['value']:.6g} ({diff:.1%}); kept {kept['source']}",
                    field=fname, period_type=ptype, period_end=pend))
    return _frame(issues)


def check_completeness(fy: pd.DataFrame, security_ids: list[str], required: list[str],
                       min_present: int) -> pd.DataFrame:
    """Latest FY must have at least `min_present` of the required fields."""
    issues = []
    latest = fy.sort_values("period_end").groupby("security_id").tail(1).set_index("security_id")
    for sid in security_ids:
        if sid not in latest.index:
            continue  # reported by check_staleness
        row = latest.loc[sid]
        missing = [f for f in required if pd.isna(row.get(f))]
        if len(required) - len(missing) < min_present:
            issues.append(_issue(sid, "completeness", DROP,
                                 f"{len(required) - len(missing)}/{len(required)} required "
                                 f"fields; missing {', '.join(missing)}",
                                 period_type="FY", period_end=row["period_end"]))
    return _frame(issues)


def check_outliers(metrics: pd.DataFrame, bounds: dict[str, list[float]]) -> pd.DataFrame:
    """Ratios outside configured bounds. `metrics` has security_id plus one column
    per ratio (built by screen/factors.py)."""
    issues = []
    for ratio, (lo, hi) in bounds.items():
        if ratio not in metrics.columns:
            continue
        for _, r in metrics[(metrics[ratio] < lo) | (metrics[ratio] > hi)].iterrows():
            issues.append(_issue(r["security_id"], "outlier", WARN,
                                 f"{ratio}={r[ratio]:.4g} outside [{lo}, {hi}]",
                                 field=ratio, period_type=r.get("period_type"),
                                 period_end=r.get("period_end")))
    return _frame(issues)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate(securities: pd.DataFrame, long: pd.DataFrame, as_of: date, cfg,
             fx: FxProvider | None = None,
             corporate_actions: pd.DataFrame | None = None) -> ValidationReport:
    """Run every ingest-time check. `cfg` is a data.config.Config."""
    priority = tuple(cfg.get("sources.priority", SOURCES))
    sids = list(securities["security_id"])
    long = long[long["security_id"].isin(sids)]
    fy = fundamentals_panel(long, "FY", as_of=as_of, priority=priority)
    q = fundamentals_panel(long, "Q", as_of=as_of, priority=priority)
    both = pd.concat([fy, q], ignore_index=True)
    checks = [
        check_currency(securities, long, fx),
        check_staleness(fy, sids, as_of, cfg.get("validation.staleness_months", 15),
                        dict(zip(securities["security_id"],
                                 securities["exchange"].map(availability_region)))),
        check_balance_identity(both, cfg.get("validation.balance_identity_tolerance", 0.02)),
        check_sign_sanity(both),
        check_share_jumps(both, cfg.get("validation.share_jump_threshold", 0.5),
                          corporate_actions),
        check_source_conflicts(long, cfg.get("sources.conflict_tolerance", 0.05), priority),
        check_completeness(fy, sids, cfg.get("validation.required_fields", []),
                           cfg.get("validation.min_required_fields", 0)),
    ]
    issues = pd.concat([c for c in checks if not c.empty], ignore_index=True) \
        if any(not c.empty for c in checks) else _frame([])
    return ValidationReport(issues)
