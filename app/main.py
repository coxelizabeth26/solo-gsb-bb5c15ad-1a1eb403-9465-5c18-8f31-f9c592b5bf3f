"""JSON merge and patch service.

* ``POST /merge`` accepts ``{"base": ..., "left": ..., "right": ...}`` and
  merges the changes that left and right each made relative to base:

  * a change made on only one side is taken;
  * identical changes on both sides are taken;
  * objects are merged key by key, recursively;
  * arrays are compared as a whole (never merged element-wise);
  * conflicting changes (same path, different values, or one side deleting a
    subtree the other side modified) produce a 409 response listing every
    conflict, sorted by JSON Pointer (RFC 6901).

* ``POST /patch`` accepts ``{"document": ..., "operations": [...]}`` and
  atomically applies RFC 6902 ``add`` / ``remove`` / ``replace`` / ``test``
  operations to a deep copy of the document. Malformed requests yield 422;
  unresolvable paths, out-of-bounds array indices and failed ``test``
  operations yield 409 without any partial result.

* ``POST /canonicalize`` accepts ``{"document": ...}`` and returns the
  RFC 8785 (JCS) canonical JSON text together with its SHA-256 digest.

* ``POST /validate`` accepts ``{"schema": ..., "document": ...}`` and
  validates the document against the schema as JSON Schema Draft 2020-12.
  Only document-local ``#`` references are allowed; remote references,
  invalid schemas and unresolvable local references yield 422. A valid
  schema always yields 200 with ``valid`` and (on failure) every leaf
  error as RFC 6901 paths, sorted by instance path and keyword.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .canonicalize import CanonicalizationError, canonicalize
from .jsonutil import MISSING, json_equal
from .patch import PatchConflict, PatchStructureError, apply_patch
from .validate import SchemaValidationError, build_validator, leaf_errors

app = FastAPI(title="JSON Merge, Patch, Validate & Canonicalize Service", version="1.3.0")


def escape_pointer(token: str) -> str:
    """Escape one reference token as per RFC 6901."""
    return token.replace("~", "~0").replace("/", "~1")


def merge3(base: Any, left: Any, right: Any, path: str, conflicts: list) -> Any:
    """Merge left and right against base, collecting conflicts.

    Returns the merged value (or MISSING for a deleted/absent key). Every
    conflict found is appended to ``conflicts`` as a dict with the JSON
    Pointer ``path`` and the ``base``/``left``/``right`` values at that path.
    """
    # Both sides agree (includes: unchanged on both, deleted on both).
    if json_equal(left, right):
        return left
    # Only the right side changed.
    if json_equal(left, base):
        return right
    # Only the left side changed.
    if json_equal(right, base):
        return left
    # Both sides changed in different ways. Objects merge key by key; a key
    # absent from base but present as an object on both sides is merged
    # against an empty object. Anything else (scalars, arrays, type changes,
    # delete-vs-modify) is a conflict.
    if isinstance(left, dict) and isinstance(right, dict):
        if isinstance(base, dict):
            base_dict = base
        elif base is MISSING:
            base_dict = {}
        else:
            base_dict = None
        if base_dict is not None:
            merged = {}
            keys = sorted(base_dict.keys() | left.keys() | right.keys())
            for key in keys:
                value = merge3(
                    base_dict.get(key, MISSING),
                    left.get(key, MISSING),
                    right.get(key, MISSING),
                    f"{path}/{escape_pointer(key)}",
                    conflicts,
                )
                if value is not MISSING:
                    merged[key] = value
            return merged
    conflicts.append({"path": path, "base": base, "left": left, "right": right})
    return MISSING


def side_repr(value: Any) -> dict:
    """Represent one side of a conflict, distinguishing missing from null."""
    if value is MISSING:
        return {"present": False, "value": None}
    return {"present": True, "value": value}


class MergeRequest(BaseModel):
    base: Any = Field(...)
    left: Any = Field(...)
    right: Any = Field(...)


@app.post("/merge")
def merge_endpoint(request: MergeRequest):
    conflicts: list = []
    merged = merge3(request.base, request.left, request.right, "", conflicts)
    if conflicts:
        conflicts.sort(key=lambda c: c["path"])
        return JSONResponse(
            status_code=409,
            content={
                "detail": "merge conflict",
                "conflicts": [
                    {
                        "path": c["path"],
                        "base": side_repr(c["base"]),
                        "left": side_repr(c["left"]),
                        "right": side_repr(c["right"]),
                    }
                    for c in conflicts
                ],
            },
        )
    return {"merged": merged}


class PatchRequest(BaseModel):
    document: Any = Field(...)
    operations: list = Field(...)


@app.post("/patch")
def patch_endpoint(request: PatchRequest):
    try:
        document = apply_patch(request.document, request.operations)
    except PatchConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "patch conflict",
                "failed_index": exc.index,
                "path": exc.path,
                "reason": exc.reason,
            },
        )
    except PatchStructureError as exc:
        content: dict = {
            "detail": "invalid patch operation",
            "reason": exc.reason,
        }
        if exc.index >= 0:
            content["failed_index"] = exc.index
        if isinstance(exc.path, str):
            content["path"] = exc.path
        return JSONResponse(status_code=422, content=content)
    return {"document": document}


class CanonicalizeRequest(BaseModel):
    document: Any = Field(...)


@app.post("/canonicalize")
def canonicalize_endpoint(request: CanonicalizeRequest):
    try:
        canonical = canonicalize(request.document)
    except CanonicalizationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "document cannot be canonicalized", "reason": str(exc)},
        )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"canonical": canonical, "sha256": digest}


@app.get("/health")
def health():
    return {"status": "ok"}


class ValidateRequest(BaseModel):
    schema_: Any = Field(..., alias="schema")
    document: Any = Field(...)

    model_config = {"populate_by_name": True}


@app.post("/validate")
def validate_endpoint(request: ValidateRequest):
    try:
        validator = build_validator(request.schema_)
        errors = leaf_errors(validator, request.document)
    except SchemaValidationError as exc:
        content: dict = {"detail": "invalid schema", "reason": exc.reason}
        if exc.ref is not None:
            content["ref"] = exc.ref
        content["message"] = exc.detail
        return JSONResponse(status_code=422, content=content)
    if errors:
        return {"valid": False, "errors": errors}
    return {"valid": True, "errors": []}
