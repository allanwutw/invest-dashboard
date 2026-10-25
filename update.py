# -*- coding: utf-8 -*-
"""每日更新：抓資料 -> 估值 -> 產生 data.js 給 dashboard.html 讀取。
用法：
  python update.py            全部重新抓取（約 3 到 6 分鐘）
  python update.py --cache    今天已經抓過的話，直接用 cache.json（除錯用）
"""
import csv
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "stocks.csv")
OUT_PATH = os.path.join(HERE, "data.js")
CACHE_PATH = os.path.join(HERE, "cache.json")
TZ = timezone(timedelta(hours=8))   # 台灣時間
HOLD_PATH = os.path.join(HERE, "etf_holdings.csv")
ETFS = [
    {"tk": "0050", "name": "元大台灣50", "mk": "TW", "yf": "0050.TW", "cur": "NT$", "ind": "台灣前50大"},
    {"tk": "VOO", "name": "Vanguard 標普500", "mk": "US", "yf": "VOO", "cur": "US$", "ind": "美國大型股"},
    {"tk": "QQQ", "name": "Invesco 那斯達克100", "mk": "US", "yf": "QQQ", "cur": "US$", "ind": "美國科技成長"},
    {"tk": "SOXX", "name": "iShares 半導體", "mk": "US", "yf": "SOXX", "cur": "US$", "ind": "美國半導體"},
]
US_URL = "https://americancompanies.com/reports/largest-american-companies/"

# ---- 可調整的假設 ----------------------------------------------------------
TG = 0.025                     # 永續成長率
PROBS = (0.25, 0.50, 0.25)     # 悲觀 / 中性 / 樂觀 的機率
MIN_ANALYSTS = 3               # 分析師少於這個人數就不採用目標價
# 折現率 = 無風險利率 + beta x 風險溢酬，再限制在合理範圍內
RATES = {"TW": (0.016, 0.06, 0.09, 0.12), "US": (0.043, 0.05, 0.08, 0.11)}
CAP = 0.5                      # 單一估值方法與現價的最大差距（正負 50%），超過就限幅並加警示

SECTOR_ZH = {
    "Technology": "科技", "Financial Services": "金融", "Healthcare": "醫療保健",
    "Consumer Cyclical": "非必需消費", "Consumer Defensive": "必需消費",
    "Communication Services": "通訊服務", "Industrials": "工業", "Energy": "能源",
    "Basic Materials": "原物料", "Utilities": "公用事業", "Real Estate": "不動產",
}
NAMES = {"hpe": "自身歷史本益比法", "hpb": "自身歷史淨值比法", "ev": "EV/EBITDA 法", "pe": "同業本益比法", "pb": "股價淨值比法（同業或ROE）", "dcf": "現金流折現",
         "gdcf": "成長型現金流折現", "ddm": "股利折現", "target": "分析師目標價（中位數）"}
# 分類 -> (模型名稱, 說明, 各方法權重)
GROUPS = {
    "bank": ("股價淨值比與本益比",
             "金融股獲利常受一次性因素影響，淨值較穩定，所以以股價淨值比為主，再對照同業本益比與分析師目標價。",
             {"pb": .35, "hpb": .2, "pe": .15, "target": .3}),
    "mature": ("本益比與現金流折現",
               "獲利與現金流穩定的成熟公司，以同業本益比與自由現金流折現為主，並參考分析師目標價。",
               {"pe": .2, "hpe": .1, "ev": .15, "dcf": .25, "target": .3}),
    "defensive": ("現金流折現與股利折現",
                  "需求穩定、股利持續的防禦型公司，以現金流折現與股利折現為主，輔以本益比與分析師目標價。",
                  {"dcf": .2, "ddm": .1, "pe": .15, "hpe": .1, "ev": .15, "target": .3}),
    "growth": ("成長型現金流折現",
               "營收與獲利仍在快速成長，單看目前本益比容易失真，所以以拉長期間的成長型現金流折現為主。",
               {"gdcf": .3, "pe": .1, "hpe": .1, "ev": .1, "target": .4}),
    "cyclical": ("股價淨值比與本益比（景氣循環）",
                 "獲利隨景氣大起大落，單一年度獲利不可靠，所以用同業股價淨值比與預估本益比，並提高分析師目標價的參考比重。",
                 {"pb": .15, "hpb": .15, "pe": .1, "hpe": .1, "ev": .1, "target": .4}),
}


