"""
Par-coupon interpolation for the MND 30yr UMBS price series.

Given that day's closes for the UMBS 5.5 / 6.0 / 6.5 coupons, the par coupon
is the coupon whose price would be exactly 100, found by linear interpolation
between the coupon just below par (price <= 100) and the one just above
(price > 100) - no convexity or duration adjustment, just the straight line
between the two prices.

If every quoted coupon sits on the same side of par, there is no true bracket
to interpolate within; fall back to extrapolating along the same line using
the two coupons nearest par on that side. Returns None only when fewer than
two usable prices exist, or the two chosen prices are identical (degenerate).
"""


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

    if c_low is None:
        # every coupon prices above par - extrapolate using the two lowest coupons
        c_low, c_high = coupons_sorted[0], coupons_sorted[1]
        p_low, p_high = usable[c_low], usable[c_high]
    elif c_high is None:
        # every coupon prices at/below par - extrapolate using the two highest coupons
        c_high, c_low = coupons_sorted[-1], coupons_sorted[-2]
        p_high, p_low = usable[c_high], usable[c_low]

    if p_high == p_low:
        return None  # degenerate, avoid divide-by-zero

    par_coupon = c_low + (100 - p_low) * (c_high - c_low) / (p_high - p_low)
    return round(par_coupon, 4), (c_low, p_low), (c_high, p_high)


if __name__ == "__main__":
    # 2026-09-25 closes: 5.5 @ 96.30, 6.0 @ 98.84, 6.5 @ 101.23
    result = compute_par_coupon({5.5: 96.3047, 6.0: 98.8448, 6.5: 101.2344})
    print("par coupon:", result[0], "| bracket:", result[1], result[2])
