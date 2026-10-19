// Gráfico estilo TradingView sobre KLineCharts v9: velas, indicadores,
// overlays del bot (escalera/lotes/trades) y herramientas de dibujo persistentes.
/* global klinecharts */
const Chart = (() => {
  const UP = '#26a69a';
  const DOWN = '#ef5350';

  let chart = null;
  let pricePrecision = 8;
  let onDrawingsChanged = null;      // callback → persistencia
  const userOverlays = new Map();    // id → {name, points}
  const indicatorPanes = new Map();  // nombre → paneId
  let lastBarTs = 0;
  let lastClose = null;

  // ───────── overlays propios ─────────
  function registerCustomOverlays() {
    // línea horizontal de nivel (escalera del bot / objetivo de lote)
    klinecharts.registerOverlay({
      name: 'levelLine',
      totalStep: 1,
      lock: true,
      createPointFigures: ({ coordinates, bounding, overlay }) => {
        const y = coordinates[0].y;
        const d = overlay.extendData || {};
        const figures = [{
          type: 'line',
          attrs: { coordinates: [{ x: 0, y }, { x: bounding.width, y }] },
          ignoreEvent: true,
          styles: {
            style: d.dashed ? 'dashed' : 'solid',
            color: d.color || '#888',
            size: 1,
            dashedValue: [5, 5],
          },
        }];
        if (d.label) {
          figures.push({
            type: 'text',
            attrs: { x: bounding.width - 4, y: y - 3, text: d.label, align: 'right', baseline: 'bottom' },
            ignoreEvent: true,
            styles: {
              color: d.color || '#888',
              size: 10,
              family: 'monospace',
              backgroundColor: 'rgba(10,14,23,0.75)',
              paddingLeft: 3, paddingRight: 3, paddingTop: 1, paddingBottom: 1,
            },
          });
        }
        return figures;
      },
    });

    // marcador de trade del bot (▲ compra / ▼ venta)
    klinecharts.registerOverlay({
      name: 'tradeMarker',
      totalStep: 1,
      lock: true,
      createPointFigures: ({ coordinates, overlay }) => {
        const c = coordinates[0];
        const side = (overlay.extendData || {}).side;
        const buy = side === 'buy';
        const col = buy ? UP : DOWN;
        const pts = buy
          ? [{ x: c.x, y: c.y + 5 }, { x: c.x - 4.5, y: c.y + 13 }, { x: c.x + 4.5, y: c.y + 13 }]
          : [{ x: c.x, y: c.y - 5 }, { x: c.x - 4.5, y: c.y - 13 }, { x: c.x + 4.5, y: c.y - 13 }];
        return [{
          type: 'polygon',
          attrs: { coordinates: pts },
          ignoreEvent: true,
          styles: { style: 'fill', color: col },
        }];
      },
    });
  }

  // ───────── estilos (tema TV oscuro) ─────────
  function themeStyles() {
    const grid = { show: true, size: 1, color: '#131a29', style: 'solid' };
    return {
      grid: { horizontal: grid, vertical: grid },
      candle: {
        bar: {
          upColor: UP, downColor: DOWN, noChangeColor: '#888888',
          upBorderColor: UP, downBorderColor: DOWN, noChangeBorderColor: '#888888',
          upWickColor: UP, downWickColor: DOWN, noChangeWickColor: '#888888',
        },
        priceMark: {
          high: { color: '#8a93a6' },
          low: { color: '#8a93a6' },
          last: {
            upColor: UP, downColor: DOWN,
            line: { style: 'dashed' },
            text: { size: 11, family: 'monospace' },
          },
        },
        tooltip: {
          showRule: 'follow_cross',
          showType: 'standard',
          text: { size: 11, color: '#d5dbe8', family: 'monospace' },
        },
      },
      indicator: {
        tooltip: { text: { size: 10, family: 'monospace' } },
        lines: [
          { color: '#f7931a' }, { color: '#935ebd' }, { color: '#2196f3' },
          { color: '#e8b931' }, { color: '#26a69a' },
        ].map(l => ({ style: 'solid', smooth: false, size: 1, ...l })),
      },
      xAxis: {
        axisLine: { color: '#1c2436' },
        tickText: { color: '#8a93a6', size: 10, family: 'monospace' },
        tickLine: { color: '#1c2436' },
      },
      yAxis: {
        axisLine: { color: '#1c2436' },
        tickText: { color: '#8a93a6', size: 10, family: 'monospace' },
        tickLine: { color: '#1c2436' },
      },
      separator: { color: '#1c2436', activeBackgroundColor: 'rgba(247,147,26,0.1)' },
      crosshair: {
        horizontal: {
          line: { style: 'dashed', color: '#3a4663' },
          text: { backgroundColor: '#2a3550', size: 10, family: 'monospace' },
        },
        vertical: {
          line: { style: 'dashed', color: '#3a4663' },
          text: { backgroundColor: '#2a3550', size: 10, family: 'monospace' },
        },
      },
      overlay: {
        line: { color: '#f7931a', size: 1 },
        point: {
          color: '#f7931a', borderColor: 'rgba(247,147,26,0.35)',
          activeColor: '#f7931a', activeBorderColor: 'rgba(247,147,26,0.35)',
        },
        text: { color: '#f7931a', family: 'monospace' },
      },
    };
  }

  // ───────── API pública ─────────
  function init(domId, drawingsChangedCb) {
    onDrawingsChanged = drawingsChangedCb;
    registerCustomOverlays();
    chart = klinecharts.init(domId, { styles: themeStyles() });
    setPrecision(8);
    chart.setOffsetRightDistance(90);
    new ResizeObserver(() => chart && chart.resize()).observe(document.getElementById(domId));
    return chart;
  }

  function setPrecision(p) {
    pricePrecision = p;
    try { chart.setPriceVolumePrecision(p, 4); } catch { /* API vieja */ }
  }

  function setData(bars) {
    chart.applyNewData(bars);
    const last = bars[bars.length - 1];
    lastBarTs = last ? last.timestamp : 0;
    lastClose = last ? last.close : null;
  }

  // merge incremental: actualiza la última vela o añade la nueva
  function updateBars(bars) {
    for (const b of bars) {
      if (b.timestamp >= lastBarTs) {
        chart.updateData(b);
        lastBarTs = b.timestamp;
        lastClose = b.close;
      }
    }
  }

  // el exchange solo emite velas en períodos CON trades: sintetiza velas planas
  // (o=h=l=c=último cierre, vol 0) hasta el período actual, estilo TradingView
  function extendToNow(ivMs) {
    if (!lastBarTs || lastClose == null || !ivMs) return;
    const current = Math.floor(Date.now() / ivMs) * ivMs;
    let guard = 0;
    for (let t = lastBarTs + ivMs; t <= current && guard < 3000; t += ivMs, guard++) {
      updateBars([{ timestamp: t, open: lastClose, high: lastClose, low: lastClose, close: lastClose, volume: 0 }]);
    }
  }

  function dataRange() {
    const list = chart.getDataList();
    if (!list.length) return null;
    return { from: list[0].timestamp, to: list[list.length - 1].timestamp };
  }

  // ───────── overlays del bot ─────────
  function clearBot() { chart.removeOverlay({ groupId: 'bot' }); }

  function setBotOverlays({ buys = [], lots = [], trades = [] }, toggles, fmtPrice) {
    clearBot();
    const range = dataRange();
    if (toggles.ladder) {
      for (const b of buys) {
        chart.createOverlay({
          name: 'levelLine', groupId: 'bot', lock: true,
          points: [{ value: b.price }],
          extendData: {
            color: 'rgba(38,166,154,0.85)', dashed: true,
            label: `▸ compra ${fmtPrice(b.price)}`,
          },
        });
      }
    }
    if (toggles.lots) {
      for (const l of lots) {
        chart.createOverlay({
          name: 'levelLine', groupId: 'bot', lock: true,
          points: [{ value: l.sell_price }],
          extendData: {
            color: 'rgba(247,147,26,0.9)', dashed: false,
            label: `lote #${l.id} → ${fmtPrice(l.sell_price)}`,
          },
        });
        // nivel de compra del lote, tenue
        chart.createOverlay({
          name: 'levelLine', groupId: 'bot', lock: true,
          points: [{ value: l.buy_price }],
          extendData: { color: 'rgba(247,147,26,0.3)', dashed: true, label: '' },
        });
      }
    }
    if (toggles.trades && range) {
      for (const t of trades) {
        const ts = t.ts * 1000;
        if (ts < range.from) continue;
        chart.createOverlay({
          name: 'tradeMarker', groupId: 'bot', lock: true,
          points: [{ timestamp: ts, value: t.price }],
          extendData: { side: t.side },
        });
      }
    }
  }

  // ───────── dibujo del usuario ─────────
  function snapshotOverlay(o) {
    return {
      name: o.name,
      points: (o.points || []).map(p => ({ timestamp: p.timestamp, value: p.value })),
    };
  }

  function notifyDrawings() {
    if (onDrawingsChanged) onDrawingsChanged([...userOverlays.values()]);
  }

  function userOverlayEvents() {
    return {
      onDrawEnd: e => { userOverlays.set(e.overlay.id, snapshotOverlay(e.overlay)); notifyDrawings(); return true; },
      onPressedMoveEnd: e => { userOverlays.set(e.overlay.id, snapshotOverlay(e.overlay)); notifyDrawings(); return true; },
      onRemoved: e => { userOverlays.delete(e.overlay.id); notifyDrawings(); return true; },
    };
  }

  function startDraw(name) {
    chart.createOverlay({ name, groupId: 'user', ...userOverlayEvents() });
  }

  function restoreDrawings(list) {
    chart.removeOverlay({ groupId: 'user' });
    userOverlays.clear();
    for (const d of (list || [])) {
      if (!d.name || !Array.isArray(d.points) || !d.points.length) continue;
      const id = chart.createOverlay({
        name: d.name, groupId: 'user', points: d.points, ...userOverlayEvents(),
      });
      if (id) userOverlays.set(id, { name: d.name, points: d.points });
    }
  }

  function clearDrawings() {
    chart.removeOverlay({ groupId: 'user' });
    userOverlays.clear();
    notifyDrawings();
  }

  // ───────── indicadores ─────────
  const MAIN_PANE_INDICATORS = new Set(['MA', 'EMA', 'BOLL', 'SAR']);

  function toggleIndicator(name, on) {
    if (on) {
      if (indicatorPanes.has(name)) return;
      if (MAIN_PANE_INDICATORS.has(name)) {
        chart.createIndicator(name, false, { id: 'candle_pane' });
        indicatorPanes.set(name, 'candle_pane');
      } else {
        const paneId = chart.createIndicator(name, false, { height: 85, dragEnabled: true });
        indicatorPanes.set(name, paneId);
      }
    } else {
      const paneId = indicatorPanes.get(name);
      if (paneId) {
        chart.removeIndicator(paneId, name);
        indicatorPanes.delete(name);
      }
    }
  }

  // ───────── mini-charts (modo 2×2) ─────────
  function createMini(dom) {
    const mini = klinecharts.init(dom, { styles: themeStyles() });
    mini.createIndicator('VOL', false, { height: 46 });
    mini.setOffsetRightDistance(40);
    const ro = new ResizeObserver(() => mini && mini.resize());
    ro.observe(dom);
    let ts = 0, close = null;
    return {
      setPrecision(p) { try { mini.setPriceVolumePrecision(p, 4); } catch { /* API vieja */ } },
      setData(bars) {
        mini.applyNewData(bars);
        const l = bars[bars.length - 1];
        ts = l ? l.timestamp : 0;
        close = l ? l.close : null;
      },
      update(bars) {
        for (const b of bars) {
          if (b.timestamp >= ts) { mini.updateData(b); ts = b.timestamp; close = b.close; }
        }
      },
      extendToNow(ivMs) {
        if (!ts || close == null || !ivMs) return;
        const current = Math.floor(Date.now() / ivMs) * ivMs;
        let guard = 0;
        for (let t = ts + ivMs; t <= current && guard < 3000; t += ivMs, guard++) {
          this.update([{ timestamp: t, open: close, high: close, low: close, close, volume: 0 }]);
        }
      },
    };
  }

  return {
    init, setData, updateBars, extendToNow, setPrecision, setBotOverlays, clearBot,
    startDraw, restoreDrawings, clearDrawings, toggleIndicator, dataRange, createMini,
  };
})();
