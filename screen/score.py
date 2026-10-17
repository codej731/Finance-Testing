"""Industry-relative percentile ranking and weighted composite (v1.1, D-002, D-006).

Each factor is ranked within the security's industry, drawn from the whole
investable universe. With fewer than `peers.min_peer_count` non-null values
in the industry, the sector is used, then the universe; the peer group used is
stored with every score. Nulls are excluded from a factor's ranking and leave
that factor's percentile null (D-006).

Percentile = (rank − 1) / (n − 1), oriented so 1.0 is best; a lone value
scores 0.5. A category score is the mean of its factors' percentiles; the
composite is the weighted mean of category scores, renormalized over the
categories a security has. A category counts only when at least
`scoring.min_factor_share` of its factors are available.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SCORE_COLUMNS = ["security_id", "factor", "raw_value", "peer_group", "peer_count", "percentile"]


def _peer_groups(valid: pd.DataFrame, min_peers: int) -> pd.Series:
    """Peer-group label per security for one factor."""
    industry_n = valid.groupby("industry")["security_id"].transform("count")
    sector_n = valid.groupby("sector")["security_id"].transform("count")
    return pd.Series(np.where(
        industry_n >= min_peers, "industry:" + valid["industry"].fillna("?"),
        np.where(sector_n >= min_peers, "sector:" + valid["sector"].fillna("?"), "universe")),
        index=valid.index)


def factor_percentiles(factors: pd.DataFrame, securities: pd.DataFrame, factor: str,
                       direction: str, min_peers: int) -> pd.DataFrame:
    df = factors[["security_id", factor]].merge(
        securities[["security_id", "industry", "sector"]], on="security_id", how="left")
    valid = df[df[factor].notna()].copy()
    out = df[["security_id"]].assign(factor=factor, raw_value=df[factor], peer_group=None,
                                     peer_count=np.nan, percentile=np.nan)
    if valid.empty:
        return out[SCORE_COLUMNS]
    valid["peer_group"] = _peer_groups(valid, min_peers)
    signed = (valid[factor] if direction == "high" else -valid[factor]).astype(float)
    # Rank each security against everyone in its peer set, including peers that
    # are themselves ranked in a narrower group.
    for label, members in valid.groupby("peer_group"):
        kind, _, key = label.partition(":")
        peers = signed if kind == "universe" else signed[valid[kind].fillna("?") == key]
        ordered = np.sort(peers.to_numpy())
        v = signed[members.index].to_numpy()
        below = np.searchsorted(ordered, v, side="left")
        tied = np.searchsorted(ordered, v, side="right") - below
        rank = below + (tied + 1) / 2                      # average rank, 1-based
        n = len(ordered)
        valid.loc[members.index, "percentile"] = (rank - 1) / (n - 1) if n > 1 else 0.5
        valid.loc[members.index, "peer_count"] = n
    out = out.set_index("security_id")
    out.update(valid.set_index("security_id")[["peer_group", "peer_count", "percentile"]])
    return out.reset_index()[SCORE_COLUMNS]


def score(factors: pd.DataFrame, securities: pd.DataFrame, cfg) -> pd.DataFrame:
    """Long table for factor_scores: one row per factor, one per category
    ("category:<name>") and one "composite" row per security."""
    specs = cfg.get("scoring.factors", {})
    weights = cfg.get("scoring.category_weights", {})
    min_peers = cfg.get("peers.min_peer_count", 5)
    min_categories = cfg.get("scoring.min_categories", 1)
    min_share = cfg.get("scoring.min_factor_share", 0.0)

    parts = [factor_percentiles(factors, securities, name, direction, min_peers)
             for name, (category, direction) in specs.items() if name in factors.columns]
    long = pd.concat(parts, ignore_index=True)
    category_of = {name: category for name, (category, _) in specs.items()}
    long["category"] = long["factor"].map(category_of)

    scored = long[long["category"].isin(weights)]
    grouped = scored.groupby(["security_id", "category"])["percentile"]
    categories = grouped.mean().unstack().reindex(columns=list(weights))
    # A category needs at least `min_share` of its factors to count.
    share = (grouped.count() / scored.groupby("category")["factor"].nunique()).unstack()
    categories = categories.where(share.reindex_like(categories).fillna(0) >= min_share - 1e-9)
    w = pd.Series(weights, dtype=float)
    present = categories.notna()
    composite = ((categories.fillna(0) * w).sum(axis=1) / (present * w).sum(axis=1))
    composite[present.sum(axis=1) < min_categories] = np.nan

    category_rows = (categories.stack().rename("percentile").reset_index()
                     .assign(factor=lambda d: "category:" + d["category"],
                             peer_group="composite"))
    composite_rows = composite.rename("percentile").reset_index().assign(
        factor="composite", peer_group="universe",
        peer_count=int(composite.notna().sum()))
    out = pd.concat([long, category_rows, composite_rows], ignore_index=True)
    return out.reindex(columns=SCORE_COLUMNS)


def ranking(scores: pd.DataFrame, securities: pd.DataFrame) -> pd.DataFrame:
    """Wide view: composite, category scores and identifiers, best first."""
    wide = scores[scores["factor"].str.startswith("category:") | (scores["factor"] == "composite")]
    wide = wide.pivot_table(index="security_id", columns="factor", values="percentile",
                            aggfunc="first")
    wide.columns = [c.replace("category:", "") for c in wide.columns]
    meta = securities.set_index("security_id")[["name", "exchange", "sector", "industry"]]
    out = meta.join(wide, how="inner")
    if "composite" not in out.columns:
        out["composite"] = np.nan
    return out.sort_values("composite", ascending=False, na_position="last")
