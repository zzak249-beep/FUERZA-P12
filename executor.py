#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ejecución real en BingX (futuros perpetuos) para el escáner Granville.

Qué hace
--------
Cuando bot.py envía una señal, este módulo (si LIVE_TRADING=1) abre la operación en BingX:
  1. entrada a mercado con tamaño calculado por riesgo (RISK_PCT) y limitado por MAX_LEV
  2. SL (STOP_MARKET) y TP1 parcial + TP2 (TAKE_PROFIT_MARKET) colocados EN EL EXCHANGE
  3. tras TP1: mueve el SL a la entrada (break-even) y sigue con trailing de TRAIL_MULT R
  4. cierra a mercado por time-stop o por señal contraria
  5. vigila cada LIVE_POLL_SEC segundos: auto-repara SL/TP cancelados, detecta cierres externos,
     calcula el resultado real en R con los precios de ejecución y las comisiones

Seguridad
---------
* Por defecto BINGX_ENV=demo (dinero virtual VST). Dinero real SOLO con BINGX_ENV=live y LIVE_TRADING=1.
* Una sola posición por símbolo, y nunca toca posiciones que no haya abierto el bot.
* Si no consigue colocar el SL, cierra la posición de inmediato.
* Límite de pérdida diaria (DAILY_LOSS_LIMIT_R), máximo de operaciones reales abiertas (MAX_LIVE_TRADES),
  filtro de deslizamiento de entrada (MAX_ENTRY_DRIFT_R), pausa/cierre de emergencia por Telegram.
* Las órdenes de entrada NUNCA se reintentan a ciegas (se comprueba la posición antes de dar nada por hecho).

No hay garantías: es software sin auditar que opera con apalancamiento. Pruébalo en demo.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import requests

log = logging.getLogger("granville.live")

BASE_LIVE = "https://open-api.bingx.com"
BASE_DEMO = "https://open-api-vst.bingx.com"  # entorno de pruebas de BingX (dinero virtual VST)
TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}
DEAD_STATUSES = {"CANCELED", "CANCELLED", "FAILED", "EXPIRED", "REJECTED"}


# ============================================================
# UTILIDADES NUMÉRICAS
# ============================================================
def _quant(decimals: int) -> Decimal:
    return Decimal(1).scaleb(-int(decimals))


def floor_to(x: float, decimals: int) -> float:
    return float(Decimal(str(x)).quantize(_quant(decimals), rounding=ROUND_DOWN))


def round_to(x: float, decimals: int) -> float:
    return float(Decimal(str(x)).quantize(_quant(decimals), rounding=ROUND_HALF_UP))


def fq(x: float, decimals: int) -> str:
    """Cantidad -> texto (siempre redondeada hacia abajo, sin notación científica)."""
    return f"{Decimal(str(x)).quantize(_quant(decimals), rounding=ROUND_DOWN):f}"


def fp(x: float, decimals: int) -> str:
    """Precio -> texto (redondeo al tick, sin notación científica)."""
    return f"{Decimal(str(x)).quantize(_quant(decimals), rounding=ROUND_HALF_UP):f}"


def _f(d, *keys, default: float = 0.0) -> float:
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def _clean(text: str) -> str:
    return re.sub(r"signature=[0-9a-fA-F]+", "signature=***", str(text))


# ============================================================
# CLIENTE BINGX (PETICIONES FIRMADAS)
# ============================================================
class ApiError(RuntimeError):
    """BingX respondió con un código de error (la orden NO se ejecutó)."""

    def __init__(self, code, msg, path=""):
        super().__init__(f"BingX {path} code={code} msg={msg}")
        self.code, self.msg, self.path = code, msg, path


class NetError(RuntimeError):
    """Fallo de red/HTTP: el resultado de la petición es AMBIGUO (puede haberse ejecutado)."""


