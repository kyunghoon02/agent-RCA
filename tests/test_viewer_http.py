from __future__ import annotations

import json
import unittest
from http import HTTPStatus

from incident_platform.viewer_http import (
    IncidentViewerHTTPAPI,
    IncidentViewerWSGI,
    ViewerHTTPConfig,
)


TOKEN = "viewer-test-token-123456"


class StaticViewerService:
    def __init__(self) -> None:
        self.last_query = None
        self.fail = False

    def list_incidents(self, query):
        if self.fail:
            raise RuntimeError("sensitive database failure")
        self.last_query = query
        return {"schema_version": "1.0.0", "items": [], "next_cursor": None}

    def get_incident_detail(self, incident_id: str):
        if incident_id == "inc-notfound01":
            raise KeyError(incident_id)
        return {
            "schema_version": "1.0.0",
            "incident": {"incident_id": incident_id},
            "evidence": [],
        }

    def get_incident_work_state(self, incident_id: str):
        return {
            "schema_version": "1.0.0",
            "incident_id": incident_id,
            "collection": None,
            "localization": None,
            "analysis": None,
        }


def decode(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


class IncidentViewerHTTPAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = StaticViewerService()
        self.api = IncidentViewerHTTPAPI(
            self.service,
            ViewerHTTPConfig(bearer_token=TOKEN),
        )

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        query_string: str = "",
        authenticated: bool = True,
    ):
        headers = {"x-request-id": "req-viewer-test"}
        if authenticated:
            headers["authorization"] = f"Bearer {TOKEN}"
        return self.api.handle(
            method=method,
            path=path,
            query_string=query_string,
            headers=headers,
        )

    def test_health_is_public_but_viewer_routes_require_authentication(self) -> None:
        health = self.request("/healthz", authenticated=False)
        denied = self.request("/api/v1/incidents", authenticated=False)

        self.assertEqual(health.status, HTTPStatus.OK)
        self.assertEqual(decode(health)["status"], "ok")
        self.assertEqual(denied.status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(decode(denied)["error"]["code"], "UNAUTHORIZED")

    def test_list_query_is_bounded_and_translated_to_the_service_contract(self) -> None:
        response = self.request(
            "/api/v1/incidents",
            query_string=(
                "status=ANALYZING&status=FAILED&severity=critical&"
                "namespace=online-boutique&search=frontend&limit=25"
            ),
        )

        self.assertEqual(response.status, HTTPStatus.OK)
        self.assertEqual(
            self.service.last_query,
            {
                "schema_version": "1.0.0",
                "statuses": ["ANALYZING", "FAILED"],
                "severities": ["critical"],
                "namespace": "online-boutique",
                "search": "frontend",
                "limit": 25,
                "cursor": None,
            },
        )
        self.assertIn(("Cache-Control", "no-store"), response.headers)
        self.assertIn(("X-Content-Type-Options", "nosniff"), response.headers)

    def test_detail_and_work_routes_are_separate_reads(self) -> None:
        detail = self.request("/api/v1/incidents/inc-viewerhttp01")
        work = self.request("/api/v1/incidents/inc-viewerhttp01/work")

        self.assertEqual(detail.status, HTTPStatus.OK)
        self.assertEqual(
            decode(detail)["incident"]["incident_id"], "inc-viewerhttp01"
        )
        self.assertEqual(work.status, HTTPStatus.OK)
        self.assertEqual(decode(work)["incident_id"], "inc-viewerhttp01")

    def test_invalid_queries_methods_and_missing_incidents_are_bounded(self) -> None:
        unknown = self.request(
            "/api/v1/incidents", query_string="unexpected=true"
        )
        duplicate = self.request(
            "/api/v1/incidents", query_string="limit=10&limit=20"
        )
        mutation = self.request("/api/v1/incidents", method="POST")
        missing = self.request("/api/v1/incidents/inc-notfound01")

        self.assertEqual(unknown.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(duplicate.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(mutation.status, HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(missing.status, HTTPStatus.NOT_FOUND)

    def test_internal_errors_and_oversized_responses_do_not_leak_content(self) -> None:
        self.service.fail = True
        failed = self.request("/api/v1/incidents")
        self.assertEqual(failed.status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertNotIn("sensitive", failed.body.decode("utf-8"))

        class LargeService(StaticViewerService):
            def get_incident_detail(self, incident_id: str):
                return {"value": "x" * 2048}

        bounded = IncidentViewerHTTPAPI(
            LargeService(),
            ViewerHTTPConfig(bearer_token=TOKEN, max_response_bytes=1024),
        ).handle(
            method="GET",
            path="/api/v1/incidents/inc-viewerlarge01",
            query_string="",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(bounded.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(decode(bounded)["error"]["code"], "RESPONSE_TOO_LARGE")

    def test_wsgi_adapter_returns_the_exact_api_response(self) -> None:
        application = IncidentViewerWSGI(self.api)
        captured = {}

        body = application(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/api/v1/incidents",
                "QUERY_STRING": "limit=5",
                "HTTP_AUTHORIZATION": f"Bearer {TOKEN}",
                "HTTP_X_REQUEST_ID": "req-wsgi-viewer",
            },
            lambda status, headers: captured.update(
                {"status": status, "headers": headers}
            ),
        )

        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(json.loads(body[0])["items"], [])
        self.assertEqual(self.service.last_query["limit"], 5)


if __name__ == "__main__":
    unittest.main()
