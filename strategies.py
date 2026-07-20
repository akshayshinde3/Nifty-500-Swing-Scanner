"""
strategies.py — Multi-Pattern Swing Trading Engine

Implements:
  1.  Triangle pattern  (Ascending / Symmetrical)
  2.  44-Day SMA breakout
  3.  Head & Shoulders / Inverse Head & Shoulders  (reversal)
  4.  Flag pattern  (Bull Flag)
  5.  Wedge pattern  (Rising Wedge)
  6.  Channel pattern  (Ascending Channel)
  7.  Pennant pattern

All patterns include:
  - Confirmed breakout detection
  - Pre-breakout / watch-list detection
  - Confidence scoring
  - Trade levels (buy, stop-loss, target1, target2)

None of this predicts the future with certainty — it identifies setups
that historically precede breakouts more often than random price action.
Treat "confidence score" as setup quality, not a probability of profit.
"""

import datetime as dt
import statistics


# ---------------------------------------------------------------------------
# Swing point + trendline helpers
# ---------------------------------------------------------------------------
def find_swing_points(highs, lows, window=3):
    """Local swing highs/lows within +/- `window` bars."""
    swing_highs, swing_lows = [], []
    n = len(highs)
    for i in range(window, n - window):
        seg_h = highs[i - window: i + window + 1]
        seg_l = lows[i - window: i + window + 1]
        if highs[i] == max(seg_h):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(seg_l):
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


def linreg(points):
    """Simple linear regression over (x, y) points. Returns (slope, intercept, r2)."""
    n = len(points)
    if n < 2:
        return 0.0, points[0][1] if points else 0.0, 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    if ss_xx == 0:
        return 0.0, mean_y, 0.0
    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
    r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    return slope, intercept, r2


def line_value(slope, intercept, x):
    return slope * x + intercept


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


# ---------------------------------------------------------------------------
# 1. TRIANGLE PATTERN
# ---------------------------------------------------------------------------
FLAT_SLOPE_THRESHOLD = 0.0015
MIN_TOUCHES = 2
LOOKBACK = 40


def detect_triangle(candles):
    if len(candles) < LOOKBACK + 5:
        return None
    window = candles[-LOOKBACK:]
    highs = [c[2] for c in window]
    lows = [c[3] for c in window]
    closes = [c[4] for c in window]
    avg_price = sum(closes) / len(closes)

    swing_highs, swing_lows = find_swing_points(highs, lows, window=2)
    if len(swing_highs) < MIN_TOUCHES or len(swing_lows) < MIN_TOUCHES:
        return None

    res_slope, res_intercept, res_r2 = linreg(swing_highs)
    sup_slope, sup_intercept, sup_r2 = linreg(swing_lows)

    res_slope_norm = res_slope / avg_price
    sup_slope_norm = sup_slope / avg_price

    last_x = len(window) - 1
    res_now = line_value(res_slope, res_intercept, last_x)
    sup_now = line_value(sup_slope, sup_intercept, last_x)
    if sup_now >= res_now:
        return None

    res_flat = abs(res_slope_norm) < FLAT_SLOPE_THRESHOLD
    sup_flat = abs(sup_slope_norm) < FLAT_SLOPE_THRESHOLD

    triangle_type = None
    if res_flat and sup_slope_norm > FLAT_SLOPE_THRESHOLD:
        triangle_type = "Ascending Triangle"
    elif res_slope_norm < -FLAT_SLOPE_THRESHOLD and sup_slope_norm > FLAT_SLOPE_THRESHOLD:
        triangle_type = "Symmetrical Triangle"
    else:
        return None

    fit_quality = (res_r2 + sup_r2) / 2
    gap_start = line_value(res_slope, res_intercept, 0) - line_value(sup_slope, sup_intercept, 0)
    gap_now = res_now - sup_now
    contraction = 1 - (gap_now / gap_start) if gap_start > 0 else 0

    if res_slope != sup_slope:
        apex_x = (sup_intercept - res_intercept) / (res_slope - sup_slope)
        bars_to_apex = apex_x - last_x
    else:
        bars_to_apex = None

    return {
        "type": triangle_type,
        "resistance_now": round(res_now, 2),
        "support_now": round(sup_now, 2),
        "fit_quality": round(fit_quality, 2),
        "contraction": round(max(contraction, 0), 2),
        "bars_to_apex": round(bars_to_apex, 1) if bars_to_apex is not None else None,
        "touches": len(swing_highs) + len(swing_lows),
    }


