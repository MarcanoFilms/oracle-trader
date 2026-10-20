# Motor: acumulador de XBT (escalera + lotes + trailing)

Bot de acumulación spot para [neoxa.exchange](https://neoxa.exchange), calibrado
en el par **BTCB2_BTC** (XBT/BTC — sin fiat: el capital de trabajo es BTC y lo
que se acumula es XBT). La estrategia es agnóstica del par: cualquier mercado
base/quote del exchange funciona con la misma configuración.

> ⚠️ **Esto opera dinero real.** Arranca SIEMPRE con `dry_run: true`, valida el
> comportamiento contra tu cuenta, y pasa a LIVE solo cuando entiendas cada
> parámetro. Nada de esto es consejo financiero; úsalo bajo tu responsabilidad.

## La estrategia (`neoxa_bot/accum.py`, clase `AccumBot`)

Escalera de compras bajo el precio + venta por lote. Sin centro EMA, sin ventas
"sembradas": el ancla es el precio actual.

- **Escalera**: N compras post-only a pasos geométricos bajo el precio
  (`step_pct`, primer escalón más somero con `first_step_pct` para cazar
  retrocesos chicos). Si el precio sube y la escalera queda lejos
  (> `reanchor_steps` pasos), se cancela y recoloca más arriba (cancelar compras
  sin fill es gratis). Si el precio baja, las compras se llenan (DCA acotado por
  `quote_capital`).
- **Lotes**: cada compra ejecutada crea un lote con su venta a
  `costo×(1+paso)`, dimensionada para recuperar el 100% del quote invertido
  (fees incluidas). El **residuo queda en la moneda base** (XBT) en cada ciclo.
- **Trailing take-profit**: al acercarse el precio al objetivo de un lote, el bot
  cancela la venta base y sigue el pico del bid; vende al retroceder `trail_pct`
  desde el máximo — captura spikes que una venta fija dejaría en la mesa.
  **Piso sagrado**: jamás se vende bajo el objetivo; si el momentum se apaga,
  vuelve la orden maker al objetivo.
- **Compound**: una fracción del residuo de cada ciclo (`compound_pct`) se
  cosecha en quote y agranda la escalera; el resto queda en el stack.
- **Preservación de munición**: la escalera no recoloca compras pegadas a lotes
  ya abiertos (`rebuy_block_frac`) — no se recarga el mismo rango en un lateral.
- **Invariantes**:
  1. Jamás una venta sin lote comprado más abajo que la respalde.
  2. Jamás vender bajo `costo×(1+paso)`; las ventas de lotes no se cancelan.
  3. La reserva (`xbt_reserve`) es intocable.
  4. Compras solo bajo el touch (post-only + `touch_buffer` contra el AMM).
- **Reconciliación**: una orden que desaparece de `status=active` se busca en
  `filled`/`cancelled` y se procesa por su `quantity_filled` real (fills
  parciales y cancelaciones externas; "desaparecida" ≠ "ejecutada").
- En caída fuerte el capital se agota comprando y el bot espera la recuperación
  con los lotes abiertos; en subida fuerte los lotes se liquidan con residuo y la
  escalera persigue el precio. Gana en lateral/choppy.

¿Por qué los invariantes? La v1 (`neoxa_bot/bot.py`, retirada) era un grid
simétrico con centro EMA: en un dump la EMA quedó sobre el precio, la banda
entera se volvió ventas y liquidó stack vendiendo abajo. La v2 hace ese modo de
fallo estructuralmente imposible.

## Uso

```bash
cd bot
python -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml   # editar: capital, reserva, geometría
cp .env.example .env                 # API key del exchange (+ Telegram opcional)

./venv/bin/python run.py --status    # lotes, escalera, residuo acumulado
./venv/bin/python run.py --once      # una iteración (coloca escalera) y sale
./venv/bin/python run.py             # loop completo (empezar con dry_run: true)
./venv/bin/python backtest_accum.py  # backtest con velas reales (mismo código)
```

Como servicio: plantillas en [`systemd/`](systemd/) (`Restart=always`; activar
`loginctl enable-linger` para que sobreviva al logout). Al parar el servicio se
cancelan **solo las compras**; las ventas de lotes quedan trabajando y se
reconcilian al arrancar. `state.json` guarda lotes/escalera/trades.

**Multi-cuenta**: `run.py --instance NOMBRE` usa
`instances/NOMBRE/{config.yaml,.env,state.json,logs}` — plantilla en
`instances/_plantilla/`, servicio `neoxa-bot@NOMBRE`. Cada cuenta es
independiente (su capital, su reserva, su estado); con varias cuentas conviene
desfasar la geometría para cubrir más rango. `watchdog.py` vigila todas las
instancias habilitadas y avisa por Telegram.

**Calibrar antes de operar**: `backtest_accum.py` corre el MISMO código del bot
contra velas reales con un exchange simulado — cualquier cambio de geometría
se valida ahí primero. `analyze.py` resume el rendimiento real (ritmo de
ciclos, residuo/día, duración por lote) para recalibrar con datos vivos.

## Notas de la API (neoxa.exchange)

- Base `https://neoxa.exchange/api`, headers `X-API-Key` / `X-API-Secret`, 600 req/min.
- `POST /exchange/order` {pair, side, quantity, price, postOnly};
  `DELETE /exchange/order/{id}`; `GET /exchange/orders?status=active|filled|cancelled`
  (trae `quantity_filled` — base de la reconciliación); `/exchange/trades/my`
  (sin order_id); `/exchange/balances`; velas `GET /exchange/candles/{pair}`.
- El libro tiene un AMM (`isAmm` en el orderbook): contraparte continua. Un
  post-only que cruzaría el pool se rechaza ("would cross the AMM pool").
- El escrow de una compra es `qty×price×(1+fee)` — dejar colchón de capital.
- **API keys**: permiso de trade marcado y campo de IP whitelist EN BLANCO
  (rellenarlo con tu IP suele rechazar las peticiones si tu IP rota).
