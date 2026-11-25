#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HAKIM_FX signal bot - Python port of the Pine Script v6 indicator "HAKIM_FX By PhD.RAID".

Replays the exact zone / state / entry logic on CLOSED candles only, then posts
each new trade signal (pair, timeframe, direction, Entry / SL / TP) to a Telegram
channel, once per trade. Optionally also reports TP / SL hits.

Free data providers:
  - "oanda"      : OANDA v20 practice API (free demo account, real-time, generous limits)
  - "twelvedata" : Twelve Data free tier (800 credits/day - only fits ~3 symbols)

State (state.json) is committed back to the repo by the GitHub Actions workflow,
so each signal is sent exactly once.

Environment variables:
  TELEGRAM_TOKEN   (required)  bot token from @BotFather
  TELEGRAM_CHAT_ID (required)  channel chat id, e.g. -100xxxxxxxxxx
  PROVIDER         oanda (default) | twelvedata
  OANDA_TOKEN      required when PROVIDER=oanda (practice account token)
  TWELVEDATA_KEY   required when PROVIDER=twelvedata
  DRY_RUN          "1" -> print messages instead of sending them
  NOTIFY_OUTCOMES  "1" (default) also message when a signaled trade hits TP/SL
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import zlib
import math
from datetime import datetime, timezone

# ------------------------------------------------------------------ config

SYMBOLS = {
    # name:      pip size, OANDA instrument, Twelve Data symbol
    "EURUSD": {"pip": 0.0001, "oanda": "EUR_USD", "td": "EUR/USD"},
    "GBPUSD": {"pip": 0.0001, "oanda": "GBP_USD", "td": "GBP/USD"},
    "USDJPY": {"pip": 0.01,   "oanda": "USD_JPY", "td": "USD/JPY"},
    "AUDUSD": {"pip": 0.0001, "oanda": "AUD_USD", "td": "AUD/USD"},
    "USDCAD": {"pip": 0.0001, "oanda": "USD_CAD", "td": "USD/CAD"},
    "XAUUSD": {"pip": 0.1,    "oanda": "XAU_USD", "td": "XAU/USD"},
}

TIMEFRAMES = {
    # name: seconds per bar, bars to fetch, OANDA granularity, Twelve Data interval
    "5m":  {"sec": 300,   "bars": 5000, "oanda": "M5",  "td": "5min"},
    "15m": {"sec": 900,   "bars": 5000, "oanda": "M15", "td": "15min"},
    "1h":  {"sec": 3600,  "bars": 2000, "oanda": "H1",  "td": "1h"},
    "4h":  {"sec": 14400, "bars": 1000, "oanda": "H4",  "td": "4h"},
}

# ---- risk model (market-standard, volatility based) ----
# Stop distance = max(zone structure + buffer, ATR(14) x ATR_MULT, pair minimum).
ATR_LEN = 14
ATR_MULT = 1.5
RR_STEPS = (1.0, 2.0, 3.0)          # TP1/TP2/TP3 at 1:1, 1:2, 1:3
ZONE_BUFFER_PIPS = 2.0              # small buffer beyond the zone edge
MIN_STOP_PIPS = {                   # volatility floor per pair, in pips
    "EURUSD": 10.0,
    "GBPUSD": 12.0,
    "USDJPY": 12.0,
    "AUDUSD": 10.0,
    "USDCAD": 10.0,
    "XAUUSD": 25.0,                 # pip = 0.1 -> 25 pips = $2.50
}
MAX_ZONES = 1000
ENGINE_V = 3  # bump when event keys/levels change -> silent re-bootstrap
STATE_FILE = "state.json"
MAX_MESSAGES_PER_RUN = 25

PROVIDER = os.environ.get("PROVIDER", "oanda").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
NOTIFY_OUTCOMES = os.environ.get("NOTIFY_OUTCOMES", "1") == "1"

UA = {"User-Agent": "hakim-fx-bot/1.0"}


# ------------------------------------------------------------------ providers

def _get_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_oanda(instrument, granularity, count, token):
    """Return list of {'t','o','h','l','c'} for CLOSED candles, ascending."""
    url = (
        "https://api-fxpractice.oanda.com/v3/instruments/%s/candles"
        "?count=%d&granularity=%s&price=M"
    ) % (instrument, count, granularity)
    data = _get_json(url, headers={"Authorization": "Bearer " + token, **UA})
    bars = []
    for c in data.get("candles", []):
        if not c.get("complete"):
            continue
        m = c["mid"]
        t = int(datetime.fromisoformat(
            c["time"].replace("Z", "+00:00")).timestamp())
        bars.append({"t": t, "o": float(m["o"]), "h": float(m["h"]),
                     "l": float(m["l"]), "c": float(m["c"])})
    bars.sort(key=lambda b: b["t"])
    return bars


def fetch_twelvedata(symbol, interval, count, apikey):
    """Return list of {'t','o','h','l','c'} for CLOSED candles, ascending (UTC)."""
    url = (
        "https://api.twelvedata.com/time_series?symbol=%s&interval=%s"
        "&outputsize=%d&timezone=UTC&apikey=%s"
    ) % (urllib.parse.quote(symbol), interval, count, apikey)
    data = _get_json(url)
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError("twelvedata error: %s" % str(data)[:300])
    bars = []
    for v in data["values"]:
        t = int(datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=timezone.utc).timestamp())
        bars.append({"t": t, "o": float(v["open"]), "h": float(v["high"]),
                     "l": float(v["low"]), "c": float(v["close"])})
    bars.sort(key=lambda b: b["t"])
    # drop the still-forming last bar
    sec = None
    for name, tf in TIMEFRAMES.items():
        if tf["td"] == interval:
            sec = tf["sec"]
    if bars and sec and bars[-1]["t"] + sec > int(time.time()):
        bars.pop()
    return bars


def fetch_bars(symbol_name, tf_name):
    tf = TIMEFRAMES[tf_name]
    sym = SYMBOLS[symbol_name]
    if PROVIDER == "twelvedata":
        key = os.environ.get("TWELVEDATA_KEY", "").strip()
        if not key:
            raise RuntimeError("TWELVEDATA_KEY is not set")
        return fetch_twelvedata(sym["td"], tf["td"], tf["bars"], key)
    token = os.environ.get("OANDA_TOKEN", "").strip()
    if not token:
        raise RuntimeError("OANDA_TOKEN is not set")
    return fetch_oanda(sym["oanda"], tf["oanda"], tf["bars"], token)


# ------------------------------------------------------------------ engine
# Faithful port of the Pine logic. Every bar index i below is the equivalent
# of Pine's bar_index over the fetched (closed) bars only.

