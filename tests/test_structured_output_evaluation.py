from __future__ import annotations

import copy
import unittest

from tools.run_evaluation_matrix import MatrixError
from tools.verify_structured_output_evaluation import (
    _load_preregistration,
    verify_current_boundary,
)


class StructuredOutputEvaluationTests(unittest.TestCase):
    def test_current_boundary_matches_frozen_preregistration(self) -> None:
        result = verify_current_boundary(_load_preregistration())

        self.assertEqual(result["status"], "BOUNDARY_VERIFIED")
        self.assertTrue(result["strict_json_schema"])
        self.assertTrue(result["post_output_evidence_gate"])

    def test_changed_runtime_digest_fails_closed(self) -> None:
        registration = copy.deepcopy(dict(_load_preregistration()))
        registration["implementation_boundary"]["agent_runtime_sha256"] = "0" * 64

        with self.assertRaises(MatrixError):
            verify_current_boundary(registration)


if __name__ == "__main__":
    unittest.main()
