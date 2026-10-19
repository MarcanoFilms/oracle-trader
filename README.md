# Oracle Trader

Dashboard de escritorio (Electron) para administrar la **flota de bots de trading** de forma local, con gráficos estilo TradingView.

![estilo](https://img.shields.io/badge/estilo-trading%20desk-f7931a) ![local](https://img.shields.io/badge/100%25-local-26a69a)

## Qué hace

- **Gráfico estilo TradingView** (KLineCharts) del par que opera el bot (XBT/BTC en neoxa.exchange), con velas en vivo (1m–1d), indicadores (MA/EMA/BOLL/VOL/MACD/RSI/KDJ) y **herramientas de dibujo** (líneas de tendencia, horizontales, canales, fibonacci, notas) que **persisten por par** entre sesiones.
- **La estrategia pintada sobre el precio**: la escalera de compras (líneas verdes), los objetivos de venta de cada lote (líneas naranjas) y cada trade del bot como flecha ▲/▼ sobre la vela donde ocurrió.
- **Flota multi-cuenta**: descubre automáticamente la cuenta principal y las instancias (`instances/*`) del bot, muestra su estado systemd en vivo y permite **arrancar / parar / reiniciar** cada bot con confirmación.
- **Detalle por cuenta**: resumen (ciclos, residuo, residuo/día, compound, capital dinámico, reserva), lotes abiertos con distancia al objetivo y estado del trailing, escalera, historial de trades con margen por ciclo, órdenes y balances en vivo del exchange (API autenticada), logs del journal y config.
- **Mercado en vivo**: libro de órdenes (con las órdenes del AMM marcadas `≋` y el estado del pool), trades del mercado y ticker 24h.

## Uso

```bash
npm install        # una vez
./oracle-trader.sh # o npm start
```

También queda instalado en el menú de aplicaciones como **Oracle Trader** (`~/.local/share/applications/oracle-trader.desktop`).

## Arquitectura

```
main.js                 proceso principal: toda la E/S vive aquí
src/
  settings.js           config de la app + persistencia de dibujos (~/.config/oracle-trader)
  fleet.js              descubre cuentas del bot, lee config/estado, systemd, journal
  exchanges/
    index.js            registro de adaptadores
    neoxa.js            adaptador neoxa.exchange (REST)
preload.js              puente IPC con lista blanca de canales
renderer/               UI (sin framework, sin build): index.html + app.js + chart.js
```

Decisiones de diseño:

- **Las credenciales jamás cruzan al renderer**: las lee `fleet.js` de los `.env` del bot y las usa el proceso principal; la UI solo recibe datos.
- **Solo lectura contra el exchange**: el dashboard observa (velas, libro, balances, órdenes); operar es trabajo del bot. El control es sobre el *servicio* (systemd), no sobre las órdenes.
- **El bot es la fuente de verdad**: lotes, escalera y trades salen de su `state.json`; no se duplica estado.

## Multi-exchange (roadmap)

La estrategia (escalera + lotes + trailing) es agnóstica del par y del exchange; el dashboard también. Para añadir un exchange basta con implementar la interfaz del adaptador en `src/exchanges/`:

```
candles(pair, interval, limit) · ticker(pair) · orderbook(pair, depth)
marketTrades(pair, limit) · balances() · openOrders()
```

Pendiente / ideas (algunas inspiradas en OctoBot):

- [x] ~~Historia profunda en el gráfico~~ — fusiona el archivo [xbt-market-data](https://github.com/MarcanoFilms/xbt-market-data) con la ventana live de la API (1d/4h desde el génesis del mercado).
- [ ] Adaptador **nonkyc.io** (misma familia de API que neoxa) y genérico vía **ccxt** para exchanges grandes.
- [ ] Registro de cuentas con exchange+par+credenciales arbitrarios (hoy la flota se descubre del directorio del bot de neoxa).
- [ ] Curva de equity / residuo acumulado por cuenta (gráfico histórico).
- [ ] Backtesting visual: pintar el resultado de `backtest_accum.py` sobre el gráfico.
- [ ] Editor de config con validación y aviso de reinicio.
- [ ] Perfiles de estrategia por cuenta (geometrías distintas de escalera).

## Requisitos

- Node 20+ (la raíz del bot se configura en `~/.config/oracle-trader/settings.json`; por defecto `~/Projects/neoxa-bot`)
- Los bots corren como servicios systemd de usuario (`neoxa-bot.service`, `neoxa-bot@INSTANCIA.service`)
