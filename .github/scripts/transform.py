"""Transform raw Smartsheet Job Costing + Sales Leads Pipeline + Operations
Schedule data into the sales dashboard data.json.

Usage:  python3 transform.py jc_raw.json [lp_raw.json] [os_raw.json] > data.json

Conventions (per Dan, 2026-06-08):
- Bid Won = New Status == "Bid Won - Send to Schedule"
- "Sold MTD" uses Date Signed from Operations Schedule (true won date)
- "Bid MTD" uses Date of Estimate
"""
import json, re, sys, statistics
from datetime import datetime, date, timezone, timedelta
from collections import defaultdict, Counter

# ---------- Column IDs ----------
JC = {
    "JOB_ID": 1510379388620676, "JOB_NUM": 2141821856599940,
    "NEW_STATUS": 5975661087510404, "DATE_ESTIMATE": 8265778829676420,
    "ESTIMATOR": 4747341620793220, "COMPANY": 5838057155547012,
    "CLIENT_FIRST": 947429435199364, "JOB_CITY": 7843566364610436,
    "JOB_STATE": 1804840870039428, "PROJECT_PRICE": 2460357435019140,
    "PROJECT_PRICE_NEW": 1710718738272132, "PROJECT_SY": 2495541807107972,
    "PRICE_PER_SY": 6963957062389636, "PRICE_PER_SY_NEW": 6214318365642628,
    "FORM_VERSION": 8502180738011012, "GROSS_MARGIN": 1193720039821188,
    "MILLING_DAYS": 3621441713950596, "PAVING_DAYS": 1369641900265348,
    "CRACKFILL_DAYS": 5873241527635844, "HAND_DAYS": 8125041341321092,
    "RECLAIM_DAYS": 806691946844036, "PULVERIZE_DAYS": 1932591853686660,
}
LP = {
    "PROPERTY": 5197202266476420, "LEAD_TIER": 1819502545948548,
    "PROJECT_VALUE": 412127662395268, "OUTREACH_STATUS": 975077615816580,
    "RECOMMENDED": 4071302359633796, "PROPERTY_TYPE": 4915727289765764,
}
OS = {
    "JOB_NUM": 7358000912879492, "STATUS": 4683988634128260,
    "START": 8765375796432772, "END": 180389006757764,
    "ASSIGNED_CREW": 8202425843011460, "DATE_SIGNED": 2432188820443012,
    "CONTRACT_PRICE": 8367024680685444,
}

WON = "Bid Won - Send to Schedule"
LOST = "Bid Lost"
EST = "Estimations"
MIN_SY = 50  # filter $/SY outliers — jobs with SY < 50 distort the average
CAPACITY_CREW_DAYS_PER_MONTH = 132  # 22 working days × 6 crews

# ---------- helpers ----------
def name_of(c):
    if isinstance(c, dict): return c.get("name") or c.get("email") or ""
    return c or ""

def clean_name(c):
    s = name_of(c) if isinstance(c, dict) else (c or "")
    s = str(s).strip()
    if not s: return ""
    local = s.split("@")[0] if "@" in s else s
    if " " in local and "@" not in s: return local
    local = local.replace(".", " ").replace("_", " ")
    if " " not in local and len(local) > 1:
        return (local[0].upper() + " " + local[1:].title()).strip()
    return " ".join(p.capitalize() for p in local.split())

def cell_value(r, cid):
    for c in (r.get("cells") or []):
        if int(c.get("columnId", 0)) == int(cid):
            v = c.get("value")
            return v if v is not None else c.get("displayValue")
    return None

def cell_display(r, cid):
    for c in (r.get("cells") or []):
        if int(c.get("columnId", 0)) == int(cid):
            return c.get("displayValue") or c.get("value")
    return None

def parse_date(s):
    if not s: return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(s))
    if not m: return None
    try: return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError: return None

def to_number(v):
    if v is None: return None
    try: return float(v)
    except (TypeError, ValueError):
        try: return float(str(v).replace("$", "").replace(",", "").strip())
        except ValueError: return None

def money_round(x): return int(round(x)) if x else 0

def week_start(d): return d - timedelta(days=d.weekday())

def load(p):
    with open(p) as f: return json.load(f)

# ---------- main ----------
jc_path = sys.argv[1]
lp_path = sys.argv[2] if len(sys.argv) > 2 else None
os_path = sys.argv[3] if len(sys.argv) > 3 else None

jc_raw = load(jc_path)
lp_raw = load(lp_path) if lp_path else None
os_raw = load(os_path) if os_path else None

