// Adaptador para neoxa.exchange — implementa la interfaz común de exchange.
// Todas las operaciones del dashboard son de SOLO LECTURA contra el exchange:
// las órdenes las gestiona el bot; el dashboard observa y controla el servicio.
const BASE = 'https://neoxa.exchange/api';

class NeoxaAdapter {
  static id = 'neoxa';
  static label = 'neoxa.exchange';
  static intervals = ['1m', '5m', '15m', '1h', '4h', '1d'];

  constructor(creds = {}) {
    this.apiKey = creds.apiKey || null;
    this.apiSecret = creds.apiSecret || null;
    this._queue = Promise.resolve();
    this._lastReq = 0;
  }

  get authed() { return Boolean(this.apiKey); }

  // rate-limit suave (~500/min máx del exchange; aquí muy por debajo)
  async _req(path) {
    const run = async () => {
      const wait = 130 - (Date.now() - this._lastReq);
      if (wait > 0) await new Promise(r => setTimeout(r, wait));
      this._lastReq = Date.now();
      const headers = { Accept: 'application/json' };
      if (this.authed) {
        headers['X-API-Key'] = this.apiKey;
        headers['X-API-Secret'] = this.apiSecret;
      }
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 15000);
      try {
        const r = await fetch(BASE + path, { headers, signal: ctl.signal });
        const data = await r.json();
        if (!r.ok || data.success === false || data.error) {
          throw new Error(String(data.error || data.message || `HTTP ${r.status}`));
        }
        return data;
      } finally {
        clearTimeout(t);
      }
    };
    const p = this._queue.then(run, run);
    this._queue = p.catch(() => {});
    return p;
  }

  // ------- mercado (público) -------
  async pairs() {
    const d = await this._req('/exchange/pairs');
    return (d.pairs || []).map(p => ({
      pair: p.pair,
      base: p.base_token || (p.pair || '').split('_')[0],
      quote: p.quote_token || (p.pair || '').split('_')[1],
      raw: p,
    }));
  }

  async candles(pair, interval = '15m', limit = 500) {
    const d = await this._req(`/exchange/candles/${pair}?interval=${interval}&limit=${limit}`);
    return (d.candles || []).map(c => ({
      timestamp: c.time * 1000,
      open: c.open, high: c.high, low: c.low, close: c.close,
      volume: c.volume,
    }));
  }

  async ticker(pair) {
    const d = await this._req(`/exchange/ticker/${pair}`);
    const t = d.ticker || {};
    return {
      last: t.lastPrice,
      change: t.change,
      changePercent: t.changePercent,
      high24h: t.high24h,
      low24h: t.low24h,
      volume24h: t.volume24h,
      quoteVolume24h: t.quoteVolume24h,
      trades24h: t.trades24h,
      bestBid: t.bestBid,
      bestAsk: t.bestAsk,
      spread: t.spread,
    };
  }

  async orderbook(pair, depth = 15) {
    const d = await this._req(`/exchange/orderbook/${pair}?depth=${depth}`);
    const norm = side => (d[side] || []).map(l => ({
      price: l.price, quantity: l.quantity, amm: Boolean(l.isAmm),
    }));
    return { bids: norm('bids'), asks: norm('asks'), pool: d.pool || null };
  }

  async marketTrades(pair, limit = 40) {
    const d = await this._req(`/exchange/trades/${pair}?limit=${limit}`);
    return (d.trades || []).map(t => ({
      side: t.side,
      price: t.price,
      quantity: t.quantity,
      total: t.total,
      ts: Date.parse(t.executed_at),
    }));
  }

  // ------- cuenta (autenticado, solo lectura) -------
  async balances() {
    const d = await this._req('/exchange/balances');
    const out = {};
    for (const [asset, b] of Object.entries(d.balances || {})) {
      out[asset] = {
        available: b.available ?? 0,
        locked: b.locked ?? 0,
        inOrders: b.in_orders ?? 0,
        total: (b.total ?? 0) + (b.in_orders ?? 0),
      };
    }
    return out;
  }

  async openOrders(limit = 100) {
    const d = await this._req(`/exchange/orders?status=active&limit=${limit}`);
    return (d.orders || []).map(o => ({
      orderId: o.order_id,
      side: o.side,
      pair: o.pair,
      price: o.price,
      quantity: o.quantity,
      filled: o.quantity_filled,
      remaining: o.quantity_remaining,
      postOnly: Boolean(o.post_only),
      type: o.order_type,
      createdAt: Date.parse(o.created_at),
    }));
  }
}

module.exports = NeoxaAdapter;
