"""
app.py -- Flask web app for the M1 Beat Plan prototype.
"""
import io
import os

import folium
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file

from pipeline import run_pipeline
from prioritization import OBJECTIVES, area_averages

app = Flask(__name__)
BASE = os.path.dirname(__file__)
SAMPLE = os.path.join(BASE, "sample", "M1_sample_universe.xlsx")
_LAST = {}

ZONE_COLORS = ["#e63946", "#2a9d8f", "#e9c46a", "#457b9d",
               "#9b5de5", "#f4a261", "#06d6a0", "#ef476f"]


@app.route("/")
def index():
    return render_template("index.html", objectives=OBJECTIVES)


@app.route("/sample")
def sample():
    return send_file(SAMPLE, as_attachment=True,
                     download_name="M1_sample_universe.xlsx")


@app.route("/run", methods=["POST"])
def run():
    try:
        objective = request.form["objective"]
        cfg = dict(
            base_lat=float(request.form["base_lat"]),
            base_lon=float(request.form["base_lon"]),
            daily_capacity_min=float(request.form["daily_capacity_min"]),
            avg_speed_kmh=float(request.form["avg_speed_kmh"]),
            working_days=int(request.form.get("working_days", "0") or 0),
            n_clusters=4,           # system-fixed
            road_factor=1.4,        # system-fixed
        )
        if "file" in request.files and request.files["file"].filename:
            df = pd.read_excel(request.files["file"])
        else:
            df = pd.read_excel(SAMPLE)

        sc, outlets, days, summary = run_pipeline(df, objective, cfg)
        _LAST["sc"] = sc
        _LAST["days"] = days
        _LAST["summary"] = summary
        _LAST["outlets"] = outlets
        _LAST["cfg"] = cfg

        # build cluster-overview folium map (universe + first few routes)
        _LAST["map_html"] = _build_cluster_map(outlets, days, cfg, sc)
        # cache per-day maps lazily
        _LAST["day_maps"] = {}

        name_lookup = {o["outlet_code"]: o["outlet_name"] for o in outlets}

        days_json = []
        for d in days:
            days_json.append({
                "day": d["day"],
                "date_label": d.get("date_label"),
                "date_iso": d.get("date_iso"),
                "travel_km": d["travel_km"],
                "travel_min": d["travel_min"],
                "visit_min": d["visit_min"],
                "total_min": d["total_min"],
                "below_floor": bool(d.get("below_floor", False)),
                "n": len(d["sequence"]),
                "bumped": [b["outlet_code"] for b in d["bumped"]],
                "stops": [{
                    "seq": i + 1,
                    "code": s["outlet_code"],
                    "name": name_lookup.get(s["outlet_code"], "\u2014"),
                    "channel": s["channel_type"],
                    "zone": s["zone"],
                    "priority": s["priority_score"],
                    "visit_min": s["time_per_visit_min"],
                } for i, s in enumerate(d["sequence"])],
            })

        # outlet-level summary rows (was: prioritization)
        scols = ["outlet_code", "priority_score", "abi_vol", "abi_share",
                 "psp_vol", "psp_share", "abi_share_dropping",
                 "outlet_context", "suggested_action"]
        score_rows_df = sc[scols].sort_values(
            "priority_score", ascending=False).head(40).copy()
        score_rows_df["outlet_name"] = score_rows_df["outlet_code"].map(
            name_lookup).fillna("\u2014")
        score_rows = score_rows_df.to_dict("records")

        return jsonify({
            "ok": True,
            "summary": {
                "objective": summary["objective"],
                "n_outlets": summary["n_outlets"],
                "n_zones": summary["n_zones"],
                "n_route_days": summary["n_route_days"],
                "total_km": summary["total_km"],
                "total_travel_min": summary["total_travel_min"],
                "total_visit_min": summary["total_visit_min"],
                "avg_outlets_day": summary["avg_outlets_day"],
                "avg_km_day": summary["avg_km_day"],
                "avg_etm_day": summary["avg_etm_day"],
                "avg_travel_day": summary["avg_travel_day"],
                "score_dist": {str(k): int(v)
                               for k, v in summary["score_dist"].items()},
                "balance_notes": summary["balance_notes"],
                "month_label": summary.get("month_label"),
                "calendar": summary.get("calendar", []),
            },
            "days": days_json,
            "scores": score_rows,
            "area_avg": area_averages(df),
            "map_url": "/map",
            "svg_map": _svg_map(outlets, days, cfg, sc),
        })
    except Exception as e:
        import traceback
        return jsonify({"ok": False,
                        "error": f"{e}",
                        "trace": traceback.format_exc()}), 400


@app.route("/map")
def map_view():
    return _LAST.get("map_html", "<p>Run a plan first.</p>")


