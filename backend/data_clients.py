"""
data_clients.py — Layer 1 data ingestion: Finnhub (REST + WebSocket, 60 req/min
token bucket), FRED, CBOE daily stats, CNN Fear & Greed, and optional adapter
stubs for scrape-based sources.

Scrape-based adapters (Barchart/Finviz/Market Chameleon/Reddit) are stubs by
design: those sites' terms may prohibit scraping. The core signal engine runs
fully on the official free APIs. Enable an adapter only after checking the ToS.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from collections import deque
from typing import Callable, Optional

import requests


# ----------------------------------------------------------------------------- rate limit + cache

class TokenBucket:
    """Stay under Finnhub's 60 req/min free-tier limit."""

    def __init__(self, rate_per_min: int = 55):  # small headroom
        self.capacity = rate_per_min
        self.tokens = float(rate_per_min)
        self.rate = rate_per_min / 60.0
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


class TTLCache:
    def __init__(self):
        self._store: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._store.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            self._store.pop(key, None)
            return None

    def set(self, key, value, ttl: float):
        with self._lock:
            self._store[key] = (time.monotonic() + ttl, value)


CACHE = TTLCache()


def cached(key: str, ttl: float, fetch: Callable):
    hit = CACHE.get(key)
    if hit is not None:
        return hit
    value = fetch()
    if value is not None:
        CACHE.set(key, value, ttl)
    return value


# ----------------------------------------------------------------------------- Finnhub

class FinnhubClient:
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, api_key: Optional[str] = None, ttl_quote: float = 5):
        self.key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        self.bucket = TokenBucket()
        self.ttl_quote = ttl_quote
        # Finnhub's free tier 403s /stock/candle. Once we see that, stop retrying
        # it process-wide (it burns a rate-limit token every call) and go straight
        # to the Alpaca bar fallback.
        self._candle_endpoint_dead = False

    def _get(self, path: str, **params):
        self.bucket.acquire()
        params["token"] = self.key
        r = requests.get(f"{self.BASE}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def quote(self, symbol: str) -> dict:
        """{'c': last, 'h':, 'l':, 'o':, 'pc': prev close, 'dp': pct change}"""
        return cached(f"q:{symbol}", self.ttl_quote, lambda: self._get("/quote", symbol=symbol))

    def candles(self, symbol: str, resolution: str = "D", days: int = 320) -> Optional[dict]:
        def fetch():
            now = int(time.time())
            if not self._candle_endpoint_dead:
                try:
                    result = self._get("/stock/candle", symbol=symbol,
                                       resolution=resolution,
                                       **{"from": now - days * 86400, "to": now})
                    if result and result.get("s") == "ok":
                        return result
                    self._candle_endpoint_dead = True  # no_data/access denied
                except Exception:
                    self._candle_endpoint_dead = True
            return _alpaca_bars().candles(symbol, resolution, days)
        # Cache the FINAL result (Finnhub or Alpaca) so the fallback is cached too.
        return cached(f"c:{symbol}:{resolution}:{days}", 300, fetch)

    def earnings_calendar(self, frm: str, to: str, symbol: str = "") -> dict:
        return cached(f"earn:{frm}:{to}:{symbol}", 3600, lambda: self._get(
            "/calendar/earnings", **{"from": frm, "to": to, "symbol": symbol}))

    def company_news(self, symbol: str, frm: str, to: str) -> list:
        return cached(f"news:{symbol}", 600, lambda: self._get(
            "/company-news", symbol=symbol, **{"from": frm, "to": to})) or []

    def earnings_in_days(self, symbol: str) -> Optional[int]:
        today = dt.date.today()
        cal = self.earnings_calendar(today.isoformat(),
                                     (today + dt.timedelta(days=30)).isoformat(), symbol)
        for e in (cal or {}).get("earningsCalendar", []):
            d = dt.date.fromisoformat(e["date"])
            return (d - today).days
        return None


class FinnhubStream:
    """WebSocket streaming, free tier ≤50 symbols. Runs in a daemon thread and
    invokes on_tick(symbol, price, volume, ts_ms)."""

    def __init__(self, symbols: list[str], on_tick: Callable, api_key: Optional[str] = None):
        self.symbols = symbols[:50]
        self.on_tick = on_tick
        self.key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        self._ws = None

    def start(self):
        import websocket  # websocket-client

        def on_open(ws):
            for s in self.symbols:
                ws.send(json.dumps({"type": "subscribe", "symbol": s}))

        def on_message(ws, message):
            data = json.loads(message)
            for t in data.get("data", []):
                self.on_tick(t["s"], t["p"], t.get("v", 0), t.get("t", 0))

        def run():
            while True:
                try:
                    self._ws = websocket.WebSocketApp(
                        f"wss://ws.finnhub.io?token={self.key}",
                        on_open=on_open, on_message=on_message)
                    self._ws.run_forever(ping_interval=20)
                except Exception:
                    pass
                time.sleep(5)  # reconnect backoff

        threading.Thread(target=run, daemon=True, name="finnhub-ws").start()


# ----------------------------------------------------------------------------- Alpaca historical bars (candle fallback)

class AlpacaBarClient:
    """Alpaca historical bar data in Finnhub candle-dict format so callers need
    no changes. Used automatically when Finnhub returns 403 / no_data."""

    _TF_MAP = {"D": ("Day", 1), "W": ("Week", 1), "60": ("Hour", 1), "M": ("Month", 1)}

    def __init__(self):
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
        self._client = None
        if api_key and secret_key:
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                self._client = StockHistoricalDataClient(
                    api_key=api_key, secret_key=secret_key)
            except Exception:
                pass

    def candles(self, symbol: str, resolution: str = "D", days: int = 320) -> Optional[dict]:
        if not self._client:
            return None
        try:
            import datetime as _dt
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            unit_name, mult = self._TF_MAP.get(resolution, ("Day", 1))
            tf = TimeFrame(mult, getattr(TimeFrameUnit, unit_name))
            start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days + 5)
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start)
            barset = self._client.get_stock_bars(req)
            bars = barset.data.get(symbol, [])
            if not bars:
                return None
            return {
                "s": "ok",
                "t": [int(b.timestamp.timestamp()) for b in bars],
                "o": [float(b.open)   for b in bars],
                "h": [float(b.high)   for b in bars],
                "l": [float(b.low)    for b in bars],
                "c": [float(b.close)  for b in bars],
                "v": [float(b.volume) for b in bars],
            }
        except Exception:
            return None


