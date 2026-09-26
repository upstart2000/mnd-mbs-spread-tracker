# MND MBS Spread Tracker

Tracks the daily par-coupon spread between 30-year UMBS TBAs and 5yr/10yr US
Treasuries, used to estimate mortgage REIT book value moves intra-quarter.

Prices come from **Mortgage News Daily** (`/mbs/chartdata` endpoint) instead
of FINRA: the UMBS 5.5 / 6.0 / 6.5 daily closes are stored every trading day,
the par coupon is interpolated between the two coupons straddling par (no
adjustment - straight price interpolation), and spreads are computed against
the 5yr/10yr Treasury par yields.

## How it works

- **`mnd_prices.py`** - POSTs to MND's `/mbs/chartdata` endpoint
  (`productKey=FNMA55` / `FNMA60` / `FNMA65`), parses the daily OHLC history
  (~10 years back) and finds the latest trading date with >= 2 coupon closes.
- **`par_coupon.py`** - linear interpolation between the coupon just below
  par (price <= 100) and the one just above (price > 100); extrapolates from
  the two nearest coupons if all three sit on the same side of par.
- **`treasury_rates.py`** - fetches 5yr/10yr UST par yields (Treasury.gov
  primary, Yahoo Finance `^FVX`/`^TNX` fallback).
- **`db.py`** - SQLite storage (`mnd_spreads.db`), one row per trading day:
  the three UMBS closes, the par bracket + par coupon, Treasury rates,
  spreads (bps), and quarter-to-date (QTD) change vs. the prior quarter's
  last close.
- **`pipeline.py`** - shared glue tying the above into one daily record.
- **`nightly_job.py`** - pulls the latest MND closes, fetches rates,
  computes spreads/QTD, upserts into SQLite. Runs on a schedule via
  `.github/workflows/nightly.yml` (GitHub Actions, ~9PM ET + 9:45PM ET retry),
  which commits the updated `mnd_spreads.db` back to this repo.
- **`backfill.py`** - one-time historical backfill straight from MND's chart
  history (no monthly archives needed - each coupon's full history comes back
  in a single request).
- **`streamlit_app.py`** - dashboard: QTD metric cards, Prior/Today/Delta
  daily table, and a historical spread chart.

## Running locally

```
pip install -r requirements.txt
python backfill.py        # one-time, populates history (~10 years)
python nightly_job.py     # fetch today's row
streamlit run streamlit_app.py
```

Backfill options: `--start YYYY-MM-DD` / `--end YYYY-MM-DD` to limit the
range, `--no-skip-existing` to reprocess days already in the db.

## Spread convention

`spread_5yr = (par_coupon - ust_5yr) * 100` (bps), same for `spread_10yr`;
`spread_avg` is the mean of the two. QTD change fields compare today's value
to the most recent row before the current quarter's start, also in bps.

## Data notes

- MND prices are indicative TBA prices (not FINRA settlement prices) and
  post ~6PM ET; the nightly job runs ~9PM ET.
- Treasury.gov posts the day's par yields by early evening; if neither it nor
  the Yahoo fallback has same-day data, the row is written with NULL rates
  (`ust_stale=1`) and a later run fills it in.
