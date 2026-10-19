// Oracle Trader — orquestador del renderer.
/* global Chart */
const api = (ch, payload) => window.oracle.invoke(ch, payload);
const $ = s => document.querySelector(s);

const S = {
  settings: null,
  exchange: 'neoxa',
  exchanges: [],
  pair: 'BTCB2_BTC',
  interval: '15m',
  fleet: [],
  selectedId: null,
  accountState: null,
  ticker: null,
  lastPrice: null,
  toggles: { ladder: true, lots: true, trades: true },
  activeTab: 'resumen',
  loadToken: 0,
  tabCache: {},        // ordenes/balances/logs por cuenta
  connOk: false,
};

// ───────────────── helpers de formato ─────────────────
function precisionFor(p) {
  if (p == null || !isFinite(p)) return 8;
  if (p >= 1000) return 2;
  if (p >= 1) return 4;
  if (p >= 0.01) return 6;
  return 8;
}
function fmtPrice(x) {
  if (x == null || !isFinite(x)) return '—';
  return Number(x).toFixed(precisionFor(S.lastPrice ?? x));
}
function fmtQty(x, d = 8) {
  if (x == null || !isFinite(x)) return '—';
  return Number(x).toFixed(d).replace(/0+$/, '').replace(/\.$/, '');
}
function fmtPct(x, signed = true) {
  if (x == null || !isFinite(x)) return '—';
  return (signed && x > 0 ? '+' : '') + x.toFixed(2) + '%';
}
function clsPct(x) { return x > 0 ? 'up' : x < 0 ? 'down' : ''; }
function ago(tsSec) {
  if (!tsSec) return '—';
  let d = Math.max(0, Math.floor(Date.now() / 1000 - tsSec));
  if (d < 60) return d + 's';
  if (d < 3600) return Math.floor(d / 60) + 'm';
  if (d < 86400) return Math.floor(d / 3600) + 'h ' + Math.floor((d % 3600) / 60) + 'm';
  return Math.floor(d / 86400) + 'd ' + Math.floor((d % 86400) / 3600) + 'h';
}
function hhmmss(tsMs) {
  return new Date(tsMs).toLocaleTimeString('es', { hour12: false });
}
function dmy(tsMs) {
  const d = new Date(tsMs);
  return d.toLocaleDateString('es', { day: '2-digit', month: '2-digit' }) + ' ' + hhmmss(tsMs);
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ───────────────── velas: relleno de huecos ─────────────────
// el exchange solo emite velas en los períodos CON trades; para un eje de
// tiempo real (estilo TV) se rellenan los huecos con velas planas (vol 0)
const IV_MS = { '1m': 60e3, '5m': 300e3, '15m': 900e3, '1h': 3600e3, '4h': 14400e3, '1d': 86400e3 };
// velas crudas a pedir cuando hay archivo histórico (en 1m/5m se acota la
// profundidad para que el relleno de huecos no dispare el nº de barras)
const ARCHIVE_LIMITS = { '1m': 15000, '5m': 10000, '15m': 8000, '1h': 6000, '4h': 4000, '1d': 2000 };
const MAX_FILLED = 20000;   // tope de barras ya rellenadas en el gráfico

function flatBar(prev, t) {
  return { timestamp: t, open: prev.close, high: prev.close, low: prev.close, close: prev.close, volume: 0 };
}
function fillSeq(bars, ivMs) {
  if (!ivMs || bars.length < 2) return bars;
  const out = [bars[0]];
  for (let i = 1; i < bars.length; i++) {
    let prev = out[out.length - 1];
    for (let t = prev.timestamp + ivMs; t < bars[i].timestamp && out.length < 60000; t += ivMs) {
      out.push(flatBar(prev, t));
      prev = out[out.length - 1];
    }
    out.push(bars[i]);
  }
  return out;
}

// ───────────────── toast + modal ─────────────────
let toastTimer = null;
function toast(msg, cls = '', ms = 3500) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + cls;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), ms);
}
function confirmModal(title, body, okLabel = 'Confirmar') {
  return new Promise(resolve => {
    $('#modalTitle').textContent = title;
    $('#modalBody').innerHTML = body;
    $('#modalOk').textContent = okLabel;
    $('#modalBack').classList.remove('hidden');
    const done = v => { $('#modalBack').classList.add('hidden'); cleanup(); resolve(v); };
    const onOk = () => done(true), onNo = () => done(false);
    const cleanup = () => {
      $('#modalOk').removeEventListener('click', onOk);
      $('#modalCancel').removeEventListener('click', onNo);
    };
    $('#modalOk').addEventListener('click', onOk);
    $('#modalCancel').addEventListener('click', onNo);
  });
}

// ───────────────── mercado: velas + ticker ─────────────────
async function loadCandles() {
  const token = ++S.loadToken;
  $('#chartStatus').textContent = 'cargando velas…';
  try {
    const bars = await api('market:candles', {
      exchange: S.exchange, pair: S.pair, interval: S.interval,
      limit: ARCHIVE_LIMITS[S.interval] || 500, useArchive: true,
    });
    if (token !== S.loadToken) return;
    const filled = fillSeq(bars, IV_MS[S.interval]).slice(-MAX_FILLED);
    Chart.setData(filled);
    if (filled.length) {
      S.lastPrice = filled[filled.length - 1].close;
      Chart.setPrecision(precisionFor(S.lastPrice));
    }
    Chart.extendToNow(IV_MS[S.interval]);
    const from = filled.length
      ? new Date(filled[0].timestamp).toLocaleDateString('es', { day: '2-digit', month: '2-digit', year: '2-digit' })
      : '—';
    $('#chartStatus').textContent = `${filled.length} velas desde ${from} · ${S.interval}`;
    refreshBotOverlays();
  } catch (e) {
    if (token === S.loadToken) $('#chartStatus').textContent = 'error: ' + e.message;
  }
}

async function pollCandles() {
  try {
    const ivMs = IV_MS[S.interval];
    const bars = await api('market:candles', { exchange: S.exchange, pair: S.pair, interval: S.interval, limit: 3 });
    Chart.updateBars(fillSeq(bars, ivMs));
    Chart.extendToNow(ivMs);   // vela nueva cada período aunque no haya trades
    if (bars.length) S.lastPrice = bars[bars.length - 1].close;
  } catch { /* siguiente tick */ }
}

