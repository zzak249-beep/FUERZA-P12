"""Meta-labeling (logística en numpy), estacionalidad aprendida y 'cerebro' que aprende de cada operación."""
import json
import math
import os
import numpy as np
import config as C


def _nz(v, d=0.0):
    try:
        v = float(v)
    except Exception:
        return d
    return d if np.isnan(v) else v


def make_x(F, i, s, code, risk, price, A):
    """Vector de características en el momento de la señal (todo orientado a favor de la operación)."""
    r = F["rsi"]; hs = F["hist"]; rv = F["rvol"]
    g = int(code[1]) - (4 if s == -1 else 0)  # 1..4
    clv = F["clv"][i] if s == 1 else 1 - F["clv"][i]
    a4 = _nz(F["a4"][i], 1.0) or 1.0
    a2 = _nz(F["a2"][i], 1.0) or 1.0
    h = F["hour"][i]
    return [
        1.0 if code in ("G1", "G2", "G3", "G5", "G6", "G7") else 0.0,
        1.0 if g == 1 else 0.0, 1.0 if g == 2 else 0.0, 1.0 if g == 3 else 0.0,
        s * (_nz(r[i], 50) - 50) / 50,
        s * (_nz(r[i]) - _nz(r[i - 1])) / 10,
        s * _nz(hs[i]) / A * 10,
        s * (_nz(hs[i]) - _nz(hs[i - 1])) / A * 10,
        min(_nz(rv[i], 1), 5),
        min(max(_nz(rv[i], 1), _nz(rv[i - 1], 1)), 5),
        _nz(clv, 0.5),
        float(np.clip(s * _nz(F["dist"][i]), -5, 5)),
        float(np.clip(_nz(F["atr_ratio"][i], 1), 0, 4)),
        float(np.clip(s * _nz(F["s4"][i]) / a4, -3, 3)),
        float(np.clip(s * _nz(F["s2"][i]) / a2, -3, 3)),
        s * (_nz(F["sc15"][i], 50) - 50) / 50,
        s * (_nz(F["sc1"][i], 50) - 50) / 50,
        s * (_nz(F["sc4"][i], 50) - 50) / 50,
        math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24),
        1.0 if F["wday"][i] >= 5 else 0.0,
        float(np.clip(s * _nz(F["funding"][i]) / C.FUNDING_EXTREME, -3, 3)),
        risk / A,
        float(np.clip(C.COST_PCT / (risk / price * 100), 0, 1)),
        1.0 if F["strong"][i] else 0.0,
        1.0 if F.get("tradfi") else 0.0,
    ]


def sigmoid(z): return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d


class MetaGate:
    def __init__(self):
        self.beta = None; self.mu = None; self.sd = None; self.base = 0.5; self.n = 0

    def fit(self, samples):
        if len(samples) < C.META_MIN_N:
            return False
        X = np.array([s["x"] for s in samples], float)
        y = np.array([s["y"] for s in samples], float)
        age = np.arange(len(y))[::-1]
        w = 0.5 ** (age / C.META_HALF_LIFE) if C.META_HALF_LIFE > 0 else np.ones(len(y))
        mu = X.mean(0); sd = X.std(0) + 1e-9
        Z = np.c_[np.ones(len(X)), (X - mu) / sd]
        beta = np.zeros(Z.shape[1])
        reg = np.r_[0.0, np.ones(Z.shape[1] - 1)] * C.META_L2
        for _ in range(30):
            p = sigmoid(Z @ beta)
            g = Z.T @ (w * (p - y)) + reg * beta
            H = (Z.T * (w * p * (1 - p))) @ Z + np.diag(reg) + 1e-6 * np.eye(len(beta))
            step = np.linalg.solve(H, g)
            beta -= step
            if np.abs(step).max() < 1e-6:
                break
        self.beta, self.mu, self.sd = beta, mu, sd
        self.base = float(np.average(y, weights=w)); self.n = len(y)
        return True

    def p(self, x):
        z = (np.array(x, float) - self.mu) / self.sd
        return float(sigmoid(self.beta[0] + z @ self.beta[1:]))

    def allow(self, x, strict=False):
        if self.beta is None:
            return (not strict), None
        p = self.p(x)
        return p >= self.base + C.META_MARGIN, p

    def save(self, path):
        if self.beta is None:
            return
        with open(path, "w") as f:
            json.dump({"beta": self.beta.tolist(), "mu": self.mu.tolist(), "sd": self.sd.tolist(),
                       "base": self.base, "n": self.n}, f)

    def load(self, path):
        try:
            with open(path) as f:
                d = json.load(f)
            self.beta = np.array(d["beta"]); self.mu = np.array(d["mu"]); self.sd = np.array(d["sd"])
            self.base = d["base"]; self.n = d["n"]
            return True
        except Exception:
            return False


def _key(wday, hour): return (1 if wday >= 5 else 0, int(hour) // 4)


def season_add(stats, s):
    k = _key(s["wday"], s["hour"])
    n, sr = stats.get(k, (0, 0.0))
    stats[k] = (n + 1, sr + s["r"])


def season_ok(stats, hour, wday):
    n, sr = stats.get(_key(wday, hour), (0, 0.0))
    if n < C.SEASON_MIN_N:
        return True, None
    m = sr / (n + C.SEASON_K)
    return m >= C.SEASON_BLOCK, m


class Brain:
    """Carga modelo + muestras, decide si una señal pasa y aprende de cada operación cerrada."""

    def __init__(self):
        self.model_path = os.path.join(C.DATA_DIR, "granville_meta_model.json")
        self.samples_path = os.path.join(C.DATA_DIR, "granville_samples.jsonl")
        self.gate = MetaGate()
        self.samples = []
        self.stats = {}
        self.new = 0
        if not self.gate.load(self.model_path):
            self.gate.load("meta_model.json")
        src = self.samples_path if os.path.exists(self.samples_path) else "samples.jsonl"
        if os.path.exists(src):
            with open(src) as f:
                for line in f:
                    try:
                        self.samples.append(json.loads(line))
                    except Exception:
                        pass
        for s in self.samples:
            season_add(self.stats, s)

    def check(self, sig):
        """-> (ok, texto, multiplicador de tamaño, p)"""
        notes, ok, mult, pv = [], True, 1.0, None
        if C.SEASON_ON:
            s_ok, m = season_ok(self.stats, sig["hour"], sig["wday"])
            if m is not None:
                notes.append(f"horario {m:+.2f}R")
            if not s_ok:
                ok = False
        if C.META_ON:
            a, p = self.gate.allow(sig["x"], strict=C.META_STRICT)
            if p is None:
                notes.append("meta: sin modelo")
                if C.META_STRICT:
                    ok = False
            else:
                pv = p
                notes.append(f"meta p={p:.2f} (base {self.gate.base:.2f})")
                if not a:
                    ok = False
                elif C.META_SIZING:
                    mult = float(np.clip(p / self.gate.base, 0.7, 1.3))
        return ok, " · ".join(notes), mult, pv

    def record(self, sample):
        self.samples.append(sample)
        season_add(self.stats, sample)
        try:
            os.makedirs(C.DATA_DIR, exist_ok=True)
            with open(self.samples_path, "a") as f:
                f.write(json.dumps(sample) + "\n")
        except Exception:
            pass
        self.new += 1
        if C.META_ON and self.new >= C.META_REFIT:
            self.new = 0
            if self.gate.fit(self.samples):
                self.gate.save(self.model_path)
