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
NAMES = {"pe": "本益比法", "pb": "股價淨值比法", "dcf": "現金流折現",
         "gdcf": "成長型現金流折現", "ddm": "股利折現", "target": "分析師目標價（中位數）"}
# 分類 -> (模型名稱, 說明, 各方法權重)
GROUPS = {
    "bank": ("股價淨值比與本益比",
             "金融股獲利常受一次性因素影響，淨值較穩定，所以以股價淨值比為主，再對照同業本益比與分析師目標價。",
             {"pb": .5, "pe": .2, "target": .3}),
    "mature": ("本益比與現金流折現",
               "獲利與現金流穩定的成熟公司，以同業本益比與自由現金流折現為主，並參考分析師目標價。",
               {"pe": .35, "dcf": .35, "target": .3}),
    "defensive": ("現金流折現與股利折現",
                  "需求穩定、股利持續的防禦型公司，以現金流折現與股利折現為主，輔以本益比與分析師目標價。",
                  {"dcf": .3, "ddm": .2, "pe": .2, "target": .3}),
    "growth": ("成長型現金流折現",
               "營收與獲利仍在快速成長，單看目前本益比容易失真，所以以拉長期間的成長型現金流折現為主。",
               {"gdcf": .4, "pe": .2, "target": .4}),
    "cyclical": ("股價淨值比與本益比（景氣循環）",
                 "獲利隨景氣大起大落，單一年度獲利不可靠，所以用同業股價淨值比與預估本益比，並提高分析師目標價的參考比重。",
                 {"pb": .3, "pe": .2, "target": .5}),
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
def fetch_info(sym):
    import yfinance as yf
    err = None
    for _ in range(3):
        try:
            return yf.Ticker(sym).info or {}
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


def main():
    rows = refresh_us(load_rows())
    save_rows(rows)
    stocks = build(rows, fetch_all(rows, "--cache" in sys.argv))
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