def n(x, lo=None, hi=None):
    """轉成有效數字；不合理或缺值回傳 None。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def rnd(x):
    return None if x is None else round(x, 2)


def median_of(xs):
    xs = [x for x in xs if x]
    return statistics.median(xs) if len(xs) >= 3 else None


def dcf(fcfps, g0, r, years):
    """每股自由現金流折現：成長率由 g0 逐年線性降到永續成長率。"""
    pv, f = 0.0, fcfps
    for t in range(1, years + 1):
        f *= 1 + g0 + (TG - g0) * (t - 1) / years
        pv += f / (1 + r) ** t
    return pv + f * (1 + TG) / (r - TG) / (1 + r) ** years


# ---- 股票名單 ---------------------------------------------------------------
def load_rows():
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def save_rows(rows):
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["market", "symbol", "name", "group"])
        w.writeheader()
        w.writerows(rows)


def refresh_us(rows):
    """自動更新美國前 50 大名單；失敗就沿用現有名單。"""
    try:
        import io
        import pandas as pd
        from curl_cffi import requests
        html = requests.get(US_URL, impersonate="chrome", timeout=30).text
        t = pd.read_html(io.StringIO(html))[0].head(50)
        old = {r["symbol"]: r for r in rows if r["market"] == "US"}
        new = []
        for _, x in t.iterrows():
            sym = str(x["Ticker"]).strip().replace(".", "-")
            o = old.get(sym)
            new.append({"market": "US", "symbol": sym,
                        "name": o["name"] if o else str(x["Company"]),
                        "group": o["group"] if o else "mature"})
        if len(new) != 50:
            raise ValueError("表格列數不是 50")
        added = [r["symbol"] for r in new if r["symbol"] not in old]
        dropped = [s for s in old if s not in {r["symbol"] for r in new}]
        if added or dropped:
            print("美國名單有變動  新增:", added, " 移出:", dropped)
            print("新增的股票預設分類為 mature，可到 stocks.csv 修改 group 欄。")
        return [r for r in rows if r["market"] != "US"] + new
    except Exception as e:
        print("美國名單自動更新失敗，沿用現有名單：", e)
        return rows


# ---- 抓資料 -----------------------------------------------------------------
def hist_multiples(tk):
    """用年度財報與月線股價，推算這家公司過去幾年的本益比、股價淨值比中位數。失敗就回傳空 dict。"""
    out = {}
    try:
        inc, bal = tk.income_stmt, tk.balance_sheet
        px = tk.history(period="6y", interval="1mo")["Close"]
        px.index = px.index.tz_localize(None)

        def row(df, names):
            for nm in names:
                if nm in df.index:
                    return df.loc[nm]
            return None

        def price_at(d):
            return n(px.asof(d.tz_localize(None) if getattr(d, "tzinfo", None) else d), 1e-4)

        eps = row(inc, ["Diluted EPS", "Basic EPS"])
        eq = row(bal, ["Stockholders Equity", "Common Stock Equity"])
        sh = row(bal, ["Ordinary Shares Number", "Share Issued"])
        pes, pbs = [], []
        if eps is not None:
            for d in inc.columns:
                e, p = n(eps.get(d), 1e-6), price_at(d)
                if e and p and 3 <= p / e <= 80:
                    pes.append(p / e)
        if eq is not None and sh is not None:
            for d in bal.columns:
                e, s_, p = n(eq.get(d), 1), n(sh.get(d), 1), price_at(d)
                if e and s_ and p and 0.2 <= p / (e / s_) <= 25:
                    pbs.append(p / (e / s_))
        if len(pes) >= 3:
            out["pe"] = round(statistics.median(pes), 2)
        if len(pbs) >= 3:
            out["pb"] = round(statistics.median(pbs), 2)
    except Exception:
        pass
    return out


def fetch_info(sym):
    import yfinance as yf
    err = None
    for _ in range(3):
        try:
            tk = yf.Ticker(sym)
            info = tk.info or {}
            if has_price(info):
                info["_hist"] = hist_multiples(tk)
            return info
        except Exception as e:
            err = e
            time.sleep(2)
    print("   失敗：", err)
    return {}


def has_price(info):
    return bool(info.get("currentPrice") or info.get("regularMarketPrice"))


def fetch_all(rows, use_cache):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if use_cache and os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("date") == today:
            print("使用今天的 cache.json")
            return c["info"]
    infos = {}
    for i, r in enumerate(rows, 1):
        sym = r["symbol"]
        print(f"[{i}/{len(rows)}] {sym} {r['name']}", flush=True)
        info = fetch_info(sym)
        if not has_price(info) and sym.endswith(".TW"):
            alt = fetch_info(sym[:-3] + ".TWO")  # 上櫃股票改試 .TWO
            info = alt if has_price(alt) else info
        infos[sym] = info
        time.sleep(0.4)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"date": today, "info": infos}, f, ensure_ascii=False, default=str)
    return infos


# ---- 估值 -------------------------------------------------------------------
def peers_of(sym, univ):
    """同產業的比較公司，優先同市場、市值相近。"""
    row, info = univ[sym]
    sec = info.get("sector")
    if not sec:
        return []
    same = [(s, v) for s, v in univ.items() if s != sym and v[1].get("sector") == sec]
    local = [x for x in same if x[1][0]["market"] == row["market"]]
    pool = local if len(local) >= 3 else same
    mc = n(info.get("marketCap"), 1) or 1
    pool.sort(key=lambda x: abs(math.log((n(x[1][1].get("marketCap"), 1) or 1) / mc)))
    return pool


def value_stock(row, info, pool):
    tw = row["market"] == "TW"
    tk = row["symbol"].replace(".TWO", "").replace(".TW", "")
    head = {"tk": tk, "name": row["name"], "mk": row["market"], "cur": "NT$" if tw else "US$"}
    sector = info.get("sector")
    ind = SECTOR_ZH.get(sector, sector or "—")
    price = (n(info.get("currentPrice"), 1e-4) or n(info.get("regularMarketPrice"), 1e-4)
             or n(info.get("previousClose"), 1e-4))
    if not price:
        return dict(head, ok=False, ind=ind, note="抓不到股價")

    label, why, W = GROUPS[row["group"]]
    rf, erp, rlo, rhi = RATES[row["market"]]
    r = clamp(rf + (n(info.get("beta"), 0.1, 3) or 1.0) * erp, rlo, rhi)
    shares, fcf = n(info.get("sharesOutstanding"), 1), n(info.get("freeCashflow"), 1)
    fcfps = fcf / shares if shares and fcf else None
    rate = n(info.get("dividendRate"), 1e-9)
    up = 1 + r - (rate / price if rate else 0.0)   # 把現值推到一年後
    gs = [clamp(x, -0.05, 0.25) for x in (n(info.get("revenueGrowth")), n(info.get("earningsGrowth")))
          if x is not None]
    g0 = sum(gs) / len(gs) if gs else None
    feps, bv, roe = n(info.get("forwardEps"), 1e-9), n(info.get("bookValue"), 1e-9), n(info.get("returnOnEquity"))
    pe_med = median_of([n(i.get("forwardPE"), 5, 60) for _, (_, i) in pool])
    pb_med = median_of([n(i.get("priceToBook"), 0.3, 15) for _, (_, i) in pool])
    na, tm = n(info.get("numberOfAnalystOpinions")), n(info.get("targetMeanPrice"), 1e-4)
    tmed = n(info.get("targetMedianPrice"), 1e-4)   # 中位數不受最高、最低兩端極端值影響
    tgt = tmed or tm
    have_t = bool(na and na >= MIN_ANALYSTS)

    m = {}
    thin = ""
    if "pe" in W and feps and pe_med:
        own = price / feps
        m["pe"] = feps * clamp(pe_med, 0.7 * own, 1.4 * own)
    if "pb" in W and bv:
        if row["group"] != "bank" and pb_med:
            own = price / bv
            m["pb"] = bv * clamp(pb_med, 0.7 * own, 1.4 * own)
        elif roe is not None or row["group"] == "bank":
            # 銀行，或同業樣本不足時：用股東權益報酬率推算合理的股價淨值比
            rc = clamp(roe if roe is not None else 0.10, 0.04, 0.15)
            m["pb"] = bv * (1 + rc * 0.6) * clamp((rc - TG) / (r - TG), 0.6, 2.5)
            if row["group"] != "bank":
                thin = "同業樣本不足，股價淨值比法改用股東權益報酬率推算"
    if "dcf" in W and fcfps and g0 is not None:
        m["dcf"] = dcf(fcfps, clamp(g0, 0, 0.12), r, 5) * up
    if "gdcf" in W and fcfps and g0 is not None:
        m["gdcf"] = dcf(fcfps, clamp(g0, 0.03, 0.40), r, 10) * up
    if "ddm" in W and rate:
        gg = clamp(g0 if g0 is not None else 0.02, 0, 0.04)
        m["ddm"] = rate * (1 + gg) / (r - gg) * up
    hist = info.get("_hist") or {}
    hpe_m, hpb_m = n(hist.get("pe"), 3, 80), n(hist.get("pb"), 0.2, 25)
    if "hpe" in W and feps and hpe_m:
        m["hpe"] = feps * hpe_m
    if "hpb" in W and bv and hpb_m:
        m["hpb"] = bv * (1 + clamp(roe if roe is not None else 0.08, 0.0, 0.15) * 0.6) * hpb_m
    ebitda, own_ev = n(info.get("ebitda"), 1), n(info.get("enterpriseToEbitda"), 2, 60)
    ev_med = median_of([n(i.get("enterpriseToEbitda"), 2, 40) for _, (_, i) in pool])
    if "ev" in W and ebitda and shares and ev_med:
        mult = clamp(ev_med, 0.7 * own_ev, 1.4 * own_ev) if own_ev else clamp(ev_med, 4, 25)
        fwd = ebitda * (1 + clamp(g0 if g0 is not None else 0.0, 0, 0.10))
        eq_v = fwd * mult - (n(info.get("totalDebt")) or 0.0) + (n(info.get("totalCash")) or 0.0)
        if eq_v > 0:
            m["ev"] = eq_v / shares
    if "target" in W and tgt and have_t:
        m["target"] = tgt

    drop = [k for k, v in m.items() if not (0.2 * price <= v <= 5 * price)]
    for k in drop:
        del m[k]
    capped = []
    for k, v in list(m.items()):          # 單一方法與現價差距限制在正負 CAP 內
        c = clamp(v, (1 - CAP) * price, (1 + CAP) * price)
        if c != v:
            m[k] = c
            capped.append(k)
    note = "；".join(x for x in (
        thin if "pb" in m else "",
        "已排除數值異常的方法：" + "、".join(NAMES[k] for k in drop) if drop else "",
        "已限幅（原估值與現價差距超過 50%）：" + "、".join(NAMES[k] for k in capped) if capped else "") if x)
    if len(m) < 2:
        return dict(head, ok=False, ind=ind,
                    note="可用的估值方法不足兩種（" + "、".join(NAMES[k] for k in m) + "）" if m else "缺少估值所需的財務資料")

    tot = sum(W[k] for k in m)
    base = sum(W[k] * v for k, v in m.items()) / tot
    vals = list(m.values())
    # 悲觀、樂觀只看各方法的高低，不直接採用分析師最高、最低目標價（兩端極端值）
    bear = clamp(min(vals), 0.6 * base, 0.92 * base)
    bull = clamp(max(vals), 1.08 * base, 1.6 * base)

    # 信賴度：可用方法數、方法間一致程度、分析師覆蓋、同業樣本（最高 100 分）
    spread = (max(vals) - min(vals)) / base
    warn = ""
    if capped:
        warn = "有估值方法與現價差距超過 50%，已限制在正負 50% 內，實際差距可能更大，請特別謹慎看待。"
    elif abs(base / price - 1) > 0.3:
        warn = "估值與現價差距超過 30%，請檢查各方法的假設是否合理。"
    score = (min(len(m), 4) * 10 + 30 * (1 - clamp(spread / 0.8, 0, 1)) - 5 * len(drop)
             - 8 * len(capped) - (5 if warn else 0))
    parts = [f"可用方法 {len(m)} 種", f"方法間差距 {spread * 100:.0f}%"]
    if "target" in m:
        score += 20 if na >= 20 else 15 if na >= 10 else 8
        parts.append(f"分析師 {int(na)} 位")
        if tm and tmed and abs(tm / tmed - 1) > 0.15:
            score -= 5
            parts.append("分析師目標價分布偏斜")
    if "pe" in m or ("pb" in m and row["group"] != "bank" and not thin):
        score += 10
        parts.append("有同業比較")
    if "hpe" in m or "hpb" in m:
        score += 5
        parts.append("有自身歷史比較")
    if thin and "pb" in m:
        parts.append("同業樣本不足")
    if drop:
        parts.append(f"排除 {len(drop)} 個異常方法")
    if capped:
        parts.append(f"限幅 {len(capped)} 個方法")
    score = int(round(clamp(score, 0, 100)))
    conf = {"score": score, "level": "高" if score >= 70 else "中" if score >= 45 else "低", "parts": parts}

    def mult(i):
        return [rnd(n(i.get("forwardPE"), 0, 500)), rnd(n(i.get("priceToBook"), 0, 500)),
                rnd(n(i.get("priceToSalesTrailing12Months"), 0, 500))]

    peers = [[v[0]["name"]] + mult(v[1]) for _, v in pool[:3]] + [[row["name"]] + mult(info)]
    return dict(
        head, ok=True, ind=ind, model=label, why=why, price=round(price, 2), note=note,
        sc=[{"n": "悲觀", "p": PROBS[0], "v": round(bear, 2)},
            {"n": "中性", "p": PROBS[1], "v": round(base, 2)},
            {"n": "樂觀", "p": PROBS[2], "v": round(bull, 2)}],
        methods=[{"n": NAMES[k], "v": round(v, 2), "w": round(W[k] / tot, 3)} for k, v in m.items()],
        peers=peers, conf=conf, warn=warn)


def build(rows, infos):
    univ = {r["symbol"]: (r, infos.get(r["symbol"]) or {}) for r in rows}
    return [value_stock(r, univ[r["symbol"]][1], peers_of(r["symbol"], univ)) for r in rows]


# ---- ETF：成分股加權估值 ------------------------------------------------------
def load_holdings():
    if not os.path.exists(HOLD_PATH):
        return []
    with open(HOLD_PATH, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def fetch_etf_price(sym):
    import yfinance as yf
    for _ in range(3):
        try:
            info = yf.Ticker(sym).info or {}
            return (n(info.get("regularMarketPrice"), 1e-4) or n(info.get("currentPrice"), 1e-4)
                    or n(info.get("previousClose"), 1e-4))
        except Exception:
            time.sleep(2)
    return None


def build_etfs(rows, stocks, holds, prices):
    """把 ETF 的成分股估值，依持股權重加權平均。只計算有估值的成分股，涵蓋率 = 它們占 ETF 的比重。"""
    by_sym = {r["symbol"]: s for r, s in zip(rows, stocks)}
    out = []
    for e in ETFS:
        head = {"tk": e["tk"], "name": e["name"], "mk": e["mk"], "cur": e["cur"], "ind": e["ind"], "etf": True}
        hs = [h for h in holds if h["etf"] == e["tk"]]
        used = [(by_sym[h["symbol"]], n(h["weight"], 0) or 0.0) for h in hs
                if h["symbol"] in by_sym and by_sym[h["symbol"]]["ok"]]
        tot = sum(w for _, w in used)
        price = prices.get(e["tk"])
        if not price:
            out.append(dict(head, ok=False, note="抓不到 ETF 價格"))
            continue
        if tot < 20:
            out.append(dict(head, ok=False, note=f"有估值的成分股只占 {tot:.0f}%，涵蓋率過低"))
            continue

        def wavg(f):
            return sum(w * f(s) for s, w in used) / tot

        rets = [wavg(lambda s, i=i: s["sc"][i]["v"] / s["price"] - 1) for i in range(3)]
        cov = min(tot / 100.0, 1.0)
        avg_conf = wavg(lambda s: s["conf"]["score"])
        score = avg_conf * clamp(cov / 0.8, 0.4, 1.0)
        warn = "涵蓋率不足 60%，沒有估值的持股不在計算內，結果僅供參考。" if cov < 0.6 else ""
        score = int(round(clamp(score - (5 if warn else 0), 0, 100)))
        asof = max((h["asof"] for h in hs), default="")
        notes = sorted({h["note"] for h in hs if h.get("note")})
        top = sorted(used, key=lambda x: -x[1])[:10]
        out.append(dict(
            head, ok=True, model="成分股加權估值", price=round(price, 2), warn=warn, note="；".join(notes),
            why="把 ETF 持有的成分股，依持股權重加權平均各自的估值結果。只計算我們有估值的成分股，涵蓋率代表這些成分股占 ETF 的比重。",
            sc=[{"n": nm, "p": PROBS[i], "v": round(price * (1 + rets[i]), 2)} for i, nm in enumerate(("悲觀", "中性", "樂觀"))],
            methods=[], peers=[],
            holds=[[s["name"], round(w, 2), round(s["sc"][1]["v"] / s["price"] - 1, 4)] for s, w in top],
            conf={"score": score, "level": "高" if score >= 70 else "中" if score >= 45 else "低",
                  "parts": [f"涵蓋率 {cov * 100:.0f}%", f"成分股平均信賴度 {avg_conf:.0f}%", f"持股權重日期 {asof}"]}))
    return out


def main():
    rows = refresh_us(load_rows())
    save_rows(rows)
    stocks = build(rows, fetch_all(rows, "--cache" in sys.argv))
    prices = {e["tk"]: fetch_etf_price(e["yf"]) for e in ETFS}
    stocks = stocks + build_etfs(rows, stocks, load_holdings(), prices)
    data = {"asof": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"), "sample": False, "stocks": stocks}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("window.DATA=" + json.dumps(data, ensure_ascii=False) + ";\n")
    bad = [f"{s['tk']}（{s['note']}）" for s in stocks if not s["ok"]]
    print(f"\n完成：共 {len(stocks)} 檔，成功估值 {len(stocks) - len(bad)} 檔，資料不足 {len(bad)} 檔")
    for b in bad:
        print("  資料不足：", b)
    print("已產生", OUT_PATH, "，請用瀏覽器開啟 dashboard.html")


if __name__ == "__main__":
    main()
