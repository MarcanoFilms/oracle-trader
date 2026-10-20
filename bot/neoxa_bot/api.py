"""Cliente REST para neoxa.exchange."""
import threading
import time

import requests

BASE = "https://neoxa.exchange/api"


class NeoxaError(Exception):
    pass


class NeoxaClient:
    """Cliente con rate-limit propio (~500 req/min, bajo el límite de 600)."""

    def __init__(self, api_key=None, api_secret=None, timeout=15):
        self.s = requests.Session()
        self.timeout = timeout
        self.authed = bool(api_key)
        if api_key:
            self.s.headers.update({"X-API-Key": api_key, "X-API-Secret": api_secret})
        self._lock = threading.Lock()
        self._last = 0.0
        self.min_interval = 0.12

    def _request(self, method, path, **kw):
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        for attempt in range(4):
            try:
                r = self.s.request(method, BASE + path, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                if attempt == 3:
                    raise NeoxaError(f"error de red: {e}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise NeoxaError(f"HTTP {r.status_code}: {r.text[:200]}")
                time.sleep(2 ** attempt + 1)
                continue
            try:
                data = r.json()
            except ValueError:
                raise NeoxaError(f"HTTP {r.status_code} sin JSON: {r.text[:200]}")
            if r.status_code >= 400 or data.get("success") is False or "error" in data:
                raise NeoxaError(str(data.get("error") or data.get("message") or data)[:300])
            return data
        raise NeoxaError("reintentos agotados")

    # ------- endpoints públicos -------
    def pairs(self):
        return self._request("GET", "/exchange/pairs")["pairs"]

    def pair_info(self, pair):
        for p in self.pairs():
            if p["pair"] == pair:
                return p
        raise NeoxaError(f"el par {pair} no existe en el exchange")

    def ticker(self, pair):
        return self._request("GET", f"/exchange/ticker/{pair}")["ticker"]

    def orderbook(self, pair, depth=20):
        return self._request("GET", f"/exchange/orderbook/{pair}?depth={depth}")

    def candles(self, pair, interval="15m", limit=200):
        return self._request(
            "GET", f"/exchange/candles/{pair}?interval={interval}&limit={limit}"
        )["candles"]

    # ------- endpoints autenticados -------
    def balances(self):
        return self._request("GET", "/exchange/balances")

    def place_limit(self, pair, side, quantity, price, post_only=True):
        body = {"pair": pair, "side": side, "quantity": quantity, "price": price}
        if post_only:
            body["postOnly"] = True
        return self._request("POST", "/exchange/order", json=body)

    def place_market(self, pair, side, quantity=None, total=None, slippage=0.02):
        body = {"pair": pair, "side": side, "type": "market", "slippageTolerance": slippage}
        if quantity is not None:
            body["quantity"] = quantity
        if total is not None:
            body["total"] = total
        return self._request("POST", "/exchange/order", json=body)

    def cancel_order(self, order_id):
        return self._request("DELETE", f"/exchange/order/{order_id}")

    def open_orders(self, limit=100):
        return self._request("GET", f"/exchange/orders?status=active&limit={limit}")

    def orders(self, status="active", limit=100):
        """Órdenes por estado: active | filled | cancelled."""
        return self._request("GET", f"/exchange/orders?status={status}&limit={limit}")

    def my_trades(self, limit=100, before=None):
        q = f"/exchange/trades/my?limit={limit}"
        if before:
            q += f"&before={before}"
        return self._request("GET", q)


def find_key(obj, *keys):
    """Busca en profundidad la primera aparición de cualquiera de las claves.

    Las respuestas autenticadas aún no se han podido observar (whitelist de IP),
    así que los campos se localizan de forma defensiva.
    """
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] is not None:
                return obj[k]
        for v in obj.values():
            r = find_key(v, *keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, *keys)
            if r is not None:
                return r
    return None


def find_list(obj, *keys):
    """Como find_key pero exige que el valor sea una lista."""
    if isinstance(obj, dict):
        for k in keys:
            if isinstance(obj.get(k), list):
                return obj[k]
        for v in obj.values():
            r = find_list(v, *keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        return obj
    return None
