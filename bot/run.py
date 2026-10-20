#!/usr/bin/env python3
"""Arranque del grid acumulador para neoxa.exchange."""
import argparse
import json
import logging
import os
import sys

import yaml

from neoxa_bot.api import NeoxaClient
from neoxa_bot.accum import AccumBot
from neoxa_bot.bot import GridBot

HERE = os.path.dirname(os.path.abspath(__file__))


def status_accum(st):
    quote = st['pair'].split('_')[1]
    print(f"par: {st['pair']}  modo: {'dry-run' if st['dry_run'] else 'LIVE'}  (acumulador v2)")
    in_sells = sum(l['sell_qty'] for l in st['lots'] if l.get('sell_oid'))
    in_buys = sum(b['qty'] * b['price'] for b in st['buys'])
    print(f"ciclos: {st['cycles']}  residuo acumulado: {st['xbt_residue']:+.8f} XBT")
    print(f"XBT total (libre+ventas): {st['free_xbt'] + in_sells:.8f}  "
          f"(inicio {st['base_start'] or 0:.8f}, reserva {st['reserve_xbt'] or 0:.8f})")
    print(f"{quote} total (libre+compras): {st['free_usdc'] + in_buys:.8g}  "
          f"(inicio {st['quote_start'] or 0:.8g})")
    print(f"\n{len(st['lots'])} lotes abiertos:")
    for l in sorted(st['lots'], key=lambda x: -x['buy_price']):
        estado = "venta activa" if l.get('sell_oid') else "SIN venta (recolocando)"
        print(f"   #{l['id']:<3} compró {l['qty']:.8f} @ {l['buy_price']:.8g} → "
              f"vende {l['sell_qty']:.8f} @ {l['sell_price']:.8g}  [{estado}]")
    print(f"\n{len(st['buys'])} compras en escalera:")
    for b in sorted(st['buys'], key=lambda x: -x['price']):
        print(f"   compra {b['qty']:.8f} @ {b['price']:.8g}")


def load_env(path):
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def main():
    ap = argparse.ArgumentParser(description="Grid acumulador para neoxa.exchange")
    ap.add_argument("--config", default=None,
                    help="ruta a config.yaml (por defecto la de la instancia)")
    ap.add_argument("--instance", default=None,
                    help="instancia multi-cuenta: usa instances/NOMBRE/{config.yaml,.env,state.json,logs}")
    ap.add_argument("--once", action="store_true", help="una sola iteración y salir")
    ap.add_argument("--status", action="store_true", help="muestra el estado y sale")
    args = ap.parse_args()

    # sin --instance todo vive en la raíz del proyecto (cuenta principal, como siempre)
    base = os.path.join(HERE, "instances", args.instance) if args.instance else HERE
    if args.instance and not os.path.isdir(base):
        sys.exit(f"instancia desconocida: no existe {base}")

    with open(args.config or os.path.join(base, "config.yaml")) as f:
        cfg = yaml.safe_load(f)

    # etiqueta para distinguir avisos de Telegram entre cuentas
    label = cfg.get("label") or args.instance
    if label:
        os.environ["BOT_LABEL"] = str(label)

    state_path = os.path.join(base, "state.json")
    if args.status:
        if not os.path.exists(state_path):
            print("sin estado todavía")
            return
        with open(state_path) as f:
            st = json.load(f)
        if st.get("strategy") == "accum":
            status_accum(st)
            return
        print(f"par: {st['pair']}  modo: {'dry-run' if st['dry_run'] else 'LIVE'}")
        print(f"centro: {st.get('center')}  niveles: {len(st.get('levels', []))}")
        print(f"ciclos: {st['cycles']}  XBT acumulado: {st['xbt_accum']:+.8f}")
        in_sell = sum(o['qty'] for o in st['orders'] if o['side'] == 'sell')
        print(f"XBT total (libre+órdenes): {st['free_xbt'] + in_sell:.8f}  (inicio {st['base_start']:.8f})")
        print(f"USDC libre: {st['free_usdc']:.2f}  (inicio {st['quote_start']:.2f})")
        buys = sorted([o for o in st['orders'] if o['side'] == 'buy'], key=lambda x: -x['price'])
        sells = sorted([o for o in st['orders'] if o['side'] == 'sell'], key=lambda x: x['price'])
        print(f"\n{len(sells)} ventas / {len(buys)} compras activas:")
        for o in sells[::-1]:
            print(f"   venta  {o['qty']:.8f} @ {o['price']:.4f}")
        for o in buys:
            print(f"   compra {o['qty']:.8f} @ {o['price']:.4f}")
        return

    os.makedirs(os.path.join(base, "logs"), exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(os.path.join(base, "logs", "bot.log"))]
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)

    env = load_env(os.path.join(base, ".env"))
    for k, v in env.items():
        os.environ.setdefault(k, v)
    key = env.get("NEOXA_API_KEY") or os.environ.get("NEOXA_API_KEY")
    secret = env.get("NEOXA_API_SECRET") or os.environ.get("NEOXA_API_SECRET")
    if not cfg["dry_run"] and not key:
        sys.exit("modo LIVE sin credenciales: rellena .env")

    client = NeoxaClient(key, secret)
    if cfg.get("strategy", "accum") == "accum":
        bot = AccumBot(cfg, client, state_path)
        if args.once:
            if not bot.dry:
                bot.refresh_balances()
            bot.step()
            print("--- iteración única completada ---")
            return
        bot.run()
        return
    bot = GridBot(cfg, client, state_path)
    if args.once:
        bot.center = bot.state.get("center")
        bot.levels = bot.state.get("levels", [])
        bot.maybe_recenter(force=True)
        bot.step()
        print("--- iteración única completada ---")
        return
    bot.run()


if __name__ == "__main__":
    main()
