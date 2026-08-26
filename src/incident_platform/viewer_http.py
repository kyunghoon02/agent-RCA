"""Bounded, authenticated, read-only HTTP transport for the RCA Viewer."""

from __future__ import annotations

import hmac
import json
import re
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, unquote

from .errors import ContractViolation
from .http_receiver import HTTPResponse
from .viewer import IncidentViewerQueryService


INCIDENTS_PATH = "/api/v1/incidents"
_DETAIL_PATH = re.compile(
    r"^/api/v1/incidents/(?P<incident_id>inc-[a-z0-9][a-z0-9-]{7,63})"
    r"(?P<work>/work)?$"
)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LIST_PARAMETERS = frozenset(
    {"status", "severity", "namespace", "search", "limit", "cursor"}
)


@dataclass(frozen=True)
class ViewerHTTPConfig:
    bearer_token: str
    default_page_size: int = 50
    max_query_string_bytes: int = 4096
    max_response_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if len(self.bearer_token) < 16:
            raise ValueError("Viewer bearer token must contain at least 16 characters")
        if not 1 <= self.default_page_size <= 100:
            raise ValueError("Viewer default page size must be between 1 and 100")
        if not 256 <= self.max_query_string_bytes <= 16 * 1024:
            raise ValueError("Viewer query-string limit is outside the allowed range")
        if not 1024 <= self.max_response_bytes <= 32 * 1024 * 1024:
            raise ValueError("Viewer response limit is outside the allowed range")


def _request_id(candidate: Optional[str]) -> str:
    if candidate and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return f"req-{uuid.uuid4().hex}"


def _encoded_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_response(
    status: HTTPStatus,
    payload: Mapping[str, Any],
    *,
    request_id: str,
    extra_headers: Iterable[Tuple[str, str]] = (),
) -> HTTPResponse:
    body = _encoded_json(payload)
    return HTTPResponse(
        status=status,
        body=body,
        headers=(
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Request-ID", request_id),
            *tuple(extra_headers),
        ),
    )


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


class IncidentViewerHTTPAPI:
    """Expose only bounded Viewer service reads over authenticated HTTP GET."""

    def __init__(
        self,
        service: IncidentViewerQueryService,
        config: ViewerHTTPConfig,
    ) -> None:
        self._service = service
        self.config = config

    def handle(
        self,
        *,
        method: str,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> HTTPResponse:
        normalized_headers = {
            str(key).lower(): str(value) for key, value in headers.items()
        }
        request_id = _request_id(normalized_headers.get("x-request-id"))
        method = method.upper()

        if path == "/healthz":
            if method != "GET":
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

        route = _DETAIL_PATH.fullmatch(unquote(path))
        if path != INCIDENTS_PATH and route is None:
            return _error_response(
                HTTPStatus.NOT_FOUND,
                code="NOT_FOUND",
                message="route not found",
                request_id=request_id,
            )
        if method != "GET":
            return _error_response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                code="METHOD_NOT_ALLOWED",
                message="Viewer API only accepts GET",
                request_id=request_id,
                extra_headers=(("Allow", "GET"),),
            )
        unauthorized = self._authorize(
            normalized_headers.get("authorization"), request_id
        )
        if unauthorized is not None:
            return unauthorized

        try:
            if len(query_string.encode("utf-8")) > self.config.max_query_string_bytes:
                raise ValueError("query string exceeds the configured limit")
            if path == INCIDENTS_PATH:
                payload = self._service.list_incidents(
                    self._parse_list_query(query_string)
                )
            else:
                assert route is not None
                if query_string:
                    raise ValueError("detail routes do not accept query parameters")
                incident_id = route.group("incident_id")
                if route.group("work"):
                    payload = self._service.get_incident_work_state(incident_id)
                else:
                    payload = self._service.get_incident_detail(incident_id)
        except KeyError:
            return _error_response(
                HTTPStatus.NOT_FOUND,
                code="INCIDENT_NOT_FOUND",
                message="Incident was not found",
                request_id=request_id,
            )
        except (ContractViolation, ValueError) as error:
            return _error_response(
                HTTPStatus.BAD_REQUEST,
                code="INVALID_QUERY",
                message=str(error),
                request_id=request_id,
            )
        except Exception:
            return _error_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                code="INTERNAL_ERROR",
                message="Viewer query failed",
                request_id=request_id,
            )

        encoded = _encoded_json(payload)
        if len(encoded) > self.config.max_response_bytes:
            return _error_response(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                code="RESPONSE_TOO_LARGE",
                message="Viewer response exceeds the configured limit",
                request_id=request_id,
            )
        return HTTPResponse(
            status=HTTPStatus.OK,
            body=encoded,
            headers=(
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(encoded))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Request-ID", request_id),
            ),
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

    def _parse_list_query(self, query_string: str) -> Dict[str, Any]:
        try:
            pairs = parse_qsl(
                query_string,
                keep_blank_values=True,
                max_num_fields=32,
            )
        except ValueError as error:
            raise ValueError("query string contains too many parameters") from error
        unknown = sorted({name for name, _ in pairs} - _LIST_PARAMETERS)
        if unknown:
            raise ValueError(f"unsupported query parameter: {unknown[0]}")

        values: Dict[str, List[str]] = {}
        for name, value in pairs:
            values.setdefault(name, []).append(value)

        def repeated(name: str) -> List[str]:
            items = values.get(name, [])
            if any(not item.strip() for item in items):
                raise ValueError(f"{name} must not be blank")
            return [item.strip() for item in items]

        def single(name: str) -> Optional[str]:
            items = values.get(name, [])
            if len(items) > 1:
                raise ValueError(f"{name} must not be repeated")
            if not items:
                return None
            value = items[0].strip()
            if not value:
                raise ValueError(f"{name} must not be blank")
            return value

        limit_text = single("limit")
        try:
            limit = (
                self.config.default_page_size
                if limit_text is None
                else int(limit_text)
            )
        except ValueError as error:
            raise ValueError("limit must be an integer") from error
        return {
            "schema_version": "1.0.0",
            "statuses": repeated("status"),
            "severities": repeated("severity"),
            "namespace": single("namespace"),
            "search": single("search"),
            "limit": limit,
            "cursor": single("cursor"),
        }


class IncidentViewerWSGI:
    def __init__(self, api: IncidentViewerHTTPAPI) -> None:
        self._api = api

    def __call__(
        self,
        environ: Mapping[str, Any],
        start_response: Any,
    ) -> List[bytes]:
        response = self._api.handle(
            method=str(environ.get("REQUEST_METHOD", "GET")),
            path=str(environ.get("PATH_INFO", "")),
            query_string=str(environ.get("QUERY_STRING", "")),
            headers={
                "authorization": str(environ.get("HTTP_AUTHORIZATION", "")),
                "x-request-id": str(environ.get("HTTP_X_REQUEST_ID", "")),
            },
        )
        start_response(
            f"{response.status.value} {response.status.phrase}",
            list(response.headers),
        )
        return [response.body]
