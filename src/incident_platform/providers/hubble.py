"""Bounded Hubble Relay adapter producing redacted network-flow summaries."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, Tuple

from ..errors import PermanentProviderError, RetryableProviderError
from ..evidence import CollectionRequest, EvidenceDraft, ProviderBatch, parse_time


_DNS_NAME = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?$")
_DROP_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_VERDICTS = frozenset(
    {
        "FORWARDED",
        "DROPPED",
        "AUDIT",
        "REDIRECTED",
        "ERROR",
        "TRACED",
        "TRANSLATED",
        "UNKNOWN",
    }
)
_PROTOCOLS = frozenset({"TCP", "UDP", "ICMPv4", "ICMPv6", "SCTP", "UNKNOWN"})


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _validate_private_server(value: str) -> str:
    """Allow only an internal DNS name or private/loopback IPv4 endpoint."""

    if not isinstance(value, str) or value.count(":") != 1:
        raise ValueError("Hubble server must use host:port syntax")
    host, port_text = value.rsplit(":", 1)
    if not host or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError("Hubble server host or port is invalid")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _DNS_NAME.fullmatch(host) or not (
            host == "localhost"
            or host.endswith(".svc")
            or host.endswith(".svc.cluster.local")
        ):
            raise ValueError(
                "Hubble server must be private IPv4 or cluster-local DNS"
            )
    else:
        if address.version != 4 or not (address.is_private or address.is_loopback):
            raise ValueError("Hubble server IP must be private IPv4")
    return value


@dataclass(frozen=True)
class HubbleFlowResult:
    flows: Tuple[Mapping[str, Any], ...]
    truncated: bool = False


class HubbleFlowClient(Protocol):
    def observe(
        self,
        *,
        namespace: str,
        pod_prefix: str,
        direction: str,
        start: str,
        end: str,
        limit: int,
        timeout_seconds: float,
    ) -> HubbleFlowResult:
        ...


class HubbleCLIClient:
    """Read JSONPB flows from Hubble Relay through a pinned CLI binary.

    The child process never receives a shell. Output is bounded by both the
    Hubble flow limit and a byte ceiling before JSON parsing.
    """

    def __init__(
        self,
        server: str,
        *,
        binary: str = "/usr/local/bin/hubble",
        max_output_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._server = _validate_private_server(server)
        if not os.path.isabs(binary):
            raise ValueError("Hubble CLI binary path must be absolute")
        if max_output_bytes <= 0:
            raise ValueError("Hubble max_output_bytes must be positive")
        self._binary = binary
        self._max_output_bytes = max_output_bytes

    def observe(
        self,
        *,
        namespace: str,
        pod_prefix: str,
        direction: str,
        start: str,
        end: str,
        limit: int,
        timeout_seconds: float,
    ) -> HubbleFlowResult:
        if direction not in {"from", "to"}:
            raise PermanentProviderError("Hubble direction is unsupported")
        if limit <= 0:
            raise PermanentProviderError("Hubble flow limit must be positive")
        scoped_pod = f"{namespace}/{pod_prefix}"
        argv = [
            self._binary,
            "observe",
            "--server",
            self._server,
            f"--{direction}-pod",
            scoped_pod,
            "--since",
            start,
            "--until",
            end,
            "--last",
            str(limit + 1),
            "--output",
            "jsonpb",
        ]
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as error:
            raise PermanentProviderError("Hubble CLI binary is unavailable") from error
        except subprocess.TimeoutExpired as error:
            raise RetryableProviderError("Hubble Relay query timed out") from error
        except OSError as error:
            raise RetryableProviderError("Hubble CLI execution failed") from error

        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").lower()
            if any(
                marker in stderr
                for marker in (
                    "connection refused",
                    "deadline exceeded",
                    "i/o timeout",
                    "no route to host",
                    "transport is closing",
                    "unavailable",
                )
            ):
                raise RetryableProviderError("Hubble Relay is unavailable")
            raise PermanentProviderError("Hubble Relay rejected the bounded query")
        if len(completed.stdout) > self._max_output_bytes:
            raise PermanentProviderError("Hubble response exceeded the byte limit")

        flows = []
        for raw_line in completed.stdout.splitlines():
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PermanentProviderError("Hubble JSONPB output is malformed") from error
            if not isinstance(payload, Mapping) or not isinstance(
                payload.get("flow"), Mapping
            ):
                raise PermanentProviderError("Hubble JSONPB flow is malformed")
            flows.append(payload["flow"])
        flows.sort(key=lambda item: str(item.get("time", "")))
        truncated = len(flows) > limit
        if truncated:
            flows = flows[-limit:]
        return HubbleFlowResult(tuple(flows), truncated=truncated)


class HubbleNetworkFlowProvider:
    """Aggregate exact root-Pod flow queries into Service-scoped Evidence."""

    provider_name = "hubble-relay-network-flow-provider"
    feature_set = "hubble-network-flow-summary-v1"

    def __init__(
        self,
        client: HubbleFlowClient,
        *,
        cluster_id: str,
        max_scoped_resources: int = 8,
        max_raw_flows: int = 500,
    ) -> None:
        if not cluster_id.strip():
            raise ValueError("Hubble cluster_id must not be empty")
        if max_scoped_resources <= 0 or max_raw_flows <= 0:
            raise ValueError("Hubble limits must be positive")
        self._client = client
        self._cluster_id = cluster_id
        self._max_scoped_resources = max_scoped_resources
        self._max_raw_flows = max_raw_flows

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        resource_names = request.scope.resource_names
        if len(resource_names) > self._max_scoped_resources:
            raise PermanentProviderError(
                "Hubble resource scope exceeded the configured query budget"
            )
        if len(resource_names) > request.scope.max_items:
            raise PermanentProviderError(
                "Hubble resource scope exceeded the Evidence item budget"
            )

        deadline = time.monotonic() + request.timeout_seconds
        drafts = []
        partial_reasons = []
        for resource_name in resource_names:
            by_uuid: dict[str, Mapping[str, Any]] = {}
            resource_truncated = False
            for direction in ("from", "to"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RetryableProviderError(
                        "Hubble collection deadline exhausted"
                    )
                result = self._client.observe(
                    namespace=request.scope.namespace,
                    pod_prefix=resource_name,
                    direction=direction,
                    start=request.window.start,
                    end=request.window.end,
                    limit=self._max_raw_flows,
                    timeout_seconds=remaining,
                )
                resource_truncated = resource_truncated or result.truncated
                for flow in result.flows:
                    flow_uuid = flow.get("uuid")
                    if not isinstance(flow_uuid, str) or not flow_uuid:
                        raise PermanentProviderError("Hubble flow UUID is missing")
                    self._validate_scoped_flow(
                        flow,
                        request=request,
                        resource_name=resource_name,
                        direction=direction,
                    )
                    by_uuid[flow_uuid] = flow

            draft, no_data_unknown = self._summarize(
                tuple(by_uuid.values()),
                request=request,
                resource_name=resource_name,
                truncated=resource_truncated,
            )
            drafts.append(draft)
            if resource_truncated:
                partial_reasons.append(
                    f"{resource_name}: flow limit reached"
                )
            if no_data_unknown:
                partial_reasons.append(
                    f"{resource_name}: no matching flow; retention coverage unknown"
                )

        if partial_reasons:
            return ProviderBatch(
                items=tuple(drafts),
                status="PARTIAL",
                error="; ".join(partial_reasons),
            )
        return ProviderBatch(items=tuple(drafts))

    @staticmethod
    def _endpoint_names(endpoint: object) -> Tuple[str, ...]:
        if not isinstance(endpoint, Mapping):
            return tuple()
        names = []
        pod_name = endpoint.get("pod_name")
        if isinstance(pod_name, str) and pod_name:
            names.append(pod_name)
        workloads = endpoint.get("workloads")
        if isinstance(workloads, Sequence) and not isinstance(workloads, (str, bytes)):
            for workload in workloads:
                if not isinstance(workload, Mapping):
                    continue
                name = workload.get("name")
                if isinstance(name, str) and name:
                    names.append(name)
        return tuple(dict.fromkeys(names))

    @classmethod
    def _matches_root(cls, endpoint: object, resource_name: str) -> bool:
        return any(
            name == resource_name or name.startswith(f"{resource_name}-")
            for name in cls._endpoint_names(endpoint)
        )

    def _validate_scoped_flow(
        self,
        flow: Mapping[str, Any],
        *,
        request: CollectionRequest,
        resource_name: str,
        direction: str,
    ) -> None:
        timestamp = flow.get("time")
        try:
            observed_at = parse_time(timestamp, "Hubble flow time")
        except Exception as error:
            raise PermanentProviderError("Hubble flow timestamp is malformed") from error
        window_start = parse_time(request.window.start, "EvidenceWindow.start")
        window_end = parse_time(request.window.end, "EvidenceWindow.end")
        if observed_at < window_start or observed_at > window_end:
            raise PermanentProviderError(
                "Hubble returned a flow outside the requested time window"
            )
        endpoint_name = "source" if direction == "from" else "destination"
        endpoint = flow.get(endpoint_name)
        if not self._matches_root(endpoint, resource_name):
            raise PermanentProviderError(
                "Hubble returned a flow outside the requested Pod prefix"
            )
        if isinstance(endpoint, Mapping):
            namespace = endpoint.get("namespace")
            if namespace != request.scope.namespace:
                raise PermanentProviderError(
                    "Hubble returned a flow outside the requested namespace"
                )

    @staticmethod
    def _protocol(flow: Mapping[str, Any]) -> str:
        l4 = flow.get("l4")
        if not isinstance(l4, Mapping):
            return "UNKNOWN"
        values = [name for name in _PROTOCOLS if name != "UNKNOWN" and name in l4]
        return values[0] if len(values) == 1 else "UNKNOWN"

    def _summarize(
        self,
        flows: Sequence[Mapping[str, Any]],
        *,
        request: CollectionRequest,
        resource_name: str,
        truncated: bool,
    ) -> tuple[EvidenceDraft, bool]:
        verdicts: Counter[str] = Counter()
        protocols: Counter[str] = Counter()
        drop_reasons: Counter[str] = Counter()
        source_root_count = 0
        destination_root_count = 0
        observed_times = []
        for flow in flows:
            verdict = flow.get("verdict")
            if verdict not in _VERDICTS:
                verdict = "UNKNOWN"
            verdicts[verdict] += 1
            protocols[self._protocol(flow)] += 1
            if verdict == "DROPPED":
                reason = flow.get("drop_reason_desc")
                if not isinstance(reason, str) or not _DROP_REASON.fullmatch(reason):
                    reason = "UNKNOWN"
                drop_reasons[reason] += 1
            source_root_count += int(
                self._matches_root(flow.get("source"), resource_name)
            )
            destination_root_count += int(
                self._matches_root(flow.get("destination"), resource_name)
            )
            observed_times.append(parse_time(flow["time"], "Hubble flow time"))

        if observed_times:
            observed_at = _format_time(max(observed_times))
            first_flow_at = _format_time(min(observed_times))
            result_status = "HAS_DATA"
            retention_status = "NOT_APPLICABLE"
            reason_codes = []
        else:
            observed_at = request.window.end
            first_flow_at = None
            result_status = "NO_DATA"
            retention_status = "UNKNOWN"
            reason_codes = ["RETENTION_WINDOW_NOT_PROVABLE"]
        facts = {
            "feature_set": self.feature_set,
            "result_status": result_status,
            "flow_count": len(flows),
            "verdict_counts": dict(sorted(verdicts.items())),
            "protocol_counts": dict(sorted(protocols.items())),
            "drop_reason_counts": dict(sorted(drop_reasons.items())),
            "source_root_flow_count": source_root_count,
            "destination_root_flow_count": destination_root_count,
            "first_flow_at": first_flow_at,
            "last_flow_at": observed_at if observed_times else None,
            "truncated": truncated,
            "retention_status": retention_status,
            "reason_codes": reason_codes,
        }
        query = (
            f"hubble observe namespace={request.scope.namespace} "
            f"pod-prefix={resource_name} directions=from,to "
            f"limit={self._max_raw_flows}"
        )
        return (
            EvidenceDraft(
                source="hubble",
                kind="network-flow-summary",
                observed_at=observed_at,
                subject={
                    "cluster_id": self._cluster_id,
                    "api_version": "v1",
                    "kind": "Service",
                    "namespace": request.scope.namespace,
                    "name": resource_name,
                    "uid": None,
                    "exists": True,
                },
                summary=(
                    f"Hubble observed {len(flows)} bounded network flow(s) for "
                    f"Service {resource_name}."
                    if flows
                    else (
                        f"Hubble returned no matching bounded network flows for "
                        f"Service {resource_name}; retention coverage is unknown."
                    )
                ),
                facts=facts,
                provider=self.provider_name,
                query=query,
                locator=(
                    f"hubble://{self._cluster_id}/{request.scope.namespace}/"
                    f"Service/{resource_name}"
                ),
                completeness=0.5 if truncated else (1.0 if flows else 0.0),
                confidence=1.0 if flows and not truncated else 0.5,
            ),
            not bool(flows),
        )
