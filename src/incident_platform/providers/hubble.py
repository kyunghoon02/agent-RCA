"""Bounded Hubble Relay adapter producing redacted network-flow summaries."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import selectors
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, Tuple

from ..errors import PermanentProviderError, RetryableProviderError
from ..evidence import CollectionRequest, EvidenceDraft, ProviderBatch, parse_time
from ..hubble_contract import (
    FEATURE_SET,
    OBSERVATION_GAPS,
    POLICY_DROP_REASONS,
    PROTOCOLS as _PROTOCOLS,
    VERDICTS as _VERDICTS,
    flow_signal,
)

_DNS_NAME = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?$")
_DROP_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_RESOURCE_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")
_FLOW_FIELDS = (
    "uuid,time,verdict,drop_reason_desc,l4,source.namespace,source.pod_name,"
    "source.workloads,destination.namespace,destination.pod_name,destination.workloads"
)


def _run_bounded(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> subprocess.CompletedProcess:
    """Drain both pipes under one byte/time cap, killing the child on failure."""
    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout_seconds)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(65536, max_output_bytes - total + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > max_output_bytes:
                        raise PermanentProviderError(
                            "Hubble response exceeded the byte limit"
                        )
                    buffers[key.data].extend(chunk)
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return subprocess.CompletedProcess(
            argv,
            code,
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
        )
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1.0)
        finally:
            process.stdout.close()
            process.stderr.close()


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
            raise ValueError("Hubble server must be private IPv4 or cluster-local DNS")
    else:
        if address.version != 4 or not (address.is_private or address.is_loopback):
            raise ValueError("Hubble server IP must be private IPv4")
    return value


@dataclass(frozen=True)
class HubbleFlowResult:
    flows: Tuple[Mapping[str, Any], ...]
    truncated: bool = False
    observation_gaps: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not set(self.observation_gaps) <= OBSERVATION_GAPS:
            raise ValueError("unsupported Hubble observation gap")


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
    ) -> HubbleFlowResult: ...


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
        if limit <= 0 or timeout_seconds <= 0:
            raise PermanentProviderError("Hubble flow limit must be positive")
        if (
            not _RESOURCE_NAME.fullmatch(namespace)
            or len(namespace) > 63
            or not _RESOURCE_NAME.fullmatch(pod_prefix)
        ):
            raise PermanentProviderError("Hubble resource selector is malformed")
        if parse_time(start, "Hubble start") > parse_time(end, "Hubble end"):
            raise PermanentProviderError("Hubble time window is reversed")
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
            "--field-mask",
            _FLOW_FIELDS,
        ]
        try:
            completed = _run_bounded(
                argv,
                timeout_seconds=timeout_seconds,
                max_output_bytes=self._max_output_bytes,
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
        if len(completed.stdout) + len(completed.stderr) > self._max_output_bytes:
            raise PermanentProviderError("Hubble response exceeded the byte limit")

        flows = []
        gaps = set()
        # The pinned CLI writes node_status JSONPB to stderr, lost_events to stdout.
        for output, diagnostic in ((completed.stdout, False), (completed.stderr, True)):
            for raw_line in output.splitlines():
                if not raw_line.strip():
                    continue
                try:
                    payload = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    if diagnostic:
                        gaps.add(
                            "CLI_RELAY_VERSION_MISMATCH"
                            if b"Hubble CLI version is lower than Hubble Relay, API compatibility is not guaranteed"
                            in raw_line
                            else "CLI_DIAGNOSTIC"
                        )
                        continue
                    raise PermanentProviderError(
                        "Hubble JSONPB output is malformed"
                    ) from error
                if not isinstance(payload, Mapping):
                    raise PermanentProviderError("Hubble JSONPB response is malformed")
                variants = set(payload) & {"flow", "lost_events", "node_status"}
                if len(variants) != 1:
                    raise PermanentProviderError(
                        "Hubble JSONPB response type is malformed"
                    )
                variant = next(iter(variants))
                body = payload[variant]
                if not isinstance(body, Mapping):
                    raise PermanentProviderError(
                        "Hubble JSONPB response body is malformed"
                    )
                if variant == "flow":
                    flows.append(body)
                elif variant == "lost_events":
                    # A notification is a coverage gap, not a scoped packet drop.
                    gaps.add("FLOW_EVENTS_LOST")
                else:
                    state = body.get("state_change", "UNKNOWN_NODE_STATE")
                    if not isinstance(state, str):
                        raise PermanentProviderError("Hubble node status is malformed")
                    if state != "NODE_CONNECTED":
                        gaps.add(
                            {
                                "NODE_UNAVAILABLE": "RELAY_NODE_UNAVAILABLE",
                                "NODE_ERROR": "RELAY_NODE_ERROR",
                                "NODE_GONE": "RELAY_NODE_GONE",
                            }.get(state, "RELAY_NODE_STATE_UNKNOWN")
                        )
        flows.sort(key=lambda item: str(item.get("time", "")))
        truncated = len(flows) > limit
        if truncated:
            flows = flows[-limit:]
        return HubbleFlowResult(
            tuple(flows), truncated=truncated, observation_gaps=tuple(sorted(gaps))
        )


class HubbleNetworkFlowProvider:
    """Aggregate exact root-Pod flow queries into Service-scoped Evidence."""

    provider_name = "hubble-relay-network-flow-provider"
    feature_set = FEATURE_SET

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
        # One Provider budget across every root and both directions. The client
        # uses one extra sentinel record per query to detect truncation.
        per_query_limit = self._max_raw_flows // (2 * len(resource_names))
        if per_query_limit < 1:
            raise PermanentProviderError(
                "Hubble flow budget cannot cover all scoped queries"
            )

        deadline = time.monotonic() + request.timeout_seconds
        drafts = []
        partial_reasons = []
        successful_queries = 0
        for resource_name in resource_names:
            by_uuid: dict[str, Mapping[str, Any]] = {}
            resource_truncated = False
            observation_gaps = set()
            for direction in ("from", "to"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    observation_gaps.add("QUERY_BUDGET_EXHAUSTED")
                    continue
                try:
                    result = self._client.observe(
                        namespace=request.scope.namespace,
                        pod_prefix=resource_name,
                        direction=direction,
                        start=request.window.start,
                        end=request.window.end,
                        limit=per_query_limit,
                        timeout_seconds=remaining,
                    )
                except RetryableProviderError:
                    observation_gaps.add("RELAY_QUERY_UNAVAILABLE")
                    continue
                successful_queries += 1
                resource_truncated = resource_truncated or result.truncated
                observation_gaps.update(result.observation_gaps)
                if len(result.flows) > per_query_limit:
                    raise PermanentProviderError(
                        "Hubble client exceeded the assigned flow budget"
                    )
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
                    if flow_uuid in by_uuid and by_uuid[flow_uuid] != flow:
                        raise PermanentProviderError(
                            "Hubble duplicate UUID has conflicting content"
                        )
                    by_uuid[flow_uuid] = flow

            draft, no_data_unknown = self._summarize(
                tuple(by_uuid.values()),
                request=request,
                resource_name=resource_name,
                truncated=resource_truncated,
                observation_gaps=tuple(sorted(observation_gaps)),
            )
            drafts.append(draft)
            if observation_gaps:
                partial_reasons.append(
                    f"{resource_name}: " + ", ".join(sorted(observation_gaps))
                )
            if resource_truncated:
                partial_reasons.append(f"{resource_name}: flow limit reached")
            if no_data_unknown:
                partial_reasons.append(
                    f"{resource_name}: no matching flow; retention coverage unknown"
                )

        if not successful_queries:
            raise RetryableProviderError("All bounded Hubble queries were unavailable")
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
            raise PermanentProviderError(
                "Hubble flow timestamp is malformed"
            ) from error
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
        observation_gaps: Tuple[str, ...],
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
                if (
                    not isinstance(reason, str)
                    or not _DROP_REASON.fullmatch(reason)
                    or reason == "DROP_REASON_UNKNOWN"
                ):
                    reason = "UNKNOWN"
                drop_reasons[reason] += 1
            source_root_count += int(
                self._matches_root(flow.get("source"), resource_name)
                and flow["source"].get("namespace") == request.scope.namespace
            )
            destination_root_count += int(
                self._matches_root(flow.get("destination"), resource_name)
                and flow["destination"].get("namespace") == request.scope.namespace
            )
            observed_times.append(parse_time(flow["time"], "Hubble flow time"))

        if observed_times:
            observed_at = _format_time(max(observed_times))
            first_flow_at = _format_time(min(observed_times))
            result_status = "HAS_DATA"
            retention_status = "UNKNOWN"
            reason_codes = []
        else:
            observed_at = request.window.end
            first_flow_at = None
            result_status = "NO_DATA"
            retention_status = "UNKNOWN"
            reason_codes = ["RETENTION_WINDOW_NOT_PROVABLE"]
        policy_denied_count = sum(
            drop_reasons[reason] for reason in POLICY_DROP_REASONS
        )
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
            "observation_gaps": list(observation_gaps),
            "policy_denied_count": policy_denied_count,
            "other_drop_count": sum(
                value
                for key, value in drop_reasons.items()
                if key not in POLICY_DROP_REASONS and key != "UNKNOWN"
            ),
            "unknown_drop_count": drop_reasons.get("UNKNOWN", 0),
            "flow_signal": flow_signal(
                len(flows),
                verdicts.get("DROPPED", 0),
                policy_denied_count,
            ),
        }
        query = (
            f"hubble observe namespace={request.scope.namespace} "
            f"pod-prefix={resource_name} directions=from,to "
            f"provider-flow-budget={self._max_raw_flows}"
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
                    f"Hubble observed {len(flows)} flow(s), including "
                    f"{verdicts.get('DROPPED', 0)} drop(s) and "
                    f"{policy_denied_count} policy denial(s) for "
                    f"Service {resource_name}. This sample does not prove network health "
                    "or an application root cause; retention coverage is unknown."
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
                completeness=0.5 if flows else 0.0,
                confidence=(
                    1.0 if flows and not truncated and not observation_gaps else 0.5
                ),
            ),
            not bool(flows),
        )
