"""Fondo multi-activo de momentum (simulado) e índice equiponderado de referencia.

Reglas del fondo (revisión diaria, la primera ejecución de cada día en hora de Madrid):
  1. Universo: los activos de config.json -> fund.assets.
  2. Un activo es apto si su rentabilidad de las últimas `lookback` velas es positiva
     y el precio está por encima de su media de `trendSma` velas (momentum + filtro de tendencia).
  3. Entre los aptos se eligen los `maxAssets` con mejor momentum.
  4. Pesos inversos a la volatilidad (los más tranquilos pesan más), con tope `maxWeight` por activo.
     Lo que no se asigna queda en liquidez: si nada es apto, el fondo está 100 % en euros.
  5. Solo se opera si la desviación respecto al peso objetivo supera `threshold` (ahorra comisiones),
     salvo para vender por completo un activo que deja de ser apto.
"""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Madrid")

def day_key(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")

def fmt(x):
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def new_fund(capital):
    return {"cash": capital, "start": capital, "hold": {}, "cost": {}, "fees": 0.0, "trades": [],
            "lastRebalance": None, "targets": {}, "scores": {}, "lastTxt": "", "equity": capital}

def fund_equity(pf, prices):
    last = pf.setdefault("prices", {})
    return pf["cash"] + sum(q * prices.get(a, last.get(a, 0)) for a, q in pf["hold"].items())

def analyze(fc, closes_by_asset):
    """Devuelve (objetivos {activo: peso}, puntuaciones {activo: dict})."""
    L, M, V = fc["lookback"], fc["trendSma"], fc["volWindow"]
    scores, elig = {}, []
    for a, c in closes_by_asset.items():
        if len(c) <= max(L, M, V):
            scores[a] = {"ok": False, "why": "pocos datos"}; continue
        mom = c[-1] / c[-1 - L] - 1
        sma = sum(c[-M:]) / M
        rets = [math.log(c[i] / c[i - 1]) for i in range(len(c) - V, len(c))]
        mu = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) or 1e-9
        ok = mom > 0 and c[-1] > sma
        scores[a] = {"ok": ok, "mom": mom, "trend": c[-1] / sma - 1, "vol": vol,
                     "why": "apto" if ok else ("momentum negativo" if mom <= 0 else "por debajo de su media")}
        if ok: elig.append(a)
    elig.sort(key=lambda a: scores[a]["mom"], reverse=True)
    chosen = elig[:fc["maxAssets"]]
    inv = {a: 1 / scores[a]["vol"] for a in chosen}
    tot = sum(inv.values())
    w = {a: v / tot for a, v in inv.items()} if tot else {}
    # tope por activo, redistribuyendo el exceso entre los que no lo alcanzan
    cap = fc["maxWeight"]
    for _ in range(10):
        over = {a: x for a, x in w.items() if x > cap + 1e-12}
        if not over: break
        excess = sum(x - cap for x in over.values())
        for a in over: w[a] = cap
        free = {a: x for a, x in w.items() if x < cap - 1e-12}
        ft = sum(free.values())
        if not ft: break
        for a, x in free.items(): w[a] = x + excess * x / ft
    w = {a: min(x, cap) for a, x in w.items()}
    for a in scores: scores[a]["target"] = w.get(a, 0.0)
    return w, scores

