from __future__ import annotations

import copy
import hashlib
import unittest
from pathlib import Path

from tools.run_evaluation_matrix import MatrixError
from tools.verify_structured_output_evaluation import (
    _load_preregistration,
    verify_current_boundary,
)


class StructuredOutputEvaluationTests(unittest.TestCase):
    def test_legacy_draft_contract_is_preserved_for_the_frozen_preregistration(self) -> None:
        # Draft contract v1.2.0 is a separate file; the v1.1.0 contract that
        # the historical preregistration froze must stay byte-identical.
        boundary = _load_preregistration()["implementation_boundary"]
        contract_path = Path(__file__).resolve().parents[1] / boundary["frozen_draft_contract_path"]
        self.assertEqual(
            hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            boundary["frozen_draft_contract_sha256"],
        )

    def test_frozen_preregistration_rejects_changed_agent_runtime(self) -> None:
        with self.assertRaisesRegex(
            MatrixError,
            r"structured-output boundary changed: src/incident_platform/agent_rca\.py",
        ):
            verify_current_boundary(_load_preregistration())

    def test_current_boundary_with_test_only_runtime_digest(self) -> None:
        # This in-memory copy tests current strict-schema/Gate invariants. It
        # does not register an evaluation or revise the historical boundary.
        registration = copy.deepcopy(dict(_load_preregistration()))
        boundary = registration["implementation_boundary"]
        runtime_path = Path(__file__).resolve().parents[1] / boundary["agent_runtime_path"]
        boundary["agent_runtime_sha256"] = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
        result = verify_current_boundary(registration)

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
