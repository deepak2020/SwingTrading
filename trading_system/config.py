"""
Configuration for the OMXS30 momentum trading system.

All strategy parameters are the validated "champion" values from the research
in ../backtest.py and ../experiments/. Do not tune these lightly — the
walk-forward test (../experiments/walk_forward.py) showed re-tuning HURTS.
"""

import os

# ---- Strategy parameters (fixed — see experiments/) ----
TOP_N = 4                 # positions held at once
MOMENTUM_WEEKS = 12       # ranking lookback
RANK_BUFFER = 4           # hold while rank <= TOP_N + RANK_BUFFER (i.e. top 8)
TREND_SMA_WEEKS = 20      # only own stocks above their 20-week SMA
TRAIL_STOP_PCT = 0.20     # daily-checked 20% trailing stop
MAX_EXPOSURE = 0.85       # fraction of capital invested (rest cash buffer)

# ---- Account ----
CAPITAL = float(os.environ.get("CAPITAL", 100_000))   # SEK, initial (paper mode)
COMMISSION_PCT = 0.0015   # Nordnet 0.15%
COMMISSION_MIN = 59       # SEK minimum per trade

# ---- Universe: OMXS30 constituents (Yahoo tickers) ----
TICKERS = [
    "ABB.ST", "ADDT-B.ST", "ALFA.ST", "ASSA-B.ST", "AZN.ST", "ATCO-A.ST",
    "BOL.ST", "EPI-A.ST", "EQT.ST", "ERIC-B.ST", "ESSITY-B.ST", "EVO.ST",
    "HM-B.ST", "HEXA-B.ST", "INVE-B.ST", "LIFCO-B.ST", "NIBE-B.ST", "NDA-SE.ST",
    "SAAB-B.ST", "SAND.ST", "SEB-A.ST", "SKA-B.ST", "SKF-B.ST", "SCA-B.ST",
    "SHB-A.ST", "SWED-A.ST", "TEL2-B.ST", "TELIA.ST", "VOLV-B.ST",
]

# ---- Paths ----
# DATA_DIR holds the mutable state (positions, cash, logs). Defaults to this
# folder for local use; set STATE_DIR to a persistent-disk path when hosting
# online (e.g. STATE_DIR=/var/data on Render) so edits survive restarts.
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("STATE_DIR", _HERE)
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "trades.log")

# ---- Nordnet API (fill via environment; never hard-code secrets) ----
# Access must be enabled for your account — verify at nordnet.se and against the
# official API docs before switching the broker to 'nordnet'.
NORDNET_BASE = os.environ.get("NORDNET_BASE", "https://www.nordnet.se/api/2")
NORDNET_USER = os.environ.get("NORDNET_USER", "")
NORDNET_PASS = os.environ.get("NORDNET_PASS", "")
NORDNET_ACCNO = os.environ.get("NORDNET_ACCNO", "")
# Safety gate: live orders are refused unless you set this to "1" AFTER you have
# verified every endpoint/auth detail below against the current Nordnet docs.
NORDNET_VERIFIED = os.environ.get("NORDNET_VERIFIED", "0") == "1"
