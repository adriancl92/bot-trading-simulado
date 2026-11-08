#!/usr/bin/env python3
"""Bot de paper trading con datos reales de mercado (Kraken, API pública).

Cada ejecución (GitHub Actions, cada ~15 min):
  1. Descarga las velas reales y el último precio.
  2. Procesa las velas cerradas nuevas para cada estrategia (decisiones al cierre de vela).
  3. Revisa stop-loss y límite diario con el mínimo de la vela en curso.
  4. Guarda el estado en data/state.json, que lee el panel (index.html).

No envía órdenes reales: simula órdenes a mercado con comisión y slippage.
"""
import json, os, sys, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "data", "state.json")
TZ = ZoneInfo("Europe/Madrid")
MAX_CURVE = 4000      # puntos de evolución guardados
MAX_LOG = 400         # entradas del registro de actividad

# ---------------------------------------------------------------- datos
def http_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-trading-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (i + 1))
    raise last

def fetch_ohlc(pair, interval):
    j = http_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}")
    if j.get("error"):
        raise RuntimeError(j["error"])
    key = [k for k in j["result"] if k != "last"][0]
    tf = interval * 60
    rows = [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "T": int(r[0]) + tf}
            for r in j["result"][key]]
    return rows

def fetch_price(pair):
    j = http_json(f"https://api.kraken.com/0/public/Ticker?pair={pair}")
    if j.get("error"):
        raise RuntimeError(j["error"])
    v = list(j["result"].values())[0]
    return float(v["c"][0])

# ---------------------------------------------------------- indicadores
def sma_at(c, i, n):
    if i < n - 1: return None
    return sum(c[i - n + 1:i + 1]) / n

def ema_arr(c, n):
    a = [None] * len(c)
    if len(c) < n: return a
    a[n - 1] = sum(c[:n]) / n
    m = 2 / (n + 1)
    for k in range(n, len(c)):
        a[k] = c[k] * m + a[k - 1] * (1 - m)
    return a