let prevLast = null;
async function pollTicker() {
  try {
    const t = await api('market:ticker', { exchange: S.exchange, pair: S.pair });
    S.ticker = t;
    S.connOk = true;
    const el = $('#tkLast');
    el.textContent = fmtPrice(t.last);
    el.className = 'tk-val ' + (prevLast != null && t.last < prevLast ? 'down' : 'up');
    prevLast = t.last;
    const ch = $('#tkChange');
    ch.textContent = fmtPct(t.changePercent);
    ch.className = 'tk-val ' + clsPct(t.changePercent);
    $('#tkHigh').textContent = fmtPrice(t.high24h);
    $('#tkLow').textContent = fmtPrice(t.low24h);
    const [base, quote] = S.pair.split('_');
    $('#tkVol').textContent = fmtQty(t.volume24h, 2) + ' ' + base;
    $('#tkSpread').textContent = t.last ? fmtPct((t.bestAsk - t.bestBid) / t.last * 100, false) : '—';
    $('#tkTrades').textContent = t.trades24h ?? '—';
    document.title = `${fmtPrice(t.last)} ${S.pair.replace('_', '/')} · Oracle Trader`;
  } catch {
    S.connOk = false;
  }
  $('#connDot').className = 'conn ' + (S.connOk ? 'ok' : 'err');
}

// ───────────────── libro + trades del mercado ─────────────────
async function pollOrderbook() {
  try {
    const ob = await api('market:orderbook', { exchange: S.exchange, pair: S.pair, depth: 12 });
    const maxQ = Math.max(...[...ob.bids, ...ob.asks].map(r => r.quantity), 1e-12);
    const row = (r, side) => `
      <div class="ob-row ${side}">
        <div class="ob-bar" style="width:${Math.min(100, r.quantity / maxQ * 100)}%"></div>
        <span class="ob-price">${fmtPrice(r.price)}${r.amm ? '<span class="ob-amm">≋</span>' : ''}</span>
        <span class="ob-qty">${fmtQty(r.quantity, 4)}</span>
      </div>`;
    $('#obAsks').innerHTML = ob.asks.slice(0, 12).reverse().map(r => row(r, 'ask')).join('');
    $('#obBids').innerHTML = ob.bids.slice(0, 12).map(r => row(r, 'bid')).join('');
    const bid = ob.bids[0]?.price, ask = ob.asks[0]?.price;
    if (bid && ask) {
      $('#obSpread').innerHTML = `${fmtPrice((bid + ask) / 2)}<span class="sp-pct">Δ ${fmtPct((ask - bid) / ask * 100, false)}</span>`;
    }
    $('#obPool').textContent = ob.pool
      ? `AMM ${fmtQty(ob.pool.reserveBase, 2)} / ${fmtQty(ob.pool.reserveQuote, 4)} · fee ${(ob.pool.feeRate * 100).toFixed(1)}%`
      : '';
  } catch { /* siguiente tick */ }
}

async function pollMarketTrades() {
  try {
    const trades = await api('market:trades', { exchange: S.exchange, pair: S.pair, limit: 36 });
    $('#mtList').innerHTML = trades.map(t => `
      <div class="mt-row">
        <span class="mt-time">${hhmmss(t.ts)}</span>
        <span class="${t.side === 'buy' ? 'up' : 'down'}">${fmtPrice(t.price)}</span>
        <span class="mt-qty">${fmtQty(t.quantity, 4)}</span>
      </div>`).join('');
  } catch { /* siguiente tick */ }
}

// ───────────────── flota ─────────────────
function sparkSvg(vals) {
  if (!vals || vals.length < 2) return '';
  const w = 100, h = 26;
  const min = Math.min(...vals), max = Math.max(...vals);
  const rng = (max - min) || 1;
  const pts = vals.map((v, i) =>
    `${(i / (vals.length - 1) * w).toFixed(1)},${(h - 2 - (v - min) / rng * (h - 4)).toFixed(1)}`).join(' ');
  return `<div class="spark-label">residuo acumulado</div>
    <svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <polygon points="0,${h} ${pts} ${w},${h}" fill="rgba(247,147,26,0.08)"/>
      <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="1.3"
        vector-effect="non-scaling-stroke"/>
    </svg>`;
}

function acctCard(a) {
  const dot = a.active === 'active' ? 'ok' : a.active === 'failed' ? 'err' : 'down';
  const badges = [
    a.dryRun ? '<span class="badge dry">DRY</span>' : '<span class="badge live">LIVE</span>',
    !a.hasKeys ? '<span class="badge nokey">SIN KEY</span>' : '',
  ].join('');
  const s = a.summary;
  const stats = s ? `
    <div class="acct-stats">
      <div class="as-item"><span class="as-label">ciclos</span><span class="as-val">${s.cycles}</span></div>
      <div class="as-item"><span class="as-label">residuo</span><span class="as-val accent">${fmtQty(s.residue, 6)}</span></div>
      <div class="as-item"><span class="as-label">lotes</span><span class="as-val">${s.lots}</span></div>
      <div class="as-item"><span class="as-label">escalera</span><span class="as-val">${s.buys}</span></div>
      <div class="as-item"><span class="as-label">capital din.</span><span class="as-val">${s.quoteCapitalDyn != null ? fmtQty(s.quoteCapitalDyn, 6) : '—'}</span></div>
      <div class="as-item"><span class="as-label">últ. trade</span><span class="as-val">${ago(s.lastTradeTs)}</span></div>
    </div>` : '<div class="acct-pending">⏳ pendiente de API key / primer arranque</div>';
  const running = a.active === 'active';
  return `
    <div class="acct-card ${a.id === S.selectedId ? 'selected' : ''}" data-id="${a.id}">
      <div class="acct-head">
        <span class="acct-dot ${dot}" title="${a.service}: ${a.active}"></span>
        <span class="acct-name">${esc(a.label)}</span>
        <span class="acct-badges">${badges}</span>
      </div>
      <div class="acct-pair">${a.pair ? a.pair.replace('_', '/') : '—'} · ${a.service}</div>
      ${stats}
      ${sparkSvg(s?.sparkline)}
      <div class="acct-actions">
        ${running
          ? `<button class="acct-btn stop" data-act="stop" data-id="${a.id}">⏹ parar</button>
             <button class="acct-btn" data-act="restart" data-id="${a.id}">↻ reiniciar</button>`
          : `<button class="acct-btn play" data-act="start" data-id="${a.id}">▶ arrancar</button>`}
        <button class="acct-btn" data-act="logs" data-id="${a.id}">☰ logs</button>
      </div>
    </div>`;
}

