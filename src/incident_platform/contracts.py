"""Runtime validation against the Phase 0 JSON contracts."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Tuple

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .errors import ContractViolation


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "contracts" / "schemas"


@lru_cache(maxsize=1)
def _schema_assets() -> Tuple[Dict[str, Dict[str, Any]], Registry]:
    schemas: Dict[str, Dict[str, Any]] = {}
    resources = []

    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        with path.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        Draft202012Validator.check_schema(schema)
        schema_id = schema.get("$id")
        if not schema_id:
            raise ContractViolation(f"{path} is missing $id")
        schemas[path.name] = schema
        resources.append((schema_id, Resource.from_contents(schema)))

    return schemas, Registry().with_resources(resources)


def validate_contract(schema_name: str, instance: Any) -> None:
    """Validate one value and raise a stable domain error on failure."""

    schemas, registry = _schema_assets()
    try:
        schema = schemas[schema_name]
    except KeyError as error:
        raise ContractViolation(f"unknown schema: {schema_name}") from error

    validator = Draft202012Validator(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
    if not errors:
        return

    details = "; ".join(
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors
    )
    raise ContractViolation(f"{schema_name} validation failed: {details}")