today = date.today()
mtd_start = today.replace(day=1)
prior_mtd_start = (mtd_start.replace(year=mtd_start.year - 1, month=12)
                   if mtd_start.month == 1
                   else mtd_start.replace(month=mtd_start.month - 1))
prior_mtd_end = mtd_start - timedelta(days=1)
yoy_mtd_start = mtd_start.replace(year=mtd_start.year - 1) if mtd_start.year > 2000 else None
yoy_today = today.replace(year=today.year - 1) if today.year > 2000 else None

window_90_start = today - timedelta(days=90)
window_30_start = today - timedelta(days=30)
window_60_start = today - timedelta(days=60)
ts_start_week = week_start(today) - timedelta(weeks=12)

# ---------- Flatten Job Costing ----------
jobs = []
for r in jc_raw.get("rows", []):
    form_v = (cell_value(r, JC["FORM_VERSION"]) or "").strip().lower()
    pp_new = to_number(cell_value(r, JC["PROJECT_PRICE_NEW"]))
    pp_old = to_number(cell_value(r, JC["PROJECT_PRICE"]))
    price = pp_new if (form_v == "v2" and pp_new) else (pp_old or pp_new or 0)
    ps_new = to_number(cell_value(r, JC["PRICE_PER_SY_NEW"]))
    ps_old = to_number(cell_value(r, JC["PRICE_PER_SY"]))
    psy = ps_new if (form_v == "v2" and ps_new) else (ps_old or ps_new)
    crew_days = sum((to_number(cell_value(r, JC[k])) or 0) for k in
                    ("MILLING_DAYS","PAVING_DAYS","CRACKFILL_DAYS","HAND_DAYS","RECLAIM_DAYS","PULVERIZE_DAYS"))
    jobs.append({
        "row_id": r.get("id"),
        "job_id": str(cell_display(r, JC["JOB_ID"]) or ""),
        "job_num": str(cell_display(r, JC["JOB_NUM"]) or ""),
        "status": cell_value(r, JC["NEW_STATUS"]) or "",
        "date_estimate": parse_date(cell_value(r, JC["DATE_ESTIMATE"])),
        "estimator": clean_name(cell_value(r, JC["ESTIMATOR"])),
        "company": cell_value(r, JC["COMPANY"]) or cell_value(r, JC["CLIENT_FIRST"]) or "Unknown",
        "price": price or 0,
        "sy": to_number(cell_value(r, JC["PROJECT_SY"])) or 0,
        "price_per_sy": psy,
        "crew_days": crew_days,
        "city": cell_value(r, JC["JOB_CITY"]) or "",
    })

# ---------- Date Signed from Ops Schedule (true "won" date) ----------
signed_by_job = {}
scheduled_jobs = set()
if os_raw:
    for r in os_raw.get("rows", []):
        jn = cell_display(r, OS["JOB_NUM"])
        if not jn: continue
        jn = str(jn).strip()
        scheduled_jobs.add(jn)
        ds = parse_date(cell_value(r, OS["DATE_SIGNED"]))
        if ds and (jn not in signed_by_job or ds < signed_by_job[jn]):
            signed_by_job[jn] = ds

for j in jobs:
    j["date_signed"] = signed_by_job.get(j["job_num"])

def signed_in(j, start, end):
    """Date the job was 'signed/won'. Falls back to Date of Estimate because
    Date Signed is not populated anywhere in the current workspace (verified
    2026-06-08). Effectively answers: 'of bids estimated in this window,
    which were won?'"""
    ds = j.get("date_signed") or j.get("date_estimate")
    return ds is not None and start <= ds <= end

# Did anyone in the current data have a real Date Signed?
DATE_SIGNED_POPULATED = any(j.get("date_signed") for j in jobs)

# ---------- KPIs ----------
won_90 = [j for j in jobs if j["status"] == WON and j["date_estimate"] and j["date_estimate"] >= window_90_start]
lost_90 = [j for j in jobs if j["status"] == LOST and j["date_estimate"] and j["date_estimate"] >= window_90_start]
hr_total = len(won_90) + len(lost_90)
hr_pct = round((len(won_90) / hr_total) * 100, 1) if hr_total else None

sold_mtd = sum(j["price"] for j in jobs if j["status"] == WON and signed_in(j, mtd_start, today))
sold_prior = sum(j["price"] for j in jobs if j["status"] == WON and signed_in(j, prior_mtd_start, prior_mtd_end))
sold_yoy = (sum(j["price"] for j in jobs if j["status"] == WON and signed_in(j, yoy_mtd_start, yoy_today))
            if yoy_mtd_start else None)
