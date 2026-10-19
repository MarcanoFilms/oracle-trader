// Registro de adaptadores de exchange. Para soportar uno nuevo basta con
// implementar la interfaz (candles/ticker/orderbook/marketTrades/balances/
// openOrders) y añadirlo aquí — el resto del dashboard no cambia.
const NeoxaAdapter = require('./neoxa');

const ADAPTERS = {
  [NeoxaAdapter.id]: NeoxaAdapter,
  // futuros: nonkyc, binance/kraken vía ccxt, etc.
};

function create(id, creds) {
  const A = ADAPTERS[id];
  if (!A) throw new Error(`exchange desconocido: ${id}`);
  return new A(creds);
}

function list() {
  return Object.values(ADAPTERS).map(A => ({
    id: A.id,
    label: A.label,
    intervals: A.intervals,
  }));
}

module.exports = { create, list };
