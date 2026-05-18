from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Iterable


def _category_f1(pred: set[str], gold: set[str]) -> float:
    if not pred and not gold:
        return 1.0
    if not pred or not gold:
        return 0.0
    inter = len(pred & gold)
    p = inter / len(pred)
    r = inter / len(gold)
    if p + r == 0:
        return 0.0
    return 2 * p * r / (p + r)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _cosine(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    vocab = sorted(set(a) | set(b))
    va = [a.count(t) for t in vocab]
    vb = [b.count(t) for t in vocab]
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _cause_semantic_sim(pred_texts: list[str], gold_texts: list[str]) -> float:
    pred_join = " ".join(pred_texts)
    gold_join = " ".join(gold_texts)
    return _cosine(_tokenize(pred_join), _tokenize(gold_join))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compute attribution v2 metrics")
    p.add_argument("--catalyst-json", required=True)
    p.add_argument("--direct-json", required=True)
    p.add_argument("--output-json", required=True)
    return p.parse_args()


def _load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _avg(xs: Iterable[float]) -> float:
    vals = list(xs)
    return sum(vals) / len(vals) if vals else 0.0


def _compute(rows: list[dict]) -> dict:
    f1s = []
    sims = []
    for r in rows:
        pred = set(r.get("pred_categories", []))
        gold = set(r.get("gold_categories", []))
        f1s.append(_category_f1(pred, gold))
        sims.append(_cause_semantic_sim(r.get("pred_causes", []), r.get("gold_causes", [])))
    return {"category_f1": _avg(f1s), "cause_semantic_sim": _avg(sims)}


def main() -> int:
    args = _parse_args()
    out = {
        "catalyst": _compute(_load_rows(Path(args.catalyst_json))),
        "direct_llm": _compute(_load_rows(Path(args.direct_json))),
    }
    p = Path(args.output_json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] wrote attribution v2 metrics: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
