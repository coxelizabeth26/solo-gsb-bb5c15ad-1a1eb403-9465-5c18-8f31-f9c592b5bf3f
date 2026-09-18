"""JSON Schema Draft 2020-12 validation.

The :func:`validate` entry point validates a JSON document against a JSON
Schema (Draft 2020-12) and returns a sorted list of *leaf* error reports.
Only local, in-document references (``$ref`` / ``$dynamicRef`` values
starting with ``#``) are supported; no external resource is ever fetched.
Three request-level failure classes are distinguished, all answered ``422``
by the service:

* :class:`InvalidSchemaError` -- the schema itself is not a valid Draft
  2020-12 schema (wrong JSON type, unknown ``type`` name, malformed
  ``pattern`` regex, ...);
* :class:`RemoteReferenceError` -- the schema contains a reference that does
  not start with ``#``;
* :class:`UnresolvableReferenceError` -- a local reference points nowhere
  (dangling JSON Pointer, unknown ``$anchor`` / ``$dynamicAnchor``) or the
  references form a cycle that validation cannot unwind.

Parent errors that merely aggregate subschema failures (``anyOf`` and
friends) are unfolded, so every reported error carries the concrete failing
``keyword`` together with RFC 6901 ``instance_path`` / ``schema_path``
pointers. Reports are sorted by ``(instance_path, keyword)``; the sort is
stable, so ties keep the validator's discovery order.
"""

from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing.exceptions import Unresolvable

# Keywords whose values are arbitrary JSON data rather than schemas; a
# "$ref" key inside them is payload, not a reference.
_DATA_KEYWORDS = frozenset({"const", "enum", "default", "examples"})

_REFERENCE_KEYWORDS = ("$ref", "$dynamicRef")

_ANCHOR_KEYWORDS = ("$anchor", "$dynamicAnchor")

# RFC 6901 allows '~' only as part of '~0' or '~1'.
_BAD_TILDE = re.compile(r"~(?![01])")


class InvalidSchemaError(Exception):
    """The supplied schema is not a valid Draft 2020-12 schema (422)."""


class RemoteReferenceError(Exception):
    """The schema references an external resource (422)."""


class UnresolvableReferenceError(Exception):
    """A local reference cannot be resolved within the schema (422)."""


def json_pointer(path: Any) -> str:
    """Render a path sequence as an RFC 6901 JSON Pointer."""
    return "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in path
    )


def _iter_subschemas(node: Any) -> Iterator[dict]:
    """Yield every (sub)schema object of a schema document.

    Keyword values that are plain JSON data (``const``, ``enum``,
    ``default``, ``examples``) are not descended into.
    """
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key not in _DATA_KEYWORDS:
                yield from _iter_subschemas(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_subschemas(item)


def _resolve_pointer(document: Any, fragment: str) -> Any:
    """Resolve a ``#/...`` fragment as a JSON Pointer into ``document``.

    Raises :class:`KeyError` when the pointer cannot be resolved.
    """
    node = document
    for raw_token in fragment[1:].split("/"):
        token = unquote(raw_token)
        if _BAD_TILDE.search(token):
            raise KeyError(token)
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            node = node[token]
        elif isinstance(node, list):
            if not token.isascii() or not token.isdigit():
                raise KeyError(token)
            if token != "0" and token.startswith("0"):
                raise KeyError(token)
            index = int(token)
            if index >= len(node):
                raise KeyError(token)
            node = node[index]
        else:
            raise KeyError(token)
    return node


def _anchors(document: Any) -> set[str]:
    """Collect every ``$anchor`` / ``$dynamicAnchor`` name in the schema."""
    names = set()
    for node in _iter_subschemas(document):
        for keyword in _ANCHOR_KEYWORDS:
            value = node.get(keyword)
            if isinstance(value, str):
                names.add(value)
    return names


def check_references(schema: Any) -> None:
    """Reject remote references and local references that resolve nowhere."""
    anchors: set[str] | None = None
    for node in _iter_subschemas(schema):
        for keyword in _REFERENCE_KEYWORDS:
            ref = node.get(keyword)
            if not isinstance(ref, str):
                continue
            if not ref.startswith("#"):
                raise RemoteReferenceError(
                    f"only local '#' references are supported, got {ref!r}"
                )
            fragment = ref[1:]
            if fragment == "" or fragment.startswith("/"):
                try:
                    _resolve_pointer(schema, fragment)
                except KeyError:
                    raise UnresolvableReferenceError(
                        f"reference {ref!r} does not resolve within the schema"
                    ) from None
            else:
                if anchors is None:
                    anchors = _anchors(schema)
                if unquote(fragment) not in anchors:
                    raise UnresolvableReferenceError(
                        f"reference {ref!r} matches no $anchor in the schema"
                    )


def _leaves(error: ValidationError) -> Iterator[ValidationError]:
    """Yield the leaf errors below ``error`` (itself when it has none)."""
    if error.context:
        for suberror in error.context:
            yield from _leaves(suberror)
    else:
        yield error


def _report(error: ValidationError) -> dict:
    """Build one leaf-error report with RFC 6901 pointers."""
    return {
        "instance_path": json_pointer(error.absolute_path),
        "schema_path": json_pointer(error.absolute_schema_path),
        "keyword": error.validator if isinstance(error.validator, str) else "",
        "message": error.message,
    }


def validate(schema: Any, document: Any) -> list[dict]:
    """Validate ``document`` against a Draft 2020-12 ``schema``.

    Returns the sorted leaf-error reports (empty when the document is
    valid). Raises :class:`InvalidSchemaError`,
    :class:`RemoteReferenceError` or :class:`UnresolvableReferenceError`
    for the 422-class request failures.
    """
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidSchemaError(exc.message) from None
    check_references(schema)
    validator = Draft202012Validator(schema)
    try:
        errors = [
            report
            for error in validator.iter_errors(document)
            for report in map(_report, _leaves(error))
        ]
    except Unresolvable as exc:
        raise UnresolvableReferenceError(str(exc)) from None
    except RecursionError:
        raise UnresolvableReferenceError(
            "references form a cycle that cannot be resolved"
        ) from None
    errors.sort(key=lambda report: (report["instance_path"], report["keyword"]))
    return errors