def rebalance(pf, targets, prices, cfg, fc, t, names):
    """Ejecuta las operaciones necesarias. Devuelve la lista de operaciones."""
    fee, slip = cfg["fee"], cfg["slippage"]
    E = fund_equity(pf, prices)
    trades = []
    # ventas primero
    for a in list(pf["hold"].keys()):
        q = pf["hold"][a]; px = prices[a]; val = q * px
        tgt = targets.get(a, 0.0) * E
        if tgt == 0 or val - tgt > fc["threshold"] * E:
            sell_val = val if tgt == 0 else val - tgt
            if sell_val < 5 and tgt != 0: continue
            qs = q if tgt == 0 else sell_val / px
            epx = px * (1 - slip); gross = qs * epx; f = gross * fee; net = gross - f
            cost_part = pf["cost"].get(a, 0) * (qs / q)
            pnl = net - cost_part
            pf["hold"][a] = q - qs; pf["cost"][a] = pf["cost"].get(a, 0) - cost_part
            if tgt == 0 or pf["hold"][a] * px < 0.01:
                pf["hold"].pop(a, None); pf["cost"].pop(a, None)
            pf["cash"] += net; pf["fees"] += f
            tr = {"t": t, "side": "VENTA", "asset": a, "price": epx, "amount": net, "fee": f, "pnl": pnl,
                  "pnlPct": pnl / cost_part if cost_part else 0,
                  "reason": "sale del fondo" if tgt == 0 else "reduce peso"}
            pf["trades"].append(tr); trades.append(tr)
    # compras
    E = fund_equity(pf, prices)
    for a, w in sorted(targets.items(), key=lambda x: -x[1]):
        px = prices[a]; val = pf["hold"].get(a, 0) * px; tgt = w * E
        if tgt - val > fc["threshold"] * E or (val == 0 and tgt > 5):
            spend = min(tgt - val, pf["cash"])
            if spend < 5: continue
            f = spend * fee; epx = px * (1 + slip); q = (spend - f) / epx
            pf["cash"] -= spend; pf["fees"] += f
            pf["hold"][a] = pf["hold"].get(a, 0) + q; pf["cost"][a] = pf["cost"].get(a, 0) + spend
            tr = {"t": t, "side": "COMPRA", "asset": a, "price": epx, "amount": spend, "fee": f,
                  "reason": "entra en el fondo" if val == 0 else "aumenta peso"}
            pf["trades"].append(tr); trades.append(tr)
    return trades

def buy_index(pf, assets, prices, cfg, t):
    each = pf["cash"] / len(assets); trades = []
    for a in assets:
        f = each * cfg["fee"]; epx = prices[a] * (1 + cfg["slippage"]); q = (each - f) / epx
        pf["cash"] -= each; pf["fees"] += f
        pf["hold"][a] = q; pf["cost"][a] = each
        tr = {"t": t, "side": "COMPRA", "asset": a, "price": epx, "amount": each, "fee": f, "reason": "índice: reparto inicial"}
        pf["trades"].append(tr); trades.append(tr)
    return trades

def backtest_fund(series, cfg, fc):
    """series: {activo: [velas cerradas]} alineadas por tiempo. Rebalanceo diario."""
    times = sorted(set.intersection(*[set(k["T"] for k in s) for s in series.values()]))
    idx = {a: {k["T"]: i for i, k in enumerate(s)} for a, s in series.items()}
    warm = max(fc["lookback"], fc["trendSma"], fc["volWindow"]) + 1
    pf = new_fund(cfg["capital"]); ix = new_fund(cfg["capital"]); curve = []; lastDay = None
    for t in times:
        if any(idx[a][t] < warm for a in series): continue
        prices = {a: series[a][idx[a][t]]["c"] for a in series}
        if not ix["hold"]: buy_index(ix, list(series), prices, cfg, t)
        d = day_key(t)
        if d != lastDay:
            closes = {a: [k["c"] for k in series[a][:idx[a][t] + 1]] for a in series}
            w, _ = analyze(fc, closes)
            rebalance(pf, w, prices, cfg, fc, t, None); lastDay = d
        curve.append([t, round(fund_equity(pf, prices), 2), round(fund_equity(ix, prices), 2)])
    def summ(i, p):
        eq = [c[i] for c in curve] or [cfg["capital"]]
        pk, mdd = -1e18, 0.0
        for v in eq: pk = max(pk, v); mdd = min(mdd, v / pk - 1)
        sells = [x for x in p["trades"] if x["side"] == "VENTA"]
        return {"ret": eq[-1] / cfg["capital"] - 1, "end": eq[-1], "mdd": mdd, "trades": len(p["trades"]),
                "winRate": (sum(1 for x in sells if x["pnl"] > 0) / len(sells)) if sells else None,
                "fees": p["fees"], "from": curve[0][0] if curve else None, "to": curve[-1][0] if curve else None}
    return summ(1, pf), summ(2, ix)

