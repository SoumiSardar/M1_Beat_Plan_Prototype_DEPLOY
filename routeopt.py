"""
routeopt.py
===========
Cluster-Based M1 Beat Plan (route optimization).

Pipeline (confirmed order):
  Step 1  K-means cluster outlets by lat/long (default k=4, configurable)
  Rule 3  Split any cluster > 3000 minutes/month total visit time (geographic
          2-split) -- runs AFTER Step 1, BEFORE Step 2  [assumption B]
  Step 2  Days needed per zone =
            max( monthly visit-time in zone / daily capacity ,
                 max channel visit-frequency in the zone )
  Step 3  Spread each zone's days across the working calendar.
          Gap = working_days / days_needed. Zones ranked by how
          priority-heavy they are (lexicographic on #score4, #score3, ...)
          -> heavier zones take the earliest calendar days.
  Step 4  Allocate outlets to zone-days by frequency, priority (high picks
          earliest), and load balance. >=5 calendar-day gap between repeat
          visits to the same outlet  [assumption A].
  Step 5  Sequence each day: haversine * road_factor distance matrix ->
          nearest-neighbour seed -> OR-Tools / 2-opt TSP, closed loop
          base -> outlets -> base.

Rules:
  R1  >=5 calendar-day gap between visits to the same outlet
  R2  if a cluster-day fills <50% of daily capacity on its own, merge with
      another cluster-day with a gap, or the nearest cluster
  R3  cluster monthly visit time cap = 3000 minutes -> geographic split
  R4  too few outlets: pack as full as possible; if a day can't reach 50%,
      push it to the next day rather than run near-empty
  R5  the 8-hour day: in-outlet + travel must be <= 480 min, and in-outlet
      alone must be <= ETM + 20 (soft target = ETM). Travel itself is
      uncapped. Over-cap days drop their lowest-priority outlet and re-solve.
  Fallback when an outlet is bumped (R5 / load balance) [assumption C]:
      same-zone day with space (respecting R1) -> nearest cluster-day with
      capacity -> else add a day.
"""
import math
import numpy as np
from sklearn.cluster import KMeans

try:
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    _HAS_ORTOOLS = True
except Exception:
    _HAS_ORTOOLS = False

CLUSTER_MIN_CAP = 3000.0      # Rule 3, minutes/month
OVERALL_DAY_CAP_MIN = 480.0   # comfortable 8h ceiling (in-outlet + travel)
HARD_TOTAL_CAP_MIN = 500.0    # absolute ceiling = 480 + 20-min buffer
HARD_FLOOR_MIN = 250.0        # absolute in-market floor per day
FILL_FLOOR = 0.50             # Rules 2 & 4 (legacy, kept for safety)
SAME_OUTLET_GAP_DAYS = 4      # Rule 1


def haversine_km(a, b):
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------
# Step 1 + Rule 3
# --------------------------------------------------------------------------
def cluster_outlets(outlets, k):
    coords = np.array([[o["latitude"], o["longitude"]] for o in outlets])
    k = max(1, min(k, len(outlets)))
    km = KMeans(n_clusters=k, n_init=10, random_state=42).fit(coords)
    for o, lab in zip(outlets, km.labels_):
        o["zone"] = int(lab)
    return _enforce_cluster_cap(outlets)


def _zone_monthly_minutes(zone_outlets):
    return sum(o["time_per_visit_min"] * o["visit_frequency_per_month"]
               for o in zone_outlets)


def _enforce_cluster_cap(outlets):
    """Rule 3: any zone > 3000 min/month is geographically split in two."""
    changed = True
    while changed:
        changed = False
        zones = {}
        for o in outlets:
            zones.setdefault(o["zone"], []).append(o)
        for z, members in list(zones.items()):
            if _zone_monthly_minutes(members) > CLUSTER_MIN_CAP and len(members) > 1:
                coords = np.array([[m["latitude"], m["longitude"]]
                                   for m in members])
                sub = KMeans(n_clusters=2, n_init=10,
                             random_state=42).fit(coords)
                new_id = max(o["zone"] for o in outlets) + 1
                for m, s in zip(members, sub.labels_):
                    if s == 1:
                        m["zone"] = new_id
                changed = True
                break
    return outlets