# ---------------------------------------------------------------------------
# 2. 44 SMA STRATEGY
# ---------------------------------------------------------------------------
def sma44_signal(candles):
    closes = [c[4] for c in candles]
    opens = [c[1] for c in candles]
    if len(closes) < 46:
        return None

    sma_today = sma(closes, 44)
    sma_yday = sma(closes[:-1], 44)
    if sma_today is None or sma_yday is None:
        return None

    today_close, today_open = closes[-1], opens[-1]
    yday_close = closes[-2]

    is_green = today_close > today_open
    crosses_above = yday_close <= sma_yday and today_close > sma_today

    if is_green and crosses_above:
        return {"sma44": round(sma_today, 2), "status": "confirmed"}

    near_below = sma_today >= today_close > sma_today * 0.99
    trending_up = closes[-1] > closes[-4] if len(closes) >= 4 else False
    if near_below and trending_up:
        return {"sma44": round(sma_today, 2), "status": "approaching"}

    return None


# ---------------------------------------------------------------------------
# 3. HEAD & SHOULDERS / INVERSE H&S
# ---------------------------------------------------------------------------
def detect_head_and_shoulders(candles):
    """
    Classic H&S (bearish reversal) and Inverse H&S (bullish reversal).
    Looks for 3 peaks (H&S) or 3 troughs (IH&S) in the last 60 bars.
    Returns dict with type, neckline, status (forming/confirmed).
    """
    if len(candles) < 60:
        return None

    window = candles[-60:]
    highs = [c[2] for c in window]
    lows = [c[3] for c in window]
    closes = [c[4] for c in window]

    swing_highs, swing_lows = find_swing_points(highs, lows, window=3)

    # --- Head & Shoulders (bearish) ---
    if len(swing_highs) >= 3:
        # Take the 3 most prominent peaks
        peaks = sorted(swing_highs, key=lambda p: p[1], reverse=True)[:5]
        peaks = sorted(peaks, key=lambda p: p[0])  # sort by position

        for i in range(len(peaks) - 2):
            left, head, right = peaks[i], peaks[i+1], peaks[i+2]
            # Head must be highest, shoulders roughly equal
            if head[1] <= left[1] or head[1] <= right[1]:
                continue
            shoulder_diff = abs(left[1] - right[1]) / head[1]
            if shoulder_diff > 0.05:  # shoulders within 5% of each other
                continue

            # Neckline: average of the troughs between shoulders
            troughs_between = [s for s in swing_lows if left[0] < s[0] < right[0]]
            if len(troughs_between) < 1:
                continue
            neckline = sum(t[1] for t in troughs_between) / len(troughs_between)
            current_price = closes[-1]

            if current_price < neckline * 0.995:
                return {
                    "type": "Head & Shoulders",
                    "direction": "bearish",
                    "neckline": round(neckline, 2),
                    "head_high": round(head[1], 2),
                    "status": "confirmed",
                }
            elif current_price < neckline * 1.02:
                return {
                    "type": "Head & Shoulders",
                    "direction": "bearish",
                    "neckline": round(neckline, 2),
                    "head_high": round(head[1], 2),
                    "status": "forming",
                }

    # --- Inverse Head & Shoulders (bullish) ---
    if len(swing_lows) >= 3:
        troughs = sorted(swing_lows, key=lambda p: p[1])[:5]
        troughs = sorted(troughs, key=lambda p: p[0])

        for i in range(len(troughs) - 2):
            left, head, right = troughs[i], troughs[i+1], troughs[i+2]
            if head[1] >= left[1] or head[1] >= right[1]:
                continue
            shoulder_diff = abs(left[1] - right[1]) / abs(head[1]) if head[1] != 0 else 1
            if shoulder_diff > 0.05:
                continue

            peaks_between = [s for s in swing_highs if left[0] < s[0] < right[0]]
            if len(peaks_between) < 1:
                continue
            neckline = sum(t[1] for t in peaks_between) / len(peaks_between)
            current_price = closes[-1]

            if current_price > neckline * 1.005:
                return {
                    "type": "Inverse Head & Shoulders",
                    "direction": "bullish",
                    "neckline": round(neckline, 2),
                    "head_low": round(head[1], 2),
                    "status": "confirmed",
                }
            elif current_price > neckline * 0.98:
                return {
                    "type": "Inverse Head & Shoulders",
                    "direction": "bullish",
                    "neckline": round(neckline, 2),
                    "head_low": round(head[1], 2),
                    "status": "forming",
                }

    return None