@app.route("/daymap/<int:day_idx>")
def day_map(day_idx):
    """Interactive map for a single day's optimized route (in-app, no redirect)."""
    if "days" not in _LAST:
        return "Run a plan first.", 400
    cache = _LAST.setdefault("day_maps", {})
    if day_idx in cache:
        return cache[day_idx]
    days = _LAST["days"]
    cfg = _LAST["cfg"]
    name_lookup = {o["outlet_code"]: o["outlet_name"]
                   for o in _LAST["outlets"]}
    day = next((d for d in days if d["day"] == day_idx), None)
    if day is None:
        return "<p>Day not found.</p>", 404

    seq = day["sequence"]
    if not seq:
        return "<p>No stops for this day.</p>"

    lats = [s["latitude"] for s in seq] + [cfg["base_lat"]]
    lons = [s["longitude"] for s in seq] + [cfg["base_lon"]]
    clat = sum(lats) / len(lats)
    clon = sum(lons) / len(lons)
    fmap = folium.Map(location=[clat, clon], zoom_start=13,
                      tiles="cartodbpositron")
    folium.Marker([cfg["base_lat"], cfg["base_lon"]],
                  tooltip=f"M1 BASE \u2014 {day.get('date_label', '')}",
                  icon=folium.Icon(color="black", icon="home",
                                   prefix="fa")).add_to(fmap)
    for i, s in enumerate(seq, 1):
        nm = name_lookup.get(s["outlet_code"], "\u2014")
        folium.Marker(
            [s["latitude"], s["longitude"]],
            tooltip=(f"#{i} \u2022 {s['outlet_code']} \u2022 {nm} \u2022 "
                     f"P{s['priority_score']} \u2022 {s['channel_type']}"),
            icon=folium.DivIcon(html=(
                f'<div style="background:#f0a04b;color:#1a1206;'
                f'border:2px solid #1a1206;border-radius:50%;width:28px;'
                f'height:28px;display:flex;align-items:center;'
                f'justify-content:center;font-weight:700;font-family:sans-serif;'
                f'font-size:13px">{i}</div>'))).add_to(fmap)
    pts = ([[cfg["base_lat"], cfg["base_lon"]]]
           + [[s["latitude"], s["longitude"]] for s in seq]
           + [[cfg["base_lat"], cfg["base_lon"]]])
    folium.PolyLine(pts, color="#f0a04b", weight=3.5, opacity=0.85,
                    tooltip=f"{day.get('date_label', f'Day {day_idx}')} route"
                    ).add_to(fmap)
    html = fmap.get_root().render()
    cache[day_idx] = html
    return html


def _build_cluster_map(outlets, days, cfg, sc):
    clat = sum(o["latitude"] for o in outlets) / len(outlets)
    clon = sum(o["longitude"] for o in outlets) / len(outlets)
    fmap = folium.Map(location=[clat, clon], zoom_start=12,
                      tiles="cartodbpositron")
    folium.Marker([cfg["base_lat"], cfg["base_lon"]],
                  tooltip="M1 BASE",
                  icon=folium.Icon(color="black", icon="home",
                                   prefix="fa")).add_to(fmap)
    sc_map = dict(zip(sc["outlet_code"], sc["priority_score"]))
    for o in outlets:
        col = ZONE_COLORS[o["zone"] % len(ZONE_COLORS)]
        folium.CircleMarker(
            [o["latitude"], o["longitude"]],
            radius=4 + 1.6 * sc_map[o["outlet_code"]],
            color=col, fill=True, fill_opacity=0.75,
            tooltip=(f"{o['outlet_code']} \u2022 {o['outlet_name']} \u2022 "
                     f"{o['channel_type']} \u2022 Cluster {o['zone']} \u2022 "
                     f"P{sc_map[o['outlet_code']]}")
        ).add_to(fmap)
    return fmap.get_root().render()


def _svg_map(outlets, days, cfg, sc):
    sc_map = dict(zip(sc["outlet_code"], sc["priority_score"]))
    lats = [o["latitude"] for o in outlets] + [cfg["base_lat"]]
    lons = [o["longitude"] for o in outlets] + [cfg["base_lon"]]
    minx, maxx = min(lons), max(lons)
    miny, maxy = min(lats), max(lats)
    W, H, pad = 760, 460, 32

    def X(lon):
        return pad + (lon - minx) / (maxx - minx + 1e-9) * (W - 2 * pad)

    def Y(lat):
        return H - pad - (lat - miny) / (maxy - miny + 1e-9) * (H - 2 * pad)

    el = [f'<rect width="{W}" height="{H}" fill="#e8e2d4"/>']
    for i in range(1, 8):
        gx = pad + i * (W - 2 * pad) / 8
        gy = pad + i * (H - 2 * pad) / 8
        el.append(f'<line x1="{gx}" y1="{pad}" x2="{gx}" y2="{H-pad}" '
                  f'stroke="#d8d0bf" stroke-width="1"/>')
        el.append(f'<line x1="{pad}" y1="{gy}" x2="{W-pad}" y2="{gy}" '
                  f'stroke="#d8d0bf" stroke-width="1"/>')
    for o in outlets:
        col = ZONE_COLORS[o["zone"] % len(ZONE_COLORS)]
        r = 3 + 1.5 * sc_map[o["outlet_code"]]
        el.append(f'<circle cx="{X(o["longitude"]):.1f}" '
                  f'cy="{Y(o["latitude"]):.1f}" r="{r:.1f}" fill="{col}" '
                  f'fill-opacity="0.85" stroke="#fffdf7" stroke-width="0.8"/>')
    bx, by = X(cfg["base_lon"]), Y(cfg["base_lat"])
    el.append(f'<rect x="{bx-6}" y="{by-6}" width="12" height="12" '
              f'fill="#2b2a26" stroke="#fffdf7" stroke-width="1.5"/>')
    el.append(f'<text x="{bx+10}" y="{by+4}" fill="#2b2a26" '
              f'font-size="11" font-family="monospace">M1 BASE</text>')
    return (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:100%;border-radius:11px">'
            + "".join(el) + "</svg>")