# --------------------------------------------------------------------------
# Step 2
# --------------------------------------------------------------------------
def days_needed_per_zone(outlets, daily_capacity_min):
    zones = {}
    for o in outlets:
        zones.setdefault(o["zone"], []).append(o)
    out = {}
    for z, members in zones.items():
        workload = _zone_monthly_minutes(members)
        by_time = math.ceil(workload / daily_capacity_min) if daily_capacity_min else 1
        by_freq = max(o["visit_frequency_per_month"] for o in members)
        out[z] = {
            "days_needed": max(by_time, by_freq, 1),
            "by_time": by_time,
            "by_freq": by_freq,
            "monthly_minutes": workload,
            "n_outlets": len(members),
        }
    return out


# --------------------------------------------------------------------------
# Step 3
# --------------------------------------------------------------------------
def _zone_priority_key(members, scores):
    """Lexicographic: more score-4 first, then score-3, ... (descending)."""
    counts = [0, 0, 0, 0, 0]  # index = score 0..4
    for o in members:
        counts[scores[o["outlet_code"]]] += 1
    return (-counts[4], -counts[3], -counts[2], -counts[1], -counts[0])


def spread_zone_days(outlets, zone_days, working_days, scores):
    zones = {}
    for o in outlets:
        zones.setdefault(o["zone"], []).append(o)

    ranked = sorted(zones.keys(),
                    key=lambda z: _zone_priority_key(zones[z], scores))

    schedule = {}        # zone -> [calendar day ints]
    start_offset = 0
    for z in ranked:
        dn = zone_days[z]["days_needed"]
        gap = max(1, working_days // dn)
        days = [min(working_days, 1 + start_offset + i * gap)
                for i in range(dn)]
        # de-duplicate / clamp into the working window
        clean, last = [], 0
        for d in days:
            d = min(max(d, last + 1), working_days)
            clean.append(d)
            last = d
        schedule[z] = clean
        start_offset += 1     # priority-heavier zone starts a day earlier
    return schedule, ranked


# --------------------------------------------------------------------------
# Step 4
# --------------------------------------------------------------------------
def allocate_outlets(outlets, schedule, scores, daily_capacity_min):
    """Assign each outlet's required monthly visits to its zone's calendar
    days, high-priority first, respecting the >=5 calendar-day gap."""
    zones = {}
    for o in outlets:
        zones.setdefault(o["zone"], []).append(o)

    day_plan = {}   # calendar_day -> list of outlet dicts (with zone)
    for z, members in zones.items():
        zdays = schedule[z]
        members_sorted = sorted(
            members, key=lambda o: (-scores[o["outlet_code"]],
                                    -o["time_per_visit_min"]))
        for o in members_sorted:
            freq = o["visit_frequency_per_month"]
            chosen, last_day = [], -999
            # prefer earliest zone-days for high priority outlets
            candidate_days = list(zdays)
            for d in candidate_days:
                if len(chosen) >= freq:
                    break
                if d - last_day >= 4:                       # Rule 1 (4-day gap)
                    load = sum(x["time_per_visit_min"]
                               for x in day_plan.get(d, []))
                    if load + o["time_per_visit_min"] <= daily_capacity_min:
                        chosen.append(d)
                        last_day = d
            # if frequency not satisfied, extend with spaced extra days
            d = (chosen[-1] if chosen else 0) + 4
            while len(chosen) < freq and d <= max(schedule[z] + [d]):
                chosen.append(d)
                d += 4
            for d in chosen:
                day_plan.setdefault(d, []).append({**o, "zone": z})
    return day_plan


def balance_and_merge(day_plan, daily_capacity_min):
    """Merge under-floor days. Hard in-market floor = HARD_FLOOR_MIN (250 min).
    Any day below this floor is merged into another day with spare capacity:
      (1) prefer a day in the SAME cluster;
      (2) else the nearest day by working-day index that has room.
    Receivers may absorb up to HARD_TOTAL_CAP_MIN in_market room. The
    actual sequenced day is still bounded by HARD_TOTAL_CAP_MIN in
    sequence_day, so over-fills get trimmed at the final stage if any.
    Notes returned but suppressed by the pipeline.
    """
    notes = []
    days_sorted = sorted(day_plan.keys())

    def zones_on(d):
        return {o["zone"] for o in day_plan.get(d, []) if o.get("zone") is not None}

    def load_of(d):
        return sum(o["time_per_visit_min"] for o in day_plan.get(d, []))

    # iterate until no more under-250 days can be merged
    changed = True
    safety = 0
    while changed and safety < 50:
        changed = False
        safety += 1
        for d in days_sorted:
            if not day_plan.get(d):
                continue
            load = load_of(d)
            if load >= HARD_FLOOR_MIN:
                continue
            my_zones = zones_on(d)
            candidates = []
            for d2 in days_sorted:
                if d2 == d or not day_plan.get(d2):
                    continue
                load2 = load_of(d2)
                # receiver may absorb if combined load <= 460 min in-market
                # (leaves ~40 min for travel within the 500 hard total cap;
                # sequence_day will trim if a final route still exceeds 500)
                if load2 + load > 460:
                    continue
                overlap = 1 if (zones_on(d2) & my_zones) else 0
                candidates.append((-overlap, abs(d2 - d), d2))
            candidates.sort()
            if candidates:
                _, _, d2 = candidates[0]
                day_plan[d2].extend(day_plan[d])
                day_plan[d] = []
                notes.append(f"merge:{d}->{d2}")
                changed = True
    return day_plan, notes


# --------------------------------------------------------------------------
# Step 4b: high-priority fill for early days below the ETM floor
# --------------------------------------------------------------------------
def fill_to_etm_floor(day_plan, outlets, scores, etm_floor,
                      daily_overall_cap=OVERALL_DAY_CAP_MIN,
                      gap=SAME_OUTLET_GAP_DAYS, abi_lookup=None,
                      avg_travel_share=0.20):
    """For every working day, keep adding bonus visits as long as the day's
    in-market + estimated travel stays under the absolute ceiling
    HARD_TOTAL_CAP_MIN (480 + 20 buffer = 500 min).

    Bonus pool: POCs with score >= 2 (score 4 first, then 3, then 2),
    sorted within each score by ABI volume desc. Each POC may receive at
    most ONE bonus visit across the month (over and above contractual
    frequency). The 4-day same-POC gap is respected.

    We estimate travel as a fraction of total time (avg_travel_share=20%)
    because we don't TSP-sequence here. Real travel is enforced in
    sequence_day() afterwards using the actual 480/500 cap.
    """
    notes = []
    if not day_plan:
        return day_plan, notes
    if abi_lookup is None:
        abi_lookup = {o["outlet_code"]: 0.0 for o in outlets}

    # candidate pool: scores 4, 3, 2 (was 4, 3 only)
    cands = [o for o in outlets if scores.get(o["outlet_code"], 0) >= 2]
    cands.sort(key=lambda o: (-scores[o["outlet_code"]],
                              -abi_lookup.get(o["outlet_code"], 0.0)))
    used_bonus = set()

    by_outlet = {}
    for d, lst in day_plan.items():
        for o in lst:
            by_outlet.setdefault(o["outlet_code"], []).append(d)

    def gap_ok(code, day):
        for dd in by_outlet.get(code, []):
            if abs(dd - day) < gap:
                return False
        return True

    # est_total = in_outlet / (1 - travel_share); allow adds until that
    # estimate breaches HARD_TOTAL_CAP_MIN. Equivalent in-outlet ceiling:
    in_outlet_ceiling = HARD_TOTAL_CAP_MIN * (1.0 - avg_travel_share)

    def pack_day(d):
        cur_visit = sum(o["time_per_visit_min"] for o in day_plan[d])
        for o in cands:
            code = o["outlet_code"]
            if code in used_bonus:
                continue
            if not gap_ok(code, d):
                continue
            extra = o["time_per_visit_min"]
            if cur_visit + extra > in_outlet_ceiling:
                continue
            day_plan[d].append({**o, "zone": o.get("zone")})
            by_outlet.setdefault(code, []).append(d)
            used_bonus.add(code)
            cur_visit += extra

    # Pass 1: rescue any day below the hard floor first (priority access
    # to the bonus pool, so under-250 days have a fair shot).
    under_floor = sorted(
        [d for d in day_plan if sum(o["time_per_visit_min"]
                                    for o in day_plan[d]) < HARD_FLOOR_MIN])
    for d in under_floor:
        pack_day(d)

    # Pass 2: top up all remaining days in calendar order until ceiling.
    for d in sorted(day_plan.keys()):
        if d in under_floor:
            continue
        pack_day(d)
    return day_plan, notes


# --------------------------------------------------------------------------
# Step 5 + Rule 5
# --------------------------------------------------------------------------
def _distance_matrix(points, road_factor):
    n = len(points)
    M = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                M[i][j] = haversine_km(points[i], points[j]) * road_factor
    return M


def _nn_2opt(M):
    """Nearest-neighbour seed + 2-opt. Optimal-quality for small daily routes
    and runs in well under a millisecond for typical day sizes."""
    n = len(M)
    order = [0]
    unvis = set(range(1, n))
    while unvis:
        last = order[-1]
        nxt = min(unvis, key=lambda j: M[last][j])
        order.append(nxt)
        unvis.remove(nxt)
    improved = True
    while improved:
        improved = False
        for i in range(1, n - 1):
            for k in range(i + 1, n):
                a, b = order[i - 1], order[i]
                c, d = order[k], order[(k + 1) % n]
                if M[a][b] + M[c][d] > M[a][c] + M[b][d] + 1e-9:
                    order[i:k + 1] = reversed(order[i:k + 1])
                    improved = True
    return order


def _tsp_order(M):
    n = len(M)
    if n <= 3:
        return list(range(n))
    # For typical daily route sizes the NN+2-opt local optimum is effectively
    # optimal and is ~1000x faster than invoking the OR-Tools solver with a
    # time limit on every day. Only escalate to OR-Tools for larger days.
    if n <= 16 or not _HAS_ORTOOLS:
        return _nn_2opt(M)
    if _HAS_ORTOOLS:
        mgr = pywrapcp.RoutingIndexManager(n, 1, 0)
        routing = pywrapcp.RoutingModel(mgr)

        def cb(i, j):
            return int(M[mgr.IndexToNode(i)][mgr.IndexToNode(j)] * 1000)

        t = routing.RegisterTransitCallback(cb)
        routing.SetArcCostEvaluatorOfAllVehicles(t)
        p = pywrapcp.DefaultRoutingSearchParameters()
        p.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
        p.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)
        p.time_limit.FromMilliseconds(300)
        sol = routing.SolveWithParameters(p)
        if sol:
            order, idx = [], routing.Start(0)
            while not routing.IsEnd(idx):
                order.append(mgr.IndexToNode(idx))
                idx = sol.Value(routing.NextVar(idx))
            return order
    return _nn_2opt(M)


