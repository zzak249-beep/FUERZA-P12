"""Calcula TODAS las características de una sola vez (vectorizado), sin mirar al futuro.
Lo usan igual el bot en vivo (última vela) y el backtester (todas las velas)."""
import numpy as np
import pandas as pd
import config as C
from indicators import sma, ema, atr, rsi, macd_hist, vwap_daily, hap_score


def ma_f(s, n):
    return sma(s, n) if C.MA_TYPE == "SMA" else ema(s, n)


def _warm():
    return int(round(max(C.HAP_FIRST, C.HAP_STEP * 29) * C.HAP_WARM_F))


def _hap_series(close):
    sc = hap_score(close, C.HAP_FIRST, C.HAP_STEP, C.HAP_SMOOTH,
                   C.HAP_W_SLOPE, C.HAP_W_PERIOD, C.HAP_W_ALIGN).copy()
    sc.iloc[:_warm()] = np.nan
    return sc


def _align(series, d, tf, close_times):
    """Valor de una serie del TF superior disponible al cierre de cada vela base (solo velas HTF ya cerradas)."""
    s = pd.Series(np.asarray(series), index=d.index + pd.Timedelta(seconds=C.tf_sec(tf)))
    s = s[~s.index.duplicated()]
    return s.reindex(close_times, method="ffill").values


def resample(df, tf):
    rule = {"1h": "1h", "4h": "4h", "1d": "1D", "15m": "15min"}[tf]
    r = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return r.dropna()


def needed_tfs():
    return {C.HTF_TF, C.HTF2_TF, C.HAP_TF1, C.HAP_TF2}


def build(df, htf, funding=None):
    c, o, h, l, v = (df[k] for k in ("close", "open", "high", "low", "volume"))
    n = len(df)
    ma = ma_f(c, C.MA_LEN)
    a = atr(df, C.ATR_LEN)
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    atrp = a / c
    F = {
        "idx": df.index, "open": o.values, "high": h.values, "low": l.values, "close": c.values,
        "ma": ma.values, "atr": a.values,
        "strong": (body > body.rolling(20).mean() * C.STRENGTH_MULT).values,
        "dist": ((c - ma) / a).values,
        "rvol": (v / sma(v, C.RVOL_LEN)).values,
        "vwap": vwap_daily(df).values,
        "rsi": rsi(c, C.RSI_LEN).values,
        "hist": macd_hist(c, C.MACD_F, C.MACD_S, C.MACD_SIG).values,
        "clv": ((c - l) / rng).fillna(0.5).values,
        "atr_ratio": (atrp / atrp.rolling(200, min_periods=50).median()).values,
        "sc15": _hap_series(c).values,
        "hour": df.index.hour.values, "wday": df.index.dayofweek.values,
    }
    ct = df.index + pd.Timedelta(seconds=C.TF_SEC)

    def slope_feats(tf):
        d = htf[tf]
        m = ma_f(d.close, C.MA_LEN)
        return _align(m.diff().values, d, tf, ct), _align(atr(d, C.ATR_LEN).values, d, tf, ct)

    F["s4"], F["a4"] = slope_feats(C.HTF_TF)
    F["s2"], F["a2"] = slope_feats(C.HTF2_TF)
    d1, d2 = htf[C.HAP_TF1], htf[C.HAP_TF2]
    F["sc1"] = _align(_hap_series(d1.close).values, d1, C.HAP_TF1, ct)
    F["sc4"] = _align(_hap_series(d2.close).values, d2, C.HAP_TF2, ct)
    if funding is None:
        F["funding"] = np.full(n, np.nan)
    elif isinstance(funding, pd.Series):
        fs = funding[~funding.index.duplicated()].sort_index()
        F["funding"] = fs.reindex(ct, method="ffill").values
    else:
        F["funding"] = np.full(n, float(funding))
    return F


def build_light(df):
    """Mínimo para detectar candidatos (barato): el resto se calcula solo si hay candidato."""
    c, o = df.close, df.open
    ma = ma_f(c, C.MA_LEN)
    a = atr(df, C.ATR_LEN)
    body = (c - o).abs()
    return {"idx": df.index, "open": o.values, "high": df.high.values, "low": df.low.values,
            "close": c.values, "ma": ma.values, "atr": a.values,
            "strong": (body > body.rolling(20).mean() * C.STRENGTH_MULT).values,
            "dist": ((c - ma) / a).values}
