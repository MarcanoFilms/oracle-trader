#!/usr/bin/env python3
"""Tests de reconciliación: precio REAL de ejecución y lotes anti-huérfanos.

Cubre el fix de telemetría del trailing (2026-10-20): al cerrar un lote por
reconciliación, el precio registrado (trades/log/compound) debe ser el del
REGISTRO del exchange (avg_price o price de la orden que ejecutó: recolocación
sobre el objetivo o cruce de trailing), no el objetivo del lote. Y un cruce
cancelado externamente no puede dejar el lote huérfano (tr_hw colgado en -1).

Sin red: cliente stub. Correr: ./venv/bin/python test_reconcile.py
"""
import json
import os
import tempfile

from neoxa_bot.accum import AccumBot

CFG = {"pair": "BTCB2_BTC", "dry_run": False, "step_pct": 0.025,
       "n_buy_levels": 10, "quote_capital": 0.001, "compound_pct": 0.5,
       "trail_enabled": True, "telegram": False, "telegram_cycles": False}


class StubClient:
    def __init__(self, records):
        self.records = records

    def pair_info(self, pair):
        return {"baseToken": "BTCB2", "quoteToken": "BTC", "pricePrecision": 8,
                "quantityPrecision": 8, "minQuantity": 0.0001,
                "makerFeeRate": 0.001, "tradingPaused": False}

    def orders(self, status, limit=50):
        return {"orders": [r for r in self.records if r["status"] == status]}

    free_xbt = 0.0  # saldo XBT libre que reporta el "exchange"

    def balances(self):
        return {"balances": {"BTCB2": {"available": self.free_xbt},
                             "BTC": {"available": 0.001}}}

    def place_limit(self, pair, side, qty, price, post_only=True):
        from neoxa_bot.api import NeoxaError
        raise NeoxaError(f"Insufficient BTCB2 balance. Need {qty}, have {self.free_xbt}")


def mk_bot(records, lots):
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    bot = AccumBot(CFG, StubClient(records), state_path=tmp.name)
    bot.state["lots"] = lots
    bot.state["trades"] = []
    return bot


def lot(oid, buy=0.0065, target=0.0066625, qty=0.01, tr_hw=None):
    return {"id": 1, "buy_price": buy, "qty": qty, "sell_price": target,
            "sell_qty": qty * 0.9989, "sell_oid": oid, "ts": 0, "tr_hw": tr_hw}


fails = 0


def check(name, cond):
    global fails
    print(("  ✓ " if cond else "  ✗ FALLO: ") + name)
    fails += 0 if cond else 1


# 1) fill de una RECOLOCACIÓN sobre el objetivo → registra el precio real
l = lot("A", tr_hw=None)
b = mk_bot([{"order_id": "A", "status": "filled", "quantity_filled": l["sell_qty"],
             "price": 0.0070, "avg_price": None}], [l])
b._resolve_missing([], [l])
tr = b.state["trades"][-1]
check("recolocación: trade registra 0.0070 (real), no 0.0066625 (objetivo)",
      abs(tr["price"] - 0.0070) < 1e-12 and not b.state["lots"])

# 2) avg_price (si el exchange lo trae) tiene prioridad sobre price
l = lot("B", tr_hw=-1)  # cruce de trailing emitido
b = mk_bot([{"order_id": "B", "status": "filled", "quantity_filled": l["sell_qty"],
             "price": 0.0069, "avg_price": 0.00695}], [l])
b._resolve_missing([], [l])
check("cruce trailing: usa avg_price 0.00695 sobre price 0.0069",
      abs(b.state["trades"][-1]["price"] - 0.00695) < 1e-12)

# 3) registro sin precios → fallback al objetivo (comportamiento previo)
l = lot("C")
b = mk_bot([{"order_id": "C", "status": "filled", "quantity_filled": l["sell_qty"],
             "price": None, "avg_price": None}], [l])