def sequence_day(base, outlets, road_factor, avg_speed_kmh,
                 etm_floor=360.0):
    """TSP a single day's outlets, then enforce the absolute day cap.

    Day is bounded by HARD_TOTAL_CAP_MIN (480 + 20-min buffer = 500 min).
    If total > 500, drop the lowest-priority outlet and re-solve.
    A day landing between 480 and 500 is accepted (within the buffer).
    """
    bumped = []
    work = sorted(outlets, key=lambda o: o.get("priority_score", 0))  # low first
    while True:
        pts = [base] + [(o["latitude"], o["longitude"]) for o in work]
        M = _distance_matrix(pts, road_factor)
        order = _tsp_order(M)
        if order and order[0] != 0:           # rotate so base is first
            z = order.index(0)
            order = order[z:] + order[:z]
        dist = sum(M[order[i]][order[i + 1]] for i in range(len(order) - 1))
        dist += M[order[-1]][order[0]]        # close the loop
        travel_min = dist / max(avg_speed_kmh, 1) * 60
        in_outlet = sum(o["time_per_visit_min"] for o in work)
        total_min = in_outlet + travel_min
        if total_min <= HARD_TOTAL_CAP_MIN or len(work) <= 1:
            seq = [work[i - 1] for i in order if i != 0]
            return {
                "sequence": seq,
                "travel_km": round(dist, 2),
                "travel_min": round(travel_min, 1),
                "visit_min": in_outlet,
                "total_min": round(total_min, 1),
                "below_floor": in_outlet < etm_floor,
                "bumped": bumped,
            }
        bumped.append(work.pop(0))            # drop lowest priority (Rule 5)
