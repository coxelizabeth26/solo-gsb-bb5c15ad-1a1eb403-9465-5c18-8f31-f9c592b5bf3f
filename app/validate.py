"""JSON Schema Draft 2020-12 validation with document-local references only.

The schema supplied to :func:`build_validator` may reference other parts of
itself via ``$ref`` / ``$dynamicRef`` whose value is a fragment-only URI
reference (starts with ``#``). Every other reference -- a remote URI
(``https://example.com/schema``), a relative document reference
(``other.json``), or a fragment that cannot be resolved inside the document
-- is rejected before validation runs. The reference registry is additionally
configured to never retrieve anything from the network.
"""

from __future__ import annotations

import sys
from collections import deque
from typing import Any, Deque, Iterator

import referencing
import referencing.exceptions
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing.jsonschema import DRAFT202012

# Reference keywords whose value identifies another schema resource.
_REF_KEYWORDS = ("$ref", "$dynamicRef")

# Keywords whose values are subschemas, arrays of subschemas, or objects whose
# values are subschemas. Annotation-only keywords (enum/default/examples/...)
# are deliberately absent: arbitrary JSON inside them must not be mistaken
# for references.
_SUBSCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SUBSCHEMA_ARRAY_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SUBSCHEMA_MAP_KEYS = frozenset(
    {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}
)


