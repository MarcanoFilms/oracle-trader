// Lector del archivo histórico de velas (repo xbt-market-data): CSVs mensuales
// por serie, `time,open,high,low,close,volume` con time en epoch segundos.
// La API del exchange solo expone ~1000 velas; el archivo aporta el resto.
const fs = require('fs');
const path = require('path');

function loadCandles(root, exchange, pair, interval, limit = 10000) {
  const dir = path.join(root, 'data', exchange, pair, interval);
  let files;
  try {
    files = fs.readdirSync(dir).filter(f => f.endsWith('.csv')).sort();
  } catch {
    return [];   // sin archivo para esta serie: se sigue solo con la API
  }
  const bars = [];
  // de más nuevo a más viejo hasta cubrir el límite
  for (let i = files.length - 1; i >= 0 && bars.length < limit; i--) {
    const lines = fs.readFileSync(path.join(dir, files[i]), 'utf8').trim().split('\n').slice(1);
    const month = [];
    for (const line of lines) {
      const [t, o, h, l, c, v] = line.split(',');
      const ts = Number(t);
      if (!ts) continue;
      month.push({ timestamp: ts * 1000, open: +o, high: +h, low: +l, close: +c, volume: +v });
    }
    bars.unshift(...month);
  }
  return bars.slice(-limit);
}

module.exports = { loadCandles };
