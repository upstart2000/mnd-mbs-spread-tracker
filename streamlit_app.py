"""
MBS Spread Tracker (Mortgage News Daily edition) - Streamlit dashboard.

Reads directly from the repo's mnd_spreads.db (populated by nightly_job.py /
backfill.py). Designed to run unmodified on Streamlit Community Cloud: the db
path is relative to the repo root, which is the working directory both
locally and on Cloud.

Each row holds one trading day's UMBS 5.5 / 6.0 / 6.5 closes from Mortgage
News Daily, the interpolated par coupon, 5yr/10yr UST par yields, and the
par-coupon spreads vs Treasuries (bps).
"""
import os
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import db

# Fixed categorical order (validated for CVD separation - see dataviz skill palette).
COLOR_SPREAD_5YR = "#2a78d6"   # blue
COLOR_SPREAD_10YR = "#1baf7a"  # aqua
COLOR_SPREAD_AVG = "#eda100"   # yellow
COLOR_UP = "#006300"
COLOR_DOWN = "#e34948"
GRIDLINE = "#e1e0d9"

st.set_page_config(page_title="MBS Spread Tracker (MND)", page_icon="📈", layout="wide")


@st.cache_data(show_spinner=False)
def _load_dataframe(db_path, mtime):
    rows = db.get_all(db_path)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["mbs_date"] = pd.to_datetime(df["mbs_date"])
        df = df.sort_values("mbs_date")
    return df


def load_dataframe(db_path=db.DEFAULT_DB_PATH):
    mtime = os.path.getmtime(db_path) if os.path.exists(db_path) else None
    return _load_dataframe(db_path, mtime)


SPREAD_COLUMNS = ["Spread vs 5yr (bps)", "Spread vs 10yr (bps)", "Spread vs 5/10yr (bps)"]
COMPUTED_ROW_LABELS = {"Daily Change", "Prior Quarter Change", "QTD Change"}
TABLE_COLUMNS = (
    ["UST 5yr", "UST 10yr", "UMBS 5.5", "UMBS 6.0", "UMBS 6.5", "Par Coupon"]
    + SPREAD_COLUMNS
)


def _row_date(row):
    """row['mbs_date'] may be a pandas Timestamp (rows from the df) or an ISO string (rows straight from db.py)."""
    d = row["mbs_date"]
    return d.date() if hasattr(d, "date") else date.fromisoformat(d)


def _quarter_label(d):
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def snapshot_row_values(row):
    if row is None:
        return None
    return {
        "UST 5yr": row.get("ust_5yr"),
        "UST 10yr": row.get("ust_10yr"),
        "UMBS 5.5": row.get("price_55"),
        "UMBS 6.0": row.get("price_60"),
        "UMBS 6.5": row.get("price_65"),
        "Par Coupon": row.get("par_coupon"),
        "Spread vs 5yr (bps)": row.get("spread_5yr"),
        "Spread vs 10yr (bps)": row.get("spread_10yr"),
        "Spread vs 5/10yr (bps)": row.get("spread_avg"),
    }


def diff_row(values_a, values_b, columns, scope_columns=None):
    """values_a - values_b, restricted to scope_columns if given (else every column)."""
    result = {}
    for col in columns:
        if scope_columns is not None and col not in scope_columns:
            result[col] = None
            continue
        a, b = values_a.get(col), values_b.get(col)
        result[col] = (a - b) if (a is not None and b is not None) else None
    return result


def build_daily_table(today_row, prior_row, current_qe, prior_qe):
    """
    Rows, in order: prior quarter-end, current quarter-end, Prior Quarter
    Change (current QE - prior QE), the two most recent stored trading days
    (labeled with their actual dates), Daily Change (latest - prior), QTD
    Change (latest - current QE). Any row whose source data isn't available
    yet (e.g. no quarter-end baseline this early in the dataset) is omitted
    rather than shown empty.
    """
    columns = TABLE_COLUMNS

    today_vals = snapshot_row_values(today_row)
    prior_vals = snapshot_row_values(prior_row)
    current_qe_vals = snapshot_row_values(current_qe)
    prior_qe_vals = snapshot_row_values(prior_qe)

    rows = {}
    if prior_qe_vals is not None:
        rows[f"{_quarter_label(_row_date(prior_qe))} End ({_row_date(prior_qe)})"] = prior_qe_vals
    if current_qe_vals is not None:
        rows[f"{_quarter_label(_row_date(current_qe))} End ({_row_date(current_qe)})"] = current_qe_vals
    if current_qe_vals is not None and prior_qe_vals is not None:
        rows["Prior Quarter Change"] = diff_row(current_qe_vals, prior_qe_vals, columns)
    if prior_vals is not None:
        rows[_row_date(prior_row).isoformat()] = prior_vals
    rows[_row_date(today_row).isoformat()] = today_vals
    if prior_vals is not None:
        rows["Daily Change"] = diff_row(today_vals, prior_vals, columns)
    if current_qe_vals is not None:
        rows["QTD Change"] = diff_row(today_vals, current_qe_vals, columns)

    return pd.DataFrame.from_dict(rows, orient="index", columns=columns)


