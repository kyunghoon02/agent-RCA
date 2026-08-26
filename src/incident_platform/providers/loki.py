"""Scoped Loki adapter for independently recorded kernel cgroup OOM signals."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..errors import PermanentProviderError, RetryableProviderError
from ..evidence import CollectionRequest, EvidenceDraft, ProviderBatch, parse_time
from .http import BoundedJSONTransport
from .kubernetes import (
    KubernetesInventoryClient,
    KubernetesResourceSpec,
)


_POD_UID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_KERNEL_OOM_PREFIX = "oom-kill:constraint=CONSTRAINT_MEMCG"
_OOM_MEMCG = re.compile(r"(?:^|,)oom_memcg=(?P<cgroup>/[^,\s]+)")
_CGROUP_POD_UID = re.compile(
    r"/kubepods[^,\s]*?-pod(?P<pod_uid>[0-9a-f_]{36})\.slice(?:[/,\s]|$)"
)


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Loki base_url must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Loki base_url must not contain credentials or query data")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _timestamp_ns(value: str, field: str) -> int:
    try:
        timestamp = parse_time(value, field).timestamp()
    except Exception as error:
        raise PermanentProviderError(f"{field} is invalid") from error
    return int(timestamp * 1_000_000_000)


def _format_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _normalized_pod_uid(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.lower().replace("_", "-")
    return normalized if _POD_UID.fullmatch(normalized) else None


@dataclass(frozen=True)
class LokiLogEntry:
    timestamp_ns: int
    line: str


@dataclass(frozen=True)
class LokiLogStream:
    labels: Mapping[str, str]
    entries: Tuple[LokiLogEntry, ...]


@dataclass(frozen=True)
class LokiRangeResult:
    streams: Tuple[LokiLogStream, ...]


class LokiRangeClient(Protocol):
    def query_range(
        self,
        expression: str,
        *,
        start: str,
        end: str,
        limit: int,
        timeout_seconds: float,
    ) -> LokiRangeResult:
        ...


class LokiHTTPAPI:
    """Minimal read-only client for Loki's bounded log range API."""

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: Optional[str] = None,
        transport: Optional[BoundedJSONTransport] = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        if bearer_token is not None and not bearer_token.strip():
            raise ValueError("Loki bearer_token must not be empty")
        self._headers = (
            {"Authorization": f"Bearer {bearer_token}"}
            if bearer_token is not None
            else {}
        )
        self._transport = transport or BoundedJSONTransport()

    def query_range(
        self,
        expression: str,
        *,
        start: str,
        end: str,
        limit: int,
        timeout_seconds: float,
    ) -> LokiRangeResult:
        if limit <= 0:
            raise PermanentProviderError("Loki query limit must be positive")
        parameters = urlencode(
            {
                "query": expression,
                "start": str(_timestamp_ns(start, "Loki query start")),
                "end": str(_timestamp_ns(end, "Loki query end")),
                "limit": str(limit),
                "direction": "forward",
            }
        )
        payload = self._transport.get_json(
            f"{self._base_url}/loki/api/v1/query_range?{parameters}",
            timeout_seconds=timeout_seconds,
            headers=self._headers,
        )
        if payload.get("status") != "success":
            error_type = payload.get("errorType", "unknown")
            if error_type in {"timeout", "canceled"}:
                raise RetryableProviderError(f"Loki query failed: {error_type}")
            raise PermanentProviderError(f"Loki query failed: {error_type}")
        data = payload.get("data")
        if not isinstance(data, Mapping) or data.get("resultType") != "streams":
            raise PermanentProviderError(
                "Loki range query did not return log streams"
            )
        result = data.get("result")
        if not isinstance(result, list) or not all(
            isinstance(stream, Mapping) for stream in result
        ):
            raise PermanentProviderError("Loki stream result is malformed")

        streams = []
        entry_count = 0
        for item in result:
            labels = item.get("stream")
            values = item.get("values")
            if not isinstance(labels, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in labels.items()
            ):
                raise PermanentProviderError("Loki stream labels are malformed")
            if not isinstance(values, list):
                raise PermanentProviderError("Loki stream values are malformed")
            entries = []
            for value in values:
                if (
                    not isinstance(value, list)
                    or len(value) != 2
                    or not isinstance(value[0], str)
                    or not value[0].isdigit()
                    or not isinstance(value[1], str)
                ):
                    raise PermanentProviderError("Loki log entry is malformed")
                entries.append(LokiLogEntry(int(value[0]), value[1]))
                entry_count += 1
            streams.append(LokiLogStream(dict(labels), tuple(entries)))
        if entry_count > limit:
            raise PermanentProviderError(
                "Loki returned more log entries than the requested limit"
            )
        return LokiRangeResult(tuple(streams))