# ---------------------------------------------------------------------------
# 4. FLAG PATTERN
# ---------------------------------------------------------------------------
def detect_flag(candles):
    """
    Bull Flag: sharp pole up (>6% in 5-10 bars), then tight downward-drifting
    consolidation (flag), breakout above flag resistance.
    """
    if len(candles) < 30:
        return None

    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]

    # Look for a pole in the last 15-30 bars (before the last 10)
    pole_window = candles[-30:-10]
    flag_window = candles[-10:]

    if len(pole_window) < 5 or len(flag_window) < 4:
        return None

    pole_start_close = pole_window[0][4]
    pole_end_close = pole_window[-1][4]
    pole_move = (pole_end_close - pole_start_close) / pole_start_close

    flag_closes = [c[4] for c in flag_window]
    flag_highs = [c[2] for c in flag_window]
    flag_lows = [c[3] for c in flag_window]
    flag_slope = (flag_closes[-1] - flag_closes[0]) / (len(flag_closes) * pole_start_close)

    current_price = closes[-1]

    # Bull Flag
    if pole_move >= 0.06 and -0.025 <= flag_slope <= 0.005:
        flag_resistance = max(flag_highs)
        flag_support = min(flag_lows)
        if current_price > flag_resistance * 1.002:
            return {
                "type": "Bull Flag",
                "direction": "bullish",
                "pole_move_pct": round(pole_move * 100, 1),
                "flag_resistance": round(flag_resistance, 2),
                "flag_support": round(flag_support, 2),
                "status": "confirmed",
            }
        elif current_price >= flag_resistance * 0.99:
            return {
                "type": "Bull Flag",
                "direction": "bullish",
                "pole_move_pct": round(pole_move * 100, 1),
                "flag_resistance": round(flag_resistance, 2),
                "flag_support": round(flag_support, 2),
                "status": "forming",
            }

    return None


# ---------------------------------------------------------------------------
# 5. WEDGE PATTERN
# ---------------------------------------------------------------------------
def detect_wedge(candles):
    """
    Rising Wedge: both trendlines slope up and converge.
    This buy-side scanner only accepts breakouts above wedge resistance.
    """
    if len(candles) < 30:
        return None

    window = candles[-30:]
    highs = [c[2] for c in window]
    lows = [c[3] for c in window]
    closes = [c[4] for c in window]
    avg_price = sum(closes) / len(closes)

    swing_highs, swing_lows = find_swing_points(highs, lows, window=2)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    res_slope, res_intercept, res_r2 = linreg(swing_highs)
    sup_slope, sup_intercept, sup_r2 = linreg(swing_lows)

    res_slope_norm = res_slope / avg_price
    sup_slope_norm = sup_slope / avg_price

    last_x = len(window) - 1
    res_now = line_value(res_slope, res_intercept, last_x)
    sup_now = line_value(sup_slope, sup_intercept, last_x)
    if sup_now >= res_now:
        return None

    # Both lines must slope same direction and converge
    both_up = res_slope_norm > 0.0005 and sup_slope_norm > 0.0005
    converging = res_slope_norm < sup_slope_norm  # resistance rising slower than support (or falling faster)

    if not both_up or not converging:
        return None

    current_price = closes[-1]
    fit_quality = (res_r2 + sup_r2) / 2

    if both_up:
        if current_price > res_now * 1.002:
            return {
                "type": "Rising Wedge",
                "direction": "bullish",
                "resistance": round(res_now, 2),
                "support": round(sup_now, 2),
                "fit_quality": round(fit_quality, 2),
                "status": "confirmed",
            }
        elif current_price >= res_now * 0.99:
            return {
                "type": "Rising Wedge",
                "direction": "bullish",
                "resistance": round(res_now, 2),
                "support": round(sup_now, 2),
                "fit_quality": round(fit_quality, 2),
                "status": "forming",
            }

    return None


