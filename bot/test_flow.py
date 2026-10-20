#!/usr/bin/env python3
"""Prueba del ciclo completo con un mercado sintético (sin tocar la API real)."""
import math
import os
import tempfile

import yaml

from neoxa_bot.bot import Bot, BUY_OPEN, SELL_OPEN, EMPTY


class FakeClient:
    """Mercado lateral sintético. Los métodos autenticados explotan a propósito:
    en dry-run el bot jamás debe llamarlos."""

    def __init__(self):
        self.price = 100.0

    def pair_info(self, pair):
        return {"pair": pair, "baseToken": "BTCB2", "quoteToken": "USDC",
                "minQuantity": 1e-8, "maxQuantity": 1e6,
                "pricePrecision": 8, "quantityPrecision": 8,
                "makerFeeRate": 0.001, "takerFeeRate": 0.002,
                "tradingPaused": False}

    def candles(self, pair, interval, limit):
        return [{"close": 100 + 5 * math.sin(i / 3.0)} for i in range(96)]

    def ticker(self, pair):
        return {"lastPrice": self.price}

    def __getattr__(self, name):
        raise AssertionError(f"¡llamada autenticada en dry-run!: {name}")


def main():
    with open(os.path.join(os.path.dirname(__file__), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    cfg.update(dry_run=True, order_levels=2, quote_allocation=100, take_profit=0.01)

    state = os.path.join(tempfile.mkdtemp(), "state.json")
    client = FakeClient()
    bot = Bot(cfg, client, state)
    bot.refresh_range()
    assert bot.rng.ranging, f"debería detectar rango: {bot.rng.reason}"
    print(f"[ok] rango detectado: {bot.rng.reason}")

    bot.step()
    buys = [s for s in bot.state["slots"] if s["status"] == BUY_OPEN]
    assert len(buys) == 2, f"esperaba 2 compras, hay {len(buys)}"
    print(f"[ok] compras colocadas en: {[s['buy_price'] for s in buys]}")

    client.price = min(s["buy_price"] for s in buys) - 0.1
    bot.step()
    sells = [s for s in bot.state["slots"] if s["status"] == SELL_OPEN]
    assert len(sells) == 2, f"esperaba 2 ventas tras los fills, hay {len(sells)}"
    print(f"[ok] compras ejecutadas y ventas colocadas en: {[s['sell_price'] for s in sells]}")

    client.price = max(s["sell_price"] for s in sells) + 0.1
    bot.step()
    assert all(s["status"] in (EMPTY, BUY_OPEN) for s in bot.state["slots"])
    assert bot.state["round_trips"] == 2
    assert bot.state["realized_pnl"] > 0, f"PnL {bot.state['realized_pnl']}"
    print(f"[ok] ventas ejecutadas, PnL realizado: {bot.state['realized_pnl']:.4f} USDC "
          f"en {bot.state['round_trips']} round-trips")

    # ruptura bajista → stop-loss
    client.price = 100.0
    bot.step()                      # recoloca compras
    client.price = min(s["buy_price"] for s in bot.state["slots"]
                       if s["buy_price"]) - 0.1
    bot.step()                      # fills → ventas abiertas
    client.price = bot.rng.low * 0.985   # bajo la banda - breakout_pct
    for _ in range(3):
        bot.step()
    assert bot.state["paused"] == "ruptura bajista", bot.state["paused"]
    assert all(s["status"] == EMPTY for s in bot.state["slots"])
    assert bot.state["sim_base"] == 0, f"inventario sin liquidar: {bot.state['sim_base']}"
    print(f"[ok] ruptura bajista: pausado y liquidado (PnL total {bot.state['realized_pnl']:.4f})")

    # recuperación → reanuda
    client.price = 100.0
    bot.refresh_range()
    bot.step()
    assert not bot.state["paused"], f"sigue pausado: {bot.state['paused']}"
    assert any(s["status"] == BUY_OPEN for s in bot.state["slots"])
    print("[ok] rango recuperado: reanudó y recolocó compras")
    print("\nTODO OK — máquina de estados validada")


if __name__ == "__main__":
    main()
