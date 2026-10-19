// Gestor de la flota de bots: descubre cuentas (raíz + instances/*), lee su
// config/estado, consulta y controla sus servicios systemd de usuario.
const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');
const { promisify } = require('util');
const yaml = require('js-yaml');

const execFileP = promisify(execFile);

function serviceFor(id) {
  return id === 'root' ? 'neoxa-bot.service' : `neoxa-bot@${id}.service`;
}

function readYaml(file) {
  try { return yaml.load(fs.readFileSync(file, 'utf8')) || {}; } catch { return null; }
}

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

function readEnv(dir) {
  const out = {};
  try {
    for (const line of fs.readFileSync(path.join(dir, '.env'), 'utf8').split('\n')) {
      const l = line.trim();
      if (l && !l.startsWith('#') && l.includes('=')) {
        const i = l.indexOf('=');
        out[l.slice(0, i).trim()] = l.slice(i + 1).trim();
      }
    }
  } catch { /* sin .env */ }
  return out;
}

async function serviceState(service) {
  try {
    const { stdout } = await execFileP('systemctl', ['--user', 'is-active', service]);
    return stdout.trim();
  } catch (e) {
    return (e.stdout || '').trim() || 'unknown';
  }
}

function summarize(state) {
  if (!state) return null;
  const trades = state.trades || [];
  const last = trades[trades.length - 1];
  return {
    cycles: state.cycles ?? 0,
    residue: state.xbt_residue ?? 0,
    lots: (state.lots || []).length,
    buys: (state.buys || []).length,
    freeQuote: state.free_usdc ?? 0,   // nombre histórico: es el quote libre (BTC)
    freeBase: state.free_xbt ?? 0,
    reserve: state.reserve_xbt ?? 0,
    quoteCapitalDyn: state.quote_capital_dyn ?? null,
    compoundHarvested: state.compound_harvested ?? 0,
    startedTs: state.started_ts ?? null,
    lastTradeTs: last ? last.ts : null,
    trades: trades.length,
  };
}

// --- descubrimiento ---
async function discover(botRoot) {
  const found = [];
  if (fs.existsSync(path.join(botRoot, 'config.yaml'))) {
    found.push({ id: 'root', dir: botRoot });
  }
  const instDir = path.join(botRoot, 'instances');
  if (fs.existsSync(instDir)) {
    for (const name of fs.readdirSync(instDir).sort()) {
      if (name.startsWith('_')) continue; // plantillas
      const dir = path.join(instDir, name);
      if (fs.statSync(dir).isDirectory() && fs.existsSync(path.join(dir, 'config.yaml'))) {
        found.push({ id: name, dir });
      }
    }
  }

  return Promise.all(found.map(async ({ id, dir }) => {
    const cfg = readYaml(path.join(dir, 'config.yaml')) || {};
    const state = readJson(path.join(dir, 'state.json'));
    const env = readEnv(dir);
    const service = serviceFor(id);
    const active = await serviceState(service);
    return {
      id,
      dir,
      label: cfg.label || id,
      pair: cfg.pair || state?.pair || null,
      dryRun: Boolean(cfg.dry_run),
      service,
      active,                                  // active | inactive | failed | unknown
      hasKeys: Boolean(env.NEOXA_API_KEY),
      hasState: Boolean(state),
      summary: summarize(state),
      config: cfg,
    };
  }));
}

function accountDir(botRoot, id) {
  const dir = id === 'root' ? botRoot : path.join(botRoot, 'instances', id);
  // nunca salir de botRoot (los ids vienen del renderer)
  const real = path.resolve(dir);
  if (!real.startsWith(path.resolve(botRoot))) throw new Error('cuenta inválida');
  return real;
}

// estado completo (para el panel de detalle; trades recortados al final)
function fullState(botRoot, id, tradesLimit = 400) {
  const state = readJson(path.join(accountDir(botRoot, id), 'state.json'));
  if (!state) return null;
  const trades = state.trades || [];
  return {
    ...state,
    trades: trades.slice(-tradesLimit),
    tradesTotal: trades.length,
    summary: summarize(state),
  };
}

function credentials(botRoot, id) {
  const env = readEnv(accountDir(botRoot, id));
  if (!env.NEOXA_API_KEY) return null;
  return { apiKey: env.NEOXA_API_KEY, apiSecret: env.NEOXA_API_SECRET };
}

const ACTIONS = new Set(['start', 'stop', 'restart']);
async function control(id, action) {
  if (!ACTIONS.has(action)) throw new Error(`acción inválida: ${action}`);
  const service = serviceFor(id);
  await execFileP('systemctl', ['--user', action, service]);
  return serviceState(service);
}

async function logs(id, lines = 150) {
  const service = serviceFor(id);
  try {
    const { stdout } = await execFileP('journalctl',
      ['--user', '-u', service, '-n', String(lines), '--no-pager', '-o', 'short-iso']);
    return stdout;
  } catch (e) {
    return e.stdout || String(e);
  }
}

module.exports = { discover, fullState, credentials, control, logs, serviceFor };
