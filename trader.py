"""Ejecución en BingX (futuros perpetuos USDT) vía ccxt.
Entrada a mercado + SL en el exchange + TP1 (50%) + TP2 (50%) o trailing ATR tras TP1.
Cada operación cerrada se registra (R neto) y alimenta el meta-modelo y la estacionalidad."""
import json
import logging
import os
import time

import ccxt
import pandas as pd

import config as C

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
        self.ex.load_markets()
        self.state = self._load()

    # ---------- estado ----------
    def _load(self):
        try:
            with open(C.STATE_PATH) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        try:
            os.makedirs(C.DATA_DIR, exist_ok=True)
            with open(C.STATE_PATH, "w") as f:
                json.dump(self.state, f)
        except Exception as e:
            log.warning("No se pudo guardar estado: %s", e)

    # ---------- datos ----------
    def fetch_closed(self, symbol, tf, limit):
        raw = self.ex.fetch_ohlcv(symbol, tf, limit=limit)
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
        side_c = "sell" if side == "long" else "buy"
        return self.ex.create_order(sym, "limit", side_c, qty, price, self._p(side, True))["id"]

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
    def _record(self, st, exit_price, final_target=False):
        """Calcula R neto de la operación y la envía al cerebro (meta + estacionalidad)."""
        if not self.brain or not st.get("x"):
            return None
        s = 1 if st["side"] == "long" else -1
        risk = st["risk"]
        real = C.TP1_FRAC * C.TP1_R if st.get("tp1_hit") else 0.0
        rem = (1 - C.TP1_FRAC) if st.get("tp1_hit") else 1.0
        R = real + rem * s * (exit_price - st["entry"]) / risk
        Rn = R - C.COST_PCT / (risk / st["entry"] * 100)
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
              "tp1_done": False, "tp1_hit": False, "trail": False, "tp_final": None,
              "x": sig["x"], "hour": sig["hour"], "wday": sig["wday"]}
        try:
            q1 = self._amt(sym, qty * C.TP1_FRAC)
            q2 = self._amt(sym, qty - q1)
            if C.PARTIAL_MODE and q1 >= min_amt and q2 >= min_amt:
                tp1 = self._px(sym, fill + s * C.TP1_R * risk)
                st["tp1_id"] = self._tp(sym, side, q1, tp1)
                if C.TRAIL_ON:
                    st["trail"] = True
                    tp_txt = f"TP1 {tp1} ({q1}) · resto con trailing {C.TRAIL_ATR}×ATR"
                else:
                    tp2 = self._px(sym, fill + s * C.TP2_R * risk)
                    st["tp2_id"] = self._tp(sym, side, q2, tp2)
                    st["tp_final"] = tp2
                    tp_txt = f"TP1 {tp1} ({q1}) · TP2 {tp2} ({q2})"
            else:
                tp = self._px(sym, fill + s * C.TP_SINGLE_R * risk)
                st["tp2_id"] = self._tp(sym, side, qty, tp)
                st["tp_final"] = tp
                st["tp1_done"] = True
                tp_txt = f"TP {tp}"
        except Exception as e:
            self.notify(f"⚠️ {sym}: error colocando TP ({e}). El SL sí está activo; revisa a mano.")
            tp_txt = "TP: ERROR"

        self.state[sym] = st
        self._save()
        self.notify(
            f"{'🟢' if side == 'long' else '🔴'} <b>{side.upper()} {sym}</b> [{sig['code']}]\n"
            f"Entrada {fill} · cantidad {qty}\nSL {sl}\n{tp_txt}\n"
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
            rn = None
            if tp_hit and st.get("tp_final"):
                rn = self._record(st, st["tp_final"])
                msg = "🎯 Objetivo final alcanzado"
            elif sl_hit:
                rn = self._record(st, st["sl"])
                msg = "🛑 Stop alcanzado" + (" (BE/trailing)" if st.get("tp1_hit") else "")
            else:
                msg = "ℹ️ Posición cerrada"
            self.notify(f"{msg} · {sym} [{st['code']}]" + (f" · {rn:+.2f}R neto" if rn is not None else ""))
            self.state.pop(sym, None)
            self._save()
            return

        if st["tp1_id"] and not st["tp1_done"] and st["tp1_id"] not in open_ids and self._closed(sym, st["tp1_id"]):
            st["tp1_done"] = st["tp1_hit"] = True
            self._cancel(sym, st["sl_id"])
            try:
                st["sl_id"] = self._stop(sym, side, qty_now, self._px(sym, st["entry"]))
                st["sl"] = st["entry"]
                self.notify(f"✅ TP1 en {sym}. Stop movido a entrada ({st['entry']}).")
            except Exception as e:
                self.notify(f"🚨 {sym}: TP1 hecho pero fallo al mover SL a BE ({e}). Revisa a mano.")
            self._save()

        if time.time() - st["opened"] >= C.MAX_BARS * C.TF_SEC:
            for k in ("sl_id", "tp1_id", "tp2_id"):
                self._cancel(sym, st.get(k))
            try:
                px = float(self.ex.fetch_ticker(sym)["last"])
                self._flatten(sym, side)
                rn = self._record(st, px)
                self.notify(f"⏱ Cierre por tiempo ({C.MAX_BARS} velas) · {sym} [{st['code']}]"
                            + (f" · {rn:+.2f}R neto" if rn is not None else ""))
                self.state.pop(sym, None)
                self._save()
            except Exception as e:
                self.notify(f"🚨 {sym}: fallo cierre por tiempo ({e})")
