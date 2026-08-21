"""Read-only Kubernetes state and Event adapter producing bounded Evidence."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from ..errors import PermanentProviderError, ProviderError, RetryableProviderError
from ..evidence import CollectionRequest, EvidenceDraft, ProviderBatch
from .http import BoundedJSONTransport, ProviderNotFound, ProviderPageExpired


_SUPPORTED_RESOURCES = {
    ("v1", "Pod"): ("api", "v1", "pods"),
    ("v1", "Service"): ("api", "v1", "services"),
    ("v1", "ConfigMap"): ("api", "v1", "configmaps"),
    ("apps/v1", "Deployment"): ("apis", "apps/v1", "deployments"),
    ("apps/v1", "StatefulSet"): ("apis", "apps/v1", "statefulsets"),
    ("apps/v1", "DaemonSet"): ("apis", "apps/v1", "daemonsets"),
}
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS_SUBDOMAIN = re.compile(
    r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"
)


def _validate_namespace(namespace: str) -> None:
    if len(namespace) > 63 or not _DNS_LABEL.fullmatch(namespace):
        raise PermanentProviderError("Kubernetes namespace is not a DNS label")


def _validate_resource_name(name: str) -> None:
    if len(name) > 253 or not _DNS_SUBDOMAIN.fullmatch(name):
        raise PermanentProviderError("Kubernetes resource name is not a DNS subdomain")


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Kubernetes api_server must be an HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Kubernetes api_server must not contain credentials or query data")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise PermanentProviderError(
            "Kubernetes Event has an invalid timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise PermanentProviderError("Kubernetes Event timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class KubernetesResourceSpec:
    api_version: str
    kind: str
    required: bool = False

    def __post_init__(self) -> None:
        if (self.api_version, self.kind) not in _SUPPORTED_RESOURCES:
            raise ValueError(
                f"unsupported or sensitive Kubernetes resource: "
                f"{self.api_version}/{self.kind}"
            )


@dataclass(frozen=True)
class KubernetesEventPage:
    items: Tuple[Mapping[str, Any], ...]
    continue_token: Optional[str] = None


class KubernetesReadClient(Protocol):
    def get_resource(
        self,
        resource: KubernetesResourceSpec,
        *,
        namespace: str,
        name: str,
        timeout_seconds: float,
    ) -> Optional[Mapping[str, Any]]:
        ...

    def list_event_page(
        self,
        *,
        namespace: str,
        involved_object_name: str,
        limit: int,
        continue_token: Optional[str],
        timeout_seconds: float,
    ) -> KubernetesEventPage:
        ...


class KubernetesHTTPAPI:
    """Minimal GET-only Kubernetes API client.

    The caller supplies an already-loaded ServiceAccount token and a transport
    configured with the cluster CA. This object never reads or returns Secrets.
    """

    def __init__(
        self,
        api_server: str,
        *,
        bearer_token: str,
        transport: BoundedJSONTransport,
    ) -> None:
        self._api_server = _validate_base_url(api_server)
        if not bearer_token.strip():
            raise ValueError("Kubernetes bearer_token must not be empty")
        self._headers = {"Authorization": f"Bearer {bearer_token}"}
        self._transport = transport

    def get_resource(
        self,
        resource: KubernetesResourceSpec,
        *,
        namespace: str,
        name: str,
        timeout_seconds: float,
    ) -> Optional[Mapping[str, Any]]:
        _validate_namespace(namespace)
        _validate_resource_name(name)
        group, version, plural = _SUPPORTED_RESOURCES[
            (resource.api_version, resource.kind)
        ]
        path = (
            f"/{group}/{version}/namespaces/{quote(namespace, safe='')}/"
            f"{plural}/{quote(name, safe='')}"
        )
        try:
            return self._transport.get_json(
                f"{self._api_server}{path}",
                timeout_seconds=timeout_seconds,
                headers=self._headers,
            )
        except ProviderNotFound:
            return None

    def list_event_page(
        self,
        *,
        namespace: str,
        involved_object_name: str,
        limit: int,
        continue_token: Optional[str],
        timeout_seconds: float,
    ) -> KubernetesEventPage:
        _validate_namespace(namespace)
        _validate_resource_name(involved_object_name)
        if limit <= 0:
            raise PermanentProviderError("Kubernetes Event page limit must be positive")
        parameters = {
            "fieldSelector": f"involvedObject.name={involved_object_name}",
            "limit": str(limit),
        }
        if continue_token:
            parameters["continue"] = continue_token
        path = (
            f"/api/v1/namespaces/{quote(namespace, safe='')}/events?"
            f"{urlencode(parameters)}"
        )
        payload = self._transport.get_json(
            f"{self._api_server}{path}",
            timeout_seconds=timeout_seconds,
            headers=self._headers,
        )
        items = payload.get("items")
        metadata = payload.get("metadata", {})
        if not isinstance(items, list) or not all(
            isinstance(item, Mapping) for item in items
        ):
            raise PermanentProviderError("Kubernetes EventList is malformed")
        if len(items) > limit:
            raise PermanentProviderError(
                "Kubernetes EventList exceeded the requested page limit"
            )
        if not isinstance(metadata, Mapping):
            raise PermanentProviderError("Kubernetes EventList metadata is malformed")
        next_token = metadata.get("continue") or None
        if next_token is not None and not isinstance(next_token, str):
            raise PermanentProviderError("Kubernetes continue token is malformed")
        return KubernetesEventPage(tuple(items), next_token)


class KubernetesStateProvider:
    """Collect one allowlisted resource kind plus its scoped core/v1 Events."""

    def __init__(
        self,
        client: KubernetesReadClient,
        resource: KubernetesResourceSpec,
        *,
        cluster_id: str,
        include_events: bool = True,
        event_page_size: int = 50,
        max_events: int = 100,
    ) -> None:
        if event_page_size <= 0 or max_events < 0:
            raise ValueError("Kubernetes Event limits are invalid")
        if not cluster_id.strip():
            raise ValueError("Kubernetes cluster_id must not be empty")
        self._client = client
        self._resource = resource
        self._cluster_id = cluster_id
        self._include_events = include_events
        self._event_page_size = event_page_size
        self._max_events = max_events

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        _validate_namespace(request.scope.namespace)
        for name in request.scope.resource_names:
            _validate_resource_name(name)
        deadline = time.monotonic() + request.timeout_seconds
        drafts = []
        partial_reasons = []
        provider_errors = []
        for name in request.scope.resource_names:
            try:
                resource = self._client.get_resource(
                    self._resource,
                    namespace=request.scope.namespace,
                    name=name,
                    timeout_seconds=self._remaining(deadline),
                )
                drafts.append(self._resource_draft(request, name, resource))
            except ProviderError as error:
                partial_reasons.append(f"resource {name}: {error}")
                provider_errors.append(error)

        if self._include_events and self._max_events:
            for name in request.scope.resource_names:
                try:
                    event_drafts, truncated = self._event_drafts(
                        request, name, deadline
                    )
                    drafts.extend(event_drafts)
                    if truncated:
                        partial_reasons.append(
                            f"events for {name}: result exceeded {self._max_events} limit"
                        )
                except ProviderError as error:
                    partial_reasons.append(f"events for {name}: {error}")
                    provider_errors.append(error)

        if not drafts and provider_errors:
            raise provider_errors[0]
        if len(drafts) > request.scope.max_items:
            raise PermanentProviderError(
                "Kubernetes query set can exceed the Evidence item budget"
            )
        if partial_reasons:
            return ProviderBatch(
                items=tuple(drafts),
                status="PARTIAL",
                error="; ".join(partial_reasons),
            )
        return ProviderBatch(items=tuple(drafts))

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetryableProviderError("Kubernetes collection deadline exhausted")
        return remaining

    def _resource_draft(
        self,
        request: CollectionRequest,
        requested_name: str,
        resource: Optional[Mapping[str, Any]],
    ) -> EvidenceDraft:
        query = (
            f"get {self._resource.api_version}/{self._resource.kind} "
            f"namespace={request.scope.namespace} name={requested_name}"
        )
        if resource is None:
            subject = {
                "cluster_id": self._cluster_id,
                "api_version": self._resource.api_version,
                "kind": self._resource.kind,
                "namespace": request.scope.namespace,
                "name": requested_name,
                "uid": None,
                "exists": False,
            }
            facts = {
                "result_status": "NOT_FOUND",
                "required": self._resource.required,
            }
            return EvidenceDraft(
                source="kubernetes",
                kind="resource-state",
                observed_at=request.window.end,
                subject=subject,
                summary=(
                    f"Kubernetes {self._resource.kind} {requested_name} was not found "
                    f"in namespace {request.scope.namespace}."
                ),
                facts=facts,
                provider="kubernetes-http-api",
                query=query,
                locator=(
                    f"k8s://{request.scope.namespace}/{self._resource.kind}/"
                    f"{requested_name}"
                ),
            )

        metadata = resource.get("metadata")
        if not isinstance(metadata, Mapping):
            raise PermanentProviderError("Kubernetes resource metadata is malformed")
        if (
            resource.get("apiVersion") != self._resource.api_version
            or resource.get("kind") != self._resource.kind
        ):
            raise PermanentProviderError(
                "Kubernetes API returned an unexpected resource kind"
            )
        if (
            metadata.get("name") != requested_name
            or metadata.get("namespace") != request.scope.namespace
        ):
            raise PermanentProviderError(
                "Kubernetes API returned a resource outside request scope"
            )
        uid = metadata.get("uid")
        if uid is not None and not isinstance(uid, str):
            raise PermanentProviderError("Kubernetes resource UID is malformed")
        subject = {
            "cluster_id": self._cluster_id,
            "api_version": self._resource.api_version,
            "kind": self._resource.kind,
            "namespace": request.scope.namespace,
            "name": requested_name,
            "uid": uid,
            "exists": True,
        }
        facts = self._safe_resource_facts(resource)
        facts["result_status"] = "FOUND"
        return EvidenceDraft(
            source="kubernetes",
            kind="resource-state",
            observed_at=request.window.end,
            subject=subject,
            summary=(
                f"Kubernetes {self._resource.kind} {requested_name} state was read "
                f"from namespace {request.scope.namespace}."
            ),
            facts=facts,
            provider="kubernetes-http-api",
            query=query,
            locator=(
                f"k8s://{request.scope.namespace}/{self._resource.kind}/{requested_name}"
            ),
        )

    def _safe_resource_facts(self, resource: Mapping[str, Any]) -> Dict[str, Any]:
        metadata = resource.get("metadata", {})
        spec = resource.get("spec", {})
        status = resource.get("status", {})
        if not all(isinstance(value, Mapping) for value in (metadata, spec, status)):
            raise PermanentProviderError("Kubernetes resource body is malformed")
        base: Dict[str, Any] = {
            "resource_version": metadata.get("resourceVersion"),
            "generation": metadata.get("generation"),
            "observed_generation": status.get("observedGeneration"),
        }
        if self._resource.kind == "Pod":
            container_statuses = []
            for item in status.get("containerStatuses", []):
                if not isinstance(item, Mapping):
                    continue
                state = item.get("state", {})
                last_state = item.get("lastState", {})
                waiting = state.get("waiting", {}) if isinstance(state, Mapping) else {}
                terminated = (
                    last_state.get("terminated", {})
                    if isinstance(last_state, Mapping)
                    else {}
                )
                container_statuses.append(
                    {
                        "name": item.get("name"),
                        "ready": item.get("ready"),
                        "restart_count": item.get("restartCount"),
                        "waiting_reason": (
                            waiting.get("reason") if isinstance(waiting, Mapping) else None
                        ),
                        "last_termination_reason": (
                            terminated.get("reason")
                            if isinstance(terminated, Mapping)
                            else None
                        ),
                        "last_exit_code": (
                            terminated.get("exitCode")
                            if isinstance(terminated, Mapping)
                            else None
                        ),
                    }
                )
            conditions = self._conditions(status.get("conditions", []))
            base.update(
                {
                    "phase": status.get("phase"),
                    "qos_class": status.get("qosClass"),
                    "conditions": conditions,
                    "container_statuses": container_statuses,
                }
            )
            reasons = {
                item["waiting_reason"]
                for item in container_statuses
                if item["waiting_reason"]
            }
            terminations = {
                item["last_termination_reason"]
                for item in container_statuses
                if item["last_termination_reason"]
            }
            if len(reasons) == 1:
                base["waiting_reason"] = next(iter(reasons))
            if len(terminations) == 1:
                base["last_termination_reason"] = next(iter(terminations))
            return base

        if self._resource.kind in {"Deployment", "StatefulSet", "DaemonSet"}:
            base.update(
                {
                    "desired_replicas": spec.get("replicas"),
                    "replicas": status.get("replicas"),
                    "ready_replicas": status.get("readyReplicas"),
                    "available_replicas": status.get("availableReplicas"),
                    "updated_replicas": status.get("updatedReplicas"),
                    "unavailable_replicas": status.get("unavailableReplicas"),
                    "conditions": self._conditions(status.get("conditions", [])),
                }
            )
            return base

        if self._resource.kind == "Service":
            base.update(
                {
                    "service_type": spec.get("type"),
                    "ip_family_policy": spec.get("ipFamilyPolicy"),
                    "port_count": len(spec.get("ports", []))
                    if isinstance(spec.get("ports", []), list)
                    else None,
                }
            )
            return base

        if self._resource.kind == "ConfigMap":
            # Deliberately omit data and binaryData values.
            data = resource.get("data", {})
            binary_data = resource.get("binaryData", {})
            base.update(
                {
                    "required": self._resource.required,
                    "data_key_count": len(data) if isinstance(data, Mapping) else None,
                    "binary_data_key_count": (
                        len(binary_data) if isinstance(binary_data, Mapping) else None
                    ),
                }
            )
            return base
        return base

    @staticmethod
    def _conditions(values: Any) -> Sequence[Dict[str, Any]]:
        if not isinstance(values, list):
            return []
        conditions = []
        for item in values:
            if not isinstance(item, Mapping):
                continue
            conditions.append(
                {
                    "type": item.get("type"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                    "last_transition_time": item.get("lastTransitionTime"),
                }
            )
        return conditions

    def _event_drafts(
        self,
        request: CollectionRequest,
        requested_name: str,
        deadline: float,
    ) -> Tuple[Tuple[EvidenceDraft, ...], bool]:
        events = []
        continue_token = None
        truncated = False
        restarted = False
        while len(events) < self._max_events:
            remaining_limit = self._max_events - len(events)
            try:
                page = self._client.list_event_page(
                    namespace=request.scope.namespace,
                    involved_object_name=requested_name,
                    limit=min(self._event_page_size, remaining_limit),
                    continue_token=continue_token,
                    timeout_seconds=self._remaining(deadline),
                )
            except ProviderPageExpired:
                if continue_token is None or restarted:
                    raise
                events = []
                continue_token = None
                restarted = True
                continue
            events.extend(page.items)
            continue_token = page.continue_token
            if not continue_token:
                break
        if continue_token:
            truncated = True

        start = _parse_time(request.window.start)
        end = _parse_time(request.window.end)
        drafts = []
        for event in events:
            event_time = self._event_time(event)
            if event_time < start or event_time > end:
                continue
            involved = event.get("involvedObject", {})
            metadata = event.get("metadata", {})
            if not isinstance(involved, Mapping) or not isinstance(metadata, Mapping):
                raise PermanentProviderError("Kubernetes Event is malformed")
            if (
                involved.get("name") != requested_name
                or metadata.get("namespace") != request.scope.namespace
            ):
                raise PermanentProviderError(
                    "Kubernetes API returned an Event outside request scope"
                )
            reason = event.get("reason")
            message = event.get("message")
            if reason is not None and not isinstance(reason, str):
                raise PermanentProviderError("Kubernetes Event reason is malformed")
            if message is not None and not isinstance(message, str):
                raise PermanentProviderError("Kubernetes Event message is malformed")
            message_excerpt = message[:1000] if message is not None else None
            facts: Dict[str, Any] = {
                "type": event.get("type"),
                "reason": reason,
                "message_code": reason,
                "message": message_excerpt,
                "message_truncated": bool(message and len(message) > 1000),
                "count": event.get("count"),
                "source_component": (
                    event.get("source", {}).get("component")
                    if isinstance(event.get("source", {}), Mapping)
                    else None
                ),
            }
            self._add_missing_reference(facts, message)
            subject = {
                "cluster_id": self._cluster_id,
                "api_version": involved.get("apiVersion") or self._resource.api_version,
                "kind": involved.get("kind") or self._resource.kind,
                "namespace": request.scope.namespace,
                "name": requested_name,
                "uid": involved.get("uid"),
                "exists": True,
            }
            drafts.append(
                EvidenceDraft(
                    source="kubernetes",
                    kind="kubernetes-event",
                    observed_at=_format_time(event_time),
                    subject=subject,
                    summary=(
                        f"Kubernetes Event {reason or 'Unknown'} was observed for "
                        f"{requested_name}."
                    ),
                    facts=facts,
                    provider="kubernetes-http-api",
                    query=(
                        "list core/v1/Event "
                        f"namespace={request.scope.namespace} "
                        f"fieldSelector=involvedObject.name={requested_name}"
                    ),
                    locator=(
                        f"k8s://{request.scope.namespace}/Event/"
                        f"{metadata.get('name', 'unknown')}"
                    ),
                    freshness="recent",
                )
            )
        return tuple(drafts), truncated

    @staticmethod
    def _event_time(event: Mapping[str, Any]) -> datetime:
        series = event.get("series", {})
        candidates = (
            event.get("eventTime"),
            series.get("lastObservedTime") if isinstance(series, Mapping) else None,
            event.get("lastTimestamp"),
            event.get("firstTimestamp"),
            (
                event.get("metadata", {}).get("creationTimestamp")
                if isinstance(event.get("metadata", {}), Mapping)
                else None
            ),
        )
        for value in candidates:
            if isinstance(value, str) and value:
                return _parse_time(value)
        raise PermanentProviderError("Kubernetes Event has no usable timestamp")

    @staticmethod
    def _add_missing_reference(facts: Dict[str, Any], message: Any) -> None:
        if not isinstance(message, str):
            return
        match = re.search(
            r'(?i)\b(configmap|secret)\s+["\']?([a-z0-9]([-a-z0-9]*[a-z0-9])?)["\']?\s+not found',
            message,
        )
        if match:
            facts["missing_kind"] = match.group(1).title().replace("map", "Map")
            facts["missing_name"] = match.group(2)