class SchemaValidationError(Exception):
    """The supplied schema cannot be used for validation.

    ``reason`` is one of ``"invalid schema"``, ``"remote reference"`` or
    ``"unresolvable reference"``.
    """

    def __init__(self, reason: str, detail: str, ref: str | None = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.ref = ref


def _iter_refs(node: Any, location: str = "") -> Iterator[tuple[str, str, str]]:
    """Yield ``(keyword, ref, json_pointer_location)`` for every reference.

    The walk follows only keywords defined by the Draft 2020-12 meta-schema
    as holding subschemas (plus the legacy ``definitions`` alias), so JSON
    inside annotations such as ``enum`` / ``default`` / ``examples`` is not
    mistaken for a schema.
    """
    if isinstance(node, bool):
        return
    if not isinstance(node, dict):
        return

    for keyword in _REF_KEYWORDS:
        ref = node.get(keyword)
        if isinstance(ref, str):
            yield keyword, ref, location

    for key, value in node.items():
        token = str(key).replace("~", "~0").replace("/", "~1")
        child_location = f"{location}/{token}"
        if key in _SUBSCHEMA_KEYS:
            yield from _iter_refs(value, child_location)
        elif key in _SUBSCHEMA_ARRAY_KEYS and isinstance(value, list):
            for index, subschema in enumerate(value):
                yield from _iter_refs(subschema, f"{child_location}/{index}")
        elif key in _SUBSCHEMA_MAP_KEYS and isinstance(value, dict):
            for name, subschema in value.items():
                name_token = str(name).replace("~", "~0").replace("/", "~1")
                yield from _iter_refs(subschema, f"{child_location}/{name_token}")


def _retrieve_blocked(uri: str):
    """Refusal callback: nothing may be fetched, not even from localhost."""
    raise referencing.exceptions.Unresolvable(ref=uri)


def build_validator(schema: Any) -> Draft202012Validator:
    """Compile ``schema`` as Draft 2020-12, refusing non-local references.

    Raises :class:`SchemaValidationError` for schemas that are not objects or
    booleans, carry references that leave the document, fail the Draft
    2020-12 meta-schema, or contain references that cannot be resolved
    inside the document.
    """
    if not isinstance(schema, (dict, bool)):
        raise SchemaValidationError(
            "invalid schema",
            "JSON Schema must be a JSON object or boolean",
        )

    references = list(_iter_refs(schema))
    for keyword, ref, location in references:
        if not ref.startswith("#"):
            if ref:
                detail = (
                    "only document-local references (starting with '#') are "
                    f"allowed; {keyword} {ref!r} at {location or '#'} is remote "
                    "or cross-document"
                )
            else:
                detail = f"{keyword} at {location or '#'} has an empty value"
            raise SchemaValidationError("remote reference", detail, ref or None)

    # check_schema raises SchemaError for anything the Draft 2020-12
    # meta-schema rejects (including non-string reference values).
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise SchemaValidationError(
            "invalid schema",
            f"schema is not a valid Draft 2020-12 schema: {exc.message}",
        ) from exc

    # Registry with no external resources and a retrieval callback that
    # always fails -- defense in depth behind the fragment-only scan above.
    registry = referencing.Registry(retrieve=_retrieve_blocked)

    # Resolve every reference statically against the document itself, so an
    # unresolvable local pointer/anchor is reported as a bad schema rather
    # than depending on which part of the document is validated.
    root_resource = DRAFT202012.create_resource(schema)
    resolver = registry.resolver_with_root(root_resource)
    for _keyword, ref, _location in references:
        try:
            resolver.lookup(ref)
        except referencing.exceptions.Unresolvable as exc:
            raise SchemaValidationError(
                "unresolvable reference",
                f"local reference {ref!r} cannot be resolved: "
                f"{str(exc).splitlines()[0]}",
                ref,
            ) from exc

    return Draft202012Validator(schema, registry=registry)


def _escape_pointer(token: Any) -> str:
    """Escape one JSON Pointer reference token per RFC 6901."""
    return str(token).replace("~", "~0").replace("/", "~1")


def _pointer(path: Deque[Any]) -> str:
    """Render an error path deque as an RFC 6901 JSON Pointer."""
    return "".join(f"/{_escape_pointer(token)}" for token in path)


def _path_sort_key(path: str):
    """Sort pointer tokens as JSON types: integer array indices first.

    Within an index token, numeric order; within a member-name token,
    lexicographic order. The empty pointer (the whole document) sorts first.
    """
    if path == "":
        return ()
    key = []
    for raw in path.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if token.isdigit() and not (len(token) > 1 and token.startswith("0")):
            key.append((0, int(token), ""))
        else:
            key.append((1, 0, token))
    return tuple(key)


def leaf_errors(validator: Draft202012Validator, document: Any) -> list[dict]:
    """Validate ``document`` and return all leaf errors, stably sorted.

    Each item gives the RFC 6901 ``instance_path`` and ``schema_path``, the
    failing ``keyword`` and the validator's message. Errors with subschema
    context (e.g. ``oneOf`` / ``anyOf``) are expanded down to their leaves;
    identical leaves reached through different branches are deduplicated.
    """
    # jsonschema descends the instance and the schema in Python frames, so
    # deeply nested (but perfectly finite) documents need headroom beyond the
    # interpreter's default recursion limit.
    previous_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(previous_limit, 10000))
    try:
        top_errors = list(validator.iter_errors(document))
    except referencing.exceptions.Unresolvable as exc:
        # Referencing can in principle still fail lazily (e.g. in the dynamic
        # scope of a $dynamicRef) even after the static lookup pass.
        raise SchemaValidationError(
            "unresolvable reference",
            f"a local reference cannot be resolved: {str(exc).splitlines()[0]}",
        ) from exc
    except RecursionError as exc:
        # Either a genuinely cyclic schema (e.g. {"$ref": "#"} with no other
        # assertion) or nesting far beyond the raised limit.
        raise SchemaValidationError(
            "unresolvable reference",
            "reference resolution cycles or the document is nested too deeply",
        ) from exc
    finally:
        sys.setrecursionlimit(previous_limit)

    leaves: list[dict] = []
    seen: set[tuple] = set()

    def walk(error) -> None:
        if error.context:
            for child in error.context:
                walk(child)
            return
        item = {
            "instance_path": _pointer(deque(error.absolute_path)),
            "schema_path": _pointer(deque(error.absolute_schema_path)),
            "keyword": error.validator or "",
            "message": error.message,
        }
        dedup_key = (
            item["instance_path"],
            item["schema_path"],
            item["keyword"],
            item["message"],
        )
        if dedup_key not in seen:
            seen.add(dedup_key)
            leaves.append(item)

    for error in top_errors:
        walk(error)

    leaves.sort(
        key=lambda e: (
            _path_sort_key(e["instance_path"]),
            e["keyword"],
            _path_sort_key(e["schema_path"]),
            e["message"],
        )
    )
    return leaves
