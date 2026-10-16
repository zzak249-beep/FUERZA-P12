import os


def _s(k, d=""): return os.getenv(k, d).strip()
def _f(k, d): return float(os.getenv(k, str(d)))
def _i(k, d): return int(float(os.getenv(k, str(d))))
def _b(k, d): return os.getenv(k, str(d)).strip().lower() in ("1", "true", "yes", "si", "sí", "y")


# ---- credenciales / modo
BINGX_KEY = _s("BINGX_API_KEY")
BINGX_SECRET = _s("BINGX_API_SECRET")
TG_TOKEN = _s("TELEGRAM_TOKEN")
TG_CHAT = _s("TELEGRAM_CHAT_ID")
DRY_RUN = _b("DRY_RUN", True)
DEMO = _b("BINGX_DEMO", False)
POSITION_MODE = _s("POSITION_MODE", "hedge").lower()
SYMBOLS = [x.strip() for x in _s("SYMBOLS", "ALL").split(",") if x.strip()]
SCAN_ALL = any(x.upper() == "ALL" for x in SYMBOLS)   # ALL = analiza todo el universo filtrado de BingX
CLASSES = [x.strip().lower() for x in _s("ASSET_CLASSES", "crypto,stock").split(",") if x.strip()]
MIN_VOL_USDT = _f("MIN_VOL_USDT", 3_000_000)   # volumen 24h mínimo
MAX_SYMBOLS = _i("MAX_SYMBOLS", 120)
MAX_SPREAD_PCT = _f("MAX_SPREAD_PCT", 0.06)    # spread máximo (% del precio)
EXCLUDE = [x.strip().upper() for x in _s("EXCLUDE", "").split(",") if x.strip()]
STOCK_RTH_ONLY = _b("STOCK_RTH_ONLY", True)    # acciones: solo sesión regular NYSE (9:30-16:00 ET)
UNIVERSE_REFRESH = _i("UNIVERSE_REFRESH", 3600)
for _x in SYMBOLS:                      # SYMBOLS=TOP:30 -> los 30 más líquidos
    if _x.upper().startswith("TOP:"):
        SCAN_ALL = True
        MAX_SYMBOLS = int(_x.split(":")[1])
BOOK = _i("BOOK", 1)
RATE_LIMIT_MS = _i("RATE_LIMIT_MS", 250)          # BingX devolvía 100410 (límite de peticiones) con 100 ms
DAILY_LOSS_PCT = _f("DAILY_LOSS_PCT", 3.0)         # freno: si el día pierde ≥ x% del capital, no abre más hasta el día UTC siguiente
MAX_POSITIONS = _i("MAX_POSITIONS", 3)
DATA_DIR = _s("DATA_DIR", "") or os.path.dirname(_s("STATE_PATH", "")) or "."
STATE_PATH = os.path.join(DATA_DIR, "granville_state.json")   # nombre propio: no pisa archivos de otros bots

# ---- timeframe (15m o 1h). Los TF superiores se derivan solos.
_DUR = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_MAP = {"15m": ("4h", "1h", "1h", "4h"), "1h": ("1d", "4h", "4h", "1d")}


def tf_sec(tf): return _DUR[tf]


def set_tf(tf):
    global TF, TF_SEC, HTF_TF, HTF2_TF, HAP_TF1, HAP_TF2
    TF = tf
    TF_SEC = _DUR[tf]
    HTF_TF, HTF2_TF, HAP_TF1, HAP_TF2 = _MAP[tf]


set_tf(_s("TF", "15m"))

# ---- riesgo / ejecución
RISK_PCT = _f("RISK_PCT", 1.0)
MAX_LEV = _f("MAX_LEV", 10)
LEVERAGE = _i("LEVERAGE", 10)
PARTIAL_MODE = _b("PARTIAL_MODE", True)
TP_SINGLE_R = _f("TP_SINGLE_R", 1.5)
ALLOW_LONG = _b("ALLOW_LONG", True)
ALLOW_SHORT = _b("ALLOW_SHORT", True)

