# ruff: noqa
"""equiv.py dump|check SRC_ROOT : derived tables on the FULL dev frame, old vs new code."""
import json, pickle, sys, time
mode, src = sys.argv[1], sys.argv[2]
sys.path[:0] = [src, ".", "research/runners"]
import pandas as pd
from alpha.common.dataset import load_research_dataset
from alpha.common.protocol import Partition, SplitPlan
from alpha.timeframe import MtfView
from alpha.regime import classify_regime
from alpha.context import classify_context
S = r"C:/Users/yanni/AppData/Local/Temp/claude/C--Users-yanni--codex--chatgpt-projects-g-p-6ab858dc4d7c8191818034e19b772625-trader/cab8f671-424c-418c-a63b-24e9b2c6852d/scratchpad/derived.pkl"
cfg = json.load(open("research/configs/ar2_phase2.json"))
plan = SplitPlan(**{k: Partition(k, *v) for k, v in cfg["splits"].items()})
ds = load_research_dataset("data/ar1_ger40")
import alpha.timeframe as tfm; print("timeframe module:", tfm.__file__)
local = pd.DatetimeIndex(ds.frame["ts"]).tz_convert("Europe/Berlin").normalize().tz_localize(None)
df = ds.frame.loc[local <= pd.Timestamp(plan.validation.end)].reset_index(drop=True)
t = time.time(); v = MtfView(df); tv = time.time() - t
t = time.time(); reg = classify_regime(df); tr = time.time() - t
t = time.time(); ctx = classify_context(df); tc = time.time() - t
print(f"bars={len(df)} view={tv:.1f}s regime={tr:.1f}s context={tc:.1f}s", flush=True)
out = dict(m15=v.m15, h1=v.h1, a15=v.m15_alignment, a1=v.h1_alignment, levels=v._levels, reg=reg, ctx=ctx)
if mode == "dump":
    pickle.dump(out, open(S, "wb")); print("DUMPED")
else:
    old = pickle.load(open(S, "rb"))
    import numpy as np
    for k in ("m15", "h1", "reg", "ctx"):
        pd.testing.assert_frame_equal(old[k], out[k], check_exact=True, check_freq=True)
        assert list(old[k].dtypes) == list(out[k].dtypes), k
        # object columns: identical python types element-wise
        for c in old[k].columns:
            if old[k][c].dtype == object:
                assert [type(x) for x in old[k][c]] == [type(x) for x in out[k][c]], (k, c)
    assert np.array_equal(old["a15"], out["a15"]) and np.array_equal(old["a1"], out["a1"])
    assert old["levels"] == out["levels"]
    # at(): new fast path vs original iloc(...).copy() semantics, every position
    for p in range(len(df)):
        s = v.at(p)
        ref = v.m5.iloc[p].copy()
        assert s.m5.equals(ref) and s.m5.dtype == ref.dtype and s.m5.name == ref.name and s.m5.index.equals(ref.index), p
        for name, al in (("m15", v.m15_alignment), ("h1", v.h1_alignment)):
            got = getattr(s, name)
            if al[p] < 0:
                assert got is None
            else:
                r2 = getattr(v, name).iloc[al[p]].copy()
                assert got.equals(r2) and got.dtype == r2.dtype and got.name == r2.name, (p, name)
                assert [type(x) for x in got] == [type(x) for x in r2], (p, name)
    # copies are independent
    a = v.at(100); a.m5["close"] = -1.0
    if a.h1 is not None: a.h1["close"] = -1.0
    b = v.at(100); assert b.m5["close"] != -1.0 and (b.h1 is None or b.h1["close"] != -1.0)
    print("DERIVED + at() IDENTICAL")