sold_mtd_count = sum(1 for j in jobs if j["status"] == WON and signed_in(j, mtd_start, today))

bid_mtd_count = sum(1 for j in jobs if j["date_estimate"] and j["date_estimate"] >= mtd_start)
bid_mtd = sum(j["price"] for j in jobs if j["date_estimate"] and j["date_estimate"] >= mtd_start)
bid_prior = sum(j["price"] for j in jobs if j["date_estimate"] and prior_mtd_start <= j["date_estimate"] <= prior_mtd_end)

# Labor-Month Backlog (open won jobs)
total_crew_days_open = sum(j["crew_days"] for j in jobs if j["status"] == WON)
labor_months = round(total_crew_days_open / CAPACITY_CREW_DAYS_PER_MONTH, 2) if CAPACITY_CREW_DAYS_PER_MONTH else None

def sane_psy(j):
    return j["price_per_sy"] and j["status"] == WON and j["sy"] and j["sy"] >= MIN_SY
recent_psy = [j["price_per_sy"] for j in jobs if sane_psy(j) and j["date_estimate"] and j["date_estimate"] >= window_30_start]
prior_psy = [j["price_per_sy"] for j in jobs if sane_psy(j) and j["date_estimate"] and window_60_start <= j["date_estimate"] < window_30_start]
avg_psy_recent = round(statistics.mean(recent_psy), 2) if recent_psy else None
avg_psy_prior = round(statistics.mean(prior_psy), 2) if prior_psy else None
delta_pct = (round(((avg_psy_recent - avg_psy_prior) / avg_psy_prior) * 100, 1)
             if avg_psy_recent and avg_psy_prior else None)

# ---------- Pipeline funnel ----------
est_open = [j for j in jobs if j["status"] == EST]
est_aging = [j for j in est_open if j["date_estimate"] and (today - j["date_estimate"]).days >= 5]
awarded_not_sched = [j for j in jobs if j["status"] == WON and j["job_num"] and j["job_num"] not in scheduled_jobs]
in_prod = [j for j in jobs if j["status"] == WON and j["job_num"] in scheduled_jobs]
pipeline_stages = [
    {"name": "Estimations Open", "count": len(est_open), "amount": money_round(sum(j["price"] for j in est_open))},
    {"name": "Aging > 5 days", "count": len(est_aging), "amount": money_round(sum(j["price"] for j in est_aging))},
    {"name": "Awarded Not Scheduled", "count": len(awarded_not_sched), "amount": money_round(sum(j["price"] for j in awarded_not_sched))},
    {"name": "In Production", "count": len(in_prod), "amount": money_round(sum(j["price"] for j in in_prod))},
]

# ---------- Estimator leaderboard (MTD) ----------
est_stats = defaultdict(lambda: {"bidsCount": 0, "bidsAmount": 0, "wonCount": 0, "wonAmount": 0, "lostCount": 0})
for j in jobs:
    if not j["estimator"]: continue
    if j["date_estimate"] and j["date_estimate"] >= mtd_start:
        est_stats[j["estimator"]]["bidsCount"] += 1
        est_stats[j["estimator"]]["bidsAmount"] += j["price"]
        if j["status"] == LOST:
            est_stats[j["estimator"]]["lostCount"] += 1
    if j["status"] == WON and signed_in(j, mtd_start, today):
        est_stats[j["estimator"]]["wonCount"] += 1
        est_stats[j["estimator"]]["wonAmount"] += j["price"]

estimators = []
for nm, s in est_stats.items():
    total_dec = s["wonCount"] + s["lostCount"]
    hit = round((s["wonCount"] / total_dec) * 100, 1) if total_dec else None
    estimators.append({"name": nm, **{k: money_round(v) if "Amount" in k else v for k, v in s.items()}, "hitRatePct": hit})
estimators.sort(key=lambda x: x["wonAmount"], reverse=True)

# ---------- Weekly time series (13 weeks back) ----------
weekly = defaultdict(lambda: {"bid": 0, "won": 0, "wonLY": 0})
for j in jobs:
    if j["date_estimate"]:
        ws = week_start(j["date_estimate"])
        if ts_start_week <= ws <= week_start(today):
            weekly[ws.isoformat()]["bid"] += j["price"]
    if j["status"] == WON and j.get("date_signed"):
        ws = week_start(j["date_signed"])
        if ts_start_week <= ws <= week_start(today):
            weekly[ws.isoformat()]["won"] += j["price"]
        # YoY ghost
        try:
            ws_shifted = date(ws.year + 1, ws.month, ws.day)
            ws_shifted = week_start(ws_shifted)
            if ts_start_week <= ws_shifted <= week_start(today):
                weekly[ws_shifted.isoformat()]["wonLY"] += j["price"]
        except ValueError:
            pass