def rsi_arr(c, n):
    a = [None] * len(c)
    if len(c) <= n: return a
    g = l = 0.0
    for k in range(1, n + 1):
        d = c[k] - c[k - 1]
        g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    a[n] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    for k in range(n + 1, len(c)):
        d = c[k] - c[k - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        a[k] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return a

def f0(x): return f"{x:,.0f}".replace(",", ".")

def make_signal(kind, p, closes):
    """Devuelve f(i, pos) -> (señal 1/0/None, texto)."""
    if kind == "cross":
        ef, es = ema_arr(closes, p["f"]), ema_arr(closes, p["s"])
        def f(i, pos):
            if ef[i] is None or es[i] is None: return None, "calentando"
            return (1 if ef[i] > es[i] else 0), f"EMA{p['f']} {f0(ef[i])} vs EMA{p['s']} {f0(es[i])}"
        return f
    if kind == "tsmom":
        def f(i, pos):
            if i < p["n"]: return None, "calentando"
            r = closes[i] / closes[i - p["n"]] - 1
            return (1 if r > 0 else 0), f"retorno {p['n']} velas {r*100:+.2f} %"
        return f
    if kind == "rsi":
        r = rsi_arr(closes, p["n"])
        def f(i, pos):
            if r[i] is None: return None, "calentando"
            s = pos
            if pos == 0 and r[i] < p["lo"]: s = 1
            elif pos == 1 and r[i] > p["hi"]: s = 0
            return s, f"RSI {r[i]:.1f}"
        return f
    if kind == "hold":
        def f(i, pos): return 1, "comprar y mantener"
        return f
    # trend (por defecto)
    def f(i, pos):
        m = sma_at(closes, i, p["n"])
        if m is None: return None, "calentando"
        return (1 if closes[i] > m else 0), f"precio {f0(closes[i])} vs media {f0(m)}"
    return f

# ----------------------------------------------------------------- motor
def day_key(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")

def new_portfolio(capital):
    return {"cash": capital, "start": capital, "qty": 0.0, "entry": 0.0, "entryCost": 0.0, "fees": 0.0,
            "trades": [], "dayK": None, "dayStart": capital, "halted": False, "stopped": False,
            "lastSig": None, "lastTxt": ""}

class Engine:
    def __init__(self, s, cfg, risk=True):
        self.s, self.c, self.risk = s, cfg, risk

    def equity(self, price):
        return self.s["cash"] + self.s["qty"] * price

    def buy(self, t, price, reason):
        s, c = self.s, self.c
        px = price * (1 + c["slippage"])
        spend = s["cash"] * c["allocation"]
        if spend < 5: return None
        fee = spend * c["fee"]
        qty = (spend - fee) / px
        s["cash"] -= spend; s["qty"] += qty; s["fees"] += fee
        s["entry"] = px; s["entryCost"] = spend
        tr = {"t": t, "side": "COMPRA", "price": px, "amount": spend, "fee": fee, "reason": reason}
        s["trades"].append(tr); return tr

    def sell(self, t, price, reason):
        s, c = self.s, self.c
        if s["qty"] <= 0: return None
        px = price * (1 - c["slippage"])
        gross = s["qty"] * px; fee = gross * c["fee"]; net = gross - fee
        pnl = net - s["entryCost"]
        s["cash"] += net; s["fees"] += fee; s["qty"] = 0.0
        tr = {"t": t, "side": "VENTA", "price": px, "amount": net, "fee": fee, "pnl": pnl,
              "pnlPct": pnl / s["entryCost"] if s["entryCost"] else 0, "reason": reason}
        s["trades"].append(tr); return tr

    def new_day(self, t, price):
        k = day_key(t)
        if k != self.s["dayK"]:
            self.s["dayK"] = k; self.s["dayStart"] = self.equity(price); self.s["halted"] = False
            return True
        return False

    def check_risk(self, t, low, price):
        s, c = self.s, self.c
        if not self.risk or s["qty"] <= 0: return None
        if c["stopLoss"] > 0:
            stop = s["entry"] * (1 - c["stopLoss"])
            if low <= stop:
                s["stopped"] = True
                return self.sell(t, min(stop, price), f"stop-loss {c['stopLoss']*100:.1f} %")
        if c["maxDailyLoss"] > 0:
            floor = s["dayStart"] * (1 - c["maxDailyLoss"])
            if self.equity(low) <= floor:
                s["halted"] = True
                px = max(low, (floor - s["cash"]) / s["qty"])
                return self.sell(t, min(px, price), f"límite de pérdida diaria {c['maxDailyLoss']*100:.1f} %")
        return None

    def on_close(self, t, close, sig):
        s = self.s; tr = None
        if sig == 0: s["stopped"] = False
        if sig == 1 and s["qty"] == 0 and not s["stopped"] and not s["halted"]:
            tr = self.buy(t, close, "señal de entrada")
        elif sig == 0 and s["qty"] > 0:
            tr = self.sell(t, close, "señal de salida")
        return tr

def backtest(candles, cfg, strat):
    closes = [k["c"] for k in candles]
    f = make_signal(strat["kind"], strat.get("params", {}), closes)
    s = new_portfolio(cfg["capital"]); e = Engine(s, cfg, risk=strat["kind"] != "hold")
    curve = []
    for i, k in enumerate(candles):
        sig, _ = f(i, 1 if s["qty"] > 0 else 0)
        if sig is None: continue
        e.new_day(k["t"], k["o"]); e.check_risk(k["T"], k["l"], k["c"]); e.on_close(k["T"], k["c"], sig)
        curve.append([k["T"], round(e.equity(k["c"]), 2)])
    eq = [p[1] for p in curve] or [cfg["capital"]]
    peak, mdd = -1e18, 0.0
    for v in eq:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    sells = [t for t in s["trades"] if t["side"] == "VENTA"]
    return {"ret": eq[-1] / cfg["capital"] - 1, "end": eq[-1], "mdd": mdd, "trades": len(s["trades"]),
            "winRate": (sum(1 for t in sells if t["pnl"] > 0) / len(sells)) if sells else None,
            "fees": s["fees"], "from": curve[0][0] if curve else None, "to": curve[-1][0] if curve else None,
            "curve": curve[::max(1, len(curve) // 300)]}

# ------------------------------------------------------------------ main
def fmt(x): return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def main():
    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    reset_capital = os.environ.get("RESET_CAPITAL", "").strip()
    state = None
    if os.path.exists(STATE_PATH) and not reset_capital:
        state = json.load(open(STATE_PATH, encoding="utf-8"))
        if state.get("pair") != cfg["pair"] or state.get("interval") != cfg["interval"]:
            state = None  # cambió el mercado: se reinicia
    if reset_capital:
        cfg["capital"] = float(reset_capital.replace(",", "."))
        json.dump(cfg, open(CFG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    candles = fetch_ohlc(cfg["pair"], cfg["interval"])
    price = fetch_price(cfg["pair"])
    now = int(time.time())
    closed = [k for k in candles if k["T"] <= now]
    partial = candles[-1] if candles[-1]["T"] > now else None
    closes = [k["c"] for k in closed]
    strats = cfg["strategies"]

    def log(msg, kind=""):
        state["log"].append({"t": now, "msg": msg, "kind": kind})

    if state is None:
        state = {"pair": cfg["pair"], "label": cfg["label"], "interval": cfg["interval"], "startedAt": now,
                 "capital": cfg["capital"], "lastT": closed[-1]["T"], "portfolios": {}, "curve": [], "log": []}
        for st in strats:
            pf = new_portfolio(cfg["capital"])
            state["portfolios"][st["id"]] = pf
        log(f"Bot iniciado con {fmt(cfg['capital'])} € por estrategia en {cfg['label']} (velas de {cfg['interval']} min). Precio actual {fmt(price)} €.")
        # comprar y mantener entra ya
        for st in strats:
            pf = state["portfolios"][st["id"]]; e = Engine(pf, cfg, risk=st["kind"] != "hold")
            e.new_day(now, price)
            f = make_signal(st["kind"], st.get("params", {}), closes)
            sig, txt = f(len(closed) - 1, 0)
            pf["lastSig"], pf["lastTxt"] = sig, txt
            if st["kind"] == "hold":
                tr = e.buy(now, price, "inicio: comprar y mantener")
                if tr: log(f"[{st['name']}] COMPRA {fmt(tr['amount'])} € a {fmt(tr['price'])} €", "b")
            else:
                log(f"[{st['name']}] situación inicial: {'DENTRO' if sig else 'FUERA'} · {txt}. Actuará al cierre de la próxima vela.")

    # ---- velas cerradas nuevas
    new = [i for i, k in enumerate(closed) if k["T"] > state["lastT"]]
    if new and len(new) > 1:
        log(f"Procesando {len(new)} velas cerradas desde la última ejecución.")
    for st in strats:
        pf = state["portfolios"].setdefault(st["id"], new_portfolio(state["capital"]))
        e = Engine(pf, cfg, risk=st["kind"] != "hold")
        f = make_signal(st["kind"], st.get("params", {}), closes)
        for i in new:
            k = closed[i]
            e.new_day(k["t"], k["o"])
            tr = e.check_risk(k["T"], k["l"], k["c"])
            if tr: log(f"[{st['name']}] VENTA a {fmt(tr['price'])} € · {fmt(tr['pnl'])} € ({tr['pnlPct']*100:+.2f} %) · {tr['reason']}", "s")
            sig, txt = f(i, 1 if pf["qty"] > 0 else 0)
            pf["lastSig"], pf["lastTxt"] = sig, txt
            if sig is None: continue
            tr = e.on_close(k["T"], k["c"], sig)
            if tr and tr["side"] == "COMPRA":
                log(f"[{st['name']}] COMPRA {fmt(tr['amount'])} € a {fmt(tr['price'])} € · {txt}", "b")
            elif tr:
                log(f"[{st['name']}] VENTA a {fmt(tr['price'])} € · {fmt(tr['pnl'])} € ({tr['pnlPct']*100:+.2f} %) · {tr['reason']} · {txt}", "s")
        # vela en curso: stop-loss / límite diario con su mínimo hasta ahora
        e.new_day(now, price)
        if partial:
            tr = e.check_risk(now, min(partial["l"], price), price)
            if tr: log(f"[{st['name']}] VENTA a {fmt(tr['price'])} € · {fmt(tr['pnl'])} € ({tr['pnlPct']*100:+.2f} %) · {tr['reason']}", "s")
    if new:
        state["lastT"] = closed[new[-1]]["T"]
        k = closed[new[-1]]
        if not any(l["t"] == now and l["kind"] for l in state["log"]):
            log(f"Vela de las {datetime.fromtimestamp(k['t'], TZ):%H:%M} cerrada a {fmt(k['c'])} €. Ninguna estrategia cambia de posición.")

    # ---- foto del momento
    point = {"t": now, "price": price}
    for st in strats:
        pf = state["portfolios"][st["id"]]
        point[st["id"]] = round(pf["cash"] + pf["qty"] * price, 4)
    state["curve"].append(point)
    if len(state["curve"]) > MAX_CURVE:  # conserva todo lo reciente, aclara lo antiguo
        old, recent = state["curve"][:-MAX_CURVE // 2], state["curve"][-MAX_CURVE // 2:]
        state["curve"] = old[::2] + recent
    state["log"] = state["log"][-MAX_LOG:]
    state.update({"updatedAt": now, "price": price, "nextCandleClose": partial["T"] if partial else None,
                  "config": {k: cfg[k] for k in ("fee", "slippage", "stopLoss", "maxDailyLoss", "allocation", "interval", "label", "capital")},
                  "strategies": strats,
                  "candles": [[k["T"], k["c"]] for k in closed[-200:]]})
    # backtest de referencia sobre las velas disponibles (~30 días con velas de 1 h)
    state["backtest"] = {st["id"]: backtest(closed, cfg, st) for st in strats}

    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)
    print(f"OK {datetime.fromtimestamp(now, TZ):%Y-%m-%d %H:%M} precio {price} · velas nuevas {len(new)}")

if __name__ == "__main__":
    main()
