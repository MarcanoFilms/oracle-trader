"""Grid acumulador de XBT: niveles geométricos + centro dinámico (EMA)."""


def geometric_levels(center, step_pct, band_pct, price_precision):
    """Precios de los niveles del grid, geométricos (paso % constante),
    dentro de ±band_pct alrededor del centro."""
    hi = center * (1 + band_pct)
    lo = center * (1 - band_pct)
    levels = []
    p = center
    while p <= hi:
        levels.append(round(p, price_precision))
        p *= (1 + step_pct)
    p = center / (1 + step_pct)
    while p >= lo:
        levels.append(round(p, price_precision))
        p /= (1 + step_pct)
    return sorted(set(levels))


def ema(values, period):
    """Media móvil exponencial sobre una serie (values en orden cronológico)."""
    if not values:
        return None
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def center_from_candles(candles, ema_periods):
    """Centro dinámico = EMA de los cierres. Devuelve (centro, último_precio)."""
    closes = [c["close"] for c in candles]
    if not closes:
        return None, None
    return ema(closes, ema_periods), closes[-1]
