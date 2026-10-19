// Configuración persistente de la app (~/.config/oracle-trader/settings.json)
const fs = require('fs');
const os = require('os');
const path = require('path');

const DIR = path.join(os.homedir(), '.config', 'oracle-trader');
const FILE = path.join(DIR, 'settings.json');
const DRAWINGS_DIR = path.join(DIR, 'drawings');

const DEFAULTS = {
  // raíz del proyecto del bot: la cuenta principal vive ahí, las demás en instances/
  botRoot: path.join(os.homedir(), 'Projects', 'neoxa-bot'),
  exchange: 'neoxa',
  pair: 'BTCB2_BTC',
  interval: '15m',
  pollTickerMs: 4000,
  pollCandlesMs: 6000,
  pollFleetMs: 10000,
  pollOrderbookMs: 5000,
};

function load() {
  try {
    return { ...DEFAULTS, ...JSON.parse(fs.readFileSync(FILE, 'utf8')) };
  } catch {
    return { ...DEFAULTS };
  }
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