def style_daily_table(table_df):
    def highlight_computed_rows(row):
        if row.name not in COMPUTED_ROW_LABELS:
            return ["" for _ in row]
        styles = []
        for v in row:
            if pd.isna(v):
                styles.append("")
            elif v > 0:
                styles.append(f"color:{COLOR_UP}; font-weight:600")
            elif v < 0:
                styles.append(f"color:{COLOR_DOWN}; font-weight:600")
            else:
                styles.append("")
        return styles

    spread_cols = [c for c in table_df.columns if c in SPREAD_COLUMNS]
    other_cols = [c for c in table_df.columns if c not in SPREAD_COLUMNS]
    return (
        table_df.style
        .format(precision=2, na_rep="—", subset=other_cols)
        .format(precision=0, na_rep="—", subset=spread_cols)
        .apply(highlight_computed_rows, axis=1)
    )


def qtd_metric(label, value, unit, qtd_chg):
    precision = 0 if unit.strip() == "bps" else 2
    value_str = f"{value:.{precision}f}{unit}" if value is not None else "—"
    delta_str = f"{qtd_chg:+.0f} bps QTD" if qtd_chg is not None else None
    st.metric(label=label, value=value_str, delta=delta_str)


st.title("MBS Spread Tracker")
st.caption(
    "Daily par-coupon spread between 30-year UMBS TBAs (Mortgage News Daily "
    "5.5 / 6.0 / 6.5 closes, par interpolated with no adjustment) and 5yr/10yr "
    "US Treasuries - for estimating mortgage REIT book value moves intra-quarter."
)

df = load_dataframe()

if df.empty:
    st.warning(f"No data found in {db.DEFAULT_DB_PATH} yet. Run backfill.py and/or nightly_job.py first.")
    st.stop()

latest_rows = df.tail(2).to_dict("records")
today_row = latest_rows[-1]
prior_row = latest_rows[-2] if len(latest_rows) > 1 else None

gap_warning = None
if prior_row is not None:
    gap_days = (today_row["mbs_date"].date() - prior_row["mbs_date"].date()).days
    if gap_days > db.MAX_EXPECTED_GAP_DAYS:
        gap_warning = (
            f"⚠️ Data gap: {gap_days} calendar days between {prior_row['mbs_date'].date()} and "
            f"{today_row['mbs_date'].date()} - wider than a normal weekend/holiday, so one or more "
            "trading days are missing from the dataset. The 'Daily Change' row below and the QTD changes "
            "reflect the full gap, not a single day's move."
        )

# --- QTD change section (prominent, up top) ---
if today_row.get("qtd_ref_date") is None:
    st.info("No prior-quarter baseline available yet for this dataset - QTD change can't be computed for the current quarter's first stretch of data.")
else:
    cols = st.columns(5)
    with cols[0]:
        qtd_metric("5yr UST", today_row.get("ust_5yr"), "%", today_row.get("qtd_chg_ust_5yr"))
    with cols[1]:
        qtd_metric("10yr UST", today_row.get("ust_10yr"), "%", today_row.get("qtd_chg_ust_10yr"))
    with cols[2]:
        qtd_metric("Spread vs 5yr", today_row.get("spread_5yr"), " bps", today_row.get("qtd_chg_spread_5yr"))
    with cols[3]:
        qtd_metric("Spread vs 10yr", today_row.get("spread_10yr"), " bps", today_row.get("qtd_chg_spread_10yr"))
    with cols[4]:
        qtd_metric("Spread vs 5/10yr", today_row.get("spread_avg"), " bps", today_row.get("qtd_chg_spread_avg"))

st.divider()

# --- Daily table ---
current_qe, prior_qe = db.get_quarter_end_rows(today_row["mbs_date"].date())
daily_table = build_daily_table(today_row, prior_row, current_qe, prior_qe)
st.dataframe(style_daily_table(daily_table), width="stretch")

st.divider()

# --- Historical chart ---
st.subheader("Historical Spread")

fig = go.Figure()
series = [
    ("spread_5yr", "Spread vs 5yr", COLOR_SPREAD_5YR),
    ("spread_10yr", "Spread vs 10yr", COLOR_SPREAD_10YR),
    ("spread_avg", "Spread vs 5/10yr", COLOR_SPREAD_AVG),
]
for col, name, color in series:
    fig.add_trace(
        go.Scatter(
            x=df["mbs_date"],
            y=df[col],
            mode="lines",
            name=name,
            line=dict(color=color, width=2),
        )
    )

fig.update_layout(
    xaxis_title="Date",
    yaxis_title="Spread (bps)",
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    margin=dict(t=60, b=40),
)
fig.update_xaxes(showgrid=True, gridcolor=GRIDLINE, zeroline=False)
fig.update_yaxes(showgrid=True, gridcolor=GRIDLINE, zeroline=True, zerolinecolor=GRIDLINE)

st.plotly_chart(fig, width="stretch")

with st.expander("Show underlying data"):
    st.dataframe(df.drop(columns=["coupon_curve"]), width="stretch")

st.divider()
if gap_warning:
    st.warning(gap_warning)