# ---------------------------------------------------------------------------
# 6. CHANNEL PATTERN
# ---------------------------------------------------------------------------
def detect_channel(candles):
    """
    Ascending Channel: both trendlines slope up and are parallel → buy dips at support.
    Breakout from the channel is a high-conviction trade.
    """
    if len(candles) < 40:
        return None

    window = candles[-40:]
    highs = [c[2] for c in window]
    lows = [c[3] for c in window]
    closes = [c[4] for c in window]
    avg_price = sum(closes) / len(closes)

    swing_highs, swing_lows = find_swing_points(highs, lows, window=3)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    res_slope, res_intercept, res_r2 = linreg(swing_highs)
    sup_slope, sup_intercept, sup_r2 = linreg(swing_lows)

    res_slope_norm = res_slope / avg_price
    sup_slope_norm = sup_slope / avg_price

    # Parallel: slopes within 20% of each other
    if sup_slope_norm == 0:
        return None
    slope_ratio = abs(res_slope_norm / sup_slope_norm)
    if not (0.6 <= slope_ratio <= 1.6):
        return None

    # Both must clearly slope the same direction
    both_up = res_slope_norm > 0.001 and sup_slope_norm > 0.001
    if not both_up:
        return None

    last_x = len(window) - 1
    res_now = line_value(res_slope, res_intercept, last_x)
    sup_now = line_value(sup_slope, sup_intercept, last_x)
    if sup_now >= res_now:
        return None

    current_price = closes[-1]
    channel_width = res_now - sup_now
    fit_quality = (res_r2 + sup_r2) / 2

    channel_type = "Ascending Channel"

    # Breakout above resistance
    if current_price > res_now * 1.003:
        return {
            "type": channel_type + " Breakout",
            "direction": "bullish",
            "resistance": round(res_now, 2),
            "support": round(sup_now, 2),
            "channel_width": round(channel_width, 2),
            "fit_quality": round(fit_quality, 2),
            "status": "confirmed",
        }

    # Price near support in ascending channel = buy opportunity
    if current_price <= sup_now * 1.015:
        return {
            "type": channel_type,
            "direction": "bullish",
            "resistance": round(res_now, 2),
            "support": round(sup_now, 2),
            "channel_width": round(channel_width, 2),
            "fit_quality": round(fit_quality, 2),
            "status": "forming",
        }

    return None




# ---------------------------------------------------------------------------
# 7. PENNANT STRATEGY
# ---------------------------------------------------------------------------
def detect_pennant(candles):
    """
    Pennant Pattern:
    - Sharp rise (pole) of at least 7% in the last 15-25 bars.
    - Small symmetrical triangle consolidation (converging lines) over 5-20 bars.
    - Confirmed: breakout above resistance.
    """
    if len(candles) < 25:
        return None

    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    n = len(candles)

    pole_found = False
    pole_start_idx = -1
    pole_move_pct = 0.0

    for i in range(max(0, n - 25), n - 8):
        move = (closes[i+8] - closes[i]) / closes[i] * 100
        if move >= 7.0:
            pole_found = True
            pole_start_idx = i
            pole_move_pct = move
            break

    if not pole_found:
        return None

    consol_window = candles[pole_start_idx + 8:]
    if len(consol_window) < 5 or len(consol_window) > 20:
        return None

    c_highs = [c[2] for c in consol_window]
    c_lows = [c[3] for c in consol_window]

    res_slope, res_intercept, _ = linreg([(idx, val) for idx, val in enumerate(c_highs)])
    sup_slope, sup_intercept, _ = linreg([(idx, val) for idx, val in enumerate(c_lows)])

    avg_price = sum(closes[-len(consol_window):]) / len(consol_window)
    res_slope_norm = res_slope / avg_price
    sup_slope_norm = sup_slope / avg_price

    # converging lines (resistance slopes down, support slopes up)
    if res_slope_norm < -0.0005 and sup_slope_norm > 0.0005:
        last_x = len(consol_window) - 1
        res_now = line_value(res_slope, res_intercept, last_x)
        sup_now = line_value(sup_slope, sup_intercept, last_x)

        if res_now <= sup_now:
            return None

        current_price = closes[-1]

        if current_price > res_now:
            return {
                "type": "Pennant Breakout",
                "direction": "bullish",
                "resistance": round(res_now, 2),
                "support": round(sup_now, 2),
                "pole_move_pct": round(pole_move_pct, 1),
                "status": "confirmed",
            }
        elif current_price >= res_now * 0.985:
            return {
                "type": "Pennant Consolidation",
                "direction": "bullish",
                "resistance": round(res_now, 2),
                "support": round(sup_now, 2),
                "pole_move_pct": round(pole_move_pct, 1),
                "status": "forming",
            }

    return None



