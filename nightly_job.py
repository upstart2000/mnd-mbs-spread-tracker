"""
Nightly job: pull the latest 30yr UMBS closes (5.5 / 6.0 / 6.5) from Mortgage
News Daily, fetch same-day Treasury rates, compute the par coupon and
spreads, and upsert one row into SQLite.

Runs on GitHub Actions (see .github/workflows/nightly.yml), scheduled around
9PM ET with a 9:45PM ET retry (MND's daily close posts ~6PM ET, Treasury.gov
usually by early evening). The job is idempotent: it checks for an
already-complete row for the latest available price date before doing any
work, so retries and off-season duplicate firings exit fast.

Treasury rate staleness: get_treasury_rates() refuses to substitute a prior
day's rate - it returns {'stale': True, ...} if neither treasury.gov nor the
Yahoo fallback has same-day data. A stale row is written with NULL rates and
ust_stale=1, and is NOT treated as complete, so a later run retries it.
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db
from mnd_prices import latest_aligned_prices
from pipeline import build_daily_record

EASTERN = ZoneInfo("America/New_York")

logger = logging.getLogger("nightly_job")


def expected_trade_date(now=None):
    """
    The trading day whose data we expect this evening: today in ET, rolled
    back over weekends. Does NOT account for market holidays - a holiday just
    means MND posts no new close, in which case the latest aligned price date
    stays on the prior trading day and the job becomes a no-op rewrite.
    """
    if now is None:
        now = datetime.now(EASTERN)
    d = now.date()
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= timedelta(days=1)
    return d


def _row_is_complete(row):
    """A row only counts as 'done' if it has a par coupon and a fresh (non-stale) treasury rate."""
    return row is not None and not row["ust_stale"] and row["par_coupon"] is not None


def run_nightly_job(db_path=db.DEFAULT_DB_PATH, expected_date=None, allow_yahoo_fallback=True, force=False):
    expected_date = expected_date or expected_trade_date()
    logger.info("Nightly job starting, expected trade date %s", expected_date)

    db.init_db(db_path)

    trade_date, prices = latest_aligned_prices()
    if trade_date is None:
        raise RuntimeError("Could not fetch MND prices for enough coupons (need >= 2 of 5.5/6.0/6.5)")

    if trade_date < expected_date:
        logger.warning(
            "Latest MND price date %s is before expected trade date %s - today's close "
            "may not have posted yet; writing the latest available date. A later run will "
            "pick up %s once it posts.",
            trade_date, expected_date, expected_date,
        )

    latest = db.get_latest(1, db_path=db_path)
    if latest and (trade_date - datetime.fromisoformat(latest[0]["mbs_date"]).date()).days > db.MAX_EXPECTED_GAP_DAYS:
        logger.warning(
            "Data gap: most recent row before this run is %s, wider than a normal "
            "weekend/holiday before %s. Likely missing trading day(s); may need a backfill.",
            latest[0]["mbs_date"], trade_date,
        )

    if not force:
        existing = db.get_day(trade_date, db_path=db_path)
        if _row_is_complete(existing):
            logger.info(
                "Row for %s already complete (par_coupon=%s, stale=%s) - skipping. Pass --force to reprocess.",
                trade_date, existing["par_coupon"], existing["ust_stale"],
            )
            return existing

    record = build_daily_record(prices, trade_date, allow_yahoo_fallback=allow_yahoo_fallback)
    if record is None:
        # weekend (or otherwise non-trading) date - nothing to write
        logger.info("Skipping %s: non-trading day.", trade_date)
        return None

    # Anomaly guard: a run of 1-3 days whose par deviates >1pt from the nearest
    # good par on both sides (fully reverts) is bad MND marks, not a market
    # move - null it before computing QTD so neither the chart nor the deltas
    # carry the artifact. extra_row lends today's not-yet-written par as
    # right-side context so runs ending yesterday can be judged; a run ending
    # today can't be judged until tomorrow (no right side yet). Idempotent:
    # already-nulled runs never reappear in the scan.
    today_str = trade_date.isoformat()
    for sev, dates in db.par_anomaly_runs(
        db_path=db_path, extra_row=(today_str, record["par_coupon"])
    ):
        for d in dates:
            if d == today_str:
                continue
            logger.warning("Nulling par anomaly %s (severity %.2f) as bad marks.", d, sev)
            db.null_par(d, db_path=db_path)

    record.update(db.compute_qtd_fields(record, db_path=db_path))

    if record["ust_stale"]:
        logger.warning(
            "Treasury rate STALE for %s: no same-day rate from treasury.gov or Yahoo fallback. "
            "ust_5yr/ust_10yr/spreads written as NULL - re-run this job later to fill them in.",
            trade_date,
        )
    if record["par_coupon"] is None:
        logger.warning("Par coupon not computable for %s: fewer than two usable coupon prices, "
                       "or the coupon curve was flat/inverted/unplausible that day.", trade_date)

    db.upsert_day(record, db_path=db_path)

    logger.info(
        "Wrote %s: UMBS 5.5/6.0/6.5=%s/%s/%s par_coupon=%s ust_5yr=%s ust_10yr=%s "
        "spread_avg=%s stale=%s qtd_ref=%s qtd_chg_spread_avg=%s",
        trade_date, record["price_55"], record["price_60"], record["price_65"],
        record["par_coupon"], record["ust_5yr"], record["ust_10yr"],
        record["spread_avg"], record["ust_stale"],
        record["qtd_ref_date"], record["qtd_chg_spread_avg"],
    )
    return record


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("nightly_job.log"),
        ],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nightly MND/Treasury spread ingestion job")
    parser.add_argument("--date", help="override expected trade date (YYYY-MM-DD); default: today in America/New_York")
    parser.add_argument("--db-path", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--no-yahoo-fallback", action="store_true")
    parser.add_argument("--force", action="store_true", help="reprocess even if a complete row already exists")
    args = parser.parse_args()

    _setup_logging()

    expected = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None

    try:
        run_nightly_job(
            db_path=args.db_path,
            expected_date=expected,
            allow_yahoo_fallback=not args.no_yahoo_fallback,
            force=args.force,
        )
    except Exception:
        logger.exception("Nightly job failed")
        sys.exit(1)