async function pollFleet() {
  try {
    S.fleet = await api('fleet:list');
  } catch (e) {
    return;
  }
  const act = S.fleet.filter(a => a.active === 'active').length;
  const cyc = S.fleet.reduce((n, a) => n + (a.summary?.cycles || 0), 0);
  const res = S.fleet.reduce((n, a) => n + (a.summary?.residue || 0), 0);
  $('#fleetTotals').innerHTML = `
    <div class="ft-item"><span class="ft-label">activos</span><span class="ft-val">${act}/${S.fleet.length}</span></div>
    <div class="ft-item"><span class="ft-label">ciclos</span><span class="ft-val">${cyc}</span></div>
    <div class="ft-item"><span class="ft-label">residuo XBT</span><span class="ft-val">${fmtQty(res, 6)}</span></div>`;
  $('#fleetList').innerHTML = S.fleet.map(acctCard).join('');

  // tray: precio + estado de la flota
  api('tray:update', {
    price: S.ticker ? fmtPrice(S.ticker.last) : null,
    pair: S.pair.replace('_', '/'),
    fleet: S.fleet.map(a => ({ label: a.label, active: a.active })),
  }).catch(() => {});

  // selección inicial: la cuenta principal
  if (!S.selectedId && S.fleet.length) selectAccount(S.fleet[0].id);
}

async function controlAccount(id, action) {
  const a = S.fleet.find(x => x.id === id);
  if (!a) return;
  const warn = action === 'stop'
    ? 'Al parar, el bot cancela sus compras de escalera (las ventas de lotes quedan en el exchange).'
    : action === 'restart' ? 'El servicio se reiniciará y el bot re-colocará su escalera.' : 'El bot arrancará y colocará su escalera.';
  const ok = await confirmModal(
    `${action === 'start' ? 'Arrancar' : action === 'stop' ? 'Parar' : 'Reiniciar'} ${a.label}`,
    `<b>${esc(a.service)}</b><br>${warn}${a.dryRun ? '' : '<br><br>⚠️ Esta cuenta opera en <b>LIVE</b> con fondos reales.'}`,
    action === 'stop' ? 'Parar' : action === 'restart' ? 'Reiniciar' : 'Arrancar');
  if (!ok) return;
  try {
    const st = await api('fleet:control', { id, action });
    toast(`${a.label}: ${action} → ${st}`, st === 'active' || action === 'stop' ? 'ok' : '');
  } catch (e) {
    toast(`${a.label}: error — ${e.message}`, 'err', 6000);
  }
  pollFleet();
}

function selectAccount(id) {
  S.selectedId = id;
  S.accountState = null;
  const a = S.fleet.find(x => x.id === id);
  document.querySelectorAll('.acct-card').forEach(c => c.classList.toggle('selected', c.dataset.id === id));
  $('#tabAccount').textContent = a ? `${a.label} · ${a.service}` : '';
  if (a?.pair && a.pair !== S.pair) switchPair(a.pair);
  refreshAccountDetail();
}

async function refreshAccountDetail() {
  if (!S.selectedId) return;
  try {
    S.accountState = await api('fleet:state', { id: S.selectedId });
  } catch { S.accountState = null; }
  refreshBotOverlays();
  renderTab();
}

// overlays del bot sobre el gráfico (solo si el par coincide)
function refreshBotOverlays() {
  const st = S.accountState;
  const a = S.fleet.find(x => x.id === S.selectedId);
  if (!st || !a || st.pair !== S.pair) { Chart.clearBot(); return; }
  Chart.setBotOverlays(
    { buys: st.buys || [], lots: st.lots || [], trades: st.trades || [] },
    S.toggles, fmtPrice,
  );
}

// ───────────────── tabs de detalle ─────────────────
const TABS = [
  ['resumen', 'Resumen'], ['lotes', 'Lotes'], ['escalera', 'Escalera'],
  ['trades', 'Trades'], ['ordenes', 'Órdenes'], ['balances', 'Balances'],
  ['logs', 'Logs'], ['config', 'Config'],
];

function buildTabs() {
  $('#tabs').innerHTML = TABS.map(([id, label]) =>
    `<div class="tab ${id === S.activeTab ? 'active' : ''}" data-tab="${id}">${label}</div>`).join('');
}

async function renderTab() {
  buildTabs();
  const el = $('#tabContent');
  const st = S.accountState;
  const a = S.fleet.find(x => x.id === S.selectedId);
  if (!a) { el.innerHTML = '<div class="empty-hint">Selecciona una cuenta de la flota</div>'; return; }
  if (!st && !['logs', 'config', 'balances', 'ordenes'].includes(S.activeTab)) {
    el.innerHTML = `<div class="empty-hint">${esc(a.label)}: sin estado todavía (pendiente de API key o primer arranque)</div>`;
    return;
  }
  switch (S.activeTab) {
    case 'resumen': return renderResumen(el, st, a);
    case 'lotes': return renderLotes(el, st);
    case 'escalera': return renderEscalera(el, st);
    case 'trades': return renderTrades(el, st);
    case 'ordenes': return renderOrdenes(el, a);
    case 'balances': return renderBalances(el, a);
    case 'logs': return renderLogs(el, a);
    case 'config': return renderConfig(el, a);
  }
}

