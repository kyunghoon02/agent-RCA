from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from incident_platform.http_receiver import (
    AlertmanagerHTTPReceiver,
    AlertmanagerWebhookWSGI,
    ReceiverConfig,
)
from incident_platform.incidents import AlertmanagerIngestionService
from incident_platform.repository import InMemoryIncidentRepository


TOKEN = "fixture-token-with-at-least-16-characters"
RECEIVED_AT = datetime(2026, 8, 18, 3, 0, tzinfo=timezone.utc)


def alertmanager_payload() -> dict:
    return {
        "receiver": "incident-platform",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "CheckoutHighErrorRate",
                    "namespace": "online-boutique",
                    "service": "checkoutservice",
                    "severity": "critical",
                },
                "annotations": {"summary": "checkout error rate is high"},
                "startsAt": "2026-08-18T02:55:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": "fixture-http-fingerprint",
            }
        ],
    }


class FailingService:
    def ingest(self, payload, *, received_at=None):
        raise RuntimeError("password=must-not-leak")


class AlertmanagerHTTPReceiverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryIncidentRepository()
        service = AlertmanagerIngestionService(self.repository)
        self.receiver = AlertmanagerHTTPReceiver(
            service,
            ReceiverConfig(
                bearer_token=TOKEN,
                max_body_bytes=4096,
                max_alerts_per_request=2,
            ),
        )

    def request(
        self,
        *,
        method: str = "POST",
        path: str = "/v1/alertmanager/webhook",
        body: Optional[bytes] = None,
        token: Optional[str] = TOKEN,
        content_type: str = "application/json; charset=utf-8",
    ):
        headers = {
            "Content-Type": content_type,
            "X-Request-ID": "req-fixture-http",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.receiver.handle(
            method=method,
            path=path,
            headers=headers,
            body=(
                body
                if body is not None
                else json.dumps(alertmanager_payload()).encode()
            ),
            received_at=RECEIVED_AT,
        )

    def test_valid_webhook_returns_bounded_incident_summary(self) -> None:
        response = self.request()
        payload = json.loads(response.body)

        self.assertEqual(response.status.value, 202)
        self.assertEqual(payload["accepted"], 1)
        self.assertEqual(payload["created"], 1)
        self.assertEqual(payload["request_id"], "req-fixture-http")
        self.assertEqual(payload["incidents"][0]["incident_status"], "RECEIVED")
        self.assertNotIn("alert", payload["incidents"][0])

    def test_duplicate_delivery_is_idempotent(self) -> None:
        first = json.loads(self.request().body)
        second = json.loads(self.request().body)

        self.assertEqual(
            first["incidents"][0]["incident_id"],
            second["incidents"][0]["incident_id"],
        )
        self.assertEqual(second["created"], 0)
        self.assertEqual(self.repository.count(), 1)

    def test_missing_or_wrong_bearer_token_is_rejected(self) -> None:
        for token in (None, "wrong-token-with-at-least-16-characters"):
            with self.subTest(token=token):
                response = self.request(token=token)
                self.assertEqual(response.status.value, 401)
                self.assertEqual(self.repository.count(), 0)
                self.assertIn(("WWW-Authenticate", "Bearer"), response.headers)

    def test_non_json_content_type_is_rejected(self) -> None:
        response = self.request(content_type="text/plain")
        self.assertEqual(response.status.value, 415)
        self.assertEqual(self.repository.count(), 0)

    def test_invalid_json_is_rejected_without_persistence(self) -> None:
        response = self.request(body=b"{not-json")
        self.assertEqual(response.status.value, 400)
        self.assertEqual(json.loads(response.body)["error"]["code"], "INVALID_JSON")
        self.assertEqual(self.repository.count(), 0)

    def test_invalid_alert_in_batch_is_rejected_before_persistence(self) -> None:
        payload = alertmanager_payload()
        invalid = copy.deepcopy(payload["alerts"][0])
        del invalid["labels"]["namespace"]
        payload["alerts"].append(invalid)

        response = self.request(body=json.dumps(payload).encode())
        self.assertEqual(response.status.value, 400)
        self.assertEqual(self.repository.count(), 0)

    def test_body_and_alert_count_limits_are_enforced(self) -> None:
        oversized = self.request(body=b"x" * 4097)
        self.assertEqual(oversized.status.value, 413)

        payload = alertmanager_payload()
        payload["alerts"] = payload["alerts"] * 3
        too_many = self.request(body=json.dumps(payload).encode())
        self.assertEqual(too_many.status.value, 413)
        self.assertEqual(
            json.loads(too_many.body)["error"]["code"],
            "TOO_MANY_ALERTS",
        )

    def test_health_route_does_not_require_authentication(self) -> None:
        response = self.request(method="GET", path="/healthz", body=b"", token=None)
        self.assertEqual(response.status.value, 200)
        self.assertEqual(json.loads(response.body)["status"], "ok")

    def test_internal_error_does_not_expose_exception_or_request_body(self) -> None:
        receiver = AlertmanagerHTTPReceiver(
            FailingService(),
            ReceiverConfig(bearer_token=TOKEN),
        )
        response = receiver.handle(
            method="POST",
            path="/v1/alertmanager/webhook",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
            body=b'{"password":"also-must-not-leak"}',
            received_at=RECEIVED_AT,
        )
        serialized = response.body.decode()
        self.assertEqual(response.status.value, 500)
        self.assertNotIn("must-not-leak", serialized)


class AlertmanagerWebhookWSGITests(unittest.TestCase):
    def setUp(self) -> None:
        repository = InMemoryIncidentRepository()
        receiver = AlertmanagerHTTPReceiver(
            AlertmanagerIngestionService(repository),
            ReceiverConfig(bearer_token=TOKEN, max_body_bytes=4096),
        )
        self.application = AlertmanagerWebhookWSGI(receiver)

    def invoke(self, *, body: bytes, content_length: Optional[str]):
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/alertmanager/webhook",
            "CONTENT_TYPE": "application/json",
            "HTTP_AUTHORIZATION": f"Bearer {TOKEN}",
            "HTTP_X_REQUEST_ID": "req-wsgi-fixture",
            "wsgi.input": BytesIO(body),
        }
        if content_length is not None:
            environ["CONTENT_LENGTH"] = content_length
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        response_body = b"".join(self.application(environ, start_response))
        return captured, json.loads(response_body)

    def test_wsgi_adapter_ingests_a_valid_request(self) -> None:
        body = json.dumps(alertmanager_payload()).encode()
        response, payload = self.invoke(body=body, content_length=str(len(body)))
        self.assertEqual(response["status"], "202 Accepted")
        self.assertEqual(payload["accepted"], 1)

    def test_wsgi_adapter_requires_content_length(self) -> None:
        response, payload = self.invoke(body=b"{}", content_length=None)
        self.assertEqual(response["status"], "411 Length Required")
        self.assertEqual(payload["error"]["code"], "LENGTH_REQUIRED")

    def test_wsgi_adapter_rejects_incomplete_body(self) -> None:
        response, payload = self.invoke(body=b"{}", content_length="10")
        self.assertEqual(response["status"], "400 Bad Request")
        self.assertEqual(payload["error"]["code"], "INCOMPLETE_BODY")
