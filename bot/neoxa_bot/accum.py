"""Acumulador de XBT v2 — escalera de compras + ventas por lote (neoxa.exchange).

Invariantes (lo que hace imposible el bug del grid v1):
  I1  una VENTA solo existe respaldada por un lote COMPRADO más abajo;
      el stack previo del balance es reserva y jamás se vende.
  I2  las COMPRAS solo se colocan bajo el precio/touch (post-only); si el
      precio se escapa hacia arriba, la escalera se re-ancla (cancelar
      compras sin fill es gratis).
  I3  la venta de un lote nunca se cancela ni se mueve por debajo de
      costo·(1+paso).
  I4  cada ciclo compra→venta recupera el USDC invertido (fees USDC
      compensadas) y retiene el residuo en XBT:
      qty·(paso − 2·fee)/(1 + paso).
"""
import json
import logging
import os
import signal
import time

import requests

from .api import NeoxaError, find_key, find_list

log = logging.getLogger("neoxa_bot")


def tg_send(text, html=True):
    """Aviso por Telegram (fire-and-forget); usa TELEGRAM_TOKEN/CHAT del entorno."""
    tok = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT")
    if not tok or not chat:
        return
    label = os.environ.get("BOT_LABEL")
    if label:  # multi-cuenta: distinguir de qué instancia viene el aviso
        text = f"[{label}] {text}"
    body = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if html:
        body["parse_mode"] = "HTML"
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json=body, timeout=10)
    except Exception as e:  # nunca tumbar el bot por un aviso
        log.debug("telegram falló: %s", e)


def _dur(seconds):
    """Duración legible: 48min, 2.3h, 1.4d."""
    if seconds < 3600:
        return f"{seconds/60:.0f}min"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"


