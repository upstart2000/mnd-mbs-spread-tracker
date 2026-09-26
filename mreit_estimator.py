"""mREIT book-value estimator tab.

Estimates each mortgage REIT's current book value per share starting from its
last reported (Q2'26, i.e. June 30 2026) book value, using the company's own
disclosed interest-rate and MBS-spread sensitivity grids.

Method (per Sunil's spec):
- Rate shock for the quarter = average of QTD changes in 5yr and 10yr UST
  yields (bps). Spread shock = QTD change in the MBS par-coupon spread (bps).
  Both are pre-filled from the app's data and user-editable.
- Each company's disclosed grid is piecewise-linearly interpolated at the
  actual shock (with a (0, 0) anchor; extrapolates the nearest segment beyond
  the grid). The reported % (common equity / tangible CE / NAV / BV-share) is
  applied directly as the % change in book value per share (constant share
  count assumption - this is what makes Annaly's "% of NAV" grid usable as-is).
- Total estimated % change = rate effect + spread effect. Additivity is
  explicitly supported by the filings: ARR's 10-Q says the spread impact is
  "in addition to" the rate sensitivity; IVR's says it is "independent of" it.
- REITs with no disclosed spread grid (ORC, MFA) use a user-editable default
  sensitivity (default: 10% BV move per 25 bps spread move).
- Accrued dividend: assumes the announced dividend approximates earnings;
  annualized latest payout x days since last ex-date / 365 is added to the
  estimated BV (frequencies and ex-dates from Yahoo Finance dividends).
"""

import pandas as pd
import pandas_market_calendars as mcal
import streamlit as st
import yfinance as yf

# ----------------------------------------------------------------------------
# Disclosed sensitivity grids, as of June 30 2026.
# Each grid: list of (shock_bps, pct_change) with sign preserved.
# Positive shock = rates up / spreads wider. (0, 0) is added at interp time.
# ----------------------------------------------------------------------------
REITS = {
    "AGNC": {
        # Attribution corrected 2026-09-26 per Sunil's labeling of the source
        # screenshots: this is the "Tangible Common Equity" grid (±25/50/75bp
        # rates, ±10/25/50bp spreads, "basis risk").
        "name": "AGNC Investment Corp",
        "bv_q2": 8.58,  # tangible net BV / common share, 6/30/2026 (AGNC headline metric)
        "rate_grid": [(-75, 0.4), (-50, 1.5), (-25, 1.3),
                      (25, -2.3), (50, -5.3), (75, -8.8)],
        "rate_denom": "tangible common equity",
        "rate_source": "Q2'26 earnings presentation",
        "spread_grid": [(-50, 24.3), (-25, 12.2), (-10, 4.9),
                        (10, -4.9), (25, -12.2), (50, -24.3)],
        "spread_denom": "tangible common equity",
        "spread_source": "Q2'26 earnings presentation ('basis risk')",
    },
    "NLY": {
        # Attribution corrected 2026-09-26 per Sunil: this is the "% of NAV"
        # grid (±25/50/75bp rates, ±5/15/25bp spreads).
        "name": "Annaly Capital Management",
        "bv_q2": 20.15,  # BV / common share, 6/30/2026
        "rate_grid": [(-75, 2.0), (-50, 0.6), (-25, 0.1),
                      (25, -0.8), (50, -2.2), (75, -3.9)],
        "rate_denom": "NAV",
        "rate_source": "Q2'26 earnings presentation",
        "spread_grid": [(-25, 8.9), (-15, 5.3), (-5, 1.8),
                        (5, -1.8), (15, -5.2), (25, -8.7)],
        "spread_denom": "NAV",
        "spread_source": "Q2'26 earnings presentation",
    },
    "DX": {
        # Attribution corrected 2026-09-26 per Sunil: this is the "Percentage
        # Change in Common Shareholders' Equity" grid (±50/±100bp rates,
        # ±10bp and ±20bp OAS spreads). The CMBS IO footnote fits Dynex, which
        # holds CMBS IO.
        "name": "Dynex Capital",
        "bv_q2": 12.90,  # BV / common share, 6/30/2026
        "rate_grid": [(-100, -6.9), (-50, -0.8), (50, -3.5), (100, -9.4)],
        "rate_denom": "common shareholders' equity",
        "rate_source": "Q2'26 earnings presentation",
        "spread_grid": [(-20, 9.2), (-10, 4.6), (10, -4.6), (20, -9.2)],
        "spread_denom": "common shareholders' equity",
        # Footnote: +/-20 row blends a 20bp OAS shift (Agency RMBS/CMBS)
        # with a 50bp shift in CMBS IO; treated as the 20bp point.
        "spread_source": "Q2'26 earnings presentation",
    },
    "ORC": {
        "name": "Orchid Island Capital",
        "bv_q2": 7.22,  # BV / share, 6/30/2026
        "rate_grid": [(-200, -20.79), (-100, -5.61), (-50, -1.36),
                      (50, -1.47), (100, -5.58), (200, -20.53)],
        "rate_denom": "stockholders' equity (book value)",
        "rate_source": "Q2'26 10-Q, Item 3",
        # No spread sensitivity disclosed (10-Q "Spread Risk" is prose only).
        "spread_grid": None,
        "spread_denom": None,
        "spread_source": None,
    },
    "ARR": {
        "name": "ARMOUR Residential REIT",
        "bv_q2": 17.53,  # BV / common share, 6/30/2026
        "rate_grid": [(-100, -7.59), (-50, -1.72), (50, -1.60), (100, -4.69)],
        "rate_denom": "shareholder's equity",
        "rate_source": "Q2'26 10-Q, Item 3",
        "spread_grid": [(-25, 10.22), (-10, 4.09), (10, -4.09), (25, -10.22)],
        "spread_denom": "shareholders' equity",
        "spread_source": "Q2'26 10-Q, Item 3",
    },
    "IVR": {
        "name": "Invesco Mortgage Capital",
        "bv_q2": 8.03,  # BV / common share, 6/30/2026
        "rate_grid": [(-100, -7.94), (-50, -1.09), (50, -3.51), (100, -9.78)],
        "rate_denom": "book value per common share",
        "rate_source": "Q2'26 10-Q, Item 3",
        "spread_grid": [(-20, 9.69), (-10, 4.82), (10, -4.78), (20, -9.51)],
        "spread_denom": "book value per common share",
        "spread_source": "Q2'26 10-Q, Item 3",
    },
    "MFA": {
        "name": "MFA Financial",
        "bv_q2": 13.20,  # economic BV / common share, 6/30/2026 (GAAP BV $12.71)
        "rate_grid": [(-100, 4.80), (-50, 3.09), (50, -4.48), (100, -10.34)],
        "rate_denom": "total stockholders' equity",
        "rate_source": "Q2'26 10-Q (via earnings materials)",
        # Spread grid not captured; 10-Q may carry one.
        "spread_grid": None,
        "spread_denom": None,
        "spread_source": None,
    },
}

