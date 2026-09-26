"""
Fetches 30-year UMBS TBA prices from Mortgage News Daily.

MND's product pages (e.g. https://www.mortgagenewsdaily.com/mbs/umbs/30/60)
load their price history from a POST endpoint:

    POST https://www.mortgagenewsdaily.com/mbs/chartdata
    form data: productKey=FNMA55 | FNMA60 | FNMA65

The response JSON carries a `prices` list of daily OHLC records (one per
trading day, going back to ~Sep 2016) plus a `product` block with the latest
indicative close. Each daily record's `priceDate` is an ISO timestamp with an
ET offset, so its calendar-date part is the trading date.

These are MND's indicative TBA prices - not FINRA settlement prices - but
they are published daily, free, and machine-readable, which is the point of
this tracker.
"""
import logging
from datetime import date, datetime

import requests

CHARTDATA_URL = "https://www.mortgagenewsdaily.com/mbs/chartdata"

# MND product keys for the three 30yr UMBS coupons shown on /mbs/umbs/30
COUPON_PRODUCT_KEYS = {
    5.5: "FNMA55",
    6.0: "FNMA60",
    6.5: "FNMA65",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

logger = logging.getLogger("mnd_prices")


def fetch_chartdata(product_key, timeout=30):
    """
    POSTs to MND's chartdata endpoint and returns the decoded JSON dict.
    Raises requests.RequestException on network/HTTP failure.
    """
    resp = requests.post(
        CHARTDATA_URL, data={"productKey": product_key}, headers=HEADERS, timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()


def _trading_date(price_date_str):
    """
    'priceDate' looks like '2016-09-26T12:00:00-04:00' (ET offset included),
    so the calendar-date part is the trading date in America/New_York.
    Returns None if the string doesn't parse.
    """
    try:
        return datetime.fromisoformat(price_date_str).date()
    except (ValueError, TypeError):
        return None


def daily_closes(chartdata):
    """
    Returns {date: close_price} for every daily record in a chartdata payload.
    Records without a usable value (hasValue false, missing/None close) are
    skipped rather than stored as zero - a missing day is a gap, not a price.
    If two records ever map to the same date, the last one wins.
    """
    closes = {}
    for rec in chartdata.get("prices", []):
        if not rec.get("hasValue", True):
            continue
        d = _trading_date(rec.get("priceDate", ""))
        px = rec.get("close")
        if d is None or not isinstance(px, (int, float)):
            continue
        closes[d] = float(px)
    return closes


def latest_aligned_prices(min_coupons=2):
    """
    Fetches all three coupon series and returns (trade_date, {coupon: price})
    for the most recent trading date on which at least `min_coupons` coupons
    have a daily close. Two coupons are the minimum needed to interpolate a
    par coupon, so that's the default.

    Returns (None, {}) if fewer than min_coupons series could be fetched at
    all (network failure etc.) - callers treat that as "no data today".
    """
    series = {}
    for coupon, product_key in COUPON_PRODUCT_KEYS.items():
        try:
            series[coupon] = daily_closes(fetch_chartdata(product_key))
        except Exception as e:
            logger.warning("failed to fetch %s (%s): %s", product_key, coupon, e)

    if len(series) < min_coupons:
        return None, {}

    all_dates = sorted({d for closes in series.values() for d in closes}, reverse=True)
    for d in all_dates:
        prices = {c: closes[d] for c, closes in series.items() if d in closes}
        if len(prices) >= min_coupons:
            return d, prices
    return None, {}


if __name__ == "__main__":
    # Smoke test: fetch all three series, print latest aligned date + prices.
    trade_date, prices = latest_aligned_prices()
    print("latest aligned date:", trade_date)
    for c in sorted(prices):
        print(f"  UMBS {c}: {prices[c]:.4f}")
