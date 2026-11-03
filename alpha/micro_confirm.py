import asyncio


def clamp01(value):
    return max(0.0, min(1.0, value))


def norm(value, bounds):
    low, high = bounds
    return clamp01((value - low) / (high - low))


async def await_micro_confirmation(signal, market, cfg, timeout=None):
    timeout = cfg["confirm_timeout_sec"] if timeout is None else timeout
    deadline = market.now() + timeout
    while market.now() < deadline:
        sm = 1 if signal.side == "long" else -1
        if (market.cvd_30s() * sm > cfg["cvd_threshold"]
                and not market.detect_absorption(signal.side)
                and market.spread() < cfg["max_spread"]):
            return {"size_mult": 1.0, "confirmed": True}
        await asyncio.sleep(min(0.1, max(0.0, deadline - market.now())))
    return {"size_mult": cfg["unconfirmed_size_mult"], "confirmed": False}


def quality_gate(signal, ctx, cfg) -> float:
    score = (
        .30 * norm(signal.cascade_strength, cfg["cs_range"])
        + .25 * norm(signal.volume_ratio, cfg["vr_range"])
        + .20 * norm(signal.book_pressure, cfg["bp_range"])
        + .15 * clamp01(ctx.regime_fit)
        + .10 * clamp01(ctx.cross_venue_agreement)
    )
    if score >= .75:
        return 1.0
    if score >= .55:
        return .6
    if score >= .40:
        return .3
    return 0.0
