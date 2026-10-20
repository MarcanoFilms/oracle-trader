// Configuración persistente de la app (~/.config/oracle-trader/settings.json)
const fs = require('fs');
const os = require('os');
const path = require('path');

// ORACLE_CONFIG_DIR permite una config aislada (tests/captura); por defecto
// ~/.config/oracle-trader
const DIR = process.env.ORACLE_CONFIG_DIR || path.join(os.homedir(), '.config', 'oracle-trader');
const FILE = path.join(DIR, 'settings.json');
const DRAWINGS_DIR = path.join(DIR, 'drawings');

const DEFAULTS = {
  // raíz del proyecto del bot: la cuenta principal vive ahí, las demás en instances/
  botRoot: path.join(os.homedir(), 'Projects', 'neoxa-bot'),
  // archivo histórico de velas (repo xbt-market-data); si no existe, solo API
  archiveRoot: path.join(os.homedir(), 'Projects', 'xbt-market-data'),
  exchange: 'neoxa',
  pair: 'BTCB2_BTC',
  interval: '15m',
  chartType: 'candles',
  indicators: { VOL: { on: true } },
  pollTickerMs: 4000,
  pollCandlesMs: 6000,
  pollFleetMs: 10000,
  pollOrderbookMs: 5000,
  ui: {
    sidebarW: 285,
    rightW: 250,
    bottomH: 265,
    collapsed: { side: false, right: false, bottom: false },
    multichart: false,
    cells: [
      { pair: null, interval: '1h' },
      { pair: null, interval: '4h' },
      { pair: null, interval: '1d' },
    ],
  },
};

function load() {
  let s;
  try {
    s = { ...DEFAULTS, ...JSON.parse(fs.readFileSync(FILE, 'utf8')) };
  } catch {
    s = { ...DEFAULTS };
  }
  // raíz del bot: override por entorno > settings > motor incluido en el repo
  if (process.env.ORACLE_BOT_ROOT) {
    s.botRoot = process.env.ORACLE_BOT_ROOT;
  } else if (!fs.existsSync(s.botRoot)) {
    const bundled = path.join(__dirname, '..', 'bot');
    if (fs.existsSync(bundled)) s.botRoot = bundled;
  }
  return s;
}

function save(patch) {
  const merged = { ...load(), ...patch };
  fs.mkdirSync(DIR, { recursive: true });
  fs.writeFileSync(FILE, JSON.stringify(merged, null, 2));
  return merged;
}

// --- dibujos del gráfico (persistencia por exchange+par) ---
function drawingsFile(key) {
  return path.join(DRAWINGS_DIR, key.replace(/[^a-zA-Z0-9_-]/g, '_') + '.json');
}

function loadDrawings(key) {
  try {
    return JSON.parse(fs.readFileSync(drawingsFile(key), 'utf8'));
  } catch {
    return [];
  }
}

function saveDrawings(key, list) {
  fs.mkdirSync(DRAWINGS_DIR, { recursive: true });
  fs.writeFileSync(drawingsFile(key), JSON.stringify(list, null, 1));
}

module.exports = { load, save, loadDrawings, saveDrawings, DIR };
