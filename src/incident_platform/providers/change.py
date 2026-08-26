"""Read-only Kubernetes Deployment revision history Evidence provider."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from ..errors import PermanentProviderError, ProviderError
from ..evidence import CollectionRequest, EvidenceDraft, ProviderBatch, parse_time
from .http import ProviderPageExpired
from .kubernetes import (
    KubernetesResourcePage,
    KubernetesResourceSpec,
    KubernetesStateProvider,
)


DEPLOYMENT_SPEC = KubernetesResourceSpec("apps/v1", "Deployment")
REPLICA_SET_SPEC = KubernetesResourceSpec("apps/v1", "ReplicaSet")
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
SAFE_RESOURCE_NAMES = frozenset({"cpu", "memory", "ephemeral-storage"})


class DeploymentHistoryClient(Protocol):
    def get_resource(
        self,
        resource: KubernetesResourceSpec,
        *,
        namespace: str,
        name: str,
        timeout_seconds: float,
    ) -> Optional[Mapping[str, Any]]:
        ...

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


class DeploymentHistoryProvider:
    """Compare retained ReplicaSet pod templates for exact Deployment roots.

    Kubernetes revision history is bounded by Deployment revisionHistoryLimit;
    this provider reports that boundary explicitly and never presents missing
    retained history as a complete Git-level change history.
    """

    def __init__(
        self,
        client: DeploymentHistoryClient,
        *,
        cluster_id: str,
        page_size: int = 100,
        max_replica_sets: int = 500,
    ) -> None:
        if not cluster_id.strip():
            raise ValueError("Deployment history cluster_id must not be empty")
        if not 1 <= page_size <= 500:
            raise ValueError("Deployment history page_size must be between 1 and 500")
        if not 1 <= max_replica_sets <= 5000:
            raise ValueError(
                "Deployment history max_replica_sets must be between 1 and 5000"
            )
        self._client = client
        self._cluster_id = cluster_id
        self._page_size = page_size
        self._max_replica_sets = max_replica_sets

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        deadline = time.monotonic() + request.timeout_seconds
        replica_sets, truncated = self._list_replica_sets(
            request.scope.namespace,
            deadline,
        )
        drafts = []
        partial_reasons = []
        if truncated:
            partial_reasons.append(
                f"ReplicaSet history exceeded the {self._max_replica_sets} item limit"
            )
        for name in request.scope.resource_names:
            try:
                deployment = self._client.get_resource(
                    DEPLOYMENT_SPEC,
                    namespace=request.scope.namespace,
                    name=name,
                    timeout_seconds=self._remaining(deadline),
                )
                result, reason = self._deployment_drafts(
                    request,
                    name,
                    deployment,
                    replica_sets,
                    truncated=truncated,
                )
                drafts.extend(result)
                if reason:
                    partial_reasons.append(f"Deployment {name}: {reason}")
            except ProviderError as error:
                partial_reasons.append(f"Deployment {name}: {error}")

        if not drafts:
            if partial_reasons:
                raise PermanentProviderError("; ".join(partial_reasons))
            raise PermanentProviderError("Deployment history returned no Evidence")
        if len(drafts) > request.scope.max_items:
            drafts = drafts[: request.scope.max_items]
            partial_reasons.append(
                "Deployment change Evidence exceeded the item budget and was truncated"
            )
        if partial_reasons:
            return ProviderBatch(
                items=tuple(drafts),
                status="PARTIAL",
                error="; ".join(partial_reasons),
            )
        return ProviderBatch(items=tuple(drafts))

    def _list_replica_sets(
        self,
        namespace: str,
        deadline: float,
    ) -> Tuple[Tuple[Mapping[str, Any], ...], bool]:
        items = []
        continue_token = None
        restarted = False
        while True:
            remaining = self._max_replica_sets - len(items)
            if remaining <= 0:
                return tuple(items), bool(continue_token)
            try:
                page = self._client.list_resource_page(
                    REPLICA_SET_SPEC,
                    namespace=namespace,
                    limit=min(self._page_size, remaining),
                    continue_token=continue_token,
                    timeout_seconds=self._remaining(deadline),
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
                return tuple(items), False

    def _deployment_drafts(
        self,
        request: CollectionRequest,
        name: str,
        deployment: Optional[Mapping[str, Any]],
        replica_sets: Sequence[Mapping[str, Any]],
        *,
        truncated: bool,
    ) -> Tuple[Tuple[EvidenceDraft, ...], Optional[str]]:
        if deployment is None:
            return (
                (
                    self._absence_draft(
                        request,
                        name,
                        result_status="DEPLOYMENT_NOT_FOUND",
                        exists=False,
                        current_revision=None,
                        retained_count=0,
                        completeness=0.0,
                    ),
                ),
                "Deployment was not found",
            )
        metadata = self._metadata(deployment, expected_name=name)
        uid = metadata.get("uid")
        if not isinstance(uid, str) or not uid:
            raise PermanentProviderError("Deployment UID is required")
        revisions = []
        malformed_owned = 0
        for replica_set in replica_sets:
            if not self._owned_by(replica_set, name=name, uid=uid):
                continue
            try:
                revisions.append(self._revision(replica_set))
            except PermanentProviderError:
                malformed_owned += 1
        revisions.sort(key=lambda item: (item["revision"], item["created_at"]))
        current_revision = self._revision_number(metadata)
        if current_revision is None and revisions:
            current_revision = revisions[-1]["revision"]
        window_start = parse_time(request.window.start, "EvidenceWindow.start")
        window_end = parse_time(request.window.end, "EvidenceWindow.end")
        in_window = [
            item
            for item in revisions
            if window_start <= item["created_at"] <= window_end
        ]
        completeness = 0.6 if truncated or malformed_owned else 1.0
        reason_parts = []
        if truncated:
            reason_parts.append("ReplicaSet list was truncated")
        if malformed_owned:
            reason_parts.append(
                f"{malformed_owned} owned ReplicaSet revisions were malformed"
            )
        if current_revision is None:
            reason_parts.append("current Deployment revision was unavailable")

        drafts = []
        for current in in_window:
            previous = self._previous_revision(revisions, current["revision"])
            drafts.append(
                self._change_draft(
                    request,
                    deployment,
                    current,
                    previous,
                    retained_count=len(revisions),
                    completeness=completeness,
                )
            )
        if not drafts:
            status = (
                "HISTORY_INCOMPLETE"
                if reason_parts
                else "NO_CHANGES"
            )
            drafts.append(
                self._absence_draft(
                    request,
                    name,
                    result_status=status,
                    exists=True,
                    current_revision=current_revision,
                    retained_count=len(revisions),
                    uid=uid,
                    completeness=completeness,
                )
            )
        return tuple(drafts), "; ".join(reason_parts) or None

    def _change_draft(
        self,
        request: CollectionRequest,
        deployment: Mapping[str, Any],
        current: Mapping[str, Any],
        previous: Optional[Mapping[str, Any]],
        *,
        retained_count: int,
        completeness: float,
    ) -> EvidenceDraft:
        metadata = self._metadata(deployment)
        name = str(metadata["name"])
        before = previous["snapshot"] if previous else None
        after = current["snapshot"]
        changed_fields = self._changed_fields(before, after)
        facts: Dict[str, Any] = {
            "result_status": "CHANGE_DETECTED",
            "revision": current["revision"],
            "previous_revision": previous["revision"] if previous else None,
            "replica_set": current["name"],
            "occurred_at": self._format_datetime(current["created_at"]),
            "changed_fields": changed_fields,
            "before": before,
            "after": after,
            "retained_revision_count": retained_count,
            "history_source": "kubernetes-replicaset",
        }
        return EvidenceDraft(
            source="deployment",
            kind="deployment-change",
            observed_at=self._format_datetime(current["created_at"]),
            subject=self._subject(request, name, str(metadata["uid"]), exists=True),
            summary=(
                f"Kubernetes Deployment {name} revision {current['revision']} "
                "was created inside the Incident window."
            ),
            facts=facts,
            provider="kubernetes-deployment-history",
            query=(
                f"get apps/v1/Deployment {name}; list owned apps/v1/ReplicaSet "
                f"namespace={request.scope.namespace}"
            ),
            locator=f"k8s://{request.scope.namespace}/Deployment/{name}/revisions",
            completeness=completeness,
        )

    def _absence_draft(
        self,
        request: CollectionRequest,
        name: str,
        *,
        result_status: str,
        exists: bool,
        current_revision: Optional[int],
        retained_count: int,
        uid: Optional[str] = None,
        completeness: float,
    ) -> EvidenceDraft:
        facts = {
            "result_status": result_status,
            "current_revision": current_revision,
            "retained_revision_count": retained_count,
            "window_change_count": 0,
            "history_source": "kubernetes-replicaset",
        }
        return EvidenceDraft(
            source="deployment",
            kind="deployment-change",
            observed_at=request.window.end,
            subject=self._subject(request, name, uid, exists=exists),
            summary=(
                f"Deployment history for {name} returned {result_status} "
                "for the Incident window."
            ),
            facts=facts,
            provider="kubernetes-deployment-history",
            query=(
                f"get apps/v1/Deployment {name}; list owned apps/v1/ReplicaSet "
                f"namespace={request.scope.namespace}"
            ),
            locator=f"k8s://{request.scope.namespace}/Deployment/{name}/revisions",
            completeness=completeness,
        )

    def _subject(
        self,
        request: CollectionRequest,
        name: str,
        uid: Optional[str],
        *,
        exists: bool,
    ) -> Mapping[str, Any]:
        return {
            "cluster_id": self._cluster_id,
            "api_version": "apps/v1",
            "kind": "Deployment",
            "namespace": request.scope.namespace,
            "name": name,
            "uid": uid,
            "exists": exists,
        }

    @classmethod
    def _revision(cls, replica_set: Mapping[str, Any]) -> Mapping[str, Any]:
        metadata = cls._metadata(replica_set)
        revision = cls._revision_number(metadata)
        if revision is None:
            raise PermanentProviderError("ReplicaSet revision annotation is missing")
        created_at = metadata.get("creationTimestamp")
        if not isinstance(created_at, str) or not created_at:
            raise PermanentProviderError("ReplicaSet creationTimestamp is missing")
        return {
            "revision": revision,
            "name": metadata["name"],
            "created_at": parse_time(created_at, "ReplicaSet.creationTimestamp"),
            "snapshot": cls._safe_template_snapshot(replica_set),
        }

    @staticmethod
    def _metadata(
        resource: Mapping[str, Any],
        *,
        expected_name: Optional[str] = None,
    ) -> Mapping[str, Any]:
        metadata = resource.get("metadata")
        if not isinstance(metadata, Mapping):
            raise PermanentProviderError("Kubernetes resource metadata is malformed")
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise PermanentProviderError("Kubernetes resource name is malformed")
        if expected_name is not None and name != expected_name:
            raise PermanentProviderError("Kubernetes returned an out-of-scope Deployment")
        return metadata

    @classmethod
    def _owned_by(
        cls,
        replica_set: Mapping[str, Any],
        *,
        name: str,
        uid: str,
    ) -> bool:
        metadata = cls._metadata(replica_set)
        owners = metadata.get("ownerReferences", [])
        if not isinstance(owners, list):
            raise PermanentProviderError("ReplicaSet ownerReferences is malformed")
        return any(
            isinstance(owner, Mapping)
            and owner.get("kind") == "Deployment"
            and owner.get("name") == name
            and owner.get("uid") == uid
            for owner in owners
        )

    @staticmethod
    def _revision_number(metadata: Mapping[str, Any]) -> Optional[int]:
        annotations = metadata.get("annotations", {})
        if not isinstance(annotations, Mapping):
            raise PermanentProviderError("Kubernetes annotations are malformed")
        value = annotations.get(REVISION_ANNOTATION)
        if value is None:
            return None
        if not isinstance(value, str) or not value.isdigit() or int(value) <= 0:
            raise PermanentProviderError("Kubernetes revision annotation is malformed")
        return int(value)

    @classmethod
    def _safe_template_snapshot(cls, resource: Mapping[str, Any]) -> Mapping[str, Any]:
        spec = resource.get("spec", {})
        if not isinstance(spec, Mapping):
            raise PermanentProviderError("ReplicaSet spec is malformed")
        template = spec.get("template", {})
        template_spec = template.get("spec", {}) if isinstance(template, Mapping) else {}
        if not isinstance(template_spec, Mapping):
            raise PermanentProviderError("ReplicaSet pod template is malformed")
        containers = template_spec.get("containers", [])
        if not isinstance(containers, list):
            raise PermanentProviderError("ReplicaSet containers are malformed")
        safe_containers = []
        for container in containers:
            if not isinstance(container, Mapping):
                raise PermanentProviderError("ReplicaSet container is malformed")
            name = container.get("name")
            image = container.get("image")
            if not isinstance(name, str) or not name:
                raise PermanentProviderError("ReplicaSet container name is malformed")
            if not isinstance(image, str) or not image:
                raise PermanentProviderError("ReplicaSet container image is malformed")
            safe_containers.append(
                {
                    "name": name,
                    "image_name": cls._image_name(image),
                    "image_fingerprint": cls._fingerprint(image),
                    "resources": cls._safe_resources(container.get("resources", {})),
                }
            )
        safe_containers.sort(key=lambda item: item["name"])
        snapshot: Dict[str, Any] = {"containers": safe_containers}
        snapshot["template_fingerprint"] = cls._fingerprint(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        )
        return snapshot

    @staticmethod
    def _safe_resources(value: Any) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        result: Dict[str, Any] = {}
        for section in ("requests", "limits"):
            raw = value.get(section, {})
            if not isinstance(raw, Mapping):
                continue
            safe = {
                key: item
                for key, item in raw.items()
                if key in SAFE_RESOURCE_NAMES and isinstance(item, (str, int, float))
                and not isinstance(item, bool)
            }
            if safe:
                result[section] = dict(sorted(safe.items()))
        return result

    @staticmethod
    def _image_name(image: str) -> str:
        basename = image.rsplit("/", 1)[-1]
        basename = basename.split("@", 1)[0]
        return basename.rsplit(":", 1)[0]

    @staticmethod
    def _fingerprint(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _previous_revision(
        revisions: Sequence[Mapping[str, Any]],
        revision: int,
    ) -> Optional[Mapping[str, Any]]:
        previous = [item for item in revisions if item["revision"] < revision]
        return previous[-1] if previous else None

    @staticmethod
    def _changed_fields(
        before: Optional[Mapping[str, Any]],
        after: Mapping[str, Any],
    ) -> Sequence[str]:
        if before is None:
            return ["pod_template"]
        if before.get("template_fingerprint") == after.get("template_fingerprint"):
            # Kubernetes issued a new revision, but the changed field was outside
            # the safe image/resource allowlist retained in Evidence.
            return ["pod_template"]
        before_containers = {
            item["name"]: item for item in before.get("containers", [])
        }
        after_containers = {
            item["name"]: item for item in after.get("containers", [])
        }
        changed = []
        for name in sorted(set(before_containers) | set(after_containers)):
            old = before_containers.get(name)
            new = after_containers.get(name)
            if old is None or new is None:
                changed.append(f"containers.{name}")
                continue
            if old.get("image_fingerprint") != new.get("image_fingerprint"):
                changed.append(f"containers.{name}.image")
            if old.get("resources") != new.get("resources"):
                changed.append(f"containers.{name}.resources")
        return changed or ["pod_template"]

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _remaining(deadline: float) -> float:
        return KubernetesStateProvider._remaining(deadline)
