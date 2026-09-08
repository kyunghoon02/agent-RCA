#!/usr/bin/env python3
"""Read one verification Incident using the deployed worker's dependencies.

Sent over stdin to an existing Pod; never invokes an LLM, mutates an Incident,
or prints credentials. Output is a private verification artifact, not a Report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys


def audit(run_id: str, *, mode: str = "graph") -> dict:
    import psycopg

    from incident_platform.agent_rca import (
        AgentInvestigationView,
        AgentToolRuntime,
        EvidenceCandidateSelector,
    )
    from incident_platform.repository import context_evidence_ids

    if not re.fullmatch(r"hubble-evidence-[0-9a-f]{12}", run_id):
        raise ValueError("invalid verification id")
    with psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DATABASE"],
        user=os.environ["POSTGRES_USERNAME"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=5,
        options="-c default_transaction_read_only=on -c statement_timeout=10000",
    ) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT document FROM incidents WHERE "
                "document->'alert'->'labels'->>'verification_id' = %s",
                (run_id,),
            )
            incidents = cursor.fetchall()
            if not incidents:
                return {"status": "WAITING"}
            if len(incidents) != 1:
                raise ValueError("verification Incident is not unique")
            incident = incidents[0][0]
            incident_id = incident["incident_id"]
            cursor.execute(
                "SELECT document FROM context_packages WHERE incident_id = %s "
                "ORDER BY frozen_at DESC", (incident_id,),
            )
            contexts = cursor.fetchall()
            if not contexts or incident["status"] != "ANALYZING":
                return {"status": incident["status"], "incident_id": incident_id}
            if len(contexts) != 1:
                raise ValueError("verification Context is not unique")
            context = contexts[0][0]
            cursor.execute(
                "SELECT document FROM evidence_items WHERE incident_id = %s "
                "ORDER BY observed_at, evidence_id", (incident_id,),
            )
            evidence = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                "SELECT count(*) FROM agent_runs WHERE incident_id = %s",
                (incident_id,),
            )
            agent_runs = cursor.fetchone()[0]

    candidate_ids = EvidenceCandidateSelector().select(
        context, evidence,
        max_candidates=int(os.environ.get("AGENT_RCA_MAX_EVIDENCE_CANDIDATES", "8")),
    )
    view = AgentInvestigationView.build(context, evidence, candidate_ids)
    by_id = {item["evidence_id"]: item for item in evidence}
    runtime = AgentToolRuntime(
        context_evidence_ids=frozenset(candidate_ids),
        evidence_by_id={key: by_id[key] for key in candidate_ids},
        reference_by_id={}, max_tool_calls=8,
        evidence_id_by_candidate_ref={
            f"E{index}": key for index, key in enumerate(candidate_ids, 1)
        },
    )
    frozen_ids = context_evidence_ids(context)
    hubble = [item for item in evidence if item["source"] == "hubble"]
    checks = []
    for item in hubble:
        key = item["evidence_id"]
        result = None
        if key in candidate_ids:
            ref = f"E{candidate_ids.index(key) + 1}"
            result = json.loads(runtime.inspect_candidate(ref))
        checks.append({
            "evidence_id": key, "subject": item["subject"],
            "window": item["window"], "facts": item["facts"],
            "quality": item["quality"],
            "in_frozen_context": key in frozen_ids,
            "in_agent_catalog": key in candidate_ids,
            "tool_status": result["status"] if result else "NOT_SELECTED",
            "tool_facts_equal": bool(result and result.get("result", {}).get("facts") == item["facts"]),
        })
    result = {
        "status": "READY", "incident_status": incident["status"],
        "incident_id": incident_id, "context_id": context["context_id"],
        "frozen_at": context["frozen_at"],
        "context_sha256": hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest(),
        "context_evidence_count": len(frozen_ids),
        "catalog_count": len(view.candidate_evidence_ids),
        "agent_run_count": agent_runs, "hubble": checks,
        "hubble_collector_statuses": [
            item for item in incident["collector_statuses"] if item["collector"] == "hubble"
        ],
        "collector_failures": context.get("collector_failures", []),
    }
    if mode == "graph":
        from incident_platform.neo4j_stategraph import create_neo4j_driver

        with create_neo4j_driver(
            os.environ["NEO4J_URI"], os.environ["NEO4J_USERNAME"],
            os.environ["NEO4J_PASSWORD"],
        ) as driver:
            with driver.session(database=os.environ.get("NEO4J_DATABASE", "neo4j"), default_access_mode="READ") as session:
                rows = session.run(
                    "MATCH (s:StateGraphEntity)-[:HAS_EVENT]->(e:StateGraphEvent) "
                    "WHERE s.cluster_id = $cluster AND s.namespace = $namespace "
                    "AND s.name = 'frontend' AND e.event_type = 'HUBBLE_NETWORK_FLOW_SUMMARY' "
                    "RETURN s.entity_id AS entity_id, e.document_json AS document",
                    cluster="agent-rca-chaos-eval", namespace="online-boutique",
                )
                ids = {item["evidence_id"] for item in hubble}
                events = []
                for row in rows:
                    document = json.loads(row["document"])
                    if ids.intersection(document["evidence_ids"]):
                        events.append({"entity_id": row["entity_id"], "record": document})
                result["graph_events"] = events
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(audit(sys.argv[1], mode=sys.argv[2] if len(sys.argv) > 2 else "graph")))
    except Exception as error:
        # Database/transport exception text can include connection details.
        print(json.dumps({"status": "AUDIT_ERROR", "error_type": type(error).__name__}))
        raise SystemExit(1)