b._resolve_missing([], [l])
check("fallback: sin price/avg_price registra el objetivo",
      abs(b.state["trades"][-1]["price"] - l["sell_price"]) < 1e-12)

# 4) ANTI-HUÉRFANO: cruce de trailing cancelado externamente → lote vuelve a
#    gestión normal (tr_hw limpio) y ensure_sells le recolocaría la venta
l = lot("D", tr_hw=-1)
b = mk_bot([{"order_id": "D", "status": "cancelled", "quantity_filled": 0,
             "price": 0.0069, "avg_price": None}], [l])
b._resolve_missing([], [l])
check("cruce cancelado: sell_oid=None y tr_hw=None (no queda huérfano)",
      l["sell_oid"] is None and l["tr_hw"] is None and b.state["lots"] == [l])

# 5) fill PARCIAL con precio real: cierra lo vendido al precio real y el resto
#    vuelve a gestión normal
l = lot("E", tr_hw=-1)
half = l["sell_qty"] / 2
b = mk_bot([{"order_id": "E", "status": "cancelled", "quantity_filled": half,
             "price": 0.0070, "avg_price": None}], [l])
b._resolve_missing([], [l])
check("parcial: trade al precio real y resto sin huérfano",
      abs(b.state["trades"][-1]["price"] - 0.0070) < 1e-12
      and l["tr_hw"] is None and l["sell_oid"] is None
      and abs(l["sell_qty"] - half) < 1e-9)

# 6) el compound usa el precio real (más BTC cosechado en cierres sobre objetivo)
l = lot("F")
l["compound_qty"] = l["sell_qty"] / 2
b = mk_bot([{"order_id": "F", "status": "filled", "quantity_filled": l["sell_qty"],
             "price": 0.0070, "avg_price": None}], [l])
b._resolve_missing([], [l])
esperado = l["compound_qty"] * 0.0070 * (1 - 0.001)
check("compound cosecha al precio real",
      abs(b.state["compound_harvested"] - esperado) < 1e-12)

# 7) venta SIN REGISTRO cuyo XBT ya no está (se llenó fuera de la ventana del
#    historial) → se cierra al objetivo, NO se recoloca con XBT del stack.
#    Caso real en producción 2026-10-31: 7 lotes fantasma, residuo inflado y lotes
#    nuevos sin XBT con que vender (~3000 rechazos/hora).
def setup_unknown(lost):
    """lost=True: el XBT del lote desconocido ya no está en el saldo."""
    gone = lot("G", qty=0.04)
    gone["id"] = 7
    other = lot("H", qty=0.05)      # otra venta viva, en escrow
    other["id"] = 8
    b = mk_bot([], [gone, other])
    b.state["reserve_xbt"] = 0.0
    b.state["base_start"] = 0.0     # ya arrancado (si no, refresh fija la reserva)
    b.state["xbt_residue"] = 0.07   # stack libre
    b.client.free_xbt = 0.07 + (0.05 - other["sell_qty"]) + (0 if lost else gone["qty"])
    return b, gone, other

b, gone, other = setup_unknown(lost=True)
b._resolve_missing([], [gone])
check("sin registro + XBT ausente: lote cerrado, no recolocado",
      gone not in b.state["lots"] and other in b.state["lots"]
      and b.state["cycles"] == 1 and b.state["trades"][-1]["lot"] == 7)

# 8) venta SIN REGISTRO cuyo XBT sigue libre (cancelada fuera de la ventana) →
#    se recoloca como antes
b, gone, other = setup_unknown(lost=False)
b._resolve_missing([], [gone])
check("sin registro + XBT libre: lote vivo y a recolocar",
      gone in b.state["lots"] and gone["sell_oid"] is None and b.state["cycles"] == 0)

