#!/usr/bin/env python3
"""Validación del grid acumulador con un mercado sintético (sin tocar la API real)."""
import os
import tempfile

import yaml

from neoxa_bot.bot import GridBot


class FakeClient:
    """Precio y centro controlables. Los métodos autenticados explotan: en dry-run
    el bot nunca debe llamarlos."""

    def __init__(self):
        self.price = 720.0
        self.center_price = 720.0

    def pair_info(self, pair):
        return {"pair": pair, "baseToken": "BTCB2", "quoteToken": "USDC",
                "minQuantity": 1e-8, "maxQuantity": 1e6,
                "pricePrecision": 8, "quantityPrecision": 8,
                "makerFeeRate": 0.001, "takerFeeRate": 0.002, "tradingPaused": False}

    def candles(self, pair, interval, limit):
        return [{"close": self.center_price} for _ in range(limit)]

    def ticker(self, pair):
        return {"lastPrice": self.price}

    def __getattr__(self, name):
        raise AssertionError(f"¡llamada autenticada en dry-run!: {name}")


def load_cfg():
    with open(os.path.join(os.path.dirname(__file__), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    cfg.update(dry_run=True, quote_capital=49.0, base_capital=0.07,
               center_mode="ema", poll_seconds=0, recenter_seconds=0)
    return cfg


def main():
    cfg = load_cfg()
    state = os.path.join(tempfile.mkdtemp(), "state.json")
    c = FakeClient()
    bot = GridBot(cfg, c, state)
    bot.maybe_recenter(force=True)
    assert bot.center and abs(bot.center - 720) < 1, bot.center
    print(f"[ok] centro EMA = {bot.center:.2f}, {len(bot.levels)} niveles: "
          f"{bot.levels[0]:.1f} … {bot.levels[-1]:.1f}")

    bot.step()
    buys = [o for o in bot.state["orders"] if o["side"] == "buy"]
    sells = [o for o in bot.state["orders"] if o["side"] == "sell"]
    assert buys and sells, f"faltan órdenes: {len(buys)} compras {len(sells)} ventas"
    assert all(o["price"] < bot.last for o in buys) and all(o["price"] > bot.last for o in sells)
    print(f"[ok] grid simétrico colocado: {len(sells)} ventas arriba / {len(buys)} compras abajo")
    print(f"      valor por orden ≈ {bot._order_value():.2f} USDC")

    def oscillate(n):
        for _ in range(n):
            c.price = 680.0; bot.step()   # abajo: llena compras
            c.price = 758.0; bot.step()   # arriba: llena ventas
            c.price = 720.0; bot.step()   # vuelta al centro
    # 2 ciclos de calentamiento (transitorio: el grid vende/compra el capital inicial
    # hasta encontrar su régimen), luego medir la acumulación en el MISMO punto del ciclo.
    oscillate(2)
    xbt0 = bot.xbt_total()
    oscillate(10)
    xbt1 = bot.xbt_total()
    per_cycle = (xbt1 - xbt0) / 10
    print(f"[ok] en régimen, medido a precio=720: XBT {xbt0:.8f} → {xbt1:.8f} "
          f"(+{xbt1-xbt0:.8f} en 10 ciclos = {per_cycle:.8f}/ciclo)")
    assert xbt1 > xbt0, f"debería ACUMULAR XBT en régimen: {xbt0} -> {xbt1}"
    # la acumulación por ciclo debe rondar V/P * step/(1+step)
    expected = bot._order_value() / 720 * (0.04 / 1.04)
    print(f"      acumulación esperada ≈ {expected:.8f}/ciclo (teórica)")
    print(f"      USDC libre: {bot.state['free_usdc']:.2f}")

    # re-centrado: el mercado se muda a una zona más alta
    c.center_price = 820.0
    c.price = 820.0
    old_top = bot.levels[-1]
    for _ in range(3):
        bot.step()
    assert bot.levels[-1] > old_top, f"el grid debería deslizarse arriba: {old_top} -> {bot.levels[-1]}"
    assert bot.center > 760, bot.center
    print(f"[ok] re-centrado dinámico: centro → {bot.center:.1f}, "
          f"niveles {bot.levels[0]:.1f} … {bot.levels[-1]:.1f}")

    # tope de inventario: no acumular XBT sin límite
    assert bot.xbt_total() <= cfg["max_base_inventory"] + 1e-6
    print(f"[ok] inventario XBT {bot.xbt_total():.6f} bajo el tope {cfg['max_base_inventory']}")
    print("\nTODO OK — grid acumulador validado")


if __name__ == "__main__":
    main()
