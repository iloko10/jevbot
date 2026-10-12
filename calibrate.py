"""Calibration + performance report from the logged (snapshot, answers, action) triples.

Usage:  python calibrate.py [path/to/jevbot.db] [--horizon 30]

Checks: when the judge says P(up)=0.8, does price actually go up ~80% of the time?
Prints Brier score, log loss, Expected Calibration Error (ECE), a reliability table,
plus fill quality (adverse selection). Run it separately for stub vs Jev data to compare.
"""
import argparse
import bisect
import json
import math
import sqlite3
from collections import defaultdict


def load(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    dec = db.execute("SELECT ts, source, model, mid, equity, snapshot, answers FROM decisions WHERE answers != '{}' ORDER BY ts").fetchall()
    fills = db.execute("SELECT * FROM fills ORDER BY ts").fetchall()
    try:  # last fee-check row per book
        books = db.execute("""SELECT b.* FROM book_stats b
            JOIN (SELECT book, MAX(ts) AS ts FROM book_stats GROUP BY book) l ON b.book = l.book AND b.ts = l.ts
            ORDER BY b.half_spread_bps""").fetchall()
    except sqlite3.OperationalError:
        books = []
    return dec, fills, books


def future_mid(ts_list, mids, t):
    i = bisect.bisect_left(ts_list, t)
    return mids[i] if i < len(ts_list) else None


def ece(pairs, bins=10):
    """pairs = [(confidence, correct 0/1)]"""
    b = defaultdict(list)
    for c, ok in pairs:
        b[min(int(c * bins), bins - 1)].append((c, ok))
    total = len(pairs)
    e, table = 0.0, []
    for k in sorted(b):
        xs = b[k]
        conf = sum(c for c, _ in xs) / len(xs)
        acc = sum(ok for _, ok in xs) / len(xs)
        e += len(xs) / total * abs(conf - acc)
        table.append((k / bins, (k + 1) / bins, len(xs), conf, acc))
    return e, table


def report(dec, fills, books, horizon):
    ts_list = [r["ts"] for r in dec]
    mids = [r["mid"] for r in dec]
    by_source = defaultdict(lambda: {"brier": [], "logloss": [], "top": [], "toxic": []})

    for r in dec:
        fm = future_mid(ts_list, mids, r["ts"] + horizon)
        if fm is None:
            break
        snap, ans = json.loads(r["snapshot"]), json.loads(r["answers"])
        move_bps = (fm / r["mid"] - 1) * 1e4
        thr = max(snap["book"]["spread_bps"], 0.5)
        outcome = "up" if move_bps > thr else "down" if move_bps < -thr else "neutral"
        key = f'{r["source"]}:{r["model"]}'
        d = ans.get("direction")
        if d:
            p = d["probabilities"]
            by_source[key]["brier"].append(sum((p.get(k, 0) - (k == outcome)) ** 2 for k in ("up", "down", "neutral")))
            by_source[key]["logloss"].append(-math.log(max(p.get(outcome, 0), 1e-9)))
            by_source[key]["top"].append((d["confidence"], int(d["choice"] == outcome)))
        t = ans.get("toxic_flow")
        if t:
            flow = snap["flow_60s"]["aggr_buy_ratio"] - 0.5
            ran_over = (flow > 0 and move_bps > thr) or (flow < 0 and move_bps < -thr)
            by_source[key]["toxic"].append((t["noul"], int(ran_over)))

    print(f"\n=== Calibration (horizon {horizon}s, {len(dec)} decisions) ===")
    for key, m in by_source.items():
        n = len(m["brier"])
        if not n:
            continue
        e, table = ece(m["top"])
        print(f"\n[{key}]  n={n}")
        print(f"  direction  Brier={sum(m['brier'])/n:.4f}  (always-1/3 baseline=0.6667)"
              f"  logloss={sum(m['logloss'])/n:.4f}  (baseline=1.0986)  ECE={e:.4f}")
        print("  reliability (top choice):  conf-bin   n     avg-conf  hit-rate")
        for lo, hi, cnt, conf, acc in table:
            print(f"                            {lo:.1f}-{hi:.1f}  {cnt:6d}  {conf:.3f}     {acc:.3f}")
        tx = m["toxic"]
        if tx:
            brier_t = sum((p - y) ** 2 for p, y in tx) / len(tx)
            base = sum(y for _, y in tx) / len(tx)
            print(f"  toxic_flow Brier={brier_t:.4f}  (constant-rate baseline={base*(1-base):.4f}, base rate={base:.3f})")

    print(f"\n=== Fills per book ({len(fills)} total) ===")
    groups = defaultdict(list)
    for f in fills:
        groups[f["book"] if "book" in f.keys() else "main"].append(f)
    for name, fl in groups.items():
        adv = []
        for f in fl:
            fm = future_mid(ts_list, mids, f["ts"] + 10)
            if fm is None:
                continue
            sign = 1 if f["side"] == "bid" else -1
            adv.append(sign * (fm - f["px"]) / f["px"] * 1e4)  # >0 good, <0 = we got picked off
        maker = sum(1 for f in fl if f["maker"])
        vol = sum(f["px"] * f["sz"] for f in fl)
        fees = sum(f["fee"] for f in fl)
        line = f"  [{name}] fills={len(fl)} (taker {len(fl) - maker})  volume=${vol:,.0f}  fees=${fees:.2f}"
        if adv:
            line += (f"  | markout after 10s {sum(adv)/len(adv):+.2f} bps vs fee {fees/vol*1e4:.2f} bps"
                     if vol else "")
        print(line)
    print("  (markout = what a fill is worth 10s later. It must be bigger than the fee, or the book loses.)")

    if books:
        print("\n=== Fees check (latest, per $ traded) ===")
        print("  book              spread  fills    volume$   earned  fees   net    PnL$    tier  result")
        for b in books:
            if not b["fills"]:
                res = "no fills yet"
            elif (b["net_bps"] or 0) > 0:
                res = "beats fees"
            elif (b["earned_bps"] or 0) > 0:
                res = "loses to fees"
            else:
                res = "loses even before fees"
            fmt = lambda x: f"{x:+.2f}" if x is not None else "  -  "
            print(f"  {b['book']:<17} {b['half_spread_bps']:>4g}bp {b['fills']:>6} {b['volume']:>11,.0f}  "
                  f"{fmt(b['earned_bps'])}  {fmt(b['fee_bps'])}  {fmt(b['net_bps'])}  {b['pnl']:>+7.2f}  "
                  f"{b['tier']:>3}   {res}{'  (stopped by kill switch)' if b['killed'] else ''}")
        print("  bps = cents per $100 traded. 'earned' is before fees.")
    if dec:
        hrs = (dec[-1]['ts'] - dec[0]['ts']) / 3600
        print(f"\n=== Equity ===\n  {dec[0]['equity']:.2f} -> {dec[-1]['equity']:.2f} "
              f"({dec[-1]['equity'] - dec[0]['equity']:+.2f} USD over {hrs:.1f}h)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="data/jevbot.db")
    ap.add_argument("--horizon", type=float, default=30)
    a = ap.parse_args()
    report(*load(a.db), a.horizon)