# 9) sin saldo para la venta → backoff de 5 min, no reintenta cada tick
l = lot(None, qty=0.08)
b = mk_bot([], [l])
b.ask = None
calls = []
orig = b.client.place_limit
b.client.place_limit = lambda *a, **k: (calls.append(1), orig(*a, **k))
b.ensure_sells()
b.ensure_sells()
check("Insufficient: 1 intento y retry_ts a futuro (no martilla)",
      len(calls) == 1 and l.get("retry_ts", 0) > __import__("time").time() + 200)

# 10) el historial no responde → reconcile() devuelve False y step() NO corre el
#     auto-sync (caso real en producción 2026-11-02: 2 compras canceladas seguían en
#     buys[] y su escrow se sumó al BTC ya libre → +0.000814 BTC fantasma)
from neoxa_bot.api import NeoxaError


class FlakyClient(StubClient):
    def orders(self, status, limit=50):
        if status == "active":
            return {"orders": []}
        raise NeoxaError("error de red: Read timed out")


b = mk_bot([], [])
b.client = FlakyClient([])
b.state["buys"] = [{"price": 0.0045, "qty": 0.09, "oid": "X"}]
b.state["free_usdc"] = 0.001  # incluye ya el escrow devuelto de X
check("historial caído: reconcile() = False (estado no limpio)", b.reconcile() is False)
synced = []
b._sync_capital = lambda: synced.append(1)
b.client.ticker = lambda pair: {"lastPrice": 0.005, "bestBid": 0.0049, "bestAsk": 0.0051}
b.client.balances = lambda: {"balances": {"BTC": {"available": 0.001}, "BTCB2": {"available": 0}}}
b.ensure_sells = b._manage_trailing = b.ladder = b._save = lambda: None
b.state["last_summary_ts"] = 2e9
b.step()
check("historial caído: step() no corre el auto-sync", not synced)

# 11) POST sin respuesta (timeout) pero la orden SÍ se creó → se adopta, no se
#     reenvía (caso real en producción 2026-11-02: 1 venta + 2 compras duplicadas fantasma)
import neoxa_bot.accum as accum_mod
from neoxa_bot.api import NeoxaUncertain
accum_mod.time.sleep = lambda s: None  # sin esperas en el test


class TimeoutClient(StubClient):
    def __init__(self, created):
        super().__init__([])
        self.created, self.posts = created, 0

    def place_limit(self, pair, side, qty, price, post_only=True):
        self.posts += 1
        if self.created:
            self.records.append({"order_id": "GHOST", "status": "active", "side": side,
                                 "quantity": qty, "price": price})
        raise NeoxaUncertain("error de red: Read timed out")


l = lot(None, qty=0.05)
b = mk_bot([], [l]); b.client = TimeoutClient(created=True); b.ask = None
b.ensure_sells()
check("timeout con orden creada: adoptada (sell_oid=GHOST) y 1 solo POST",
      l["sell_oid"] == "GHOST" and b.client.posts == 1)
l = lot(None, qty=0.05)
b = mk_bot([], [l]); b.client = TimeoutClient(created=False); b.ask = None
b.ensure_sells()
check("timeout sin orden creada: sin oid, se reintentará en otro tick",
      l["sell_oid"] is None and b.client.posts == 1)

# 12) el cliente HTTP no reintenta un POST de orden tras error de red
import requests
from neoxa_bot.api import NeoxaClient
nc = NeoxaClient("k", "s"); nc.min_interval = 0; n = []
def boom(*a, **k):
    n.append(1); raise requests.exceptions.ReadTimeout("timed out")
nc.s.request = boom
try:
    nc.place_limit("BTCB2_BTC", "sell", 0.01, 0.005)
except NeoxaUncertain:
    pass
check("API: POST de orden con timeout → 1 intento y NeoxaUncertain", len(n) == 1)

print()
if fails:
    raise SystemExit(f"❌ {fails} test(s) fallaron")
print("✅ TODOS LOS TESTS DE RECONCILIACIÓN PASARON")