@dataclass(frozen=True)
class ScopedPodIdentity:
    name: str
    uid: str


class LokiKernelOOMProvider:
    """Normalize exact, Pod-UID-scoped kernel memcg OOM lines from Loki.

    Kubernetes metadata supplies the currently trusted Pod UID/name mapping.
    Loki is queried only for those UIDs, and each returned label must agree with
    the UID parsed independently from the kernel cgroup path.
    """

    provider_name = "loki-kernel-oom-provider"
    pattern_id = "kernel-cgroup-oom"

    def __init__(
        self,
        loki_client: LokiRangeClient,
        kubernetes_client: KubernetesInventoryClient,
        *,
        cluster_id: str,
        pod_page_size: int = 100,
        max_raw_pods: int = 500,
        max_matches: int = 50,
    ) -> None:
        if not cluster_id.strip():
            raise ValueError("Loki kernel OOM cluster_id must not be empty")
        if pod_page_size <= 0 or max_raw_pods <= 0 or max_matches <= 0:
            raise ValueError("Loki kernel OOM limits must be positive")
        self._loki = loki_client
        self._kubernetes = kubernetes_client
        self._cluster_id = cluster_id
        self._pod_page_size = pod_page_size
        self._max_raw_pods = max_raw_pods
        self._max_matches = max_matches

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        deadline = time.monotonic() + request.timeout_seconds
        pods = self._scoped_pods(request, deadline)
        if not pods:
            return ProviderBatch()

        expression = self._scoped_expression(tuple(pods.values()))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetryableProviderError("Loki collection deadline exhausted")
        result = self._loki.query_range(
            expression,
            start=request.window.start,
            end=request.window.end,
            limit=self._max_matches + 1,
            timeout_seconds=remaining,
        )
        start_ns = _timestamp_ns(request.window.start, "EvidenceWindow.start")
        end_ns = _timestamp_ns(request.window.end, "EvidenceWindow.end")
        matches: Dict[str, list[LokiLogEntry]] = {uid: [] for uid in pods}
        raw_matches = 0
        for stream in result.streams:
            if (
                stream.labels.get("cluster_id") != self._cluster_id
                or stream.labels.get("job") != "kernel-journal"
            ):
                raise PermanentProviderError(
                    "Loki returned a kernel OOM stream outside the trusted source"
                )
            label_uid = _normalized_pod_uid(stream.labels.get("pod_uid"))
            if label_uid is None or label_uid not in pods:
                raise PermanentProviderError(
                    "Loki returned a kernel OOM stream outside the scoped Pod UIDs"
                )
            for entry in stream.entries:
                if entry.timestamp_ns < start_ns or entry.timestamp_ns > end_ns:
                    raise PermanentProviderError(
                        "Loki returned a log entry outside the requested time window"
                    )
                parsed_uid = self._kernel_oom_pod_uid(entry.line)
                if parsed_uid is None:
                    raise PermanentProviderError(
                        "Loki returned a non-matching line for the kernel OOM query"
                    )
                if parsed_uid != label_uid:
                    raise PermanentProviderError(
                        "Loki Pod UID label disagrees with the kernel cgroup path"
                    )
                raw_matches += 1
                if raw_matches <= self._max_matches:
                    matches[label_uid].append(entry)

        truncated = raw_matches > self._max_matches
        drafts = []
        for uid, entries in sorted(matches.items(), key=lambda item: pods[item[0]].name):
            if not entries:
                continue
            entries.sort(key=lambda item: item.timestamp_ns)
            pod = pods[uid]
            drafts.append(
                EvidenceDraft(
                    source="loki",
                    kind="log-pattern",
                    observed_at=_format_ns(entries[-1].timestamp_ns),
                    subject={
                        "cluster_id": self._cluster_id,
                        "api_version": "v1",
                        "kind": "Pod",
                        "namespace": request.scope.namespace,
                        "name": pod.name,
                        "uid": pod.uid,
                        "exists": True,
                    },
                    summary=(
                        f"Kernel cgroup OOM signal matched Pod {pod.name} "
                        f"{len(entries)} time(s)."
                    ),
                    facts={
                        "pattern_id": self.pattern_id,
                        "kernel_constraint": "CONSTRAINT_MEMCG",
                        "match_count": len(entries),
                        "pod_uid": pod.uid,
                        "first_match_at": _format_ns(entries[0].timestamp_ns),
                        "last_match_at": _format_ns(entries[-1].timestamp_ns),
                    },
                    provider=self.provider_name,
                    query=expression,
                    locator=(
                        f"loki://kernel-journal/{self._cluster_id}/"
                        f"{request.scope.namespace}/Pod/{pod.name}"
                    ),
                )
            )
        if len(drafts) > request.scope.max_items:
            raise PermanentProviderError(
                "Loki kernel OOM result exceeded the Evidence item budget"
            )
        if truncated:
            return ProviderBatch(
                items=tuple(drafts),
                status="PARTIAL",
                error=f"kernel OOM matches exceeded {self._max_matches} limit",
            )
        return ProviderBatch(items=tuple(drafts))

    def _scoped_pods(
        self,
        request: CollectionRequest,
        deadline: float,
    ) -> Dict[str, ScopedPodIdentity]:
        resource = KubernetesResourceSpec("v1", "Pod")
        continue_token: Optional[str] = None
        raw_count = 0
        pods: Dict[str, ScopedPodIdentity] = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetryableProviderError("Kubernetes Pod lookup deadline exhausted")
            page = self._kubernetes.list_resource_page(
                resource,
                namespace=request.scope.namespace,
                limit=min(self._pod_page_size, self._max_raw_pods - raw_count),
                continue_token=continue_token,
                timeout_seconds=remaining,
            )
            raw_count += len(page.items)
            for item in page.items:
                metadata = item.get("metadata")
                if not isinstance(metadata, Mapping):
                    raise PermanentProviderError(
                        "Kubernetes Pod metadata is malformed"
                    )
                name = metadata.get("name")
                uid = _normalized_pod_uid(metadata.get("uid"))
                if not request.scope.contains_resource_name(name):
                    continue
                if not isinstance(name, str) or uid is None:
                    raise PermanentProviderError(
                        "Scoped Kubernetes Pod has no valid name or UID"
                    )
                if uid in pods and pods[uid].name != name:
                    raise PermanentProviderError(
                        "Kubernetes returned one Pod UID for multiple names"
                    )
                pods[uid] = ScopedPodIdentity(name, uid)
            continue_token = page.continue_token
            if continue_token is None:
                return pods
            if raw_count >= self._max_raw_pods:
                raise PermanentProviderError(
                    "Kubernetes Pod lookup exceeded the configured raw resource limit"
                )

    def _scoped_expression(
        self,
        pods: Sequence[ScopedPodIdentity],
    ) -> str:
        label_uids = sorted(pod.uid.replace("-", "_") for pod in pods)
        uid_pattern = "^(?:" + "|".join(re.escape(uid) for uid in label_uids) + ")$"
        selectors = ",".join(
            (
                f"cluster_id={json.dumps(self._cluster_id)}",
                'job="kernel-journal"',
                f"pod_uid=~{json.dumps(uid_pattern)}",
            )
        )
        return "{" + selectors + "}" + f" |= {json.dumps(_KERNEL_OOM_PREFIX)}"

    @staticmethod
    def _kernel_oom_pod_uid(line: str) -> Optional[str]:
        if _KERNEL_OOM_PREFIX not in line:
            return None
        memcg = _OOM_MEMCG.search(line)
        if memcg is None:
            return None
        uid = _CGROUP_POD_UID.search(memcg.group("cgroup"))
        return _normalized_pod_uid(uid.group("pod_uid")) if uid else None
