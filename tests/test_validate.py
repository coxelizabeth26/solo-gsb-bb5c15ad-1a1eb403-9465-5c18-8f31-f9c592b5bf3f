"""Tests for ``POST /validate`` (JSON Schema Draft 2020-12)."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def validate(schema, document):
    return client.post(
        "/validate", json={"schema": schema, "document": document}
    )


# ------------------------------------------------------------- happy paths


def test_valid_document():
    r = validate({"type": "object", "properties": {"a": {"type": "integer"}}},
                 {"a": 3})
    assert r.status_code == 200
    assert r.json() == {"valid": True, "errors": []}


def test_invalid_document_is_still_200():
    # A valid schema always yields 200, whether or not the document matches.
    r = validate({"type": "string"}, 42)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["errors"] == [
        {
            "instance_path": "",
            "schema_path": "/type",
            "keyword": "type",
            "message": "42 is not of type 'string'",
        }
    ]


def test_boolean_schemas():
    assert validate(True, "anything").json() == {"valid": True, "errors": []}
    r = validate(False, "anything")
    assert r.status_code == 200
    assert r.json()["valid"] is False
    assert r.json()["errors"][0]["keyword"] == ""
    assert r.json()["errors"][0]["instance_path"] == ""


def test_document_may_be_any_json_value():
    assert validate({"type": "null"}, None).json()["valid"] is True
    assert validate({"type": "array"}, []).json()["valid"] is True


# ------------------------------------------------------------- local refs


def test_local_pointer_reference():
    schema = {
        "$defs": {"name": {"type": "string", "minLength": 1}},
        "properties": {"first": {"$ref": "#/$defs/name"}},
    }
    ok = validate(schema, {"first": "Ada"})
    assert ok.status_code == 200 and ok.json()["valid"] is True
    bad = validate(schema, {"first": ""})
    assert bad.status_code == 200 and bad.json()["valid"] is False
    err = bad.json()["errors"][0]
    assert err["instance_path"] == "/first"
    assert err["schema_path"] == "/properties/first/minLength"
    assert err["keyword"] == "minLength"


def test_plain_name_fragment_anchor_with_id():
    schema = {
        "$id": "https://example.test/root",
        "$defs": {"a": {"$anchor": "a", "type": "integer"}},
        "$ref": "#a",
    }
    assert validate(schema, 5).json()["valid"] is True
    assert validate(schema, "x").json()["valid"] is False


def test_recursive_schema_walks_arbitrary_depth():
    schema = {
        "$defs": {
            "node": {
                "type": "object",
                "properties": {
                    "value": {"type": "integer"},
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/node"},
                    },
                },
            }
        },
        "$ref": "#/$defs/node",
    }
    doc = {"children": [{"value": 1}, {"children": [{"value": "no"}]}]}
    r = validate(schema, doc)
    assert r.status_code == 200
    errors = r.json()["errors"]
    assert len(errors) == 1
    assert errors[0]["instance_path"] == "/children/1/children/0/value"
    assert errors[0]["keyword"] == "type"


def test_dynamic_anchor_resolves_locally():
    schema = {
        "$defs": {
            "intnode": {"$dynamicAnchor": "kind", "type": "integer"},
            "refnode": {"$dynamicRef": "#kind"},
        },
        "properties": {"x": {"$ref": "#/$defs/refnode"}},
    }
    assert validate(schema, {"x": 3}).json()["valid"] is True
    r = validate(schema, {"x": "s"})
    assert r.status_code == 200 and r.json()["valid"] is False
    errors = r.json()["errors"]
    assert len(errors) == 1
    assert errors[0]["instance_path"] == "/x"
    assert errors[0]["keyword"] == "type"


# ------------------------------------------------------------- leaf errors


def test_all_leaf_errors_are_reported():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
            "arr": {"items": {"type": "boolean"}},
        },
    }
    r = validate(schema, {"a": 1, "b": "x", "arr": [True, 5]})
    paths = [(e["instance_path"], e["keyword"]) for e in r.json()["errors"]]
    assert paths == [
        ("/a", "type"),
        ("/arr/1", "type"),
        ("/b", "type"),
    ]


def test_one_of_expands_to_branch_leaves():
    r = validate({"oneOf": [{"type": "string"}, {"type": "integer"}]}, True)
    assert r.status_code == 200
    errors = r.json()["errors"]
    assert [e["schema_path"] for e in errors] == ["/oneOf/0/type", "/oneOf/1/type"]
    assert {e["keyword"] for e in errors} == {"type"}


def test_array_indices_sort_numerically_and_before_member_names():
    schema = {
        "type": "object",
        "properties": {
            "z": {"type": "string"},
            "list": {"prefixItems": [
                {"type": "string"}, {"type": "string"}, {"type": "string"},
            ]},
            "a": {"type": "string"},
        },
    }
    r = validate(schema, {"z": 1, "list": [1, 2, 3], "a": 2})
    assert [e["instance_path"] for e in r.json()["errors"]] == [
        "/a", "/list/0", "/list/1", "/list/2", "/z",
    ]


def test_same_path_sorts_by_keyword_then_schema_path():
    # const and type both fail at the document root; "const" sorts first.
    r = validate({"type": "string", "const": "x"}, 5)
    keywords = [e["keyword"] for e in r.json()["errors"]]
    assert keywords == ["const", "type"]


def test_special_characters_in_paths_are_rfc6901_escaped():
    r = validate(
        {"properties": {"a/b": {"type": "string"}, "~x": {"type": "integer"}}},
        {"a/b": 1, "~x": "s"},
    )
    # Member names sort lexicographically; token-wise "/a~1b" vs "/~0x":
    # "a" (0x61) sorts before "~" (0x7E).
    assert [e["instance_path"] for e in r.json()["errors"]] == [
        "/a~1b", "/~0x",
    ]


def test_errors_are_deduplicated():
    # A $ref and an identical sibling assertion produce two library errors
    # with exactly the same paths, keyword and message: keep one.
    schema = {
        "$defs": {"a": {"type": "string"}},
        "$ref": "#/$defs/a",
        "type": "string",
    }
    r = validate(schema, 1)
    assert r.status_code == 200
    assert len(r.json()["errors"]) == 1
    assert r.json()["errors"][0]["keyword"] == "type"


# ------------------------------------------------------------- 422 cases


def test_remote_http_reference_rejected():
    r = validate({"$ref": "https://example.com/schema.json"}, {})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "invalid schema"
    assert body["reason"] == "remote reference"
    assert body["ref"] == "https://example.com/schema.json"


def test_remote_dynamic_reference_rejected():
    r = validate({"$dynamicRef": "https://example.com/schema#x"}, {})
    assert r.status_code == 422
    assert r.json()["reason"] == "remote reference"


def test_relative_cross_document_reference_rejected():
    r = validate({"$ref": "other.json#/$defs/x"}, {})
    assert r.status_code == 422
    assert r.json()["reason"] == "remote reference"
    assert r.json()["ref"] == "other.json#/$defs/x"


def test_remote_reference_nested_in_subschema_rejected():
    schema = {
        "type": "object",
        "properties": {
            "x": {"items": {"oneOf": [{"$ref": "http://evil.example/s"}]}},
        },
    }
    r = validate(schema, {"x": [{}]})
    assert r.status_code == 422
    assert r.json()["reason"] == "remote reference"
    assert r.json()["ref"] == "http://evil.example/s"


def test_remote_reference_inside_annotations_is_ignored():
    # enum/default/examples are annotations, not schemas: a $ref-shaped value
    # inside them is data and must not trigger the remote-reference guard.
    schema = {"enum": [{"$ref": "https://evil.example"}]}
    r = validate(schema, {"$ref": "https://evil.example"})
    assert r.status_code == 200 and r.json()["valid"] is True
    r = validate(schema, "anything else")
    assert r.status_code == 200 and r.json()["valid"] is False


def test_unresolvable_local_pointer_is_422_regardless_of_document():
    r = validate({"$ref": "#/$defs/missing"}, {})
    assert r.status_code == 422
    body = r.json()
    assert body["reason"] == "unresolvable reference"
    assert body["ref"] == "#/$defs/missing"


def test_unresolvable_anchor_is_422():
    r = validate({"$id": "https://example.test/s", "$ref": "#ghost"}, {})
    assert r.status_code == 422
    assert r.json()["reason"] == "unresolvable reference"


def test_self_referential_cycle_is_422():
    # {"$ref": "#"} with no sibling assertion never produces a result.
    r = validate({"$ref": "#"}, 1)
    assert r.status_code == 422
    assert r.json()["reason"] == "unresolvable reference"


def test_invalid_type_keyword_is_422():
    r = validate({"type": "nope"}, {})
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid schema"


def test_schema_that_is_not_object_or_boolean_is_422():
    r = validate("not-a-schema", {})
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid schema"


def test_non_string_ref_value_is_422():
    r = validate({"$ref": 3}, {})
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid schema"


def test_invalid_schema_does_not_depend_on_document():
    # The same broken schema is 422 even against a document that "matches".
    assert validate({"$ref": "#/$defs/missing"}, None).status_code == 422


# ------------------------------------------------------------- request shape


def test_missing_document_field_is_422():
    r = client.post("/validate", json={"schema": {"type": "integer"}})
    assert r.status_code == 422


def test_missing_schema_field_is_422():
    r = client.post("/validate", json={"document": 1})
    assert r.status_code == 422


def test_malformed_json_body_is_422():
    r = client.post(
        "/validate",
        content="{not valid json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


def test_schema_accepted_via_alias_only():
    # The JSON field is "schema"; the Python attribute is schema_.
    r = client.post(
        "/validate", json={"schema": False, "document": 1}
    )
    assert r.status_code == 200 and r.json()["valid"] is False
