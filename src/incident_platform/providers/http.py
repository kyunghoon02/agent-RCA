"""Bounded JSON-over-HTTP transport shared by read-only providers."""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from ..errors import PermanentProviderError, RetryableProviderError


class ProviderNotFound(PermanentProviderError):
    """A scoped provider resource does not exist."""


class ProviderPageExpired(RetryableProviderError):
    """A paginated provider snapshot expired and must restart."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class BoundedJSONTransport:
    """GET JSON with response limits and stable provider error classes."""

    max_response_bytes: int = 2 * 1024 * 1024
    ssl_context: Optional[ssl.SSLContext] = None
    opener: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")

    def get_json(
        self,
        url: str,
        *,
        timeout_seconds: float,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        if timeout_seconds <= 0:
            raise RetryableProviderError("provider request deadline was exhausted")
        request_headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "incident-platform/1.0",
        }
        request_headers.update(dict(headers or {}))
        request = Request(url, headers=request_headers, method="GET")
        opener = self.opener
        if opener is None:
            handlers = [_RejectRedirects()]
            if self.ssl_context is not None:
                handlers.append(HTTPSHandler(context=self.ssl_context))
            opener = build_opener(*handlers)
        try:
            with opener.open(
                request,
                timeout=timeout_seconds,
            ) as response:
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        length = int(declared_length)
                    except ValueError as error:
                        raise PermanentProviderError(
                            "provider returned an invalid Content-Length"
                        ) from error
                    if length > self.max_response_bytes:
                        raise PermanentProviderError(
                            "provider response exceeds configured byte limit"
                        )
                body = response.read(self.max_response_bytes + 1)
                if len(body) > self.max_response_bytes:
                    raise PermanentProviderError(
                        "provider response exceeds configured byte limit"
                    )
        except HTTPError as error:
            if error.code == 404:
                raise ProviderNotFound("scoped provider resource was not found") from error
            if error.code == 410:
                raise ProviderPageExpired("provider pagination snapshot expired") from error
            if error.code == 429 or error.code >= 500:
                raise RetryableProviderError(
                    f"provider HTTP request failed with status {error.code}"
                ) from error
            raise PermanentProviderError(
                f"provider HTTP request failed with status {error.code}"
            ) from error
        except (TimeoutError, URLError) as error:
            raise RetryableProviderError("provider HTTP request failed") from error

        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PermanentProviderError(
                "provider returned malformed JSON"
            ) from error
        if not isinstance(decoded, Mapping):
            raise PermanentProviderError("provider JSON response must be an object")
        return dict(decoded)
