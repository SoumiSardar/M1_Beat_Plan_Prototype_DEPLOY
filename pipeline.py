"""
pipeline.py  -- ties prioritization + route optimization into one run.

Calendar: works against May 2026. May 1 is treated as Labour Day (no beat);
Sundays in May are non-working; Saturdays are working. The integer "day"
inside the engine still refers to a working-day index 1..N. Calendar dates
are produced on the output side for display.
"""
import datetime as _dt

import pandas as pd

from prioritization import score_objective, OBJECTIVES
from routeopt import (cluster_outlets, days_needed_per_zone, spread_zone_days,
                      allocate_outlets, balance_and_merge, sequence_day,
                      fill_to_etm_floor)


MAY_YEAR = 2026
LABOUR_DAY = _dt.date(MAY_YEAR, 5, 1)


def build_may_calendar():
    """Return ordered list of (working_day_index, real_date, label) for May.
    Skips Sundays and May 1 (Labour Day)."""
    out = []
    idx = 0
    for dom in range(1, 32):
        d = _dt.date(MAY_YEAR, 5, dom)
        if d.weekday() == 6:                # Sunday
            continue
        if d == LABOUR_DAY:
            continue
        idx += 1
        out.append((idx, d, d.strftime("%a %d %b")))
    return out


def run_pipeline(df, objective, cfg):
    """
    cfg keys:
      base_lat, base_lon, daily_capacity_min, avg_speed_kmh,
      working_days, n_clusters, road_factor
    daily_capacity_min is now interpreted as the per-day ETM FLOOR
    (minimum target) - not an upper cap.
    """
    # ---- A: prioritization (selected objective only) ---------------------
    sc = score_objective(df, objective)
    scores = dict(zip(sc["outlet_code"], sc["priority_score"]))
    abi_lookup = dict(zip(sc["outlet_code"], sc["abi_vol"]))
    has_name = "outlet_name" in df.columns
    name_lookup = (dict(zip(df["outlet_code"], df["outlet_name"]))
                   if has_name else {})

    outlets = []
    for _, r in df.iterrows():
        nm = name_lookup.get(r["outlet_code"], "")
        if pd.isna(nm) or str(nm).strip() == "":
            nm = "\u2014"      # em dash placeholder
        outlets.append({
            "outlet_code": r["outlet_code"],
            "outlet_name": str(nm),
            "channel_type": r["channel_type"],
            "visit_frequency_per_month": int(r["visit_frequency_per_month"]),
            "time_per_visit_min": int(r["time_per_visit_min"]),
            "latitude": float(r["latitude"]),
            "longitude": float(r["longitude"]),
            "priority_score": int(scores[r["outlet_code"]]),
        })

    # ---- Calendar: derive working days from May, not from cfg ------------
    cal = build_may_calendar()
    working_days = len(cal)
    cfg["working_days"] = working_days

    # ---- Step 1 (+Rule 3) ------------------------------------------------
    outlets = cluster_outlets(outlets, cfg["n_clusters"])
    zone_days = days_needed_per_zone(outlets, cfg["daily_capacity_min"])
    schedule, ranked = spread_zone_days(
        outlets, zone_days, working_days, scores)
    day_plan = allocate_outlets(
        outlets, schedule, scores, cfg["daily_capacity_min"])
    day_plan, balance_notes = balance_and_merge(
        day_plan, cfg["daily_capacity_min"])

    # ---- Step 4b: bonus visits up to the 500-min absolute ceiling --------
    day_plan, fill_notes = fill_to_etm_floor(
        day_plan, outlets, scores,
        etm_floor=float(cfg["daily_capacity_min"]),
        abi_lookup=abi_lookup)

    # ---- Step 4c: re-merge any day still under the 250 hard floor --------
    day_plan, balance_notes2 = balance_and_merge(
        day_plan, cfg["daily_capacity_min"])

    # ---- Step 5 (+Rule 5: 480-min total cap) -----------------------------
    base = (cfg["base_lat"], cfg["base_lon"])
    days_out = []
    cal_by_idx = {idx: (date, label) for idx, date, label in cal}
    for d in sorted(k for k, v in day_plan.items() if v):
        res = sequence_day(base, day_plan[d],
                           cfg["road_factor"], cfg["avg_speed_kmh"],
                           etm_floor=float(cfg["daily_capacity_min"]))
        date_obj, date_label = cal_by_idx.get(d, (None, f"Day {d}"))
        days_out.append({
            "day": d,
            "date_label": date_label,
            "date_iso": date_obj.isoformat() if date_obj else None,
            **res,
        })

    below_notes = [
        f"{x['date_label']}: in-market {x['visit_min']:.0f}m under "
        f"ETM floor {cfg['daily_capacity_min']:.0f}m"
        for x in days_out if x.get("below_floor")]

    nd = max(len(days_out), 1)
    total_km = round(sum(x["travel_km"] for x in days_out), 1)
    total_travel = round(sum(x["travel_min"] for x in days_out), 0)
    total_visit = round(sum(x["visit_min"] for x in days_out), 0)
    total_stops = sum(len(x["sequence"]) for x in days_out)

    summary = {
        "objective": OBJECTIVES[objective],
        "n_outlets": len(outlets),
        "n_zones": len(set(o["zone"] for o in outlets)),
        "n_route_days": len(days_out),
        "total_km": total_km,
        "total_travel_min": total_travel,
        "total_visit_min": total_visit,
        "avg_outlets_day": round(total_stops / nd, 1),
        "avg_km_day": round(total_km / nd, 1),
        "avg_etm_day": round(total_visit / nd, 0),
        "avg_travel_day": round(total_travel / nd, 0),
        "score_dist": sc["priority_score"].value_counts().sort_index().to_dict(),
        "balance_notes": [],   # engine actions intentionally not surfaced
        "zone_days": zone_days,
        "schedule": schedule,
        "ranked_zones": ranked,
        "calendar": [{"idx": i, "iso": d.isoformat(),
                      "label": lbl, "weekday": d.strftime("%a")}
                     for i, d, lbl in cal],
        "month_label": "May 2026",
    }
    return sc, outlets, days_out, summary
