# ruff: noqa
"""golden.py record|check  -- candidate lists for 22 variants on dev_frame slice 46000:58000."""
import json
import pickle
import sys
import time

sys.path[:0] = ["src", ".", "research/runners"]
GOLD = r"C:/Users/yanni/AppData/Local/Temp/claude/C--Users-yanni--codex--chatgpt-projects-g-p-6ab858dc4d7c8191818034e19b772625-trader/cab8f671-424c-418c-a63b-24e9b2c6852d/scratchpad/golden.pkl"

if __name__ == "__main__":
    mode = sys.argv[1]
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    from alpha.common.dataset import load_research_dataset
    from alpha.common.protocol import Partition, SplitPlan

    import ar2_compare as r
    cfg = json.load(open("research/configs/ar2_phase2.json"))
    plan = SplitPlan(**{k: Partition(k, *v) for k, v in cfg["splits"].items()})
    ds = load_research_dataset("data/ar1_ger40")
    df = r.dev_frame(ds.frame, plan).iloc[46000:58000].reset_index(drop=True)
    lab = r.Lab(cfg, df, plan)
    variants = r.discover_variants()
    t = time.time()
    got = lab.candidates_many(variants, workers)
    print("generate", round(time.time() - t, 1), "s", {k: len(v) for k, v in got.items()}, flush=True)
    if mode == "record":
        with open(GOLD, "wb") as fh:
            pickle.dump(got, fh)
        print("RECORDED")
    else:
        with open(GOLD, "rb") as fh:
            gold = pickle.load(fh)
        bad = [k for k in gold if gold[k] != got.get(k)]
        assert set(gold) == set(got), (set(gold) ^ set(got))
        print("MISMATCH", bad) if bad else print("GOLDEN IDENTICAL", len(gold), "variants",
                                                   sum(map(len, gold.values())), "candidates")
