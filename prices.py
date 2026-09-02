from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import threading
import time

from config import FINNHUB_API_KEY


def month_end_price_date_iso(year: int, month_num: int, today_d) -> str:
    """Calendar date used for month-end price (today for the in-progress current month)."""
    if year == today_d.year and month_num == today_d.month:
        return today_d.isoformat()
    if month_num == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month_num + 1, 1)
    return (next_month - timedelta(days=1)).date().isoformat()

def _coingecko_history_date_param(iso_date: str) -> str:
    y, m, d = iso_date.split("-")
    return f"{int(d):02d}-{int(m):02d}-{y}"

_coingecko_history_last_at = 0.0

COINGECKO_HISTORY_MIN_INTERVAL = 0.35

def _throttle_coingecko_history():
    global _coingecko_history_last_at
    elapsed = time.time() - _coingecko_history_last_at
    if elapsed < COINGECKO_HISTORY_MIN_INTERVAL:
        time.sleep(COINGECKO_HISTORY_MIN_INTERVAL - elapsed)
    _coingecko_history_last_at = time.time()

_price_cache: dict = {"prices": {}, "fetched_at": 0.0}

_price_cache_lock = threading.Lock()

PRICE_CACHE_TTL = 300

def fetch_coingecko_prices(coin_ids, force=False):
    if not coin_ids:
        return {}
    now = time.time()
    cached = _price_cache
    with _price_cache_lock:
        if not force and cached["prices"] and (now - cached["fetched_at"]) < PRICE_CACHE_TTL:
            if all(cid in cached["prices"] for cid in coin_ids):
                return {cid: cached["prices"][cid] for cid in coin_ids}
    ids_str = ",".join(sorted(set(coin_ids)))
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=eur&include_24hr_change=true"
    try:
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "VibeBudgeting/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        prices = {}
        for cid in coin_ids:
            if cid in data and "eur" in data[cid]:
                prices[cid] = {
                    "price": data[cid]["eur"],
                    "change_24h": data[cid].get("eur_24h_change"),
                }
        with _price_cache_lock:
            cached["prices"].update(prices)
            cached["fetched_at"] = time.time()
        return prices
    except (URLError, HTTPError, json.JSONDecodeError, OSError):
        with _price_cache_lock:
            return {cid: cached["prices"][cid] for cid in coin_ids if cid in cached["prices"]}

def fetch_coingecko_history_eur(coin_id: str, price_date_iso: str) -> float | None:
    """CoinGecko daily snapshot for a calendar date (dd-mm-yyyy query param)."""
    date_param = _coingecko_history_date_param(price_date_iso)
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/history?date={date_param}"
    _throttle_coingecko_history()
    try:
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "VibeBudgeting/1.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        market = data.get("market_data") or {}
        current = market.get("current_price") or {}
        eur = current.get("eur")
        if eur is not None and float(eur) > 0:
            return float(eur)
    except (URLError, HTTPError, json.JSONDecodeError, OSError, ValueError, TypeError):
        return None
    return None

def fetch_stock_month_close_usd(symbol: str, price_date_iso: str) -> float | None:
    """Last available close in the month window up to price_date_iso, in USD."""
    lookup = _quote_lookup_symbol(symbol)
    try:
        import yfinance as yf

        end_d = datetime.strptime(price_date_iso, "%Y-%m-%d").date()
        start_d = end_d.replace(day=1)
        hist = yf.Ticker(lookup).history(
            start=start_d.isoformat(),
            end=(end_d + timedelta(days=1)).isoformat(),
            auto_adjust=True,
        )
        if hist is None or hist.empty:
            return None
        close = float(hist["Close"].iloc[-1])
        if close <= 0:
            return None
        return _listing_price_to_usd(lookup, close)
    except Exception:
        return None

def _is_current_report_month(year: int, month_num: int, today_d) -> bool:
    return year == today_d.year and month_num == today_d.month

