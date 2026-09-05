"""JSON schema normalisation for strict structured output.

Pydantic's generated schema is rejected by strict mode: every object needs
``additionalProperties: false`` including those under ``$defs``, every property
must appear in ``required``, and ``default`` is not permitted. Normalising here
means the constraint is handled once rather than discovered at runtime on a
live incident.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def strictify(node: Any) -> Any:  # noqa: ANN401 - walks arbitrary JSON schema nodes
    """Recursively make a JSON schema acceptable to strict structured output."""
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
            properties = node.get("properties")
            if isinstance(properties, dict):
                # Strict mode requires every property to be required. Fields
                # that are logically optional stay in the schema and are asked
                # for explicitly as an empty string instead.
                node["required"] = list(properties.keys())
        node.pop("default", None)
        for value in node.values():
            strictify(value)
    elif isinstance(node, list):
        for value in node:
            strictify(value)
    return node


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return a strict-mode-safe JSON schema for a Pydantic model."""
    schema: dict[str, Any] = model.model_json_schema()
    result = strictify(schema)
    assert isinstance(result, dict)
    return result
