#!/usr/bin/env python3
"""Generate bounded synthetic Online Boutique traffic through a loopback tunnel."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Callable, Sequence


PRODUCT_IDS = (
    "0PUK6V6EV0",
    "1YMWWN1N4O",
    "2ZYFJ3GM2N",
    "66VCHSJNUP",
    "6E92ZMYYFZ",
    "9SIQT8TOJO",
    "L9ECAV7KIM",
    "LS4PSXUNUM",
    "OLJCESPC7Z",
)
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
MIN_DURATION_SECONDS = 10
MAX_DURATION_SECONDS = 1200
MAX_REQUESTS_PER_SECOND = 50.0
WORKLOAD_PROFILES = ("normal", "path-weighted", "krca-route-coverage")
COVERAGE_ACTIONS = (
    "home",
    "product",
    "add-cart",
    "view-cart",
    "empty-cart",
    "checkout",
)


def require_loopback_base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "base URL must be an unauthenticated loopback HTTP endpoint"
        )
    if parsed.port is None:
        raise ValueError("base URL must include an explicit loopback port")
    return value.rstrip("/") + "/"


def deterministic_action_names(seed: int, count: int) -> list[str]:
    if count < 0:
        raise ValueError("count must not be negative")
    generator = random.Random(seed)
    return generator.choices(
        ("home", "product", "add-cart", "checkout"),
        weights=(1, 2, 3, 8),
        k=count,
    )


def deterministic_coverage_action_names(count: int) -> list[str]:
    """Return a fixed cycle that exercises every KRCA-owned frontend route."""

    if count < 0:
        raise ValueError("count must not be negative")
    return [COVERAGE_ACTIONS[index % len(COVERAGE_ACTIONS)] for index in range(count)]


def _request(
    opener: urllib.request.OpenerDirector,
    *,
    url: str,
    marker: str,
    timeout_seconds: float,
    form: dict[str, str] | None = None,
) -> int:
    body = None
    headers = {
        "User-Agent": "agent-rca-controlled-workload/1.0",
        "X-Agent-RCA-Synthetic": marker,
    }
    if form is not None:
        body = urllib.parse.urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            response.read(65_536)
            return int(response.status)
    except urllib.error.HTTPError as error:
        error.read(65_536)
        return int(error.code)


def _checkout_form() -> dict[str, str]:
    return {
        "email": "synthetic@example.invalid",
        "street_address": "1600 Amphitheatre Parkway",
        "zip_code": "94043",
        "city": "Mountain View",
        "state": "CA",
        "country": "United States",
        "credit_card_number": "4111111111111111",
        "credit_card_expiration_month": "12",
        "credit_card_expiration_year": "2030",
        "credit_card_cvv": "123",
    }


def run_workload(
    *,
    base_url: str,
    duration_seconds: int,
    requests_per_second: float,
    seed: int,
    marker: str,
    timeout_seconds: float,
    stop_file: Path | None,
    profile: str = "path-weighted",
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    base = require_loopback_base_url(base_url)
    if not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS:
        raise ValueError(
            f"duration must be between {MIN_DURATION_SECONDS} and "
            f"{MAX_DURATION_SECONDS} seconds"
        )
    if not 0 < requests_per_second <= MAX_REQUESTS_PER_SECOND:
        raise ValueError(
            f"requests per second must be above 0 and at most "
            f"{MAX_REQUESTS_PER_SECOND}"
        )
    if not 0 < timeout_seconds <= 30:
        raise ValueError("timeout must be above 0 and at most 30 seconds")
    if not marker or len(marker) > 64:
        raise ValueError("synthetic marker must contain 1 to 64 characters")
    if profile not in WORKLOAD_PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(WORKLOAD_PROFILES)}")

    generator = random.Random(seed)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    deadline = monotonic() + duration_seconds
    interval = 1.0 / requests_per_second
    next_start = monotonic()
    statuses: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    transport_error_types: Counter[str] = Counter()
    transport_errors = 0
    request_attempts = 0

    def call(path: str, form: dict[str, str] | None = None) -> None:
        nonlocal request_attempts, transport_errors
        request_attempts += 1
        try:
            status = _request(
                opener,
                url=urllib.parse.urljoin(base, path.lstrip("/")),
                marker=marker,
                timeout_seconds=timeout_seconds,
                form=form,
            )
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            transport_errors += 1
            reason = getattr(error, "reason", error)
            error_type = type(reason).__name__
            transport_error_types[error_type] += 1
        else:
            statuses[f"{status // 100}xx"] += 1

    while monotonic() < deadline:
        if stop_file is not None and stop_file.exists():
            break
        if profile == "krca-route-coverage":
            action = COVERAGE_ACTIONS[sum(actions.values()) % len(COVERAGE_ACTIONS)]
        elif profile == "normal":
            action = generator.choices(
                ("home", "product", "add-cart", "checkout"),
                weights=(6, 5, 2, 1),
                k=1,
            )[0]
        else:
            action = generator.choices(
                ("home", "product", "add-cart", "checkout"),
                weights=(1, 2, 3, 8),
                k=1,
            )[0]
        product_id = generator.choice(PRODUCT_IDS)
        actions[action] += 1
        if action == "home":
            call("/")
        elif action == "product":
            call(f"/product/{product_id}")
        elif action == "add-cart":
            call("/cart", {"product_id": product_id, "quantity": "1"})
        elif action == "view-cart":
            call("/cart")
        elif action == "empty-cart":
            call("/cart/empty", {})
        else:
            call("/cart", {"product_id": product_id, "quantity": "1"})
            call("/cart/checkout", _checkout_form())

        next_start += interval
        remaining = next_start - monotonic()
        if remaining > 0:
            sleep(remaining)

    return {
        "schema_version": "1.1.0",
        "profile": profile,
        "seed": seed,
        "synthetic_marker": marker,
        "operations": sum(actions.values()),
        "request_attempts": request_attempts,
        "status_families": dict(sorted(statuses.items())),
        "transport_errors": transport_errors,
        "transport_error_types": dict(sorted(transport_error_types.items())),
        "actions": dict(sorted(actions.items())),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run bounded Online Boutique traffic through a loopback tunnel."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--requests-per-second", type=float, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument(
        "--profile",
        choices=WORKLOAD_PROFILES,
        default="path-weighted",
    )
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--stop-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = run_workload(
            base_url=arguments.base_url,
            duration_seconds=arguments.duration_seconds,
            requests_per_second=arguments.requests_per_second,
            seed=arguments.seed,
            marker=arguments.marker,
            timeout_seconds=arguments.timeout_seconds,
            stop_file=arguments.stop_file,
            profile=arguments.profile,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