def ensure_crypto_month_price(conn, coin_id: str, year: int, month_num: int, today_d):
    if _is_current_report_month(year, month_num, today_d):
        live = fetch_coingecko_prices([coin_id], force=False)
        return live.get(coin_id)

    row = conn.execute(
        """
        SELECT price_eur FROM crypto_month_prices
        WHERE coin_id = ? AND year = ? AND month = ?
        """,
        (coin_id, year, month_num),
    ).fetchone()
    if row:
        return {"price": float(row["price_eur"]), "source": "cache"}

    price_date = month_end_price_date_iso(year, month_num, today_d)
    price = fetch_coingecko_history_eur(coin_id, price_date)
    source = "coingecko"
    if price is None:
        live = fetch_coingecko_prices([coin_id], force=False)
        pinfo = live.get(coin_id)
        if pinfo:
            price = float(pinfo["price"])
            source = "coingecko_live_fallback"
    if price is None:
        return None

    conn.execute(
        """
        INSERT OR REPLACE INTO crypto_month_prices
            (coin_id, year, month, price_eur, price_date, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (coin_id, year, month_num, price, price_date, source),
    )
    return {"price": price, "source": source}

def ensure_stock_month_price(conn, symbol: str, year: int, month_num: int, today_d):
    if _is_current_report_month(year, month_num, today_d):
        live = fetch_finnhub_quotes([symbol], force=False)
        return live.get(symbol)

    row = conn.execute(
        """
        SELECT price_usd FROM stock_month_prices
        WHERE symbol = ? AND year = ? AND month = ?
        """,
        (symbol, year, month_num),
    ).fetchone()
    if row:
        return {"price": float(row["price_usd"]), "source": "cache"}

    price_date = month_end_price_date_iso(year, month_num, today_d)
    price = fetch_stock_month_close_usd(symbol, price_date)
    source = "yfinance"
    if price is None:
        live = fetch_finnhub_quotes([symbol], force=False)
        pinfo = live.get(symbol)
        if pinfo:
            price = float(pinfo["price"])
            source = "finnhub_live_fallback"
    if price is None:
        return None

    conn.execute(
        """
        INSERT OR REPLACE INTO stock_month_prices
            (symbol, year, month, price_usd, price_date, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (symbol, year, month_num, price, price_date, source),
    )
    return {"price": price, "source": source}

def prices_for_holdings_at_month_end(
    conn, holdings, year: int, month_num: int, today_d, price_key: str, asset_kind: str
) -> dict:
    price_map = {}
    for h in holdings:
        key = h[price_key]
        if asset_kind == "crypto":
            pinfo = ensure_crypto_month_price(conn, key, year, month_num, today_d)
        else:
            pinfo = ensure_stock_month_price(conn, key, year, month_num, today_d)
        if pinfo:
            price_map[key] = pinfo
    return price_map

_stock_price_cache: dict = {"prices": {}, "fetched_at": 0.0}

_stock_price_cache_lock = threading.Lock()

_fx_usd_cache: dict = {"rates": {}, "fetched_at": 0.0}

_fx_usd_cache_lock = threading.Lock()

_EUR_LISTING_SUFFIXES = (
    ".DE",
    ".AS",
    ".PA",
    ".MI",
    ".HE",
    ".SW",
    ".BR",
    ".VI",
    ".OL",
    ".ST",
    ".CO",
    ".IR",
    ".LS",
    ".MC",
    ".BE",
    ".WA",
)

_GBP_LISTING_SUFFIXES = (".L",)

_FREEDOM_EU_SUFFIX = ".EU"

def _quote_lookup_symbol(symbol):
    """Map broker symbols to a symbol Yahoo/Finnhub can quote."""
    sym = (symbol or "").strip().upper()
    if sym.endswith(_FREEDOM_EU_SUFFIX):
        return f"{sym[: -len(_FREEDOM_EU_SUFFIX)]}.DE"
    return sym

def _finnhub_request(path):
    token = FINNHUB_API_KEY
    if not token:
        return None
    sep = "&" if "?" in path else "?"
    url = f"https://finnhub.io/api/v1{path}{sep}token={token}"
    try:
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "VibeBudgeting/1.0"})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (URLError, HTTPError, json.JSONDecodeError, OSError):
        return None

def _yfinance_last_price(symbol):
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        price = getattr(ticker, "fast_info", {}).get("lastPrice")
        if price is not None and float(price) > 0:
            return float(price)
        hist = ticker.history(period="5d")
        if hist is not None and not hist.empty:
            close = float(hist["Close"].iloc[-1])
            if close > 0:
                return close
    except Exception:
        return None
    return None

def _finnhub_fx_to_usd(pair):
    """OANDA:EUR_USD / OANDA:GBP_USD → USD per 1 unit of base currency."""
    now = time.time()
    cached = _fx_usd_cache
    with _fx_usd_cache_lock:
        if not cached["rates"] or (now - cached["fetched_at"]) >= PRICE_CACHE_TTL:
            cached["rates"] = {}
            cached["fetched_at"] = now
        if pair in cached["rates"]:
            return cached["rates"][pair]
    data = _finnhub_request(f"/quote?symbol={pair}")
    if data and data.get("c") is not None and float(data["c"]) > 0:
        rate = float(data["c"])
        with _fx_usd_cache_lock:
            cached["rates"][pair] = rate
        return rate
    yf_symbol = "EURUSD=X" if "EUR" in pair else "GBPUSD=X"
    yf_rate = _yfinance_last_price(yf_symbol)
    if yf_rate:
        with _fx_usd_cache_lock:
            cached["rates"][pair] = yf_rate
        return yf_rate
    return None

def _listing_price_to_usd(symbol, price):
    sym = symbol.upper()
    if any(sym.endswith(suf) for suf in _GBP_LISTING_SUFFIXES):
        rate = _finnhub_fx_to_usd("OANDA:GBP_USD")
        if rate:
            return price * rate
    if any(sym.endswith(suf) for suf in _EUR_LISTING_SUFFIXES):
        rate = _finnhub_fx_to_usd("OANDA:EUR_USD")
        if rate:
            return price * rate
    return price

def fetch_finnhub_quotes(symbols, force=False):
    if not symbols:
        return {}
    now = time.time()
    cached = _stock_price_cache
    with _stock_price_cache_lock:
        if not force and cached["prices"] and (now - cached["fetched_at"]) < PRICE_CACHE_TTL:
            if all(sym in cached["prices"] for sym in symbols):
                return {sym: cached["prices"][sym] for sym in symbols}
    if force:
        with _fx_usd_cache_lock:
            _fx_usd_cache["rates"] = {}
            _fx_usd_cache["fetched_at"] = 0.0
    prices = {}
    for sym in sorted(set(symbols)):
        raw = None
        change_24h = None
        lookup = _quote_lookup_symbol(sym)
        data = _finnhub_request(f"/quote?symbol={lookup}") if FINNHUB_API_KEY else None
        if data and data.get("c") is not None and float(data["c"]) > 0:
            raw = float(data["c"])
            change_24h = float(data["dp"]) if data.get("dp") is not None else None
        if raw is None:
            yf_raw = _yfinance_last_price(lookup)
            if yf_raw is not None and yf_raw > 0:
                raw = yf_raw
        if raw is not None and raw > 0:
            prices[sym] = {
                "price": _listing_price_to_usd(lookup, raw),
                "change_24h": change_24h,
                "source": "finnhub" if data and data.get("c") else "yfinance",
            }
    with _stock_price_cache_lock:
        cached["prices"].update(prices)
        cached["fetched_at"] = time.time()
    return prices
