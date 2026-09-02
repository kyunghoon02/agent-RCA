#!/usr/bin/env python3
"""Check links in Markdown files tracked by Git.

Local targets are resolved relative to the document that contains them. External
links are probed so stale references fail CI, while authentication and rate-limit
responses are treated as reachable endpoints rather than broken links.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


class Link(NamedTuple):
    path: Path
    line: int
    target: str

LINK_TARGET_PATTERN = re.compile(r"]\(\s*<?([^\s)>]+)>?")
AUTOLINK_PATTERN = re.compile(r"<(https?://[^>]+)>")
REFERENCE_PATTERN = re.compile(r"^\s*\[[^]]+]:\s*<?([^\s>]+)>?")
REACHABLE_RESTRICTED_STATUSES = {401, 403, 429}
TRANSIENT_STATUSES = {500, 502, 503, 504}


def tracked_markdown_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [
        root / entry.decode()
        for entry in result.stdout.split(b"\0")
        if entry
    ]


def extract_links(path: Path) -> list[Link]:
    links: list[Link] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        targets = [match.group(1) for match in LINK_TARGET_PATTERN.finditer(line)]
        targets.extend(match.group(1) for match in AUTOLINK_PATTERN.finditer(line))
        reference = REFERENCE_PATTERN.match(line)
        if reference:
            targets.append(reference.group(1))

        seen_on_line: set[str] = set()
        for target in targets:
            if target not in seen_on_line:
                links.append(Link(path=path, line=line_number, target=target))
                seen_on_line.add(target)
    return links


def local_target_path(root: Path, link: Link) -> Path | None:
    parsed = urlsplit(link.target)
    if parsed.scheme or link.target.startswith("#") or not parsed.path:
        return None

    target = Path(unquote(parsed.path))
    if target.is_absolute():
        return root / str(target).lstrip("/")
    return link.path.parent / target


def check_external_link(url: str, timeout: float) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return None

    request = Request(
        url,
        method="HEAD",
        headers={"User-Agent": "agent-rca-markdown-link-check/1.0"},
    )
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                if response.status < 400:
                    return None
                status = response.status
        except HTTPError as error:
            status = error.code
            if status in REACHABLE_RESTRICTED_STATUSES:
                return None
        except URLError as error:
            if attempt == 0:
                time.sleep(0.25)
                continue
            return f"network error: {error.reason}"

        if status in TRANSIENT_STATUSES and attempt == 0:
            time.sleep(0.25)
            continue
        return f"HTTP {status}"

    return "external link check failed"


def validate_links(
    root: Path,
    links: list[Link],
    *,
    check_external: bool,
    timeout: float,
) -> tuple[list[str], int, int]:
    errors: list[str] = []
    local_count = 0
    external_count = 0
    checked_external: dict[str, str | None] = {}

    for link in links:
        parsed = urlsplit(link.target)
        location = f"{link.path.relative_to(root)}:{link.line}"
        if parsed.scheme in {"http", "https"}:
            if not check_external:
                continue
            if link.target not in checked_external:
                checked_external[link.target] = check_external_link(link.target, timeout)
                external_count += 1
            error = checked_external[link.target]
            if error:
                errors.append(f"{location}: {link.target}: {error}")
            continue

        if parsed.scheme or link.target.startswith("#"):
            continue
        target = local_target_path(root, link)
        if target is None:
            continue
        local_count += 1
        if not target.exists():
            errors.append(f"{location}: missing local target {link.target}")

    return errors, local_count, external_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-external",
        action="store_true",
        help="validate only repository-local links",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    markdown_files = tracked_markdown_files(root)
    links = [link for path in markdown_files for link in extract_links(path)]
    errors, local_count, external_count = validate_links(
        root,
        links,
        check_external=not args.skip_external,
        timeout=args.timeout,
    )
    if errors:
        print("Markdown link validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(
        f"Validated {len(markdown_files)} tracked Markdown files: "
        f"{local_count} local links, {external_count} external links."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