def run_engine(bars, pip, min_stop):
    """Replay the indicator over bars. Return list of signal events:
    entry: {'key','time','kind'='entry','dir','entry','sl','tps'}
    outcome: kind in ('tp1','tp2','tp3','sl') with 'price'"""
    zones = []   # dicts, oldest first
    events = []
    buffer = ZONE_BUFFER_PIPS * pip

    # ATR(14) of true range, simple mean, value per bar index (None until warm)
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    atr = [None] * len(bars)
    for i in range(ATR_LEN, len(bars)):
        atr[i] = sum(trs[i - ATR_LEN + 1: i + 1]) / ATR_LEN

    def engulf_at(j):
        """(bull, bear) engulfing with engulfing bar j and engulfed bar j-1."""
        e, p = bars[j], bars[j - 1]
        bull = (p["c"] < p["o"] and e["c"] > e["o"]
                and e["o"] <= p["c"] and e["c"] >= p["o"])
        bear = (p["c"] > p["o"] and e["c"] < e["o"]
                and e["o"] >= p["c"] and e["c"] <= p["o"])
        return bull, bear

    for i in range(len(bars)):
        b = bars[i]

        # ---------------- A ZONE ----------------
        if i >= 1:
            bull, bear = engulf_at(i)
            if bull or bear:
                body = abs(b["c"] - b["o"])
                prev_body = abs(bars[i - 1]["c"] - bars[i - 1]["o"])
                if body >= prev_body * 2:  # isA
                    p = bars[i - 1]
                    zones.append({
                        "type": 1,  # 1=A 2=B 3=C
                        "dir": 1 if bull else -1,
                        "stage": 0,
                        "top": max(p["o"], p["c"]),
                        "bottom": min(p["o"], p["c"]),
                        "engulf_i": i,
                        "engulf_t": b["t"],
                        "fb": None, "sb": None, "rb": None,
                        "active": False, "closed": None,
                        "entry": None, "sl": None, "tp": None,
                        "entry_i": None, "entry_t": None,
                    })

        # ---------------- B or upgrade to C ----------------
        if i >= 3:
            b2, b3 = bars[i - 2], bars[i - 3]
            bl2, br2 = engulf_at(i - 2)
            bull_mom = bl2 and bars[i - 1]["c"] > bars[i - 1]["o"] and b["c"] > b["o"]
            bear_mom = br2 and bars[i - 1]["c"] < bars[i - 1]["o"] and b["c"] < b["o"]
            if bull_mom or bear_mom:
                mdir = 1 if bull_mom else -1
                engulf_body = abs(b2["c"] - b2["o"])
                engulf_prev = abs(b3["c"] - b3["o"])
                if engulf_body >= engulf_prev * 2:  # wasA -> upgrade to C
                    for z in zones:
                        if z["engulf_i"] == i - 2:
                            z["type"] = 3
                else:  # new B zone from the candle 3 back
                    zones.append({
                        "type": 2, "dir": mdir, "stage": 0,
                        "top": max(b3["o"], b3["c"]),
                        "bottom": min(b3["o"], b3["c"]),
                        "engulf_i": i - 2, "engulf_t": b2["t"],
                        "fb": None, "sb": None, "rb": None,
                        "active": False, "closed": None,
                        "entry": None, "sl": None, "tp": None,
                        "entry_i": None, "entry_t": None,
                    })

        # ---------------- STATE ENGINE ----------------
        touch = lambda z: b["h"] >= z["bottom"] and b["l"] <= z["top"]
        for z in zones:
            st = z["stage"]
            if z["dir"] == 1:
                if st == 0 and b["c"] < z["bottom"]:
                    z["stage"] = 1; z["fb"] = i
                elif st == 1 and i > z["fb"] and b["c"] > z["top"]:
                    z["stage"] = 2; z["sb"] = i
                elif st == 2 and i > z["sb"] + 1 and touch(z):
                    z["stage"] = 3; z["rb"] = i
                elif st == 3 and z["rb"] is not None and i >= z["rb"] + 2:
                    z["stage"] = 4
            else:
                if st == 0 and b["c"] > z["top"]:
                    z["stage"] = 1; z["fb"] = i
                elif st == 1 and i > z["fb"] and b["c"] < z["bottom"]:
                    z["stage"] = 2; z["sb"] = i
                elif st == 2 and i > z["sb"] + 1 and touch(z):
                    z["stage"] = 3; z["rb"] = i
                elif st == 3 and z["rb"] is not None and i >= z["rb"] + 2:
                    z["stage"] = 4

        # ---------------- TRADE ASSISTANT (entry) ----------------
        for z in zones:
            if i <= z["engulf_i"] or z["stage"] != 0:
                continue
            if z["active"] or not touch(z):
                continue
            a = atr[i] or 0.0
            structural = (z["top"] - z["bottom"]) + buffer
            sl_dist = max(structural, a * ATR_MULT, min_stop)
            if z["dir"] == 1:
                entry = z["top"]
                sl = entry - sl_dist
                tps = [entry + sl_dist * r for r in RR_STEPS]
            else:
                entry = z["bottom"]
                sl = entry + sl_dist
                tps = [entry - sl_dist * r for r in RR_STEPS]
            z.update(active=True, entry=entry, sl=sl, tps=tps,
                     hits=[], entry_i=i, entry_t=b["t"])
            events.append({
                "key": "e2:%d:%d" % (z["engulf_t"], z["dir"]),
                "time": b["t"], "kind": "entry", "dir": z["dir"],
                "entry": entry, "sl": sl, "tps": tps,
            })

        # ---------------- OUTCOME: TP1/TP2/TP3 then SL; SL wins same-bar ties
        for z in zones:
            if not (z["active"] and z["closed"] is None
                    and z["entry_i"] is not None and i > z["entry_i"]):
                continue
            if z["dir"] == 1:
                hit_sl = b["l"] <= z["sl"]
                tp_hit = [b["h"] >= tp for tp in z["tps"]]
            else:
                hit_sl = b["h"] >= z["sl"]
                tp_hit = [b["l"] <= tp for tp in z["tps"]]
            ekey = "e2:%d:%d" % (z["engulf_t"], z["dir"])
            if hit_sl:
                z["closed"] = "sl"
                events.append({"key": "x2:%d:%d" % (z["engulf_t"], z["dir"]),
                               "time": b["t"], "kind": "sl", "dir": z["dir"],
                               "entry": z["entry"], "price": z["sl"],
                               "entry_key": ekey})
                continue
            for n, hit in enumerate(tp_hit, start=1):
                if hit and n not in z["hits"]:
                    z["hits"].append(n)
                    events.append({
                        "key": "t2:%d:%d:%d" % (z["engulf_t"], z["dir"], n),
                        "time": b["t"], "kind": "tp%d" % n, "dir": z["dir"],
                        "entry": z["entry"], "price": z["tps"][n - 1],
                        "entry_key": ekey,
                    })
            if len(z["hits"]) == len(z["tps"]):
                z["closed"] = "tp3"

        # ---------------- zone cap (mirror Pine's drawing limit) -------------
        if len(zones) > MAX_ZONES:
            for pred in (lambda z: z["stage"] == 4,
                         lambda z: z["closed"] is not None,
                         lambda z: z["stage"] != 0,
                         lambda z: True):
                idx = next((k for k, z in enumerate(zones) if pred(z)), None)
                if idx is not None:
                    zones.pop(idx)
                    break

    return events


# ------------------------------------------------------------------ telegram

