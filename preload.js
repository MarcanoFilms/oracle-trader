// Puente seguro renderer ↔ main. Solo canales de la lista blanca.
const { contextBridge, ipcRenderer } = require('electron');

const CHANNELS = new Set([
  'settings:get', 'settings:set',
  'exchanges:list',
  'market:candles', 'market:ticker', 'market:orderbook', 'market:trades', 'market:pairs',
  'fleet:list', 'fleet:state', 'fleet:control', 'fleet:logs',
  'fleet:balances', 'fleet:orders',
  'drawings:load', 'drawings:save',
  'tray:update',
]);

contextBridge.exposeInMainWorld('oracle', {
  invoke(channel, payload) {
    if (!CHANNELS.has(channel)) return Promise.reject(new Error(`canal no permitido: ${channel}`));
    return ipcRenderer.invoke(channel, payload || {});
  },
});