def run(state, cfg, now, fetch_ohlc, fetch_prices, log):
    fc = cfg.get("fund")
    if not fc or not fc.get("enabled"): return
    names = {x["pair"]: x["symbol"] for x in fc["assets"]}
    series, bad = {}, []
    for x in fc["assets"]:
        try:
            ks = fetch_ohlc(x["pair"], cfg["interval"])
            series[x["symbol"]] = [k for k in ks if k["T"] <= now]
        except Exception as e:  # noqa
            bad.append(x["symbol"])
    if not series: return
    try:
        live = fetch_prices([x["pair"] for x in fc["assets"] if x["symbol"] in series])
        prices = {names[p]: v for p, v in live.items() if p in names}
    except Exception:  # noqa
        prices = {}
    for a, s in series.items():
        prices.setdefault(a, s[-1]["c"])

    P = state["portfolios"]
    if "fund" not in P:
        P["fund"] = new_fund(state["capital"]); P["index"] = new_fund(state["capital"])
        log(f"[Fondo] creado con {fmt(state['capital'])} €. Universo: {', '.join(series)}.")
        tr = buy_index(P["index"], list(series), prices, cfg, now)
        log(f"[Índice] reparte {fmt(state['capital'])} € a partes iguales entre {len(tr)} activos y no vuelve a operar.", "b")
    pf = P["fund"]
    closes = {a: [k["c"] for k in s] for a, s in series.items()}
    w, sc = analyze(fc, closes)
    pf["scores"] = sc; pf["targets"] = w
    d = day_key(now)
    if pf["lastRebalance"] != d and bad:
        if pf.get("waitLogged") != d:
            log(f"[Fondo] revisión aplazada: faltan datos de {', '.join(bad)}. Se reintenta en la próxima ejecución.", "w")
            pf["waitLogged"] = d
    elif pf["lastRebalance"] != d:
        trades = rebalance(pf, w, prices, cfg, fc, now, names)
        pf["lastRebalance"] = d
        if w:
            comp = ", ".join(f"{a} {x*100:.0f} %" for a, x in sorted(w.items(), key=lambda x: -x[1]))
            cash = (1 - sum(w.values())) * 100
            log(f"[Fondo] revisión diaria → {comp}{f', liquidez {cash:.0f} %' if cash > 0.5 else ''}.")
        else:
            log("[Fondo] revisión diaria → ningún activo en tendencia alcista: 100 % en liquidez.")
        for tr in trades:
            if tr["side"] == "COMPRA":
                log(f"[Fondo] COMPRA {tr['asset']} {fmt(tr['amount'])} € a {fmt(tr['price'])} € · {tr['reason']}", "b")
            else:
                log(f"[Fondo] VENTA {tr['asset']} a {fmt(tr['price'])} € · {fmt(tr['pnl'])} € ({tr['pnlPct']*100:+.2f} %) · {tr['reason']}", "s")
    pf["lastTxt"] = f"{len(pf['hold'])} activos · próxima revisión mañana" if pf["hold"] else "en liquidez · próxima revisión mañana"
    P["index"]["lastTxt"] = f"{len(P['index']['hold'])} activos a partes iguales"
    for k in ("fund", "index"):
        P[k]["equity"] = fund_equity(P[k], prices)
        P[k]["prices"] = {a: prices.get(a, P[k].get("prices", {}).get(a, 0)) for a in P[k]["hold"]}
    state["fundPrices"] = prices
    state["fundBacktest"] = dict(zip(("fund", "index"), backtest_fund(series, cfg, fc)))
