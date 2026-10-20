#!/usr/bin/env python3
"""Watchdog del neoxa-bot — vigilancia 24/7 independiente, alerta por Telegram.

Corre como servicio systemd propio (sobrevive reinicios de la Asus y cierres de
sesión). NO toca los bots ni sus órdenes: solo observa (systemctl, mtime de
state.json, journal, métricas del estado) y avisa por Telegram cuando
detecta algo anómalo, con cooldown por tipo para no spamear.

Multi-cuenta: vigila el bot principal (raíz) y toda instancia de instances/*
cuyo servicio neoxa-bot@NOMBRE esté habilitado (se descubren solas en cada
pasada; deshabilitar el servicio la saca de vigilancia).

Detecta por cuenta: bot caído, atascado (state.json sin refrescar), crash-loop,
munición agotada sin actividad prolongada, trailing en loop, re-anclajes
excesivos. Los arreglos de código los hace Claude; esto son los ojos que nunca
duermen.
"""
import json
import os
import subprocess
import time

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
WD_STATE = os.path.join(HERE, "watchdog_state.json")
CHECK_EVERY = 60          # segundos entre chequeos
STALE_STATE = 150         # state.json sin refrescar más de esto = bot atascado
MUTE = {                  # cooldown por tipo de alerta (segundos)
    "bot_down": 600, "bot_stuck": 600, "crashloop": 900, "shadow_down": 1800,
    "no_ammo_idle": 7200, "trail_loop": 3600, "reanchor_storm": 3600, "recovered": 1,
}


def env(path=os.path.join(HERE, ".env")):
    d = {}
    for line in open(path):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            d[k] = v
    return d


E = env()
TOKEN = E.get("TELEGRAM_TOKEN")
CHAT = E.get("TELEGRAM_CHAT")


def tg(text):
    if not TOKEN or not CHAT:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=10)
    except Exception:
        pass


