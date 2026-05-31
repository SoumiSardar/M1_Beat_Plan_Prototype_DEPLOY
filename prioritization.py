"""
prioritization.py
==================
Outlet prioritization for the M1 universe.

The user selects ONE objective for the month. Only that objective's four
binary (0/1) criteria run, producing a score 0-4 per outlet.
Higher score = higher priority. Scores are equal-level tiers (not a ranking).

Period logic: latest complete quarter (Q1 2026) vs same quarter prior year
(Q1 2025), YoY. "Area average" / SD = across the whole M1 universe (the file).

Objectives
----------
1. Market Share Increase
2. Volume Growth
3. Increase P+SP Share of ABI
(Numeric Distribution is intentionally parked for this prototype.)
"""
import numpy as np
import pandas as pd

Q_CUR = ["2026_01", "2026_02", "2026_03"]
Q_PRV = ["2025_01", "2025_02", "2025_03"]

OBJECTIVES = {
    "market_share": "Market Share Increase",
    "volume_growth": "Volume Growth",
    "psp_share": "Increase P+SP Share of ABI",
}


def _q(df, prefix, months):
    """Sum a metric across the 3 months of a quarter -> Series."""
    cols = [f"{prefix}_{m}" for m in months]
    return df[cols].sum(axis=1)


def _growth(cur, prv):
    """YoY growth ratio, safe against zero prior."""
    out = np.where(prv > 0, (cur - prv) / prv, np.where(cur > 0, 1.0, 0.0))
    return pd.Series(out, index=cur.index)


def _share(part, whole):
    out = np.where(whole > 0, part / whole, 0.0)
    return pd.Series(out, index=part.index)


def compute_quarter_frame(df):
    """Build the per-outlet derived quarterly metrics used by all objectives."""
    f = pd.DataFrame(index=df.index)
    f["outlet_code"] = df["outlet_code"].values

    # ---- totals
    f["tot_cur"] = _q(df, "total_vol", Q_CUR)
    f["tot_prv"] = _q(df, "total_vol", Q_PRV)
    f["psp_cur"] = _q(df, "psp_vol", Q_CUR)
    f["psp_prv"] = _q(df, "psp_vol", Q_PRV)
    f["abi_cur"] = _q(df, "abi_vol", Q_CUR)
    f["abi_prv"] = _q(df, "abi_vol", Q_PRV)
    f["abipsp_cur"] = _q(df, "abi_psp_vol", Q_CUR)
    f["abipsp_prv"] = _q(df, "abi_psp_vol", Q_PRV)

    # ---- growth
    f["tot_growth"] = _growth(f["tot_cur"], f["tot_prv"])
    f["abi_growth"] = _growth(f["abi_cur"], f["abi_prv"])
    f["psp_growth"] = _growth(f["psp_cur"], f["psp_prv"])

    # ---- shares
    f["abi_share_cur"] = _share(f["abi_cur"], f["tot_cur"])
    f["abi_share_prv"] = _share(f["abi_prv"], f["tot_prv"])
    f["abi_share_drop"] = (f["abi_share_cur"] < f["abi_share_prv"]).astype(int)

    f["abipsp_share_cur"] = _share(f["abipsp_cur"], f["abi_cur"])
    f["abipsp_share_prv"] = _share(f["abipsp_prv"], f["abi_prv"])
    f["abipsp_share_drop"] = (
        f["abipsp_share_cur"] < f["abipsp_share_prv"]
    ).astype(int)

    f["psp_share_cur"] = _share(f["psp_cur"], f["tot_cur"])
    f["psp_share_prv"] = _share(f["psp_prv"], f["tot_prv"])
    f["psp_share_growth"] = f["psp_share_cur"] - f["psp_share_prv"]
    return f


def _within_1sd(series):
    m, s = series.mean(), series.std(ddof=0)
    if s == 0:
        return (series == series).astype(int)
    return ((series >= m - s) & (series <= m + s)).astype(int)


