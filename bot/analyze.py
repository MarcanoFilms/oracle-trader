#!/usr/bin/env python3
"""Cuaderno de aprendizaje del acumulador: estadísticas de los datos REALES.

Lee state.json (+ archivados state-accum-*.json) y el mercado actual, y responde:
ritmo de ciclos y residuo/día, duración de ciclos, a qué profundidad llenan las
compras, cuánto compound se cosechó, y cómo compara el régimen actual de
volatilidad con el que calibró los parámetros.

Uso: ./venv/bin/python analyze.py
"""
import glob
import json
import os
import time
from datetime import datetime, timezone

from neoxa_bot.api import NeoxaClient

HERE = os.path.dirname(os.path.abspath(__file__))


def load_states():
    states = []
    for path in [os.path.join(HERE, "state.json")] + \
            sorted(glob.glob(os.path.join(HERE, "state-accum-*.json"))):
        if not os.path.exists(path) or path.endswith("pre-fix-backup.json"):
            continue
        try:
            st = json.load(open(path))
        except Exception:
            continue
        if st.get("strategy") == "accum":
            states.append((os.path.basename(path), st))
    return states


def fmt_dur(s):
    if s < 3600:
        return f"{s/60:.0f}min"
    if s < 86400:
        return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


def analyze_state(name, st):
    pair = st["pair"]
    trades = st.get("trades", [])
    sells = [t for t in trades if t["side"] == "sell"]
    buys = [t for t in trades if t["side"] == "buy"]
    print(f"\n━━ {name} [{pair}] ━━")
    print(f"ciclos {st['cycles']} | residuo {st['xbt_residue']:+.8f} XBT | "
          f"compound {st.get('compound_harvested') or 0:.8g} {pair.split('_')[1]} "
          f"(capital dinámico {st.get('quote_capital_dyn') or '—'})")
    if not trades:
        return
    t0 = min(t["ts"] for t in trades)
    span_d = max((time.time() - t0) / 86400, 1e-9) if name == "state.json" \
        else max((max(t["ts"] for t in trades) - t0) / 86400, 1e-9)
    print(f"ventana: {span_d:.1f} días desde {datetime.fromtimestamp(t0, tz=timezone.utc):%m-%d %H:%M}Z | "
          f"{len(buys)} compras, {len(sells)} ventas ejecutadas")
    print(f"ritmo: {st['cycles']/span_d:.1f} ciclos/día | "
          f"{st['xbt_residue']/span_d:+.8f} XBT/día de residuo")
    # duración de ciclos (ventas con telemetría de lote)
    durs = [t["ts"] - t["buy_ts"] for t in sells if t.get("buy_ts")]
    if durs:
        durs.sort()
        print(f"duración ciclo: mediana {fmt_dur(durs[len(durs)//2])} | "
              f"min {fmt_dur(durs[0])} | max {fmt_dur(durs[-1])}")
    # margen real de las ventas con buy_price registrado
    margins = [(t["price"] / t["buy_price"] - 1) * 100 for t in sells if t.get("buy_price")]
    if margins:
        print(f"margen venta/costo: {sum(margins)/len(margins):.2f}% promedio "
              f"({len(margins)} ciclos con telemetría)")
    # lotes abiertos y su edad
    now = time.time()
    for l in st.get("lots", []):
        age = fmt_dur(now - l.get("ts", now))
        print(f"  lote #{l['id']} abierto hace {age}: {l['qty']:.8f} @ {l['buy_price']:.8g} "
              f"→ espera {l['sell_price']:.8g}")


def market_regime():
    c = NeoxaClient()
    print("\n━━ régimen actual del mercado (BTCB2_BTC) ━━")
    for interval, n, label in (("1h", 48, "48h"), ("15m", 96, "últimas 24h")):
        candles = c.candles("BTCB2_BTC", interval, limit=n)
        if not candles:
            continue
        closes = [float(x["close"]) for x in candles]
        rng = [(float(x["high"]) - float(x["low"])) / float(x["close"]) * 100
               for x in candles if float(x["close"])]
        moves = [abs(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
        print(f"[{label}] rango medio/vela {sum(rng)/len(rng):.2f}% | "
              f"movimiento medio entre velas {sum(moves)/len(moves):.2f}% | "
              f"precio {closes[0]:.6g} → {closes[-1]:.6g} ({(closes[-1]/closes[0]-1)*100:+.1f}%)")
    tk = c.ticker("BTCB2_BTC")
    spread = (float(tk["bestAsk"]) / float(tk["bestBid"]) - 1) * 100
    print(f"ahora: {float(tk['lastPrice']):.6g} | spread {spread:.2f}% | "
          f"trades24h {tk.get('trades24h')} | vol24h {float(tk.get('volume24h') or 0):.1f} XBT")
    # señal rápida de calibración: paso vs movimiento típico
    if moves:
        avg_move = sum(moves) / len(moves)
        print(f"señal: paso 2.5% vs movimiento 15m medio {avg_move:.2f}% → "
              f"~{2.5/max(avg_move,1e-9):.1f} velas para cruzar un nivel")


if __name__ == "__main__":
    for name, st in load_states():
        analyze_state(name, st)
    market_regime()