weekly_series = []
cursor = ts_start_week
while cursor <= week_start(today):
    w = weekly.get(cursor.isoformat(), {"bid": 0, "won": 0, "wonLY": 0})
    weekly_series.append({"weekStart": cursor.isoformat(),
                          "bid": money_round(w["bid"]),
                          "won": money_round(w["won"]),
                          "wonLY": money_round(w["wonLY"])})
    cursor += timedelta(weeks=1)

# ---------- Top customers (uses Date Signed) ----------
cust = defaultdict(float)
for j in jobs:
    if j["status"] == WON and signed_in(j, mtd_start, today):
        cust[j["company"]] += j["price"]
top_customers = [{"name": n, "amount": money_round(a)}
                 for n, a in sorted(cust.items(), key=lambda x: -x[1])[:10]]

# ---------- Aging open bids ----------
aging = sorted(
    [{"jobId": j["job_id"], "jobNum": j["job_num"], "company": j["company"],
      "amount": money_round(j["price"]),
      "ageDays": (today - j["date_estimate"]).days}
     for j in est_open if j["date_estimate"] and (today - j["date_estimate"]).days >= 14],
    key=lambda x: -x["ageDays"]
)[:15]

# ---------- Lead pipeline ----------
lead_pipeline = None
if lp_raw:
    tier_counts = Counter()
    tier_value = defaultdict(float)
    outreach_counts = Counter()
    for r in lp_raw.get("rows", []):
        tier = cell_value(r, LP["LEAD_TIER"]) or "Unranked"
        val = to_number(cell_value(r, LP["PROJECT_VALUE"])) or 0
        out = cell_value(r, LP["OUTREACH_STATUS"]) or "Unset"
        tier_counts[tier] += 1
        tier_value[tier] += val
        outreach_counts[out] += 1
    lead_pipeline = {
        "tierCounts": dict(tier_counts),
        "tierValue": {k: money_round(v) for k, v in tier_value.items()},
        "outreachCounts": dict(outreach_counts),
        "totalLeads": sum(tier_counts.values()),
        "totalValue": money_round(sum(tier_value.values())),
    }

# ---------- Assemble ----------
out = {
    "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "source": "Cassidy Smartsheet — Completed_JobCostingSheets + Operations Schedule + Sales Leads Pipeline",
    "asOf": {
        "today": today.isoformat(),
        "mtdStart": mtd_start.isoformat(),
        "priorMtdStart": prior_mtd_start.isoformat(),
        "priorMtdEnd": prior_mtd_end.isoformat(),
    },
    "kpis": {
        "hitRate90d": {"wonCount": len(won_90), "lostCount": len(lost_90), "ratePct": hr_pct},
        "soldMTD": {"amount": money_round(sold_mtd), "count": sold_mtd_count,
                    "priorMonth": money_round(sold_prior),
                    "yoySameMonth": money_round(sold_yoy) if sold_yoy is not None else None,
                    "dateSignedPopulated": DATE_SIGNED_POPULATED,
                    "note": ("by Date Signed" if DATE_SIGNED_POPULATED
                             else "by Date of Estimate — Date Signed not populated")},
        "bidMTD": {"amount": money_round(bid_mtd), "count": bid_mtd_count, "priorMonth": money_round(bid_prior)},
        "laborMonthBacklog": {"months": labor_months, "totalCrewDays": int(total_crew_days_open),
                              "capacityCrewDaysPerMonth": CAPACITY_CREW_DAYS_PER_MONTH},
        "avgPerSY": {"last30": avg_psy_recent, "prior30": avg_psy_prior, "deltaPct": delta_pct,
                     "sampleSizeRecent": len(recent_psy), "sampleSizePrior": len(prior_psy)},
    },
    "pipeline": {"stages": pipeline_stages},
    "estimators": estimators,
    "weeklySeries": weekly_series,
    "topCustomers": top_customers,
    "agingOpenBids": aging,
    "leadPipeline": lead_pipeline,
}

json.dump(out, sys.stdout, indent=2, ensure_ascii=False, default=str)
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         