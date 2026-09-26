"""
SQLite storage for daily MND 30yr UMBS / Treasury spread data.

One row per trading day (keyed on the MBS price date), holding that day's
UMBS 5.5 / 6.0 / 6.5 closes from Mortgage News Daily, the interpolated par
coupon, the UST 5yr/10yr par yields, and the spreads:

    spread_5yr  = (par_coupon - ust_5yr)  * 100   # bps
    spread_10yr = (par_coupon - ust_10yr) * 100   # bps
    spread_avg  = (spread_5yr + spread_10yr) / 2

Quarter-to-date (QTD) change tracking: each row also stores its change since
the most recent row dated before the first day of its quarter (i.e. the prior
quarter's last available close) - used to estimate rate-driven book value
moves intra-quarter, before official financials post. All QTD deltas are in
bps (including the UST yield changes, for unit consistency with the spreads).
A row in the first quarter a dataset covers has no prior-quarter baseline
available, so its QTD fields are NULL - not a bug, just no reference point.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone

DEFAULT_DB_PATH = "mnd_spreads.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_spreads (
    mbs_date        TEXT PRIMARY KEY,   -- ISO date (YYYY-MM-DD), MND price date

    price_55        REAL,               -- UMBS 5.5 daily close
    price_60        REAL,               -- UMBS 6.0 daily close
    price_65        REAL,               -- UMBS 6.5 daily close

    coupon_low      REAL,               -- par bracket: coupon just below par
    price_low       REAL,
    coupon_high     REAL,               -- par bracket: coupon just above par
    price_high      REAL,
    par_coupon      REAL,               -- interpolated coupon priced at 100

    ust_5yr         REAL,
    ust_10yr        REAL,
    ust_source      TEXT,               -- 'treasury.gov' | 'yahoo_fallback' | NULL
    ust_stale       INTEGER NOT NULL DEFAULT 0,  -- 1 if no same-day treasury rate was available

    spread_5yr      REAL,               -- bps
    spread_10yr     REAL,               -- bps
    spread_avg      REAL,               -- bps

    coupon_curve    TEXT,               -- JSON {coupon: price}, that day's three closes

    qtd_ref_date    TEXT,               -- date of the prior-quarter reference row used below (NULL if none)
    qtd_chg_ust_5yr     REAL,           -- bps, vs qtd_ref_date
    qtd_chg_ust_10yr    REAL,           -- bps, vs qtd_ref_date
    qtd_chg_spread_5yr  REAL,           -- bps, vs qtd_ref_date
    qtd_chg_spread_10yr REAL,           -- bps, vs qtd_ref_date
    qtd_chg_spread_avg  REAL,           -- bps, vs qtd_ref_date

    ingested_at     TEXT NOT NULL       -- UTC timestamp this row was last written
);
"""


@contextmanager
def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path=DEFAULT_DB_PATH):
    with _connect(db_path) as conn:
        conn.execute(SCHEMA)


def parse_coupon_curve(curve_json):
    """
    Deserializes a coupon_curve cell (JSON object with string coupon keys, as
    written by pipeline.py) back into a {float coupon: float price} dict.
    Returns {} for None/empty/NaN input.
    """
    if not curve_json or isinstance(curve_json, float):
        return {}
    return {float(c): p for c, p in json.loads(curve_json).items()}


def compute_spreads(par_coupon, ust_5yr, ust_10yr):
    """
    Returns (spread_5yr, spread_10yr, spread_avg) in bps, or (None, None, None)
    if any required input is missing.
    """
    if par_coupon is None or ust_5yr is None or ust_10yr is None:
        return None, None, None
    spread_5yr = round((par_coupon - ust_5yr) * 100, 2)
    spread_10yr = round((par_coupon - ust_10yr) * 100, 2)
    spread_avg = round((spread_5yr + spread_10yr) / 2, 2)
    return spread_5yr, spread_10yr, spread_avg