TICKER_ORDER = ["AGNC", "NLY", "DX", "ORC", "ARR", "IVR", "MFA"]


def interp_pct(grid, x_bps):
    """Piecewise-linear interpolation of a sensitivity grid at x_bps.

    A (0, 0) anchor is added (no shock -> no change). Beyond the grid's ends
    the nearest segment is extrapolated linearly.
    """
    pts = sorted([(0, 0.0)] + [(float(x), float(y)) for x, y in grid])
    if x_bps <= pts[0][0]:
        (x0, y0), (x1, y1) = pts[0], pts[1]
    elif x_bps >= pts[-1][0]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
    else:
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= x_bps <= x1:
                break
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x_bps - x0) / (x1 - x0)


def estimate(rate_chg_bps, spread_chg_bps, default_spread_sens=10.0):
    """Estimate current BV/share for every REIT.

    default_spread_sens: % BV move per +25 bps spread widening, used where a
    REIT discloses no spread grid. Returns a list of row dicts.
    """
    rows = []
    for t in TICKER_ORDER:
        info = REITS[t]
        d_rate = interp_pct(info["rate_grid"], rate_chg_bps)
        if info["spread_grid"]:
            d_spread = interp_pct(info["spread_grid"], spread_chg_bps)
        else:
            d_spread = -(default_spread_sens / 25.0) * spread_chg_bps
        total = d_rate + d_spread
        bv_q2 = info["bv_q2"]
        est_bv = bv_q2 * (1 + total / 100.0) if bv_q2 else None
        rows.append({
            "REIT": t,
            "Q2'26 BV ($)": bv_q2,
            "ΔBV rates (%)": d_rate,
            "ΔBV spreads (%)": d_spread,
            "Total ΔBV (%)": total,
            "Est. BV today ($)": est_bv,
        })
    return rows


