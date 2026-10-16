#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pruebas del módulo de ejecución con un exchange SIMULADO (no usa red ni claves):
    python test_executor.py
Comprueba: entrada con SL/TP1/TP2, TP1 -> break-even, trailing, SL, time-stop, señal contraria,
SL imposible de colocar -> cierre de emergencia, cancelación externa -> auto-reparación, cierre externo,
límite diario, pausa, modo hedge y one-way, y persistencia.
"""
import os
import sys
import tempfile
import time
import types

import pandas as pd

os.environ.update({
    "LIVE_TRADING": "1", "BINGX_ENV": "demo", "BINGX_API_KEY": "k", "BINGX_API_SECRET": "s",
    "ACCOUNT_USDT": "1000", "RISK_PCT": "1", "MAX_LEV": "3", "LIVE_LEVERAGE": "3", "MAX_LIVE_TRADES": "3",
    "DAILY_LOSS_LIMIT_R": "2", "STATE_FILE": os.path.join(tempfile.mkdtemp(), "state.json"),
    "MAX_BARS": "5", "USE_TIME_STOP": "1", "EXIT_ON_OPP": "1", "PARTIAL_PCT": "50", "TRAIL_MULT": "1.0",
})
T0 = (int(time.time() * 1000) // 900_000) * 900_000 - 900_000
import bot  # noqa: E402
import executor as ex  # noqa: E402


class FakeApi:
    """Exchange mínimo: mercado inmediato, órdenes condicionales que saltan al mover el precio."""

    def __init__(self, hedge=True):
        self.hedge, self.price = hedge, 100.0
        self.orders, self.pos, self.n = {}, {}, 0
        self.fail_place = set()      # tipos de orden que fallan
        self.placed = []             # historial de parámetros enviados
        self.base = "fake"

    # --- público / cuenta ---
    def public_price(self, sym): return self.price
    def balance(self): return {"asset": "USDT", "balance": "1000", "equity": "1000", "availableMargin": "900"}
    def dual_side(self): return self.hedge
    def set_margin_type(self, sym, t): pass
    def set_leverage(self, sym, side, lev): pass

    def positions(self, symbol=None):
        out = []
        for (sym, ps), q in self.pos.items():
            if q > 0 and (symbol is None or symbol == sym):
                out.append({"symbol": sym, "positionSide": ps, "positionAmt": str(q), "avgPrice": str(self.entry_px.get((sym, ps), self.price))})
        return out
    entry_px = {}

    # --- órdenes ---
    def place(self, p):
        if p["type"] in self.fail_place:
            raise ex.ApiError(100400, "simulado: orden rechazada", "trade/order")
        self.n += 1
        oid = str(self.n)
        self.placed.append(dict(p))
        qty = float(p["quantity"])
        ps = p["positionSide"]
        side = "LONG" if (ps == "LONG" or (ps == "BOTH" and p["side"] == "BUY" and p.get("reduceOnly") != "true") or
                          (ps == "BOTH" and p["side"] == "SELL" and p.get("reduceOnly") == "true")) else "SHORT"
        o = {"orderId": oid, "symbol": p["symbol"], "type": p["type"], "side": p["side"], "qty": qty, "ps": side,
             "stop": float(p["stopPrice"]) if "stopPrice" in p else None, "status": "NEW", "avgPrice": "0", "executedQty": "0",
             "closing": ("reduceOnly" in p) or (ps in ("LONG", "SHORT") and ((ps == "LONG") == (p["side"] == "SELL")))}
        self.orders[oid] = o
        if p["type"] == "MARKET":
            self._fill(o, self.price)
        return {"orderId": oid}

    def _fill(self, o, px):
        key = (o["symbol"], o["ps"])
        if o["closing"]:
            q = min(o["qty"], self.pos.get(key, 0.0))
            self.pos[key] = self.pos.get(key, 0.0) - q
        else:
            q = o["qty"]
            self.pos[key] = self.pos.get(key, 0.0) + q
            self.entry_px[key] = px
        o.update(status="FILLED", avgPrice=str(px), executedQty=str(q), commission=str(-q * px * 0.0005))

    def query_order(self, sym, oid):
        return dict(self.orders[str(oid)])

    def cancel_order(self, sym, oid):
        o = self.orders[str(oid)]
        if o["status"] != "NEW":
            raise ex.ApiError(80018, "order already filled/canceled", "cancel")
        o["status"] = "CANCELED"

    def move(self, px):
        """Mueve el precio y dispara las órdenes condicionales."""
        self.price = px
        for o in list(self.orders.values()):
            if o["status"] != "NEW" or o["stop"] is None:
                continue
            sell = o["side"] == "SELL"
            if o["type"] == "STOP_MARKET":
                hit = px <= o["stop"] if sell else px >= o["stop"]
            else:
                hit = px >= o["stop"] if sell else px <= o["stop"]
            if hit:
                self._fill(o, o["stop"])

    def active(self, typ=None):
        return [o for o in self.orders.values() if o["status"] == "NEW" and (typ is None or o["type"] == typ)]


class Notif:
    def __init__(self): self.msgs = []
    def send(self, t): self.msgs.append(t); return True


def mk(hedge=True):
    cfg = bot.make_cfg()
    n = Notif()
    lt = ex.make_live(cfg, n)
    lt.path = ex.Path(tempfile.mkdtemp()) / "live.json"
    lt.api = FakeApi(hedge)
    lt.api.entry_px = {}
    lt.set_contracts([{"symbol": "AAA-USDT", "pricePrecision": 2, "quantityPrecision": 2, "tradeMinQuantity": 0.01,
                       "tradeMinUSDT": 1, "apiStateOpen": "true", "maxLongLeverage": 20},
                      {"symbol": "BBB-USDT", "pricePrecision": 2, "quantityPrecision": 2, "tradeMinQuantity": 0.01,
                       "tradeMinUSDT": 1, "apiStateOpen": "true", "maxLongLeverage": 20}])
    lt.hedge = lt._detect_mode()
    return lt, n, cfg


def sig(side="LONG", sym="AAA-USDT", price=100.0, stop=2.0, tf="15m", ms=T0):
    return types.SimpleNamespace(symbol=sym, tf=tf, side=side, kinds=["G1"], candle_open_ms=ms, price=price,
                                 stop_dist=stop, prec=2, atr=stop / 1.5, stats={})


def candles(*rows, start=T0 + 900_000):
    """rows = (high, low) por vela"""
    return pd.DataFrame([{"time": start + i * 900_000, "open": 100, "high": h, "low": l, "close": (h + l) / 2, "volume": 1}
                         for i, (h, l) in enumerate(rows)])


def check(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        check.fail += 1
check.fail = 0


def t_entry_and_sizing(hedge):
    print(f"\n[1] entrada, SL/TP y tamaño ({'hedge' if hedge else 'one-way'})")
    lt, n, cfg = mk(hedge)
    lt.try_open(sig())
    check("abre 1 operación", len(lt.open) == 1, n.msgs)
    tr = lt.open[0]
    # riesgo 1% de 1000 = 10 USDT; stop 2 -> 5 unidades; notional 500 <= 3000
    check("cantidad = riesgo/stop = 5", abs(tr["qty"] - 5.0) < 1e-9, tr["qty"])
    check("SL a 98, TP1 a 102, TP2 a 106 (long 3R)", (tr["sl"], tr["tp1"], tr["tp2"]) == (98.0, 102.0, 106.0), (tr["sl"], tr["tp1"], tr["tp2"]))
    act = lt.api.active()
    check("3 órdenes activas (SL, TP1, TP2)", len(act) == 3, [(o["type"], o["qty"]) for o in act])
    sl = lt.api.active("STOP_MARKET")[0]
    check("SL por la cantidad total", sl["qty"] == 5.0)
    tps = sorted(o["qty"] for o in lt.api.active("TAKE_PROFIT_MARKET"))
    check("TP1 2.5 y TP2 2.5", tps == [2.5, 2.5], tps)
    ps_ok = all(p["positionSide"] == ("LONG" if hedge else "BOTH") for p in lt.api.placed)
    check("positionSide correcto", ps_ok)
    if not hedge:
        check("one-way: cierres con reduceOnly", all(p.get("reduceOnly") == "true" for p in lt.api.placed[1:]))
        check("one-way: la entrada NO lleva reduceOnly", "reduceOnly" not in lt.api.placed[0])
    check("una segunda señal del mismo símbolo se ignora", (lt.try_open(sig(ms=T0 + 1)), len(lt.open))[1] == 1)


def t_full_winner(hedge):
    print(f"\n[2] TP1 -> break-even -> trailing -> cierre ({'hedge' if hedge else 'one-way'})")
    lt, n, cfg = mk(hedge)
    lt.try_open(sig())
    api = lt.api
    api.move(102.5)           # TP1 (102) salta
    lt.poll()
    tr = lt.open[0]
    check("TP1 detectado: etapa 2", tr["tp1_done"] and tr["stage"] == 2)
    check("SL movido a la entrada (100)", abs(tr["sl"] - 100.0) < 1e-9 and tr["be_done"], tr["sl"])
    sls = api.active("STOP_MARKET")
    check("hay UN solo SL activo, por la cantidad restante", len(sls) == 1 and sls[0]["qty"] == 2.5, [(o["stop"], o["qty"]) for o in sls])
    # sube a 105 (máximo 105): trailing = 105 - 2 = 103
    api.price = 105
    lt.on_candles("AAA-USDT", "15m", candles((105.0, 103.5)), [])
    check("trailing sube el SL a 103", abs(lt.open[0]["sl"] - 103.0) < 1e-9, lt.open[0]["sl"])
    # cae a 103 -> salta el SL
    api.move(102.0)
    lt.poll()
    check("operación cerrada por trailing", len(lt.open) == 0 and lt.closed[-1]["reason"] == "TRAIL", lt.closed[-1]["reason"] if lt.closed else "sin cerrar")
    tr = lt.closed[-1]
    # pnl: 2.5*(102-100) + 2.5*(103-100) = 12.5 ; fees ~ ; R ≈ 12.5/(2*5) = 1.25 menos comisiones
    check("R ≈ 1.2 (neto de comisiones)", 1.1 < tr["R"] < 1.25, tr["R"])
    check("no quedan órdenes activas", len(api.active()) == 0, [(o["type"], o["stop"]) for o in api.active()])
    check("no queda posición", api.positions() == [])


def t_stop_loss():
    print("\n[3] stop loss directo")
    lt, n, cfg = mk()
    lt.try_open(sig())
    lt.api.move(97.5)
    lt.poll()
    check("cerrada por SL", len(lt.open) == 0 and lt.closed[-1]["reason"] == "SL")
    check("R ≈ -1 (con comisiones algo peor)", -1.1 < lt.closed[-1]["R"] < -1.0, lt.closed[-1]["R"])
    check("órdenes TP canceladas", len(lt.api.active()) == 0)
    check("el pnl diario cuenta", lt.day_r() < -1.0)


def t_short():
    print("\n[4] short: TP2 completo")
    lt, n, cfg = mk()
    lt.try_open(sig("SHORT"))
    tr = lt.open[0]
    check("niveles short: SL 102, TP1 98, TP2 96 (2R)", (tr["sl"], tr["tp1"], tr["tp2"]) == (102.0, 98.0, 96.0), (tr["sl"], tr["tp1"], tr["tp2"]))
    check("riesgo short ×0.75 -> cantidad 3.75", abs(tr["qty"] - 3.75) < 1e-9, tr["qty"])
    lt.api.move(95.0)         # salta TP1 y TP2 a la vez
    lt.poll()
    check("cerrada por TP2", len(lt.open) == 0 and lt.closed[-1]["reason"] == "TP2", lt.closed[-1]["reason"] if lt.closed else "")
    check("R positivo", lt.closed[-1]["R"] > 1.0, lt.closed[-1]["R"])


def t_emergency():
    print("\n[5] SL imposible de colocar -> cierre de emergencia")
    lt, n, cfg = mk()
    lt.api.fail_place.add("STOP_MARKET")
    lt.try_open(sig())
    check("no queda operación abierta", len(lt.open) == 0)
    check("no queda posición en el exchange", lt.api.positions() == [], lt.api.positions())
    check("avisa por Telegram", any("STOP LOSS" in m for m in n.msgs))
    check("registrada como EMERGENCY", lt.closed and lt.closed[-1]["reason"] == "EMERGENCY")


def t_selfheal_and_external():
    print("\n[6] auto-reparación y cierre externo")
    lt, n, cfg = mk()
    lt.try_open(sig())
    sl = lt.api.active("STOP_MARKET")[0]
    lt.api.orders[sl["orderId"]]["status"] = "CANCELED"      # alguien cancela el SL
    lt.poll()
    check("SL recolocado", len(lt.api.active("STOP_MARKET")) == 1)
    check("avisa de la cancelación", any("cancelada" in m for m in n.msgs))
    # cierre manual por fuera
    lt.api.pos[("AAA-USDT", "LONG")] = 0.0
    lt.open[0]["opened_ms"] -= 60_000
    lt.poll()
    check("detecta el cierre externo", len(lt.open) == 0 and lt.closed[-1]["reason"] == "EXTERNAL", lt.closed[-1]["reason"] if lt.closed else "")
    check("cancela las órdenes huérfanas", len(lt.api.active()) == 0)


def t_time_and_opposite():
    print("\n[7] time-stop y señal contraria")
    lt, n, cfg = mk()
    lt.try_open(sig())
    for i in range(5):
        df = candles(*[(101.0, 99.5)] * (i + 1))
        lt.on_candles("AAA-USDT", "15m", df, [])
    check("time-stop tras MAX_BARS velas", len(lt.open) == 0 and lt.closed[-1]["reason"] == "TIME", lt.closed[-1]["reason"] if lt.closed else "")
    lt2, n2, _ = mk()
    lt2.try_open(sig())
    opp = types.SimpleNamespace(side="SHORT", candle_open_ms=T0 + 900_000)
    lt2.on_candles("AAA-USDT", "15m", candles((101.0, 99.5)), [opp])
    check("cierra por señal contraria", len(lt2.open) == 0 and lt2.closed[-1]["reason"] == "SIGNAL", lt2.closed[-1]["reason"] if lt2.closed else "")


def t_limits():
    print("\n[8] límites: diario, máximo, pausa, deslizamiento, mínimos")
    lt, n, cfg = mk()
    lt.try_open(sig())
    lt.api.move(97.0); lt.poll()
    lt.api.price = 100
    lt.try_open(sig(sym="BBB-USDT", ms=T0 + 1)); lt.api.move(97.0); lt.poll()
    check("2 pérdidas = -2R -> límite diario", lt.day_r() <= -2.0, lt.day_r())
    lt.api.price = 100
    lt.try_open(sig(sym="AAA-USDT", ms=T0 + 2))
    check("con límite diario no abre", len(lt.open) == 0)
    check("avisa del límite", any("Límite de pérdida diaria" in m for m in n.msgs))

    lt, n, cfg = mk()
    lt.pause(True); lt.try_open(sig())
    check("en pausa no abre", len(lt.open) == 0)
    lt.pause(False); lt.try_open(sig())
    check("al reanudar abre", len(lt.open) == 1)
    msg = lt.panic()
    check("panic cierra todo y pausa", len(lt.open) == 0 and lt.paused and lt.api.positions() == [], msg)

    lt, n, cfg = mk()
    lt.api.price = 101.0       # 0.5R de deslizamiento > 0.25R
    lt.try_open(sig())
    check("rechaza entrada con deslizamiento > MAX_ENTRY_DRIFT_R", len(lt.open) == 0 and any("se movió" in m for m in n.msgs))

    lt, n, cfg = mk()
    lt.api.pos[("AAA-USDT", "LONG")] = 3.0     # posición ajena
    lt.try_open(sig())
    check("no toca símbolos con posición ajena", len(lt.open) == 0 and any("no la toco" in m for m in n.msgs))

    lt, n, cfg = mk()
    lt.try_open(sig(stop=2000.0))              # stop enorme -> cantidad minúscula
    check("cantidad bajo el mínimo -> no opera y avisa", len(lt.open) == 0 and any("mínimo" in m for m in n.msgs), n.msgs)

    lt, n, cfg = mk()
    for k, sym in enumerate(["AAA-USDT", "BBB-USDT"]):
        lt.try_open(sig(sym=sym, ms=T0 + k))
    lt.L.max_live = 2
    lt.set_contracts(lt.specs and [{"symbol": "CCC-USDT", "pricePrecision": 2, "quantityPrecision": 2, "tradeMinQuantity": 0.01, "tradeMinUSDT": 1}])
    lt.try_open(sig(sym="CCC-USDT"))
    check("respeta MAX_LIVE_TRADES", len(lt.open) == 2)


def t_persistence():
    print("\n[9] persistencia y reinicio")
    lt, n, cfg = mk()
    lt.try_open(sig())
    api, path = lt.api, lt.path
    # "reinicio": nuevo LiveTrader que lee el fichero y se reconcilia
    lt2, n2, _ = mk()
    lt2.path = path; lt2._load(); lt2.api = api; lt2.hedge = True
    check("recupera la operación abierta", len(lt2.open) == 1 and lt2.open[0]["symbol"] == "AAA-USDT")
    api.move(102.5)           # el TP1 salta mientras el bot estaba caído
    lt2._reconcile()
    check("al reiniciar detecta el TP1 y mueve el SL a BE", lt2.open[0]["tp1_done"] and abs(lt2.open[0]["sl"] - 100.0) < 1e-9)


def t_signature():
    print("\n[10] firma HMAC y formato de números")
    import hashlib, hmac
    qs = "quantity=1&symbol=BTC-USDT&timestamp=1"
    check("HMAC-SHA256 hex de 64 caracteres", len(hmac.new(b"SECRET", qs.encode(), hashlib.sha256).hexdigest()) == 64)
    check("sin notación científica", ex.fq(5.4e-05, 6) == "0.000054" and ex.fp(1e-05, 8) == "0.00001000")
    check("cantidad siempre hacia abajo", ex.fq(1.999, 2) == "1.99" and ex.floor_to(0.30000000000000004, 2) == 0.3)


def t_wiring():
    print("\n[11] cableado bot.scan() -> ejecutor (con analyze_symbol simulado)")
    lt, n, cfg = mk()
    cfg.min_volume = 0
    d = tempfile.mkdtemp()
    state, tracker = bot.State(os.path.join(d, "s.json")), bot.Tracker(os.path.join(d, "t.json"), cfg)
    stats = {"er": 0.5, "adx": 25.0, "rvol": 1.3, "body": 0.7}
    s1 = bot.Signal(symbol="AAA-USDT", tf="15m", side="LONG", kinds=["G1"], candle_open_ms=T0, price=100.0, atr=1.3333,
                    sl=98.0, tp1=102.0, tp2=106.0, stop_dist=2.0, prec=2, stats=stats, notes=[])
    df = candles((101.0, 99.5), start=T0)
    orig = bot.analyze_symbol
    bot.analyze_symbol = lambda api, c, tf, cfg_, now_ms: ([s1], df)
    try:
        bot.scan(types.SimpleNamespace(), cfg, "15m", [{"symbol": "AAA-USDT", "pricePrecision": 2}], state, n, tracker, lt)
        check("la señal se envía por Telegram", any("LONG" in m and "AAA-USDT" in m for m in n.msgs))
        check("scan abrió la operación real", len(lt.open) == 1 and lt.open[0]["symbol"] == "AAA-USDT")
        check("y la virtual (paper) también", tracker.open_count() == 1)
        # siguiente escaneo: ya hay operación real -> scan debe pasar el df a on_candles sin romperse
        bot.analyze_symbol = lambda api, c, tf, cfg_, now_ms: ([], candles((101.0, 99.5), (101.0, 99.5), start=T0 + 900_000))
        bot.scan(types.SimpleNamespace(), cfg, "15m", [{"symbol": "AAA-USDT", "pricePrecision": 2}], state, n, tracker, lt)
        check("on_candles recibió las velas nuevas (bars=2)", lt.open and lt.open[0]["bars"] == 2, lt.open[0]["bars"] if lt.open else None)
        # sin live: scan sigue funcionando igual que antes
        bot.analyze_symbol = lambda api, c, tf, cfg_, now_ms: ([], df)
        bot.scan(types.SimpleNamespace(), cfg, "15m", [{"symbol": "AAA-USDT", "pricePrecision": 2}], state, n, tracker, None)
        check("scan sin ejecutor funciona (modo solo señales)", True)
    finally:
        bot.analyze_symbol = orig
    msg = lt.startup()
    check("startup devuelve resumen con modo y límites", "ejecución ACTIVA" in msg and "hedge" in msg, msg)
    check("mensajes de stats/abiertas se generan", "Abiertas ahora" in lt.stats_message() and "abiertas" in lt.open_message().lower())


if __name__ == "__main__":
    for h in (True, False):
        t_entry_and_sizing(h)
        t_full_winner(h)
    t_stop_loss(); t_short(); t_emergency(); t_selfheal_and_external(); t_time_and_opposite(); t_limits(); t_persistence(); t_signature(); t_wiring()
    print("\n" + ("TODO CORRECTO ✅" if check.fail == 0 else f"{check.fail} COMPROBACIONES FALLARON ❌"))
    sys.exit(1 if check.fail else 0)
