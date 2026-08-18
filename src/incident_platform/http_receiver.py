"""Bounded, authenticated HTTP transport for Alertmanager webhooks.

The core remains framework-neutral. ``AlertmanagerWebhookWSGI`` can be mounted
behind a production WSGI server later, while contract tests exercise the exact
HTTP boundary without requiring a cloud environment.
"""

from __future__ import annotations

import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from io import BufferedIOBase
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import ContractViolation, InvalidAlert, InvalidTransition
from .incidents import AlertmanagerIngestionService


DEFAULT_WEBHOOK_PATH = "/v1/alertmanager/webhook"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True)
class ReceiverConfig:
    """Security and resource bounds for the webhook transport."""

    bearer_token: str
    webhook_path: str = DEFAULT_WEBHOOK_PATH
    max_body_bytes: int = 1024 * 1024
    max_alerts_per_request: int = 100

    def __post_init__(self) -> None:
        if len(self.bearer_token) < 16:
            raise ValueError("bearer_token must contain at least 16 characters")
        if not self.webhook_path.startswith("/"):
            raise ValueError("webhook_path must start with /")
        if self.max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        if self.max_alerts_per_request <= 0:
            raise ValueError("max_alerts_per_request must be positive")


@dataclass(frozen=True)
class HTTPResponse:
    status: HTTPStatus
    body: bytes
    headers: Tuple[Tuple[str, str], ...]


def _request_id(candidate: Optional[str]) -> str:
    if candidate and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return f"req-{uuid.uuid4().hex}"


def _json_response(
    status: HTTPStatus,
    payload: Mapping[str, Any],
    *,
    request_id: str,
    extra_headers: Iterable[Tuple[str, str]] = (),
) -> HTTPResponse:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    headers = (
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(encoded))),
        ("Cache-Control", "no-store"),
        ("X-Request-ID", request_id),
        *tuple(extra_headers),
    )
    return HTTPResponse(status=status, body=encoded, headers=headers)


def _error_response(
    status: HTTPStatus,
    *,
    code: str,
    message: str,
    request_id: str,
    extra_headers: Iterable[Tuple[str, str]] = (),
) -> HTTPResponse:
    return _json_response(
        status,
        {
            "error": {"code": code, "message": message},
            "request_id": request_id,
        },
        request_id=request_id,
        extra_headers=extra_headers,
    )


class AlertmanagerHTTPReceiver:
    """Translate a bounded HTTP request into one ingestion service call."""

    def __init__(
        self,
        service: AlertmanagerIngestionService,
        config: ReceiverConfig,
    ) -> None:
        self._service = service
        self.config = config

    def handle(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        received_at: Optional[datetime] = None,
    ) -> HTTPResponse:
        normalized_headers = {
            str(key).lower(): str(value) for key, value in headers.items()
        }
        request_id = _request_id(normalized_headers.get("x-request-id"))

        if path == "/healthz":
            if method.upper() != "GET":
                return _error_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    code="METHOD_NOT_ALLOWED",
                    message="healthz only accepts GET",
                    request_id=request_id,
                    extra_headers=(("Allow", "GET"),),
                )
            return _json_response(
                HTTPStatus.OK,
                {"request_id": request_id, "status": "ok"},
                request_id=request_id,
            )

        if path != self.config.webhook_path:
            return _error_response(
                HTTPStatus.NOT_FOUND,
                code="NOT_FOUND",
                message="route not found",
                request_id=request_id,
            )
        if method.upper() != "POST":
            return _error_response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                code="METHOD_NOT_ALLOWED",
                message="webhook only accepts POST",
                request_id=request_id,
                extra_headers=(("Allow", "POST"),),
            )

        unauthorized = self._authorize(
            normalized_headers.get("authorization"), request_id
        )
        if unauthorized is not None:
            return unauthorized

        content_type = normalized_headers.get("content-type", "")
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type != "application/json":
            return _error_response(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                code="UNSUPPORTED_MEDIA_TYPE",
                message="Content-Type must be application/json",
                request_id=request_id,
            )
        if len(body) > self.config.max_body_bytes:
            return _error_response(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                code="BODY_TOO_LARGE",
                message="request body exceeds the configured limit",
                request_id=request_id,
            )

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error_response(
                HTTPStatus.BAD_REQUEST,
                code="INVALID_JSON",
                message="request body must be valid UTF-8 JSON",
                request_id=request_id,
            )

        if isinstance(payload, Mapping):
            alerts = payload.get("alerts")
            if isinstance(alerts, list) and len(alerts) > (
                self.config.max_alerts_per_request
            ):
                return _error_response(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    code="TOO_MANY_ALERTS",
                    message="alert count exceeds the configured limit",
                    request_id=request_id,
                )

        try:
            results = self._service.ingest(payload, received_at=received_at)
        except (InvalidAlert, ContractViolation) as error:
            return _error_response(
                HTTPStatus.BAD_REQUEST,
                code="INVALID_ALERT",
                message=str(error),
                request_id=request_id,
            )
        except InvalidTransition as error:
            return _error_response(
                HTTPStatus.CONFLICT,
                code="INCIDENT_CONFLICT",
                message=str(error),
                request_id=request_id,
            )
        except Exception:
            return _error_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                code="INTERNAL_ERROR",
                message="incident ingestion failed",
                request_id=request_id,
            )

        incidents: List[Dict[str, Any]] = [
            {
                "alert_status": result.alert_status,
                "created": result.created,
                "incident_id": result.incident["incident_id"],
                "incident_status": result.incident["status"],
            }
            for result in results
        ]
        return _json_response(
            HTTPStatus.ACCEPTED,
            {
                "accepted": len(incidents),
                "created": sum(item["created"] for item in incidents),
                "incidents": incidents,
                "request_id": request_id,
            },
            request_id=request_id,
        )

    def _authorize(
        self, authorization: Optional[str], request_id: str
    ) -> Optional[HTTPResponse]:
        if authorization:
            scheme, separator, credential = authorization.partition(" ")
            if separator and scheme.lower() == "bearer" and hmac.compare_digest(
                credential.encode("utf-8"),
                self.config.bearer_token.encode("utf-8"),
            ):
                return None
        return _error_response(
            HTTPStatus.UNAUTHORIZED,
            code="UNAUTHORIZED",
            message="a valid bearer token is required",
            request_id=request_id,
            extra_headers=(("WWW-Authenticate", "Bearer"),),
        )


