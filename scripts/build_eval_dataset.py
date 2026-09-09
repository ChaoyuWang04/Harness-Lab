from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.eval.catalog import load_eval_catalog
from app.eval.dataset import build_dataset, dataset_provenance_from_artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description="Build reviewed M4 dataset_v1")
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--review-sample", type=Path, required=True)
    parser.add_argument("--human-review", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    source_date_epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    normalized = [json.loads(line) for line in args.normalized.read_text(encoding="utf-8").splitlines()]
    sample = json.loads(args.review_sample.read_text(encoding="utf-8"))
    review = json.loads(args.human_review.read_text(encoding="utf-8"))
    build_dataset(
        normalized,
        load_eval_catalog(args.config_root),
        sample,
        review,
        output_dir=args.output_dir,
        source_date_epoch=source_date_epoch,
        metadata={"commit": args.commit, "model": args.model},
        provenance=dataset_provenance_from_artifacts(args.artifact_dir),
    )


if __name__ == "__main__":
    main()