# ---- base (igual que el Pine)
MA_LEN = 50
MA_TYPE = "SMA"
ATR_LEN = 14
STRENGTH_MULT = 1.2
DEV_MULT = 2.5

# ---- salidas
STOP_MULT = 1.5
TP1_R = 1.0
TP2_R = 2.0
TP1_FRAC = 0.5
MOVE_BE = True
MAX_BARS = _i("MAX_BARS", 18)
COST_PCT = _f("COST_PCT", 0.12)

# ---- stop estructural
STRUCT_BUF = 0.3
MIN_RISK_ATR = 0.7
MAX_RISK_ATR = 3.0
SPRING_BARS = 3

# ---- filtros V2
HTF_FLAT = 0.10
RVOL_LEN = 20
RVOL_MIN = 1.2
USE_VWAP_EXT = True
ALLOW_ASIA = True
ALLOW_EU = True
ALLOW_US = True

# ---- V3
RSI_LEN = 14
RSI_TREND_MIN = 50.0
RSI_CHASE_MAX = 75.0
RSI_OS = 30.0
RSI_LOOK = 3
MACD_F, MACD_S, MACD_SIG = 12, 26, 9
RVOL2_MIN = 1.4
RVOL2_BARS = 2
CLV_MIN = 0.65
MAX_COST_R = 0.25

# ---- V4 (HaP reconstruido)
HAP_FIRST = 3
HAP_STEP = 3
HAP_SMOOTH = 2
HAP_W_SLOPE = 0.35
HAP_W_PERIOD = 0.35
HAP_W_ALIGN = 0.30
HAP_WARM_F = 1.5
HAP_MIN = 55.0
HAP_MAX = 90.0
HAP_MOM_LEN = 5
HAP_MTF1_MIN = 50.0
HAP_MTF2_MIN = 50.0
HAP_EXT_FLOOR = 40.0
HAP_BLOCK_IF_MISSING = True

# ================= MEJORAS NUEVAS (todas desactivables) =================
# 3) Meta-labeling: regresión logística que aprende cuándo la señal gana
META_ON = _b("META_ON", True)
META_MIN_N = _i("META_MIN_N", 80)          # muestras mínimas para fiarse del modelo
META_MARGIN = _f("META_MARGIN", 0.02)      # exige p >= tasa base + margen
META_HALF_LIFE = _f("META_HALF_LIFE", 400) # olvido (en operaciones); 0 = sin olvido
META_L2 = _f("META_L2", 5.0)
META_REFIT = _i("META_REFIT", 10)          # reentrenar cada N operaciones nuevas
META_STRICT = _b("META_STRICT", False)     # True: sin modelo entrenado no opera
META_SIZING = _b("META_SIZING", False)     # tamaño según p (0.7x–1.3x)
# 6) Estacionalidad aprendida (12 cubos: finde/laborable x tramo de 4h)
SEASON_ON = _b("SEASON_ON", True)
SEASON_MIN_N = _i("SEASON_MIN_N", 20)
SEASON_K = _f("SEASON_K", 10.0)
SEASON_BLOCK = _f("SEASON_BLOCK", -0.10)   # bloquea cubos con R medio encogido < x
# 4) Funding (OI no se incluye: no hay histórico para validarlo)
FUNDING_ON = _b("FUNDING_ON", False)
FUNDING_EXTREME = _f("FUNDING_EXTREME", 0.0003)  # 0,03% por 8h
# 5) Trailing ATR tras TP1 (sustituye al TP2) y escalado por volatilidad
TRAIL_ON = _b("TRAIL_ON", False)
TRAIL_ATR = _f("TRAIL_ATR", 2.0)
VOL_SCALE_ON = _b("VOL_SCALE_ON", False)
VOL_SCALE_LO = 0.5
VOL_SCALE_HI = 1.25
