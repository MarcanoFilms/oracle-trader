#!/usr/bin/env python3
"""Test controlado del trailing take-profit — verifica el PISO SAGRADO.

No usa el exchange real: inyecta un lote y simula secuencias de bid, capturando
a qué precio habría vendido el bot. El invariante crítico: NINGUNA venta por
debajo del objetivo (costo×1.025), pase lo que pase.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neoxa_bot.accum import AccumBot


class MockClient:
    def pair_info(self, pair):
        return {"pair": pair, "baseToken": "BTCB2", "quoteToken": "BTC",
                "pricePrecision": 8, "quantityPrecision": 8, "minQuantity": 1e-8,
                "makerFeeRate": 0.001, "takerFeeRate": 0.002, "tradingPaused": False}


CFG = {"pair": "BTCB2_BTC", "dry_run": False, "quote_capital": 0.008,
       "xbt_reserve": 0, "step_pct": 0.025, "n_buy_levels": 10,
       "trail_enabled": True, "trail_pct": 0.01, "trail_activate": 0.005}


def run_scenario(name, target, bids, expect_capture, min_capture=None):
    bot = AccumBot(CFG, MockClient(), os.path.join(tempfile.mkdtemp(), "s.json"))
    bot.state["lots"] = [{"id": 1, "buy_price": target / 1.025, "qty": 1.0,
                          "sell_price": target, "sell_qty": 1.0,
                          "sell_oid": "BASE", "tr_hw": None, "compound_qty": 0.0,
                          "ts": 0}]
    sells = []
    bot._submit = lambda side, qty, price, post_only=True: (
        sells.append({"price": price, "taker": not post_only}) or f"OID{len(sells)}")
    bot._cancel_api = lambda oid: None
    for bid in bids:
        bot.bid = float(bid)
        bot._manage_trailing()
    ok = True
    # invariante crítico: NINGUNA venta por debajo del objetivo (piso sagrado)
    violations = [s for s in sells if s["price"] < target - 1e-12]
    if violations:
        ok = False
        print(f"  ✗ {name}: VIOLACIÓN DEL PISO — venta(s) bajo {target}: {violations}")
    # captura = venta taker (cruce) por encima del objetivo
    captures = [s for s in sells if s["taker"] and s["price"] > target * 1.0001]
    # nunca debe cruzar (taker) AL objetivo exacto (eso sería pagar taker por nada)
    taker_at_target = [s for s in sells if s["taker"] and abs(s["price"] - target) < target * 1e-4]
    if taker_at_target:
        ok = False
        print(f"  ✗ {name}: cruce taker al objetivo sin upside (debió ser maker): {taker_at_target}")
    if expect_capture:
        if not captures:
            ok = False
            print(f"  ✗ {name}: esperaba captura de upside y no hubo")
        elif min_capture and captures[-1]["price"] < min_capture - 1e-9:
            ok = False
            print(f"  ✗ {name}: capturó {captures[-1]['price']} < esperado >= {min_capture}")
    if ok:
        if captures:
            detail = f"capturó @ {captures[-1]['price']} (+{(captures[-1]['price']/target-1)*100:.1f}%)"
        else:
            detail = "vendió al objetivo (maker, sin taker)"
        print(f"  ✓ {name}: {detail} · piso {target} respetado")
    return ok


print("Test del trailing take-profit (objetivo = 100.0, trail 1%, activación 1%)\n")
allok = True
# 1) spike sostenido con reversión suave → captura cerca del pico (taker)
allok &= run_scenario("spike +5% y reversión",
                      100.0, [99, 100, 103, 105, 104.5, 103.9],
                      expect_capture=True, min_capture=103.0)
# 2) amago sin momentum → orden base al objetivo, nunca bajo, sin taker inútil
allok &= run_scenario("amago sin momentum",
                      100.0, [99, 99.4, 99.6, 99.2, 98.5],
                      expect_capture=False)
# 3) pico pequeño y COLAPSA → vende MAKER al objetivo (no taker), piso respetado
allok &= run_scenario("pico pequeño y colapso",
                      100.0, [99.6, 100.4, 100.6, 99, 96, 93],
                      expect_capture=False)
# 4) crash directo → nunca vende bajo objetivo, sin taker inútil
allok &= run_scenario("crash directo",
                      100.0, [99.6, 99.7, 98, 95, 90],
                      expect_capture=False)
# 5) spike enorme y reversión → captura mucho upside (taker a 113)
allok &= run_scenario("spike +15%",
                      100.0, [99.7, 101, 108, 115, 113, 113.8, 113.0],
                      expect_capture=True, min_capture=113.0)

print("\n" + ("✅ TODOS LOS TESTS PASARON — piso sagrado intacto" if allok
              else "❌ HAY FALLOS — NO desplegar"))
sys.exit(0 if allok else 1)
