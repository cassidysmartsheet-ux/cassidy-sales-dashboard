# Cassidy Sales Dashboard

Live KPI dashboard for Dan D'Ambrosio, pulled from Smartsheet every 15 minutes.

**Live URL:** https://cassidysmartsheet-ux.github.io/cassidy-sales-dashboard/

## What it shows
- **Hit Rate (90d)** — bid won vs lost from `Completed_JobCostingSheets.New Status`
- **Sold MTD** — currently uses `Date of Estimate` as won-date proxy (Date Signed is not populated)
- **Bid MTD** — total estimated value with prior-month + delta
- **Labor-Month Backlog** — open won crew-days / 132 crew-days-per-month capacity
- **Avg $/SY** — last 30 vs prior 30, filters jobs with SY < 50
- Pipeline funnel, Estimator leaderboard, weekly bid/won chart, top customers, aging open bids, Lead Pipeline tier-2 panel

## How it refreshes
GitHub Action runs every 15 min:
1. Curls Smartsheet API (read-only `CalendarGit` token)
2. Runs `.github/scripts/transform.py` → writes `data.json`
3. Commits + pushes (with 3-attempt rebase retry)

The page polls `data.json` every 60s.

## Known data quality issues (Cassidy to address)
1. `Date Signed` is not populated on Operations Schedule OR Setup Sheet — without it "Sold MTD" approximates from `Date of Estimate`
2. No "Bid Lost" rows in the last 90 days — hit rate shows 100% which is unrealistic
3. Some Job Costing rows have `Project Square Yards` = 0/1, distorting $/SY (filtered with SY ≥ 50)

## Read-only — do not edit
This repo and its workflow have read-only Smartsheet credentials.