class AlertmanagerWebhookWSGI:
    """Minimal WSGI adapter; production server selection remains deployment-owned."""

    def __init__(self, receiver: AlertmanagerHTTPReceiver) -> None:
        self._receiver = receiver

    def __call__(
        self,
        environ: Mapping[str, Any],
        start_response: Any,
    ) -> List[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET"))
        path = str(environ.get("PATH_INFO", ""))
        request_id = _request_id(environ.get("HTTP_X_REQUEST_ID"))
        headers = {
            "content-type": str(environ.get("CONTENT_TYPE", "")),
            "authorization": str(environ.get("HTTP_AUTHORIZATION", "")),
            "x-request-id": request_id,
        }

        body = b""
        if method.upper() == "POST":
            content_length = str(environ.get("CONTENT_LENGTH", "")).strip()
            if not content_length:
                response = _error_response(
                    HTTPStatus.LENGTH_REQUIRED,
                    code="LENGTH_REQUIRED",
                    message="Content-Length is required",
                    request_id=request_id,
                )
                return self._finish(response, start_response)
            try:
                length = int(content_length)
            except ValueError:
                response = _error_response(
                    HTTPStatus.BAD_REQUEST,
                    code="INVALID_CONTENT_LENGTH",
                    message="Content-Length must be an integer",
                    request_id=request_id,
                )
                return self._finish(response, start_response)
            if length < 0:
                response = _error_response(
                    HTTPStatus.BAD_REQUEST,
                    code="INVALID_CONTENT_LENGTH",
                    message="Content-Length must not be negative",
                    request_id=request_id,
                )
                return self._finish(response, start_response)
            if length > self._receiver.config.max_body_bytes:
                response = _error_response(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    code="BODY_TOO_LARGE",
                    message="request body exceeds the configured limit",
                    request_id=request_id,
                )
                return self._finish(response, start_response)

            input_stream = environ.get("wsgi.input")
            if not isinstance(input_stream, BufferedIOBase) and not hasattr(
                input_stream, "read"
            ):
                response = _error_response(
                    HTTPStatus.BAD_REQUEST,
                    code="INVALID_BODY_STREAM",
                    message="request body is unavailable",
                    request_id=request_id,
                )
                return self._finish(response, start_response)
            body = input_stream.read(length)
            if len(body) != length:
                response = _error_response(
                    HTTPStatus.BAD_REQUEST,
                    code="INCOMPLETE_BODY",
                    message="request body ended before Content-Length",
                    request_id=request_id,
                )
                return self._finish(response, start_response)

        response = self._receiver.handle(
            method=method,
            path=path,
            headers=headers,
            body=body,
        )
        return self._finish(response, start_response)

    @staticmethod
    def _finish(response: HTTPResponse, start_response: Any) -> List[bytes]:
        start_response(
            f"{response.status.value} {response.status.phrase}",
            list(response.headers),
        )
        return [response.body]
