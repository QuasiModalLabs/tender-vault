"""
Phase 2 analysis, off cached distributions only. No API calls, no new verdicts.

WHAT CHANGES FROM PHASE 1. Phase 1's "Jev admits" was the top choice being a
profile option, and its rule weighed recall and precision alike. Here admit is
PROBABILITY MASS: the summed probability over the profile options >= t. The
reason is a cost asymmetry stated by the user after phase 1: a false negative
is a tender never seen, a false positive is seconds of reading. So the sweep
exists to find what recall costs, not to maximise a balanced score.

THIS IS POST HOC, and it says so. The phase 1 rule was pre-registered; nothing
here is. A threshold chosen on these 23,314 notices is scored on the same
23,314, so its recall is optimistic by construction. `split_half` measures how
optimistic: choose t on a seeded half, report what it does on the other half.
One parameter over ~900 positives per half should transfer well, but that is
now measured rather than assumed.

THE UNION reuses production's keyword matcher exactly as phase 1 did - the
`kw_admit` field on each scored row, which came from
comparator.keyword_hits -> ingest.matched_competencies.

RECALL IS AGAINST PUBLISHER CODES, which contain miscodes. Chasing recall 0.90
against those codes partly chases the publisher's errors; the disagreement
labels are the only measure of how much.
"""
from __future__ import annotations

import csv
import random
from collections import Counter

from .evaluate import SEED, wilson

# 0.05 .. 0.95, plus five points below 0.05. The low tail was added after the
# first sweep put best recall at its lowest point - a curve peaking at the edge
# of its grid was cut off, not exhausted.
THRESHOLDS = (0.005, 0.01, 0.02, 0.03, 0.04) + tuple(round(0.05 * i, 2) for i in range(1, 20))
RECALL_TARGET = 0.90

# Bins for the probability mass carried by missed publisher admits.
MASS_BINS = (0.0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.10, 0.25, 0.50, 1.0001)


def mass_histogram(misses) -> list:
    """Profile-option mass of missed admits, binned. If most sit near zero, no
    threshold recovers them."""
    out = []
    for lo, hi in zip(MASS_BINS, MASS_BINS[1:]):
        n = sum(1 for r in misses if lo <= r["p_profile"] < hi)
        out.append({"lo": lo, "hi": min(hi, 1.0), "n": n})
    return out


def neither(rows, t: float = 0.05) -> list:
    """Publisher admits found by neither Jev mass >= t nor the keyword branch."""
    return [r for r in rows if r["publisher_admit"] and r["p_profile"] < t
            and not r["kw_admit"]]


def _rate(k, n):
    return {"k": k, "n": n, "value": (k / n) if n else None, "wilson95": wilson(k, n)}


def _metrics(rows, admit) -> dict:
    tp = sum(1 for r in rows if r["publisher_admit"] and admit(r))
    pos = sum(1 for r in rows if r["publisher_admit"])
    said = sum(1 for r in rows if admit(r))
    return {"recall": _rate(tp, pos), "precision": _rate(tp, said), "admits": said}


def curve(rows) -> list:
    out = []
    kw = lambda r: r["kw_admit"]  # noqa: E731
    top = lambda r: r["jev_admit"]  # noqa: E731
    for t in THRESHOLDS:
        mass = lambda r, t=t: r["p_profile"] >= t  # noqa: E731
        union = lambda r, t=t: r["p_profile"] >= t or r["kw_admit"]  # noqa: E731
        pos = [r for r in rows if r["publisher_admit"]]
        out.append({
            "t": t,
            "jev_mass": _metrics(rows, mass),
            "union": _metrics(rows, union),
            # which method finds each publisher admit - do they fail on the same notices?
            "tp_both": sum(1 for r in pos if mass(r) and r["kw_admit"]),
            "tp_jev_only": sum(1 for r in pos if mass(r) and not r["kw_admit"]),
            "tp_kw_only": sum(1 for r in pos if r["kw_admit"] and not mass(r)),
            "tp_neither": sum(1 for r in pos if not mass(r) and not r["kw_admit"]),
            "fp_both": sum(1 for r in rows if not r["publisher_admit"] and mass(r) and r["kw_admit"]),
            "fp_jev_only": sum(1 for r in rows if not r["publisher_admit"] and mass(r) and not r["kw_admit"]),
            "fp_kw_only": sum(1 for r in rows if not r["publisher_admit"] and r["kw_admit"] and not mass(r)),
        })
    ref = {"jev_top_choice": _metrics(rows, top), "keyword": _metrics(rows, kw)}
    return out, ref


def highest_t_reaching(points, key, target=RECALL_TARGET):
    """The highest threshold whose recall still meets the target - the
    cheapest point on the grid that gets there. None if no point does."""
    ok = [p for p in points if (p[key]["recall"]["value"] or 0) >= target]
    return max(ok, key=lambda p: p["t"]) if ok else None


