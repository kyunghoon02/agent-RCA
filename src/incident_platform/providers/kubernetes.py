"""Read-only Kubernetes state and Event adapter producing bounded Evidence."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from ..errors import PermanentProviderError, ProviderError, RetryableProviderError
from ..evidence import CollectionRequest, EvidenceDraft, ProviderBatch, ResourceScope
from .http import BoundedJSONTransport, ProviderNotFound, ProviderPageExpired


_SUPPORTED_RESOURCES = {
    ("v1", "Pod"): ("api", "v1", "pods", True),
    ("v1", "Service"): ("api", "v1", "services", True),
    ("v1", "ConfigMap"): ("api", "v1", "configmaps", True),
    ("v1", "Node"): ("api", "v1", "nodes", False),
    ("apps/v1", "Deployment"): ("apis", "apps/v1", "deployments", True),
    ("apps/v1", "ReplicaSet"): ("apis", "apps/v1", "replicasets", True),
    ("apps/v1", "StatefulSet"): ("apis", "apps/v1", "statefulsets", True),
    ("apps/v1", "DaemonSet"): ("apis", "apps/v1", "daemonsets", True),
    (
        "discovery.k8s.io/v1",
        "EndpointSlice",
    ): ("apis", "discovery.k8s.io/v1", "endpointslices", True),
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


def _validate_field_selector_value(value: str, label: str) -> None:
    if (
        not value
        or len(value) > 253
        or any(character in value for character in (",", "=", "\x00", "\n", "\r"))
    ):
        raise PermanentProviderError(
            f"Kubernetes {label} is not safe for a field selector"
        )


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


@dataclass(frozen=True)
class KubernetesResourcePage:
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
        involved_object_kind: str,
        involved_object_uid: Optional[str],
        limit: int,
        continue_token: Optional[str],
        timeout_seconds: float,
    ) -> KubernetesEventPage:
        ...


class KubernetesInventoryClient(Protocol):
    def list_resource_page(
        self,
        resource: KubernetesResourceSpec,
        *,
        namespace: Optional[str],
        limit: int,
        continue_token: Optional[str],
        timeout_seconds: float,
    ) -> KubernetesResourcePage:
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
        group, version, plural, namespaced = _SUPPORTED_RESOURCES[
            (resource.api_version, resource.kind)
        ]
        if not namespaced:
            raise PermanentProviderError(
                "get_resource requires a namespace-scoped Kubernetes resource"
            )
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

    def list_resource_page(
        self,
        resource: KubernetesResourceSpec,
        *,
        namespace: Optional[str],
        limit: int,
        continue_token: Optional[str],
        timeout_seconds: float,
    ) -> KubernetesResourcePage:
        if limit <= 0:
            raise PermanentProviderError("Kubernetes resource page limit must be positive")
        group, version, plural, namespaced = _SUPPORTED_RESOURCES[
            (resource.api_version, resource.kind)
        ]
        if namespaced:
            if namespace is None:
                raise PermanentProviderError(
                    "namespaced Kubernetes inventory requires a namespace"
                )
            _validate_namespace(namespace)
            path = (
                f"/{group}/{version}/namespaces/{quote(namespace, safe='')}/{plural}"
            )
        else:
            if namespace is not None:
                raise PermanentProviderError(
                    "cluster-scoped Kubernetes inventory must not set a namespace"
                )
            path = f"/{group}/{version}/{plural}"
        parameters = {"limit": str(limit)}
        if continue_token:
            parameters["continue"] = continue_token
        payload = self._transport.get_json(
            f"{self._api_server}{path}?{urlencode(parameters)}",
            timeout_seconds=timeout_seconds,
            headers=self._headers,
        )
        if (
            payload.get("apiVersion") != resource.api_version
            or payload.get("kind") != f"{resource.kind}List"
        ):
            raise PermanentProviderError(
                "Kubernetes inventory returned an unexpected list kind: "
                f"expected {resource.api_version}/{resource.kind}List, got "
                f"{payload.get('apiVersion')}/{payload.get('kind')}"
            )
        items = payload.get("items")
        metadata = payload.get("metadata", {})
        if not isinstance(items, list) or not all(
            isinstance(item, Mapping) for item in items
        ):
            raise PermanentProviderError("Kubernetes resource list is malformed")
        if len(items) > limit:
            raise PermanentProviderError(
                "Kubernetes resource list exceeded the requested page limit"
            )
        if not isinstance(metadata, Mapping):
            raise PermanentProviderError("Kubernetes resource list metadata is malformed")
        next_token = metadata.get("continue") or None
        if next_token is not None and not isinstance(next_token, str):
            raise PermanentProviderError("Kubernetes continue token is malformed")
        for item in items:
            item_metadata = item.get("metadata")
            if not isinstance(item_metadata, Mapping):
                raise PermanentProviderError(
                    "Kubernetes inventory resource metadata is malformed"
                )
            item_api_version = item.get("apiVersion")
            item_kind = item.get("kind")
            if (
                item_api_version not in (None, resource.api_version)
                or item_kind not in (None, resource.kind)
            ):
                raise PermanentProviderError(
                    "Kubernetes inventory returned an unexpected resource kind: "
                    f"expected {resource.api_version}/{resource.kind}, got "
                    f"{item_api_version}/{item_kind}"
                )
            item_namespace = item_metadata.get("namespace")
            if namespaced and item_namespace != namespace:
                raise PermanentProviderError(
                    "Kubernetes inventory returned a resource outside namespace scope"
                )
            if not namespaced and item_namespace is not None:
                raise PermanentProviderError(
                    "cluster-scoped Kubernetes inventory returned a namespace"
                )
        normalized_items = tuple(
            {
                **dict(item),
                "apiVersion": resource.api_version,
                "kind": resource.kind,
            }
            for item in items
        )
        return KubernetesResourcePage(normalized_items, next_token)

    def list_event_page(
        self,
        *,
        namespace: str,
        involved_object_name: str,
        involved_object_kind: str,
        involved_object_uid: Optional[str],
        limit: int,
        continue_token: Optional[str],
        timeout_seconds: float,
    ) -> KubernetesEventPage:
        _validate_namespace(namespace)
        _validate_resource_name(involved_object_name)
        _validate_field_selector_value(involved_object_kind, "resource kind")
        if involved_object_uid is not None:
            _validate_field_selector_value(involved_object_uid, "resource UID")
        if limit <= 0:
            raise PermanentProviderError("Kubernetes Event page limit must be positive")
        selectors = [
            f"involvedObject.name={involved_object_name}",
            f"involvedObject.kind={involved_object_kind}",
        ]
        if involved_object_uid is not None:
            selectors.append(f"involvedObject.uid={involved_object_uid}")
        parameters = {
            "fieldSelector": ",".join(selectors),
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


class KubernetesInventoryProvider:
    """Collect a bounded, read-only workload topology for one namespace.

    The requested names are trusted logical workload roots. Dynamic ReplicaSet,
    Pod, and EndpointSlice names are admitted only through Kubernetes ownership
    or selector relationships rooted at those exact names.
    """

    _NAMESPACED_SPECS = (
        KubernetesResourceSpec("v1", "Service"),
        KubernetesResourceSpec("apps/v1", "Deployment"),
        KubernetesResourceSpec("apps/v1", "ReplicaSet"),
        KubernetesResourceSpec("v1", "Pod"),
        KubernetesResourceSpec("discovery.k8s.io/v1", "EndpointSlice"),
    )
    _NODE_SPEC = KubernetesResourceSpec("v1", "Node")

    def __init__(
        self,
        client: KubernetesInventoryClient,
        *,
        cluster_id: str,
        page_size: int = 100,
        max_raw_resources: int = 500,
    ) -> None:
        if not cluster_id.strip():
            raise ValueError("Kubernetes cluster_id must not be empty")
        if not 1 <= page_size <= 500:
            raise ValueError("Kubernetes inventory page_size must be between 1 and 500")
        if not 1 <= max_raw_resources <= 5000:
            raise ValueError(
                "Kubernetes inventory max_raw_resources must be between 1 and 5000"
            )
        self._client = client
        self._cluster_id = cluster_id
        self._page_size = page_size
        self._max_raw_resources = max_raw_resources

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        _validate_namespace(request.scope.namespace)
        roots = set(request.scope.resource_names)
        for name in roots:
            _validate_resource_name(name)
        allowed_prefixes = {f"{name}-" for name in roots}
        unexpected_prefixes = (
            set(request.scope.resource_name_prefixes) - allowed_prefixes
        )
        if unexpected_prefixes:
            raise PermanentProviderError(
                "Kubernetes inventory prefixes must be derived from exact roots"
            )
        deadline = time.monotonic() + request.timeout_seconds
        listed: Dict[str, Tuple[Mapping[str, Any], ...]] = {}
        total = 0
        for spec in self._NAMESPACED_SPECS:
            items = self._list_all(
                spec,
                namespace=request.scope.namespace,
                deadline=deadline,
                remaining=self._max_raw_resources - total,
            )
            listed[spec.kind] = items
            total += len(items)
        nodes = self._list_all(
            self._NODE_SPEC,
            namespace=None,
            deadline=deadline,
            remaining=self._max_raw_resources - total,
        )

        retained = self._retain_rooted_resources(listed, roots)
        node_by_name = {
            str(item.get("metadata", {}).get("name")): item
            for item in nodes
            if isinstance(item.get("metadata"), Mapping)
        }
        drafts = []
        for kind in ("Service", "Deployment", "ReplicaSet", "Pod", "EndpointSlice"):
            for resource in sorted(
                retained[kind], key=lambda item: str(item["metadata"]["name"])
            ):
                name = resource["metadata"]["name"]
                if not request.scope.contains_resource_name(name):
                    raise PermanentProviderError(
                        f"Kubernetes inventory subject {name!r} exceeded root scope"
                    )
                facts = self._safe_inventory_facts(resource)
                facts["relationships"] = self._relationships_for(
                    resource,
                    retained=retained,
                    node_by_name=node_by_name,
                )
                drafts.append(
                    EvidenceDraft(
                        source="kubernetes",
                        kind="resource-state",
                        observed_at=request.window.end,
                        subject=self._subject(resource, request.scope.namespace),
                        summary=(
                            f"Kubernetes {kind} {name} was read from the bounded "
                            f"{request.scope.namespace} inventory."
                        ),
                        facts=facts,
                        provider="kubernetes-inventory-http-api",
                        query=(
                            f"list {resource['apiVersion']}/{kind} "
                            f"namespace={request.scope.namespace} rooted=true"
                        ),
                        locator=f"k8s://{request.scope.namespace}/{kind}/{name}",
                    )
                )
        if not drafts:
            raise PermanentProviderError(
                "Kubernetes inventory found no resources for the requested roots"
            )
        if len(drafts) > request.scope.max_items:
            raise PermanentProviderError(
                "Kubernetes inventory exceeded the Evidence item budget"
            )
        return ProviderBatch(items=tuple(drafts))

    def _list_all(
        self,
        spec: KubernetesResourceSpec,
        *,
        namespace: Optional[str],
        deadline: float,
        remaining: int,
    ) -> Tuple[Mapping[str, Any], ...]:
        if remaining <= 0:
            raise PermanentProviderError(
                "Kubernetes inventory exceeded the raw resource budget"
            )
        items = []
        continue_token = None
        restarted = False
        while True:
            limit = min(self._page_size, remaining - len(items))
            if limit <= 0:
                raise PermanentProviderError(
                    "Kubernetes inventory exceeded the raw resource budget"
                )
            try:
                page = self._client.list_resource_page(
                    spec,
                    namespace=namespace,
                    limit=limit,
                    continue_token=continue_token,
                    timeout_seconds=KubernetesStateProvider._remaining(deadline),
                )
            except ProviderPageExpired:
                if continue_token is None or restarted:
                    raise
                items = []
                continue_token = None
                restarted = True
                continue
            items.extend(page.items)
            continue_token = page.continue_token
            if not continue_token:
                return tuple(items)

    @staticmethod
    def _metadata(resource: Mapping[str, Any]) -> Mapping[str, Any]:
        metadata = resource.get("metadata")
        if not isinstance(metadata, Mapping):
            raise PermanentProviderError(
                "Kubernetes inventory resource metadata is malformed"
            )
        name = metadata.get("name")
        uid = metadata.get("uid")
        if not isinstance(name, str) or not name:
            raise PermanentProviderError("Kubernetes inventory resource name is malformed")
        if not isinstance(uid, str) or not uid:
            raise PermanentProviderError("Kubernetes inventory resource UID is required")
        return metadata

    @classmethod
    def _owner_matches(
        cls,
        resource: Mapping[str, Any],
        *,
        kind: str,
        allowed_names: set[str],
        allowed_uids: set[str],
    ) -> bool:
        metadata = cls._metadata(resource)
        owners = metadata.get("ownerReferences", [])
        if not isinstance(owners, list):
            raise PermanentProviderError("Kubernetes ownerReferences is malformed")
        return any(
            isinstance(owner, Mapping)
            and owner.get("kind") == kind
            and (
                owner.get("name") in allowed_names
                or owner.get("uid") in allowed_uids
            )
            for owner in owners
        )

    @classmethod
    def _retain_rooted_resources(
        cls,
        listed: Mapping[str, Tuple[Mapping[str, Any], ...]],
        roots: set[str],
    ) -> Dict[str, Tuple[Mapping[str, Any], ...]]:
        services = tuple(
            item for item in listed["Service"] if cls._metadata(item)["name"] in roots
        )
        deployments = tuple(
            item
            for item in listed["Deployment"]
            if cls._metadata(item)["name"] in roots
        )
        deployment_names = {cls._metadata(item)["name"] for item in deployments}
        deployment_uids = {cls._metadata(item)["uid"] for item in deployments}
        replica_sets = tuple(
            item
            for item in listed["ReplicaSet"]
            if cls._owner_matches(
                item,
                kind="Deployment",
                allowed_names=deployment_names,
                allowed_uids=deployment_uids,
            )
        )
        replica_set_names = {cls._metadata(item)["name"] for item in replica_sets}
        replica_set_uids = {cls._metadata(item)["uid"] for item in replica_sets}
        pods = tuple(
            item
            for item in listed["Pod"]
            if cls._owner_matches(
                item,
                kind="ReplicaSet",
                allowed_names=replica_set_names,
                allowed_uids=replica_set_uids,
            )
        )
        endpoint_slices = tuple(
            item
            for item in listed["EndpointSlice"]
            if isinstance(cls._metadata(item).get("labels"), Mapping)
            and cls._metadata(item)["labels"].get(
                "kubernetes.io/service-name"
            )
            in roots
        )
        return {
            "Service": services,
            "Deployment": deployments,
            "ReplicaSet": replica_sets,
            "Pod": pods,
            "EndpointSlice": endpoint_slices,
        }

    def _subject(
        self, resource: Mapping[str, Any], namespace: str
    ) -> Dict[str, Any]:
        metadata = self._metadata(resource)
        if metadata.get("namespace") != namespace:
            raise PermanentProviderError(
                "Kubernetes inventory resource is outside request scope"
            )
        return {
            "cluster_id": self._cluster_id,
            "api_version": resource["apiVersion"],
            "kind": resource["kind"],
            "namespace": namespace,
            "name": metadata["name"],
            "uid": metadata["uid"],
            "exists": True,
        }

    @classmethod
    def _reference(
        cls,
        resource: Mapping[str, Any],
        *,
        relation_type: str,
        reference_key: str,
    ) -> Dict[str, Any]:
        metadata = cls._metadata(resource)
        return {
            "relation_type": relation_type,
            "api_version": resource["apiVersion"],
            "kind": resource["kind"],
            "namespace": metadata.get("namespace"),
            "name": metadata["name"],
            "uid": metadata["uid"],
            "reference_key": reference_key,
        }

    @classmethod
    def _relationships_for(
        cls,
        resource: Mapping[str, Any],
        *,
        retained: Mapping[str, Tuple[Mapping[str, Any], ...]],
        node_by_name: Mapping[str, Mapping[str, Any]],
    ) -> list[Dict[str, Any]]:
        kind = resource["kind"]
        metadata = cls._metadata(resource)
        relationships: list[Dict[str, Any]] = []
        if kind == "Deployment":
            for replica_set in retained["ReplicaSet"]:
                if cls._owner_matches(
                    replica_set,
                    kind="Deployment",
                    allowed_names={metadata["name"]},
                    allowed_uids={metadata["uid"]},
                ):
                    relationships.append(
                        cls._reference(
                            replica_set,
                            relation_type="OWNS",
                            reference_key="deployment-owner-reference",
                        )
                    )
        elif kind == "ReplicaSet":
            for pod in retained["Pod"]:
                if cls._owner_matches(
                    pod,
                    kind="ReplicaSet",
                    allowed_names={metadata["name"]},
                    allowed_uids={metadata["uid"]},
                ):
                    relationships.append(
                        cls._reference(
                            pod,
                            relation_type="OWNS",
                            reference_key="replicaset-owner-reference",
                        )
                    )
        elif kind == "Service":
            spec = resource.get("spec", {})
            selector = spec.get("selector", {}) if isinstance(spec, Mapping) else {}
            if isinstance(selector, Mapping) and selector:
                for pod in retained["Pod"]:
                    labels = cls._metadata(pod).get("labels", {})
                    if isinstance(labels, Mapping) and all(
                        labels.get(key) == value for key, value in selector.items()
                    ):
                        relationships.append(
                            cls._reference(
                                pod,
                                relation_type="SELECTS",
                                reference_key="service-selector",
                            )
                        )
            for endpoint_slice in retained["EndpointSlice"]:
                labels = cls._metadata(endpoint_slice).get("labels", {})
                if (
                    isinstance(labels, Mapping)
                    and labels.get("kubernetes.io/service-name") == metadata["name"]
                ):
                    relationships.append(
                        cls._reference(
                            endpoint_slice,
                            relation_type="ROUTES_TO",
                            reference_key="service-endpointslice-label",
                        )
                    )
        elif kind == "EndpointSlice":
            retained_pods = {
                cls._metadata(item)["uid"]: item for item in retained["Pod"]
            }
            endpoints = resource.get("endpoints", [])
            if isinstance(endpoints, list):
                for endpoint in endpoints:
                    target = endpoint.get("targetRef", {}) if isinstance(endpoint, Mapping) else {}
                    if not isinstance(target, Mapping) or target.get("kind") != "Pod":
                        continue
                    pod = retained_pods.get(target.get("uid"))
                    if pod is not None:
                        relationships.append(
                            cls._reference(
                                pod,
                                relation_type="ROUTES_TO",
                                reference_key="endpointslice-target-ref",
                            )
                        )
        elif kind == "Pod":
            spec = resource.get("spec", {})
            node_name = spec.get("nodeName") if isinstance(spec, Mapping) else None
            node = node_by_name.get(node_name) if isinstance(node_name, str) else None
            if node is not None:
                relationships.append(
                    cls._reference(
                        node,
                        relation_type="SCHEDULED_ON",
                        reference_key="pod-node-name",
                    )
                )
        relationships.sort(
            key=lambda item: (
                item["relation_type"],
                item["kind"],
                item["namespace"] or "",
                item["name"],
            )
        )
        return relationships

    @classmethod
    def _safe_inventory_facts(cls, resource: Mapping[str, Any]) -> Dict[str, Any]:
        metadata = cls._metadata(resource)
        spec = resource.get("spec", {})
        status = resource.get("status", {})
        if not isinstance(spec, Mapping) or not isinstance(status, Mapping):
            raise PermanentProviderError("Kubernetes inventory state is malformed")
        facts: Dict[str, Any] = {
            "result_status": "FOUND",
            "resource_version": metadata.get("resourceVersion"),
            "generation": metadata.get("generation"),
            "observed_generation": status.get("observedGeneration"),
        }
        kind = resource["kind"]
        if kind == "Service":
            ports = spec.get("ports", [])
            facts.update(
                {
                    "service_type": spec.get("type"),
                    "ip_family_policy": spec.get("ipFamilyPolicy"),
                    "port_count": len(ports) if isinstance(ports, list) else None,
                }
            )
        elif kind in {"Deployment", "ReplicaSet"}:
            facts.update(
                {
                    "desired_replicas": spec.get("replicas"),
                    "replicas": status.get("replicas"),
                    "ready_replicas": status.get("readyReplicas"),
                    "available_replicas": status.get("availableReplicas"),
                    "fully_labeled_replicas": status.get("fullyLabeledReplicas"),
                    "conditions": KubernetesStateProvider._conditions(
                        status.get("conditions", [])
                    ),
                }
            )
        elif kind == "Pod":
            container_statuses = status.get("containerStatuses", [])
            safe_statuses = []
            if isinstance(container_statuses, list):
                for item in container_statuses:
                    if not isinstance(item, Mapping):
                        continue
                    state = item.get("state", {})
                    waiting = state.get("waiting", {}) if isinstance(state, Mapping) else {}
                    last_state = item.get("lastState", {})
                    terminated = (
                        last_state.get("terminated", {})
                        if isinstance(last_state, Mapping)
                        else {}
                    )
                    safe_statuses.append(
                        {
                            "name": item.get("name"),
                            "ready": item.get("ready"),
                            "restart_count": item.get("restartCount"),
                            "waiting_reason": waiting.get("reason")
                            if isinstance(waiting, Mapping)
                            else None,
                            "last_termination_reason": terminated.get("reason")
                            if isinstance(terminated, Mapping)
                            else None,
                            "last_exit_code": terminated.get("exitCode")
                            if isinstance(terminated, Mapping)
                            else None,
                        }
                    )
            waiting_reasons = {
                item["waiting_reason"]
                for item in safe_statuses
                if item["waiting_reason"]
            }
            termination_reasons = {
                item["last_termination_reason"]
                for item in safe_statuses
                if item["last_termination_reason"]
            }
            exit_codes = {
                item["last_exit_code"]
                for item in safe_statuses
                if item["last_exit_code"] is not None
            }
            facts.update(
                {
                    "phase": status.get("phase"),
                    "qos_class": status.get("qosClass"),
                    "conditions": KubernetesStateProvider._conditions(
                        status.get("conditions", [])
                    ),
                    "container_statuses": safe_statuses,
                }
            )
            required_configmaps = cls._required_configmap_references(spec)
            if required_configmaps:
                facts["required_configmap_references"] = required_configmaps
            if len(waiting_reasons) == 1:
                facts["waiting_reason"] = next(iter(waiting_reasons))
            if len(termination_reasons) == 1:
                facts["last_termination_reason"] = next(iter(termination_reasons))
            if len(exit_codes) == 1:
                facts["last_exit_code"] = next(iter(exit_codes))
        elif kind == "EndpointSlice":
            endpoints = resource.get("endpoints", [])
            ready = 0
            total = 0
            if isinstance(endpoints, list):
                for endpoint in endpoints:
                    if not isinstance(endpoint, Mapping):
                        continue
                    total += 1
                    conditions = endpoint.get("conditions", {})
                    if isinstance(conditions, Mapping) and conditions.get("ready") is True:
                        ready += 1
            facts.update(
                {
                    "address_type": resource.get("addressType"),
                    "endpoint_count": total,
                    "ready_endpoint_count": ready,
                }
            )
        return facts

    @staticmethod
    def _required_configmap_references(
        pod_spec: Mapping[str, Any],
    ) -> list[Dict[str, Any]]:
        """Return required ConfigMap names without copying keys or Pod env values."""

        references: Dict[str, set[str]] = {}

        def add(reference: Any, reference_key: str) -> None:
            if not isinstance(reference, Mapping) or reference.get("optional") is True:
                return
            name = reference.get("name")
            if not isinstance(name, str):
                return
            _validate_resource_name(name)
            references.setdefault(name, set()).add(reference_key)

        volumes = pod_spec.get("volumes", [])
        if isinstance(volumes, list):
            for volume in volumes:
                if not isinstance(volume, Mapping):
                    continue
                add(volume.get("configMap"), "pod-volume-configmap")
                projected = volume.get("projected")
                sources = (
                    projected.get("sources", [])
                    if isinstance(projected, Mapping)
                    else []
                )
                if isinstance(sources, list):
                    for source in sources:
                        if isinstance(source, Mapping):
                            add(
                                source.get("configMap"),
                                "pod-projected-volume-configmap",
                            )

        for field in ("initContainers", "containers"):
            containers = pod_spec.get(field, [])
            if not isinstance(containers, list):
                continue
            for container in containers:
                if not isinstance(container, Mapping):
                    continue
                env_from = container.get("envFrom", [])
                if isinstance(env_from, list):
                    for source in env_from:
                        if isinstance(source, Mapping):
                            add(
                                source.get("configMapRef"),
                                "container-envfrom-configmap",
                            )
                env = container.get("env", [])
                if isinstance(env, list):
                    for variable in env:
                        value_from = (
                            variable.get("valueFrom", {})
                            if isinstance(variable, Mapping)
                            else {}
                        )
                        if isinstance(value_from, Mapping):
                            add(
                                value_from.get("configMapKeyRef"),
                                "container-env-configmap-key",
                            )

        return [
            {"name": name, "reference_keys": sorted(reference_keys)}
            for name, reference_keys in sorted(references.items())
        ]


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
        max_raw_events: Optional[int] = None,
    ) -> None:
        if event_page_size <= 0 or max_events < 0:
            raise ValueError("Kubernetes Event limits are invalid")
        effective_max_raw_events = (
            max(max_events, event_page_size * 5)
            if max_raw_events is None
            else max_raw_events
        )
        if effective_max_raw_events < max_events or effective_max_raw_events > 5000:
            raise ValueError(
                "Kubernetes max_raw_events must be between max_events and 5000"
            )
        if not cluster_id.strip():
            raise ValueError("Kubernetes cluster_id must not be empty")
        self._client = client
        self._resource = resource
        self._cluster_id = cluster_id
        self._include_events = include_events
        self._event_page_size = event_page_size
        self._max_events = max_events
        self._max_raw_events = effective_max_raw_events

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        _validate_namespace(request.scope.namespace)
        for name in request.scope.resource_names:
            _validate_resource_name(name)
        deadline = time.monotonic() + request.timeout_seconds
        drafts = []
        partial_reasons = []
        provider_errors = []
        event_target_uids: Dict[str, Optional[str]] = {
            name: None for name in request.scope.resource_names
        }
        for name in request.scope.resource_names:
            try:
                resource = self._client.get_resource(
                    self._resource,
                    namespace=request.scope.namespace,
                    name=name,
                    timeout_seconds=self._remaining(deadline),
                )
                resource_draft = self._resource_draft(request, name, resource)
                drafts.append(resource_draft)
                target_uid = resource_draft.subject.get("uid")
                event_target_uids[name] = (
                    str(target_uid) if isinstance(target_uid, str) else None
                )
                if event_target_uids[name] is not None:
                    _validate_field_selector_value(
                        event_target_uids[name] or "", "resource UID"
                    )
            except ProviderError as error:
                partial_reasons.append(f"resource {name}: {error}")
                provider_errors.append(error)

        if self._include_events and self._max_events:
            for name in request.scope.resource_names:
                try:
                    event_drafts, event_partial_reasons = self._event_drafts(
                        request,
                        name,
                        event_target_uids[name],
                        deadline,
                    )
                    drafts.extend(event_drafts)
                    for partial_reason in event_partial_reasons:
                        partial_reasons.append(
                            f"events for {name}: {partial_reason}"
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
        requested_uid: Optional[str],
        deadline: float,
    ) -> Tuple[Tuple[EvidenceDraft, ...], Tuple[str, ...]]:
        events = []
        continue_token = None
        restarted = False
        while len(events) < self._max_raw_events:
            remaining_limit = self._max_raw_events - len(events)
            try:
                page = self._client.list_event_page(
                    namespace=request.scope.namespace,
                    involved_object_name=requested_name,
                    involved_object_kind=self._resource.kind,
                    involved_object_uid=requested_uid,
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

        partial_reasons = []
        if continue_token:
            partial_reasons.append(
                f"raw scan exceeded {self._max_raw_events} limit before complete "
                "window coverage"
            )

        start = _parse_time(request.window.start)
        end = _parse_time(request.window.end)
        seen_event_objects = set()
        grouped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for event in events:
            involved = event.get("involvedObject", {})
            metadata = event.get("metadata", {})
            if not isinstance(involved, Mapping) or not isinstance(metadata, Mapping):
                raise PermanentProviderError("Kubernetes Event is malformed")
            involved_kind = involved.get("kind")
            involved_uid = involved.get("uid")
            if (
                involved.get("name") != requested_name
                or involved_kind != self._resource.kind
                or metadata.get("namespace") != request.scope.namespace
                or (requested_uid is not None and involved_uid != requested_uid)
            ):
                raise PermanentProviderError(
                    "Kubernetes API returned an Event outside request scope"
                )
            if involved_uid is not None and not isinstance(involved_uid, str):
                raise PermanentProviderError("Kubernetes Event UID is malformed")
            event_name = metadata.get("name")
            event_uid = metadata.get("uid")
            if event_name is not None and not isinstance(event_name, str):
                raise PermanentProviderError("Kubernetes Event name is malformed")
            if event_uid is not None and not isinstance(event_uid, str):
                raise PermanentProviderError("Kubernetes Event UID is malformed")
            event_time = self._event_time(event)
            reason = event.get("reason")
            message = event.get("message")
            if reason is not None and not isinstance(reason, str):
                raise PermanentProviderError("Kubernetes Event reason is malformed")
            if message is not None and not isinstance(message, str):
                raise PermanentProviderError("Kubernetes Event message is malformed")
            object_identity = event_uid or event_name or (
                _format_time(event_time),
                involved_uid,
                reason,
                message,
            )
            if object_identity in seen_event_objects:
                continue
            seen_event_objects.add(object_identity)
            if event_time < start or event_time > end:
                continue
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
            image_pull_code = self._image_pull_code(reason, message)
            if image_pull_code is not None:
                facts["image_pull_code"] = image_pull_code
            self._add_missing_reference(facts, message)
            raw_count = event.get("count")
            occurrence_count = (
                raw_count
                if isinstance(raw_count, int)
                and not isinstance(raw_count, bool)
                and raw_count > 0
                else 1
            )
            facts["count"] = occurrence_count
            facts["event_series_count"] = 1
            subject = {
                "cluster_id": self._cluster_id,
                "api_version": involved.get("apiVersion") or self._resource.api_version,
                "kind": involved_kind,
                "namespace": request.scope.namespace,
                "name": requested_name,
                "uid": involved_uid,
                "exists": True,
            }
            group_key = (
                involved_kind,
                involved_uid,
                reason,
                image_pull_code,
                facts.get("missing_kind"),
                facts.get("missing_name"),
                message_excerpt,
                facts.get("source_component"),
            )
            existing = grouped.get(group_key)
            if existing is not None:
                existing["facts"]["count"] += occurrence_count
                existing["facts"]["event_series_count"] += 1
                existing["facts"]["message_truncated"] = bool(
                    existing["facts"]["message_truncated"]
                    or facts["message_truncated"]
                )
                if event_time > existing["event_time"]:
                    existing["event_time"] = event_time
                    existing["event_name"] = event_name
                continue
            grouped[group_key] = {
                "event_time": event_time,
                "event_name": event_name,
                "reason": reason,
                "facts": facts,
                "subject": subject,
            }

        ordered = sorted(
            grouped.values(),
            key=lambda item: (
                item["event_time"],
                str(item["event_name"] or ""),
                str(item["reason"] or ""),
            ),
            reverse=True,
        )
        if len(ordered) > self._max_events:
            partial_reasons.append(
                f"in-window result exceeded {self._max_events} limit after aggregation"
            )
            ordered = ordered[: self._max_events]

        selectors = [
            f"involvedObject.name={requested_name}",
            f"involvedObject.kind={self._resource.kind}",
        ]
        if requested_uid is not None:
            selectors.append(f"involvedObject.uid={requested_uid}")
        drafts = []
        for item in ordered:
            reason = item["reason"]
            drafts.append(
                EvidenceDraft(
                    source="kubernetes",
                    kind="kubernetes-event",
                    observed_at=_format_time(item["event_time"]),
                    subject=item["subject"],
                    summary=(
                        f"Kubernetes Event {reason or 'Unknown'} was observed for "
                        f"{requested_name}."
                    ),
                    facts=item["facts"],
                    provider="kubernetes-http-api",
                    query=(
                        "list core/v1/Event "
                        f"namespace={request.scope.namespace} "
                        f"fieldSelector={','.join(selectors)}"
                    ),
                    locator=(
                        f"k8s://{request.scope.namespace}/Event/"
                        f"{item['event_name'] or 'unknown'}"
                    ),
                    freshness="recent",
                )
            )
        return tuple(drafts), tuple(partial_reasons)

    @staticmethod
    def _image_pull_code(
        reason: Optional[str], message: Optional[str]
    ) -> Optional[str]:
        """Normalize kubelet image-pull Events without copying registry details."""

        candidate = message or ""
        if "ImagePullBackOff" in candidate:
            return "ImagePullBackOff"
        if "ErrImagePull" in candidate:
            return "ErrImagePull"
        lowered = candidate.lower()
        if reason == "BackOff" and "pulling image" in lowered:
            return "ImagePullBackOff"
        if reason == "Failed" and any(
            marker in lowered
            for marker in (
                "failed to pull image",
                "failed to pull and unpack image",
                "failed to resolve image",
            )
        ):
            return "ErrImagePull"
        if reason in {"ErrImagePull", "ImagePullBackOff"}:
            return reason
        return None

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


class KubernetesIncidentProvider:
    """Combine rooted inventory, Events, and required ConfigMap existence.

    Inventory establishes which dynamic Pod names belong to the exact logical
    service roots. Event collection is then restricted to those admitted names,
    so a caller cannot broaden an Incident by supplying an arbitrary prefix.
    Required ConfigMap names are discovered only from those Pod specs and exact
    GET results never include ConfigMap values.
    """

    def __init__(
        self,
        inventory: KubernetesInventoryProvider,
        service_events: KubernetesStateProvider,
        pod_events: KubernetesStateProvider,
        required_configmaps: Optional[KubernetesStateProvider] = None,
        *,
        max_required_configmaps: int = 16,
    ) -> None:
        if not 1 <= max_required_configmaps <= 100:
            raise ValueError("max_required_configmaps must be between 1 and 100")
        self._inventory = inventory
        self._service_events = service_events
        self._pod_events = pod_events
        self._required_configmaps = required_configmaps
        self._max_required_configmaps = max_required_configmaps

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        deadline = time.monotonic() + request.timeout_seconds
        inventory_batch = self._inventory.collect(request)
        items = list(inventory_batch.items)
        partial_reasons = []
        if inventory_batch.status == "PARTIAL" and inventory_batch.error:
            partial_reasons.append(inventory_batch.error)

        service_scope = ResourceScope(
            namespace=request.scope.namespace,
            resource_names=request.scope.resource_names,
            max_items=request.scope.max_items,
        )
        self._collect_events(
            items,
            partial_reasons,
            self._service_events,
            request,
            service_scope,
            deadline,
            label="service events",
        )

        pod_names = tuple(
            sorted(
                {
                    str(item.subject.get("name"))
                    for item in inventory_batch.items
                    if item.kind == "resource-state"
                    and item.subject.get("kind") == "Pod"
                    and item.subject.get("name")
                }
            )
        )
        if pod_names:
            pod_scope = ResourceScope(
                namespace=request.scope.namespace,
                resource_names=pod_names,
                max_items=request.scope.max_items,
            )
            self._collect_events(
                items,
                partial_reasons,
                self._pod_events,
                request,
                pod_scope,
                deadline,
                label="pod events",
            )

        if self._required_configmaps is not None:
            self._collect_required_configmaps(
                items,
                partial_reasons,
                inventory_batch,
                request,
                deadline,
            )

        if len(items) > request.scope.max_items:
            items = items[: request.scope.max_items]
            partial_reasons.append(
                "Kubernetes Incident Evidence exceeded the item budget and was truncated"
            )
        if partial_reasons:
            return ProviderBatch(
                items=tuple(items),
                status="PARTIAL",
                error="; ".join(partial_reasons),
            )
        return ProviderBatch(items=tuple(items))

    def _collect_required_configmaps(
        self,
        items: list[EvidenceDraft],
        partial_reasons: list[str],
        inventory_batch: ProviderBatch,
        request: CollectionRequest,
        deadline: float,
    ) -> None:
        references: Dict[str, list[Dict[str, Any]]] = {}
        for item in inventory_batch.items:
            if item.kind != "resource-state" or item.subject.get("kind") != "Pod":
                continue
            raw_references = item.facts.get("required_configmap_references", [])
            if not isinstance(raw_references, list):
                partial_reasons.append("required ConfigMap references are malformed")
                return
            for reference in raw_references:
                if not isinstance(reference, Mapping):
                    partial_reasons.append("required ConfigMap reference is malformed")
                    return
                name = reference.get("name")
                reference_keys = reference.get("reference_keys")
                if not isinstance(name, str) or not isinstance(reference_keys, list):
                    partial_reasons.append("required ConfigMap reference is malformed")
                    return
                _validate_resource_name(name)
                references.setdefault(name, []).append(
                    {
                        "cluster_id": item.subject.get("cluster_id"),
                        "kind": "Pod",
                        "namespace": request.scope.namespace,
                        "name": item.subject.get("name"),
                        "uid": item.subject.get("uid"),
                        "reference_keys": sorted(
                            key for key in reference_keys if isinstance(key, str)
                        ),
                    }
                )
        if not references:
            return
        if len(references) > self._max_required_configmaps:
            partial_reasons.append(
                "required ConfigMap references exceeded the bounded lookup budget"
            )
            return
        scope = ResourceScope(
            namespace=request.scope.namespace,
            resource_names=tuple(sorted(references)),
            max_items=min(request.scope.max_items, len(references)),
        )
        try:
            derived_request = self._derived_request(request, scope, deadline)
            assert self._required_configmaps is not None
            batch = self._required_configmaps.collect(derived_request)
        except ProviderError as error:
            partial_reasons.append(f"required ConfigMaps: {error}")
            return
        for item in batch.items:
            name = item.subject.get("name")
            sources = references.get(str(name), [])
            if not sources:
                partial_reasons.append(
                    "required ConfigMap lookup returned an unrequested subject"
                )
                continue
            facts = dict(item.facts)
            facts["scope_derivation"] = {
                "relation_type": "REFERENCES",
                "destination_kind": "ConfigMap",
                "destination_namespace": request.scope.namespace,
                "destination_name": name,
                "sources": sources,
            }
            items.append(replace(item, facts=facts))
        if batch.status == "PARTIAL" and batch.error:
            partial_reasons.append(f"required ConfigMaps: {batch.error}")

    @staticmethod
    def _derived_request(
        request: CollectionRequest,
        scope: ResourceScope,
        deadline: float,
    ) -> CollectionRequest:
        return CollectionRequest(
            request_id=request.request_id,
            incident_id=request.incident_id,
            window=request.window,
            scope=scope,
            timeout_seconds=KubernetesStateProvider._remaining(deadline),
            attempt=request.attempt,
        )

    @staticmethod
    def _append_events(
        items: list[EvidenceDraft],
        partial_reasons: list[str],
        provider: KubernetesStateProvider,
        request: CollectionRequest,
        *,
        label: str,
    ) -> None:
        try:
            batch = provider.collect(request)
        except ProviderError as error:
            partial_reasons.append(f"{label}: {error}")
            return
        items.extend(item for item in batch.items if item.kind == "kubernetes-event")
        if batch.status == "PARTIAL" and batch.error:
            partial_reasons.append(f"{label}: {batch.error}")

    @classmethod
    def _collect_events(
        cls,
        items: list[EvidenceDraft],
        partial_reasons: list[str],
        provider: KubernetesStateProvider,
        request: CollectionRequest,
        scope: ResourceScope,
        deadline: float,
        *,
        label: str,
    ) -> None:
        try:
            derived_request = cls._derived_request(request, scope, deadline)
        except ProviderError as error:
            partial_reasons.append(f"{label}: {error}")
            return
        cls._append_events(
            items,
            partial_reasons,
            provider,
            derived_request,
            label=label,
        )