def score_objective(df, objective):
    """Return a DataFrame with the 4 binary criteria + total score 0-4."""
    f = compute_quarter_frame(df)
    out = pd.DataFrame(index=df.index)
    out["outlet_code"] = df["outlet_code"].values

    if objective == "market_share":
        out["c1_outlet_vol"] = (f["tot_cur"] >= f["tot_cur"].mean()).astype(int)
        out["c2_outlet_vol_growth"] = (
            f["tot_growth"] >= f["tot_growth"].mean()
        ).astype(int)
        out["c3_abi_share_within_1sd"] = _within_1sd(f["abi_share_cur"])
        out["c4_abi_share_dropping"] = f["abi_share_drop"]
        labels = ["POC Volume \u2265 area avg",
                  "POC Volume Growth \u2265 area avg",
                  "ABI Share within \u00b11 SD of area avg",
                  "ABI Share dropping (YoY)"]

    elif objective == "volume_growth":
        out["c1_total_vol"] = (f["tot_cur"] >= f["tot_cur"].mean()).astype(int)
        out["c2_outlet_vol_growth"] = (
            f["tot_growth"] >= f["tot_growth"].mean()
        ).astype(int)
        out["c3_abi_vol"] = (f["abi_cur"] >= f["abi_cur"].mean()).astype(int)
        out["c4_abi_vol_growth"] = (
            f["abi_growth"] >= f["abi_growth"].mean()
        ).astype(int)
        labels = ["Total POC Volume \u2265 area avg",
                  "POC Volume Growth \u2265 area avg",
                  "ABI Volume Sold \u2265 area avg",
                  "ABI Volume Growth \u2265 area avg"]

    elif objective == "psp_share":
        out["c1_psp_vol"] = (f["psp_cur"] >= f["psp_cur"].mean()).astype(int)
        out["c2_psp_share_growth"] = (
            f["psp_share_growth"] >= f["psp_share_growth"].mean()
        ).astype(int)
        out["c3_abipsp_share_within_1sd"] = _within_1sd(f["abipsp_share_cur"])
        out["c4_abipsp_share_dropping"] = f["abipsp_share_drop"]
        labels = ["POC P+SP Volume \u2265 area avg",
                  "P+SP Share Growth \u2265 area avg",
                  "ABI P+SP Share within \u00b11 SD of area avg",
                  "ABI P+SP Share dropping (YoY)"]
    else:
        raise ValueError(f"Unknown objective: {objective}")

    crit_cols = [c for c in out.columns if c.startswith("c")]
    out["priority_score"] = out[crit_cols].sum(axis=1)
    out["criteria_labels"] = [labels] * len(out)

    # --- per-outlet summary fields (shown in the Outlet-level Summary tab) ---
    out["abi_vol"] = f["abi_cur"].round(2).values
    out["abi_share"] = (f["abi_share_cur"] * 100).round(1).values   # %
    out["psp_vol"] = f["psp_cur"].round(2).values
    out["psp_share"] = (f["psp_share_cur"] * 100).round(1).values   # %
    # "ABI share dropping" - YoY decline of ABI share at outlet
    out["abi_share_dropping"] = f["abi_share_drop"].astype(int).values

    # human-readable context column
    ctx = []
    for _, r in out.iterrows():
        hit = [labels[i] for i, c in enumerate(crit_cols) if r[c] == 1]
        ctx.append("; ".join(hit) if hit else "No criteria met")
    out["outlet_context"] = ctx

    def action(s):
        if s >= 3:
            return "High priority \u2014 visit early, push discount/volume deal"
        if s == 2:
            return "Medium \u2014 standard visit, monitor share"
        return "Low \u2014 maintenance visit / fill spare capacity"
    out["suggested_action"] = out["priority_score"].map(action)
    return out


def area_averages(df):
    """Universe area averages (the same values the scoring compares against).
    Volumes are in KHL. Display-only; does not affect any scoring logic."""
    f = compute_quarter_frame(df)
    return {
        "avg_total_vol": round(float(f["tot_cur"].mean()), 2),
        "avg_abi_vol": round(float(f["abi_cur"].mean()), 2),
        "avg_abi_share": round(float(f["abi_share_cur"].mean()) * 100, 1),
        "avg_psp_vol": round(float(f["psp_cur"].mean()), 2),
        "avg_abipsp_vol": round(float(f["abipsp_cur"].mean()), 2),
        "avg_abipsp_share": round(float(f["abipsp_share_cur"].mean()) * 100, 1),
        "n_outlets": int(len(f)),
    }
