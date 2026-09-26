"""
One-time historical backfill of daily_spreads from Mortgage News Daily.

MND's /mbs/chartdata endpoint returns ~10 years of daily closes per coupon
(one POST per coupon), so the backfill is: fetch the three histories once,
align them by trading date (a date needs >= 2 coupon closes to interpolate
a par coupon), then upsert one row per date in ascending chronological order
(ascending matters - QTD reference rows are looked up against what's already
in the db).

Treasury rates come from the same treasury_rates.get_treasury_rates() the
nightly job uses; Treasury.gov's monthly XML feed covers the full history,
so even 2016 dates resolve without the Yahoo fallback.
"""
import argparse
import logging
import sys
from datetime import date

import db
from mnd_prices import COUPON_PRODUCT_KEYS, daily_closes, fetch_chartdata
from pipeline import build_daily_record
from treasury_rates import prefetch_months

logger = logging.getLogger("backfill")


def fetch_histories():
    """Returns {coupon: {date: close}} for each of the three coupons."""
    histories = {}
    for coupon, product_key in COUPON_PRODUCT_KEYS.items():
        logger.info("Fetching full history for %s (UMBS %s)...", product_key, coupon)
        histories[coupon] = daily_closes(fetch_chartdata(product_key))
        logger.info("  %d daily closes", len(histories[coupon]))
    return histories


def run_backfill(db_path=db.DEFAULT_DB_PATH, start=None, end=None, skip_existing=True):
    db.init_db(db_path)
    histories = fetch_histories()

    all_dates = sorted({d for closes in histories.values() for d in closes})
    if start:
        all_dates = [d for d in all_dates if d >= start]
    if end:
        all_dates = [d for d in all_dates if d <= end]
    logger.info("Backfilling %d trading days (%s to %s)", len(all_dates),
                all_dates[0] if all_dates else None, all_dates[-1] if all_dates else None)

    # Treasury.gov serves one XML per month and can be very slow per request;
    # pre-download every spanned month concurrently so the per-day loop below
    # never blocks on the network.
    if all_dates:
        months = sorted({d.strftime("%Y%m") for d in all_dates})
        logger.info("Pre-fetching %d Treasury.gov month feeds...", len(months))
        ok, failed_months = prefetch_months(months)
        logger.info("Treasury pre-fetch: %d ok, %d failed %s", len(ok), len(failed_months), failed_months)

    written = skipped = failed = thin = 0
    today = date.today()
    for d in all_dates:
        prices = {c: closes[d] for c, closes in histories.items() if d in closes}
        if len(prices) < 2:
            thin += 1
            continue
        if skip_existing and db.get_day(d, db_path=db_path) is not None:
            skipped += 1
            continue
        try:
            # The Yahoo fallback only matters for dates whose Treasury.gov
            # row may not have posted yet (the last few days). For older
            # dates Treasury.gov's monthly archive is complete, so skip the
            # per-date yfinance call - it just burns time on weekends and
            # holidays, where neither source has data anyway.
            allow_yahoo = (today - d).days <= 5
            record = build_daily_record(prices, d, allow_yahoo_fallback=allow_yahoo)
            if record is None:
                continue  # weekend / non-trading day
            record.update(db.compute_qtd_fields(record, db_path=db_path))
            db.upsert_day(record, db_path=db_path)
            written += 1
            if record["ust_stale"]:
                logger.warning("%s: treasury rate unavailable (no data from either source)", d)
            if record["par_coupon"] is None:
                logger.warning("%s: par coupon not computable", d)
        except Exception:
            logger.exception("%s: failed to process, skipping", d)
            failed += 1

    logger.info(
        "Backfill complete: %d written, %d skipped (already in db), %d failed, "
        "%d skipped (fewer than 2 coupon prices)",
        written, skipped, failed, thin,
    )
    return written, skipped, failed, thin


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("backfill.log")],
    )


def _parse_date(s):
    return date.fromisoformat(s) if s else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-time historical backfill from Mortgage News Daily")
    parser.add_argument("--db-path", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--start", help="only backfill dates >= YYYY-MM-DD")
    parser.add_argument("--end", help="only backfill dates <= YYYY-MM-DD")
    parser.add_argument("--no-skip-existing", action="store_true", help="re-process and overwrite days already in the db")
    args = parser.parse_args()

    _setup_logging()

    try:
        run_backfill(
            db_path=args.db_path,
            start=_parse_date(args.start),
            end=_parse_date(args.end),
            skip_existing=not args.no_skip_existing,
        )
    except Exception:
        logger.exception("Backfill failed")
        sys.exit(1)
