#!/usr/bin/env python3
"""Tests del auto-sync de capital (_sync_capital): absorber depósitos solo.

Cuando el capital real de la cuenta (quote libre + escrow en compras + quote
invertida en lotes) supera el capital dinámico por el doble umbral, la
escalera se amplía sola. Nunca encoge, ignora el goteo del compound y se
puede apagar por config.

Sin red: cliente stub. Correr: ./venv/bin/python test_autosync.py
"""
import os
import tempfile

from neoxa_bot.accum import AccumBot

CFG = {"pair": "BTCB2_BTC", "dry_run": False, "step_pct": 0.025,
       "n_buy_levels": 10, "quote_capital": 0.004, "compound_pct": 0.5,
       "telegram": False, "telegram_cycles": False}


class StubClient:
    def pair_info(self, pair):
        return {"baseToken": "BTCB2", "quoteToken": "BTC", "pricePrecision": 8,
                "quantityPrecision": 8, "minQuantity": 0.0001,
                "makerFeeRate": 0.001, "tradingPaused": False}


def mk_bot(cfg_extra=None, free=0.0, buys=(), lots=(), dyn=None):
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    cfg = dict(CFG)
    cfg.update(cfg_extra or {})
    bot = AccumBot(cfg, StubClient(), state_path=tmp.name)
    bot.state["free_usdc"] = free
    bot.state["buys"] = [{"price": p, "qty": q, "oid": f"O{i}"}
                         for i, (p, q) in enumerate(buys)]
    bot.state["lots"] = [{"id": i, "buy_price": p, "qty": q, "sell_price": p * 1.025,
                          "sell_qty": q * 0.9989, "sell_oid": f"S{i}", "ts": 0,
                          "tr_hw": None} for i, (p, q) in enumerate(lots)]
    if dyn is not None:
        bot.state["quote_capital_dyn"] = dyn
    return bot


fails = 0


def check(name, cond):
    global fails
    print(("  ✓ " if cond else "  ✗ FALLO: ") + name)
    fails += 0 if cond else 1


def dyn_of(bot):
    return max(bot.state.get("quote_capital_dyn") or 0.0, float(bot.cfg["quote_capital"]))


# 1) depósito claro → absorbe hasta el capital real
print("1) depósito en quote libre → la escalera lo absorbe")
bot = mk_bot(free=0.003, lots=[(0.005, 0.4)])  # real = 0.003 + 0.002 = 0.005 > 0.004
bot._sync_capital()
check("dyn sube al capital real", abs(dyn_of(bot) - 0.005) < 1e-12)

# 2) el escrow de compras cuenta como capital (no dispara por sí solo)
print("2) capital ya presupuestado repartido entre libre/escrow/lotes → no dispara")
bot = mk_bot(free=0.0005, buys=[(0.004, 0.25)], lots=[(0.005, 0.5)])  # real = 0.004
bot._sync_capital()
check("dyn intacto en el config", abs(dyn_of(bot) - 0.004) < 1e-12)

# 3) goteo del compound (salto chico) → ignorado por el doble umbral
print("3) goteo bajo el umbral absoluto → ignorado")
bot = mk_bot(free=0.00405)  # +0.00005 < min_abs 0.0001
bot._sync_capital()
check("dyn intacto", abs(dyn_of(bot) - 0.004) < 1e-12)

print("4) salto absoluto pero < +2% relativo → ignorado")
bot = mk_bot(cfg_extra={"quote_capital": 0.02}, free=0.0202)  # +0.0002 pero +1%
bot._sync_capital()
check("dyn intacto", abs(dyn_of(bot) - 0.02) < 1e-12)

# 5) nunca encoge (retirada = decisión manual)
print("5) capital real por DEBAJO del trabajo → nunca encoge")
bot = mk_bot(free=0.001, dyn=0.006)  # real 0.001 << dyn 0.006
bot._sync_capital()
check("dyn intacto", abs(dyn_of(bot) - 0.006) < 1e-12)

# 6) apagado por config
print("6) capital_autosync: false → no hace nada")
bot = mk_bot(cfg_extra={"capital_autosync": False}, free=0.02)
bot._sync_capital()
check("dyn intacto", abs(dyn_of(bot) - 0.004) < 1e-12)

# 7) absorbe por encima del dyn ya crecido por compound, no del config
print("7) con dyn ya crecido por compound, el umbral es sobre dyn")
bot = mk_bot(free=0.0052, dyn=0.005)  # real 0.0052 = +4% sobre dyn → absorbe
bot._sync_capital()
check("dyn sube a 0.0052", abs(dyn_of(bot) - 0.0052) < 1e-12)
bot = mk_bot(free=0.00505, dyn=0.005)  # +1% sobre dyn → no
bot._sync_capital()
check("salto chico sobre dyn no dispara", abs(dyn_of(bot) - 0.005) < 1e-12)

print()
if fails:
    print(f"{fails} test(s) FALLARON")
    raise SystemExit(1)
print("todos los tests de auto-sync pasaron ✔")
