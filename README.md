# Escáner Granville BingX → Telegram → (opcional) ejecución en BingX · v3

Escanea los futuros perpetuos de BingX, detecta señales **G1/G3/G4** (long) y **G5/G7/G8** (short) al cierre de
cada vela y te avisa por Telegram. **Novedad v3:** puede abrir y gestionar esas operaciones **en tu cuenta de
BingX**, primero en el entorno **demo (dinero virtual)** y, cuando tú lo decidas, con **dinero real**.

> ⚠️ **Aviso.** Opera futuros con apalancamiento: puedes perder todo el capital que pongas. Que una señal exista no
> significa que tenga ventaja: mídelo con el backtest. El código de ejecución se ha probado contra un exchange
> **simulado** (`test_executor.py`), no contra BingX; por eso existe `--live-selftest`, que debes pasar en demo antes
> de usar dinero real. Esto no es consejo financiero.

## Archivos
| Archivo | Para qué sirve |
|---|---|
| `bot.py` | Escáner, señales, seguimiento virtual, backtester, comandos de Telegram |
| `executor.py` | **Nuevo.** Cliente firmado de BingX y gestor de operaciones reales |
| `test_executor.py` | **Nuevo.** Pruebas del ejecutor con un exchange simulado (`python test_executor.py`) |
| `railway.json`, `requirements.txt` | Despliegue en Railway |
| `.env.example` | Todas las variables, comentadas (para uso local) |
| `railway_variables_DEMO.txt` / `_REAL.txt` | Bloques para pegar en Railway → Variables → Raw Editor. Úsalos como plantilla y **no guardes tus claves reales dentro de ficheros que subas a GitHub** |
| `granville_senales_indicador.pine` | Indicador de TradingView (sin cambios) |

## Qué hace con cada señal (si `LIVE_TRADING=1`)
1. **Comprueba** que no esté en pausa, que no se haya alcanzado la pérdida diaria, que no haya ya una operación o una
   posición tuya en ese símbolo, que haya hueco (`MAX_LIVE_TRADES`) y que el precio no se haya alejado más de
   `MAX_ENTRY_DRIFT_R` desde el cierre de la vela.
2. **Calcula el tamaño** por riesgo (`ACCOUNT_USDT` × `RISK_PCT`, ×`SHORT_RISK_MULT` en cortos), limitado por `MAX_LEV` y
   redondeado a la precisión/mínimos del contrato. Usa `min(ACCOUNT_USDT, saldo real)`.
3. **Entra a mercado** (margen aislado + apalancamiento `LIVE_LEVERAGE`) y recalcula SL/TP1/TP2 desde el precio real de
   ejecución, con las mismas distancias en R que tu estrategia.
4. **Coloca en BingX**: SL (`STOP_MARKET`) por toda la cantidad, TP1 parcial y TP2 (`TAKE_PROFIT_MARKET`).
   Si no consigue colocar el SL, **cierra la posición al instante**.
5. **Tras TP1**: coloca el nuevo SL en la entrada y *después* cancela el viejo (nunca queda sin protección); luego sigue
   el trailing de `TRAIL_MULT` R en cada vela cerrada.
6. **Cierra a mercado** por time-stop (`MAX_BARS`), señal contraria (`EXIT_ON_OPP`) o `/cerrartodo`.
7. **Vigila cada `LIVE_POLL_SEC` s**: si alguien cancela una orden la recoloca, si cierras la posición a mano lo detecta,
   y calcula el resultado real en R con precios de ejecución y comisiones.

El seguimiento virtual (paper) sigue funcionando en paralelo: puedes comparar `/stats` (simulado) con `/real`.

## Camino recomendado
### 1. Medir (sin claves)
```
pip install -r requirements.txt
cp .env.example .env            # rellena TELEGRAM_TOKEN y TELEGRAM_CHAT_ID
python bot.py --get-chat-id     # tras escribir /start a tu bot
python bot.py --backtest top:30 --days 60 --tf 15m
```
En "Por tipo de señal" deja en `SIGNAL_TYPES` solo los tipos con n ≥ 30 y **"ventaja probable"**.

### 2. Demo (dinero virtual)
1. En BingX crea una clave de API del **entorno demo (VST)** con permisos de lectura y trading de futuros. Confirma el
   procedimiento actual en la documentación de BingX: las claves demo son independientes de las reales.
2. En `.env`: `LIVE_TRADING=1`, `BINGX_ENV=demo`, `BINGX_API_KEY=...`, `BINGX_API_SECRET=...`
3. Comprueba y haz el autotest (abre y cierra una posición mínima y prueba cada tipo de orden):
```
python bot.py --live-check
python bot.py --live-selftest BTC-USDT
```
   Debe terminar en `AUTOTEST CORRECTO ✅`. Si algo sale con ✗, **no sigas**: copia el mensaje y revísalo.
4. Despliega en Railway con `railway_variables_DEMO.txt` y déjalo **al menos 2-4 semanas**. Compara `/real` con `/stats`.

### 3. Dinero real
1. Crea una clave **real** en BingX: permisos de lectura y trading de futuros, **sin retiros**. Lo ideal es una
   **subcuenta** solo para el bot con el saldo que estés dispuesto a perder. Si Railway no te da IP fija en tu plan y no
   puedes restringir la clave por IP, razón de más para minimizar permisos y saldo.