class AccumBot:
    def __init__(self, cfg, client, state_path="state.json"):
        self.cfg = cfg
        self.client = client
        self.state_path = state_path
        self.pair = cfg["pair"]
        self.dry = cfg["dry_run"]
        info = client.pair_info(self.pair)
        if info.get("tradingPaused"):
            raise NeoxaError(f"el par {self.pair} está pausado: {info.get('pauseReason')}")
        self.base, self.quote = info["baseToken"], info["quoteToken"]
        self.pp = int(info["pricePrecision"])
        self.qp = int(info["quantityPrecision"])
        self.min_qty = float(info["minQuantity"])
        self.fee = float(info["makerFeeRate"])
        self.step_pct = float(cfg["step_pct"])
        self.n_levels = int(cfg["n_buy_levels"])
        # umbral de polvo en unidades del quote (USDC≈0.5; para BTC usar ~5e-6)
        self.min_lot_value = float(cfg.get("min_lot_value", 0.5))
        # compound: fracción del residuo de cada ciclo que se cosecha en quote
        # para engordar la escalera (0 = todo el residuo queda en XBT)
        self.compound = min(1.0, max(0.0, float(cfg.get("compound_pct", 0))))
        # trailing take-profit: deja correr la venta en spikes, cruza en la
        # reversión; PISO SAGRADO en el objetivo (nunca vende por debajo)
        self.trail_on = bool(cfg.get("trail_enabled", False))
        self.trail_pct = float(cfg.get("trail_pct", 0.01))        # retroceso para vender
        self.trail_activate = float(cfg.get("trail_activate", 0.01))   # ventana de intercepción
        self.trail_hot_zone = float(cfg.get("trail_hot_zone", 0.02))   # % del objetivo → polling rápido
        self._hot = False  # hay lotes en zona caliente → polling acelerado
        self._last_reanchor = 0.0  # cooldown anti-loop de re-anclaje
        self.state = self._load_state()
        self._sim_seq = 0
        self._tick = 0
        self.last = None
        self.bid = None
        self.ask = None
        self._mkt_chg = None

    # ---------------- estado ----------------
    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                st = json.load(f)
            if st.get("strategy") == "accum" and st.get("pair") == self.pair \
                    and st.get("dry_run") == self.dry:
                # migración: estados viejos no traen los campos nuevos
                st.setdefault("quote_capital_dyn", None)
                st.setdefault("compound_harvested", 0.0)
                st.setdefault("fees_usdc", 0.0)
                # hitos: anclar al nivel ya alcanzado para no spamear al actualizar
                st.setdefault("last_milestone",
                              int(st.get("xbt_residue", 0) / 0.005))
                for lot in st.get("lots", []):
                    lot.setdefault("tr_hw", None)  # trailing: lotes previos en resting
                log.info("estado accum cargado: %d lotes, %d compras, %d ciclos, residuo %+.8f XBT",
                         len(st["lots"]), len(st["buys"]), st["cycles"], st["xbt_residue"])
                return st
            log.warning("estado previo incompatible; empezando de cero")
        return {
            "strategy": "accum", "pair": self.pair, "dry_run": self.dry,
            "lots": [],      # [{id, buy_price, qty, sell_price, sell_qty, sell_oid, ts}]
            "buys": [],      # [{price, qty, oid}]
            "lot_seq": 0,
            "cycles": 0, "xbt_residue": 0.0, "fees_usdc": 0.0,
            "quote_capital_dyn": None, "compound_harvested": 0.0,
            "last_milestone": 0,
            "free_usdc": 0.0, "free_xbt": 0.0,
            "base_start": None, "quote_start": None, "reserve_xbt": None,
            "trades": [], "started_ts": int(time.time()), "last_summary_ts": 0,
        }

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)

    # ---------------- balances ----------------
    def refresh_balances(self):
        data = self.client.balances().get("balances", {})

        def avail(tok):
            v = data.get(tok, {})
            return float(v.get("available", 0)) if isinstance(v, dict) else float(v or 0)

        self.state["free_usdc"] = avail(self.quote)
        self.state["free_xbt"] = avail(self.base)
        if self.state["base_start"] is None:
            # primer arranque: todo el XBT presente es reserva intocable
            in_sells = sum(l["sell_qty"] for l in self.state["lots"] if l.get("sell_oid"))
            self.state["base_start"] = self.state["free_xbt"] + in_sells
            self.state["quote_start"] = self.state["free_usdc"] + \
                sum(b["qty"] * b["price"] for b in self.state["buys"]) + \
                sum(l["qty"] * l["buy_price"] for l in self.state["lots"])
            self.state["reserve_xbt"] = float(self.cfg.get("xbt_reserve") or self.state["base_start"])
            log.info("capital inicial: %.8g %s + %.8f %s (reserva intocable %.8f)",
                     self.state["quote_start"], self.quote,
                     self.state["base_start"], self.base, self.state["reserve_xbt"])

    def xbt_total(self):
        in_sells = sum(l["sell_qty"] for l in self.state["lots"] if l.get("sell_oid"))
        return self.state["free_xbt"] + in_sells

    def usdc_total(self):
        in_buys = sum(b["qty"] * b["price"] for b in self.state["buys"])
        return self.state["free_usdc"] + in_buys

    def order_value(self):
        # capital de trabajo repartido entre los niveles, con colchón para fees/redondeos.
        # dinámico: crece con lo cosechado por compound (nunca por debajo del config)
        base = float(self.cfg["quote_capital"])
        dyn = max(self.state.get("quote_capital_dyn") or 0.0, base)
        return dyn * 0.97 / self.n_levels

    # ---------------- colocación ----------------
    def _submit(self, side, qty, price, post_only=True):
        if self.dry:
            self._sim_seq += 1
            return f"SIM-{self._sim_seq}"
        try:
            resp = self.client.place_limit(self.pair, side, qty, price, post_only=post_only)
        except NeoxaError as e:
            log.warning("orden %s %.8f @ %.8g rechazada: %s", side, qty, price, str(e)[:160])
            return None
        return find_key(resp, "order_id", "orderId", "id")

    def _cancel_api(self, oid):
        if self.dry or not oid or str(oid).startswith("SIM-"):
            return
        try:
            self.client.cancel_order(oid)
        except NeoxaError as e:
            log.warning("no pude cancelar %s: %s", oid, str(e)[:120])

    # ---------------- lotes ----------------
    def _new_lot(self, buy_price, qty, oid=""):
        """Compra ejecutada (total o parcial) → lote con su venta objetivo."""
        if qty * buy_price < self.min_lot_value:  # polvo: ni vale la pena una venta
            log.info("fill de polvo ignorado: %.8f @ %.8f", qty, buy_price)
            return
        sell_price = round(buy_price * (1 + self.step_pct), self.pp)
        # dimensionada para recuperar el costo completo con fees (fee en USDC ambos lados)
        base_qty = qty * (1 + 2 * self.fee) / (1 + self.step_pct)
        residue_full = max(0.0, qty - base_qty)
        # compound: una parte del residuo se vende también → BTC que engorda la escalera
        sell_qty = round(base_qty + self.compound * residue_full, self.qp)
        sell_qty = max(self.min_qty, min(sell_qty, qty))
        self.state["lot_seq"] += 1
        lot = {"id": self.state["lot_seq"], "buy_price": buy_price, "qty": qty,
               "sell_price": sell_price, "sell_qty": sell_qty, "sell_oid": None,
               "compound_qty": max(0.0, sell_qty - base_qty),
               "tr_hw": None,   # trailing: None=resting, float=pico, -1=cruce emitido
               "ts": int(time.time())}
        self.state["lots"].append(lot)
        self.state["trades"].append({"ts": lot["ts"], "side": "buy",
                                     "price": buy_price, "qty": qty, "lot": lot["id"]})
        self.state["trades"] = self.state["trades"][-1000:]
        log.info("COMPRA ejecutada: lote #%d %.8f @ %.8g → venta %.8f @ %.8g "
                 "(residuo al cerrar %+.8f)", lot["id"], qty, buy_price,
                 sell_qty, sell_price, qty - sell_qty)

    def _close_lot(self, lot, sold_qty, sell_price):
        residue = lot["qty"] - sold_qty
        self.state["xbt_residue"] += residue
        comp_q = min(lot.get("compound_qty", 0.0), sold_qty)
        if comp_q > 0:
            extra = comp_q * sell_price * (1 - self.fee)
            base = float(self.cfg["quote_capital"])
            dyn = max(self.state.get("quote_capital_dyn") or 0.0, base) + extra
            self.state["quote_capital_dyn"] = dyn
            self.state["compound_harvested"] = self.state.get("compound_harvested", 0.0) + extra
            log.info("compound: +%.8g %s a la escalera (capital dinámico %.8g)",
                     extra, self.quote, dyn)
        self.state["cycles"] += 1
        self.state["trades"].append({"ts": int(time.time()), "side": "sell",
                                     "price": sell_price, "qty": sold_qty,
                                     "lot": lot["id"], "buy_ts": lot.get("ts"),
                                     "buy_price": lot["buy_price"]})
        self.state["trades"] = self.state["trades"][-1000:]
        self.state["lots"].remove(lot)
        log.info("VENTA ejecutada: lote #%d %.8f @ %.8g (costo %.8g) | ciclo %d | "
                 "residuo %+.8f XBT | acumulado %+.8f XBT",
                 lot["id"], sold_qty, sell_price, lot["buy_price"],
                 self.state["cycles"], residue, self.state["xbt_residue"])
        if self.cfg.get("telegram_cycles"):
            margin = (sell_price / lot["buy_price"] - 1) * 100
            dur = _dur(max(0, int(time.time()) - lot.get("ts", int(time.time()))))
            stack = (self.state.get("reserve_xbt") or 0) + self.state["xbt_residue"]
            lines = [
                f"♻️ <b>Ciclo #{self.state['cycles']}</b>  <b>+{residue:.8f} XBT</b>",
                f"🟢 {lot['buy_price']:.8g} → 🔴 {sell_price:.8g}  "
                f"(+{margin:.1f}% · {dur})",
                f"Σ acumulado <b>{self.state['xbt_residue']:+.8f} XBT</b> · "
                f"stack {stack:.6f}",
            ]
            if comp_q > 0:
                lines.append(f"🌱 compound +{comp_q * sell_price * (1 - self.fee):.8g} "
                             f"{self.quote} → trabajo {self.state['quote_capital_dyn']:.8g}")
            tg_send("\n".join(lines))
            self._maybe_milestone()

    # ---------------- reconciliación (anti-bug: cancelada ≠ ejecutada) ----------------
    def _resolve_missing(self, missing_buys, missing_sells):
        """Busca órdenes desaparecidas en filled/cancelled y procesa su quantity_filled."""
        lookup = {}
        for status in ("filled", "cancelled"):
            try:
                recs = find_list(self.client.orders(status, limit=50), "orders") or []
            except NeoxaError as e:
                log.warning("no pude leer órdenes %s: %s", status, e)
                return False
            for r in recs:
                lookup[str(r.get("order_id"))] = r
        for b in missing_buys:
            rec = lookup.get(str(b["oid"]))
            if rec is None:
                log.warning("compra %s desapareció sin registro; la doy por cancelada", b["oid"])
                self.state["buys"].remove(b)
                continue
            qf = float(rec.get("quantity_filled") or 0)
            self.state["buys"].remove(b)
            if qf > 0:
                self._new_lot(b["price"], qf, b["oid"])
            else:
                log.info("compra @ %.8g cancelada sin fill", b["price"])
        for lot in missing_sells:
            rec = lookup.get(str(lot["sell_oid"]))
            if rec is None:
                log.warning("venta del lote #%d desapareció sin registro; la recoloco", lot["id"])
                lot["sell_oid"] = None
                continue
            qf = float(rec.get("quantity_filled") or 0)
            status = rec.get("status")
            if status == "filled" or qf >= lot["sell_qty"] - 1e-12:
                self._close_lot(lot, lot["sell_qty"], lot["sell_price"])
            elif qf > 0:  # cancelada con fill parcial: cerrar lo vendido, recolocar el resto
                ratio = qf / lot["sell_qty"]
                sold_part = {"id": lot["id"], "buy_price": lot["buy_price"],
                             "qty": lot["qty"] * ratio, "sell_price": lot["sell_price"],
                             "sell_qty": qf, "sell_oid": lot["sell_oid"], "ts": lot["ts"]}
                self.state["lots"].append(sold_part)
                self._close_lot(sold_part, qf, lot["sell_price"])
                lot["qty"] -= lot["qty"] * ratio
                lot["sell_qty"] = round(lot["sell_qty"] - qf, self.qp)
                lot["sell_oid"] = None
            else:
                log.warning("venta del lote #%d cancelada externamente; la recoloco", lot["id"])
                lot["sell_oid"] = None
        return True

    def reconcile(self):
        if self.dry:
            # simulación: compra llena si last <= precio; venta si last >= precio
            for b in list(self.state["buys"]):
                if self.last is not None and self.last <= b["price"]:
                    self.state["buys"].remove(b)
                    self.state["free_usdc"] -= b["qty"] * b["price"] * (1 + self.fee)
                    self.state["free_xbt"] += b["qty"]
                    self._new_lot(b["price"], b["qty"])
            for lot in list(self.state["lots"]):
                if lot["sell_oid"] and self.last is not None and self.last >= lot["sell_price"]:
                    self.state["free_xbt"] -= lot["sell_qty"]
                    self.state["free_usdc"] += lot["sell_qty"] * lot["sell_price"] * (1 - self.fee)
                    self._close_lot(lot, lot["sell_qty"], lot["sell_price"])
            return
        active_ids = {str(find_key(o, "order_id", "orderId", "id"))
                      for o in (find_list(self.client.orders("active"), "orders") or [])}
        missing_buys = [b for b in self.state["buys"] if str(b["oid"]) not in active_ids]
        missing_sells = [l for l in self.state["lots"]
                         if l.get("sell_oid") and str(l["sell_oid"]) not in active_ids]
        if missing_buys or missing_sells:
            if self._resolve_missing(missing_buys, missing_sells):
                self.refresh_balances()

    # ---------------- ventas de lotes ----------------
    def ensure_sells(self):
        for lot in self.state["lots"]:
            if lot.get("sell_oid") or lot.get("tr_hw") is not None:
                continue  # ya tiene orden, o está en gestión de trailing
            px = lot["sell_price"]
            # si el precio ya corrió por encima del objetivo, vender aún más arriba
            if self.ask and self.ask * 1.0005 > px:
                px = round(self.ask * 1.0005, self.pp)
            px = max(px, lot["sell_price"])  # I3: jamás bajo costo·(1+paso)
            oid = self._submit("sell", lot["sell_qty"], px)
            if oid:
                lot["sell_oid"] = oid
                if self.dry:
                    self.state["free_xbt"] -= 0  # escrow simulado implícito
                log.info("venta colocada: lote #%d %.8f @ %.8g", lot["id"], lot["sell_qty"], px)

    # ---------------- trailing take-profit ----------------
    def _manage_trailing(self):
        """Deja correr la venta en spikes; cruza (taker) en la reversión.
        PISO SAGRADO: una venta jamás se ejecuta por debajo del objetivo."""
        if not self.trail_on or self.bid is None:
            self._hot = False
            return
        bid = self.bid
        hot = False
        for lot in list(self.state["lots"]):
            target = lot["sell_price"]
            hw = lot.get("tr_hw")
            if hw == -1:
                continue  # venta de cruce ya emitida; reconcile la cerrará
            if bid >= target * (1 - self.trail_hot_zone):
                hot = True  # zona caliente → polling acelerado
            if hw is None:
                # RESTING → entrar en trailing al acercarse al objetivo (antes del
                # fill de la orden base), para poder dejar correr si hay momentum
                if lot.get("sell_oid") and bid >= target * (1 - self.trail_activate):
                    self._cancel_api(lot["sell_oid"])
                    lot["sell_oid"] = None
                    lot["tr_hw"] = bid
                    log.info("trailing ▲ lote #%d: bid %.8g cerca de objetivo %.8g, dejo correr",
                             lot["id"], bid, target)
            else:
                # TRAILING activo
                if bid > hw:
                    lot["tr_hw"] = hw = bid  # nuevo pico
                if hw > target and bid <= hw * (1 - self.trail_pct):
                    # reversión desde el pico
                    if bid >= target:
                        # SÍ capturamos upside → cruzar (taker) para asegurar la venta
                        exit_px = round(bid, self.pp)
                        oid = self._submit("sell", lot["sell_qty"], exit_px, post_only=False)
                        if oid:
                            lot["sell_oid"] = oid
                            lot["tr_hw"] = -1
                            log.info("trailing ✓ lote #%d: reversión, vendo %.8f @ %.8g "
                                     "(pico %.8g, objetivo %.8g, +%.1f%% sobre objetivo)",
                                     lot["id"], lot["sell_qty"], exit_px, hw, target,
                                     (exit_px / target - 1) * 100)
                    else:
                        # el precio revirtió bajo el objetivo sin dejar upside → volver a
                        # orden base MAKER al objetivo (no pagamos taker por nada)
                        lot["tr_hw"] = None
                        oid = self._submit("sell", lot["sell_qty"], target)
                        if oid:
                            lot["sell_oid"] = oid
                        log.info("trailing ▽ lote #%d: reversión sin upside, orden base maker @ %.8g",
                                 lot["id"], target)
                elif bid < target * (1 - self.trail_activate):
                    # falso arranque: sin momentum, vuelvo a la orden base en el objetivo
                    lot["tr_hw"] = None
                    oid = self._submit("sell", lot["sell_qty"], target)
                    if oid:
                        lot["sell_oid"] = oid
                    log.info("trailing ▼ lote #%d: sin momentum, orden base al objetivo", lot["id"])
        self._hot = hot

    # ---------------- escalera de compras ----------------
    def ladder(self):
        ref = min(x for x in (self.last, self.bid) if x) if (self.last or self.bid) else None
        if not ref:
            return
        cap = (self.bid or ref) * (1 - float(self.cfg["touch_buffer"]))
        open_sells = [l["sell_price"] for l in self.state["lots"] if l.get("sell_oid")]
        if open_sells:  # no cruzarse con nuestras propias ventas
            cap = min(cap, min(open_sells) * (1 - self.step_pct / 2))
        first = float(self.cfg.get("first_step_pct") or self.step_pct)
        targets = [round(ref / (1 + first) / (1 + self.step_pct) ** k, self.pp)
                   for k in range(0, self.n_levels)]
        targets = [min(t, round(cap, self.pp)) for t in targets]

        # re-anclaje: el precio se escapó hacia arriba → recolocar la escalera cerca.
        # "top" = nivel cubierto más alto entre COMPRAS y LOTES: si hay un lote pegado
        # al precio, la escalera ya está anclada ahí → NO re-anclar (evita el loop con
        # el bloqueo de recompra de munición). Cooldown extra como cinturón anti-spam.
        covered_tops = [b["price"] for b in self.state["buys"]] + \
                       [l["buy_price"] for l in self.state["lots"]]
        if self.state["buys"] and covered_tops:
            top = max(covered_tops)
            far = top < ref / (1 + self.step_pct) ** float(self.cfg["reanchor_steps"])
            cooled = time.monotonic() - self._last_reanchor > float(self.cfg.get("reanchor_cooldown", 60))
            if far and cooled:
                self._last_reanchor = time.monotonic()
                log.info("re-anclaje: nivel cubierto más alto %.8g lejos del precio %.8g; recoloco",
                         top, ref)
                for b in list(self.state["buys"]):
                    self._cancel_api(b["oid"])
                    if self.dry:
                        self.state["buys"].remove(b)
                # live: quedan en buys[] hasta que reconcile() las resuelva como
                # canceladas (o con fill parcial → lote); no colocamos nada este tick
                if not self.dry:
                    return

        V = self.order_value()
        # rangos ya cubiertos: compras activas Y lotes abiertos. No recomprar un
        # rango donde ya tenemos XBT esperando vender → no quemamos munición
        # cazando el mismo tramo; solo compramos en rangos nuevos (precio bajando)
        # o esperamos a que el precio se mueva en una dirección favorable.
        placed = {b["price"] for b in self.state["buys"]} | \
                 {lot["buy_price"] for lot in self.state["lots"]}
        block = self.step_pct * float(self.cfg.get("rebuy_block_frac", 0.5))
        max_lots = int(self.cfg.get("max_lots", 30))
        for t in sorted(targets, reverse=True):
            if len(self.state["buys"]) >= self.n_levels:
                break
            if len(self.state["lots"]) + len(self.state["buys"]) >= max_lots:
                break
            if any(abs(t - p) / t < block for p in placed):
                continue  # rango ya cubierto por una compra o un lote abierto
            qty = round(V / t, self.qp)
            cost = qty * t * (1 + self.fee)
            if qty < self.min_qty or cost > self.state["free_usdc"] - V * 0.005:
                continue
            oid = self._submit("buy", qty, t)
            if oid:
                self.state["buys"].append({"price": t, "qty": qty, "oid": oid})
                placed.add(t)
                if self.dry:
                    pass  # escrow simulado se descuenta al llenarse
                else:
                    self.state["free_usdc"] -= qty * t  # escrow local hasta el próximo refresh
                log.info("compra colocada: %.8f @ %.8g (nivel %.1f%% bajo ref)",
                         qty, t, (ref / t - 1) * 100)

    # ---------------- hitos ----------------
    def _maybe_milestone(self):
        """Hito cada MILESTONE_XBT de residuo acumulado (barra de progreso)."""
        step_xbt = float(self.cfg.get("milestone_xbt", 0.005))
        if step_xbt <= 0:
            return
        reached = int(self.state["xbt_residue"] / step_xbt)
        last = int(self.state.get("last_milestone", 0))
        if reached > last:
            self.state["last_milestone"] = reached
            level = reached * step_xbt
            if self.cfg.get("telegram"):
                tg_send(f"🏆 <b>HITO</b>: superado <b>+{level:.5f} XBT</b> de residuo "
                        f"acumulado en {self.state['cycles']} ciclos 🚀")

    # ---------------- resumen ----------------
    def summary(self, to_telegram=False):
        st = self.state
        q = self.quote
        reserve = st.get("reserve_xbt") or 0
        stack = reserve + st["xbt_residue"]
        growth = (st["xbt_residue"] / reserve * 100) if reserve else 0
        work = max(st.get("quote_capital_dyn") or 0, float(self.cfg["quote_capital"]))
        comp = st.get("compound_harvested") or 0
        days = max((time.time() - st.get("started_ts", time.time())) / 86400, 1e-9)
        rate = st["xbt_residue"] / days
        px = self.last or 0
        chg = ""
        if self._mkt_chg is not None:
            chg = f" ({self._mkt_chg:+.1f}% 24h)"
        log.info("resumen: ciclos %d residuo %+.8f stack %.8f trabajo %.8g",
                 st["cycles"], st["xbt_residue"], stack, work)
        if not to_telegram:
            return
        lines = [
            "📊 <b>neoxa-bot · resumen</b>",
            "━━━━━━━━━━━━━━━",
            f"♻️ <b>{st['cycles']}</b> ciclos · residuo <b>{st['xbt_residue']:+.8f} XBT</b>",
            f"📈 stack <b>{stack:.6f} XBT</b> (+{growth:.0f}% desde inicio)",
            f"💰 trabajo {work:.8g} {q}" + (f" · 🌱 +{comp:.6g} compound" if comp else ""),
            f"📦 {len(st['lots'])} lotes abiertos · {len(st['buys'])} compras armadas",
            f"🏷️ precio {px:.8g}{chg}",
            f"⏱️ ritmo {rate:+.8f} XBT/día",
        ]
        tg_send("\n".join(lines))

    # ---------------- bucle ----------------
    def step(self):
        tk = self.client.ticker(self.pair)
        self.last = float(tk.get("lastPrice") or 0) or self.last
        self.bid = float(tk.get("bestBid") or 0) or None
        self.ask = float(tk.get("bestAsk") or 0) or None
        chg = tk.get("changePercent")
        self._mkt_chg = float(chg) if chg is not None else self._mkt_chg
        self._tick += 1
        if not self.dry and (self._tick % 10 == 1):
            self.refresh_balances()
        elif self.dry and self.state["base_start"] is None:
            self.state["free_usdc"] = float(self.cfg["quote_capital"])
            self.state["base_start"] = self.state["free_xbt"]
            self.state["quote_start"] = self.state["free_usdc"]
            self.state["reserve_xbt"] = self.state["free_xbt"]
        self.reconcile()
        self.ensure_sells()
        self._manage_trailing()
        self.ladder()
        now = time.time()
        if now - self.state.get("last_summary_ts", 0) > float(self.cfg.get("summary_seconds", 21600)):
            self.state["last_summary_ts"] = int(now)
            self.summary(to_telegram=bool(self.cfg.get("telegram")))
        self._save()

    def run(self):
        def _term(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _term)
        mode = "DRY-RUN" if self.dry else "LIVE (dinero real)"
        log.info("acumulador v2 iniciado: %s | %s | paso %.2f%% | %d niveles | V=%.8g %s",
                 self.pair, mode, self.step_pct * 100, self.n_levels,
                 self.order_value(), self.quote)
        if not self.dry:
            self.refresh_balances()
        if self.cfg.get("telegram"):
            comp = " · compound " + (f"{self.compound*100:.0f}%" if self.compound else "off")
            tg_send("\n".join([
                f"🤖 <b>neoxa-bot v2 arrancó</b> ({mode})",
                f"━━━━━━━━━━━━━━━",
                f"📍 par <b>{self.pair}</b>",
                f"🪜 escalera {self.n_levels} niveles · paso {self.step_pct*100:.1f}%"
                f" (1er {float(self.cfg.get('first_step_pct') or self.step_pct)*100:.1f}%){comp}",
                f"💰 V {self.order_value():.6g} {self.quote} × nivel",
                f"🔒 reserva intocable {self.state.get('reserve_xbt') or 0:.8f} {self.base}",
                f"♻️ ciclos previos {self.state['cycles']} · "
                f"residuo {self.state['xbt_residue']:+.8f} XBT",
            ]))
        try:
            while True:
                try:
                    self.step()
                except NeoxaError as e:
                    log.warning("error en ciclo: %s", e)
                # polling acelerado cuando hay lotes en zona caliente (trailing)
                sleep_s = float(self.cfg["poll_seconds"])
                if self._hot:
                    sleep_s = min(sleep_s, float(self.cfg.get("poll_hot", 3)))
                time.sleep(sleep_s)
        except KeyboardInterrupt:
            log.info("parando…")
        except Exception as e:
            log.exception("error fatal")
            # anti-spam: en crash-loop (Restart=always) solo avisa el primer fallo
            # dentro de una ventana, no cada 20s
            now = time.time()
            if self.cfg.get("telegram") and now - self.state.get("last_crash_alert", 0) > 600:
                self.state["last_crash_alert"] = int(now)
                self._save()
                tg_send(f"🚨 <b>neoxa-bot MURIÓ</b>\n<code>{str(e)[:200]}</code>\n"
                        f"(systemd reintenta; silencio 10min para no spamear)")
            raise
        finally:
            # al salir: cancelar solo COMPRAS sin llenar; las ventas de lotes se
            # quedan trabajando (si llenan estando apagados, reconcile() las
            # procesa al arrancar). I3 intacto.
            if not self.dry and self.cfg.get("cancel_buys_on_exit", True):
                for b in list(self.state["buys"]):
                    self._cancel_api(b["oid"])
                log.info("%d compras canceladas al salir (las ventas quedan activas)",
                         len(self.state["buys"]))
            self._save()
            self.summary(to_telegram=bool(self.cfg.get("telegram")))