function renderResumen(el, st, a) {
  const s = st.summary;
  const days = s.startedTs ? (Date.now() / 1000 - s.startedTs) / 86400 : null;
  const perDay = days && days > 0.2 ? s.residue / days : null;
  const quote = (st.pair || '_').split('_')[1] || 'quote';
  const inBuys = (st.buys || []).reduce((n, b) => n + b.qty * b.price, 0);
  const cards = [
    ['ciclos cerrados', s.cycles, '', `${s.trades} trades · desde hace ${ago(s.startedTs)}`],
    ['residuo acumulado', fmtQty(s.residue, 8) + ' XBT', 'accent', perDay ? `≈ ${fmtQty(perDay, 6)} XBT/día` : ''],
    ['compound cosechado', fmtQty(s.compoundHarvested, 8) + ' ' + quote, 'up', 'engorda la escalera'],
    ['capital dinámico', s.quoteCapitalDyn != null ? fmtQty(s.quoteCapitalDyn, 8) + ' ' + quote : '—', '', 'quote de trabajo actual'],
    ['lotes abiertos', s.lots, '', `${fmtQty((st.lots || []).reduce((n, l) => n + l.qty, 0), 6)} XBT esperando venta`],
    ['escalera', s.buys + ' compras', '', fmtQty(inBuys, 8) + ' ' + quote + ' en escrow'],
    ['reserva intocable', fmtQty(s.reserve, 8) + ' XBT', '', 'piso sagrado'],
    [quote + ' libre', fmtQty(s.freeQuote, 8), '', 'munición sin colocar'],
    ['XBT libre', fmtQty(s.freeBase, 8), '', 'incluye depósito sin convertir'],
    ['último trade', ago(s.lastTradeTs), '', a.active === 'active' ? 'bot activo' : 'BOT PARADO'],
  ];
  el.innerHTML = '<div class="stats-grid">' + cards.map(([l, v, cls, sub]) => `
    <div class="stat-card">
      <div class="stat-label">${l}</div>
      <div class="stat-val ${cls}">${v}</div>
      ${sub ? `<div class="stat-sub">${sub}</div>` : ''}
    </div>`).join('') + '</div>';
}