@st.cache_data(ttl=43200)
def fetch_dividends(tickers):
    """Dividend accrual inputs per ticker from Yahoo Finance.

    Returns {ticker: {annual, last_amt, mult, freq ('monthly'/'quarterly'),
    last_ex (date str), days, accrued}} with accrued = annual * days / 365.
    Annualized from the most recent payout so cuts/hikes are picked up
    immediately; frequency from the median gap of recent payouts.
    """
    out = {}
    try:
        tk = yf.Tickers(" ".join(tickers))
    except Exception:
        return {t: None for t in tickers}
    today = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
    for t in tickers:
        try:
            d = tk.tickers[t].dividends
            if d is None or d.empty:
                out[t] = None
                continue
            d = d.copy()
            d.index = pd.to_datetime(d.index)
            if d.index.tz is not None:
                d.index = d.index.tz_convert("America/New_York").tz_localize(None)
            d = d[d.index.normalize() <= today]
            if d.empty:
                out[t] = None
                continue
            last_ex = d.index[-1].normalize()
            last_amt = float(d.iloc[-1])
            gaps = d.index.to_series().diff().dt.days.dropna().tail(4)
            monthly = not gaps.empty and gaps.median() < 40
            mult = 12 if monthly else 4
            annual = last_amt * mult
            days = max((today - last_ex).days, 0)
            out[t] = {
                "annual": annual,
                "last_amt": last_amt,
                "mult": mult,
                "freq": "monthly" if monthly else "quarterly",
                "last_ex": last_ex.date().isoformat(),
                "days": days,
                "accrued": annual * days / 365.0,
            }
        except Exception:
            out[t] = None
    return out


@st.cache_data(ttl=900)
def fetch_prices(tickers):
    """Latest price per ticker from Yahoo Finance (15-min delayed while open)."""
    out = {}
    try:
        tk = yf.Tickers(" ".join(tickers))
        for t in tickers:
            try:
                fi = dict(tk.tickers[t].fast_info)
                px = fi.get("lastPrice") or fi.get("previousClose")
                out[t] = float(px) if px else None
            except Exception:
                out[t] = None
    except Exception:
        out = {t: None for t in tickers}
    return out


def market_state():
    """NYSE regular-session state: (is_live, last_session_str, now_str), ET."""
    try:
        nyse = mcal.get_calendar("NYSE")
        now = pd.Timestamp.now(tz="America/New_York")
        sched = nyse.schedule(start_date=(now - pd.Timedelta(days=10)).date(),
                              end_date=now.date())
        if not sched.empty:
            last = sched.iloc[-1]
            live = last["market_open"] <= now <= last["market_close"]
            return (live,
                    last.name.strftime("%a %b %d, %Y"),
                    now.strftime("%I:%M %p ET").lstrip("0"))
    except Exception:
        pass
    return False, "", ""


