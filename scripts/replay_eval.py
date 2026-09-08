from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.eval.artifacts import atomic_write_json
from app.eval.replay import score_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Score frozen M4 replay outputs")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    dataset = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines()]
    outputs = json.loads(args.outputs.read_text(encoding="utf-8"))
    atomic_write_json(args.report, score_dataset(dataset, outputs))


if __name__ == "__main__":
    main()
