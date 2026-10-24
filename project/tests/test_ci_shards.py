"""The CI partition is complete, disjoint and preserves whole test modules."""

import importlib.util
from pathlib import Path
import unittest


_SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/run_test_shard.py"
_SPEC = importlib.util.spec_from_file_location("pdm_ci_shards", _SCRIPT)
shards = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(shards)


def example(module, name, methods=("test_first", "test_second")):
    def check(self):
        pass
    test_class = type(name, (unittest.TestCase,),
                      {"__module__": module, **{method: check for method in methods}})
    return unittest.defaultTestLoader.loadTestsFromTestCase(test_class)


def suite():
    return unittest.TestSuite([
        example("test_a", "Alpha"),
        example(shards.HEAVY_MODULE, "Heavy"),
        example("test_b", "Bravo"),
        example("test_a", "AnotherAlpha"),
        example("test_c", "Charlie"),
    ])


class CiShardTests(unittest.TestCase):
    def test_whole_modules_are_unique_complete_and_heavy_module_is_dedicated(self):
        discovered = suite()
        expected = {test.id() for test in shards.cases(discovered)}
        buckets = shards.partition(discovered)
        all_ids = [test.id() for bucket in buckets.values() for test in bucket]
        self.assertEqual(len(all_ids), len(expected))
        self.assertEqual(set(all_ids), expected)
        owners = {}
        for owner, tests in buckets.items():
            for test in tests:
                module = type(test).__module__
                self.assertEqual(owners.setdefault(module, owner), owner)
        self.assertEqual(owners[shards.HEAVY_MODULE], "decomposed")
        self.assertEqual({type(test).__module__ for test in buckets["decomposed"]},
                         {shards.HEAVY_MODULE})
        self.assertEqual(owners["test_a"], owners["test_c"])
        self.assertNotEqual(owners["test_a"], owners["test_b"])

    def test_module_and_class_case_order_survives_partition(self):
        discovered = suite()
        before = [test.id() for test in shards.cases(discovered)
                  if type(test).__module__ == "test_a"]
        after = [test.id() for test in shards.partition(discovered)["other-a"]
                 if type(test).__module__ == "test_a"]
        self.assertEqual(after, before)

    def test_any_discovery_error_blocks_every_shard_and_duplicates_are_rejected(self):
        with self.assertRaises(shards.DiscoveryError):
            shards.partition(suite(), errors=["an unselected module failed to import"])
        broken = suite()
        broken.addTests(unittest.TestLoader().loadTestsFromName(
            "test_ci_deliberately_missing_module_9f8c"))
        with self.assertRaises(shards.DiscoveryError):
            shards.partition(broken)
        duplicated = suite()
        duplicated.addTests(example("test_a", "Alpha"))
        with self.assertRaises(shards.DiscoveryError):
            shards.partition(duplicated)
        with self.assertRaises(shards.DiscoveryError):
            shards.partition(unittest.TestSuite())

    def test_current_repository_discovery_is_partitioned_without_loss(self):
        buckets = shards.discover()
        flattened = [test.id() for bucket in buckets.values() for test in bucket]
        loader = unittest.TestLoader()
        original = loader.discover(str(_SCRIPT.parents[2] / "project/tests"))
        expected = [test.id() for test in shards.cases(original)]
        self.assertFalse(loader.errors)
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(set(flattened), set(expected))
        reports = [shards.manifest(buckets, name) for name in shards.SHARDS]
        self.assertEqual(len({report["discovery_sha256"] for report in reports}), 1)
        self.assertEqual(sum(report["selected_tests"] for report in reports), len(expected))


if __name__ == "__main__":
    unittest.main()
