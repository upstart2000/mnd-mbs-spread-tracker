"""
One-off repair/cleaning for mnd_spreads.db.

Pass 1: recompute every row's par coupon with the hardened par_coupon logic
        (flat/inverted pairs and out-of-band extrapolations yield None instead
        of garbage), rebuild spreads, and delete weekend rows (MND feed noise
        - MBS doesn't trade weekends).
Pass 2: iteratively null par-spike days (db.is_par_spike) - a par that jumps
        >1 coupon point and fully reverts the next day is a bad coupon mark,
        not a market move. Nulling the most severe spike first and repeating
        keeps adjacent-day artifacts from masking each other.
Pass 3: recompute QTD fields for all rows (after all nulling, so deltas vs a
        nulled quarter-end reference correctly come out NULL).

No network access needed: prices and treasury yields are re-read from the
rows already stored.

Usage: python3 repair_db.py [--db PATH]
"""
import argparse
import logging
import sqlite3
import sys
from datetime import date

sys.path.insert(0, ".")

import db
from db import compute_spreads, compute_qtd_fields, upsert_day, par_anomaly_runs, null_par
from par_coupon import compute_par_coupon

logger = logging.getLogger("repair")


def _all_dates(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT mbs_date FROM daily_spreads ORDER BY mbs_date")]
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    db_path = args.db

    # ---- Pass 1: recompute par/spreads, drop weekends ----
    dates = _all_dates(db_path)
    deleted = nulled = repaired = 0
    for ds in dates:
        d = date.fromisoformat(ds)
        if d.weekday() >= 5:
            with db._connect(db_path) as conn:
                conn.execute("DELETE FROM daily_spreads WHERE mbs_date = ?", (ds,))
            deleted += 1
            continue

        row = db.get_day(d, db_path=db_path)
        prices = {5.5: row["price_55"], 6.0: row["price_60"], 6.5: row["price_65"]}

        record = {
            "mbs_date": d,
            "price_55": row["price_55"],
            "price_60": row["price_60"],
            "price_65": row["price_65"],
            "coupon_low": None, "price_low": None,
            "coupon_high": None, "price_high": None,
            "par_coupon": None,
            "ust_5yr": row["ust_5yr"],
            "ust_10yr": row["ust_10yr"],
            "ust_source": row["ust_source"],
            "ust_stale": row["ust_stale"],
            "coupon_curve": row["coupon_curve"],
        }

        bracket = compute_par_coupon(prices)
        if bracket is not None:
            par, (c_low, p_low), (c_high, p_high) = bracket
            record.update(par_coupon=par, coupon_low=c_low, price_low=p_low,
                          coupon_high=c_high, price_high=p_high)
        else:
            nulled += 1

        s5, s10, savg = compute_spreads(record["par_coupon"], record["ust_5yr"], record["ust_10yr"])
        record.update(spread_5yr=s5, spread_10yr=s10, spread_avg=savg)
        # QTD recomputed in pass 3; write without it for now
        upsert_day(record, db_path=db_path)
        repaired += 1

    logger.info("Pass 1: %d rows repaired, %d nulled (par not computable), %d weekend rows deleted",
                repaired, nulled, deleted)

    # ---- Pass 2: iterative par-anomaly nulling ----
    # A run of 1-3 trading days whose par deviates >1pt from the nearest good
    # par on BOTH sides (fully reverts) is bad MND marks, not a market move.
    # Null the most severe run first and rescan: nulling changes neighbors'
    # context, so adjacent artifacts are handled one at a time.
    anomalies_nulled = 0
    while True:
        runs = db.par_anomaly_runs(db_path=db_path)
        if not runs:
            break
        sev, dates = runs[0]
        for d in dates:
            null_par(d, db_path=db_path)
        anomalies_nulled += len(dates)
        logger.info("Nulled par anomaly %s (severity %.2f)",
                    dates[0] if len(dates) == 1 else f"{dates[0]}..{dates[-1]}", sev)

    logger.info("Pass 2: %d par-anomaly days nulled", anomalies_nulled)

    # ---- Pass 3: recompute QTD fields in date order ----
    for ds in _all_dates(db_path):
        row = db.get_day(date.fromisoformat(ds), db_path=db_path)
        record = dict(row)
        record["mbs_date"] = date.fromisoformat(ds)
        record.update(compute_qtd_fields(record, db_path=db_path))
        upsert_day(record, db_path=db_path)

    logger.info("Pass 3: QTD fields recomputed for all rows")


if __name__ == "__main__":
    main()
