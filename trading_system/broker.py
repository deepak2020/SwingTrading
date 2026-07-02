"""
Broker abstraction: a PaperBroker (fully working, simulated fills) and a
NordnetBroker adapter (structured against the documented nExt API, gated behind
safety flags).

IMPORTANT — the NordnetBroker is an INTEGRATION SKELETON, not a verified live
client. Nordnet's API access is customer/partner-gated and its auth/endpoints
change. Before using broker='nordnet' with --live you MUST:
  1. Confirm your account has API access (nordnet.se).
  2. Verify every endpoint, the login/auth scheme, and the order payload below
     against the CURRENT official Nordnet API documentation.
  3. Set NORDNET_VERIFIED=1 to acknowledge you have done so.
Until then, live Nordnet orders are refused by design.
"""

import base64
import time
from abc import ABC, abstractmethod

import requests

import config
import engine


class Broker(ABC):
    @abstractmethod
    def execute(self, state, order):
        """Execute an Order, mutate state, return a fill dict."""


class PaperBroker(Broker):
    """Simulates fills at the order's reference price. Fully functional."""

    def execute(self, state, order):
        fee = engine.apply_fill(state, order)
        return {"status": "FILLED", "ticker": order.ticker, "side": order.side,
                "shares": order.shares, "price": order.price, "fee": round(fee, 2),
                "mode": "paper"}


class NordnetBroker(Broker):
    """Adapter for the Nordnet nExt API (VERIFY against current docs before live).

    Auth flow (classic nExt v2 scheme — CONFIRM it is still current):
      POST {base}/login  with an RSA-encrypted 'auth' blob + service=NEXTAPI,
      then use the returned session_key as HTTP basic auth (key:key) on all
      subsequent calls. Nordnet publishes the RSA public key used to encrypt
      "{user}:{pass}:{timestamp_ms}".
    """

    def __init__(self, live=False):
        self.live = live
        self.session = requests.Session()
        self.session_key = None
        self._instrument_cache = {}

    # ---- auth ----
    def login(self):
        if not (config.NORDNET_USER and config.NORDNET_PASS):
            raise RuntimeError("Set NORDNET_USER / NORDNET_PASS in the environment.")
        # VERIFY: RSA public key + exact 'auth' construction against current docs.
        auth_blob = self._build_auth(config.NORDNET_USER, config.NORDNET_PASS)
        r = self.session.post(f"{config.NORDNET_BASE}/login",
                              data={"auth": auth_blob, "service": "NEXTAPI"}, timeout=30)
        r.raise_for_status()
        self.session_key = r.json().get("session_key")
        if not self.session_key:
            raise RuntimeError(f"Login returned no session_key: {r.text[:200]}")
        self.session.auth = (self.session_key, self.session_key)
        return self.session_key

    def _build_auth(self, user, password):
        # PLACEHOLDER — replace with the current documented scheme.
        # The classic scheme RSA-encrypts f"{user}:{password}:{int(time*1000)}"
        # with Nordnet's published public key, then base64-encodes it.
        raise NotImplementedError(
            "Implement _build_auth() per current Nordnet API docs before live use.")

    # ---- instrument lookup ----
    def resolve_instrument(self, ticker):
        """Map a Yahoo '.ST' ticker to a Nordnet instrument id + market id."""
        if ticker in self._instrument_cache:
            return self._instrument_cache[ticker]
        query = ticker.replace(".ST", "").replace("-", " ")
        r = self.session.get(f"{config.NORDNET_BASE}/instruments",
                             params={"query": query}, timeout=30)
        r.raise_for_status()
        hits = r.json()
        if not hits:
            raise RuntimeError(f"No Nordnet instrument for {ticker!r} (query={query!r}).")
        inst = hits[0]  # VERIFY: pick the correct market/segment, not just first hit
        info = {"identifier": inst["identifier"], "market_id": inst["market_id"]}
        self._instrument_cache[ticker] = info
        return info

    # ---- orders ----
    def execute(self, state, order):
        if not self.live:
            return {"status": "DRY_RUN", "ticker": order.ticker, "side": order.side,
                    "shares": order.shares, "price": order.price, "mode": "nordnet-dry"}
        if not config.NORDNET_VERIFIED:
            raise RuntimeError(
                "Refusing live Nordnet order: set NORDNET_VERIFIED=1 only after you have "
                "verified the API auth/endpoints/payload against the current docs.")
        if self.session_key is None:
            self.login()
        inst = self.resolve_instrument(order.ticker)
        payload = {   # VERIFY payload keys/values against current docs
            "identifier": inst["identifier"],
            "market_id": inst["market_id"],
            "side": order.side,                 # BUY / SELL
            "order_type": "MARKET",
            "volume": order.shares,
            "currency": "SEK",
        }
        r = self.session.post(
            f"{config.NORDNET_BASE}/accounts/{config.NORDNET_ACCNO}/orders",
            data=payload, timeout=30)
        r.raise_for_status()
        result = r.json()
        # keep local state in sync with the confirmed fill
        engine.apply_fill(state, order)
        return {"status": result.get("result_code", "SENT"), "ticker": order.ticker,
                "side": order.side, "shares": order.shares, "raw": result, "mode": "nordnet-live"}


def get_broker(name, live=False):
    if name == "paper":
        return PaperBroker()
    if name == "nordnet":
        return NordnetBroker(live=live)
    raise ValueError(f"Unknown broker {name!r}")
