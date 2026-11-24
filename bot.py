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

# same defaults as the Pine inputs
STOP_OFFSET_PIPS = 5.0
RISK_REWARD = 2.0
MAX_ZONES = 1000
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

def run_engine(bars, pip):
    """Replay the indicator over bars. Return list of signal events:
    {'key', 'time', 'kind' ('entry'|'tp'|'sl'), 'dir', 'entry', 'sl', 'tp'}"""
    stop_offset = STOP_OFFSET_PIPS * pip
    zones = []   # dicts, oldest first
    events = []

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
            if z["dir"] == 1:
                entry = z["top"]
                sl = z["bottom"] - stop_offset
                tp = entry + (entry - sl) * RISK_REWARD
            else:
                entry = z["bottom"]
                sl = z["top"] + stop_offset
                tp = entry - (sl - entry) * RISK_REWARD
            z.update(active=True, entry=entry, sl=sl, tp=tp,
                     entry_i=i, entry_t=b["t"])
            events.append({
                "key": "e:%d:%d" % (z["engulf_t"], z["dir"]),
                "time": b["t"], "kind": "entry", "dir": z["dir"],
                "entry": entry, "sl": sl, "tp": tp,
            })

        # ---------------- OUTCOME (TP / SL hit), same rules as the stats engine
        for z in zones:
            if not (z["active"] and z["closed"] is None
                    and z["entry_i"] is not None and i > z["entry_i"]):
                continue
            if z["dir"] == 1:
                hit_tp = b["h"] >= z["tp"]; hit_sl = b["l"] <= z["sl"]
            else:
                hit_tp = b["l"] <= z["tp"]; hit_sl = b["h"] >= z["sl"]
            if hit_tp and not hit_sl:
                z["closed"] = "tp"
                events.append({"key": "t:%d:%d" % (z["engulf_t"], z["dir"]),
                               "time": b["t"], "kind": "tp", "dir": z["dir"],
                               "entry": z["entry"], "sl": z["sl"], "tp": z["tp"]})
            elif hit_sl and not hit_tp:
                z["closed"] = "sl"
                events.append({"key": "t:%d:%d" % (z["engulf_t"], z["dir"]),
                               "time": b["t"], "kind": "sl", "dir": z["dir"],
                               "entry": z["entry"], "sl": z["sl"], "tp": z["tp"]})

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

def send_telegram(text):
    if DRY_RUN:
        print("[DRY RUN telegram]", text.replace("\n", " | "))
        return True
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    relay = os.environ.get("TELEGRAM_RELAY", "").strip()
    if not chat or (not relay and not token):
        print("ERROR: TELEGRAM_RELAY or TELEGRAM_TOKEN + TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return False
    # relay: a Cloudflare Worker holding the bot token, used because
    # api.telegram.org throttle-lists datacenter egress IPs
    url = relay if relay else "https://api.telegram.org/bot%s/sendMessage" % token
    payload = json.dumps({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json", **UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        print("telegram error:", e.code, e.read()[:200], file=sys.stderr)
        return False
    except Exception as e:
        # hang/timeout/refused (transient) -> log, skip this run, retry next run
        print("telegram send failed (transient):", repr(e), file=sys.stderr)
        return False


def fmt_price(x, pip):
    decimals = max(0, round(-__import__("math").log10(pip)) + 1)
    return ("%.*f" % (decimals, x)).rstrip("0").rstrip(".") if "." in ("%.*f" % (decimals, x)) else ("%.*f" % (decimals, x))


def build_message(sym, tf, ev):
    if ev["kind"] == "entry":
        d = "BUY شراء" if ev["dir"] == 1 else "SELL بيع"
        return (
            "🚨 إشارة HAKIM_FX\n"
            "الزوج: %s\n"
            "الفريم: %s\n"
            "الاتجاه: %s\n"
            "الدخول Entry: %s\n"
            "الستوب SL: %s\n"
            "الهدف TP: %s"
        ) % (sym, tf, d,
             fmt_price(ev["entry"], SYMBOLS[sym]["pip"]),
             fmt_price(ev["sl"], SYMBOLS[sym]["pip"]),
             fmt_price(ev["tp"], SYMBOLS[sym]["pip"]))
    else:
        d = "BUY" if ev["dir"] == 1 else "SELL"
        mark = "✅ ضربت الهدف (TP)" if ev["kind"] == "tp" else "❌ ضربت الستوب (SL)"
        return "HAKIM_FX %s\nالزوج: %s - الفريم: %s - %s" % (mark, sym, tf, d)


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
            events = run_engine(bars, SYMBOLS[sym]["pip"])
            st = state.setdefault(key, {"last_time": 0, "signaled": []})
            if not st.get("last_time"):
                # bootstrap: learn current state without sending anything
                st["signaled"] = [e["key"] for e in events][-500:]
                st["last_time"] = bars[-1]["t"]
                print("bootstrap %s: %d bars, %d known events" % (key, len(bars), len(events)))
                continue
            for ev in events:
                if ev["key"] in st["signaled"]:
                    continue
                if ev["kind"] != "entry" and not NOTIFY_OUTCOMES:
                    st["signaled"].append(ev["key"])
                    continue
                if sent >= MAX_MESSAGES_PER_RUN:
                    break
                if send_telegram(build_message(sym, tf, ev)):
                    st["signaled"].append(ev["key"])
                    sent += 1
                    time.sleep(0.3)
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
