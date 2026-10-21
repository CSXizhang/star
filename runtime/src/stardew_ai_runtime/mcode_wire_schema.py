"""Expose the canonical game contract to mcode's shallow argument converter.

mcode 0.6.x validates local references but does not follow them while converting
XML wire values. Inline references and keep concrete nested parameter types.
The server separately accepts bounded legacy wrappers; native validation stays intact.
"""

from copy import deepcopy
from typing import Any


def mcode_tool_schema(name: str, schema: dict[str, Any], parameter_shapes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make a client-only schema projection without changing the server parser."""
    root = deepcopy(schema)

    def resolve(value: Any, field: str = "", seen: frozenset[str] = frozenset()) -> Any:
        if isinstance(value, list):
            return [resolve(part, field, seen) for part in value]
        if not isinstance(value, dict):
            return value
        if field == "params" and parameter_shapes and (value.get("type") == "object"
                or any(part.get("type") == "object" for part in value.get("anyOf", []))):
            value = {"type": "object", "properties": deepcopy(parameter_shapes), "additionalProperties": False}
        if "$ref" in value:
            ref = value["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
                raise ValueError("mcode tool schemas require non-recursive local references")
            target = root
            for key in ref[2:].split("/"):
                target = target[key.replace("~1", "/").replace("~0", "~")]
            return resolve({**target, **{key: part for key, part in value.items() if key != "$ref"}},
                           field, seen | {ref})
        result = {}
        for key, part in value.items():
            if key in {"$defs", "definitions"}:
                continue
            if key == "properties":
                result[key] = {prop: resolve(shape, prop, seen) for prop, shape in part.items()}
            else:
                result[key] = resolve(part, field, seen)
        # Keep concrete array/object/integer types after inlining references.
        # An anyOf(array, XML-wrapper) hides items from mcode's shallow converter
        # and turns nested coordinate/count values into strings. The canonical
        # parser still accepts bounded legacy wrappers, but new calls are typed.
        return result

    return resolve(root)
