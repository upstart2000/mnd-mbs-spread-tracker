"""
Shared logic to turn one day's MND UMBS closes into a daily_spreads record
(db.py schema). Used by both the nightly job and the historical backfill so
the two stay consistent.

Given {coupon: price} for the 5.5 / 6.0 / 6.5 coupons:

- the par coupon is interpolated between the two coupons straddling par
  (par_coupon.py - straight price interpolation, no adjustment);
- the spreads vs the 5yr/10yr UST par yields (treasury_rates.py) are computed
  in bps by db.compute_spreads().

Fields that couldn't be computed (fewer than two usable coupon prices that
day, or no same-day treasury rate) are left as None / ust_stale=True rather
than guessed at.
"""
import json

from par_coupon import compute_par_coupon
from treasury_rates import get_treasury_rates
from db import compute_spreads


def _serialize_curve(curve):
    """{coupon: price} -> JSON object with string keys (JSON has no float keys). None if empty/missing."""
    if not curve:
        return None
    return json.dumps({str(c): p for c, p in curve.items()})


def build_daily_record(prices_by_coupon, mbs_date, allow_yahoo_fallback=True):
    """
    prices_by_coupon: {coupon(float): close(float)} for this trading day.
    mbs_date: the trading date (a datetime.date).

    Returns a dict matching db.upsert_day()'s expected keys, or None for
    weekend dates: MBS doesn't trade on weekends, and MND's feed occasionally
    carries Sunday rows with partial/stale marks that aren't trading days.
    """
    if mbs_date.weekday() >= 5:
        return None

    record = {
        "mbs_date": mbs_date,
        "price_55": prices_by_coupon.get(5.5),
        "price_60": prices_by_coupon.get(6.0),
        "price_65": prices_by_coupon.get(6.5),
        "coupon_low": None,
        "price_low": None,
        "coupon_high": None,
        "price_high": None,
        "par_coupon": None,
    }

    bracket = compute_par_coupon(prices_by_coupon)
    if bracket is not None:
        par, (c_low, p_low), (c_high, p_high) = bracket
        record.update(
            par_coupon=par,
            coupon_low=c_low,
            price_low=p_low,
            coupon_high=c_high,
            price_high=p_high,
        )

    record["coupon_curve"] = _serialize_curve(
        {c: p for c, p in prices_by_coupon.items() if p is not None}
    )

    treasury = get_treasury_rates(mbs_date, allow_yahoo_fallback=allow_yahoo_fallback)
    record["ust_5yr"] = treasury.get("ust_5yr")
    record["ust_10yr"] = treasury.get("ust_10yr")
    record["ust_source"] = treasury.get("source")
    record["ust_stale"] = bool(treasury.get("stale", False))

    spread_5yr, spread_10yr, spread_avg = compute_spreads(
        record["par_coupon"], record["ust_5yr"], record["ust_10yr"]
    )
    record["spread_5yr"] = spread_5yr
    record["spread_10yr"] = spread_10yr
    record["spread_avg"] = spread_avg

    return record