class BingXTrade:
    def __init__(self, key: str, secret: str, base: str, rps: float = 5.0, timeout: float = 15.0, recv: int = 10_000):
        self.key, self.secret, self.base = key, secret.encode("utf-8"), base.rstrip("/")
        self.timeout, self.recv = timeout, recv
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "granville-live/1.0"})
        self.min_interval = 1.0 / max(rps, 0.5)
        self.next_t = 0.0
        self.lock = threading.Lock()

    # ---- infraestructura ----
    def _wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next_t)
            self.next_t = t + self.min_interval
        if t > now:
            time.sleep(t - now)

    @staticmethod
    def _s(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    def _req(self, method: str, path: str, params: Optional[dict] = None, signed: bool = True, retry: bool = False,
             base: Optional[str] = None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempts = 3 if retry else 1
        last: Optional[Exception] = None
        for i in range(attempts):
            self._wait()
            p = dict(params)
            headers = {}
            if signed:
                p["timestamp"] = int(time.time() * 1000)
                p["recvWindow"] = self.recv
                qs = "&".join(f"{k}={self._s(v)}" for k, v in sorted(p.items()))
                sig = hmac.new(self.secret, qs.encode("utf-8"), hashlib.sha256).hexdigest()
                url = f"{base or self.base}{path}?{qs}&signature={sig}"
                headers["X-BX-APIKEY"] = self.key
            else:
                qs = "&".join(f"{k}={self._s(v)}" for k, v in sorted(p.items()))
                url = f"{base or self.base}{path}" + (f"?{qs}" if qs else "")
            try:
                r = self.session.request(method, url, headers=headers, timeout=self.timeout)
            except requests.RequestException as ex:
                last = NetError(f"{path}: {_clean(ex)}")
                time.sleep(0.5 * (i + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = NetError(f"{path}: HTTP {r.status_code}")
                time.sleep(1.5 * (i + 1))
                continue
            try:
                j = r.json()
            except ValueError:
                last = NetError(f"{path}: respuesta no válida (HTTP {r.status_code})")
                time.sleep(0.5 * (i + 1))
                continue
            code = j.get("code", 0)
            if code != 0:
                raise ApiError(code, j.get("msg"), path)
            return j.get("data")
        raise last or NetError(path)

    # ---- datos públicos (siempre del host real: es información de mercado) ----
    def public_price(self, symbol: str) -> float:
        for path in ("/openApi/swap/v2/quote/price", "/openApi/swap/v2/quote/ticker"):
            try:
                d = self._req("GET", path, {"symbol": symbol}, signed=False, retry=True, base=BASE_LIVE)
            except (ApiError, NetError):
                continue
            if isinstance(d, list) and d:
                d = d[0]
            px = _f(d, "price", "lastPrice")
            if px > 0:
                return px
        raise NetError(f"sin precio para {symbol}")

    # ---- cuenta ----
    def balance(self) -> dict:
        d = self._req("GET", "/openApi/swap/v2/user/balance", retry=True)
        b = d.get("balance") if isinstance(d, dict) else d
        if isinstance(b, list):
            b = next((x for x in b if str(x.get("asset", "")).upper() in ("USDT", "VST")), b[0] if b else {})
        return b or {}

    def dual_side(self) -> bool:
        d = self._req("GET", "/openApi/swap/v1/positionSide/dual", retry=True)
        v = d.get("dualSidePosition") if isinstance(d, dict) else d
        return str(v).lower() == "true"

    def positions(self, symbol: Optional[str] = None) -> list:
        d = self._req("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol}, retry=True)
        if isinstance(d, dict):
            d = d.get("positions", [])
        return d or []

    def set_margin_type(self, symbol: str, mtype: str = "ISOLATED") -> None:
        self._req("POST", "/openApi/swap/v2/trade/marginType", {"symbol": symbol, "marginType": mtype})

    def set_leverage(self, symbol: str, side: str, leverage: int) -> None:
        self._req("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "side": side, "leverage": int(leverage)})

    # ---- órdenes ----
    def place(self, params: dict) -> dict:
        d = self._req("POST", "/openApi/swap/v2/trade/order", params, retry=False)
        if isinstance(d, dict):
            return d.get("order", d) or {}
        return {}

    def query_order(self, symbol: str, order_id) -> dict:
        d = self._req("GET", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": order_id}, retry=True)
        if isinstance(d, dict):
            return d.get("order", d) or {}
        return {}

    def cancel_order(self, symbol: str, order_id) -> None:
        self._req("DELETE", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": order_id}, retry=True)


# ============================================================
# GESTOR DE OPERACIONES REALES
# ============================================================
class LiveTrader:
    def __init__(self, cfg, L: SimpleNamespace, notifier):
        self.cfg, self.L, self.notifier = cfg, L, notifier
        self.api = BingXTrade(L.api_key, L.api_secret, BASE_LIVE if L.env == "live" else BASE_DEMO, rps=L.rps)
        self.tag = "💰 REAL" if L.env == "live" else "🧪 DEMO"
        self.lock = threading.RLock()
        self.specs: dict[str, dict] = {}
        self.open: list[dict] = []
        self.closed: list[dict] = []
        self.paused = False
        self.hedge: Optional[bool] = None
        self._setup_done: set = set()
        self._open_syms: set = set()
        self._blocked_day: Optional[str] = None
        self._fail_n = 0
        self._last_fail_alert = 0.0
        self.path = Path(L.file)
        self._load()

    # ---------- persistencia ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.open, self.closed = d.get("open", []), d.get("closed", [])
            self.paused = bool(d.get("paused", False))
            self._sync()
        except Exception as ex:  # noqa: BLE001
            log.error("No se pudo leer %s (%s). REVISA tus posiciones en BingX a mano.", self.path, ex)

    def _save(self) -> None:
        self.closed = self.closed[-2000:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({"open": self.open, "closed": self.closed, "paused": self.paused}), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as ex:  # noqa: BLE001
            log.error("No se pudo guardar %s: %s", self.path, ex)

    def save(self) -> None:
        with self.lock:
            self._save()

    def _sync(self) -> None:
        self._open_syms = {t["symbol"] for t in self.open}

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self._open_syms

    def notify(self, text: str) -> None:
        try:
            self.notifier.send(text)
        except Exception as ex:  # noqa: BLE001
            log.error("No se pudo notificar: %s", ex)

    # ---------- contratos ----------
    def set_contracts(self, contracts: list[dict]) -> None:
        specs = {}
        for c in contracts:
            sym = c.get("symbol")
            if not sym:
                continue
            try:
                qp = int(c["quantityPrecision"]) if c.get("quantityPrecision") not in (None, "") else None
            except (TypeError, ValueError):
                qp = None
            try:
                pp = int(c.get("pricePrecision", 6))
            except (TypeError, ValueError):
                pp = 6
            specs[sym] = {
                "pp": pp, "qp": qp,
                "min_qty": _f(c, "tradeMinQuantity"), "min_usdt": _f(c, "tradeMinUSDT"),
                "api_open": str(c.get("apiStateOpen", "true")).lower() != "false",
                "max_lev": _f(c, "maxLongLeverage", "maxShortLeverage"),
            }
        self.specs = specs

    # ---------- modo de posición ----------
    def _detect_mode(self) -> bool:
        want = self.L.position_mode
        try:
            detected = self.api.dual_side()
        except (ApiError, NetError) as ex:
            if want in ("hedge", "oneway"):
                log.warning("No pude detectar el modo de posición (%s); uso POSITION_MODE=%s", ex, want)
                return want == "hedge"
            raise RuntimeError(f"No pude detectar el modo de posición ({ex}). Define POSITION_MODE=hedge o oneway.")
        if want in ("hedge", "oneway") and (want == "hedge") != detected:
            log.warning("POSITION_MODE=%s no coincide con tu cuenta (%s): uso el de la cuenta",
                        want, "hedge" if detected else "oneway")
        return detected

    # ---------- posiciones ----------
    def _pos_qty_from(self, rows: list, sym: str, side: str) -> float:
        tot = 0.0
        for p in rows or []:
            if p.get("symbol") != sym:
                continue
            amt = _f(p, "positionAmt", "availableAmt")
            ps = str(p.get("positionSide", "")).upper()
            if ps in ("LONG", "SHORT"):
                if ps == side:
                    tot += abs(amt)
            elif amt != 0 and (amt > 0) == (side == "LONG"):  # one-way con positionSide BOTH: el signo manda
                tot += abs(amt)
        return tot

    def _pos_qty(self, sym: str, side: str) -> float:
        return self._pos_qty_from(self.api.positions(sym), sym, side)

    @staticmethod
    def _any_qty(rows: list, sym: str) -> float:
        return sum(abs(_f(p, "positionAmt", "availableAmt")) for p in rows or [] if p.get("symbol") == sym)

    # ---------- construcción de órdenes ----------
    def _order_params(self, sym: str, side: str, closing: bool, typ: str, qty: float, qp: int, pp: int,
                      stop: Optional[float] = None) -> dict:
        long = side == "LONG"
        order_side = ("SELL" if long else "BUY") if closing else ("BUY" if long else "SELL")
        p = {"symbol": sym, "side": order_side, "type": typ, "quantity": fq(qty, qp)}
        if self.hedge:
            p["positionSide"] = side
        else:
            p["positionSide"] = "BOTH"
            if closing:
                p["reduceOnly"] = "true"
        if stop is not None:
            p["stopPrice"] = fp(stop, pp)
            p["workingType"] = self.L.trigger
        return p

    def _place_exit(self, tr: dict, typ: str, price: float, qty: float) -> dict:
        params = self._order_params(tr["symbol"], tr["side"], True, typ, qty, tr["qp"], tr["pp"], stop=price)
        last: Optional[Exception] = None
        for _ in range(2):
            try:
                o = self.api.place(params)
                oid = o.get("orderId")
                if oid in (None, ""):
                    raise ApiError(-1, "BingX no devolvió orderId", "trade/order")
                return {"id": str(oid), "px": price, "qty": qty, "done": False}
            except NetError as ex:  # ambiguo: reintento una vez (un duplicado de cierre es inofensivo)
                last = ex
                time.sleep(1.0)
        raise last  # type: ignore[misc]

    def _status(self, sym: str, oid) -> str:
        try:
            d = self.api.query_order(sym, oid)
        except (ApiError, NetError) as ex:
            log.debug("query_order %s %s: %s", sym, oid, ex)
            return "UNKNOWN"
        s = str(d.get("status", "")).upper()
        if s == "FILLED":
            return "FILLED"
        if s in DEAD_STATUSES:
            return "DEAD"
        return "ACTIVE"

    def _record_leg(self, tr: dict, name: str, oid, fb_px: float, fb_qty: float) -> dict:
        px, qty, fee = fb_px, fb_qty, None
        try:
            d = self.api.query_order(tr["symbol"], oid)
            ap, eq, cm = _f(d, "avgPrice"), _f(d, "executedQty"), _f(d, "commission")
            if ap > 0:
                px = ap
            if eq > 0:
                qty = eq
            if cm != 0:
                fee = abs(cm)
        except (ApiError, NetError) as ex:
            log.debug("No pude leer el fill de %s: %s", name, ex)
        if fee is None:
            fee = qty * px * self.cfg.comm_pct / 100.0
        leg = {"name": name, "px": px, "qty": qty, "fee": fee}
        tr["legs"].append(leg)
        return leg

    def _fill_info(self, sym: str, oid, side: str, fb_px: float) -> tuple[float, float, float]:
        """(precio medio, cantidad, comisión) de la entrada."""
        if oid not in (None, ""):
            for _ in range(5):
                try:
                    d = self.api.query_order(sym, oid)
                    ap, eq = _f(d, "avgPrice"), _f(d, "executedQty")
                    if str(d.get("status", "")).upper() == "FILLED" and ap > 0 and eq > 0:
                        fee = abs(_f(d, "commission")) or eq * ap * self.cfg.comm_pct / 100.0
                        return ap, eq, fee
                except (ApiError, NetError):
                    pass
                time.sleep(0.6)
        try:  # plan B: la posición
            for p in self.api.positions(sym):
                if p.get("symbol") == sym and self._pos_qty_from([p], sym, side) > 0:
                    ap, q = _f(p, "avgPrice"), self._pos_qty_from([p], sym, side)
                    if ap > 0:
                        return ap, q, q * ap * self.cfg.comm_pct / 100.0
        except (ApiError, NetError):
            pass
        return fb_px, 0.0, 0.0

    # ---------- límites ----------
    @staticmethod
    def _day_start_ms() -> int:
        now = datetime.now(timezone.utc)
        return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    def day_r(self) -> float:
        d0 = self._day_start_ms()
        return sum(float(t.get("R", 0.0)) for t in self.closed if t.get("exit_ms", 0) >= d0)

    def _daily_blocked(self) -> bool:
        lim = self.L.daily_loss_r
        return lim > 0 and self.day_r() <= -lim

    # ============================================================
    # ARRANQUE / COMPROBACIONES
    # ============================================================
    def startup(self) -> str:
        with self.lock:
            bal = self.api.balance()  # lanza error si las claves no valen
            self.hedge = self._detect_mode()
            eq = _f(bal, "equity", "balance")
            self._reconcile()
            warn = ""
            if os.getenv("RAILWAY_ENVIRONMENT") and not str(self.path).startswith("/data"):
                warn = ("\n⚠️ Sin Volume en /data: si Railway reinicia, el bot pierde la memoria de sus operaciones "
                        "(los SL/TP seguirían en BingX pero sin trailing). Añade un Volume y define STATE_FILE=/data/state.json.")
            return (f"{self.tag} ejecución ACTIVA · saldo {eq:.2f} · modo {'hedge' if self.hedge else 'one-way'} · "
                    f"riesgo {self.cfg.risk_pct:g}%/op · máx. {self.L.max_live} abiertas · "
                    f"límite diario {self.L.daily_loss_r:g}R · {len(self.open)} operaciones reales abiertas"
                    + (" · ⏸ PAUSADO" if self.paused else "") + warn)

    def _reconcile(self) -> None:
        rows = self.api.positions()
        for tr in list(self.open):
            try:
                self._poll_trade(tr, rows)
            except (ApiError, NetError) as ex:
                log.warning("reconcile %s: %s", tr["symbol"], ex)
        mine = {t["symbol"] for t in self.open}
        orphans = sorted({p.get("symbol") for p in rows if abs(_f(p, "positionAmt", "availableAmt")) > 0
                          and p.get("symbol") not in mine})
        if orphans:
            self.notify(f"{self.tag} ℹ️ Hay posiciones en BingX que el bot NO gestiona (no las toca): "
                        f"{html.escape(', '.join(orphans))}")
        self._sync()
        self._save()

    def check(self) -> None:
        """--live-check: comprueba claves y estado de la cuenta sin operar."""
        print(f"Entorno: {self.L.env.upper()} · base {self.api.base}")
        bal = self.api.balance()
        print(f"Claves OK · saldo: balance={_f(bal, 'balance'):.4f} equity={_f(bal, 'equity'):.4f} "
              f"disponible={_f(bal, 'availableMargin'):.4f} ({bal.get('asset', '?')})")
        self.hedge = self._detect_mode()
        print(f"Modo de posición: {'HEDGE (long y short separados)' if self.hedge else 'ONE-WAY'}")
        rows = self.api.positions()
        act = [p for p in rows if abs(_f(p, 'positionAmt', 'availableAmt')) > 0]
        print(f"Posiciones abiertas en la cuenta: {len(act)}")
        for p in act:
            print(f"  {p.get('symbol')} {p.get('positionSide')} cant={p.get('positionAmt')} ent={p.get('avgPrice')}")
        sp = self.specs.get("BTC-USDT")
        print(f"Contrato BTC-USDT: {sp}")
        print(f"Operaciones reales registradas por el bot: {len(self.open)} abiertas · {len(self.closed)} cerradas")

    def selftest(self, symbol: str, force_live: bool = False) -> None:
        """--live-selftest: abre una posición MÍNIMA, coloca SL y TP lejanos, los consulta, los cancela y cierra.
        Sirve para comprobar de extremo a extremo que BingX acepta cada orden. Solo en demo salvo --force-live."""
        if self.L.env == "live" and not force_live:
            raise SystemExit("Por seguridad el autotest solo corre en BINGX_ENV=demo. "
                             "(Para forzarlo en real, con cantidad mínima: --force-live)")
        spec = self.specs.get(symbol)
        if not spec or spec["qp"] is None:
            raise SystemExit(f"No hay datos del contrato {symbol}")
        self.hedge = self._detect_mode()
        px = self.api.public_price(symbol)
        qp, pp = spec["qp"], spec["pp"]
        qty = max(spec["min_qty"], 10 ** (-qp))
        while qty * px < max(spec["min_usdt"], 0.0) * 1.2:
            qty = floor_to(qty * 1.5 + 10 ** (-qp), qp)
        qty = floor_to(qty, qp) or 10 ** (-qp)
        print(f"Autotest {symbol} · precio {px} · cantidad {qty} (~{qty * px:.2f} USDT) · {self.L.env.upper()}")
        ok = True

        def step(name, fn):
            nonlocal ok
            try:
                r = fn()
                print(f"  ✓ {name}")
                return r
            except Exception as ex:  # noqa: BLE001
                ok = False
                print(f"  ✗ {name}: {_clean(ex)}")
                return None

        step("margen aislado", lambda: self.api.set_margin_type(symbol, "ISOLATED"))
        step("apalancamiento", lambda: self.api.set_leverage(symbol, "LONG" if self.hedge else "BOTH", 2))
        o = step("entrada a mercado", lambda: self.api.place(self._order_params(symbol, "LONG", False, "MARKET", qty, qp, pp)))
        if o is None:
            raise SystemExit("La entrada falló: no sigo.")
        time.sleep(1.5)
        fpx, fqty, _ = self._fill_info(symbol, o.get("orderId"), "LONG", px)
        print(f"  · ejecutada a {fpx} cant {fqty}")
        sl = step("SL (STOP_MARKET)", lambda: self.api.place(
            self._order_params(symbol, "LONG", True, "STOP_MARKET", qty, qp, pp, stop=round_to(px * 0.85, pp))))
        tp = step("TP (TAKE_PROFIT_MARKET)", lambda: self.api.place(
            self._order_params(symbol, "LONG", True, "TAKE_PROFIT_MARKET", qty, qp, pp, stop=round_to(px * 1.15, pp))))
        for name, od in (("SL", sl), ("TP", tp)):
            if od:
                st = step(f"consultar {name}", lambda od=od: self.api.query_order(symbol, od.get("orderId")))
                if st is not None:
                    print(f"    estado: {st.get('status')}")
                step(f"cancelar {name}", lambda od=od: self.api.cancel_order(symbol, od.get("orderId")))
        step("cierre a mercado", lambda: self.api.place(self._order_params(symbol, "LONG", True, "MARKET", qty, qp, pp)))
        time.sleep(1.5)
        left = self._pos_qty(symbol, "LONG")
        print(f"  · posición restante: {left}")
        if left > 0:
            ok = False
            print("  ✗ ¡Queda posición abierta! Ciérrala a mano en BingX.")
        print("AUTOTEST " + ("CORRECTO ✅" if ok else "CON FALLOS ❌ (revisa los ✗ antes de operar)"))

    # ============================================================
    # APERTURA
    # ============================================================
    def _skip(self, s, msg: str, notify: bool = True) -> None:
        log.info("[live] %s %s %s no ejecutada: %s", s.symbol, s.tf, s.side, msg)
        if notify:
            self.notify(f"{self.tag} ⏭ {s.side} {html.escape(s.symbol)} {s.tf} no ejecutada: {html.escape(msg)}")

    def try_open(self, s) -> None:
        with self.lock:
            try:
                self._open(s)
            except Exception as ex:  # noqa: BLE001
                log.exception("[live] error abriendo %s", s.symbol)
                self.notify(f"{self.tag} ⚠️ Error al abrir {s.side} {html.escape(s.symbol)}: {html.escape(_clean(ex))[:300]}")

    def _open(self, s) -> None:
        cfg, L = self.cfg, self.L
        sym, long = s.symbol, s.side == "LONG"
        if self.hedge is None:
            return self._skip(s, "ejecución no inicializada", False)
        if self.paused:
            return self._skip(s, "bot en pausa (/reanudar)", False)
        if self._daily_blocked():
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._blocked_day != day:
                self._blocked_day = day
                self.notify(f"{self.tag} 🧯 Límite de pérdida diaria alcanzado ({self.day_r():+.2f}R ≤ -{L.daily_loss_r:g}R): "
                            "no abro más operaciones hasta mañana (UTC). Las abiertas se siguen gestionando.")
            return self._skip(s, "límite diario", False)
        if sym in self._open_syms:
            return self._skip(s, "ya hay una operación real en ese símbolo", False)
        if len(self.open) >= L.max_live:
            return self._skip(s, f"máximo de {L.max_live} operaciones reales abiertas", False)
        spec = self.specs.get(sym)
        if not spec or spec["qp"] is None:
            return self._skip(s, "no tengo la especificación del contrato")
        if not spec["api_open"]:
            return self._skip(s, "BingX no permite abrir por API en este contrato")
        pp, qp = spec["pp"], spec["qp"]

        # precio actual y deslizamiento respecto a la señal
        px = self.api.public_price(sym)
        drift = abs(px - s.price) / s.stop_dist if s.stop_dist > 0 else 99
        if drift > L.max_drift_r:
            return self._skip(s, f"el precio ya se movió {drift:.2f}R desde la señal (máx. {L.max_drift_r:g}R)")

        # saldo y posiciones existentes
        bal = self.api.balance()
        eq_real = _f(bal, "equity", "balance")
        avail = _f(bal, "availableMargin", "available")
        equity = min(cfg.account, eq_real) if eq_real > 0 else cfg.account
        if self._any_qty(self.api.positions(sym), sym) > 0:
            return self._skip(s, "ya existe una posición tuya en BingX en este símbolo (no la toco)")

        # tamaño por riesgo
        risk_mult = cfg.long_risk_mult if long else cfg.short_risk_mult
        risk_cash = equity * cfg.risk_pct / 100 * risk_mult
        qty = risk_cash / s.stop_dist
        if qty * px > equity * cfg.max_lev:
            qty = equity * cfg.max_lev / px
        qty = floor_to(qty, qp)
        notional = qty * px
        min_q = max(spec["min_qty"], 10 ** (-qp))
        if qty < min_q or notional < spec["min_usdt"]:
            return self._skip(s, f"tamaño calculado {qty:g} (≈{notional:.2f} USDT) por debajo del mínimo del contrato "
                                 f"({min_q:g} / {spec['min_usdt']:g} USDT). Sube ACCOUNT_USDT o RISK_PCT")
        lev = int(max(L.leverage, math.ceil(cfg.max_lev)))
        if spec["max_lev"] > 0:
            lev = int(min(lev, spec["max_lev"]))
        if avail > 0 and notional / lev > avail * 0.9:
            return self._skip(s, f"margen insuficiente (necesito ≈{notional / lev:.2f}, disponible {avail:.2f})")

        self._setup_symbol(sym, s.side, lev)

        # ---- entrada a mercado (sin reintentos ciegos) ----
        params = self._order_params(sym, s.side, False, "MARKET", qty, qp, pp)
        try:
            o = self.api.place(params)
        except NetError:
            time.sleep(1.5)
            if self._pos_qty(sym, s.side) <= 0:
                return self._skip(s, "la orden de entrada no se pudo confirmar (error de red); no se abre")
            o = {}
        oid = o.get("orderId")
        fpx, fqty, fee = self._fill_info(sym, oid, s.side, px)
        if fqty <= 0:
            fqty = qty
            fee = fqty * fpx * cfg.comm_pct / 100.0
        fqty = floor_to(fqty, qp)

        sgn = 1 if long else -1
        tp2_r = cfg.long_tp2_r if long else cfg.short_tp2_r
        qty_tp1 = floor_to(fqty * cfg.partial_pct / 100.0, qp)
        qty_rest = floor_to(fqty - qty_tp1, qp)
        has_partial = 0 < cfg.partial_pct < 100 and qty_tp1 >= min_q and qty_rest >= min_q
        if not has_partial:
            qty_tp1, qty_rest = 0.0, fqty
        tr = {
            "id": f"{sym}|{s.tf}|{s.candle_open_ms}|{s.side}", "symbol": sym, "tf": s.tf, "side": s.side,
            "kinds": list(s.kinds), "sig_ms": s.candle_open_ms, "sig_px": s.price, "opened_ms": int(time.time() * 1000),
            "entry": fpx, "entry_fee": fee, "risk": s.stop_dist, "qty": fqty, "qty_tp1": qty_tp1, "qty_rest": qty_rest,
            "has_partial": has_partial, "pp": pp, "qp": qp, "lev": lev, "risk_cash": risk_cash,
            "sl": round_to(fpx - sgn * s.stop_dist, pp), "tp1": round_to(fpx + sgn * s.stop_dist * cfg.tp1_r, pp),
            "tp2": round_to(fpx + sgn * s.stop_dist * tp2_r, pp), "tp2_r": tp2_r,
            "stage": 1, "tp1_done": False, "be_done": False, "bars": 0, "last_ms": s.candle_open_ms, "ext": fpx,
            "orders": {"sl": None, "tp1": None, "tp2": None}, "legs": [], "closed": False, "fail": 0,
        }
        self.open.append(tr)
        self._sync()
        self._save()

        if not self._ensure_orders(tr):
            self.notify(f"{self.tag} 🚨 No pude colocar el STOP LOSS de {html.escape(sym)}: cierro la posición ahora.")
            self._market_close(tr, "EMERGENCY")
            return
        self._save()
        self.notify(self._entry_msg(tr, notional))

    def _setup_symbol(self, sym: str, side: str, lev: int) -> None:
        key = (sym, side if self.hedge else "BOTH", lev)
        if key in self._setup_done:
            return
        try:
            self.api.set_margin_type(sym, "ISOLATED")
        except (ApiError, NetError) as ex:
            log.info("margin ISOLATED %s: %s (puede que ya lo estuviera)", sym, ex)
        try:
            self.api.set_leverage(sym, side if self.hedge else "BOTH", lev)
        except (ApiError, NetError) as ex:
            log.warning("leverage %s: %s", sym, ex)
            self.notify(f"{self.tag} ⚠️ No pude fijar el apalancamiento {lev}x en {html.escape(sym)} ({html.escape(_clean(ex))[:150]}). "
                        "Se usa el que tengas configurado en BingX.")
        self._setup_done.add(key)

    # ============================================================
    # ÓRDENES PROTECTORAS
    # ============================================================
    def _remaining(self, tr: dict) -> float:
        done = sum(l["qty"] for l in tr["legs"] if l["name"] == "TP1")
        return max(0.0, floor_to(tr["qty"] - done, tr["qp"]))

    def _ensure_orders(self, tr: dict) -> bool:
        """Coloca las órdenes que falten. Devuelve True si el SL está colocado."""
        o, rem = tr["orders"], self._remaining(tr)
        if rem <= 0:
            return True
        if o["sl"] is None:
            try:
                o["sl"] = self._place_exit(tr, "STOP_MARKET", tr["sl"], rem)
            except (ApiError, NetError) as ex:
                log.error("SL %s: %s", tr["symbol"], ex)
        if o["sl"] is None:
            return False
        try:
            if not tr["tp1_done"]:
                if tr["has_partial"] and o["tp1"] is None:
                    o["tp1"] = self._place_exit(tr, "TAKE_PROFIT_MARKET", tr["tp1"], tr["qty_tp1"])
                if o["tp2"] is None:
                    q2 = tr["qty_rest"] if tr["has_partial"] else tr["qty"]
                    o["tp2"] = self._place_exit(tr, "TAKE_PROFIT_MARKET", tr["tp2"], q2)
            elif o["tp2"] is None:
                o["tp2"] = self._place_exit(tr, "TAKE_PROFIT_MARKET", tr["tp2"], rem)
        except (ApiError, NetError) as ex:
            tr["fail"] = tr.get("fail", 0) + 1
            log.warning("TP %s: %s (intento %d)", tr["symbol"], ex, tr["fail"])
            if tr["fail"] == 3:
                self.notify(f"{self.tag} ⚠️ No consigo colocar un TP de {html.escape(tr['symbol'])} "
                            f"({html.escape(_clean(ex))[:150]}). El SL sí está activo; reintento en cada ciclo.")
        else:
            tr["fail"] = 0
        return True

    def _move_sl(self, tr: dict, new_px: float, reason: str) -> bool:
        """Coloca el nuevo SL y DESPUÉS cancela el viejo (nunca queda sin protección)."""
        sym, old = tr["symbol"], tr["orders"]["sl"]
        rem = self._remaining(tr)
        if rem <= 0:
            return False
        try:
            new = self._place_exit(tr, "STOP_MARKET", new_px, rem)
        except (ApiError, NetError) as ex:
            px = None
            try:
                px = self.api.public_price(sym)
            except (ApiError, NetError):
                pass
            crossed = px is not None and ((tr["side"] == "LONG" and px <= new_px) or (tr["side"] == "SHORT" and px >= new_px))
            if crossed:
                self.notify(f"{self.tag} ℹ️ {html.escape(sym)}: el precio ya cruzó el nuevo stop ({new_px}); cierro a mercado.")
                self._market_close(tr, reason)
            else:
                log.warning("No pude mover el SL de %s (%s); mantengo el anterior", sym, ex)
            return False
        tr["orders"]["sl"] = new
        tr["sl"] = new_px
        if old:
            try:
                self.api.cancel_order(sym, old["id"])
            except (ApiError, NetError):
                if self._status(sym, old["id"]) == "FILLED":  # el SL viejo saltó justo ahora
                    old["done"] = True
                    self._record_leg(tr, "SL", old["id"], old["px"], self._remaining(tr))
                    self._finalize(tr, "SL" if not tr["tp1_done"] else "TRAIL")
                    return False
        return True

    # ============================================================
    # SEGUIMIENTO (cada vela cerrada y cada LIVE_POLL_SEC)
    # ============================================================
    def on_candles(self, symbol: str, tf: str, df, sigs: list) -> None:
        with self.lock:
            tr = next((t for t in self.open if t["symbol"] == symbol and t["tf"] == tf), None)
            if tr is None or df is None or df.empty:
                return
            try:
                self._on_candles(tr, df, sigs)
            except (ApiError, NetError) as ex:
                log.warning("[live] %s on_candles: %s", symbol, ex)
            except Exception:  # noqa: BLE001
                log.exception("[live] %s on_candles", symbol)
            self._save()

    def _on_candles(self, tr: dict, df, sigs: list) -> None:
        cfg = self.cfg
        s = 1 if tr["side"] == "LONG" else -1
        new = df[df["time"] > tr["last_ms"]]
        if new.empty:
            return
        for row in new.itertuples(index=False):
            tr["bars"] += 1
            if tr["tp1_done"]:
                tr["ext"] = max(tr["ext"], float(row.high)) if s > 0 else min(tr["ext"], float(row.low))
            tr["last_ms"] = int(row.time)
        if tr["tp1_done"] and tr["be_done"]:
            self._trail(tr)
            if tr["closed"]:
                return
        if cfg.exit_on_opp and any(x.side != tr["side"] and x.candle_open_ms == tr["last_ms"] for x in sigs):
            self._market_close(tr, "SIGNAL")
            return
        if cfg.use_time_stop and tr["bars"] >= cfg.max_bars:
            self._market_close(tr, "TIME")

    def _trail(self, tr: dict) -> None:
        s = 1 if tr["side"] == "LONG" else -1
        tick = 10 ** (-tr["pp"])
        desired = tr["ext"] - s * tr["risk"] * self.cfg.trail_mult
        new = max(tr["entry"], desired) if s > 0 else min(tr["entry"], desired)
        new = round_to(new, tr["pp"])
        better = new > tr["sl"] + tick * 0.5 if s > 0 else new < tr["sl"] - tick * 0.5
        if better:
            self._move_sl(tr, new, "TRAIL")

    def poll(self) -> None:
        with self.lock:
            if not self.open:
                return
            try:
                rows = self.api.positions()
            except (ApiError, NetError) as ex:
                self._api_fail(ex)
                return
            self._fail_n = 0
            for tr in list(self.open):
                try:
                    self._poll_trade(tr, rows)
                except (ApiError, NetError) as ex:
                    log.warning("[live] poll %s: %s", tr["symbol"], ex)
                except Exception:  # noqa: BLE001
                    log.exception("[live] poll %s", tr["symbol"])
            self._sync()
            self._save()

    def start_poller(self, stop: threading.Event) -> None:
        def loop():
            while not stop.is_set():
                try:
                    self.poll()
                except Exception:  # noqa: BLE001
                    log.exception("[live] poller")
                stop.wait(self.L.poll_sec)

        threading.Thread(target=loop, daemon=True, name="live-poller").start()

    def _api_fail(self, ex) -> None:
        self._fail_n += 1
        log.warning("[live] BingX no responde (%d): %s", self._fail_n, _clean(ex))
        if self._fail_n >= 6 and time.time() - self._last_fail_alert > 3600:
            self._last_fail_alert = time.time()
            self.notify(f"{self.tag} ⚠️ No puedo comunicar con BingX ({html.escape(_clean(ex))[:120]}). "
                        "Tus SL/TP siguen colocados en el exchange.")

    def _poll_trade(self, tr: dict, rows: list) -> None:
        sym, o, cfg = tr["symbol"], tr["orders"], self.cfg
        tick = 10 ** (-tr["pp"])
        pos_qty = self._pos_qty_from(rows, sym, tr["side"])
        st = {n: self._status(sym, o[n]["id"]) for n in ("sl", "tp1", "tp2") if o.get(n) and not o[n].get("done")}

        # a) TP1 ejecutado -> stage 2 y SL a break-even
        if st.get("tp1") == "FILLED" and not tr["tp1_done"]:
            o["tp1"]["done"] = True
            self._record_leg(tr, "TP1", o["tp1"]["id"], tr["tp1"], tr["qty_tp1"])
            tr["tp1_done"], tr["stage"] = True, 2
            try:
                last = self.api.public_price(sym)
            except (ApiError, NetError):
                last = tr["tp1"]
            tr["ext"] = max(tr["tp1"], last) if tr["side"] == "LONG" else min(tr["tp1"], last)
            self.notify(self._tp1_msg(tr))

        # b) salidas completas
        if st.get("tp2") == "FILLED":
            o["tp2"]["done"] = True
            self._record_leg(tr, "TP2", o["tp2"]["id"], tr["tp2"], self._remaining(tr))
            self._finalize(tr, "TP2")
            return
        if st.get("sl") == "FILLED":
            o["sl"]["done"] = True
            self._record_leg(tr, "SL", o["sl"]["id"], tr["sl"], self._remaining(tr))
            if not tr["tp1_done"]:
                reason = "SL"
            else:
                reason = "BE" if abs(tr["sl"] - round_to(tr["entry"], tr["pp"])) <= tick * 0.5 else "TRAIL"
            self._finalize(tr, reason)
            return

        # c) cerrada por fuera del bot (manual, liquidación...)
        if pos_qty <= 0 and int(time.time() * 1000) - tr["opened_ms"] > 30_000:
            try:
                last = self.api.public_price(sym)
            except (ApiError, NetError):
                last = tr["entry"]
            rem = self._remaining(tr)
            if rem > 0:
                tr["legs"].append({"name": "EXTERNAL", "px": last, "qty": rem, "fee": rem * last * cfg.comm_pct / 100.0})
            self._finalize(tr, "EXTERNAL")
            return

        # d) auto-reparación si alguien canceló una orden
        for n in ("sl", "tp1", "tp2"):
            if st.get(n) == "DEAD":
                self.notify(f"{self.tag} ⚠️ {html.escape(sym)}: la orden {n.upper()} fue cancelada; la vuelvo a colocar.")
                o[n] = None
        # e) mover a break-even si TP1 ya ocurrió y aún no se hizo
        if tr["tp1_done"] and not tr["be_done"]:
            if self._move_sl(tr, round_to(tr["entry"], tr["pp"]), "BE"):
                tr["be_done"] = True
            if tr["closed"]:
                return
        # f) completar lo que falte; sin SL no se queda ninguna posición
        if not self._ensure_orders(tr):
            self.notify(f"{self.tag} 🚨 {html.escape(sym)} sin STOP LOSS y no puedo recolocarlo: cierro a mercado.")
            self._market_close(tr, "EMERGENCY")
            return
        # g) time-stop por reloj (por si el símbolo ya no entra en el escaneo)
        if cfg.use_time_stop:
            elapsed = (int(time.time() * 1000) - (tr["sig_ms"] + TF_MS[tr["tf"]])) / TF_MS[tr["tf"]]
            if elapsed >= cfg.max_bars + 1:
                self._market_close(tr, "TIME")

    # ============================================================
    # CIERRES
    # ============================================================
    def _market_close(self, tr: dict, reason: str) -> bool:
        sym, side = tr["symbol"], tr["side"]
        try:
            pos = self._pos_qty(sym, side)
        except (ApiError, NetError) as ex:
            self.notify(f"{self.tag} 🚨 No pude leer la posición de {html.escape(sym)} para cerrarla: {html.escape(_clean(ex))[:150]}")
            return False
        rem = self._remaining(tr)
        if pos <= 0:
            if rem > 0:
                try:
                    last = self.api.public_price(sym)
                except (ApiError, NetError):
                    last = tr["entry"]
                tr["legs"].append({"name": "EXTERNAL", "px": last, "qty": rem, "fee": 0.0})
            self._finalize(tr, reason if reason in ("SL", "TP2") else "EXTERNAL")
            return True
        qty = floor_to(min(pos, rem) if rem > 0 else pos, tr["qp"])
        if qty <= 0:
            qty = floor_to(pos, tr["qp"])
        params = self._order_params(sym, side, True, "MARKET", qty, tr["qp"], tr["pp"])
        try:
            o = self.api.place(params)
        except NetError:
            time.sleep(1.5)
            if self._pos_qty(sym, side) > 0:
                try:
                    o = self.api.place(params)
                except (ApiError, NetError) as ex:
                    self.notify(f"{self.tag} 🚨 NO pude cerrar {html.escape(sym)}: {html.escape(_clean(ex))[:150]}. Hazlo a mano en BingX.")
                    return False
            else:
                o = {}
        except ApiError as ex:
            self.notify(f"{self.tag} 🚨 NO pude cerrar {html.escape(sym)}: {html.escape(_clean(ex))[:150]}. Hazlo a mano en BingX.")
            return False
        time.sleep(0.8)
        try:
            last = self.api.public_price(sym)
        except (ApiError, NetError):
            last = tr["entry"]
        self._record_leg(tr, reason, o.get("orderId"), last, qty)
        self._finalize(tr, reason)
        return True

    def _finalize(self, tr: dict, reason: str) -> None:
        sym = tr["symbol"]
        for n in ("sl", "tp1", "tp2"):
            od = tr["orders"].get(n)
            if od and not od.get("done"):
                try:
                    self.api.cancel_order(sym, od["id"])
                except (ApiError, NetError):
                    pass
                od["done"] = True
        s = 1 if tr["side"] == "LONG" else -1
        legs = tr["legs"]
        pnl = sum(s * (l["px"] - tr["entry"]) * l["qty"] for l in legs)
        fees = tr["entry_fee"] + sum(l["fee"] for l in legs)
        tq = sum(l["qty"] for l in legs)
        denom = tr["risk"] * tr["qty"]
        r = (pnl - fees) / denom if denom > 0 else 0.0
        tr.update(closed=True, reason=reason, exit_ms=int(time.time() * 1000), R=r, pnl=pnl - fees,
                  exit_px=(sum(l["px"] * l["qty"] for l in legs) / tq) if tq > 0 else tr["entry"])
        self.open = [t for t in self.open if t["id"] != tr["id"]]
        self.closed.append(tr)
        self._sync()
        self._save()
        self.notify(self._close_msg(tr))

    # ============================================================
    # MANDOS (Telegram)
    # ============================================================
    def pause(self, flag: bool) -> str:
        with self.lock:
            self.paused = flag
            self._save()
        return ("⏸ Pausado: no abriré operaciones nuevas (las abiertas siguen gestionadas)." if flag
                else "▶️ Reanudado: vuelvo a abrir operaciones con las señales nuevas.")

    def panic(self) -> str:
        with self.lock:
            self.paused = True
            n = len(self.open)
            for tr in list(self.open):
                try:
                    self._market_close(tr, "PANIC")
                except Exception as ex:  # noqa: BLE001
                    log.exception("panic %s", tr["symbol"])
                    self.notify(f"{self.tag} 🚨 Fallo al cerrar {html.escape(tr['symbol'])}: {html.escape(_clean(ex))[:150]}")
            self._save()
        return f"🧯 Cierre de emergencia: {n} operaciones procesadas y bot en pausa. Revisa BingX por si alguna quedó abierta."

    # ============================================================
    # MENSAJES
    # ============================================================
    def _head(self, tr: dict) -> str:
        return f"<b>{tr['side']}</b> · <b>{html.escape(tr['symbol'])}</b> · {tr['tf']} · {'+'.join(tr['kinds'])}"

    def _entry_msg(self, tr: dict, notional: float) -> str:
        pp, qp = tr["pp"], tr["qp"]
        icon = "🟢" if tr["side"] == "LONG" else "🔴"
        o = tr["orders"]
        lines = [
            f"{self.tag} {icon} {self._head(tr)}",
            f"Entrada <code>{fp(tr['entry'], pp)}</code> (señal {fp(tr['sig_px'], pp)}) · cantidad <code>{fq(tr['qty'], qp)}</code> "
            f"≈ {notional:.2f} USDT · {tr['lev']}x",
            f"🛑 SL <code>{fp(tr['sl'], pp)}</code>" + ("" if o["sl"] else " ⚠️ sin colocar"),
        ]
        if tr["has_partial"]:
            lines.append(f"🎯 TP1 <code>{fp(tr['tp1'], pp)}</code> ({fq(tr['qty_tp1'], qp)})" + ("" if o["tp1"] else " ⚠️ sin colocar")
                         + f" · 🏆 TP2 <code>{fp(tr['tp2'], pp)}</code> ({fq(tr['qty_rest'], qp)})" + ("" if o["tp2"] else " ⚠️ sin colocar"))
        else:
            lines.append(f"🏆 TP2 <code>{fp(tr['tp2'], pp)}</code> (sin parcial: cantidad demasiado pequeña)"
                         + ("" if o["tp2"] else " ⚠️ sin colocar"))
        lines.append(f"Riesgo ≈ {tr['risk_cash']:.2f} USDT")
        return "\n".join(lines)

    def _tp1_msg(self, tr: dict) -> str:
        return (f"{self.tag} 🎯 TP1 ejecutado · {self._head(tr)}\n"
                f"Cerrado parcial de <code>{fq(tr['qty_tp1'], tr['qp'])}</code>. Muevo el SL a la entrada "
                f"<code>{fp(tr['entry'], tr['pp'])}</code>; el resto sigue con trailing {self.cfg.trail_mult:g}R hacia TP2 "
                f"<code>{fp(tr['tp2'], tr['pp'])}</code>.")

    def _close_msg(self, tr: dict) -> str:
        names = {
            "SL": ("🛑", "Stop loss"), "TP2": ("🏆", "TP2 alcanzado"), "BE": ("⚪", "Cierre en break-even"),
            "TRAIL": ("✅", "Cierre por trailing"), "TIME": ("⏱", "Cierre por tiempo"),
            "SIGNAL": ("🔁", "Cierre por señal contraria"), "EMERGENCY": ("🚨", "Cierre de emergencia (sin SL)"),
            "PANIC": ("🧯", "Cierre manual de emergencia"), "EXTERNAL": ("❔", "Posición cerrada fuera del bot"),
        }
        icon, label = names.get(tr["reason"], ("ℹ️", tr["reason"]))
        return (f"{self.tag} {icon} {label} · {self._head(tr)}\n"
                f"Salida media ≈ <code>{fp(tr['exit_px'], tr['pp'])}</code> · resultado <b>{tr['R']:+.2f}R</b> "
                f"(≈ {tr['pnl']:+.2f} USDT, neto de comisiones) · {tr['bars']} velas\n"
                f"Hoy: {self.day_r():+.2f}R")

    @staticmethod
    def _summ(rows: list[dict]) -> Optional[dict]:
        if not rows:
            return None
        rs = [float(r.get("R", 0.0)) for r in rows]
        wins = [x for x in rs if x > 0]
        gl = -sum(x for x in rs if x <= 0)
        return {"n": len(rs), "wr": len(wins) / len(rs), "avg": sum(rs) / len(rs), "tot": sum(rs),
                "pf": (sum(wins) / gl) if gl > 0 else float("inf"), "usdt": sum(float(r.get("pnl", 0.0)) for r in rows)}

    def stats_message(self, days: Optional[float] = None, title: Optional[str] = None) -> str:
        with self.lock:
            since = 0 if days is None else int((time.time() - days * 86400) * 1000)
            rows = [t for t in self.closed if t.get("exit_ms", 0) >= since]
            n_open = len(self.open)
        sm = self._summ(rows)
        span = "total" if days is None else f"últimos {days:g} días"
        lines = [f"<b>{title or (self.tag + ' · resultados reales')}</b> ({span})",
                 f"Abiertas ahora: {n_open}" + (" · ⏸ pausado" if self.paused else "")]
        if not sm:
            lines.append("Aún no hay operaciones reales cerradas.")
            return "\n".join(lines)
        pf = "∞" if sm["pf"] == float("inf") else f"{sm['pf']:.2f}"
        lines += [f"Cerradas: {sm['n']} · win rate {sm['wr'] * 100:.0f}%",
                  f"Expectancia: <b>{sm['avg']:+.2f}R</b>/op · total {sm['tot']:+.1f}R ≈ {sm['usdt']:+.2f} USDT · PF {pf}",
                  f"Hoy: {self.day_r():+.2f}R (límite -{self.L.daily_loss_r:g}R)"]
        return "\n".join(lines)

    def open_message(self) -> str:
        with self.lock:
            opn = list(self.open)
        if not opn:
            return f"{self.tag}: no hay operaciones reales abiertas."
        lines = [f"<b>{self.tag} · operaciones abiertas ({len(opn)})</b>"]
        for t in opn:
            stage = "tras TP1" if t["tp1_done"] else "esperando TP1"
            lines.append(f"{'🟢' if t['side'] == 'LONG' else '🔴'} {html.escape(t['symbol'])} {t['tf']} {'+'.join(t['kinds'])} · "
                         f"ent {fp(t['entry'], t['pp'])} · SL {fp(t['sl'], t['pp'])} · {stage} · {t['bars']} velas")
        return "\n".join(lines)


# ============================================================
# CREACIÓN DESDE VARIABLES DE ENTORNO
# ============================================================
def make_live(cfg, notifier, force: bool = False) -> Optional[LiveTrader]:
    e = os.getenv

    def s(name, d):
        return e(name, str(d)).strip()

    def f(name, d):
        return float(s(name, d))

    enabled = s("LIVE_TRADING", "0").lower() in ("1", "true", "yes", "si", "sí", "on")
    if not enabled and not force:
        return None
    env = s("BINGX_ENV", "demo").lower()
    if env not in ("demo", "live"):
        raise SystemExit("BINGX_ENV debe ser 'demo' (dinero virtual) o 'live' (dinero real)")
    key, secret = s("BINGX_API_KEY", ""), s("BINGX_API_SECRET", "")
    if not key or not secret:
        if force:
            raise SystemExit("Faltan BINGX_API_KEY / BINGX_API_SECRET")
        log.error("LIVE_TRADING=1 pero faltan BINGX_API_KEY / BINGX_API_SECRET: solo señales")
        notifier.send("⛔ LIVE_TRADING=1 pero faltan BINGX_API_KEY / BINGX_API_SECRET: ejecución desactivada, solo señales.")
        return None
    pm = s("POSITION_MODE", "auto").lower()
    if pm not in ("auto", "hedge", "oneway"):
        raise SystemExit("POSITION_MODE debe ser auto, hedge u oneway")
    default_file = str(Path(cfg.state_file).with_name("live_trades.json"))
    L = SimpleNamespace(
        env=env, api_key=key, api_secret=secret, position_mode=pm,
        leverage=int(f("LIVE_LEVERAGE", 3)), max_live=int(f("MAX_LIVE_TRADES", 3)),
        max_drift_r=f("MAX_ENTRY_DRIFT_R", 0.25), daily_loss_r=f("DAILY_LOSS_LIMIT_R", 3.0),
        trigger=s("TRIGGER_PRICE", "MARK_PRICE").upper(), poll_sec=max(3.0, f("LIVE_POLL_SEC", 10)),
        rps=f("LIVE_RATE_LIMIT_RPS", 5), file=s("LIVE_TRADES_FILE", "") or default_file,
    )
    if L.trigger not in ("MARK_PRICE", "CONTRACT_PRICE", "INDEX_PRICE"):
        raise SystemExit("TRIGGER_PRICE debe ser MARK_PRICE, CONTRACT_PRICE o INDEX_PRICE")
    return LiveTrader(cfg, L, notifier)
