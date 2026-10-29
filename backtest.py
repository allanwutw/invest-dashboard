# -*- coding: utf-8 -*-
"""讀取 history/ 裡每天的估值快照，對照後來的實際漲跌，產生 backtest.html。
不需要網路。快照累積的時間不夠長時，報告會顯示「資料累積中」。"""
import csv
import glob
import math
import os
import statistics
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
OUT = os.path.join(HERE, "backtest.html")
HORIZONS = [("約 3 個月", 90), ("約 6 個月", 180), ("約 12 個月", 365)]
TOL = 7        # 對照日期允許前後差幾天
MIN_N = 20     # 單日至少要有幾檔，才計算排序相關係數


def load():
    snaps = {}
    for p in sorted(glob.glob(os.path.join(HIST, "snapshot_*.csv"))):
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        if rows:
            snaps[datetime.strptime(rows[0]["date"], "%Y-%m-%d").date()] = {r["key"]: r for r in rows}
    return snaps


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r, i = [0.0] * len(xs), 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for t in range(i, j + 1):
            r[order[t]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(a, b):
    if len(a) < 3:
        return None
    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((y - mb) ** 2 for y in rb))
    if not da or not db:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / (da * db)


def pairs_for(snaps, days, etf):
    """每個起始日配上約 days 天後的快照，回傳 {起始日: [(預測潛在報酬, 實際報酬, 信賴度), ...]}"""
    dates = sorted(snaps)
    out = {}
    for d0 in dates:
        target = d0 + timedelta(days=days)
        near = [d for d in dates if abs((d - target).days) <= TOL]
        if not near:
            continue
        d1 = min(near, key=lambda d: abs((d - target).days))
        lst = []
        for k, r0 in snaps[d0].items():
            r1 = snaps[d1].get(k)
            if not r1 or (r0["etf"] == "1") != etf:
                continue
            p0, p1 = float(r0["price"]), float(r1["price"])
            real = p1 / p0 - 1 if p0 > 0 else None
            if real is None or abs(real) > 3:      # 疑似股票分割或資料錯誤，略過
                continue
            lst.append((float(r0["fair"]) / p0 - 1, real, float(r0["conf"])))
        if lst:
            out[d0] = lst
    return out


def summarize(by):
    ics, spreads, preds, reals, n = [], [], [], [], 0
    for lst in by.values():
        n += len(lst)
        preds += [x[0] for x in lst]
        reals += [x[1] for x in lst]
        if len(lst) >= MIN_N:
            ic = spearman([x[0] for x in lst], [x[1] for x in lst])
            if ic is not None:
                ics.append(ic)
            srt = sorted(lst, key=lambda x: x[0])
            k = len(srt) // 3
            spreads.append(statistics.mean(x[1] for x in srt[-k:]) - statistics.mean(x[1] for x in srt[:k]))
    return {"days": len(by), "n": n, "ic": statistics.mean(ics) if ics else None,
            "ic_pos": sum(1 for x in ics if x > 0) / len(ics) if ics else None,
            "spread": statistics.mean(spreads) if spreads else None,
            "pred": statistics.mean(preds) if preds else None, "real": statistics.mean(reals) if reals else None}


def pct(x, sign=False):
    return "–" if x is None else f"{x * 100:+.1f}%" if sign else f"{x * 100:.0f}%"


def build_html(snaps):
    days = sorted(snaps)
    head = (f"目前累積 {len(days)} 天快照（{days[0]} 到 {days[-1]}）。" if days else "目前還沒有任何快照。")
    rows = ""
    for label, d in HORIZONS:
        s = summarize(pairs_for(snaps, d, False))
        if s["days"] == 0 or s["ic"] is None:
            rows += f"<tr><td>{label}</td><td colspan=\"6\">資料累積中，還沒有間隔夠久的快照可以對照</td></tr>"
        else:
            rows += (f"<tr><td>{label}</td><td>{s['days']}</td><td>{s['n']}</td><td>{s['ic']:+.2f}</td><td>{pct(s['ic_pos'])}</td>"
                     f"<td>{pct(s['spread'], True)}</td><td>{pct(s['pred'], True)} / {pct(s['real'], True)}</td></tr>")
    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>驗證紀錄</title>
<style>
:root{{--bg:#f2f4f3;--card:#fff;--ink:#1b2421;--mute:#66726d;--line:#dde3e0;--accent:#0e5c55;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121816;--card:#1a2220;--ink:#e8eeeb;--mute:#93a19b;--line:#2c3834;--accent:#6fc2b8;color-scheme:dark}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 "Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif}}
main{{max-width:900px;margin:0 auto;padding:20px 16px 48px}}h1{{font-size:26px;margin:8px 0}}
.box{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:14px 0}}
.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:15px}}
th,td{{text-align:right;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}th{{color:var(--mute);font-weight:500}}
small,li{{color:var(--mute)}}a{{color:var(--accent)}}
</style></head><body><main>
<h1>驗證紀錄</h1>
<div class="box">{head}每天的估值會存成快照，這裡把「當時算出的潛在報酬」和「之後實際的漲跌」對照。<a href="./">回儀表板</a></div>
<div class="box scroll"><table>
<tr><th>對照期間</th><th>快照天數</th><th>配對筆數</th><th>排序相關</th><th>相關為正的比例</th><th>前三分之一減後三分之一</th><th>預測平均 / 實際平均</th></tr>
{rows}
</table></div>
<div class="box"><b>怎麼看</b><ul>
<li><b>排序相關（IC）</b>：同一天裡，潛在報酬高的股票，後來是不是也漲得比較多。大於 0 代表有效，0 附近代表沒有預測力，小於 0 代表反向。通常要持續高於 0.05 才算有意義。</li>
<li><b>前三分之一減後三分之一</b>：潛在報酬最高的三分之一，和最低的三分之一，後來平均漲幅的差距。為正代表模型挑得對。</li>
<li><b>預測平均 / 實際平均</b>：模型平均預測的報酬，和實際平均報酬。差很多，代表整體偏樂觀或偏悲觀。</li>
</ul>
<small>限制：只比較股價漲跌，不含股利和匯率；不同起始日的樣本互相重疊，且同一時期的股票會一起受大盤影響，所以資料天數不多時，請當作參考，不要過度解讀。分析師調整、模型修改之後，新舊結果混在一起，也會影響判讀。</small></div>
</main></body></html>"""


def main():
    snaps = load()
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(build_html(snaps))
    print("已產生", OUT, "，快照天數:", len(snaps))


if __name__ == "__main__":
    main()