def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def _systemctl(args, timeout=10):
    try:
        r = subprocess.run(["systemctl", "--user"] + args,
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return None  # desconocido: no alertar en falso


def svc_active(name):
    out = _systemctl(["is-active", name])
    return None if out is None else out == "active"


def svc_enabled(name):
    out = _systemctl(["is-enabled", name])
    return out == "enabled"


def journal_since(unit, seconds):
    try:
        r = subprocess.run(["journalctl", "--user", "-u", unit, "--no-pager",
                            "-o", "cat", "--since", f"-{seconds} seconds"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout
    except Exception:
        return ""


def _label(cfg_path, fallback):
    try:
        return str(yaml.safe_load(open(cfg_path)).get("label") or fallback)
    except Exception:
        return fallback


def targets():
    """Bot principal + instancias con servicio habilitado (auto-descubiertas)."""
    t = [{"name": _label(os.path.join(HERE, "config.yaml"), "principal"),
          "svc": "neoxa-bot", "state": os.path.join(HERE, "state.json")}]
    inst = os.path.join(HERE, "instances")
    if os.path.isdir(inst):
        for n in sorted(os.listdir(inst)):
            d = os.path.join(inst, n)
            if n.startswith("_") or not os.path.isfile(os.path.join(d, "config.yaml")):
                continue
            svc = f"neoxa-bot@{n}"
            if svc_enabled(svc):
                t.append({"name": _label(os.path.join(d, "config.yaml"), n),
                          "svc": svc, "state": os.path.join(d, "state.json")})
    return t


def main():
    wd = load(WD_STATE, {})
    # migración del formato mono-cuenta: cycles pasan a dict por instancia
    if not isinstance(wd.get("last_cycles"), dict):
        wd["last_cycles"] = {}
        wd["last_cycles_ts"] = {}
    wd.setdefault("last_alert", {})
    last_alert = wd["last_alert"]

    names = " + ".join(t["name"] for t in targets())
    tg(f"🛡️ <b>Watchdog neoxa-bot activo</b> — vigilando: {names}. "
       f"Te aviso solo si algo anda mal.")

    def alert(scope, kind, msg):
        now = time.time()
        key = f"{scope}:{kind}"
        if now - last_alert.get(key, 0) < MUTE.get(kind, 600):
            return False
        last_alert[key] = now
        tg(msg)
        return True

    def pending(scope, kind):
        return f"{scope}:{kind}" in last_alert

    def clear(scope, kind):
        # permite re-alertar inmediatamente si vuelve a fallar tras recuperarse
        last_alert.pop(f"{scope}:{kind}", None)

    while True:
        try:
            now = time.time()
            # observador del trailing (audita solo al principal; no confundir
            # con el bot principal)
            if svc_active("neoxa-shadow") is False:
                alert("observador", "shadow_down",
                      "🟠 <b>neoxa-shadow (observador del trailing) caído</b> — el trailing "
                      "pierde su auditoría. No afecta a los bots, pero conviene revisar.")

            for t in targets():
                tn, svc, state_path = t["name"], t["svc"], t["state"]
                tag = f"[{tn}] "

                # 1) servicio vivo
                bot_up = svc_active(svc)
                if bot_up is False:
                    alert(tn, "bot_down", f"🔴 {tag}<b>bot CAÍDO</b> — el servicio {svc} no está "
                          f"activo. systemd debería reiniciarlo; si sigue caído hay un problema serio.")
                elif bot_up is True and pending(tn, "bot_down"):
                    clear(tn, "bot_down")
                    alert(tn, "recovered", f"🟢 {tag}bot recuperado y activo de nuevo.")

                # 2) atascado: state.json sin refrescar (debería ser cada ~4s)
                if bot_up and os.path.exists(state_path):
                    age = now - os.path.getmtime(state_path)
                    if age > STALE_STATE:
                        alert(tn, "bot_stuck", f"🟠 {tag}<b>bot ATASCADO</b> — state.json sin "
                              f"refrescar hace {age/60:.0f} min (debería ser cada 4s). "
                              f"Posible cuelgue de red/API.")
                    elif pending(tn, "bot_stuck") and age < 30:
                        clear(tn, "bot_stuck")

                # 3) crash-loop: error fatal / traceback reciente
                log5 = journal_since(svc, 300)
                fatals = log5.count("error fatal") + log5.count("Traceback (most recent call")
                if fatals >= 2:
                    sample = ""
                    for line in log5.splitlines():
                        if "Error" in line or "error fatal" in line:
                            sample = line[:200]
                    alert(tn, "crashloop", f"🔴 {tag}<b>CRASH-LOOP</b> — {fatals} fallos fatales "
                          f"en 5 min.\n<code>{sample}</code>\nNecesita arreglo de código.")

                # 4) trailing en loop: muchas activaciones sin cruces
                log30 = journal_since(svc, 1800)
                activ = log30.count("cerca de objetivo")
                cruces = log30.count("sobre objetivo")
                if activ >= 15 and cruces == 0:
                    alert(tn, "trail_loop", f"🟠 {tag}<b>Trailing en loop</b> — {activ} "
                          f"activaciones sin un solo cruce en 30 min. Posible churn de "
                          f"cancelar/recolocar; revisar umbrales.")

                # 5) LOOP de re-anclaje — un loop patológico re-ancla cada ~9s
                reanchors5 = log5.count("re-anclaje")
                if reanchors5 >= 8:
                    alert(tn, "reanchor_storm", f"🔴 {tag}<b>LOOP de re-anclaje</b> — "
                          f"{reanchors5} re-anclajes en 5 min (debería ser 0-1). La escalera "
                          f"se cancela/recoloca sin parar. Churn de órdenes; necesita arreglo.")

                # 6) munición agotada + sin actividad prolongada
                st = load(state_path, None)
                if st:
                    cyc = st.get("cycles")
                    if wd["last_cycles"].get(tn) != cyc:
                        wd["last_cycles"][tn] = cyc
                        wd["last_cycles_ts"][tn] = now
                    idle = now - (wd["last_cycles_ts"].get(tn) or now)
                    buys = st.get("buys", [])
                    # heurística: sin ciclos en 3h y sin compras activas = podría estar sin pólvora
                    if idle > 10800 and len(buys) == 0 and len(st.get("lots", [])) > 0:
                        alert(tn, "no_ammo_idle", f"🟠 {tag}<b>Bot sin actividad</b> — "
                              f"{idle/3600:.1f}h sin ciclos, sin compras activas y con lotes "
                              f"abiertos. Posible munición agotada esperando rebote. "
                              f"Revisar si conviene recargar/ajustar.")

            wd["last_alert"] = last_alert
            json.dump(wd, open(WD_STATE + ".tmp", "w"))
            os.replace(WD_STATE + ".tmp", WD_STATE)
        except Exception as e:
            # el watchdog jamás debe morir por un error propio
            try:
                tg(f"⚠️ watchdog: error interno no fatal: {str(e)[:150]}")
            except Exception:
                pass
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
