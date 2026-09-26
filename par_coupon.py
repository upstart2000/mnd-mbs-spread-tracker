"""
Par-coupon interpolation for the MND 30yr UMBS price series.

Given that day's closes for the UMBS 5.5 / 6.0 / 6.5 coupons, the par coupon
is the coupon whose price would be exactly 100, found by linear interpolation
between the coupon just below par (price <= 100) and the one just above
(price > 100) - no convexity or duration adjustment, just the straight line
between the two prices.

If every quoted coupon sits on the same side of par, there is no true bracket
to interpolate within; fall back to extrapolating along the same line using
the two coupons nearest par on that side.

Sanity guards (added after history rows came back with par between -379
and +369, wrecking the spread chart):
- the chosen pair must be strictly upward-sloping in price (p_high > p_low).
  A flat or inverted pair means that day's coupon curve is broken (stale
  mark, crossed quotes) and no meaningful line runs through it. This is the
  guard that kills the explosions: every one of them came from dividing by
  a near-zero or negative price gap;
- extrapolated par must land inside a loose [1, 10] plausibility band, a
  backstop against divide-by-near-zero artifacts. Interpolation is
  self-bounded by its bracket and needs no band.

Note: for most of 2016-2021 the 5.5/6.0/6.5 stack sat entirely above par, so
par is routinely an extrapolation a couple of points below 5.5 - that is the
straight-line model working as intended, not an error. Only numerically
degenerate days yield None (shown as gaps, not spikes).

Returns None when fewer than two usable prices exist, when the chosen pair
is flat/inverted, or when the extrapolation lands outside the plausible band.
"""

# Loose plausibility band for extrapolated par only: a backstop against
# divide-by-near-zero artifacts. True par 2016-2026 never left roughly
# [2.5, 7]; this band is deliberately wide so it never touches real history,
# but it catches the linear model's degenerate outputs (it once printed
# par = 0.29 with 5.5s at 102.77 - pure numerical artifact).
EXTRAPOLATED_PAR_MIN = 1.0
EXTRAPOLATED_PAR_MAX = 10.0


def compute_par_coupon(coupon_prices):
    """
    coupon_prices: {coupon(float): price(float)}.
    Returns (par_coupon, (c_low, p_low), (c_high, p_high)) or None.
    """
    usable = {
        c: p
        for c, p in coupon_prices.items()
        if isinstance(c, (int, float)) and isinstance(p, (int, float))
    }
    if len(usable) < 2:
        return None

    coupons_sorted = sorted(usable.keys())

    c_low = p_low = c_high = p_high = None
    for c in coupons_sorted:
        p = usable[c]
        if p <= 100:
            c_low, p_low = c, p  # keep updating to the highest-coupon sub-100 price
        elif c_high is None:
            c_high, p_high = c, p  # first coupon above par, right after the sub-100 run
            break

    extrapolated = False
    if c_low is None:
        # every coupon prices above par - extrapolate using the two lowest coupons
        extrapolated = True
        c_low, c_high = coupons_sorted[0], coupons_sorted[1]
        p_low, p_high = usable[c_low], usable[c_high]
    elif c_high is None:
        # every coupon prices at/below par - extrapolate using the two highest coupons
        extrapolated = True
        c_high, c_low = coupons_sorted[-1], coupons_sorted[-2]
        p_high, p_low = usable[c_high], usable[c_low]

    if not p_high > p_low:
        return None  # flat or inverted pair: no meaningful line through it

    par_coupon = c_low + (100 - p_low) * (c_high - c_low) / (p_high - p_low)

    if extrapolated and not (EXTRAPOLATED_PAR_MIN <= par_coupon <= EXTRAPOLATED_PAR_MAX):
        return None  # divide-by-near-zero artifact, not a market level

    return round(par_coupon, 4), (c_low, p_low), (c_high, p_high)


if __name__ == "__main__":
    # 2026-09-25 closes: 5.5 @ 96.30, 6.0 @ 98.84, 6.5 @ 101.23
    result = compute_par_coupon({5.5: 96.3047, 6.0: 98.8448, 6.5: 101.2344})
    print("par coupon:", result[0], "| bracket:", result[1], result[2])
