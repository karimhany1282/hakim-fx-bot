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
ENGINE_V = 2  # bump when event keys/levels change -> silent re-bootstrap
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
        tfline = _variant(ev["key"] + "f", TF_LINES) % tf
        lines = [
            tpl % (head, sym, fmt_price(ev["entry"], pip)),
            tfline,
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


# ------------------------------------------------------------------ main

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def main():
    state = load_state()
    sent = 0
    telegram_down = False
    errors = 0
    td_gate = PROVIDER == "twelvedata"
    now = time.time()
    fetch_log = state.setdefault("__fetch__", {})
    total_combos = 0
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            key = "%s_%s" % (sym, tf)
            if td_gate:
                sec = TIMEFRAMES[tf]["sec"]
                if sec < 900:
                    # sub-15m TFs can't be honored at a 15-min cron cadence;
                    # skip them on the credit-capped free tier
                    continue
                if sec > 900 and now - fetch_log.get(key, 0) < sec - 120:
                    # free tier budget: fetch 1h/4h only when due (~126
                    # credits/symbol/day -> 6 symbols fits in 800/day)
                    continue
            total_combos += 1
            try:
                bars = fetch_bars(sym, tf)
                fetch_log[key] = now
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
                st["signaled"] = [e["key"] for e in events][-500:]
                st["last_time"] = bars[-1]["t"]
                st["engine_v"] = ENGINE_V
                print("bootstrap %s (engine v%d): %d bars, %d known events"
                      % (key, ENGINE_V, len(bars), len(events)))
                continue
            for ev in events:
                if ev["key"] in st["signaled"]:
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
            st["signaled"] = st["signaled"][-500:]
            st["last_time"] = bars[-1]["t"]
            print("%s: %d bars, %d total events, sent so far %d"
                  % (key, len(bars), len(events), sent))
            time.sleep(1)  # be gentle with the data provider
    save_state(state)
    print("done. messages sent:", sent, "fetch errors:", errors)
    if total_combos and errors == total_combos:
        # transient provider/network problem: stay green, retry next run
        print("WARNING: all fetches failed this run; will retry next run", file=sys.stderr)


if __name__ == "__main__":
    main()
