import logging
import time

import config as C
import features
import strategy
import universe
from meta import Brain
from notifier import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

BOOKS = {0: "Base", 1: "V2 (filtros)", 2: "V3 (+momentum)", 3: "V4 (+HaP)"}


def fetch_all(trader, sym):
    df = trader.fetch_closed(sym, C.TF, 500)
    htf = {t: (df if t == C.TF else trader.fetch_closed(sym, t, 300)) for t in features.needed_tfs()}
    return df, htf


def scan_symbol(trader, brain, sym, cls, bar_id, now, last_bar):
    """Devuelve (score, sym, sig, info, mult) si hay señal válida; None si no."""
    df = trader.fetch_closed(sym, C.TF, 120)
    expected = (bar_id - 1) * C.TF_SEC * 1000
    if len(df) < 60 or int(df.index[-1].timestamp() * 1000) != expected:
        if now % C.TF_SEC > 120:
            last_bar[sym] = bar_id   # mercado cerrado / sin datos nuevos
        return None
    last_bar[sym] = bar_id
    if sym in trader.state:
        trader.trail_update(sym, float(df.close.iloc[-1]))
        return None
    close_ts = df.index[-1] + __import__("pandas").Timedelta(seconds=C.TF_SEC)
    if not universe.market_open(cls, close_ts):
        return None
    if not strategy.has_candidate(features.build_light(df)):
        return None
    df, htf = fetch_all(trader, sym)
    F = features.build(df, htf, trader.funding(sym) if C.FUNDING_ON else None)
    F["tradfi"] = universe.is_tradfi(cls)
    sig = strategy.signal_at(F, len(df) - 1, C.BOOK)
    if not sig:
        return None
    ok, info, mult, p = brain.check(sig)
    if not ok:
        log.info("Señal bloqueada %s %s: %s", sym, sig["code"], info)
        return None
    score = p if p is not None else float(F["rvol"][-1])
    return (score, sym, sig, info, mult)


def main():
    from trader import Trader
    brain = Brain()
    trader = Trader(notify, brain)
    last_bar, cooldown, cls_of = {}, {}, {}
    syms, last_uni = [], 0
    g = brain.gate
    notify(
        "🤖 <b>Granville Bot iniciado</b>\n"
        f"Modo: {'SOLO SEÑALES (DRY_RUN)' if C.DRY_RUN else ('DEMO' if C.DEMO else 'DINERO REAL')}\n"
        f"TF {C.TF} · Libro: {BOOKS.get(C.BOOK)} · Universo: {'TODO BingX (' + ','.join(C.CLASSES) + ')' if C.SCAN_ALL else ', '.join(C.SYMBOLS)}\n"
        f"Riesgo {C.RISK_PCT}% · Apal. máx {C.MAX_LEV}x · Máx. posiciones {C.MAX_POSITIONS}\n"
        f"Meta: {'ON' if C.META_ON else 'OFF'} (modelo {'n=' + str(g.n) if g.beta is not None else 'NO cargado'}) · "
        f"Estac.: {'ON' if C.SEASON_ON else 'OFF'} · Trailing: {'ON' if C.TRAIL_ON else 'OFF'} · "
        f"Funding: {'ON' if C.FUNDING_ON else 'OFF'} · Muestras: {len(brain.samples)}")

    while True:
        try:
            now = time.time()
            if C.SCAN_ALL and now - last_uni > C.UNIVERSE_REFRESH:
                try:
                    u = universe.build(trader.ex)
                    syms = [s for s, _, _ in u]
                    cls_of = {s: c for s, c, _ in u}
                    last_uni = now
                    from collections import Counter
                    notify(f"🔎 Universo: {len(u)} símbolos {dict(Counter(cls_of.values()))}")
                except Exception as e:
                    notify(f"⚠️ No pude construir el universo: {e}")
                    last_uni = now - C.UNIVERSE_REFRESH + 300
            elif not C.SCAN_ALL and not syms:
                syms = C.SYMBOLS
                cls_of = {s: universe.classify(trader.ex.market(s)) for s in syms}
            scan = list(dict.fromkeys(syms + list(trader.state.keys())))

            bar_id = int(now // C.TF_SEC)
            for sym in list(trader.state.keys()):
                trader.manage(sym)

            if now % C.TF_SEC >= 4:
                found = []
                for sym in scan:
                    if last_bar.get(sym) == bar_id:
                        continue
                    try:
                        r = scan_symbol(trader, brain, sym, cls_of.get(sym, "crypto"), bar_id, now, last_bar)
                        if r:
                            found.append(r)
                    except Exception as e:
                        log.warning("Error en %s: %s", sym, e)
                found.sort(key=lambda r: -r[0])
                for score, sym, sig, info, mult in found:
                    head = (f"{'🟢' if sig['side'] == 'long' else '🔴'} <b>SEÑAL {sig['side'].upper()} {sym}</b> "
                            f"[{sig['code']}] · {cls_of.get(sym, '')} · {BOOKS[C.BOOK]}")
                    if C.DRY_RUN:
                        if cooldown.get(sym, 0) > now:
                            continue
                        cooldown[sym] = now + C.MAX_BARS * C.TF_SEC
                        notify(f"{head}\nEntrada ~{sig['entry']:.6g}\nSL {sig['sl']:.6g}\n"
                               f"TP1 {sig['tp1']:.6g} · TP2 {sig['tp2']:.6g}\n"
                               f"Riesgo {sig['risk'] / sig['entry'] * 100:.2f}% del precio\n{info}")
                    else:
                        if trader.has_position(sym):
                            continue
                        if len(trader.state) >= C.MAX_POSITIONS:
                            notify(f"ℹ️ {len(found)} señal(es) este cierre; máx. posiciones ({C.MAX_POSITIONS}) alcanzado, "
                                   f"se descarta {sym} (puntuación {score:.2f})")
                            continue
                        if not trader.spread_ok(sym):
                            notify(f"ℹ️ {sym} descartado: spread > {C.MAX_SPREAD_PCT}%")
                            continue
                        notify(f"{head}\n{info}")
                        trader.open_trade(sym, sig, mult)
        except Exception as e:
            log.exception("Error en bucle")
            notify(f"⚠️ Error en bucle principal: {e}")
            time.sleep(30)
        time.sleep(10)


if __name__ == "__main__":
    main()
