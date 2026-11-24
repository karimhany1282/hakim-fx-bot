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
SCALP_V = 1
SCALP_STALE_SEC = 1800          # "buy now" wording: never post a scalp >30 min old
BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines?symbol=PAXGUSDT&interval=5m"

BE_TEXTS = [
    "\U0001F6E1\uFE0F الستوب كان على الدخول وخرجنا على التعادل 🤍 (%s)\n0 نقطة",
    "\U0001F6E1\uFE0F خرجنا على التعادل بعد ما الستوب اتحرك للدخول 🤲 (%s)\n0 نقطة",
]
SCALP_TP1_SUFFIX = "\n\U0001F6E1\uFE0F الستوب دلوقتي على الدخول"


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
    # weekday filter (Mon-Fri UTC), exactly like the page engine
    return [b for b in out if time.gmtime(b["t"]).tm_wday <= 4]


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
    # --- new entries ---
    groups = {}
    for e in entries:
        groups.setdefault((int(bars[e["bar_i"]]["t"]), e["dir"]), []).append(e)
    quote_dead = False
    for gkey in sorted(groups):
        evs = groups[gkey]
        rep = next((e for e in evs if e["rule"] == "sweep"), evs[0])
        if any(e["key"] in st["signaled"] for e in evs):
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
            sent += 1
            time.sleep(0.3)
            if ev["kind"] in ("sl", "be", "tp3"):
                del st["positions"][pkey]
                break
    st["signaled"] = st["signaled"][-1500:]
    st["last_time"] = bars[-1]["t"]
    ids = st["msg_ids"]
    st["msg_ids"] = dict(list(ids.items())[-200:])
    print("scalp: %d bars, %d entries in window, %d open positions, sent so far %d"
          % (len(bars), len(entries), len(st["positions"]), sent))
    return sent, telegram_down


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


def load_state():
    if GH_PAT and GH_REPO:
        # serverless mode: state.json lives in the GitHub repo
        try:
            d = _gh_api("GET", "/repos/%s/contents/state.json" % GH_REPO)
            import base64
            return json.loads(base64.b64decode(d["content"]).decode())
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
    try:
        sent, telegram_down = run_scalp(state, now, sent, telegram_down)
    except Exception as e:
        print("scalp run failed (non-fatal):", repr(e), file=sys.stderr)
    save_state(state)
    print("done. messages sent:", sent, "fetch errors:", errors)
    if total_combos and errors == total_combos:
        # transient provider/network problem: stay green, retry next run
        print("WARNING: all fetches failed this run; will retry next run", file=sys.stderr)


if __name__ == "__main__":
    main()