# ---------------------------------------------------------------------------
# Per-pattern alert builder helpers
# ---------------------------------------------------------------------------
def _pattern_levels(pattern_type, pattern, candles):
    """Compute buy/sl/target levels based on pattern type."""
    today_close = candles[-1][4]
    atr = sum(c[2] - c[3] for c in candles[-14:]) / 14

    if pattern_type == "triangle":
        buy_price = round(pattern["resistance_now"] * 1.002, 2)
        stop_loss = round(pattern["support_now"], 2)

    elif pattern_type == "flag":
        buy_price = round(pattern["flag_resistance"] * 1.002, 2)
        stop_loss = round(pattern["flag_support"], 2)

    elif pattern_type == "wedge":
        buy_price = round(pattern["resistance"] * 1.002, 2)
        stop_loss = round(pattern["support"], 2)

    elif pattern_type == "darvas":
        buy_price = round(pattern["box_top"] * 1.002, 2)
        stop_loss = round(pattern["box_bottom"], 2)

    elif pattern_type == "pennant":
        buy_price = round(pattern["resistance"] * 1.002, 2)
        stop_loss = round(pattern["support"], 2)

    else:
        buy_price = round(today_close, 2)
        stop_loss = round(today_close - 1.5 * atr, 2)

    risk = buy_price - stop_loss
    if risk <= 0:
        return None, None, None, None

    target1 = round(buy_price + 2 * risk, 2)
    target2 = round(buy_price + 3 * risk, 2)
    return buy_price, stop_loss, target1, target2