_alpaca_bar_instance: Optional[AlpacaBarClient] = None


def _alpaca_bars() -> AlpacaBarClient:
    global _alpaca_bar_instance
    if _alpaca_bar_instance is None:
        _alpaca_bar_instance = AlpacaBarClient()
    return _alpaca_bar_instance


# ----------------------------------------------------------------------------- FRED

FRED_SERIES = {
    "DFF": "fed_funds_rate", "T10Y2Y": "yield_curve_2s10s", "CPIAUCSL": "cpi",
    "VIXCLS": "vix_close", "DTWEXBGS": "dxy", "UNRATE": "unemployment",
}


class FREDClient:
    BASE = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(self, api_key: Optional[str] = None):
        self.key = api_key or os.environ.get("FRED_API_KEY", "")

    def latest(self, series_id: str, n: int = 10) -> list[dict]:
        def fetch():
            r = requests.get(self.BASE, params={
                "series_id": series_id, "api_key": self.key, "file_type": "json",
                "limit": n, "sort_order": "desc"}, timeout=10)
            r.raise_for_status()
            obs = r.json().get("observations", [])
            return [o for o in obs if o.get("value") not in (".", None)]
        return cached(f"fred:{series_id}", 3600, fetch) or []

    def latest_value(self, series_id: str) -> Optional[float]:
        obs = self.latest(series_id, 1)
        return float(obs[0]["value"]) if obs else None

    def macro_snapshot(self) -> dict:
        out = {}
        for sid, name in FRED_SERIES.items():
            try:
                out[name] = self.latest_value(sid)
            except Exception:
                out[name] = None
        return out


# ----------------------------------------------------------------------------- sentiment gauges

# NOTE: a CBOE put/call-ratio fetcher used to live here, but CBOE's free
# market-statistics endpoints now return Access Denied, so it could only ever
# return None. ctx.put_call_ratio therefore stays at its neutral default and the
# extreme_put_call signal is dormant until a permitted source is wired in. Rather
# than keep dead code that looks live, it's removed; see README "Spec deviations".


def fear_greed_index() -> Optional[float]:
    """CNN Fear & Greed via its public JSON endpoint (widely used; revisit if it moves)."""
    def fetch():
        try:
            r = requests.get(
                "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            r.raise_for_status()
            return float(r.json()["fear_and_greed"]["score"])
        except Exception:
            return None
    return cached("fng", 1800, fetch)


# ----------------------------------------------------------------------------- optional adapters (stubs)

class OptionsFlowAdapter:
    """Interface for unusual options activity. Default implementation returns
    nothing (the conviction criterion simply won't fire). Implement `fetch` against
    a source whose terms you have verified (e.g., a paid flow API, or Barchart's
    free tier if their ToS permits your use)."""

    def fetch(self) -> list[dict]:
        """Return [{symbol, premium, direction('bullish'|'bearish'), strike, expiry,
        dte, otm(bool), single_print(bool), ts}]"""
        return []


class RedditSentimentAdapter:
    """Stub for r/options & r/algotrading mention counts via the official Reddit
    API (praw). Requires REDDIT_CLIENT_ID/SECRET; contrarian signal per spec."""

    def fetch(self, symbols: list[str]) -> dict:
        return {}


# ----------------------------------------------------------------------------- FinBERT (optional, local)

class SentimentScorer:
    """FinBERT if transformers+torch are installed (≈420MB model download on first
    run); otherwise a light keyword fallback so the pipeline never blocks."""

    POS = ("beat", "beats", "record", "surge", "upgrade", "strong", "growth", "raises")
    NEG = ("miss", "misses", "selloff", "downgrade", "weak", "cuts", "lawsuit",
           "probe", "falls", "plunge")

    def __init__(self):
        self._pipe = None
        try:
            from transformers import pipeline  # type: ignore
            self._pipe = pipeline("text-classification", model="ProsusAI/finbert",
                                  tokenizer="ProsusAI/finbert")
        except Exception:
            self._pipe = None

    def score(self, headlines: list[str]) -> float:
        """Mean sentiment in [-1, 1]."""
        if not headlines:
            return 0.0
        if self._pipe:
            res = self._pipe(headlines[:16], truncation=True)
            vals = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
            return sum(vals[r["label"]] * r["score"] for r in res) / len(res)
        score = 0
        for h in headlines[:16]:
            hl = h.lower()
            score += sum(w in hl for w in self.POS) - sum(w in hl for w in self.NEG)
        return max(min(score / max(len(headlines), 1), 1.0), -1.0)
