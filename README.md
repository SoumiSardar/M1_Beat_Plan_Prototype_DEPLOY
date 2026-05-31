# M1 Beat Plan — Outlet Prioritization & Route Optimization (Prototype)

A working prototype that turns an **M1 outlet universe** into an
**optimized monthly beat plan**, exactly to the agreed business logic.

---

## What it does

1. **Outlet Prioritization** — you pick **one objective for the month**
   (Market Share Increase / Volume Growth / Increase P+SP Share). Only that
   objective's **4 binary criteria** run, scoring every outlet **0–4**
   (higher = higher priority). Period logic is YoY: latest complete quarter
   (Q1 2026) vs same quarter prior year (Q1 2025). "Area average" and the
   ±1 SD bands are computed across the whole M1 universe.

2. **Route Optimization** — the confirmed 5-step cluster-based pipeline:
   - **Step 1** K-means clusters outlets into geographic zones (default 4)
   - **Rule 3** any zone > 3000 min/month is geographically split
   - **Step 2** days-needed per zone = max(workload ÷ daily capacity,
     max channel visit-frequency)
   - **Step 3** zone days spread across the calendar; priority-heavier
     zones take the earliest days
   - **Step 4** outlets allocated to zone-days by frequency, priority
     (high picks earliest), load balance; ≥5 calendar-day gap between
     repeat visits to the same outlet
   - **Step 5** each day sequenced with haversine × road-factor distance
     and a TSP solve (closed loop base → outlets → base)
   - **Rules 2 & 4** under-50% days merged / pushed
   - **Rule 5** if a day's travel > 2.5 h, lowest-priority outlets are
     dropped and the day re-solved

## Assumptions in effect (each is a single config toggle)

- **A** — the ≥5-day gap is measured on calendar/working days
- **B** — oversized-cluster split runs right after Step 1, before Step 2
- **C** — a bumped outlet goes: same-zone day with space → nearest
  cluster-day with capacity → else an added day

These are surfaced in the UI so they can be discussed/changed.

---

## Run it

```bash
pip install flask pandas openpyxl scikit-learn folium ortools
cd beatplan
python3 app.py
# open http://localhost:5000
```

Click **Download sample M1 file** to see the exact expected input format,
or upload your own M1 Excel in the same structure.

## Input file structure (one row per outlet)

| Column | Meaning |
|---|---|
| `outlet_code` | unique outlet id |
| `channel_type` | Counter A (4×/mo), Counter B (3×), Grocery (2×), Convenience (1×) |
| `visit_frequency_per_month` | required visits / month |
| `time_per_visit_min` | minutes spent in the outlet per visit |
| `latitude`, `longitude` | outlet location |
| `total_vol_<YYYY_MM>` | total category volume for that month |
| `psp_vol_<YYYY_MM>` | Premium + Super-Premium volume |
| `abi_vol_<YYYY_MM>` | ABI volume |
| `abi_psp_vol_<YYYY_MM>` | ABI P+SP volume |

Months provided: `2025_01..03` and `2026_01..03` (Q1 YoY pair). All growth
and "is dropping" values are derived by the engine.

## Files

- `app.py` — Flask web app
- `prioritization.py` — the 3 objectives, 4 binary criteria each
- `routeopt.py` — 5-step cluster pipeline + all 5 rules + TSP
- `pipeline.py` — orchestrator
- `make_sample.py` — regenerates the sample M1 file
- `templates/index.html` — dashboard UI

## Notes for the project-owner discussion

- Numeric Distribution (Objective 4) is intentionally parked for this
  prototype; the engine is structured so it slots in later.
- TSP uses nearest-neighbour + 2-opt, which is effectively optimal at
  daily-route sizes and ~1000× faster than a full solver per day;
  OR-Tools is used automatically for unusually large days.
- Distances are haversine × a configurable road factor (default 1.3),
  not live road-network data — appropriate and defensible for a prototype.