# ---------------------------------------------------------------------------
# Main combined alert builder
# ---------------------------------------------------------------------------
def build_alert(symbol, candles, live_price):
    """
    Runs ALL strategies, picks the best qualifying signal, returns a full
    alert dict or None if nothing qualifies.
    """
    if len(candles) < 50:
        return None

    today = candles[-1]
    today_open, today_close = today[1], today[4]
    is_green_today = today_close > today_open
    atr = sum(c[2] - c[3] for c in candles[-14:]) / 14

    # Run all detectors
    triangle      = detect_triangle(candles)
    flag          = detect_flag(candles)
    wedge         = detect_wedge(candles)
    pennant       = detect_pennant(candles)

    reasons = []
    strategies_used = []
    status = "confirmed"
    score = 0
    best_pattern_type = None
    best_pattern = None

    # ---- Triangle ----
    if triangle:
        breakout_now = is_green_today and today_close > triangle["resistance_now"]
        approaching = (
            triangle["contraction"] >= 0.4
            and triangle["bars_to_apex"] is not None and 0 < triangle["bars_to_apex"] <= 6
            and today_close >= triangle["resistance_now"] * 0.985
        )
        if breakout_now:
            strategies_used.append("Triangle Breakout")
            reasons.append(f"{triangle['type']} breakout above {triangle['resistance_now']}")
            score += 40
            best_pattern_type = "triangle"
            best_pattern = triangle
        elif approaching:
            strategies_used.append("Triangle Breakout")
            status = "pre_breakout_watch"
            reasons.append(
                f"{triangle['type']} tightening ({int(triangle['contraction']*100)}% contraction), "
                f"price {today_close} near resistance {triangle['resistance_now']}, "
                f"~{triangle['bars_to_apex']} bars to apex"
            )
            score += 22
            best_pattern_type = "triangle"
            best_pattern = triangle

    # ---- Flag ----
    if flag and flag["direction"] == "bullish":
        if flag["status"] == "confirmed":
            strategies_used.append(flag["type"])
            reasons.append(f"{flag['type']}: pole +{flag['pole_move_pct']}%, breakout above flag at {flag['flag_resistance']}")
            score += 36
            if not best_pattern_type:
                best_pattern_type = "flag"
                best_pattern = flag
        elif flag["status"] == "forming":
            strategies_used.append(flag["type"])
            status = "pre_breakout_watch"
            reasons.append(f"{flag['type']} forming after +{flag['pole_move_pct']}% pole, resistance at {flag['flag_resistance']}")
            score += 20
            if not best_pattern_type:
                best_pattern_type = "flag"
                best_pattern = flag

    # ---- Wedge ----
    if wedge and wedge["direction"] == "bullish":
        if wedge["status"] == "confirmed":
            strategies_used.append(wedge["type"])
            reasons.append(f"{wedge['type']} breakout above {wedge['resistance']} (fit quality {wedge['fit_quality']})")
            score += 34
            if not best_pattern_type:
                best_pattern_type = "wedge"
                best_pattern = wedge
        elif wedge["status"] == "forming":
            strategies_used.append(wedge["type"])
            status = "pre_breakout_watch"
            reasons.append(f"{wedge['type']} forming, resistance at {wedge['resistance']}, support at {wedge['support']}")
            score += 18
            if not best_pattern_type:
                best_pattern_type = "wedge"
                best_pattern = wedge

    # ---- Pennant ----
    if pennant:
        if pennant["status"] == "confirmed":
            strategies_used.append("Pennant Breakout")
            reasons.append(f"Pennant breakout: pole +{pennant['pole_move_pct']}%, closed above resistance {pennant['resistance']}")
            score += 37
            if not best_pattern_type:
                best_pattern_type = "pennant"
                best_pattern = pennant
        elif pennant["status"] == "forming":
            strategies_used.append("Pennant Consolidation")
            status = "pre_breakout_watch"
            reasons.append(f"Pennant forming after +{pennant['pole_move_pct']}% move, resistance at {pennant['resistance']}")
            score += 19
            if not best_pattern_type:
                best_pattern_type = "pennant"
                best_pattern = pennant

    if not strategies_used:
        return None


    if len(strategies_used) >= 2:
        score += 10  # multiple patterns agreeing

    confidence = min(round(score), 95)

    # ---- Trade levels ----
    buy_price, stop_loss, target1, target2 = _pattern_levels(best_pattern_type, best_pattern, candles)
    if buy_price is None:
        # Fallback to ATR-based levels
        buy_price = round(today_close, 2)
        stop_loss = round(today_close - 1.5 * atr, 2)
        risk = buy_price - stop_loss
        if risk <= 0:
            return None
        target1 = round(buy_price + 2 * risk, 2)
        target2 = round(buy_price + 3 * risk, 2)

    risk = buy_price - stop_loss
    if risk <= 0:
        return None

    rr = round((target1 - buy_price) / risk, 2)
    if rr < 2:
        return None  # enforce minimum 1:2 R:R

    # ── Pre-breakout sanity: LTP must be BELOW the buy trigger ──────────────
    # If LTP is already at or above buy_price the breakout already happened
    # but didn't meet confirmed criteria — drop it from Watch entirely.
    if status == "pre_breakout_watch" and live_price >= buy_price:
        return None

    # Also drop pre-breakout alerts where the trigger is more than 5% away —
    # those are too far from current price to be "near" breakout candidates.
    if status == "pre_breakout_watch" and buy_price > live_price * 1.05:
        return None

    expected_breakout_date = "Today / already triggered" if status == "confirmed" else \
        (dt.date.today() + dt.timedelta(days=1)).strftime("%d-%b-%Y")

    return {
        "symbol": symbol,
        "ltp": round(live_price, 2),
        "status": status,
        "strategy_used": " + ".join(strategies_used) if len(strategies_used) > 1 else strategies_used[0],
        "reasons": reasons,
        "buy_price": buy_price,
        "stop_loss": stop_loss,
        "target1": target1,
        "target2": target2,
        "risk_reward": f"1:{rr}",
        "confidence": confidence,
        "expected_breakout_date": expected_breakout_date,
    }
