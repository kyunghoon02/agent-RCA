#!/usr/bin/env python3
"""Expose the bounded Alertmanager receiver with durable PostgreSQL storage."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Callable

from incident_platform.http_receiver import (
    AlertmanagerHTTPReceiver,
    AlertmanagerWebhookWSGI,
    ReceiverConfig,
)
from incident_platform.incidents import AlertmanagerIngestionService
from incident_platform.postgresql import (
    PostgreSQLIncidentRepository,
    apply_migrations,
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
class ReceiverRuntimeConfig:
    bearer_token: str
    max_body_bytes: int
    max_alerts_per_request: int
    postgres_host: str
    postgres_port: int
    postgres_database: str
    postgres_username: str
    postgres_password: str

    def __post_init__(self) -> None:
        if len(self.bearer_token) < 16:
            raise ValueError("receiver bearer token must contain at least 16 characters")
        if not 1 <= self.postgres_port <= 65535:
            raise ValueError("PostgreSQL port is invalid")
        if not 1024 <= self.max_body_bytes <= 4 * 1024 * 1024:
            raise ValueError("receiver body limit is outside the allowed range")
        if not 1 <= self.max_alerts_per_request <= 500:
            raise ValueError("receiver alert limit is outside the allowed range")

    @classmethod
    def from_environment(cls) -> "ReceiverRuntimeConfig":
        return cls(
            bearer_token=_required_secret_environment("WEBHOOK_BEARER_TOKEN"),
            max_body_bytes=int(os.environ.get("WEBHOOK_MAX_BODY_BYTES", "1048576")),
            max_alerts_per_request=int(
                os.environ.get("WEBHOOK_MAX_ALERTS_PER_REQUEST", "100")
            ),
            postgres_host=_required_environment("POSTGRES_HOST"),
            postgres_port=int(os.environ.get("POSTGRES_PORT", "5432")),
            postgres_database=_required_environment("POSTGRES_DATABASE"),
            postgres_username=_required_environment("POSTGRES_USERNAME"),
            postgres_password=_required_secret_environment("POSTGRES_PASSWORD"),
        )


def _postgres_connection_factory(
    config: ReceiverRuntimeConfig,
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
            application_name="incident-webhook",
        )

    return connect


def build_application(config: ReceiverRuntimeConfig) -> AlertmanagerWebhookWSGI:
    connection_factory = _postgres_connection_factory(config)
    apply_migrations(connection_factory)
    repository = PostgreSQLIncidentRepository(connection_factory)
    service = AlertmanagerIngestionService(repository)
    receiver = AlertmanagerHTTPReceiver(
        service,
        ReceiverConfig(
            bearer_token=config.bearer_token,
            max_body_bytes=config.max_body_bytes,
            max_alerts_per_request=config.max_alerts_per_request,
        ),
    )
    return AlertmanagerWebhookWSGI(receiver)


class LazyIncidentReceiverApplication:
    """Initialize database-backed WSGI state once per Gunicorn worker."""

    def __init__(
        self,
        config_factory: Callable[[], ReceiverRuntimeConfig] = (
            ReceiverRuntimeConfig.from_environment
        ),
        application_factory: Callable[
            [ReceiverRuntimeConfig], AlertmanagerWebhookWSGI
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


application = LazyIncidentReceiverApplication()
