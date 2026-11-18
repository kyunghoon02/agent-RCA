from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import unittest

from tools.run_evaluation_matrix import MatrixError
from tools.verify_structured_output_evaluation import (
    _load_preregistration,
    verify_current_boundary,
)


class StructuredOutputEvaluationTests(unittest.TestCase):
    def test_legacy_boundary_is_preserved_but_new_runtime_cannot_reuse_it(self) -> None:
        registration = _load_preregistration()
        boundary = registration["implementation_boundary"]
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(hashlib.sha256((root / boundary["frozen_draft_contract_path"]).read_bytes()).hexdigest(),
                         boundary["frozen_draft_contract_sha256"])
        with self.assertRaisesRegex(MatrixError, "boundary changed"):
            verify_current_boundary(registration)

    def test_changed_runtime_digest_fails_closed(self) -> None:
        registration = copy.deepcopy(dict(_load_preregistration()))
        registration["implementation_boundary"]["agent_runtime_sha256"] = "0" * 64

        with self.assertRaises(MatrixError):
            verify_current_boundary(registration)


if __name__ == "__main__":
    unittest.main()
