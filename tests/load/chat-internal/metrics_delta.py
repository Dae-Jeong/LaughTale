"""Summarize same-process histogram snapshots; bucket bounds are not exact p99."""

import argparse
import json
from pathlib import Path

from prometheus_client.parser import text_string_to_metric_families


def samples(path):
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(path.read_text())
        for sample in family.samples
    }


def summarize(folder):
    before = samples(folder / "metrics-before.prom")
    after = samples(folder / "metrics-after.prom")
    result = []
    for (name, labels), value in after.items():
        if not name.endswith("_count"):
            continue
        stem = name.removesuffix("_count")
        count = value - before.get((name, labels), 0)
        elapsed = after[(stem + "_sum", labels)] - before.get(
            (stem + "_sum", labels), 0
        )
        if count <= 0:
            continue
        buckets = []
        for (candidate, bucket_labels), cumulative in after.items():
            if candidate != stem + "_bucket":
                continue
            bucket = dict(bucket_labels)
            bound = float(bucket.pop("le"))
            if bucket != dict(labels):
                continue
            delta = cumulative - before.get((candidate, bucket_labels), 0)
            if delta >= count * 0.99:
                buckets.append(bound)
        result.append(
            {
                "metric": stem,
                "labels": dict(labels),
                "count": count,
                "mean_ms": elapsed / count * 1000,
                "p99_bucket_upper_seconds": min(buckets) if buckets else None,
            }
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    result = summarize(args.folder)
    encoded = json.dumps(result, indent=2)
    (args.folder / "metrics-delta.json").write_text(encoded)
    print(encoded)
