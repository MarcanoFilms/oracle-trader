"""Grid acumulador de XBT para neoxa.exchange.

Niveles geométricos alrededor de un centro dinámico (EMA). Cada orden vale lo
mismo en USDC (V). Emparejamiento event-driven: al llenarse una COMPRA en P se
coloca una VENTA en el nivel inmediatamente superior (vende V/P_up XBT, menos de
lo comprado V/P → retiene la diferencia en XBT); al llenarse una VENTA en P se
coloca una COMPRA en el nivel inferior. Así cada oscilación deja XBT en el
bolsillo y el capital de trabajo en USDC se mantiene. El grid se desliza cuando
el centro (EMA) se mueve: se siembran los niveles nuevos y se cancelan los que
quedan fuera de la banda.
"""
import json
import logging
import os
import signal
import time

from .api import NeoxaError, find_key, find_list
from .strategy import center_from_candles, geometric_levels

log = logging.getLogger("neoxa_bot")


class GridBot:
    def __init__(self, cfg, client, state_path="state.json"):
        self.cfg = cfg
        self.client = client
        self.state_path = state_path
        self.pair = cfg["pair"]
        self.dry = cfg["dry_run"]
        info = client.pair_info(self.pair)
        if info.get("tradingPaused"):
            raise NeoxaError(f"el par {self.pair} está pausado: {info.get('pauseReason')}")
        self.info = info
        self.base, self.quote = info["baseToken"], info["quoteToken"]
        self.pp = int(info["pricePrecision"])
        self.qp = int(info["quantityPrecision"])
        self.min_qty = float(info["minQuantity"])
        self.maker_fee = float(info["makerFeeRate"])
        self.center = None
        self.levels = []
        self.last = None
        self._sim_id = 0
        self._next_recenter = 0.0
        self.state = self._load_state()

    # ---------------- estado ----------------
    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                st = json.load(f)
            if st.get("pair") == self.pair and st.get("dry_run") == self.dry \
                    and st.get("strategy") == "grid":
                log.info("estado grid previo cargado: %d órdenes, %d ciclos, XBT acum %.8f",
                         len(st["orders"]), st["cycles"], st["xbt_accum"])
                return st
            log.warning("estado previo incompatible; empezando de cero")
        return {
            "strategy": "grid", "pair": self.pair, "dry_run": self.dry,
            "orders": [],                 # [{price, side, qty, order_id}]
            "free_usdc": float(self.cfg["quote_capital"]),
            "free_xbt": float(self.cfg["base_capital"]),
            "base_start": float(self.cfg["base_capital"]),
            "quote_start": float(self.cfg["quote_capital"]),
            "cycles": 0, "fees_usdc": 0.0, "xbt_accum": 0.0,
            "center": None, "levels": [], "order_value": 0.0, "trades": [],
        }

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)

    # ---------------- centro / niveles ----------------
    def compute_center(self):
        if self.cfg["center_mode"] == "fixed":
            return float(self.cfg["fixed_center"])
        candles = self.client.candles(self.pair, self.cfg["candle_interval"],
                                      limit=max(self.cfg["ema_periods"] + 5, 50))
        c, _ = center_from_candles(candles, self.cfg["ema_periods"])
        return c

    def _order_value(self):
        if self.state.get("order_value", 0) > 0:
            return self.state["order_value"]
        # cuenta niveles de cada lado excluyendo el nivel pegado al centro (zona muerta),
        # para que el capital se reparta entre los niveles que de verdad llevan orden
        half = self.center * self.cfg["step_pct"] / 2
        n_buy = sum(1 for p in self.levels if p < self.center - half)
        n_sell = sum(1 for p in self.levels if p > self.center + half)
        # 97% del capital: deja colchón para escrow/fees/redondeo, si no la última
        # orden de cada lado se rechaza por "insufficient balance" por centavos
        safe = 0.97
        base_val = float(self.cfg["base_capital"]) * (self.center or 1)
        v = min(safe * float(self.cfg["quote_capital"]) / max(n_buy, 1),
                safe * base_val / max(n_sell, 1))
        self.state["order_value"] = v
        return v

    def maybe_recenter(self, force=False):
        now = time.monotonic()
        if not force and now < self._next_recenter and self.levels:
            return
        self._next_recenter = now + self.cfg["recenter_seconds"]
        try:
            new_center = self.compute_center()
        except NeoxaError as e:
            log.warning("no pude calcular el centro: %s", e)
            return
        if not new_center:
            return
        moved = self.center is None or \
            abs(new_center - self.center) > self.center * self.cfg["step_pct"] * self.cfg["recenter_step_frac"]
        if not moved:
            return
        old = set(self.levels)
        self.center = new_center
        self.levels = geometric_levels(new_center, self.cfg["step_pct"],
                                       self.cfg["band_pct"], self.pp)
        self.state["center"] = new_center
        self.state["levels"] = self.levels
        log.info("centro → %.4f | niveles %.4f … %.4f (%d)",
                 new_center, self.levels[0], self.levels[-1], len(self.levels))
        # cancelar órdenes que quedaron fuera de la banda
        valid = set(self.levels)
        for o in list(self.state["orders"]):
            if round(o["price"], self.pp) not in valid:
                self._cancel(o)
                self.state["orders"].remove(o)
        # sembrar los niveles nuevos (los que no existían antes)
        if self.last:
            self._seed([p for p in self.levels if p not in old])

    def level_above(self, p):
        for x in self.levels:
            if x > p + 10 ** (-self.pp):
                return x
        return None

    def level_below(self, p):
        for x in reversed(self.levels):
            if x < p - 10 ** (-self.pp):
                return x
        return None

    # ---------------- balances ----------------
    def _refresh_live_balances(self):
        data = self.client.balances().get("balances", {})
        def avail(tok):
            v = data.get(tok, {})
            return float(v.get("available", 0)) if isinstance(v, dict) else float(v or 0)
        self.state["free_usdc"] = avail(self.quote)
        self.state["free_xbt"] = avail(self.base)

    def xbt_total(self):
        in_sell = sum(o["qty"] for o in self.state["orders"] if o["side"] == "sell")
        return self.state["free_xbt"] + in_sell

    def _has_order(self, price):
        pk = round(price, self.pp)
        return any(round(o["price"], self.pp) == pk for o in self.state["orders"])

    # ---------------- colocación ----------------
    def _submit(self, side, qty, price):
        if self.dry:
            self._sim_id += 1
            return f"SIM-{self._sim_id}"
        try:
            resp = self.client.place_limit(self.pair, side, qty, price,
                                           post_only=self.cfg["post_only"])
        except NeoxaError as e:
            log.warning("orden %s %.8f @ %.4f rechazada: %s", side, qty, price, e)
            return None
        return find_key(resp, "order_id", "orderId", "id")

    def _cancel(self, o):
        if not self.dry and o["order_id"] and not str(o["order_id"]).startswith("SIM-"):
            try:
                self.client.cancel_order(o["order_id"])
            except NeoxaError as e:
                log.warning("no pude cancelar %s: %s", o["order_id"], e)
        if o["side"] == "buy":
            self.state["free_usdc"] += o["qty"] * o["price"]
        else:
            self.state["free_xbt"] += o["qty"]

    def place_buy(self, price, value):
        if self._has_order(price):
            return
        qty = round(min(value, self.state["free_usdc"]) / price, self.qp)
        if qty < self.min_qty or qty * price > self.state["free_usdc"] + 1e-9:
            return
        oid = self._submit("buy", qty, price)
        if oid:
            self.state["free_usdc"] -= qty * price
            self.state["orders"].append({"price": price, "side": "buy", "qty": qty, "order_id": oid})
            log.info("compra colocada: %.8f %s @ %.4f", qty, self.base, price)

    def place_sell(self, price, value):
        if self._has_order(price):
            return
        qty = round(value / price, self.qp)
        if qty < self.min_qty or qty > self.state["free_xbt"] + 1e-12:
            return
        oid = self._submit("sell", qty, price)
        if oid:
            self.state["free_xbt"] -= qty
            self.state["orders"].append({"price": price, "side": "sell", "qty": qty, "order_id": oid})
            log.info("venta colocada: %.8f %s @ %.4f", qty, self.base, price)

    def _seed(self, levels):
        """Siembra niveles vacíos: compra si está bajo el precio, venta si encima
        (solo arranque inicial y niveles nuevos del re-centrado)."""
        v = self._order_value()
        dz = self.cfg["dead_zone"] * self.cfg["step_pct"]
        buy_ceil = self.last * (1 - dz)
        sell_floor = self.last * (1 + dz)
        max_xbt = float(self.cfg["max_base_inventory"])
        for p in sorted([x for x in levels if x <= buy_ceil], reverse=True):
            if self.xbt_total() + v / p <= max_xbt:
                self.place_buy(p, v)
        for p in sorted([x for x in levels if x >= sell_floor]):
            self.place_sell(p, v)

    # ---------------- fills (event-driven) ----------------
    def _on_fill(self, side, price, qty):
        v = self._order_value()
        if side == "buy":
            self.state["free_xbt"] += qty           # recibimos el XBT comprado
            up = self.level_above(price)
            if up:
                self.place_sell(up, v)              # vende V/up < qty → retiene XBT
        else:
            self.state["free_usdc"] += qty * price * (1 - self.maker_fee)
            self.state["fees_usdc"] += qty * price * self.maker_fee
            dn = self.level_below(price)
            if dn:
                self.place_buy(dn, v)
        self.state["cycles"] += 1
        self.state["xbt_accum"] = self.xbt_total() - self.state["base_start"]
        self.state["trades"].append({"ts": int(time.time()), "side": side, "price": price, "qty": qty})
        self.state["trades"] = self.state["trades"][-500:]
        log.info("%s ejecutada: %.8f %s @ %.4f | XBT total %.8f (acum %+.8f) USDC %.2f",
                 side.upper(), qty, self.base, price, self.xbt_total(),
                 self.state["xbt_accum"], self.state["free_usdc"])

    def sync_fills(self):
        if self.dry:
            for o in [x for x in self.state["orders"]
                      if (x["side"] == "buy" and self.last <= x["price"])
                      or (x["side"] == "sell" and self.last >= x["price"])]:
                self.state["orders"].remove(o)
                self._on_fill(o["side"], o["price"], o["qty"])
        else:
            active = find_list(self.client.open_orders(), "orders") or []
            active_ids = {str(find_key(o, "order_id", "orderId", "id")) for o in active}
            filled = [o for o in self.state["orders"]
                      if o["order_id"] is not None and str(o["order_id"]) not in active_ids]
            if filled:
                self._refresh_live_balances()
                for o in filled:
                    self.state["orders"].remove(o)
                    # en live, el balance ya refleja el fill; solo recolocamos el par
                    v = self._order_value()
                    if o["side"] == "buy":
                        up = self.level_above(o["price"])
                        if up:
                            self.place_sell(up, v)
                    else:
                        dn = self.level_below(o["price"])
                        if dn:
                            self.place_buy(dn, v)
                    self.state["cycles"] += 1
                    self.state["trades"].append({"ts": int(time.time()), "side": o["side"],
                                                 "price": o["price"], "qty": o["qty"]})
                    log.info("%s ejecutada (live): %.8f @ %.4f", o["side"].upper(), o["qty"], o["price"])
                self.state["xbt_accum"] = self.xbt_total() - self.state["base_start"]

    # ---------------- bucle ----------------
    def step(self):
        self.last = float(self.client.ticker(self.pair)["lastPrice"])
        if not self.dry and not self.state["orders"] and not self.levels:
            self._refresh_live_balances()
        self.maybe_recenter()
        if not self.state["orders"]:          # arranque / grid vacío: sembrar todo
            self._seed(self.levels)
        self.sync_fills()
        self._save()

    def run(self):
        def _term(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _term)
        mode = "DRY-RUN (simulado)" if self.dry else "LIVE (dinero real)"
        log.info("grid iniciado: %s | %s | capital %.2f %s + %.8f %s | paso %.1f%% banda ±%.0f%%",
                 self.pair, mode, self.cfg["quote_capital"], self.quote,
                 self.cfg["base_capital"], self.base,
                 self.cfg["step_pct"] * 100, self.cfg["band_pct"] * 100)
        self.center = self.state.get("center")
        self.levels = self.state.get("levels", [])
        self.maybe_recenter(force=True)
        try:
            while True:
                try:
                    self.step()
                except NeoxaError as e:
                    log.warning("error en ciclo: %s", e)
                time.sleep(self.cfg["poll_seconds"])
        except KeyboardInterrupt:
            log.info("parando…")
        finally:
            if self.cfg["cancel_on_exit"] and not self.dry:
                for o in list(self.state["orders"]):
                    self._cancel(o)
                self.state["orders"] = []
                log.info("órdenes canceladas al salir")
            self._save()
            log.info("estado guardado. ciclos %d | XBT total %.8f (acum %+.8f) | USDC libre %.2f | fees %.4f",
                     self.state["cycles"], self.xbt_total(), self.state["xbt_accum"],
                     self.state["free_usdc"], self.state["fees_usdc"])
