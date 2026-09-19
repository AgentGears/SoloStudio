from __future__ import annotations

import json
import unittest
from pathlib import Path

from solostudio.kernel.errors import InvalidCanonicalValue
from solostudio.kernel.identity import canonical_hash, canonical_text


class CanonicalizationGoldenTests(unittest.TestCase):
    def test_golden_vectors(self) -> None:
        path = Path(__file__).parents[1] / "golden" / "canonicalization" / "v1.json"
        vectors = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(vectors["canonicalization_version"], 1)
        for vector in vectors["vectors"]:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(canonical_text(vector["payload"]), vector["canonical"])
                self.assertEqual(canonical_hash(vector["payload"]), vector["sha256"])

    def test_null_and_absent_are_distinct(self) -> None:
        self.assertNotEqual(canonical_hash({"x": None}), canonical_hash({}))

    def test_floating_point_values_are_rejected(self) -> None:
        with self.assertRaises(InvalidCanonicalValue):
            canonical_text({"x": 1.0})

    def test_non_string_keys_are_rejected(self) -> None:
        with self.assertRaises(InvalidCanonicalValue):
            canonical_text({1: "x"})


if __name__ == "__main__":
    unittest.main()