@app.route("/daymap_svg/<int:day_idx>")
def day_map_svg(day_idx):
    if "days" not in _LAST:
        return "<svg/>", 400
    cfg = _LAST["cfg"]
    day = next((d for d in _LAST["days"] if d["day"] == day_idx), None)
    if day is None or not day["sequence"]:
        return "<svg/>", 404
    seq = day["sequence"]
    lats = [s["latitude"] for s in seq] + [cfg["base_lat"]]
    lons = [s["longitude"] for s in seq] + [cfg["base_lon"]]
    minx, maxx = min(lons), max(lons)
    miny, maxy = min(lats), max(lats)
    if maxx - minx < 1e-4: maxx = minx + 1e-4
    if maxy - miny < 1e-4: maxy = miny + 1e-4
    W, H, pad = 760, 460, 36

    def X(lon):
        return pad + (lon - minx) / (maxx - minx) * (W - 2 * pad)

    def Y(lat):
        return H - pad - (lat - miny) / (maxy - miny) * (H - 2 * pad)

    el = [f'<rect width="{W}" height="{H}" fill="#e8e2d4"/>']
    for i in range(1, 8):
        gx = pad + i * (W - 2 * pad) / 8
        gy = pad + i * (H - 2 * pad) / 8
        el.append(f'<line x1="{gx}" y1="{pad}" x2="{gx}" y2="{H-pad}" '
                  f'stroke="#d8d0bf" stroke-width="1"/>')
        el.append(f'<line x1="{pad}" y1="{gy}" x2="{W-pad}" y2="{gy}" '
                  f'stroke="#d8d0bf" stroke-width="1"/>')
    pts = ([(cfg["base_lon"], cfg["base_lat"])]
           + [(s["longitude"], s["latitude"]) for s in seq]
           + [(cfg["base_lon"], cfg["base_lat"])])
    d_attr = " ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in pts)
    el.append(f'<polyline points="{d_attr}" fill="none" stroke="#f0a04b" '
              f'stroke-width="2.4" opacity="0.85"/>')
    for i, s in enumerate(seq, 1):
        x, y = X(s["longitude"]), Y(s["latitude"])
        el.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="#f0a04b" '
                  f'stroke="#1a1206" stroke-width="1.5"/>')
        el.append(f'<text x="{x:.1f}" y="{y+3.5:.1f}" fill="#1a1206" '
                  f'font-size="11" font-family="monospace" font-weight="700" '
                  f'text-anchor="middle">{i}</text>')
    bx, by = X(cfg["base_lon"]), Y(cfg["base_lat"])
    el.append(f'<rect x="{bx-7}" y="{by-7}" width="14" height="14" '
              f'fill="#2b2a26" stroke="#fffdf7" stroke-width="1.5"/>')
    el.append(f'<text x="{bx+12}" y="{by+5}" fill="#2b2a26" '
              f'font-size="11" font-family="monospace">BASE</text>')
    return (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:100%">' + "".join(el) + "</svg>")


@app.route("/export")
def export():
    if "sc" not in _LAST:
        return "Run a plan first", 400
    name_lookup = {o["outlet_code"]: o["outlet_name"]
                   for o in _LAST["outlets"]}
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        sc_export = _LAST["sc"].drop(columns=["criteria_labels"]).copy()
        sc_export["outlet_name"] = sc_export["outlet_code"].map(
            name_lookup).fillna("\u2014")
        sc_export.to_excel(xl, index=False, sheet_name="OutletSummary")
        rows = []
        for d in _LAST["days"]:
            for i, s in enumerate(d["sequence"]):
                rows.append({
                    "day": d["day"], "date": d.get("date_label"),
                    "stop": i + 1,
                    "outlet_code": s["outlet_code"],
                    "outlet_name": name_lookup.get(s["outlet_code"], "\u2014"),
                    "channel": s["channel_type"], "cluster": s["zone"],
                    "priority": s["priority_score"],
                    "visit_min": s["time_per_visit_min"],
                    "day_travel_km": d["travel_km"],
                    "day_travel_min": d["travel_min"],
                    "day_total_min": d["total_min"],
                })
        pd.DataFrame(rows).to_excel(xl, index=False, sheet_name="BeatPlan")
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name="M1_beat_plan.xlsx",
                     mimetype="application/vnd.openxmlformats-"
                              "officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
