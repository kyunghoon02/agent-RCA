from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from tools.check_markdown_links import (
    Link,
    check_external_link,
    extract_links,
    local_target_path,
    validate_links,
)


class MarkdownLinkValidationTest(unittest.TestCase):
    def test_extracts_inline_autolink_and_reference_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "README.md"
            document.write_text(
                "[local](docs/guide.md)\n"
                "<https://example.com/reference>\n"
                "[guide]: docs/guide.md#usage\n",
                encoding="utf-8",
            )

            targets = [link.target for link in extract_links(document)]

        self.assertEqual(
            targets,
            [
                "docs/guide.md",
                "https://example.com/reference",
                "docs/guide.md#usage",
            ],
        )

    def test_resolves_relative_local_target_from_document(self) -> None:
        root = Path("/workspace")
        document = root / "docs" / "README.md"
        link = Link(
            path=document,
            line=1,
            target="../contracts/schema.json#identifier",
        )

        resolved = local_target_path(root, link)

        self.assertEqual(resolved, root / "docs" / "../contracts/schema.json")

    def test_reports_missing_local_target_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = root / "README.md"
            document.write_text("[missing](docs/missing.md)\n", encoding="utf-8")
            links = extract_links(document)

            errors, local_count, external_count = validate_links(
                root,
                links,
                check_external=False,
                timeout=0.1,
            )

        self.assertEqual(local_count, 1)
        self.assertEqual(external_count, 0)
        self.assertEqual(
            errors,
            ["README.md:1: missing local target docs/missing.md"],
        )

    @patch("tools.check_markdown_links.urlopen")
    def test_external_404_is_reported_as_broken(self, mocked_urlopen) -> None:
        mocked_urlopen.side_effect = HTTPError(
            "https://doi.org/example",
            404,
            "Not Found",
            None,
            None,
        )

        error = check_external_link("https://doi.org/example", timeout=0.1)

        self.assertEqual(error, "HTTP 404")


if __name__ == "__main__":
    unittest.main()