2. Pasa `--live-check` con la clave real (`BINGX_ENV=live`). El autotest en real exige `--force-live`.
3. Empieza pequeño: `ACCOUNT_USDT` bajo, `RISK_PCT` 0.25–0.5, `MAX_LIVE_TRADES` 1–2, `MAX_LEV` 1–2. Sube solo si los
   resultados reales se parecen a los del backtest y la demo.

## Railway
1. Sube el repo a GitHub (el `.gitignore` ya excluye `.env`, `*.env` y los ficheros de estado).
2. Railway → *New Project → Deploy from GitHub repo*.
3. **Antes de nada**: servicio → *Settings → Volumes* → crea un Volume montado en `/data`. Sin él, un reinicio borra la
   memoria del bot (los SL/TP seguirían en BingX, pero se pierde el trailing y las estadísticas).
4. *Variables → Raw Editor* → pega `railway_variables_DEMO.txt` (o `_REAL.txt`) y sustituye los `PEGA_AQUI_...`.
5. Deploy. Deberías recibir en Telegram "Escáner Granville v3 iniciado" y el resumen de la ejecución (modo, saldo, límites).

Para pasar de demo a real: cambia `BINGX_ENV=live` y las dos claves. Nada más.

## Variables de ejecución
| Variable | Por defecto | Significado |
|---|---|---|
| `LIVE_TRADING` | `0` | `1` = el bot abre operaciones en BingX |
| `BINGX_ENV` | `demo` | `demo` = VST virtual · `live` = **dinero real** |
| `BINGX_API_KEY` / `BINGX_API_SECRET` | — | Claves del entorno elegido |
| `POSITION_MODE` | `auto` | `auto` lo detecta; `hedge` u `oneway` si no puede |
| `LIVE_LEVERAGE` | `3` | Apalancamiento (margen aislado); nunca menor que `ceil(MAX_LEV)` |
| `MAX_LIVE_TRADES` | `3` | Operaciones reales abiertas a la vez (una por símbolo) |
| `MAX_ENTRY_DRIFT_R` | `0.25` | No entra si el precio se movió más de esta fracción de R |
| `DAILY_LOSS_LIMIT_R` | `3` | Con -N R en el día (UTC) no abre más hasta mañana (`0` = sin límite) |
| `TRIGGER_PRICE` | `MARK_PRICE` | Precio que dispara SL/TP |
| `LIVE_POLL_SEC` | `10` | Frecuencia de vigilancia |
| `LIVE_TRADES_FILE` | junto a `STATE_FILE` | Registro de operaciones reales |

El resto (`RISK_PCT`, `MAX_LEV`, `PARTIAL_PCT`, `TRAIL_MULT`, `MAX_BARS`, …) es el mismo que ya tenías.
Con ejecución real usa `MAX_OPEN_TRADES=0` (ese tope es de las operaciones *virtuales*; las reales las limita `MAX_LIVE_TRADES`).

## Comandos de Telegram (solo desde tu chat)
`/stats` · `/semana` · `/abiertas` · `/estado` · **`/real`** (resultados y operaciones reales) · **`/pausa`** (no abre
nuevas; las abiertas siguen gestionadas) · **`/reanudar`** · **`/cerrartodo`** (cierra a mercado todo lo que abrió el bot y pausa).

## Diferencias entre el backtest y la realidad
- Los stops reales tienen deslizamiento; el backtest sale en el precio exacto del nivel.
- No se modelan los funding rates.
- Si SL y TP1 caen en la misma vela, el backtest asume el SL primero.
- El backtest no aplica el filtro de delta y tiene sesgo de supervivencia.
- Si la cantidad es tan pequeña que no se puede partir en dos (mínimo del contrato), esa operación va **sin parcial**: SL y TP2 por todo.
- `PARTIAL_PCT` debe estar entre 1 y 99; con 100 no se usa el parcial.
- Correlación: varias posiciones a la vez en criptos suelen moverse juntas; `MAX_LIVE_TRADES` × `RISK_PCT` es tu riesgo conjunto aproximado.

## Seguridad y límites del diseño
- Solo gestiona lo que abrió él; no toca posiciones ajenas (y no entra en símbolos donde ya tengas una).
- Las entradas no se reintentan a ciegas: ante un error de red comprueba la posición antes de decidir.
- Si BingX no responde, los SL/TP ya colocados siguen en el exchange; avisa por Telegram tras ~1 minuto de fallos.
- Las claves solo se leen de variables de entorno y no se escriben en los registros.

## Solución de problemas
| Síntoma | Qué hacer |
|---|---|
| "No pude detectar el modo de posición" | Define `POSITION_MODE=hedge` u `oneway` según tu cuenta |
| "tamaño … por debajo del mínimo del contrato" | Sube `ACCOUNT_USDT` o `RISK_PCT`, o sube `MIN_VOLUME_24H_USDT` para evitar monedas con mínimos altos |
| "no pude fijar el apalancamiento" | Configúralo a mano en BingX para ese par, o revisa el máximo del contrato |
| El autotest da ✗ en SL/TP | Copia el mensaje exacto: indica qué parámetro rechaza BingX |
| Tras un redeploy no veo mis operaciones | Falta el Volume en `/data` o `STATE_FILE` no apunta a él |
| `/real` dice que no está activa | `LIVE_TRADING=0`, faltan claves o falló el arranque (mira el aviso en Telegram) |