def split_half(rows, key="jev_mass") -> dict:
    """Choose t on half A, report half B. Seeded, split by notice."""
    ids = sorted(r["notice_id"] for r in rows)
    rng = random.Random(SEED)
    half_a = set(rng.sample(ids, len(ids) // 2))
    a = [r for r in rows if r["notice_id"] in half_a]
    b = [r for r in rows if r["notice_id"] not in half_a]
    pts_a, _ = curve(a)
    chosen = highest_t_reaching(pts_a, key)
    if chosen is None:
        return {"chosen_t": None}
    pts_b, _ = curve(b)
    on_b = next(p for p in pts_b if p["t"] == chosen["t"])
    return {"chosen_t": chosen["t"],
            "half_a": {"recall": chosen[key]["recall"], "precision": chosen[key]["precision"]},
            "half_b": {"recall": on_b[key]["recall"], "precision": on_b[key]["precision"]}}


def split_spread(rows, thresholds=THRESHOLDS, n_splits: int = 50) -> list:
    """
    How optimistic is a number read off the same data a threshold was picked
    on? For each t, split the notices in half n_splits times (seeded) and
    record half B minus half A for recall, precision and admit rate.

    A threshold here is picked on CORPUS SIZE, not recall, so there is no
    selection on the scored quantity at a fixed t; the spread is the noise any
    single-sample figure carries. `pick_on_a` separately emulates picking t on
    half A to hit a recall target and scoring it on half B, which is the
    optimism the original check was built to catch.
    """
    ids = sorted(r["notice_id"] for r in rows)
    out = {t: {"recall": [], "precision": [], "admit_rate": []} for t in thresholds}
    for k in range(n_splits):
        rng = random.Random(SEED + k)
        half_a = set(rng.sample(ids, len(ids) // 2))
        a = [r for r in rows if r["notice_id"] in half_a]
        b = [r for r in rows if r["notice_id"] not in half_a]
        for t in thresholds:
            ma = _metrics(a, lambda r, t=t: r["p_profile"] >= t)
            mb = _metrics(b, lambda r, t=t: r["p_profile"] >= t)
            for q in ("recall", "precision"):
                va, vb = ma[q]["value"], mb[q]["value"]
                if va is not None and vb is not None:
                    out[t][q].append(vb - va)
            out[t]["admit_rate"].append(mb["admits"] / len(b) - ma["admits"] / len(a))
    table = []
    for t in thresholds:
        row = {"t": t}
        for q, diffs in out[t].items():
            s = sorted(diffs)
            row[q] = {"mean_abs": sum(abs(d) for d in s) / len(s),
                      "lo": s[int(0.025 * (len(s) - 1))], "hi": s[int(0.975 * (len(s) - 1))]}
        table.append(row)
    return table


def pick_on_a(rows, target: float, n_splits: int = 50) -> dict:
    """Pick the highest t reaching `target` recall on half A; score on half B.
    Returns mean optimism (A recall minus B recall) over seeded splits."""
    ids = sorted(r["notice_id"] for r in rows)
    gaps, picks, b_recalls = [], [], []
    for k in range(n_splits):
        rng = random.Random(SEED + k)
        half_a = set(rng.sample(ids, len(ids) // 2))
        a = [r for r in rows if r["notice_id"] in half_a]
        b = [r for r in rows if r["notice_id"] not in half_a]
        ok = [t for t in THRESHOLDS
              if (_metrics(a, lambda r, t=t: r["p_profile"] >= t)["recall"]["value"] or 0) >= target]
        if not ok:
            continue
        t = max(ok)
        ra = _metrics(a, lambda r, t=t: r["p_profile"] >= t)["recall"]["value"]
        rb = _metrics(b, lambda r, t=t: r["p_profile"] >= t)["recall"]["value"]
        gaps.append(ra - rb)
        picks.append(t)
        b_recalls.append(rb)
    if not gaps:
        return {"target": target, "splits": 0}
    return {"target": target, "splits": len(gaps),
            "mean_optimism": sum(gaps) / len(gaps),
            "b_below_target": sum(1 for rb in b_recalls if rb < target) / len(b_recalls),
            "t_picked": dict(Counter(picks))}


def write_csv(points, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["t"]
    for m in ("jev_mass", "union"):
        cols += [f"{m}_recall", f"{m}_recall_lo", f"{m}_recall_hi",
                 f"{m}_precision", f"{m}_precision_lo", f"{m}_precision_hi", f"{m}_admits"]
    cols += ["tp_both", "tp_jev_only", "tp_kw_only", "tp_neither",
             "fp_both", "fp_jev_only", "fp_kw_only"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for p in points:
            row = [p["t"]]
            for m in ("jev_mass", "union"):
                for q in ("recall", "precision"):
                    r = p[m][q]
                    lo, hi = r["wilson95"] or (None, None)
                    row += [r["value"], lo, hi]
                row.append(p[m]["admits"])
            row += [p[k] for k in cols[15:]]
            w.writerow(row)
