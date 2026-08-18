from __future__ import annotations

import io
import json
import unittest
from email.message import Message
from urllib.error import HTTPError, URLError

from incident_platform.errors import PermanentProviderError, RetryableProviderError
from incident_platform.providers.http import (
    BoundedJSONTransport,
    ProviderNotFound,
    ProviderPageExpired,
)


class FakeResponse:
    def __init__(self, body: bytes, content_length=None) -> None:
        self._body = body
        self.headers = Message()
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


class FakeOpener:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    def open(self, request, timeout: float):
        self.calls.append((request, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class BoundedJSONTransportTests(unittest.TestCase):
    def test_valid_object_is_returned(self) -> None:
        payload = json.dumps({"status": "success"}).encode()
        opener = FakeOpener(FakeResponse(payload, len(payload)))
        result = BoundedJSONTransport(
            max_response_bytes=100, opener=opener
        ).get_json("https://provider.example/api", timeout_seconds=1)
        self.assertEqual(result, {"status": "success"})
        self.assertEqual(opener.calls[0][0].get_method(), "GET")

    def test_declared_or_actual_oversize_response_is_rejected(self) -> None:
        transport = BoundedJSONTransport(
            max_response_bytes=10,
            opener=FakeOpener(FakeResponse(b"{}", 11)),
        )
        with self.assertRaisesRegex(PermanentProviderError, "byte limit"):
            transport.get_json("https://provider.example/api", timeout_seconds=1)

        transport = BoundedJSONTransport(
            max_response_bytes=10,
            opener=FakeOpener(FakeResponse(b"{" + (b"x" * 20))),
        )
        with self.assertRaisesRegex(PermanentProviderError, "byte limit"):
            transport.get_json("https://provider.example/api", timeout_seconds=1)

    def test_malformed_json_is_permanent(self) -> None:
        with self.assertRaisesRegex(PermanentProviderError, "malformed JSON"):
            BoundedJSONTransport(
                opener=FakeOpener(FakeResponse(b"not-json"))
            ).get_json("https://provider.example/api", timeout_seconds=1)

    def test_http_statuses_are_classified_without_response_body(self) -> None:
        cases = (
            (404, ProviderNotFound),
            (410, ProviderPageExpired),
            (302, PermanentProviderError),
            (403, PermanentProviderError),
            (429, RetryableProviderError),
            (503, RetryableProviderError),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                opener = FakeOpener(
                    HTTPError(
                        "https://provider.example/api",
                        status,
                        "provider detail must not escape",
                        Message(),
                        io.BytesIO(b"sensitive provider response"),
                    )
                )
                with self.assertRaises(expected) as raised:
                    BoundedJSONTransport(opener=opener).get_json(
                        "https://provider.example/api", timeout_seconds=1
                    )
                self.assertNotIn("sensitive", str(raised.exception))

    def test_network_error_is_retryable(self) -> None:
        with self.assertRaisesRegex(RetryableProviderError, "request failed"):
            BoundedJSONTransport(
                opener=FakeOpener(URLError("internal endpoint detail"))
            ).get_json("https://provider.example/api", timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
