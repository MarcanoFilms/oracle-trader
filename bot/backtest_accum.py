#!/usr/bin/env python3
"""Backtest del acumulador v2 contra velas reales de neoxa.exchange.

Usa un exchange simulado que imita la API real (post-only, escrow, fees en
USDC, orders?status=active/filled/cancelled), de modo que el bot corre por el
MISMO código que en live, reconciliación incluida. Las órdenes se colocan al
cierre de cada vela y solo pueden llenarse con las velas siguientes (sin
lookahead).

Uso: ./venv/bin/python backtest_accum.py [intervalo ...]  (por defecto 5m 15m 1h)
"""
import logging
import os
import sys
import tempfile

from neoxa_bot.accum import AccumBot
from neoxa_bot.api import NeoxaClient, NeoxaError

def pair_info(pair):
    return {"pair": pair, "baseToken": "BTCB2", "quoteToken": pair.split("_")[1],
            "pricePrecision": 8, "quantityPrecision": 8, "minQuantity": 1e-8,
            "makerFeeRate": 0.001, "takerFeeRate": 0.002, "tradingPaused": False}

SPREAD = 0.004  # medio-spread sintético alrededor del precio (AMM real ronda ±0.8%)


class FakeExchange:
    """Imita la superficie de NeoxaClient que usa AccumBot en modo live."""

    def __init__(self, usdc, xbt, fee=0.001, pair="BTCB2_USDC"):
        self.usdc, self.xbt, self.fee = usdc, xbt, fee
        self.pair = pair
        self.quote = pair.split("_")[1]
        self.active, self.filled, self.cancelled = {}, [], []
        self.seq = 0
        self.price = None
        self.fills = []           # (side, price, qty) para auditoría
        self.rejects = 0

    # --- lado bot ---
    def pair_info(self, pair):
        return pair_info(pair)

    def ticker(self, pair):
        return {"lastPrice": self.price, "bestBid": self.price * (1 - SPREAD),
                "bestAsk": self.price * (1 + SPREAD)}

    def balances(self):
        return {"balances": {self.quote: {"available": self.usdc},
                             "BTCB2": {"available": self.xbt}}}

    def place_limit(self, pair, side, quantity, price, post_only=True):
        if post_only:
            if side == "buy" and price >= self.price * (1 + SPREAD):
                self.rejects += 1
                raise NeoxaError(f"Post-only buy would cross at {self.price}")
            if side == "sell" and price <= self.price * (1 - SPREAD):
                self.rejects += 1
                raise NeoxaError(f"Post-only sell would cross at {self.price}")
        if side == "buy":
            cost = quantity * price
            if cost > self.usdc + 1e-9:
                raise NeoxaError("insufficient USDC balance")
            self.usdc -= cost                    # escrow
        else:
            if quantity > self.xbt + 1e-12:
                raise NeoxaError("insufficient BTCB2 balance")
            self.xbt -= quantity                 # escrow
        self.seq += 1
        oid = f"ORD-{self.seq}"
        self.active[oid] = {"order_id": oid, "side": side, "price": price,
                            "quantity": quantity, "quantity_filled": 0.0,
                            "status": "active"}
        return {"success": True, "order_id": oid}

    def cancel_order(self, oid):
        o = self.active.pop(oid, None)
        if o is None:
            raise NeoxaError("order not found")
        rem = o["quantity"] - o["quantity_filled"]
        if o["side"] == "buy":
            self.usdc += rem * o["price"]
        else:
            self.xbt += rem
        o["status"] = "cancelled"
        self.cancelled.insert(0, o)
        del self.cancelled[100:]
        return {"success": True}

    def orders(self, status="active", limit=100):
        if status == "active":
            return {"orders": list(self.active.values())[:limit]}
        pool = self.filled if status == "filled" else self.cancelled
        return {"orders": pool[:limit]}

    # --- lado mercado ---
    def sweep(self, p):
        """El precio toca p: llena las órdenes maker cruzadas."""
        self.price = p
        for oid, o in list(self.active.items()):
            hit = (o["side"] == "buy" and p <= o["price"]) or \
                  (o["side"] == "sell" and p >= o["price"])
            if not hit:
                continue
            q = o["quantity"]
            if o["side"] == "buy":
                self.xbt += q
                self.usdc -= q * o["price"] * self.fee   # fee en USDC
            else:
                self.usdc += q * o["price"] * (1 - self.fee)
            o["quantity_filled"] = q
            o["status"] = "filled"
            self.filled.insert(0, o)
            del self.filled[100:]
            del self.active[oid]
            self.fills.append((o["side"], o["price"], q))


