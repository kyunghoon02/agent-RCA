#!/usr/bin/env python3
"""Expose the bounded read-only Incident Viewer API over PostgreSQL."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Callable

from incident_platform.postgresql import PostgreSQLIncidentRepository
from incident_platform.viewer import IncidentViewerQueryService
from incident_platform.viewer_http import (
    IncidentViewerHTTPAPI,
    IncidentViewerWSGI,
    ViewerHTTPConfig,
)


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _required_secret_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True)
class ViewerRuntimeConfig:
    bearer_token: str
    max_response_bytes: int
    postgres_host: str
    postgres_port: int
    postgres_database: str
    postgres_username: str
    postgres_password: str

    def __post_init__(self) -> None:
        ViewerHTTPConfig(
            bearer_token=self.bearer_token,
            max_response_bytes=self.max_response_bytes,
        )
        if not 1 <= self.postgres_port <= 65535:
            raise ValueError("PostgreSQL port is invalid")

    @classmethod
    def from_environment(cls) -> "ViewerRuntimeConfig":
        return cls(
            bearer_token=_required_secret_environment("VIEWER_BEARER_TOKEN"),
            max_response_bytes=int(
                os.environ.get("VIEWER_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024))
            ),
            postgres_host=_required_environment("POSTGRES_HOST"),
            postgres_port=int(os.environ.get("POSTGRES_PORT", "5432")),
            postgres_database=_required_environment("POSTGRES_DATABASE"),
            postgres_username=_required_environment("POSTGRES_USERNAME"),
            postgres_password=_required_secret_environment("POSTGRES_PASSWORD"),
        )


def _postgres_connection_factory(
    config: ViewerRuntimeConfig,
) -> Callable[[], object]:
    def connect() -> object:
        import psycopg

        return psycopg.connect(
            host=config.postgres_host,
            port=config.postgres_port,
            dbname=config.postgres_database,
            user=config.postgres_username,
            password=config.postgres_password,
            connect_timeout=5,
            application_name="incident-viewer",
        )

    return connect


def build_application(config: ViewerRuntimeConfig) -> IncidentViewerWSGI:
    repository = PostgreSQLIncidentRepository(
        _postgres_connection_factory(config)
    )
    service = IncidentViewerQueryService(repository)
    api = IncidentViewerHTTPAPI(
        service,
        ViewerHTTPConfig(
            bearer_token=config.bearer_token,
            max_response_bytes=config.max_response_bytes,
        ),
    )
    return IncidentViewerWSGI(api)


class LazyIncidentViewerApplication:
    def __init__(
        self,
        config_factory: Callable[[], ViewerRuntimeConfig] = (
            ViewerRuntimeConfig.from_environment
        ),
        application_factory: Callable[
            [ViewerRuntimeConfig], IncidentViewerWSGI
        ] = build_application,
    ) -> None:
        self._config_factory = config_factory
        self._application_factory = application_factory
        self._application: Any = None
        self._lock = threading.Lock()

    def __call__(self, environ: Any, start_response: Any) -> Any:
        if self._application is None:
            with self._lock:
                if self._application is None:
                    self._application = self._application_factory(
                        self._config_factory()
                    )
        return self._application(environ, start_response)


application = LazyIncidentViewerApplication()
