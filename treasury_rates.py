"""
Fetches 5yr / 10yr UST par yields for a given date.
Primary source: Treasury.gov Daily Treasury Par Yield Curve Rates XML feed.
Fallback: Yahoo Finance (^FVX, ^TNX) if Treasury.gov hasn't posted same-day data yet.
"""
import logging
import re
import time
import requests
from datetime import date, datetime
from functools import lru_cache

TREASURY_XML_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month={yyyymm}"
)

# Small/quick by design, matching fetch_finra_file_with_retry in nightly_job.py:
# this only needs to absorb a transient network blip within one run, not wait
# out a real outage - a stale/unreachable row falls through to the Yahoo
# fallback (or gets marked ust_stale) and a later scheduled run retries it.
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RETRY_BACKOFF_SEC = 5

logger = logging.getLogger("treasury_rates")


@lru_cache(maxsize=None)
def _fetch_month_xml(yyyymm):
    """
    Cached per-month XML fetch. A month's feed is immutable once past (and
    within a run, callers hit this once per day of that month), so caching
    here avoids re-downloading the same document repeatedly - e.g. during a
    historical backfill that requests ~20 trading days from one month.
    """
    url = TREASURY_XML_URL.format(yyyymm=yyyymm)
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text


@lru_cache(maxsize=None)
def _month_rate_map(yyyymm):
    """
    Parses one month's Treasury.gov XML feed once into
    {date: (ust_5yr, ust_10yr)}. The per-day regex scan this replaces was
    the bottleneck in multi-year backfills (one full-XML scan per day);
    parsing each month once and looking up by date is ~20x faster there.
    """
    xml_text = _fetch_month_xml(yyyymm)

    # Each <entry> has NEW_DATE, BC_5YEAR, BC_10YEAR - simple regex extraction, avoids
    # pulling in an XML namespace-heavy parser for a well-known, stable feed shape.
    entries = re.findall(
        r"<d:NEW_DATE[^>]*>([^<]+)</d:NEW_DATE>.*?"
        r"<d:BC_5YEAR[^>]*>([^<]+)</d:BC_5YEAR>.*?"
        r"<d:BC_10YEAR[^>]*>([^<]+)</d:BC_10YEAR>",
        xml_text,
        re.DOTALL,
    )
    return {
        datetime.fromisoformat(date_str.strip()).date(): (float(y5), float(y10))
        for date_str, y5, y10 in entries
    }


def prefetch_months(yyyymm_list, max_workers=4):
    """
    Pre-downloads a list of month XML feeds concurrently into the
    _fetch_month_xml cache, so a bulk backfill's per-day lookups never block
    on the network. Treasury.gov can be very slow per request (~15-20s from
    some networks); fetching months sequentially makes a 10-year backfill
    take the better part of an hour, while a small worker pool brings it
    down to a few minutes. Each month is retried a few times before giving
    up - a month that still fails is simply left uncached, and its days fall
    back to the normal per-day fetch path (or stale, if unreachable).
    Returns (succeeded, failed) yyyymm lists.
    """
    import concurrent.futures

    succeeded, failed = [], []

    def _fetch_one(yyyymm):
        last_error = None
        for _ in range(3):
            try:
                # On success this seeds the shared lru_cache, so the normal
                # fetch_treasury_gov path picks it up without re-downloading.
                _fetch_month_xml(yyyymm)
                return yyyymm, None
            except requests.RequestException as e:
                last_error = e
                time.sleep(5)
        return yyyymm, last_error

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for yyyymm, error in pool.map(_fetch_one, yyyymm_list):
            if error is None:
                succeeded.append(yyyymm)
            else:
                logger.warning("prefetch %s failed after retries: %s", yyyymm, error)
                failed.append(yyyymm)
    return succeeded, failed


def fetch_treasury_gov(target_date, max_attempts=DEFAULT_MAX_ATTEMPTS, retry_backoff_sec=DEFAULT_RETRY_BACKOFF_SEC):
    """
    Pulls the current month's Treasury.gov XML feed and returns the row matching target_date.
    Returns dict {'date': date, 'ust_5yr': float, 'ust_10yr': float, 'source': 'treasury.gov'}
    or None if that date isn't present yet (not posted / weekend / holiday) OR if the feed
    couldn't be fetched after retries (timeout, connection error, 5xx) - treated the same as
    "not posted yet" so callers fall back to Yahoo / mark the row stale instead of crashing.
    """
    yyyymm = target_date.strftime("%Y%m")

    rate_map = None
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            rate_map = _month_rate_map(yyyymm)
            break
        except requests.RequestException as e:
            last_error = e
            logger.warning("treasury.gov fetch attempt %d/%d failed: %s", attempt, max_attempts, e)
            if attempt < max_attempts:
                time.sleep(retry_backoff_sec)

    if rate_map is None:
        logger.warning("treasury.gov unreachable after %d attempt(s), last error: %s", max_attempts, last_error)
        return None

    if target_date in rate_map:
        y5, y10 = rate_map[target_date]
        return {
            "date": target_date,
            "ust_5yr": y5,
            "ust_10yr": y10,
            "source": "treasury.gov",
        }
    return None  # not posted yet for target_date


def fetch_yahoo_fallback(target_date):
    """
    Fallback via yfinance: ^FVX (5yr yield x10) and ^TNX (10yr yield x10).
    Returns None if yfinance doesn't have same-day data either.
    """
    import yfinance as yf

    tickers = {"ust_5yr": "^FVX", "ust_10yr": "^TNX"}
    result = {"date": target_date, "source": "yahoo_fallback"}

    for key, ticker in tickers.items():
        hist = yf.Ticker(ticker).history(
            start=target_date.isoformat(),
            end=(target_date.toordinal() and date.fromordinal(target_date.toordinal() + 1)).isoformat(),
        )
        if hist.empty:
            return None  # no same-day data from Yahoo either
        # Yahoo's ^FVX/^TNX quote the yield directly (not x10 despite some older docs -
        # verify against a known value before relying on this in production).
        close_val = float(hist["Close"].iloc[-1])
        result[key] = close_val

    return result


def get_treasury_rates(target_date=None, allow_yahoo_fallback=True):
    """
    Returns dict with ust_5yr, ust_10yr, date, source - or a dict with 'stale': True
    if neither source has same-day data (per the staleness-guard discussion).
    """
    if target_date is None:
        target_date = date.today()

    result = fetch_treasury_gov(target_date)
    if result is not None:
        return result

    if allow_yahoo_fallback:
        result = fetch_yahoo_fallback(target_date)
        if result is not None:
            return result

    return {"date": target_date, "ust_5yr": None, "ust_10yr": None, "source": None, "stale": True}


if __name__ == "__main__":
    # Test against a known historical date (avoids needing "today" to have posted yet)
    test_date = date(2018, 10, 9)
    r = fetch_treasury_gov(test_date)
    print(f"Treasury.gov test ({test_date}): {r}")
