// Oracle Trader — proceso principal de Electron.
// Toda la E/S (exchange, filesystem del bot, systemd) vive aquí; el renderer
// solo habla por IPC. Las credenciales jamás cruzan al renderer.
const { app, BrowserWindow, ipcMain } = require('electron');
const fs = require('fs');
const path = require('path');

const settings = require('./src/settings');
const fleet = require('./src/fleet');
const exchanges = require('./src/exchanges');

let win = null;

function createWindow() {
  win = new BrowserWindow({
    width: 1760,
    height: 1020,
    minWidth: 1180,
    minHeight: 700,
    backgroundColor: '#0a0e17',
    title: 'Oracle Trader',
    autoHideMenuBar: true,
    icon: path.join(__dirname, 'assets', 'icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  // modo captura para verificación headless: ORACLE_SHOT=/ruta.png
  if (process.env.ORACLE_SHOT) {
    win.webContents.once('did-finish-load', () => {
      setTimeout(async () => {
        try {
          const img = await win.webContents.capturePage();
          fs.writeFileSync(process.env.ORACLE_SHOT, img.toPNG());
          console.log('captura guardada en', process.env.ORACLE_SHOT);
        } catch (e) {
          console.error('captura falló:', e);
        }
        app.quit();
      }, Number(process.env.ORACLE_SHOT_DELAY || 12000));
    });
  }
}

// adaptadores públicos (sin credenciales) cacheados por exchange
const publicAdapters = {};
function publicAdapter(ex) {
  if (!publicAdapters[ex]) publicAdapters[ex] = exchanges.create(ex);
  return publicAdapters[ex];
}

function botRoot() { return settings.load().botRoot; }

// --- IPC ---
ipcMain.handle('settings:get', () => settings.load());
ipcMain.handle('settings:set', (_e, patch) => settings.save(patch));
ipcMain.handle('exchanges:list', () => exchanges.list());

ipcMain.handle('market:candles', (_e, { exchange, pair, interval, limit }) =>
  publicAdapter(exchange).candles(pair, interval, limit));
ipcMain.handle('market:ticker', (_e, { exchange, pair }) =>
  publicAdapter(exchange).ticker(pair));
ipcMain.handle('market:orderbook', (_e, { exchange, pair, depth }) =>
  publicAdapter(exchange).orderbook(pair, depth));
ipcMain.handle('market:trades', (_e, { exchange, pair, limit }) =>
  publicAdapter(exchange).marketTrades(pair, limit));
ipcMain.handle('market:pairs', (_e, { exchange }) =>
  publicAdapter(exchange).pairs());

ipcMain.handle('fleet:list', () => fleet.discover(botRoot()));
ipcMain.handle('fleet:state', (_e, { id }) => fleet.fullState(botRoot(), id));
ipcMain.handle('fleet:control', (_e, { id, action }) => fleet.control(id, action));
ipcMain.handle('fleet:logs', (_e, { id, lines }) => fleet.logs(id, lines));

ipcMain.handle('fleet:balances', async (_e, { id, exchange }) => {
  const creds = fleet.credentials(botRoot(), id);
  if (!creds) return { error: 'sin credenciales (.env sin API key)' };
  return exchanges.create(exchange || 'neoxa', creds).balances();
});
ipcMain.handle('fleet:orders', async (_e, { id, exchange }) => {
  const creds = fleet.credentials(botRoot(), id);
  if (!creds) return { error: 'sin credenciales (.env sin API key)' };
  return exchanges.create(exchange || 'neoxa', creds).openOrders();
});

ipcMain.handle('drawings:load', (_e, { key }) => settings.loadDrawings(key));
ipcMain.handle('drawings:save', (_e, { key, list }) => settings.saveDrawings(key, list));

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
