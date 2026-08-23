#!/usr/bin/env python3
"""Read live span-derived Prometheus metrics through the bounded KRCA Provider."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceWindow,
    ResourceScope,
    format_time,
    validate_provider_batch,
)
from incident_platform.errors import ProviderError
from incident_platform.krca_pipeline import EvidenceBackedKRCADrilldownService
from incident_platform.krca_runtime import load_krca_runtime_config
from incident_platform.providers.prometheus import PrometheusHTTPAPI


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "online-boutique-krca.yaml"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def main() -> int:
    arguments = _arguments()
    endpoint = arguments.prometheus_url.rstrip("/")
    if urlsplit(endpoint).hostname not in LOOPBACK_HOSTS:
        raise SystemExit(
            "live KRCA smoke requires a loopback-only Prometheus tunnel"
        )

    config = load_krca_runtime_config(arguments.config)
    client = PrometheusHTTPAPI(endpoint)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    window = EvidenceWindow(
        start=format_time(now - timedelta(seconds=config.collection.window_seconds)),
        end=format_time(now),
    )
    profiles = []
    all_complete = True
    for profile in config.profiles:
        incident_id = f"inc-live-krca-{profile.profile_id}"
        request = CollectionRequest(
            request_id=f"req-live-krca-{profile.profile_id}",
            incident_id=incident_id,
            window=window,
            scope=ResourceScope(
                namespace=config.namespace,
                resource_names=profile.resource_names,
                max_items=config.collection.max_evidence_items,
            ),
            timeout_seconds=config.collection.timeout_seconds,
        )
        try:
            batch = config.provider(client, profile).collect(request)
        except ProviderError as error:
            all_complete = False
            profiles.append(
                {
                    "profile_id": profile.profile_id,
                    "status": "INCOMPLETE",
                    "failure_class": type(error).__name__,
                    "edges": [],
                    "drilldown_stop_reason": None,
                    "top_services": [],
                    "unavailable_evidence_count": 0,
                }
            )
            continue
        validate_provider_batch(batch, request)
        evidence = tuple(
            EvidenceBuilder().build(item, request, collected_at=now)
            for item in batch.items
        )
        feature_run = EvidenceBackedKRCADrilldownService().localize(
            incident_id,
            window=window,
            alerting_api=profile.alerting_api,
            evidence=evidence,
        )
        edges = [
            {
                "edge_id": item["facts"]["edge_id"],
                "result_status": item["facts"]["result_status"],
                "aligned_sample_count": item["facts"].get(
                    "computation", {}
                ).get("aligned_sample_count", 0),
                "reason_codes": item["facts"].get("reason_codes", []),
            }
            for item in evidence
        ]
        profile_complete = batch.status == "SUCCEEDED" and all(
            item["result_status"] == "HAS_DATA" for item in edges
        )
        all_complete = all_complete and profile_complete
        profiles.append(
            {
                "profile_id": profile.profile_id,
                "status": "CONNECTED" if profile_complete else "INCOMPLETE",
                "edges": edges,
                "drilldown_stop_reason": feature_run.drilldown.stop_reason,
                "top_services": [
                    item.api.service for item in feature_run.drilldown.top_services
                ],
                "unavailable_evidence_count": len(
                    feature_run.unavailable_feature_evidence_ids
                ),
            }
        )

    print(
        json.dumps(
            {
                "status": "CONNECTED" if all_complete else "INCOMPLETE",
                "namespace": config.namespace,
                "profile_count": len(profiles),
                "profiles": profiles,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0 if all_complete else 2


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
    )
    parser.add_argument(
        "--prometheus-url",
        default=os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:19090"),
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