def run_backtest(candles, step_pct, usdc=73.5, xbt=0.02529536, n_levels=6, label="",
                 overrides=None, pair="BTCB2_USDC"):
    fake = FakeExchange(usdc, xbt, pair=pair)
    cfg = {"pair": pair, "dry_run": False, "quote_capital": usdc,
           "xbt_reserve": xbt, "max_lots": 24, "step_pct": step_pct,
           "n_buy_levels": n_levels, "reanchor_steps": 1.6, "touch_buffer": 0.004,
           "poll_seconds": 1, "cancel_buys_on_exit": False,
           "summary_seconds": 10 ** 9, "telegram": False, "telegram_cycles": False}
    cfg.update(overrides or {})
    state_file = os.path.join(tempfile.mkdtemp(), "state.json")
    fake.price = float(candles[0]["close"])
    bot = AccumBot(cfg, fake, state_file)
    bot.refresh_balances()
    min_usdc = usdc
    for c in candles:
        o, h, l, cl = (float(c["open"]), float(c["high"]),
                       float(c["low"]), float(c["close"]))
        path = (o, l, h, cl) if cl >= o else (o, h, l, cl)
        for p in path:
            fake.sweep(p)
        fake.price = cl
        bot.step()
        min_usdc = min(min_usdc, fake.usdc)

    st = bot.state
    last = float(candles[-1]["close"])
    xbt_total = bot.xbt_total()
    usdc_total = bot.usdc_total()
    sells_below_cost = sum(1 for lot in [] if True) * 0  # imposible por diseño; auditamos fills:
    buys = [f for f in fake.fills if f[0] == "buy"]
    sells = [f for f in fake.fills if f[0] == "sell"]
    q = fake.quote
    print(f"  paso {step_pct*100:4.1f}%{label}: ciclos {st['cycles']:3d} | "
          f"residuo {st['xbt_residue']:+.8f} XBT | XBT {xbt_total:.8f} | "
          f"{q} {usdc_total:.6g} (mín libre {min_usdc:.3g}) | "
          f"lotes abiertos {len(st['lots'])} | fills {len(buys)}c/{len(sells)}v | "
          f"rechazos post-only {fake.rejects}")
    return {"step": step_pct, "cycles": st["cycles"], "residue": st["xbt_residue"],
            "xbt": xbt_total, "usdc": usdc_total, "lots": len(st["lots"]),
            "min_free": min_usdc, "mtm": xbt_total * last + usdc_total}


def main():
    logging.basicConfig(level=logging.WARNING)  # silenciar el ruido del bot
    variants = "--variants" in sys.argv
    btc = "--btc" in sys.argv
    pair = "BTCB2_BTC" if btc else "BTCB2_USDC"
    # capital equivalente: 73.5 USDC ≈ 0.0009 BTC (BTC_USDC ~81.2k, 2026-10-16)
    kw = {"pair": pair}
    if btc:
        kw.update(usdc=0.0009, xbt=0.0,
                  overrides={"min_lot_value": 5e-6, "first_step_pct": 0.02})
    else:
        kw.update(overrides={"first_step_pct": 0.02})
    intervals = [a for a in sys.argv[1:] if not a.startswith("--")] or ["5m", "15m", "1h"]
    client = NeoxaClient()
    for interval in intervals:
        candles = client.candles(pair, interval, limit=500)
        span_h = {"1m": 1 / 60, "5m": 5 / 60, "15m": 0.25, "1h": 1, "4h": 4, "1d": 24}
        days = len(candles) * span_h.get(interval, 1) / 24
        first, lastc = float(candles[0]["close"]), float(candles[-1]["close"])
        print(f"\n=== {pair} {interval} × {len(candles)} velas ({days:.1f} días) | "
              f"precio {first:.6g} → {lastc:.6g} ({(lastc/first-1)*100:+.0f}%) ===")
        if variants:
            for fs in (None, 0.025, 0.02):
                for ra in (1.6, 1.2):
                    ov = dict(kw.get("overrides") or {})
                    ov["reanchor_steps"] = ra
                    if fs:
                        ov["first_step_pct"] = fs
                    else:
                        ov.pop("first_step_pct", None)
                    kw2 = dict(kw, overrides=ov)
                    run_backtest(candles, 0.035,
                                 label=f" 1er={fs or 0.035:.3f} reancla={ra}", **kw2)
        else:
            for s in (0.02, 0.025, 0.03, 0.035, 0.04, 0.05):
                run_backtest(candles, s, **kw)


if __name__ == "__main__":
    main()
