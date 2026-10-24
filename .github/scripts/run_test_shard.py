"""Run every discovered test exactly once per Python, keeping modules together."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
HEAVY_MODULE = "test_decomposed_pipeline"
SHARDS = ("decomposed", "other-a", "other-b")


class DiscoveryError(ValueError):
    """A partial or ambiguous discovery must never become a green shard."""


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        elif isinstance(item, unittest.TestCase):
            yield item
        else:
            raise DiscoveryError("Unsupported discovered test object")


def partition(suite, *, errors=()):
    """Preserve discovery's order inside each whole module and class."""
    if errors:
        raise DiscoveryError("Test discovery failed:\n" + "\n".join(errors))
    tests = list(cases(suite))
    if not tests:
        raise DiscoveryError("No tests discovered")
    identifiers = [test.id() for test in tests]
    if len(set(identifiers)) != len(identifiers):
        raise DiscoveryError("Duplicate test IDs in full discovery")
    modules = defaultdict(list)
    for test in tests:
        module = type(test).__module__
        if not module.startswith("test_"):
            # Includes unittest.loader._FailedTest, even if a custom loader did
            # not populate errors. It must not hide in an unselected shard.
            raise DiscoveryError("Unexpected test module: " + module)
        modules[module].append(test)
    if HEAVY_MODULE not in modules:
        raise DiscoveryError("The dedicated test module was not discovered")
    buckets = {name: [] for name in SHARDS}
    buckets["decomposed"].extend(modules[HEAVY_MODULE])
    for index, name in enumerate(sorted(set(modules) - {HEAVY_MODULE})):
        buckets[SHARDS[1 + index % 2]].extend(modules[name])
    if any(not bucket for bucket in buckets.values()):
        raise DiscoveryError("A configured shard is empty")
    routed = [test.id() for bucket in buckets.values() for test in bucket]
    if len(routed) != len(identifiers) or set(routed) != set(identifiers):
        raise DiscoveryError("Shards do not cover full discovery exactly once")
    return buckets


def discover():
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "project/tests"))
    return partition(suite, errors=loader.errors)


def digest(identifiers):
    return hashlib.sha256(("\n".join(sorted(identifiers)) + "\n").encode()).hexdigest()


def manifest(buckets, selected):
    all_ids = [test.id() for bucket in buckets.values() for test in bucket]
    return {
        "schema_version": 1,
        "routing": "whole-modules-heavy-plus-sorted-alternation-v1",
        "selected_shard": selected,
        "total_tests": len(all_ids),
        "discovery_sha256": digest(all_ids),
        "selected_tests": len(buckets[selected]),
        "partitions": {
            name: {
                "tests": len(bucket),
                "test_ids_sha256": digest(test.id() for test in bucket),
                "modules": sorted({type(test).__module__ for test in bucket}),
            } for name, bucket in buckets.items()
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", choices=SHARDS, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--list", action="store_true", help="Validate and report discovery without running tests")
    args = parser.parse_args(argv)
    try:
        buckets = discover()
        report = manifest(buckets, args.shard)
    except DiscoveryError as error:
        print(str(error), file=sys.stderr)
        return 2
    encoded = json.dumps(report, sort_keys=True, indent=2)
    print(encoded, flush=True)
    if args.manifest is not None:
        args.manifest.write_text(encoded + "\n", encoding="utf-8")
    if args.list:
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(buckets[args.shard]))
    # A future custom fixture that unexpectedly skips execution cannot report
    # successful coverage solely because its parent shard returned zero.
    return 0 if result.wasSuccessful() and result.testsRun == report["selected_tests"] else 1


if __name__ == "__main__":
    sys.exit(main())