def get_quarter_start(d):
    """First calendar day of the quarter containing date d."""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    quarter_start_month = 3 * ((d.month - 1) // 3) + 1
    return date(d.year, quarter_start_month, 1)


def get_qtd_reference_row(mbs_date, db_path=DEFAULT_DB_PATH):
    """
    The most recent row dated strictly before mbs_date's quarter start -
    i.e. the prior quarter's last available close. Returns None if no such
    row exists in the db (e.g. the very first quarter this dataset covers).
    """
    quarter_start = get_quarter_start(mbs_date)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM daily_spreads WHERE mbs_date < ? ORDER BY mbs_date DESC LIMIT 1",
            (quarter_start.isoformat(),),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_quarter_end_rows(latest_date, db_path=DEFAULT_DB_PATH):
    """
    Resolves the current and prior quarter-end snapshot rows relative to
    latest_date, purely from what's actually in the db (not a hardcoded
    calendar date, so this rolls forward automatically each quarter):

    - current_quarter_end: the last stored row before latest_date's quarter
      started - i.e. the most recently completed quarter's close (same
      row get_qtd_reference_row(latest_date) would return).
    - prior_quarter_end: the last stored row before *that* row's quarter
      started - one quarter further back.

    Either (or both) may be None if the dataset doesn't go back far enough.
    """
    current_quarter_end = get_qtd_reference_row(latest_date, db_path=db_path)
    prior_quarter_end = (
        get_qtd_reference_row(current_quarter_end["mbs_date"], db_path=db_path)
        if current_quarter_end is not None
        else None
    )
    return current_quarter_end, prior_quarter_end


def compute_qtd_fields(record, db_path=DEFAULT_DB_PATH):
    """
    Given a record dict (as built by pipeline.build_daily_record, pre-upsert),
    returns the qtd_ref_date / qtd_chg_* fields to merge into it. All deltas
    are today's value minus the reference row's value, in bps. Any field is
    None if either side of the comparison is missing (no reference row, or
    the metric itself wasn't computable that day).
    """
    empty = {
        "qtd_ref_date": None,
        "qtd_chg_ust_5yr": None,
        "qtd_chg_ust_10yr": None,
        "qtd_chg_spread_5yr": None,
        "qtd_chg_spread_10yr": None,
        "qtd_chg_spread_avg": None,
    }

    ref = get_qtd_reference_row(record["mbs_date"], db_path=db_path)
    if ref is None:
        return empty

    def _delta_bps(today_val, ref_val, already_bps):
        if today_val is None or ref_val is None:
            return None
        diff = (today_val - ref_val) if already_bps else (today_val - ref_val) * 100
        return round(diff, 2)

    return {
        "qtd_ref_date": ref["mbs_date"],
        "qtd_chg_ust_5yr": _delta_bps(record.get("ust_5yr"), ref.get("ust_5yr"), already_bps=False),
        "qtd_chg_ust_10yr": _delta_bps(record.get("ust_10yr"), ref.get("ust_10yr"), already_bps=False),
        "qtd_chg_spread_5yr": _delta_bps(record.get("spread_5yr"), ref.get("spread_5yr"), already_bps=True),
        "qtd_chg_spread_10yr": _delta_bps(record.get("spread_10yr"), ref.get("spread_10yr"), already_bps=True),
        "qtd_chg_spread_avg": _delta_bps(record.get("spread_avg"), ref.get("spread_avg"), already_bps=True),
    }


_COLUMNS = [
    "mbs_date",
    "price_55", "price_60", "price_65",
    "coupon_low", "price_low", "coupon_high", "price_high", "par_coupon",
    "ust_5yr", "ust_10yr", "ust_source", "ust_stale",
    "spread_5yr", "spread_10yr", "spread_avg",
    "coupon_curve",
    "qtd_ref_date", "qtd_chg_ust_5yr", "qtd_chg_ust_10yr",
    "qtd_chg_spread_5yr", "qtd_chg_spread_10yr", "qtd_chg_spread_avg",
    "ingested_at",
]


def upsert_day(record, db_path=DEFAULT_DB_PATH):
    """
    record: dict with keys matching the daily_spreads columns (mbs_date required;
    all others optional / may be None). ingested_at is stamped automatically.
    Insert-or-replace keyed on mbs_date, so re-running the nightly job or backfill
    for the same day is idempotent.
    """
    mbs_date = record["mbs_date"]
    if isinstance(mbs_date, date):
        mbs_date = mbs_date.isoformat()

    row = {col: record.get(col) for col in _COLUMNS}
    row["mbs_date"] = mbs_date
    row["ust_stale"] = int(bool(record.get("ust_stale", False)))
    row["ingested_at"] = datetime.now(timezone.utc).isoformat()

    placeholders = ", ".join(f":{c}" for c in _COLUMNS)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "mbs_date")

    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO daily_spreads ({", ".join(_COLUMNS)})
            VALUES ({placeholders})
            ON CONFLICT(mbs_date) DO UPDATE SET {update_clause}
            """,
            row,
        )


def get_day(mbs_date, db_path=DEFAULT_DB_PATH):
    if isinstance(mbs_date, date):
        mbs_date = mbs_date.isoformat()
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM daily_spreads WHERE mbs_date = ?", (mbs_date,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_latest(n=2, db_path=DEFAULT_DB_PATH):
    """Most recent n rows, ordered ascending by date (oldest first)."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM daily_spreads ORDER BY mbs_date DESC LIMIT ?", (n,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        return list(reversed(rows))


MAX_EXPECTED_GAP_DAYS = 4  # a normal weekend is 3; a Monday/Friday holiday + weekend is 4


def find_date_gaps(db_path=DEFAULT_DB_PATH, max_expected_gap_days=MAX_EXPECTED_GAP_DAYS):
    """
    Scans all stored dates in order and returns a list of
    (date_before, date_after, calendar_gap_days) for every consecutive pair
    whose gap exceeds max_expected_gap_days - i.e. wider than an ordinary
    weekend or a single holiday long-weekend, and therefore likely missing
    trading day(s) rather than an expected non-trading stretch.
    """
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT mbs_date FROM daily_spreads ORDER BY mbs_date ASC")
        dates = [date.fromisoformat(r["mbs_date"]) for r in cur.fetchall()]

    gaps = []
    for prev_date, next_date in zip(dates, dates[1:]):
        gap_days = (next_date - prev_date).days
        if gap_days > max_expected_gap_days:
            gaps.append((prev_date, next_date, gap_days))
    return gaps


def get_all(db_path=DEFAULT_DB_PATH):
    """All rows ordered ascending by date - primarily for the historical chart."""
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM daily_spreads ORDER BY mbs_date ASC")
        return [dict(r) for r in cur.fetchall()]


if __name__ == "__main__":
    # Smoke test: init, upsert a row, read it back.
    init_db(DEFAULT_DB_PATH)
    s5, s10, savg = compute_spreads(6.2427, 3.90, 4.20)
    upsert_day(
        {
            "mbs_date": date(2026, 9, 25),
            "price_55": 96.3047,
            "price_60": 98.8448,
            "price_65": 101.2344,
            "coupon_low": 6.0,
            "price_low": 98.8448,
            "coupon_high": 6.5,
            "price_high": 101.2344,
            "par_coupon": 6.2427,
            "ust_5yr": 3.90,
            "ust_10yr": 4.20,
            "ust_source": "treasury.gov",
            "ust_stale": False,
            "spread_5yr": s5,
            "spread_10yr": s10,
            "spread_avg": savg,
            "coupon_curve": '{"5.5": 96.3047, "6.0": 98.8448, "6.5": 101.2344}',
        }
    )
    print(get_day(date(2026, 9, 25)))
    print(get_latest(5))