function renderLotes(el, st) {
  const lots = [...(st.lots || [])].sort((x, y) => y.buy_price - x.buy_price);
  if (!lots.length) { el.innerHTML = '<div class="empty-hint">Sin lotes abiertos — toda la munición está en la escalera</div>'; return; }
  el.innerHTML = `<table class="grid"><thead><tr>
      <th>#</th><th>compra</th><th>cantidad</th><th>objetivo</th><th>vende</th>
      <th>dist. al objetivo</th><th>edad</th><th>estado</th></tr></thead><tbody>` +
    lots.map(l => {
      const dist = S.lastPrice ? (l.sell_price / S.lastPrice - 1) * 100 : null;
      const trailing = l.tr_hw != null;
      return `<tr>
        <td>#${l.id}</td>
        <td class="n">${fmtPrice(l.buy_price)}</td>
        <td class="n">${fmtQty(l.qty, 8)}</td>
        <td class="n">${fmtPrice(l.sell_price)}</td>
        <td class="n">${fmtQty(l.sell_qty, 8)}</td>
        <td class="n ${dist != null && dist <= 1 ? 'up' : ''}">${dist != null ? fmtPct(dist) : '—'}</td>
        <td class="dim">${ago(l.ts)}</td>
        <td>${trailing ? '<span class="up">🎯 trailing (pico ' + fmtPrice(l.tr_hw) + ')</span>'
          : l.sell_oid ? '<span class="dim">venta activa</span>' : '<span class="down">SIN venta</span>'}</td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

function renderEscalera(el, st) {
  const buys = [...(st.buys || [])].sort((x, y) => y.price - x.price);
  if (!buys.length) { el.innerHTML = '<div class="empty-hint">Sin compras en escalera</div>'; return; }
  const quote = (st.pair || '_').split('_')[1] || '';
  el.innerHTML = `<table class="grid"><thead><tr>
      <th>nivel</th><th>precio</th><th>cantidad</th><th>valor ${quote}</th><th>dist. al precio</th></tr></thead><tbody>` +
    buys.map((b, i) => {
      const dist = S.lastPrice ? (b.price / S.lastPrice - 1) * 100 : null;
      return `<tr>
        <td>${i + 1}</td>
        <td class="n up">${fmtPrice(b.price)}</td>
        <td class="n">${fmtQty(b.qty, 8)}</td>
        <td class="n">${fmtQty(b.qty * b.price, 8)}</td>
        <td class="n dim">${dist != null ? fmtPct(dist) : '—'}</td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

function renderTrades(el, st) {
  const trades = [...(st.trades || [])].reverse().slice(0, 200);
  if (!trades.length) { el.innerHTML = '<div class="empty-hint">Sin trades todavía</div>'; return; }
  el.innerHTML = `<table class="grid"><thead><tr>
      <th>fecha</th><th>lado</th><th>precio</th><th>cantidad</th><th>lote</th><th>margen</th></tr></thead><tbody>` +
    trades.map(t => {
      const margin = t.side === 'sell' && t.buy_price ? (t.price / t.buy_price - 1) * 100 : null;
      return `<tr>
        <td class="dim">${dmy(t.ts * 1000)}</td>
        <td class="${t.side === 'buy' ? 'side-buy' : 'side-sell'}">${t.side === 'buy' ? 'COMPRA' : 'VENTA'}</td>
        <td class="n">${fmtPrice(t.price)}</td>
        <td class="n">${fmtQty(t.qty, 8)}</td>
        <td class="dim">${t.lot != null ? '#' + t.lot : '—'}</td>
        <td class="n ${margin != null ? clsPct(margin) : ''}">${margin != null ? fmtPct(margin) : '—'}</td>
      </tr>`;
    }).join('') + `</tbody></table>
    <div class="dim" style="padding:6px 10px">${st.tradesTotal} trades en total · mostrando los últimos ${trades.length}</div>`;
}

async function renderOrdenes(el, a) {
  if (!a.hasKeys) { el.innerHTML = '<div class="empty-hint">Sin API key en el .env de esta cuenta</div>'; return; }
  el.innerHTML = '<div class="empty-hint">cargando órdenes del exchange…</div>';
  const res = await api('fleet:orders', { id: a.id, exchange: S.exchange });
  if (S.activeTab !== 'ordenes') return;
  if (res.error) { el.innerHTML = `<div class="empty-hint">${esc(res.error)}</div>`; return; }
  if (!res.length) { el.innerHTML = '<div class="empty-hint">Sin órdenes activas en el exchange</div>'; return; }
  el.innerHTML = `<table class="grid"><thead><tr>
      <th>orden</th><th>lado</th><th>par</th><th>precio</th><th>cantidad</th><th>ejecutado</th><th>tipo</th><th>creada</th></tr></thead><tbody>` +
    res.sort((x, y) => y.price - x.price).map(o => `<tr>
      <td class="dim">${esc(o.orderId)}</td>
      <td class="${o.side === 'buy' ? 'side-buy' : 'side-sell'}">${o.side === 'buy' ? 'COMPRA' : 'VENTA'}</td>
      <td class="dim">${esc(o.pair)}</td>
      <td class="n">${fmtPrice(o.price)}</td>
      <td class="n">${fmtQty(o.quantity, 8)}</td>
      <td class="n dim">${fmtQty(o.filled, 8)}</td>
      <td class="dim">${o.postOnly ? 'post-only' : esc(o.type || '')}</td>
      <td class="dim">${dmy(o.createdAt)}</td>
    </tr>`).join('') + '</tbody></table>';
}

async function renderBalances(el, a) {
  if (!a.hasKeys) { el.innerHTML = '<div class="empty-hint">Sin API key en el .env de esta cuenta</div>'; return; }
  el.innerHTML = '<div class="empty-hint">cargando balances…</div>';
  const res = await api('fleet:balances', { id: a.id, exchange: S.exchange });
  if (S.activeTab !== 'balances') return;
  if (res.error) { el.innerHTML = `<div class="empty-hint">${esc(res.error)}</div>`; return; }
  const rows = Object.entries(res).filter(([, b]) => b.total > 0 || b.inOrders > 0);
  el.innerHTML = `<table class="grid"><thead><tr>
      <th>activo</th><th>disponible</th><th>en órdenes</th><th>total</th></tr></thead><tbody>` +
    rows.map(([asset, b]) => `<tr>
      <td><b>${esc(asset)}</b></td>
      <td class="n">${fmtQty(b.available, 8)}</td>
      <td class="n dim">${fmtQty(b.inOrders, 8)}</td>
      <td class="n accent" style="color:var(--accent)">${fmtQty(b.total, 8)}</td>
    </tr>`).join('') + '</tbody></table>';
}

async function renderLogs(el, a) {
  const first = !el.querySelector('pre.logbox');
  if (first) el.innerHTML = '<div class="empty-hint">cargando journal…</div>';
  const out = await api('fleet:logs', { id: a.id, lines: 200 });
  if (S.activeTab !== 'logs') return;
  const atBottom = !first && el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  el.innerHTML = `<pre class="logbox">${esc(out)}</pre>`;
  if (first || atBottom) el.scrollTop = el.scrollHeight;
}

function renderConfig(el, a) {
  const lines = Object.entries(a.config || {})
    .map(([k, v]) => `<span class="cfg-key">${esc(k)}</span>: ${esc(JSON.stringify(v))}`)
    .join('\n');
  el.innerHTML = `<pre class="cfgbox">${lines}\n\n<span class="dim"># ${esc(a.dir)}/config.yaml</span></pre>`;
}

// ───────────────── gráfico: toolbar ─────────────────
function buildTimeframes() {
  const ex = S.exchanges.find(e => e.id === S.exchange);
  const intervals = ex ? ex.intervals : ['15m'];
  $('#tfBar').innerHTML = intervals.map(iv =>
    `<button class="tf-btn ${iv === S.interval ? 'active' : ''}" data-iv="${iv}">${iv}</button>`).join('');
}

// ── indicadores: metadatos, menú y editor ──
const PALETTE = ['#f7931a', '#935ebd', '#2196f3', '#e8b931', '#26a69a'];
const IND_META = {
  MA:   { label: 'Medias móviles', params: [5, 10, 30, 60], lines: 'perParam', variable: true },
  EMA:  { label: 'EMA',            params: [6, 12, 20],     lines: 'perParam', variable: true },
  BOLL: { label: 'Bollinger',      params: [20, 2],         lines: 3 },
  VOL:  { label: 'Volumen',        params: [5, 10, 20],     lines: 'perParam', variable: true },
  MACD: { label: 'MACD',           params: [12, 26, 9],     lines: 2 },
  RSI:  { label: 'RSI',            params: [6, 12, 24],     lines: 'perParam', variable: true },
  KDJ:  { label: 'KDJ',            params: [9, 3, 3],       lines: 3 },
};
const INDICATOR_GROUPS = [
  ['precio', ['MA', 'EMA', 'BOLL']],
  ['paneles', ['VOL', 'MACD', 'RSI', 'KDJ']],
];

function fitColors(colors, n) {
  const out = [];
  for (let i = 0; i < n; i++) out.push(colors[i] || PALETTE[i % PALETTE.length]);
  return out;
}

function buildIndicatorMenu() {
  $('#indMenu').innerHTML = INDICATOR_GROUPS.map(([group, names]) =>
    `<div class="ind-group">${group.toUpperCase()}</div>` +
    names.map(name => `
      <div class="ind-item">
        <label><input type="checkbox" data-ind="${name}" ${S.indCfg[name]?.on ? 'checked' : ''}/> ${IND_META[name].label}</label>
        <button class="ind-gear" data-gear="${name}" title="Configurar períodos y colores">⚙</button>
      </div>`).join('')
  ).join('');
}

// ── editor de indicadores ──
let ieName = null;

function renderIeParams(params, meta) {
  const canDel = meta.variable && params.length > 1;
  $('#ieParams').innerHTML = params.map((p, i) => `
    <span class="ie-param">
      <input type="number" min="1" step="any" value="${p}" />
      ${canDel ? `<button class="ie-del" data-del="${i}" title="Quitar período">✕</button>` : ''}
    </span>`).join('') +
    (meta.variable && params.length < 5
      ? '<button class="ie-add" id="ieAdd" title="Añadir período">+</button>' : '');
}

function renderIeColors(colors) {
  $('#ieColors').innerHTML = colors.map((c, i) =>
    `<input type="color" value="${c}" title="línea ${i + 1}" />`).join('');
}

function ieReadParams() {
  return [...document.querySelectorAll('#ieParams input[type=number]')]
    .map(el => parseFloat(el.value)).filter(v => isFinite(v) && v > 0);
}
function ieReadColors() {
  return [...document.querySelectorAll('#ieColors input[type=color]')].map(el => el.value);
}

function openIndicatorEditor(name) {
  const meta = IND_META[name];
  if (!meta) return;
  ieName = name;
  const cfg = S.indCfg[name] || {};
  const params = (cfg.calcParams?.length ? cfg.calcParams : meta.params).slice();
  const n = meta.lines === 'perParam' ? params.length : meta.lines;
  $('#ieTitle').textContent = `⚙ ${name} — ${meta.label}`;
  renderIeParams(params, meta);
  renderIeColors(fitColors(cfg.colors || [], n));
  $('#indEditorBack').classList.remove('hidden');
}

function ieApply() {
  const meta = IND_META[ieName];
  let params = ieReadParams();
  if (!meta.variable) params = params.slice(0, meta.params.length);
  if (!params.length) { toast('períodos inválidos', 'err'); return; }
  S.indCfg[ieName] = { ...(S.indCfg[ieName] || {}), on: true, calcParams: params, colors: ieReadColors() };
  api('settings:set', { indicators: S.indCfg });
  Chart.toggleIndicator(ieName, true, S.indCfg[ieName]);
  buildIndicatorMenu();
  $('#indEditorBack').classList.add('hidden');
  toast(`${ieName} actualizado`, 'ok', 1800);
}

function ieResetDefaults() {
  const meta = IND_META[ieName];
  renderIeParams(meta.params.slice(), meta);
  renderIeColors(fitColors([], meta.lines === 'perParam' ? meta.params.length : meta.lines));
}

// ── tipo de gráfico (estilo TV) ──
const CHART_TYPES = [
  ['candles', 'Velas'],
  ['hollow', 'Velas huecas'],
  ['heikin', 'Heikin Ashi'],
  ['ohlc', 'Barras OHLC'],
  ['area', 'Línea'],
];

function buildChartTypeMenu() {
  $('#ctMenu').innerHTML = CHART_TYPES.map(([id, label]) => `
    <div class="ct-item ${id === S.chartType ? 'active' : ''}" data-ct="${id}">
      <span class="ct-check">${id === S.chartType ? '✓' : ''}</span>${label}
    </div>`).join('');
  $('#ctBtn').textContent = (CHART_TYPES.find(c => c[0] === S.chartType)?.[1] || 'Velas') + ' ▾';
}

function applyChartType(type) {
  S.chartType = type;
  Chart.setChartType(type);
  api('settings:set', { chartType: type });
  buildChartTypeMenu();
}

const DRAW_TOOLS = [
  ['horizontalStraightLine', '─', 'Línea horizontal'],
  ['segment', '╱', 'Segmento'],
  ['straightLine', '⤢', 'Línea extendida'],
  ['rayLine', '↗', 'Rayo'],
  ['verticalStraightLine', '│', 'Línea vertical'],
  ['priceLine', '$', 'Línea de precio'],
  ['parallelStraightLine', '⫽', 'Paralelas'],
  ['priceChannelLine', '≡', 'Canal de precio'],
  ['fibonacciLine', 'φ', 'Fibonacci'],
  ['simpleAnnotation', 'T', 'Nota'],
];
function buildDrawbar() {
  $('#drawbar').innerHTML =
    DRAW_TOOLS.map(([name, icon, title]) =>
      `<button class="draw-btn" data-draw="${name}" title="${title}">${icon}</button>`).join('') +
    '<div class="draw-sep"></div>' +
    '<button class="draw-btn danger" data-draw="__clear__" title="Borrar todos los dibujos">🗑</button>';
}

// persistencia de dibujos por exchange+par
function drawingsKey() { return `${S.exchange}__${S.pair}`; }
let saveDrawTimer = null;
function onDrawingsChanged(list) {
  clearTimeout(saveDrawTimer);
  saveDrawTimer = setTimeout(() => api('drawings:save', { key: drawingsKey(), list }), 600);
}
async function loadDrawings() {
  const list = await api('drawings:load', { key: drawingsKey() });
  Chart.restoreDrawings(list);
}

// ───────────────── layout: splitters, fullscreen, multichart ─────────────────
function saveUi() {
  clearTimeout(saveUi._t);
  saveUi._t = setTimeout(() => api('settings:set', { ui: S.ui }), 500);
}

function applyUi() {
  const u = S.ui;
  $('#sidebar').style.width = u.sidebarW + 'px';
  $('#rightbar').style.width = u.rightW + 'px';
  $('#bottomPanel').style.height = u.bottomH + 'px';
  $('#sidebar').classList.toggle('collapsed', !!u.collapsed.side);
  $('#rightbar').classList.toggle('collapsed', !!u.collapsed.right);
  $('#bottomPanel').classList.toggle('collapsed', !!u.collapsed.bottom);
}

function makeSplitter(el, { drag, collapse }) {
  el.addEventListener('mousedown', e => {
    e.preventDefault();
    el.classList.add('dragging');
    const move = ev => drag(ev);
    const up = () => {
      el.classList.remove('dragging');
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
      saveUi();
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  });
  el.addEventListener('dblclick', () => { collapse(); applyUi(); saveUi(); });
}

function setupSplitters() {
  makeSplitter($('#splitLeft'), {
    drag: e => { S.ui.sidebarW = Math.max(180, Math.min(460, e.clientX)); S.ui.collapsed.side = false; applyUi(); },
    collapse: () => { S.ui.collapsed.side = !S.ui.collapsed.side; },
  });
  makeSplitter($('#splitRight'), {
    drag: e => { S.ui.rightW = Math.max(170, Math.min(460, window.innerWidth - e.clientX)); S.ui.collapsed.right = false; applyUi(); },
    collapse: () => { S.ui.collapsed.right = !S.ui.collapsed.right; },
  });
  makeSplitter($('#splitBottom'), {
    drag: e => { S.ui.bottomH = Math.max(90, Math.min(window.innerHeight - 220, window.innerHeight - e.clientY)); S.ui.collapsed.bottom = false; applyUi(); },
    collapse: () => { S.ui.collapsed.bottom = !S.ui.collapsed.bottom; },
  });
}

function toggleFullscreen() { document.body.classList.toggle('fullscreen'); }

// ── multichart 2×2: la celda principal conserva dibujo/overlays; las otras
// tres son vistas de mercado ligeras con su propio par e intervalo ──
const minis = [null, null, null];

function setMultichart(on) {
  S.ui.multichart = on;
  document.body.classList.toggle('multichart', on);
  $('#btnGrid').classList.toggle('tb-active', on);
  if (on) initMinis();
  saveUi();
}

function buildCellSelects() {
  const ex = S.exchanges.find(e => e.id === S.exchange);
  const ivs = ex ? ex.intervals : ['15m'];
  for (let i = 0; i < 3; i++) {
    const cfg = S.ui.cells[i] = S.ui.cells[i] || { pair: null, interval: '1h' };
    if (!cfg.pair) cfg.pair = S.pair;
    const cell = document.querySelector(`.chart-cell.mini[data-cell="${i}"]`);
    const pairSel = cell.querySelector('.cell-pair');
    const ivSel = cell.querySelector('.cell-iv');
    pairSel.innerHTML = S.pairList.map(p =>
      `<option value="${p}" ${p === cfg.pair ? 'selected' : ''}>${p.replace('_', '/')}</option>`).join('');
    ivSel.innerHTML = ivs.map(iv =>
      `<option value="${iv}" ${iv === cfg.interval ? 'selected' : ''}>${iv}</option>`).join('');
    pairSel.addEventListener('change', () => { cfg.pair = pairSel.value; saveUi(); loadMini(i); });
    ivSel.addEventListener('change', () => { cfg.interval = ivSel.value; saveUi(); loadMini(i); });
  }
}

function initMinis() {
  for (let i = 0; i < 3; i++) {
    if (!minis[i]) {
      minis[i] = Chart.createMini(document.getElementById('miniChart' + i));
      loadMini(i);
    }
  }
}

async function loadMini(i) {
  const mc = minis[i], cfg = S.ui.cells[i];
  if (!mc || !cfg) return;
  try {
    const bars = await api('market:candles', {
      exchange: S.exchange, pair: cfg.pair, interval: cfg.interval,
      limit: Math.floor((ARCHIVE_LIMITS[cfg.interval] || 600) / 2), useArchive: true,
    });
    const filled = fillSeq(bars, IV_MS[cfg.interval]).slice(-8000);
    mc.setData(filled);
    if (filled.length) mc.setPrecision(precisionFor(filled[filled.length - 1].close));
    mc.extendToNow(IV_MS[cfg.interval]);
  } catch { /* siguiente intento */ }
}

async function pollMinis() {
  if (!S.ui.multichart) return;
  for (let i = 0; i < 3; i++) {
    const mc = minis[i], cfg = S.ui.cells[i];
    if (!mc || !cfg) continue;
    try {
      const bars = await api('market:candles', { exchange: S.exchange, pair: cfg.pair, interval: cfg.interval, limit: 3 });
      mc.update(fillSeq(bars, IV_MS[cfg.interval]));
      mc.extendToNow(IV_MS[cfg.interval]);
    } catch { /* siguiente tick */ }
  }
}

// ───────────────── cambios de par / intervalo ─────────────────
async function switchPair(pair) {
  if (pair === S.pair) return;
  S.pair = pair;
  prevLast = null;
  $('#pairSel').value = pair;
  api('settings:set', { pair });
  await loadCandles();
  await loadDrawings();
  pollTicker(); pollOrderbook(); pollMarketTrades();
}

async function switchInterval(iv) {
  S.interval = iv;
  api('settings:set', { interval: iv });
  buildTimeframes();
  await loadCandles();
}

// ───────────────── eventos ─────────────────
function wireEvents() {
  $('#fleetList').addEventListener('click', e => {
    const btn = e.target.closest('.acct-btn');
    if (btn) {
      e.stopPropagation();
      const { act, id } = btn.dataset;
      if (act === 'logs') { selectAccount(id); setTab('logs'); }
      else controlAccount(id, act);
      return;
    }
    const card = e.target.closest('.acct-card');
    if (card) selectAccount(card.dataset.id);
  });

  $('#tabs').addEventListener('click', e => {
    const t = e.target.closest('.tab');
    if (t) setTab(t.dataset.tab);
  });

  $('#tfBar').addEventListener('click', e => {
    const b = e.target.closest('.tf-btn');
    if (b) switchInterval(b.dataset.iv);
  });

  $('#indBtn').addEventListener('click', e => {
    e.stopPropagation();
    $('#ctDropdown').classList.remove('open');
    $('#indDropdown').classList.toggle('open');
  });
  $('#ctBtn').addEventListener('click', e => {
    e.stopPropagation();
    $('#indDropdown').classList.remove('open');
    $('#ctDropdown').classList.toggle('open');
  });
  document.addEventListener('click', () => {
    $('#indDropdown').classList.remove('open');
    $('#ctDropdown').classList.remove('open');
  });
  $('#ctMenu').addEventListener('click', e => {
    e.stopPropagation();
    const it = e.target.closest('.ct-item');
    if (it) { applyChartType(it.dataset.ct); $('#ctDropdown').classList.remove('open'); }
  });
  $('#indMenu').addEventListener('click', e => {
    e.stopPropagation();
    const g = e.target.closest('.ind-gear');
    if (g) { $('#indDropdown').classList.remove('open'); openIndicatorEditor(g.dataset.gear); }
  });
  $('#indMenu').addEventListener('change', e => {
    const ind = e.target.dataset.ind;
    if (!ind) return;
    S.indCfg[ind] = { ...(S.indCfg[ind] || {}), on: e.target.checked };
    api('settings:set', { indicators: S.indCfg });
    Chart.toggleIndicator(ind, e.target.checked, S.indCfg[ind]);
  });

  // editor de indicadores
  $('#ieCancel').addEventListener('click', () => $('#indEditorBack').classList.add('hidden'));
  $('#ieApply').addEventListener('click', ieApply);
  $('#ieReset').addEventListener('click', ieResetDefaults);
  $('#ieParams').addEventListener('click', e => {
    const meta = IND_META[ieName];
    if (!meta) return;
    if (e.target.id === 'ieAdd') {
      const params = ieReadParams();
      const colors = ieReadColors();
      params.push(Math.round((params[params.length - 1] || 10) * 2));
      renderIeParams(params, meta);
      if (meta.lines === 'perParam') renderIeColors(fitColors(colors, params.length));
    } else if (e.target.dataset.del != null) {
      const i = +e.target.dataset.del;
      const params = ieReadParams();
      const colors = ieReadColors();
      params.splice(i, 1);
      if (meta.lines === 'perParam') colors.splice(i, 1);
      renderIeParams(params, meta);
      renderIeColors(meta.lines === 'perParam' ? fitColors(colors, params.length) : colors);
    }
  });

  $('#drawbar').addEventListener('click', e => {
    const b = e.target.closest('.draw-btn');
    if (!b) return;
    if (b.dataset.draw === '__clear__') {
      Chart.clearDrawings();
      toast('dibujos borrados', 'ok', 1500);
      return;
    }
    document.querySelectorAll('.draw-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    Chart.startDraw(b.dataset.draw);
    setTimeout(() => b.classList.remove('active'), 400);
  });

  for (const [id, key] of [['#ovLadder', 'ladder'], ['#ovLots', 'lots'], ['#ovTrades', 'trades']]) {
    $(id).addEventListener('change', e => {
      S.toggles[key] = e.target.checked;
      refreshBotOverlays();
    });
  }

  $('#btnFull').addEventListener('click', toggleFullscreen);
  $('#btnGrid').addEventListener('click', () => setMultichart(!S.ui.multichart));
  document.addEventListener('keydown', e => {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
    const k = e.key.toLowerCase();
    if (k === 'f') toggleFullscreen();
    else if (k === 'g') setMultichart(!S.ui.multichart);
    else if (e.key === 'Escape') {
      document.body.classList.remove('fullscreen');
      $('#indEditorBack').classList.add('hidden');
    }
  });

  $('#pairSel').addEventListener('change', e => switchPair(e.target.value));
  $('#exchangeSel').addEventListener('change', e => {
    S.exchange = e.target.value;
    api('settings:set', { exchange: S.exchange });
    buildTimeframes();
    loadCandles();
  });
}

function setTab(tab) {
  S.activeTab = tab;
  renderTab();
}

// ───────────────── loops ─────────────────
function startLoops() {
  const cfg = S.settings;
  setInterval(pollTicker, cfg.pollTickerMs);
  setInterval(pollCandles, cfg.pollCandlesMs);
  setInterval(pollFleet, cfg.pollFleetMs);
  setInterval(pollOrderbook, cfg.pollOrderbookMs);
  setInterval(pollMarketTrades, cfg.pollOrderbookMs + 1500);
  setInterval(pollMinis, cfg.pollCandlesMs + 800);
  setInterval(refreshAccountDetail, 8000);
  setInterval(() => {
    if (['ordenes', 'balances'].includes(S.activeTab)) renderTab();
  }, 12000);
  setInterval(() => {
    if (S.activeTab === 'logs') renderTab();
  }, 6000);
  setInterval(() => { $('#clock').textContent = hhmmss(Date.now()); }, 1000);
}

// ───────────────── arranque ─────────────────
async function boot() {
  S.settings = await api('settings:get');
  S.exchange = S.settings.exchange;
  S.pair = S.settings.pair;
  S.interval = S.settings.interval || '15m';
  S.exchanges = await api('exchanges:list');

  $('#exchangeSel').innerHTML = S.exchanges.map(e =>
    `<option value="${e.id}" ${e.id === S.exchange ? 'selected' : ''}>${e.label}</option>`).join('');

  // estado de layout persistido (con defaults defensivos para settings viejos)
  S.ui = S.settings.ui || {};
  S.ui.sidebarW = S.ui.sidebarW || 285;
  S.ui.rightW = S.ui.rightW || 250;
  S.ui.bottomH = S.ui.bottomH || 265;
  S.ui.collapsed = S.ui.collapsed || {};
  S.ui.cells = S.ui.cells || [];

  Chart.init('chart', onDrawingsChanged);

  // tipo de gráfico + indicadores guardados
  S.chartType = S.settings.chartType || 'candles';
  S.indCfg = S.settings.indicators || { VOL: { on: true } };
  Chart.setChartType(S.chartType);
  for (const [name, cfg] of Object.entries(S.indCfg)) {
    if (cfg.on) Chart.toggleIndicator(name, true, cfg);
  }

  buildTimeframes();
  buildChartTypeMenu();
  buildIndicatorMenu();
  buildDrawbar();
  buildTabs();
  wireEvents();
  applyUi();
  setupSplitters();

  // pares disponibles del exchange (con fallback al par configurado)
  try {
    const pairs = await api('market:pairs', { exchange: S.exchange });
    const opts = pairs.map(p => p.pair).sort();
    if (!opts.includes(S.pair)) opts.unshift(S.pair);
    S.pairList = opts;
    $('#pairSel').innerHTML = opts.map(p =>
      `<option value="${p}" ${p === S.pair ? 'selected' : ''}>${p.replace('_', '/')}</option>`).join('');
  } catch {
    S.pairList = [S.pair];
    $('#pairSel').innerHTML = `<option value="${S.pair}">${S.pair.replace('_', '/')}</option>`;
  }
  buildCellSelects();
  if (S.ui.multichart) setMultichart(true);

  await loadCandles();
  await loadDrawings();
  await pollFleet();
  pollTicker(); pollOrderbook(); pollMarketTrades();
  startLoops();
}

boot().catch(e => {
  console.error(e);
  toast('error de arranque: ' + e.message, 'err', 10000);
});
