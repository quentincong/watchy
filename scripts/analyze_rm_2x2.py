"""2x2 RM analysis for compare_rm_pm_qwen.py output: model (deepseek / qwen) x format (free / structured), 2 reps.

Usage on the VPS:  PYTHONPATH=~/watchy:~/abtest_qwen $PY analyze_rm_2x2.py rm2x2_ds.jsonl rm2x2_qw.jsonl
"""
import json
import statistics as st
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/home/watchy/abtest_qwen")
from compare_rm_pm_qwen import rating_of  # noqa: E402

SCORE = {"Sell": -2, "Underweight": -1, "Hold": 0, "Overweight": 1, "Buy": 2}
CELLS = {
    "deepseek": ("DeepSeek", "free"),
    "deepseek:struct": ("DeepSeek", "struct"),
    "qwen:4000:free": ("Qwen", "free"),
    "qwen:4000:json": ("Qwen", "struct"),
}
rows = []
for f in sys.argv[1:]:
    for line in open(f):
        r = json.loads(line)
        if r.get("error") or r["node"] != "RM" or r["arm"] not in CELLS:
            continue
        r["rating"] = rating_of(r["text"])
        rows.append(r)

cell = defaultdict(dict)          # (arm) -> {(ticker, rep): row}
for r in rows:
    cell[r["arm"]][(r["ticker"], r["rep"])] = r
tickers = sorted({r["ticker"] for r in rows})

print(f"{'cell':22} {'n':>3} {'mean score':>10} {'unparsed':>8} {'r0==r1':>7} {'chars':>6}  ratings")
for arm, (m, fmt) in CELLS.items():
    rs = list(cell[arm].values())
    sc = [SCORE[r["rating"]] for r in rs if r["rating"] in SCORE]
    same = sum(1 for t in tickers if (t, 0) in cell[arm] and (t, 1) in cell[arm]
               and cell[arm][(t, 0)]["rating"] == cell[arm][(t, 1)]["rating"])
    pairs = sum(1 for t in tickers if (t, 0) in cell[arm] and (t, 1) in cell[arm])
    c = Counter(r["rating"] for r in rs)
    print(f"{m + ' ' + fmt:22} {len(rs):3d} {st.mean(sc):10.2f} {c.get('?', 0):8d} "
          f"{same:>3}/{pairs:<3} {st.mean(r['chars'] for r in rs):6.0f}  "
          + " ".join(f"{k}:{c[k]}" for k in ("Sell", "Underweight", "Hold", "Overweight", "Buy") if c[k]))


def tscore(arm, t):
    v = [SCORE[cell[arm][(t, k)]["rating"]] for k in (0, 1)
         if (t, k) in cell[arm] and cell[arm][(t, k)]["rating"] in SCORE]
    return st.mean(v) if v else None


def effect(a, b, label):
    d = [tscore(b, t) - tscore(a, t) for t in tickers
         if tscore(a, t) is not None and tscore(b, t) is not None]
    up = sum(1 for x in d if x > 0)
    dn = sum(1 for x in d if x < 0)
    print(f"  {label:38} mean shift {st.mean(d):+.2f}  (up {up}, down {dn}, same {len(d) - up - dn})")


print("\neffects (per-ticker mean score over reps; + = more bullish)")
effect("deepseek", "qwen:4000:free", "model, free text   DeepSeek -> Qwen")
effect("deepseek:struct", "qwen:4000:json", "model, structured  DeepSeek -> Qwen")
effect("deepseek", "deepseek:struct", "format, DeepSeek   free -> struct")
effect("qwen:4000:free", "qwen:4000:json", "format, Qwen       free -> struct")
effect("deepseek", "qwen:4000:json", "both (the planned switch)")

print("\nper ticker (r0/r1)")
print(f"{'ticker':7}" + "".join(f"{CELLS[a][0] + ' ' + CELLS[a][1]:>26}" for a in CELLS))
ab = {"Sell": "SELL", "Underweight": "UW", "Hold": "HOLD", "Overweight": "OW", "Buy": "BUY", "?": "?"}
for t in tickers:
    print(f"{t:7}" + "".join(
        f"{'/'.join(ab[cell[a][(t, k)]['rating']] for k in (0, 1) if (t, k) in cell[a]):>26}"
        for a in CELLS))
