"""Ejecución en BingX (futuros perpetuos USDT) vía ccxt.
Entrada a mercado + SL en el exchange + TP1 (50%) + TP2 (50%) o trailing ATR tras TP1.
Cada operación cerrada se registra (R neto) y alimenta el meta-modelo y la estacionalidad."""
import json
import logging
import math
import os
import time

import ccxt
import pandas as pd

import config as C
import messages
from store import load_state

log = logging.getLogger("trader")


class Trader:
    def __init__(self, notify, brain=None):
        self.notify = notify
        self.brain = brain
        self.ex = ccxt.bingx({
            "apiKey": C.BINGX_KEY, "secret": C.BINGX_SECRET,
            "enableRateLimit": True, "options": {"defaultType": "swap"},
        })
        if C.DEMO:
            self.ex.set_sandbox_mode(True)
        self.ex.rateLimit = max(self.ex.rateLimit or 0, C.RATE_LIMIT_MS)
        self.ex.load_markets()
        self.state = self._load()
        self.day, self.day_pnl = None, 0.0
        self.ledger = None

    # ---------- estado ----------
    def _load(self):
        return load_state(C.STATE_PATH)

    def _save(self):
        try:
            os.makedirs(C.DATA_DIR, exist_ok=True)
            with open(C.STATE_PATH, "w") as f:
                json.dump(self.state, f)
        except Exception as e:
            log.warning("No se pudo guardar estado: %s", e)

    # ---------- utilidades ----------
    def _retry(self, fn, *a, **k):
        for n in range(4):
            try:
                return fn(*a, **k)
            except Exception as e:
                if n < 3 and ("100410" in str(e) or "RateLimit" in type(e).__name__ or "DDoS" in type(e).__name__):
                    time.sleep(2 * (n + 1))
                    continue
                raise

    def _roll(self):
        d = time.strftime("%Y-%m-%d", time.gmtime())
        if d != self.day:
            self.day, self.day_pnl = d, 0.0

    def can_open(self):
        self._roll()
        return self.day_pnl > -C.DAILY_LOSS_PCT

    def check_account(self):
        eq = self._equity()
        self.notify(f"✅ Cuenta BingX conectada · equity {eq:.2f} USDT · modo posición: {C.POSITION_MODE}")
        return eq

    def prec(self, sym):
        try:
            p = float(self.ex.market(sym).get("precision", {}).get("price"))
        except Exception:
            return 6
        return int(round(-math.log10(p))) if 0 < p < 1 else int(p)

    def _view(self, sym, st):
        pl = st.get("plan") or {}
        return {"side": st["side"].upper(), "symbol": st.get("sym_id") or sym, "tf": C.TF, "kinds": [st["code"]],
                "entry": st["entry"], "tp2": pl.get("tp2") or st.get("tp_final") or st["entry"],
                "prec": st.get("prec", 6), "bars": int((time.time() - st["opened"]) / C.TF_SEC)}

    # ---------- datos ----------
    def fetch_closed(self, symbol, tf, limit):
        raw = self._retry(self.ex.fetch_ohlcv, symbol, tf, limit=limit)
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.ts, unit="ms", utc=True)
        tf_ms = self.ex.parse_timeframe(tf) * 1000
        if df.ts.iloc[-1] + tf_ms > time.time() * 1000:
            df = df.iloc[:-1]
        return df.drop(columns="ts").astype(float)

    def spread_ok(self, sym):
        try:
            tk = self.ex.fetch_ticker(sym)
            b, a = tk.get("bid"), tk.get("ask")
            return not (b and a) or (a - b) / ((a + b) / 2) * 100 <= C.MAX_SPREAD_PCT
        except Exception:
            return True

    def funding(self, sym):
        try:
            return float(self.ex.fetch_funding_rate(sym).get("fundingRate"))
        except Exception:
            return None

    # ---------- helpers ----------
    def _p(self, side, closing=False, extra=None):
        p = {}
        if C.POSITION_MODE == "hedge":
            p["positionSide"] = "LONG" if side == "long" else "SHORT"
        else:
            p["positionSide"] = "BOTH"
            if closing:
                p["reduceOnly"] = True
        if extra:
            p.update(extra)
        return p

    def _amt(self, sym, x): return float(self.ex.amount_to_precision(sym, x))
    def _px(self, sym, x): return float(self.ex.price_to_precision(sym, x))

    def _equity(self):
        b = self.ex.fetch_balance()
        u = b.get("USDT", {})
        return float(u.get("total") or u.get("free") or 0)

    def pos_qty(self, sym, side=None):
        try:
            for p in self.ex.fetch_positions([sym]):
                if p.get("symbol") == sym and (side is None or p.get("side") == side):
                    q = float(p.get("contracts") or 0)
                    if q > 0:
                        return q
        except Exception as e:
            log.warning("fetch_positions: %s", e)
        return 0.0

    def has_position(self, sym):
        return sym in self.state or self.pos_qty(sym) > 0

    def _stop(self, sym, side, qty, price):
        side_c = "sell" if side == "long" else "buy"
        try:
            o = self.ex.create_order(sym, "market", side_c, qty, None,
                                     self._p(side, True, {"stopLossPrice": price}))
        except Exception as e1:
            log.warning("SL unificado falló (%s), probando STOP_MARKET", e1)
            o = self.ex.create_order(sym, "STOP_MARKET", side_c, qty, None,
                                     self._p(side, True, {"stopPrice": price}))
        return o["id"]

    def _tp(self, sym, side, qty, price):
        """Take profit nativo (disparador TAKE_PROFIT_MARKET, igual que el SL). Respaldo: orden límite."""
        side_c = "sell" if side == "long" else "buy"
        errs = []

        def trig1(): return self.ex.create_order(sym, "market", side_c, qty, None, self._p(side, True, {"takeProfitPrice": price}))
        def trig2(): return self.ex.create_order(sym, "TAKE_PROFIT_MARKET", side_c, qty, None, self._p(side, True, {"stopPrice": price}))
        def lim(): return self.ex.create_order(sym, "limit", side_c, qty, price, self._p(side, True))
        for fn in ((lim, trig1, trig2) if C.TP_ORDER == "limit" else (trig1, trig2, lim)):
            try:
                return fn()["id"]
            except Exception as e:
                errs.append(str(e)[:140])
        raise Exception(" | ".join(errs))

    def _is_open(self, sym, oid):
        try:
            return self.ex.fetch_order(oid, sym).get("status") == "open"
        except Exception:
            return True   # ante la duda, asumir que existe (evita duplicar)

    def _plan(self, sym, st):
        s = 1 if st["side"] == "long" else -1
        e, r, qty = st["entry"], st["risk"], st["qty"]
        m = self.ex.market(sym)
        min_amt = (m.get("limits", {}).get("amount", {}).get("min") or 0)
        q1 = self._amt(sym, qty * C.TP1_FRAC)
        q2 = self._amt(sym, qty - q1)
        if C.PARTIAL_MODE and q1 >= min_amt and q2 >= min_amt:
            return {"partial": True, "q1": q1, "q2": q2, "tp1": self._px(sym, e + s * C.TP1_R * r),
                    "tp2": None if C.TRAIL_ON else self._px(sym, e + s * C.TP2_R * r)}
        return {"partial": False, "q": qty, "tp": self._px(sym, e + s * C.TP_SINGLE_R * r)}

    def _place_tps(self, sym, st):
        """Coloca los TP que falten según el plan. Devuelve (ok, texto, errores)."""
        pl, side, errs = st["plan"], st["side"], []
        if pl["partial"]:
            st["trail"] = bool(C.TRAIL_ON)
            if not st["tp1_hit"] and not st["tp1_id"]:
                try:
                    st["tp1_id"] = self._tp(sym, side, pl["q1"], pl["tp1"])
                except Exception as e:
                    errs.append(f"TP1: {e}")
            if pl["tp2"] and not st["tp2_id"]:
                try:
                    st["tp2_id"] = self._tp(sym, side, pl["q2"], pl["tp2"])
                    st["tp_final"] = pl["tp2"]
                except Exception as e:
                    errs.append(f"TP2: {e}")
            need = ((not st["tp1_hit"] and not st["tp1_id"]) or (pl["tp2"] and not st["tp2_id"]))
            txt = f"TP1 {pl['tp1']} ({pl['q1']})" + (f" · TP2 {pl['tp2']} ({pl['q2']})" if pl["tp2"]
                                                      else f" · resto con trailing {C.TRAIL_ATR}×ATR")
        else:
            st["tp1_done"] = True
            if not st["tp2_id"]:
                try:
                    st["tp2_id"] = self._tp(sym, side, pl["q"], pl["tp"])
                    st["tp_final"] = pl["tp"]
                except Exception as e:
                    errs.append(f"TP: {e}")
            need = not st["tp2_id"]
            txt = f"TP {pl['tp']}"
        st["tp_ok"] = not need
        return st["tp_ok"], txt, errs

    def _cancel(self, sym, oid):
        if oid:
            try:
                self.ex.cancel_order(oid, sym)
            except Exception:
                pass

    def _closed(self, sym, oid):
        try:
            return self.ex.fetch_order(oid, sym).get("status") == "closed"
        except Exception:
            return False

    def _flatten(self, sym, side):
        q = self.pos_qty(sym, side)
        if q > 0:
            side_c = "sell" if side == "long" else "buy"
            self.ex.create_order(sym, "market", side_c, q, None, self._p(side, True))

    # ---------- aprendizaje ----------
    def _record(self, st, exit_price, reason=""):
        """R neto de la operación -> freno diario + cerebro (meta + estacionalidad)."""
        s = 1 if st["side"] == "long" else -1
        risk = st["risk"]
        real = C.TP1_FRAC * C.TP1_R if st.get("tp1_hit") else 0.0
        rem = (1 - C.TP1_FRAC) if st.get("tp1_hit") else 1.0
        R = real + rem * s * (exit_price - st["entry"]) / risk
        Rn = R - C.COST_PCT / (risk / st["entry"] * 100)
        self._roll()
        self.day_pnl += Rn * C.RISK_PCT
        if self.ledger is not None:
            self.ledger.add_closed({"mode": "real", "symbol": st.get("sym_id", ""), "side": st["side"].upper(),
                                    "tf": C.TF, "kinds": [st["code"]], "R": Rn, "reason": reason,
                                    "exit_ms": int(time.time() * 1000),
                                    "bars": int((time.time() - st["opened"]) / C.TF_SEC)})
            self.ledger.save()
        if self.brain and st.get("x"):
            self.brain.record({"x": st["x"], "y": int(Rn > 0), "r": Rn, "hour": st["hour"],
                               "wday": st["wday"], "ts": time.time()})
        return Rn

    # ---------- abrir ----------
    def open_trade(self, sym, sig, mult=1.0):
        side = sig["side"]
        m = self.ex.market(sym)
        try:
            self.ex.set_margin_mode("isolated", sym)
        except Exception:
            pass
        try:
            self.ex.set_leverage(C.LEVERAGE, sym,
                                 {"side": ("LONG" if side == "long" else "SHORT") if C.POSITION_MODE == "hedge" else "BOTH"})
        except Exception as e:
            log.warning("set_leverage: %s", e)

        equity = self._equity()
        if equity <= 0:
            self.notify(f"⚠️ {sym}: balance 0, no se opera")
            return
        risk = sig["risk"]
        mult = mult * sig.get("risk_mult", 1.0)
        qty = equity * C.RISK_PCT / 100.0 * mult / risk
        qty = self._amt(sym, min(qty, equity * C.MAX_LEV / sig["entry"]))
        min_amt = (m.get("limits", {}).get("amount", {}).get("min") or 0)
        min_cost = (m.get("limits", {}).get("cost", {}).get("min") or 0)
        if qty < min_amt or qty * sig["entry"] < min_cost:
            self.notify(f"⚠️ {sym}: tamaño {qty} por debajo del mínimo del exchange; señal omitida")
            return

        o = self.ex.create_order(sym, "market", "buy" if side == "long" else "sell", qty, None, self._p(side))
        fill = o.get("average") or o.get("price")
        if not fill:
            try:
                fill = self.ex.fetch_order(o["id"], sym).get("average")
            except Exception:
                fill = None
        fill = float(fill or sig["entry"])
        s = sig["s"]
        sl = self._px(sym, fill - s * risk)

        try:
            sl_id = self._stop(sym, side, qty, sl)
        except Exception as e:
            self._flatten(sym, side)
            self.notify(f"🚨 {sym}: no pude poner el SL ({e}). Posición CERRADA por seguridad.")
            return

        st = {"side": side, "code": sig["code"], "entry": fill, "risk": risk, "qty": qty, "atr": sig["atr"],
              "sl": sl, "sl_id": sl_id, "opened": time.time(), "tp1_id": None, "tp2_id": None,
              "tp1_done": False, "tp1_hit": False, "trail": False, "tp_final": None, "tp_ok": False, "plan": None, "sym_id": m["id"], "prec": self.prec(sym),
              "x": sig["x"], "hour": sig["hour"], "wday": sig["wday"]}
        st["plan"] = self._plan(sym, st)
        ok, tp_txt, errs = self._place_tps(sym, st)
        if not ok:
            tp_txt += "\n⚠️ TP NO colocado: " + " ; ".join(errs)[:400] + "\n(el bot reintenta cada minuto; el SL sí está activo)"

        self.state[sym] = st
        self._save()
        self.notify(
            f"✅ <b>Orden ejecutada en BingX</b> · {m['id']} · {side.upper()} [{sig['code']}]\n"
            f"Entrada {fill} · cantidad {qty} · SL {sl}\n{tp_txt}\n"
            f"Riesgo {C.RISK_PCT * mult:.2f}% del capital (≈{equity * C.RISK_PCT * mult / 100:.2f} USDT)")

    # ---------- trailing (se llama al cerrar cada vela) ----------
    def trail_update(self, sym, close):
        st = self.state.get(sym)
        if not st or not st.get("trail") or not st.get("tp1_hit"):
            return
        s = 1 if st["side"] == "long" else -1
        new = self._px(sym, close - s * C.TRAIL_ATR * st["atr"])
        if s * (new - st["sl"]) <= 0:
            return
        qty = self.pos_qty(sym, st["side"])
        if qty <= 0:
            return
        try:
            new_id = self._stop(sym, st["side"], qty, new)
            self._cancel(sym, st["sl_id"])
            st["sl_id"], st["sl"] = new_id, new
            self._save()
            self.notify(f"📈 Trailing {sym}: SL → {new}")
        except Exception as e:
            self.notify(f"⚠️ {sym}: fallo al mover trailing ({e})")

    # ---------- gestionar ----------
    def manage(self, sym):
        st = self.state.get(sym)
        if not st:
            return
        side = st["side"]
        try:
            open_ids = {o["id"] for o in self.ex.fetch_open_orders(sym)}
            qty_now = self.pos_qty(sym, side)
        except Exception as e:
            log.warning("manage %s: %s", sym, e)
            return

        if qty_now <= 0:
            tp_hit = bool(st["tp2_id"]) and self._closed(sym, st["tp2_id"])
            sl_hit = self._closed(sym, st["sl_id"])
            for k in ("sl_id", "tp1_id", "tp2_id"):
                self._cancel(sym, st.get(k))
            ev, px, rn = None, None, None
            if tp_hit and st.get("tp_final"):
                ev, px = "TP2", st["tp_final"]
            elif sl_hit:
                px = st["sl"]
                ev = "SL" if not st.get("tp1_hit") else ("BE" if abs(st["sl"] - st["entry"]) < 1e-12 else "TRAIL")
            view = self._view(sym, st)
            if ev:
                rn = self._record(st, px, ev)
                self.notify(messages.event_message(view, ev, rn, px))
            else:
                self.notify(f"ℹ️ Posición cerrada · {view['symbol']} [{st['code']}]")
            self.state.pop(sym, None)
            self._save()
            return

        reduced = bool(st.get("plan") and st["plan"].get("partial")) and qty_now <= st["qty"] * (1 - C.TP1_FRAC) * 1.05
        if st["tp1_id"] and not st["tp1_done"] and (reduced or (st["tp1_id"] not in open_ids and self._closed(sym, st["tp1_id"]))):
            st["tp1_done"] = st["tp1_hit"] = True
            self._cancel(sym, st["sl_id"])
            try:
                st["sl_id"] = self._stop(sym, side, qty_now, self._px(sym, st["entry"]))
                st["sl"] = st["entry"]
                self.notify(messages.event_message(self._view(sym, st), "TP1"))
            except Exception as e:
                self.notify(f"🚨 {sym}: TP1 hecho pero fallo al mover SL a BE ({e}). Revisa a mano.")
            self._save()

        # auto-reparación: coloca el TP que falte (también en posiciones abiertas con versiones anteriores)
        if not st.get("tp_ok") and time.time() - st.get("tp_try", 0) > 60:
            st["tp_try"] = time.time()
            if not st.get("plan"):
                st["plan"] = self._plan(sym, st)
                for k in ("tp1_id", "tp2_id"):
                    if st.get(k) and not self._is_open(sym, st[k]) and not (k == "tp1_id" and st["tp1_hit"]):
                        st[k] = None
            ok, txt, errs = self._place_tps(sym, st)
            if ok:
                self.notify(f"🔧 TP colocado en {sym}: {txt}")
            elif not st.get("tp_err_sent"):
                st["tp_err_sent"] = True
                self.notify(f"⚠️ {sym}: sigo sin poder poner el TP: " + " ; ".join(errs)[:400])
            self._save()

        if time.time() - st["opened"] >= C.MAX_BARS * C.TF_SEC:
            for k in ("sl_id", "tp1_id", "tp2_id"):
                self._cancel(sym, st.get(k))
            try:
                px = float(self.ex.fetch_ticker(sym)["last"])
                self._flatten(sym, side)
                rn = self._record(st, px, "TIME")
                self.notify(messages.event_message(self._view(sym, st), "TIME", rn, px))
                self.state.pop(sym, None)
                self._save()
            except Exception as e:
                self.notify(f"🚨 {sym}: fallo cierre por tiempo ({e})")
