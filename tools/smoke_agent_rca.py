#!/usr/bin/env python3
"""Run one bounded live Agent RCA call against an isolated fixture Incident."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import APIStatusError

from incident_platform.agent_rca import AgentRCAService, OpenAIAgentsSDKRunner
from incident_platform.errors import ContractViolation
from incident_platform.knowledge import BoundedKnowledgeRetriever
from tests.test_agent_rca import StaticKnowledgeRepository, prepared_repository


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_dotenv(ROOT / ".env")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not configured in the environment or .env")

    repository, incident_id, context_id = prepared_repository()
    runner = OpenAIAgentsSDKRunner()
    service = AgentRCAService(
        repository,
        BoundedKnowledgeRetriever(StaticKnowledgeRepository()),
        runner,
    )
    try:
        result = service.run(
            incident_id,
            context_id=context_id,
            generated_at=datetime.now(timezone.utc),
        )
    except APIStatusError as error:
        code = None
        if isinstance(error.body, dict):
            code = error.body.get("code") or error.body.get("error", {}).get("code")
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(error).__name__,
                    "http_status": error.status_code,
                    "error_code": code,
                },
                sort_keys=True,
            )
        )
        return 2
    except ContractViolation:
        print(
            json.dumps(
                {
                    "status": "GATE_REJECTED",
                    "error_type": "ContractViolation",
                    "error_code": "EVIDENCE_GATE_REJECTED",
                },
                sort_keys=True,
            )
        )
        return 3
    print(
        json.dumps(
            {
                "status": result.incident["status"],
                "report_status": result.report["status"],
                "model": result.audit["model"],
                "llm_calls": result.audit["usage"]["llm_calls"],
                "tool_calls": result.audit["usage"]["tool_calls"],
                "input_tokens": result.audit["usage"]["input_tokens"],
                "output_tokens": result.audit["usage"]["output_tokens"],
                "cited_evidence_count": len(result.audit["cited_evidence_ids"]),
                "cited_reference_count": len(
                    result.audit["cited_reference_document_ids"]
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