def render(today_row):
    st.subheader("mREIT Book Value Estimator")
    st.caption(
        "Guesstimates today's book value per share from each REIT's last reported "
        "(June 30, 2026) book value, using the company's own disclosed rate/spread "
        "sensitivity grids interpolated to this quarter's actual moves."
    )

    r5 = today_row.get("qtd_chg_ust_5yr")
    r10 = today_row.get("qtd_chg_ust_10yr")
    s_avg = today_row.get("qtd_chg_spread_avg")
    ref = today_row.get("qtd_ref_date")
    last = today_row["mbs_date"].date()

    default_rate = round((r5 + r10) / 2) if r5 is not None and r10 is not None else 0
    default_spread = round(s_avg) if s_avg is not None else 0

    st.markdown(
        f"Quarter-to-date moves measured **{ref} → {last}** "
        f"(5yr {r5:+.0f} bps, 10yr {r10:+.0f} bps, spread {s_avg:+.0f} bps). "
        "Override any input below."
        if r5 is not None and s_avg is not None else
        "QTD baselines unavailable - enter shocks manually."
    )
    st.markdown(
        "<style>"
        "div[data-testid='stNumberInput']{max-width:210px}"
        "div[data-testid='stNumberInput'] label{white-space:nowrap}"
        "</style>",
        unsafe_allow_html=True,
    )
    c1, c2, c3, _spacer = st.columns([1, 1, 1, 2])
    with c1:
        rate_chg = st.number_input(
            "Rate change since 6/30 (bps)", value=default_rate, step=5,
            help="Average of QTD 5yr/10yr UST moves. Positive = rates rose.")
    with c2:
        spread_chg = st.number_input(
            "Spread change since 6/30 (bps)", value=default_spread, step=1,
            help="QTD change in MBS par-coupon spread. Positive = spreads widened.")
    with c3:
        default_sens = st.number_input(
            "Default spread sensitivity", value=10.0, step=0.5,
            min_value=0.0,
            help="Used for REITs with no disclosed spread grid (ORC, MFA). "
                 "% of BV per +25 bps spread widening.")

    rows = estimate(rate_chg, spread_chg, default_sens)

    dividends = fetch_dividends(TICKER_ORDER)
    for r in rows:
        info = dividends.get(r["REIT"]) or {}
        r["Accrued div. ($)"] = info.get("accrued")
        mech = r["Est. BV today ($)"]
        r["Est. BV + accr. div. ($)"] = (
            mech + info["accrued"]
            if mech is not None and info.get("accrued") else None
        )

    prices = fetch_prices(TICKER_ORDER)
    for r in rows:
        p = prices.get(r["REIT"])
        bv = r["Est. BV + accr. div. ($)"]
        r["Price ($)"] = p
        r["P / Est. BV (%)"] = (100.0 * p / bv) if p and bv else None
    df = pd.DataFrame(rows)[[
        "REIT", "Q2'26 BV ($)", "ΔBV rates (%)", "ΔBV spreads (%)",
        "Total ΔBV (%)", "Est. BV today ($)", "Accrued div. ($)",
        "Est. BV + accr. div. ($)", "Price ($)", "P / Est. BV (%)",
    ]]

    def _pct(v):
        return f"{v:+.1f}%" if pd.notna(v) else "—"

    styled = (
        df.style
        .format({
            "Q2'26 BV ($)": lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "ΔBV rates (%)": _pct,
            "ΔBV spreads (%)": _pct,
            "Total ΔBV (%)": _pct,
            "Est. BV today ($)": lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "Accrued div. ($)": lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "Est. BV + accr. div. ($)": lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "Price ($)": lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "P / Est. BV (%)": lambda v: f"{v:.1f}%" if pd.notna(v) else "—",
        })
        .map(lambda v: "color: #e34948" if pd.notna(v) and v < 0 else
             ("color: #006300" if pd.notna(v) and v > 0 else ""),
             subset=["ΔBV rates (%)", "ΔBV spreads (%)",
                     "Total ΔBV (%)", "Accrued div. ($)"])
        .set_properties(**{"text-align": "center"})
    )
    st.dataframe(
        styled,
        width="stretch",
        hide_index=True,
        column_config={
            c: st.column_config.TextColumn(
                width="medium" if c == "Est. BV + accr. div. ($)" else "small")
            for c in df.columns
        },
    )

    is_live, last_session, now_et = market_state()
    if st.button("↻ Refresh prices",
                 help="Re-fetch the latest prices from Yahoo Finance"):
        fetch_prices.clear()
        st.rerun()
    price_note = (f"live (15-min delayed), as of {now_et}" if is_live
                  else f"last close ({last_session})")
    st.caption(
        "Accrued dividend assumes each REIT earns its announced dividend: "
        "annualized latest payout × days since last ex-date ÷ 365. "
        "Price / Est. BV is on the with-accrual estimate. "
        "ORC and MFA disclose no spread sensitivity grid — their spread effect "
        f"uses the default sensitivity above ({default_sens:g}% of BV per +25 bps "
        "widening). MFA's Q2'26 book value is economic book value ($13.20). "
        f"Prices and dividends via Yahoo Finance — {price_note}."
    )

    with st.expander("Disclosed sensitivity grids & methodology"):
        st.markdown(
            "- **Interpolation:** piecewise-linear on each company's grid, "
            "anchored at (0 bps → 0%). Beyond the grid, the nearest segment is "
            "extended linearly.\n"
            "- **Additivity:** rate effect + spread effect. ARR's 10-Q states the "
            "spread impact is \"in addition to\" rate sensitivity; IVR's says it is "
            "\"independent of\" it.\n"
            "- **Denominators:** each company's reported % is applied directly as "
            "the % change in BV/share (constant share count). Annaly reports % of "
            "NAV, which equals % BV under that assumption.\n"
            "- **Defaults:** ORC and MFA disclose no spread grid, so the editable "
            "default above is used for them.\n"
            "- **Accrued dividend:** assumes the announced dividend approximates "
            "earnings. Annualized latest payout × days since last ex-date ÷ 365 "
            "is added to estimated BV (frequencies and ex-dates from Yahoo "
            "Finance dividends; latest payout annualized so cuts/hikes register "
            "immediately)."
        )
        for t in TICKER_ORDER:
            info = REITS[t]
            st.markdown(f"**{t} — {info['name']}**")
            st.markdown(
                f"- Rates ({info['rate_denom']}; {info['rate_source']}): "
                + ", ".join(f"{x:+.0f}bps → {y:+.2f}%"
                            for x, y in sorted(info["rate_grid"]))
            )
            if info["spread_grid"]:
                st.markdown(
                    f"- Spreads ({info['spread_denom']}; {info['spread_source']}): "
                    + ", ".join(f"{x:+.0f}bps → {y:+.2f}%"
                                for x, y in sorted(info["spread_grid"]))
                )
            else:
                st.markdown("- Spreads: not disclosed — default sensitivity used.")
            div = dividends.get(t)
            if div:
                st.markdown(
                    f"- Dividend: {div['freq']}, annualized ${div['annual']:.2f} "
                    f"({div['mult']}×${div['last_amt']:.2f}), last ex-date "
                    f"{div['last_ex']}, {div['days']}d accrued → +${div['accrued']:.2f}/share."
                )
            else:
                st.markdown("- Dividend: no data — no accrual added.")