def send_telegram(text, reply_to=None):
    """Send a message; return its Telegram message_id, or None on failure."""
    if DRY_RUN:
        print("[DRY RUN telegram%s] %s"
              % (" (reply to %s)" % reply_to if reply_to else "",
                 text.replace("\n", " | ")))
        return -1  # fake id so callers treat it as sent
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    relay = os.environ.get("TELEGRAM_RELAY", "").strip()
    if not chat or (not relay and not token):
        print("ERROR: TELEGRAM_RELAY or TELEGRAM_TOKEN + TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return None
    # relay: a server holding the bot token, used because api.telegram.org
    # throttle-lists datacenter egress IPs; it forwards the body verbatim
    url = relay if relay else "https://api.telegram.org/bot%s/sendMessage" % token
    body = {"chat_id": chat, "text": text}
    if reply_to:
        body["reply_to_message_id"] = reply_to
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json", **UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
            return data.get("result", {}).get("message_id")
    except urllib.error.HTTPError as e:
        print("telegram error:", e.code, e.read()[:200], file=sys.stderr)
        return None
    except Exception as e:
        # hang/timeout/refused (transient) -> log, skip this run, retry next run
        print("telegram send failed (transient):", repr(e), file=sys.stderr)
        return None


def fmt_price(x, pip):
    decimals = max(0, round(-__import__("math").log10(pip)) + 1)
    return ("%.*f" % (decimals, x)).rstrip("0").rstrip(".") if "." in ("%.*f" % (decimals, x)) else ("%.*f" % (decimals, x))


# --- template variants: rotated deterministically per signal (stable per key)
ENTRY_HEADS = [
    "\U0001F6A8 توصية %s | %s @ %s",
    "⚡ توصية %s\n%s @ %s",
    "\U0001F525 توصية %s يا صنايعية\n%s @ %s",
]
ENTRY_BUY = ["شراء الآن 🟢🚀", "شراء الآن 🟢💪", "شراء الآن 🟢📈"]
ENTRY_SELL = ["بيع مباشر الآن 🔴🔥", "بيع مباشر الآن 🔴📉", "بيع مباشر الآن 🔴⚡"]
ENTRY_BLOCKS = [
    ["\U0001F3AF TP1: %s 👑", "\U0001F3AF TP2: %s 👑", "\U0001F3AF TP3: %s 👑", "\U0001F6D1 SL: %s ☠️"],
    ["1️⃣ الهدف الأول: %s 👑", "2️⃣ الهدف الثاني: %s 👑", "3️⃣ الهدف الثالث: %s 👑", "\U0001F6D1 الستوب: %s ☠️"],
    ["\U0001F947 الهدف 1: %s", "\U0001F948 الهدف 2: %s", "\U0001F949 الهدف 3: %s", "☠️ الستوب: %s"],
]
TF_LINES = ["⏱ الفريم: %s", "الفريم ⏱: %s", "🕐 الفريم: %s"]
TP_TEXTS = {
    1: ["👑 الهدف الأول اتحقق ✅🎉 (%s)\nربح +%s نقطة 💰",
        "\U0001F3AF TP1 خبط الهدف ✅🔥 (%s)\n+%s نقطة مكسب 💵"],
    2: ["👑👑 الهدف الثاني اتحقق ✅🎉🎉🔥 (%s)\nربح +%s نقطة 💰💰",
        "🔥 TP2 تاني هدف في الجيب ✅💪 (%s)\n+%s نقطة مكسب 💵💵"],
    3: ["👑👑👑 الهدف الثالث اتحقق ✅🏆🎉🎉🎉 (%s)\nربح +%s نقطة 🏆💰",
        "\U0001F3C6 TP3 الهدف الكامل اتحقق ✅🎉🎉 (%s)\n+%s نقطة مكسب كامل 💰\U0001F3C6"],
}
SL_TEXTS = [
    "\U0001F6D1 ضربت الستوب، ونستنى الفرصة الجاية 🤍 (%s)\n-%s نقطة",
    "☠️ الستوب ضربت، سلامتها من غيرها 🤲 (%s)\nخسارة -%s نقطة",
    "\U0001F6D1 خرجنا على الستوب، والسوق فيه فرص كل يوم 🤍 (%s)\n-%s نقطة",
]


def _variant(seed_key, options):
    return options[zlib.crc32(seed_key.encode("utf-8")) % len(options)]


def _fmt_pips(pips):
    r = round(pips, 1)
    return ("%g" % r)


def build_message(sym, tf, ev):
    pip = SYMBOLS[sym]["pip"]
    if ev["kind"] == "entry":
        heads = ENTRY_BUY if ev["dir"] == 1 else ENTRY_SELL
        head = _variant(ev["key"] + "h", heads)
        tpl = _variant(ev["key"] + "t", ENTRY_HEADS)
        block = _variant(ev["key"] + "b", ENTRY_BLOCKS)
        lines = [
            tpl % (head, sym, fmt_price(ev["entry"], pip)),
            "",
            block[0] % fmt_price(ev["tps"][0], pip),
            block[1] % fmt_price(ev["tps"][1], pip),
            block[2] % fmt_price(ev["tps"][2], pip),
            block[3] % fmt_price(ev["sl"], pip),
        ]
        return "\n".join(lines)
    pips = (ev["price"] - ev["entry"]) * ev["dir"] / pip
    if ev["kind"].startswith("tp"):
        n = int(ev["kind"][2])
        tpl = _variant(ev["key"] + "o", TP_TEXTS[n])
        return tpl % (fmt_price(ev["price"], pip), _fmt_pips(abs(pips)))
    tpl = _variant(ev["key"] + "o", SL_TEXTS)
    return tpl % (fmt_price(ev["price"], pip), _fmt_pips(abs(pips)))



# ------------------------------------------------------------------ gold scalp
# Gold scalp engine v2 (same rules as https://hakim-gold.vercel.app), on closed
# 5m PAXG bars from Binance (free, no key). Patterns are basis-insensitive;
# posted levels are offsets from the LIVE XAUUSD price at send time (Twelve
# Data quote, ~1 credit per entry), never raw PAXG prices (PAXG trades ~$17
# below spot). Touch detection reuses the stored constant basis so PAXG bars
# can be checked against XAU-referenced levels.
SCALP_KEY = "XAUUSD_scalp"
GOLD_LIVE = False  # PAUSED by Karim 24 Nov 15:03 EET: gold scalp evaluates in
# dry-run only (recorded, never posted, no positions) until a proven strategy
# exists. Re-enable only on parent's instruction. ML system is held the same
# way (ML_LIVE below).
SCALP_V = 1
SCALP_STALE_SEC = 1800          # "buy now" wording: never post a scalp >30 min old
BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines?symbol=PAXGUSDT&interval=5m"

BE_TEXTS = [
    "\U0001F6E1\uFE0F الستوب كان على الدخول وخرجنا على التعادل 🤍 (%s)\n0 نقطة",
    "\U0001F6E1\uFE0F خرجنا على التعادل بعد ما الستوب اتحرك للدخول 🤲 (%s)\n0 نقطة",
]
SCALP_TP1_SUFFIX = "\n\U0001F6E1\uFE0F الستوب دلوقتي على الدخول"


def xau_open_ts(ts):
    """Spot gold (XAUUSD) trades Sun 23:00 UTC to Fri 22:00 UTC. PAXG ticks
    24/7; outside these hours its moves are thin-market noise TradingView
    XAUUSD would never show, so no signals or touch checks then."""
    t = time.gmtime(ts)
    if t.tm_wday == 5:
        return False                      # Saturday
    if t.tm_wday == 6 and t.tm_hour < 23:
        return False                      # Sunday before 23:00 UTC
    if t.tm_wday == 4 and t.tm_hour >= 22:
        return False                      # Friday from 22:00 UTC
    return True


def xau_open():
    return xau_open_ts(time.time())


def fetch_paxg_5m(limit=1000):
    """Closed weekday 5m PAXG bars, oldest first: [{t,o,h,l,c}, ...].
    Always 3000 bars like the page engine: the 576-bar volatility-ratio
    window plus weekend gaps need the depth."""
    out = []
    end = None
    for _ in range(3):
        url = BINANCE_KLINES + "&limit=%d" % limit
        if end:
            url += "&endTime=%d" % end
        k = _get_json(url)
        now_ms = time.time() * 1000
        batch = [{"t": x[0] / 1000.0, "o": float(x[1]), "h": float(x[2]),
                  "l": float(x[3]), "c": float(x[4])} for x in k if x[6] < now_ms]
        out = batch + out
        if not k or len(k) < limit:  # raw page size; the open bar is filtered above
            break
        end = int(batch[0]["t"] * 1000) - 1
    # keep only bars from spot-gold market hours: PAXG weekend bars are
    # thin-market artifacts XAUUSD never trades at
    return [b for b in out if xau_open_ts(b["t"])]


def fetch_paxg_ticker():
    """Live PAXG price (Binance, free, real-time) for intra-bar touch checks."""
    data = _get_json("https://data-api.binance.vision/api/v3/ticker/price?symbol=PAXGUSDT")
    return float(data["price"])


def fetch_paxg_1m(limit=8):
    """Recent 1m PAXG bars INCLUDING the still-forming one: intrabar high/low
    touch evidence for the last few minutes, newest last."""
    k = _get_json("https://data-api.binance.vision/api/v3/klines?symbol=PAXGUSDT&interval=1m&limit=%d" % limit)
    return [{"t": x[0] / 1000.0, "h": float(x[2]), "l": float(x[3])} for x in k]


def _ema(a, n):
    k = 2.0 / (n + 1)
    o = []
    p = a[0]
    for i, x in enumerate(a):
        p = x * k + p * (1 - k) if i else a[0]
        o.append(p)
    return o


def _atr_sma(b, n=14):
    tr = []
    for i, x in enumerate(b):
        if i:
            pc = b[i - 1]["c"]
            tr.append(max(x["h"] - x["l"], abs(x["h"] - pc), abs(x["l"] - pc)))
        else:
            tr.append(x["h"] - x["l"])
    return [sum(tr[i - n + 1:i + 1]) / n if i >= n - 1 else 0.0
            for i in range(len(tr))]


def _wilder(a, n):
    k = 1.0 / n
    o = []
    p = a[0]
    for i, x in enumerate(a):
        p = x * k + p * (1 - k) if i else a[0]
        o.append(p)
    return o


def _rsi2(c):
    d = [c[i] - c[i - 1] if i else 0.0 for i in range(len(c))]
    u = _wilder([max(x, 0.0) for x in d], 2)
    w = _wilder([max(-x, 0.0) for x in d], 2)
    return [100.0 - 100.0 / (1.0 + u[i] / (w[i] + 1e-12)) for i in range(len(c))]


def scalp_entries(bars):
    """All entry signals in the window, both rules, chronological.
    Returns [{key, rule, dir, bar_i, sig_time, s}]."""
    c = [x["c"] for x in bars]
    e200 = _ema(c, 200)
    a = _atr_sma(bars)
    r = _rsi2(c)
    am576 = [sum(a[i - 575:i + 1]) / 576.0 if i >= 576 + 13 else float("nan")
             for i in range(len(a))]
    out = []
    for kind, mult in (("sweep", 0.75), ("rsi2", 1.0)):
        last = {}
        N = 12
        for i in range(max(200, N + 1, 600), len(bars)):
            x = bars[i]
            if not a[i] > 0:
                continue
            vr = a[i] / am576[i]
            if kind == "sweep" and not vr < 0.8:
                continue
            if kind == "rsi2" and not vr < 1.0:
                continue
            d = 0
            if kind == "sweep":
                hh = max(y["h"] for y in bars[i - N:i])
                ll = min(y["l"] for y in bars[i - N:i])
                if x["h"] > hh and c[i] < hh and c[i] < x["o"]:
                    d = -1
                if x["l"] < ll and c[i] > ll and c[i] > x["o"]:
                    d = 1
            else:
                if r[i] < 20 and c[i] > e200[i] and c[i] > x["o"]:
                    d = 1
                if r[i] > 80 and c[i] < e200[i] and c[i] < x["o"]:
                    d = -1
            if not d:
                continue
            if i - last.get(d, -99) < 3:
                continue
            last[d] = i
            out.append({
                "key": "sc:%s:%d:%d" % (kind, int(x["t"]), d),
                "rule": kind, "dir": d, "bar_i": i,
                "sig_time": x["t"] + 300, "s": a[i] * mult,
            })
    out.sort(key=lambda e: (e["sig_time"], e["rule"]))
    return out


def resolve_scalp_position(pos, bars):
    """Walk closed PAXG bars against the position's basis-locked levels.
    Same resolution rules as the page engine: SL beats TP1 on the same bar;
    after TP1 the stop moves to entry (checked from the next bar); TP<=BE
    same-bar counts the TP. Returns [{kind, bar_i}] chronological."""
    d = pos["dir"]
    sl = pos["sl_p"]; entry = pos["entry_p"]; tps = pos["tps_p"]
    INF = float("inf")
    tsl = t1 = t2 = t3 = INF
    start = pos["sig_bar_i"] + 1
    hit_l = lambda y, p: y["h"] >= p if d > 0 else y["l"] <= p
    hit_s = lambda y: y["l"] <= sl if d > 0 else y["h"] >= sl
    for j in range(start, len(bars)):
        y = bars[j]
        if tsl == INF and hit_s(y):
            tsl = j
        if t1 == INF and hit_l(y, tps[0]):
            t1 = j
        if t2 == INF and hit_l(y, tps[1]):
            t2 = j
        if t3 == INF and hit_l(y, tps[2]):
            t3 = j
    evs = []
    if tsl < INF and tsl <= t1:
        evs.append({"kind": "sl", "bar_i": tsl})
    elif t1 < INF:
        tbe = INF
        for j in range(int(t1) + 1, len(bars)):
            y = bars[j]
            if (y["l"] <= entry) if d > 0 else (y["h"] >= entry):
                tbe = j
                break
        evs.append({"kind": "tp1", "bar_i": t1})
        if t2 <= tbe and t2 < INF:
            evs.append({"kind": "tp2", "bar_i": t2})
        if t3 <= tbe and t3 < INF:
            evs.append({"kind": "tp3", "bar_i": t3})
        if tbe < INF and tbe < t3:
            evs.append({"kind": "be", "bar_i": tbe})
    order = {"tp1": 1, "tp2": 2, "tp3": 3, "be": 4, "sl": 5}
    evs.sort(key=lambda e: (e["bar_i"], order[e["kind"]]))
    return evs


def td_quote_xau():
    """Live XAU/USD price from Twelve Data (~1 credit), or None."""
    key = os.environ.get("TWELVEDATA_KEY", "").strip()
    if not key:
        return None
    try:
        data = _get_json("https://api.twelvedata.com/quote?symbol=XAU/USD&apikey=" + key)
        if isinstance(data, dict) and data.get("status") == "error":
            print("TD quote error:", data.get("message"), file=sys.stderr)
            return None
        return float(data["close"])
    except Exception as e:
        print("TD quote failed:", repr(e), file=sys.stderr)
        return None


def _remote_scalp_unchanged(state, pkey, kind):
    """Race guard for parallel invocations (regular 1-min run + touch hint):
    re-read remote state; True = this event still needs sending."""
    if not (GH_PAT and GH_REPO):
        return True
    try:
        _, remote = _gh_state_get()
        rst = (remote or {}).get(SCALP_KEY) or {}
        ekey = pkey + ":" + kind
        if ekey in rst.get("signaled", []):
            return False
        pos = (rst.get("positions") or {}).get(pkey)
        if pos is None:
            # entry not posted yet remotely, or already closed: only allow
            # closing kinds when the position is still open there
            return False
        if kind.startswith("tp") and pos.get("tps_hit", 0) >= int(kind[2]):
            return False
        return True
    except Exception as e:
        print("remote recheck failed; deferring:", repr(e), file=sys.stderr)
        return False


def build_scalp_entry(dir_, entry, tps, sl, key):
    pip = SYMBOLS["XAUUSD"]["pip"]
    heads = ENTRY_BUY if dir_ == 1 else ENTRY_SELL
    head = _variant(key + "h", heads)
    tpl = _variant(key + "t", ENTRY_HEADS)
    block = _variant(key + "b", ENTRY_BLOCKS)
    lines = [
        tpl % (head, "XAUUSD", fmt_price(entry, pip)),
        "",
        block[0] % fmt_price(tps[0], pip),
        block[1] % fmt_price(tps[1], pip),
        block[2] % fmt_price(tps[2], pip),
        block[3] % fmt_price(sl, pip),
    ]
    return "\n".join(lines)


def build_scalp_outcome(pos, ev):
    pip = SYMBOLS["XAUUSD"]["pip"]
    key = pos["entry_key"]
    kind = ev["kind"]
    if kind == "sl":
        price = pos["sl_xau"]
    elif kind == "be":
        price = pos["entry_xau"]
    else:
        price = pos["tps_xau"][int(kind[2]) - 1]
    if kind in ("tp1", "tp2", "tp3"):
        n = int(kind[2])
        tpl = _variant(key + "o", TP_TEXTS[n])
        pips = abs(price - pos["entry_xau"]) / pip
        msg = tpl % (fmt_price(price, pip), _fmt_pips(pips))
        if n == 1:
            msg += SCALP_TP1_SUFFIX
        return msg
    if kind == "be":
        return _variant(key + "o", BE_TEXTS) % fmt_price(price, pip)
    tpl = _variant(key + "o", SL_TEXTS)
    pips = abs(price - pos["entry_xau"]) / pip
    return tpl % (fmt_price(price, pip), _fmt_pips(pips))


def run_scalp(state, now, sent, telegram_down):
    """Gold scalp on 5m PAXG; returns (sent, telegram_down)."""
    st = state.setdefault(SCALP_KEY, {"scalp_v": 0, "last_time": 0,
                                      "signaled": [], "msg_ids": {},
                                      "positions": {}})
    bootstrap = st.get("scalp_v") != SCALP_V or not st.get("last_time")
    try:
        bars = fetch_paxg_5m()
    except Exception as e:
        print("PAXG fetch failed:", repr(e), file=sys.stderr)
        return sent, telegram_down
    if len(bars) < 1500:
        print("too few PAXG bars:", len(bars))
        return sent, telegram_down
    entries = scalp_entries(bars)
    if bootstrap:
        st["signaled"] = [e["key"] for e in entries][-1500:]
        st["positions"] = {}
        st["last_time"] = bars[-1]["t"]
        st["scalp_v"] = SCALP_V
        print("bootstrap %s (scalp v%d): %d bars, %d known entries"
              % (SCALP_KEY, SCALP_V, len(bars), len(entries)))
        return sent, telegram_down
    # locate each stored position's signal bar in this window
    t_index = {int(b["t"]): i for i, b in enumerate(bars)}
    if not GOLD_LIVE:
        # dry-run: record what would have posted, keep dedupe bookkeeping so
        # re-enable is clean, but send nothing and open no positions
        dry = st.setdefault("dry_scalp", [])
        groups = {}
        for e in entries:
            groups.setdefault((int(bars[e["bar_i"]]["t"]), e["dir"]), []).append(e)
        for gkey in sorted(groups):
            evs = groups[gkey]
            rep = next((e for e in evs if e["rule"] == "sweep"), evs[0])
            if any(e["key"] in st["signaled"] for e in evs):
                continue
            for e in evs:
                st["signaled"].append(e["key"])
            if now - rep["sig_time"] > SCALP_STALE_SEC:
                continue
            dry.append({"key": rep["key"], "time": rep["sig_time"],
                        "dir": rep["dir"], "rule": rep["rule"],
                        "dist": round(rep["s"], 3)})
            print("scalp DRY-RUN signal (gold paused, not sent):", rep["key"])
        st["dry_scalp"] = dry[-200:]
        st["signaled"] = st["signaled"][-1500:]
        st["last_time"] = bars[-1]["t"]
        if st["positions"]:
            # should not happen: positions were closed before the pause
            print("WARNING: %d gold positions still open while paused"
                  % len(st["positions"]), file=sys.stderr)
        return sent, telegram_down
    # --- new entries ---
    groups = {}
    for e in entries:
        groups.setdefault((int(bars[e["bar_i"]]["t"]), e["dir"]), []).append(e)
    quote_dead = False
    market_open = xau_open()
    for gkey in sorted(groups):
        evs = groups[gkey]
        rep = next((e for e in evs if e["rule"] == "sweep"), evs[0])
        if any(e["key"] in st["signaled"] for e in evs):
            continue
        if not market_open:
            # spot gold closed (Fri 22:00-Sun 23:00 UTC): never post entries
            continue
        if sent >= MAX_MESSAGES_PER_RUN or telegram_down or quote_dead:
            break
        if now - rep["sig_time"] > SCALP_STALE_SEC:
            for e in evs:
                st["signaled"].append(e["key"])
            continue
        live = td_quote_xau()
        if live is None:
            quote_dead = True
            print("XAU quote unavailable; deferring scalp entries", file=sys.stderr)
            continue
        d = rep["dir"]; s_ = rep["s"]
        basis = live - bars[rep["bar_i"]]["c"]
        entry = live
        sl = entry - d * s_
        tps = [entry + d * 2 * s_, entry + d * 3 * s_, entry + d * 4 * s_]
        mid = send_telegram(build_scalp_entry(d, entry, tps, sl, rep["key"]))
        if not mid:
            telegram_down = True
            continue
        for e in evs:
            st["signaled"].append(e["key"])
        st["msg_ids"][rep["key"]] = mid
        st["positions"][rep["key"]] = {
            "entry_key": rep["key"], "dir": d, "rule": rep["rule"],
            "s": s_, "entry_xau": entry, "sl_xau": sl, "tps_xau": tps,
            "entry_p": entry - basis, "sl_p": sl - basis,
            "tps_p": [tp - basis for tp in tps],
            "sig_bar_t": int(bars[rep["bar_i"]]["t"]),
            "sig_bar_i": rep["bar_i"],
            "tps_hit": 0,
        }
        sent += 1
        time.sleep(0.3)
    # --- outcomes for open positions ---
    for pkey in list(st["positions"].keys()):
        pos = st["positions"][pkey]
        i0 = t_index.get(pos["sig_bar_t"])
        if i0 is None:
            # signal bar aged out of the fetch window; stop tracking
            print("scalp position aged out of window:", pkey)
            del st["positions"][pkey]
            continue
        pos["sig_bar_i"] = i0
        for ev in resolve_scalp_position(pos, bars):
            ekey = pkey + ":" + ev["kind"]
            if ekey in st["signaled"]:
                continue
            if sent >= MAX_MESSAGES_PER_RUN or telegram_down:
                break
            reply_to = st["msg_ids"].get(pkey)
            mid = send_telegram(build_scalp_outcome(pos, ev), reply_to=reply_to)
            if not mid:
                telegram_down = True
                break
            st["signaled"].append(ekey)
            if ev["kind"].startswith("tp"):
                pos["tps_hit"] = max(pos.get("tps_hit", 0), int(ev["kind"][2]))
            sent += 1
            time.sleep(0.3)
            if ev["kind"] in ("sl", "be", "tp3"):
                del st["positions"][pkey]
                break
    # --- live intra-bar touch check: walk recent 1m bars (incl. the forming
    # one) oldest-first, then the live tick; stop before TP within a bar ---
    if st["positions"] and xau_open():
        try:
            probe = fetch_paxg_1m(8)
            tick = fetch_paxg_ticker()
            probe.append({"t": time.time(), "h": tick, "l": tick})
        except Exception as e:
            probe = []
            print("PAXG live probe failed (non-fatal):", repr(e), file=sys.stderr)
        for pkey in list(st["positions"].keys()):
            pos = st["positions"][pkey]
            d = pos["dir"]
            for yb in probe:
                n_tp = pos.get("tps_hit", 0)
                stop = pos["sl_p"] if n_tp == 0 else pos["entry_p"]
                hit_stop = yb["l"] <= stop if d > 0 else yb["h"] >= stop
                ev = None
                if hit_stop:
                    kind = "sl" if n_tp == 0 else "be"
                    if pkey + ":" + kind not in st["signaled"]:
                        ev = {"kind": kind, "bar_i": None}
                elif n_tp < 3:
                    tp = pos["tps_p"][n_tp]
                    hit_tp = yb["h"] >= tp if d > 0 else yb["l"] <= tp
                    kind = "tp%d" % (n_tp + 1)
                    if hit_tp and pkey + ":" + kind not in st["signaled"]:
                        ev = {"kind": kind, "bar_i": None}
                if ev is None:
                    continue
                if sent >= MAX_MESSAGES_PER_RUN or telegram_down:
                    break
                if not _remote_scalp_unchanged(state, pkey, ev["kind"]):
                    # a concurrent run already handled it
                    print("skip %s:%s - handled concurrently" % (pkey, ev["kind"]))
                    if pkey + ":" + ev["kind"] in st["signaled"]:
                        pass
                    continue
                reply_to = st["msg_ids"].get(pkey)
                mid = send_telegram(build_scalp_outcome(pos, ev), reply_to=reply_to)
                if not mid:
                    telegram_down = True
                    break
                st["signaled"].append(pkey + ":" + ev["kind"])
                if ev["kind"].startswith("tp"):
                    pos["tps_hit"] = max(n_tp, int(ev["kind"][2]))
                sent += 1
                time.sleep(0.3)
                if ev["kind"] in ("sl", "be", "tp3"):
                    del st["positions"][pkey]
                    break
    st["signaled"] = st["signaled"][-1500:]
    ids = st["msg_ids"]
    st["msg_ids"] = dict(list(ids.items())[-200:])
    print("scalp: %d bars, %d entries in window, %d open positions, sent so far %d"
          % (len(bars), len(entries), len(st["positions"]), sent))
    return sent, telegram_down


# ------------------------------------------------------------------ ML-ranked gold
# ML-ranked multi-rule gold system (research package of 24 Nov, approved by
# Karim for the channel). Six rule votes + 16 features -> frozen ridge score;
# a signal fires when score > ML_THR (one per bar, no consecutive bars).
# Thresholds and weights are frozen by the research - do not tune.
# Geometry: SL = 1.5*ATR(14), TP1/2/3 = 2x/3x/4x the SL distance; the shared
# position tracker moves SL to entry after TP1, like the other gold signals.
ML_KEY = "XAUUSD_ml"
ML_LIVE = False  # HELD by parent 24 Nov 14:13 EET: dry-run only (evaluate +
# record every signal, never send) until revalidated on real XAUUSD data.
ML_W = [-0.004795361647470501, -0.006931398440520428, -0.00814816486513296, -0.011991680176664138, -0.025593175199036068, -0.0312814957322029, 0.011097220773312867, 0.049259037989981116, 0.005855442604092558, 0.009182156240966148, 0.013608390281354254, -0.0013089628711275949, -0.0071497412161657045, 0.016460904622307006, -0.004638415863337652, 0.013888862390991122, 0.01691248758555308, -0.009212230324969536, 0.0010324959888403731, 0.007083511794288525, 0.009676707989731716, 0.00015151266155998916]
ML_MU = [-0.3874469449515239, -0.4689251123252492, -0.47291956920334227, -0.4131589991939655, -0.21745905774264931, -0.17835113678758882, 2.8748773841928283, 5.254585920771126, 0.9313663159509391, 0.9773738174158573, 0.18804528268597603, -0.3238014244960126, -0.07357367655162507, 0.028524279985362656, 0.009202192645398167, 1.4253360559752943, 0.05422577239131433, 0.25863287885047975, 0.008474498462103476, 0.04469540467336914, 0.10865353716200707, 0.7137951613643667]
ML_SD = [0.7592704528855888, 0.9451281139834996, 1.1358379123325981, 1.534109962534404, 2.078459237332209, 1.2161657688710972, 4.293466346130285, 9.957192062754034, 0.4414267190454469, 0.790324949284703, 0.3319663047182797, 0.5276914475228933, 0.3098760471825951, 0.7168416962720339, 0.6965914659358765, 1.4790278451582675, 0.22646266458903755, 0.43788344762465303, 0.09166614162961118, 0.20663428052434085, 0.31120402736245256, 0.45198631603302175]
ML_THR = 0.17741965918397018


def _sign(x):
    return (x > 0) - (x < 0)


def fetch_paxg_5m_ml(pages=5, limit=1000):
    """~5000 closed Mon-Fri 5m PAXG bars incl. volume + taker-buy volume,
    oldest first. The ML model was fit on this exact window (the reference
    engine's wk filter: Mon-Fri UTC bars only)."""
    out = []
    end = None
    for _ in range(pages):
        url = BINANCE_KLINES + "&limit=%d" % limit
        if end:
            url += "&endTime=%d" % end
        k = _get_json(url)
        now_ms = time.time() * 1000
        batch = [{"t": x[0] / 1000.0, "o": float(x[1]), "h": float(x[2]),
                  "l": float(x[3]), "c": float(x[4]), "v": float(x[5]),
                  "tb": float(x[9])} for x in k if x[6] < now_ms]
        out = batch + out
        if not k or len(k) < limit:
            break
        end = int(batch[0]["t"] * 1000) - 1
    return [b for b in out if time.gmtime(b["t"]).tm_wday <= 4]


def ml_signals(bars):
    """Python port of engine.js mlSignals (signal side only; SL/TP outcomes
    are tracked by the live position machinery, not replayed here).
    Returns [{i, d, score, time, dist}] for bars passing the threshold."""
    n = len(bars)
    c = [x["c"] for x in bars]
    a = _atr_sma(bars)
    e20 = _ema(c, 20)
    e200 = _ema(c, 200)
    e600 = _ema(c, 600)
    e2400 = _ema(c, 2400)
    r2 = _rsi2(c)
    am = [sum(a[i - 575:i + 1]) / 576.0 if i >= 576 + 13 else float("nan")
          for i in range(n)]
    hi12 = [max(y["h"] for y in bars[i - 12:i]) if i >= 12 else float("nan")
            for i in range(n)]
    lo12 = [min(y["l"] for y in bars[i - 12:i]) if i >= 12 else float("nan")
            for i in range(n)]
    cand = []
    for i in range(2400, n):
        x = bars[i]
        if not a[i] > 0:
            continue
        t = time.gmtime(x["t"])
        hr = t.tm_hour
        dw = (t.tm_wday + 1) % 7  # JS getUTCDay(): Sun=0
        if (dw == 5 and hr >= 21) or (dw == 1 and hr < 1):
            continue
        vr = a[i] / am[i]
        rg = (x["h"] - x["l"]) / a[i]
        loc = (x["c"] - x["l"]) / (x["h"] - x["l"] + 1e-9)
        tbr = x["tb"] / (x["v"] + 1e-9)
        best = None
        for d in (1, -1):
            vt = [0] * 6
            if d > 0:
                vt[0] = 1 if (x["l"] < lo12[i] and x["c"] > lo12[i]
                              and vr < 0.8) else 0
                cond1 = r2[i] < 20 and x["c"] > e200[i]
            else:
                vt[0] = 1 if (x["h"] > hi12[i] and x["c"] < hi12[i]
                              and vr < 0.8) else 0
                cond1 = r2[i] > 80 and x["c"] < e200[i]
            vt[1] = 1 if (cond1 and vr < 1.0) else 0
            ex = rg > 2 and (loc <= 0.3 if d > 0 else loc >= 0.7)
            tra = (1 if _sign(x["c"] - e600[i]) == d else 0) + \
                  (1 if _sign(e600[i] - e2400[i]) == d else 0)
            vt[2] = 1 if (ex and vr >= 1 and 12 <= hr < 17 and tra >= 1) else 0
            vt[3] = 1 if (rg > 2.5 and (loc <= 0.3 if d > 0
                                        else loc >= 0.7)) else 0
            if d > 0:
                vt[4] = 1 if (x["c"] > e200[i] and x["c"] < e20[i]
                              and x["c"] > x["o"]
                              and c[i - 1] < bars[i - 1]["o"]) else 0
                vt[5] = 1 if (loc < 0.15 and x["c"] > e200[i]) else 0
            else:
                vt[4] = 1 if (x["c"] < e200[i] and x["c"] > e20[i]
                              and x["c"] < x["o"]
                              and c[i - 1] > bars[i - 1]["o"]) else 0
                vt[5] = 1 if (loc > 0.85 and x["c"] < e200[i]) else 0
            if not any(vt):
                continue
            f = [d * (x["c"] - c[i - k]) / a[i] for k in (1, 2, 3, 6, 12)]
            f += [d * (x["c"] - e20[i]) / a[i],
                  d * (x["c"] - e200[i]) / a[i],
                  d * (x["c"] - e600[i]) / a[i],
                  vr, rg, loc if d > 0 else 1 - loc,
                  (r2[i] - 50) * d / 50, (tbr - 0.5) * d,
                  math.sin(hr / 24 * 6.283), math.cos(hr / 24 * 6.283),
                  (x["c"] - lo12[i]) / a[i] if d > 0
                  else (hi12[i] - x["c"]) / a[i]]
            f += vt
            s = 0.0
            for q in range(len(f)):
                s += ((f[q] - ML_MU[q]) / ML_SD[q]) * ML_W[q]
            if best is None or s > best[0]:
                best = (s, d)
        if best is not None:
            cand.append((i, best[0], best[1]))
    out = []
    last_i = -9
    for i, s, d in cand:
        if not s > ML_THR:
            continue
        if i - last_i <= 1:
            continue
        last_i = i
        out.append({"i": i, "d": d, "score": s,
                    "time": bars[i]["t"] + 300, "dist": a[i] * 1.5})
    return out


def run_ml(state, now, sent, telegram_down):
    """Post ML-ranked gold signals from the latest closed 5m bar. Shares the
    scalp state (positions/msg_ids/signaled), so touch watching, threaded
    follow-ups and the weekend gate behave exactly like the other gold
    signals. Returns (sent, telegram_down)."""
    st = state.setdefault(ML_KEY, {"ml_v": 1, "last_bar": 0})
    if not xau_open():
        return sent, telegram_down
    if (int(now) // 300 - 1) * 300 <= st.get("last_bar", 0):
        return sent, telegram_down
    try:
        bars = fetch_paxg_5m_ml()
    except Exception as e:
        print("ML PAXG fetch failed:", repr(e), file=sys.stderr)
        return sent, telegram_down
    if len(bars) < 2500:
        print("too few ML bars:", len(bars))
        return sent, telegram_down
    if int(bars[-1]["t"]) <= st.get("last_bar", 0):
        return sent, telegram_down  # exchange lag; retried next run
    fresh = [s for s in ml_signals(bars) if s["time"] == bars[-1]["t"] + 300]
    if not fresh:
        st["last_bar"] = int(bars[-1]["t"])
        return sent, telegram_down
    s = fresh[0]
    bar_t = int(bars[s["i"]]["t"])
    key = "ml:%d:%d" % (bar_t, s["d"])
    sc = state.setdefault(SCALP_KEY, {"scalp_v": SCALP_V, "last_time": 0,
                                      "signaled": [], "msg_ids": {},
                                      "positions": {}})
    if not ML_LIVE:
        suffix = ":%d:%d" % (bar_t, s["d"])
        suppressed = (any(k.endswith(suffix) for k in sc["signaled"])
                      or any(p.get("dir") == -s["d"]
                             for p in sc["positions"].values()))
        dry = st.setdefault("dry", [])
        dry.append({"key": key, "time": bar_t + 300, "dir": s["d"],
                    "score": round(s["score"], 4),
                    "dist": round(s["dist"], 3),
                    "paxg_close": bars[s["i"]]["c"],
                    "would_suppress": suppressed})
        st["dry"] = dry[-200:]
        st["last_bar"] = bar_t
        print("ML DRY-RUN signal (not sent): %s score %.3f dist %.2f%s"
              % (key, s["score"], s["dist"],
                 " [would be suppressed]" if suppressed else ""))
        return sent, telegram_down
    if key in sc["signaled"]:
        st["last_bar"] = bar_t
        return sent, telegram_down
    if now - (bar_t + 300) > SCALP_STALE_SEC:
        st["last_bar"] = bar_t
        return sent, telegram_down
    # conflict policy vs the existing gold rules: never contradict an open
    # gold position, never double-post a bar the scalp engine already fired
    suffix = ":%d:%d" % (bar_t, s["d"])
    same_bar = any(k.endswith(suffix) for k in sc["signaled"])
    opposite = any(p.get("dir") == -s["d"] for p in sc["positions"].values())
    if same_bar or opposite:
        sc["signaled"].append(key)
        st["last_bar"] = bar_t
        print("ML signal suppressed (%s): %s"
              % ("same-bar" if same_bar else "opposite-open", key))
        return sent, telegram_down
    if sent >= MAX_MESSAGES_PER_RUN or telegram_down:
        return sent, telegram_down  # retried next run until stale
    live = td_quote_xau()
    if live is None:
        print("XAU quote unavailable; deferring ML signal", file=sys.stderr)
        return sent, telegram_down
    d = s["d"]
    dist = s["dist"]
    basis = live - bars[s["i"]]["c"]
    entry = live
    sl = entry - d * dist
    tps = [entry + d * 2 * dist, entry + d * 3 * dist, entry + d * 4 * dist]
    mid = send_telegram(build_scalp_entry(d, entry, tps, sl, key))
    if not mid:
        return sent, True
    sc["signaled"].append(key)
    sc["msg_ids"][key] = mid
    sc["positions"][key] = {
        "entry_key": key, "dir": d, "rule": "ml",
        "s": dist, "entry_xau": entry, "sl_xau": sl, "tps_xau": tps,
        "entry_p": entry - basis, "sl_p": sl - basis,
        "tps_p": [tp - basis for tp in tps],
        "sig_bar_t": bar_t,
        "sig_bar_i": s["i"],
        "tps_hit": 0,
    }
    st["last_bar"] = bar_t
    sent += 1
    time.sleep(0.3)
    print("ML signal posted: %s score %.3f mid %s" % (key, s["score"], mid))
    return sent, telegram_down


# ------------------------------------------------- 1m failed-breakout fade forward test
# Dry-run forward test (24 Nov, parent task; gold paused - nothing posts to
# Telegram). Two variants of the 1m failed-breakout fade on Yahoo GC=F 1m
# (free; delayed ~10 min on the free tier - prices are real, log timestamps
# lag by the feed delay). Mirrors fadeSignals in
# https://hakim-gold.vercel.app/engine.js. Deliberate deviations:
#  - the $4 D floor applies to BOTH variants per the task spec (the page
#    engine uses MIND=0 for variant A) - flagged to parent;
#  - the gap discard uses any per-bar gap > 600s inside the trade window
#    (page uses the aggregate window-span rule); in-session trades never
#    span the daily COMEX break, so only weekend-spanning trades differ.
FBF_KEY = "XAU_fbfade"
FBF_W = 120                 # time stop, bars (entry bar included)
FBF_MIND = 4.0              # D floor in USD, per task spec
FBF_SPREAD = 0.25           # USD per trade, for net R
FBF_VARIANTS = (("A", 2.0, 120, None), ("B", 1.5, 144, None),
                ("C", 1.3, 240, 8.0))  # (name, vol thr, N, fixed D or None)
FBF_RV = (2.0, 4.0, 6.0)    # R per third at TP1/2/3 (D / 0.5D)
GCF_CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
             "?interval=1m&range=5d")


def fetch_gcf_1m():
    """GC=F 1m bars: [{t,o,h,l,c,v}] ascending, null-close minutes dropped,
    null volume -> 0. Includes the latest (possibly forming) bar; callers
    treat a bar as closed only when t+60 <= now."""
    data = _get_json(GCF_CHART, timeout=25)
    r = data["chart"]["result"][0]
    ts = r["timestamp"]
    q = r["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if c is None or o is None or h is None or l is None:
            continue
        v = q["volume"][i]
        out.append({"t": float(t), "o": float(o), "h": float(h),
                    "l": float(l), "c": float(c),
                    "v": float(v) if v else 0.0})
    return out


def fbf_signals(bars, VT, N, fixed_D=None):
    """Python port of fadeSignals (signal side only). bars must be CLOSED.
    fixed_D overrides the ATR-based D (variant C: fixed $8 TP1 / $4 SL).
    Returns [{i, time, dir, D, SLd, atr}] for bars passing every filter."""
    n = len(bars)
    tr = []
    for i, x in enumerate(bars):
        if i:
            pc = bars[i - 1]["c"]
            tr.append(max(x["h"] - x["l"], abs(x["h"] - pc),
                          abs(x["l"] - pc)))
        else:
            tr.append(max(x["h"] - x["l"], abs(x["h"] - x["c"])))
    cs = [0.0]
    vs = [0.0]
    for i in range(n):
        cs.append(cs[i] + tr[i])
        vs.append(vs[i] + bars[i]["v"])
    raw = [0] * n
    for i in range(N, n):
        x = bars[i]
        t = time.gmtime(x["t"])
        hr = t.tm_hour
        dw = (t.tm_wday + 1) % 7  # JS getUTCDay(): Sun=0
        if dw < 1 or dw > 5 or hr < 7 or hr >= 20 or i < 47:
            continue
        atr = (cs[i + 1] - cs[i - 47]) / 48
        if not atr > 0:
            continue
        hh = -1e18
        ll = 1e18
        for k in range(i - N, i):
            if bars[k]["h"] > hh:
                hh = bars[k]["h"]
            if bars[k]["l"] < ll:
                ll = bars[k]["l"]
        raw[i] = 1 if x["c"] > hh else (-1 if x["c"] < ll else 0)
    out = []
    for i in range(1440, n):
        if not raw[i] or raw[i - 1]:
            continue
        v3 = (vs[i + 1] - vs[i - 2]) / 3
        vm = (vs[i + 1] - vs[i - 1439]) / 1440
        if not (vm > 0 and v3 / vm < VT):
            continue
        x = bars[i]
        atr = (cs[i + 1] - cs[i - 47]) / 48
        D = fixed_D if fixed_D else max(6 * atr, FBF_MIND)
        out.append({"i": i, "time": x["t"] + 60, "dir": -raw[i],
                    "D": D, "SLd": 0.5 * D, "atr": atr})
    return out


def _fbf_step(pos, y):
    """Advance one position by one closed bar (stop-first, then TPs, then the
    120-bar time stop). Returns the close reason or None."""
    if y["t"] - pos["last_t"] > 600:
        pos["gap"] = True
    pos["last_t"] = y["t"]
    pos["held"] += 1
    d = pos["dir"]
    lvl = pos["lvl"]
    if (y["l"] <= pos["sl"]) if d > 0 else (y["h"] >= pos["sl"]):
        return "sl"
    while lvl < 3 and ((y["h"] >= pos["tps"][lvl]) if d > 0
                       else (y["l"] <= pos["tps"][lvl])):
        pos["fin"][lvl] = FBF_RV[lvl]
        lvl += 1
    pos["lvl"] = lvl
    if lvl == 3:
        return "tp3"
    if pos["held"] >= FBF_W:
        return "timestop"
    return None


def _fbf_close(pos, reason, last_bar):
    """Finalize a position: fill remaining thirds, compute R, return the log
    entry (marked skipped=gap if the trade window had a >600s bar gap)."""
    lvl = pos["lvl"]
    fin = pos["fin"]
    d = pos["dir"]
    SLd = pos["SLd"]
    if reason == "sl":
        for q in range(lvl, 3):
            fin[q] = -1.0
    else:
        for q in range(3):
            if fin[q] is None:
                fin[q] = (last_bar["c"] - pos["entry"]) * d / SLd
    base = {"v": pos["v"], "key": pos["key"], "sig_t": pos["sig_t"],
            "dir": d, "entry_t": pos["e_t"], "entry": pos["entry"],
            "sl": pos["sl"], "tp1": pos["tps"][0], "tp2": pos["tps"][1],
            "tp3": pos["tps"][2], "atr48": round(pos["atr"], 3),
            "D": round(pos["D"], 3), "exit_t": last_bar["t"],
            "exit": reason, "tps": lvl, "held": pos["held"]}
    if pos.get("gap"):
        base["skipped"] = "gap"
        return base
    r = sum(fin) / 3.0
    base["r_gross"] = round(r, 4)
    base["r_net"] = round(r - FBF_SPREAD / SLd, 4)
    return base


def _fbf_walk(v, bars, now):
    """Advance and close open positions of one variant over closed bars."""
    for pos in list(v["positions"]):
        for y in bars:
            if y["t"] + 60 > now or y["t"] <= pos["last_t"]:
                continue
            done = _fbf_step(pos, y)
            if not done:
                continue
            entry = _fbf_close(pos, done, y)
            v["log"].append(entry)
            if not entry.get("skipped"):
                v["closed"] += 1
                v["sum_r"] = round(v["sum_r"] + entry["r_gross"], 4)
                v["sum_r_net"] = round(v["sum_r_net"] + entry["r_net"], 4)
                v["mean_r"] = round(v["sum_r"] / v["closed"], 4)
                v["mean_r_net"] = round(v["sum_r_net"] / v["closed"], 4)
            v["positions"].remove(pos)
            print("fbfade %s closed: %s %s r=%s net=%s%s"
                  % (pos["v"], pos["key"], done,
                     entry.get("r_gross"), entry.get("r_net"),
                     " [skipped: gap]" if entry.get("skipped") else ""))
            break


def run_fbfade(state, now):
    """Forward-test driver for both variants. Dry-run only: records signals,
    entries and outcomes into state[FBF_KEY]; never sends anything."""
    st = state.setdefault(FBF_KEY, {"started_at": now})
    try:
        bars = fetch_gcf_1m()
    except Exception as e:
        print("GC=F fetch failed (non-fatal):", repr(e), file=sys.stderr)
        return
    if len(bars) < 1600:
        print("too few GC=F bars:", len(bars))
        return
    closed = [b for b in bars if b["t"] + 60 <= now]
    by_t = {int(b["t"]): b for b in bars}
    for name, VT, N, fixed_D in FBF_VARIANTS:
        v = st.setdefault(name, {"signaled": [], "pending": {},
                                 "positions": [], "log": [], "closed": 0,
                                 "sum_r": 0.0, "sum_r_net": 0.0,
                                 "mean_r": None, "mean_r_net": None})
        try:
            sigs = fbf_signals(closed, VT, N, fixed_D)
        except Exception as e:
            print("fbfade %s scan failed: %s" % (name, repr(e)),
                  file=sys.stderr)
            continue
        if not st.get("inited_" + name):
            # bootstrap: history is not a forward test - mark seen, log none
            v["signaled"] = ["fb%s:%d:%d" % (name, s["time"] - 60, s["dir"])
                             for s in sigs][-500:]
            st["inited_" + name] = True
            print("fbfade %s bootstrap: %d historical signals marked"
                  % (name, len(v["signaled"])))
            continue
        # 1) open pendings whose entry bar now exists in the feed
        for key in list(v["pending"].keys()):
            p = v["pending"][key]
            eb = by_t.get(int(p["e_t"]))
            if eb is None:
                if now - p["e_t"] > 900:  # entry bar never showed: gap
                    v["log"].append({"v": name, "key": key,
                                     "sig_t": p["sig_t"], "dir": p["dir"],
                                     "skipped": "entry_gap"})
                    del v["pending"][key]
                continue
            d = p["dir"]
            ep = eb["o"]
            SLd = p["SLd"]
            v["positions"].append({
                "v": name, "key": key, "sig_t": p["sig_t"], "e_t": p["e_t"],
                "dir": d, "entry": ep, "sl": ep - d * SLd,
                "tps": [ep + d * p["D"], ep + d * 2 * p["D"],
                        ep + d * 3 * p["D"]],
                "D": p["D"], "SLd": SLd, "atr": p["atr"],
                "lvl": 0, "fin": [None, None, None],
                "held": 0, "last_t": p["e_t"] - 60, "gap": False})
            del v["pending"][key]
            print("fbfade %s entry: %s dir=%d @ %.2f D=%.2f"
                  % (name, key, d, ep, p["D"]))
        # 2) register new signals as pending
        for s in sigs:
            key = "fb%s:%d:%d" % (name, s["time"] - 60, s["dir"])
            if key in v["signaled"]:
                continue
            v["signaled"].append(key)
            v["pending"][key] = {"sig_t": s["time"], "e_t": s["time"],
                                 "dir": s["dir"], "D": s["D"],
                                 "SLd": s["SLd"], "atr": s["atr"]}
        v["signaled"] = v["signaled"][-500:]
        # 3) advance open positions
        _fbf_walk(v, bars, now)
        v["log"] = v["log"][-400:]

# ------------------------------------------------------------------ main

GH_PAT = os.environ.get("GH_PAT", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()  # owner/name; state.json lives there


def _gh_api(method, path, body=None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + GH_PAT,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", **UA},
        method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _gh_state_get():
    """Return (sha, state_dict) from the repo, or (None, None) on failure."""
    import base64
    d = _gh_api("GET", "/repos/%s/contents/state.json" % GH_REPO)
    return d["sha"], json.loads(base64.b64decode(d["content"]).decode())


def load_state():
    if GH_PAT and GH_REPO:
        # serverless mode: state.json lives in the GitHub repo
        try:
            _, st = _gh_state_get()
            return st
        except Exception as e:
            print("state load from github failed: %s" % e, file=sys.stderr)
            return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    if GH_PAT and GH_REPO:
        import base64
        try:
            cur = _gh_api("GET", "/repos/%s/contents/state.json" % GH_REPO)
            _gh_api("PUT", "/repos/%s/contents/state.json" % GH_REPO, {
                "message": "state update",
                "content": base64.b64encode(
                    json.dumps(state).encode()).decode(),
                "sha": cur["sha"],
            })
        except Exception as e:
            # losing a state save only re-runs deduped events next time
            print("state save to github failed: %s" % e, file=sys.stderr)
        return
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def main():
    state = load_state()
    sent = 0
    telegram_down = False
    errors = 0
    fetches_done = 0
    td_gate = PROVIDER == "twelvedata"
    now = time.time()
    fetch_log = state.setdefault("__fetch__", {})
    total_combos = 0
    # fair scheduling under Twelve Data's 8-credits/min free cap and the
    # 60s serverless ceiling: least-recently-fetched combos first, at most
    # FETCH_BUDGET fetches per run; the rest stay due and go first next run
    FETCH_BUDGET = 7
    candidates = []
    for sym in SYMBOLS:
        if sym == "XAUUSD" and not GOLD_LIVE:
            # gold paused (Karim 24 Nov 15:03 EET): scalp+ML are dry-run;
            # the forex engine must not trade XAUUSD either
            continue
        for tf in TIMEFRAMES:
            key = "%s_%s" % (sym, tf)
            if td_gate:
                sec = TIMEFRAMES[tf]["sec"]
                if sec < 900:
                    # sub-15m TFs can't be honored on the credit-capped free
                    # tier; the gold scalp gets its 5m bars from Binance
                    continue
                if now - fetch_log.get(key, 0) < sec - 120:
                    # free tier budget: fetch each TF only when due (the cron
                    # runs every 5 min for the scalp; 15m fetches stay ~15 min)
                    continue
            candidates.append((sym, tf, key))
    candidates.sort(key=lambda c: fetch_log.get(c[2], 0))
    for sym, tf, key in candidates:
            if td_gate and fetches_done >= FETCH_BUDGET:
                print("credit budget reached; deferring %s to next run" % key)
                continue
            total_combos += 1
            try:
                bars = fetch_bars(sym, tf)
                fetch_log[key] = now
                fetches_done += 1
            except Exception as e:
                errors += 1
                print("fetch failed %s: %s" % (key, e), file=sys.stderr)
                time.sleep(1)
                continue
            if len(bars) < 10:
                print("too few bars for", key)
                continue
            events = run_engine(bars, SYMBOLS[sym]["pip"],
                                MIN_STOP_PIPS[sym] * SYMBOLS[sym]["pip"])
            st = state.setdefault(key, {"last_time": 0, "signaled": []})
            if not st.get("last_time") or st.get("engine_v") != ENGINE_V:
                # bootstrap (or risk-engine upgrade): learn current state
                # without sending anything - no backlog posts after upgrades
                st["signaled"] = [e["key"] for e in events][-1500:]
                st["msg_ids"] = {}  # old message ids are dead after a reset
                st["last_time"] = bars[-1]["t"]
                st["engine_v"] = ENGINE_V
                print("bootstrap %s (engine v%d): %d bars, %d known events"
                      % (key, ENGINE_V, len(bars), len(events)))
                continue
            prev_last_time = st.get("last_time") or 0
            tf_sec = TIMEFRAMES[tf]["sec"]
            for ev in events:
                if ev["key"] in st["signaled"]:
                    continue
                if ev["time"] <= prev_last_time:
                    # older than the last processed bar: already handled in a
                    # previous run even if its key fell out of the list
                    st["signaled"].append(ev["key"])
                    continue
                if ev["kind"] == "entry" and now - ev["time"] > 2 * tf_sec:
                    # stale "now" wording: never post an old entry late
                    st["signaled"].append(ev["key"])
                    continue
                if ev["kind"] != "entry" and ev.get("entry_key") not in st.setdefault("msg_ids", {}):
                    # follow-up for a signal never posted in this era: skip
                    st["signaled"].append(ev["key"])
                    continue
                if ev["kind"] != "entry" and not NOTIFY_OUTCOMES:
                    st["signaled"].append(ev["key"])
                    continue
                if sent >= MAX_MESSAGES_PER_RUN:
                    break
                if telegram_down:
                    # one failed send means Telegram is unreachable this run:
                    # stop attempting (no hammering), retry everything next run
                    break
                reply_to = None
                if ev["kind"] != "entry":
                    reply_to = st.setdefault("msg_ids", {}).get(ev.get("entry_key"))
                mid = send_telegram(build_message(sym, tf, ev), reply_to=reply_to)
                if mid:
                    st["signaled"].append(ev["key"])
                    if ev["kind"] == "entry":
                        ids = st.setdefault("msg_ids", {})
                        ids[ev["key"]] = mid
                        st["msg_ids"] = dict(list(ids.items())[-200:])
                    sent += 1
                    time.sleep(0.3)
                else:
                    telegram_down = True
            st["signaled"] = st["signaled"][-1500:]
            st["last_time"] = bars[-1]["t"]
            print("%s: %d bars, %d total events, sent so far %d"
                  % (key, len(bars), len(events), sent))
            time.sleep(1)  # be gentle with the data provider
    scalp_before = json.dumps(state.get(SCALP_KEY), sort_keys=True)
    ml_before = json.dumps(state.get(ML_KEY), sort_keys=True)
    try:
        sent, telegram_down = run_scalp(state, now, sent, telegram_down)
    except Exception as e:
        print("scalp run failed (non-fatal):", repr(e), file=sys.stderr)
    try:
        sent, telegram_down = run_ml(state, now, sent, telegram_down)
    except Exception as e:
        print("ML run failed (non-fatal):", repr(e), file=sys.stderr)
    fbf_before = json.dumps(state.get(FBF_KEY), sort_keys=True)
    try:
        run_fbfade(state, now)
    except Exception as e:
        print("fbfade run failed (non-fatal):", repr(e), file=sys.stderr)
    if (fetches_done or sent
            or json.dumps(state.get(SCALP_KEY), sort_keys=True) != scalp_before
            or json.dumps(state.get(ML_KEY), sort_keys=True) != ml_before
            or json.dumps(state.get(FBF_KEY), sort_keys=True) != fbf_before):
        save_state(state)
    else:
        print("no state changes; skipping save")
    print("done. messages sent:", sent, "fetch errors:", errors)
    if total_combos and errors == total_combos:
        # transient provider/network problem: stay green, retry next run
        print("WARNING: all fetches failed this run; will retry next run", file=sys.stderr)


if __name__ == "__main__":
    main()
