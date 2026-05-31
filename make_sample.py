"""
Generates a sample M1-specific Excel file in the EXACT input structure agreed:

Static / routing fields (one row per outlet):
  outlet_code, channel_type, visit_frequency_per_month, time_per_visit_min,
  latitude, longitude

Sales fields, monthly, for Q1 2026 (Jan/Feb/Mar) and Q1 2025 (Jan/Feb/Mar):
  For each of the 6 months:
    total_vol_<m>, psp_vol_<m>, abi_vol_<m>, abi_psp_vol_<m>

All growth / "is dropping" values are DERIVED by the engine (not supplied).
Area average + SD are computed across the M1 universe (this whole file).
"""
import numpy as np
import pandas as pd

rng = np.random.default_rng(42)

# Bengaluru-ish cluster centres so the K-means zones look real on a map
CENTRES = [
    (12.9716, 77.5946),  # central
    (12.9352, 77.6245),  # koramangala
    (13.0298, 77.5400),  # north-west
    (12.9081, 77.6476),  # south-east
]

CHANNELS = {
    "Counter A": 4,   # must be visited 4x / month
    "Counter B": 3,
    "Grocery":   2,
    "Convenience": 1,
}

MONTHS_2026 = ["2026_01", "2026_02", "2026_03"]
MONTHS_2025 = ["2025_01", "2025_02", "2025_03"]
ALL_MONTHS = MONTHS_2025 + MONTHS_2026

rows = []
N = 70
for i in range(N):
    cx, cy = CENTRES[i % len(CENTRES)]
    lat = cx + rng.normal(0, 0.012)
    lon = cy + rng.normal(0, 0.012)
    channel = rng.choice(list(CHANNELS.keys()), p=[0.18, 0.25, 0.32, 0.25])
    freq = CHANNELS[channel]
    tpv = int(rng.choice([20, 25, 30, 40, 45]))

    # base scale for the outlet
    scale = float(rng.lognormal(mean=6.2, sigma=0.55))
    growth = rng.normal(1.06, 0.18)          # YoY multiplier on total
    abi_growth = rng.normal(1.04, 0.22)      # YoY multiplier on ABI
    psp_ratio = np.clip(rng.normal(0.32, 0.1), 0.05, 0.7)
    abi_ratio = np.clip(rng.normal(0.28, 0.12), 0.0, 0.8)
    abi_psp_ratio = np.clip(rng.normal(0.30, 0.12), 0.0, 0.9)

    rec = {
        "outlet_code": f"OUT{1000+i}",
        "channel_type": channel,
        "visit_frequency_per_month": freq,
        "time_per_visit_min": tpv,
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
    }

    for m in ALL_MONTHS:
        yr = m.startswith("2026")
        g = growth if yr else 1.0
        ag = abi_growth if yr else 1.0
        month_noise = rng.normal(1.0, 0.08)
        total = max(0.0, scale * g * month_noise)
        psp = total * psp_ratio * rng.normal(1.0, 0.05)
        abi = total * abi_ratio * ag * rng.normal(1.0, 0.06)
        abi_psp = abi * abi_psp_ratio * rng.normal(1.0, 0.05)
        rec[f"total_vol_{m}"] = round(total, 1)
        rec[f"psp_vol_{m}"] = round(psp, 1)
        rec[f"abi_vol_{m}"] = round(abi, 1)
        rec[f"abi_psp_vol_{m}"] = round(min(abi_psp, abi), 1)

    rows.append(rec)

df = pd.DataFrame(rows)
out = "/home/claude/beatplan/sample/M1_sample_universe.xlsx"
with pd.ExcelWriter(out, engine="openpyxl") as xl:
    df.to_excel(xl, index=False, sheet_name="M1_Universe")

print("wrote", out, df.shape)
print(df[["outlet_code", "channel_type", "visit_frequency_per_month",
          "time_per_visit_min"]].head())
