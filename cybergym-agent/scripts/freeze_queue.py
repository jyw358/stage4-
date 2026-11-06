#!/usr/bin/env python3
"""freeze_queue.py -- B4/G1: freeze a stratified, seed-fixed 50-task queue.

Implements the r5 review's launch gate G1/B4: the formal pilot sample must be
frozen BEFORE launch -- stratified by task source (ARVO vs OSS-Fuzz in
dataset proportion), excluding previously-verified tasks (evaluation
contamination), with a fixed seed, and a manifest recording every choice so
the sample is reproducible and auditable.

Usage (host, root):
  python3 freeze_queue.py --pool /data/cybergym/stage3/queue_all.json \
      --out /var/lib/cybergym/runs/formal50.queue.json \
      --manifest /var/lib/cybergym/runs/formal50.manifest.json \
      --exclude arvo:10628,arvo:1065,arvo:10710,arvo:11007,arvo:11908 \
      --n 50 --seed 20261106

NOTE: casualties/hard tasks are deliberately NOT excluded -- the sample must
represent the population, not inflate the score.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path


def source_of(tid: str) -> str:
    return "oss-fuzz" if tid.startswith("oss-fuzz") else "arvo"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True, help="full task-id pool (json list)")
    ap.add_argument("--out", required=True, help="queue file for stage4.py --queue")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--exclude", default="", help="comma-separated task ids")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20261106)
    args = ap.parse_args()

    pool = json.load(open(args.pool, encoding="utf-8"))
    if isinstance(pool, dict):
        pool = pool.get("queue", pool.get("tasks"))
    pool = [t for t in pool if isinstance(t, str)]
    exclude = {t.strip() for t in args.exclude.split(",") if t.strip()}
    eligible = [t for t in pool if t not in exclude]

    by_src = {"arvo": [], "oss-fuzz": []}
    for t in eligible:
        by_src[source_of(t)].append(t)
    total = len(eligible)
    if total == 0 or args.n > total:
        print(f"freeze_queue: pool too small ({total})", file=sys.stderr)
        return 1

    # dataset-proportional strata (139/1507 OSS-Fuzz ≈ 4.6 -> round to 5)
    oss_n = round(args.n * len(by_src["oss-fuzz"]) / total)
    arvo_n = args.n - oss_n
    if arvo_n > len(by_src["arvo"]) or oss_n > len(by_src["oss-fuzz"]):
        print("freeze_queue: stratum larger than pool slice", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    picked = sorted(rng.sample(by_src["arvo"], arvo_n)) + \
        sorted(rng.sample(by_src["oss-fuzz"], oss_n))

    Path(args.out).write_text(json.dumps(picked, indent=1) + "\n",
                              encoding="utf-8")
    queue_sha = hashlib.sha256(
        Path(args.out).read_bytes()).hexdigest()
    manifest = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "n": args.n,
        "pool_size": len(pool),
        "excluded": sorted(exclude),
        "excluded_reason": "previously verified in pilots (contamination)",
        "strata": {"arvo": arvo_n, "oss-fuzz": oss_n},
        "queue_sha256": queue_sha,
        "queue": picked,
        "report_metrics": ["first_pass_rate", "after_one_valid_retry",
                           "by_source_stratum"],
    }
    Path(args.manifest).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[freeze] n={args.n} strata arvo={arvo_n} oss-fuzz={oss_n} "
          f"excluded={len(exclude)} seed={args.seed}")
    print(f"[freeze] queue={args.out} sha256={queue_sha[:16]}… "
          f"manifest={args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
