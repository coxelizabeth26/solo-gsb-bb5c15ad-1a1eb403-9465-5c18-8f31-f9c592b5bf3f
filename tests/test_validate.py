from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def validate_via_api(schema, document):
    return client.post("/validate", json={"schema": schema, "document": document})


# --------------------------------------------------------- happy path


def test_valid_document_returns_200_and_valid_true():
    r = validate_via_api(
        {"type": "object", "properties": {"a": {"type": "integer"}}}, {"a": 1}
    )
    assert r.status_code == 200
    assert r.json() == {"valid": True, "errors": []}


def test_any_json_value_as_document():
    assert validate_via_api({"type": "null"}, None).json()["valid"] is True
    assert validate_via_api({"type": "string"}, "x").json()["valid"] is True
    assert validate_via_api({"type": "array"}, [1, 2]).json()["valid"] is True
    assert validate_via_api(True, {"anything": "goes"}).json()["valid"] is True


def test_boolean_false_schema_rejects_everything():
    r = validate_via_api(False, 1)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert len(body["errors"]) == 1
    assert body["errors"][0]["instance_path"] == ""
    assert body["errors"][0]["schema_path"] == ""


def test_invalid_document_returns_200_with_errors():
    r = validate_via_api({"properties": {"a": {"type": "integer"}}}, {"a": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["errors"] == [
        {
            "instance_path": "/a",
            "schema_path": "/properties/a/type",
            "keyword": "type",
            "message": "'x' is not of type 'integer'",
        }
    ]


# --------------------------------------------------------- error shape


def test_error_fields_and_rfc6901_escaping():
    r = validate_via_api(
        {"properties": {"a/b": {"type": "integer"}, "~x": {"type": "integer"}}},
        {"a/b": "s", "~x": "s"},
    )
    assert r.status_code == 200
    paths = [e["instance_path"] for e in r.json()["errors"]]
    assert paths == ["/a~1b", "/~0x"]
    for error in r.json()["errors"]:
        assert set(error) == {"instance_path", "schema_path", "keyword", "message"}
        assert error["keyword"] == "type"
        assert error["message"]


def test_array_indices_in_instance_path():
    r = validate_via_api({"items": {"type": "string"}}, [1, "a", 2])
    assert [e["instance_path"] for e in r.json()["errors"]] == ["/0", "/2"]


def test_errors_sorted_by_path_then_keyword():
    schema = {
        "type": "object",
        "properties": {"b": {"type": "integer"}, "a": {"type": "integer"}},
        "required": ["c"],
    }
    r = validate_via_api(schema, {"a": "x", "b": "y"})
    keys = [(e["instance_path"], e["keyword"]) for e in r.json()["errors"]]
    assert keys == [("", "required"), ("/a", "type"), ("/b", "type")]


def test_same_path_sorted_by_keyword():
    schema = {"allOf": [{"maximum": 3}, {"multipleOf": 2}]}
    r = validate_via_api(schema, 5)
    assert [e["keyword"] for e in r.json()["errors"]] == ["maximum", "multipleOf"]
    assert all(e["instance_path"] == "" for e in r.json()["errors"])


def test_leaf_errors_unfold_anyof_context():
    r = validate_via_api({"anyOf": [{"type": "string"}, {"minimum": 10}]}, 5)
    assert r.status_code == 200
    keywords = [e["keyword"] for e in r.json()["errors"]]
    assert keywords == ["minimum", "type"]  # sorted by keyword, no 'anyOf' parent
    assert all(e["schema_path"].startswith("/anyOf/") for e in r.json()["errors"])


def test_nested_instance_and_schema_paths():
    schema = {"properties": {"a": {"items": {"properties": {"b": {"type": "null"}}}}}}
    r = validate_via_api(schema, {"a": [{"b": 1}]})
    (error,) = r.json()["errors"]
    assert error["instance_path"] == "/a/0/b"
    assert error["schema_path"] == "/properties/a/items/properties/b/type"
    assert error["keyword"] == "type"


# --------------------------------------------------------- local references


def test_local_ref_into_defs_resolves():
    schema = {
        "$defs": {"count": {"type": "integer", "minimum": 0}},
        "properties": {"n": {"$ref": "#/$defs/count"}},
    }
    assert validate_via_api(schema, {"n": 3}).json()["valid"] is True
    r = validate_via_api(schema, {"n": -1})
    assert r.json()["valid"] is False
    assert r.json()["errors"][0]["keyword"] == "minimum"


def test_local_ref_with_escaped_pointer():
    schema = {"$defs": {"a/b": {"type": "string"}}, "$ref": "#/$defs/a~1b"}
    assert validate_via_api(schema, "ok").json()["valid"] is True
    assert validate_via_api(schema, 1).json()["valid"] is False


def test_local_anchor_and_dynamic_ref():
    schema = {
        "$defs": {"named": {"$anchor": "named", "type": "string"}},
        "properties": {"a": {"$ref": "#named"}},
    }
    assert validate_via_api(schema, {"a": "x"}).json()["valid"] is True
    assert validate_via_api(schema, {"a": 1}).json()["valid"] is False

    dyn = {
        "$defs": {"node": {"$dynamicAnchor": "node", "type": "object"}},
        "$dynamicRef": "#node",
    }
    assert validate_via_api(dyn, {}).json()["valid"] is True
    assert validate_via_api(dyn, []).json()["valid"] is False


def test_recursive_local_ref():
    schema = {
        "$defs": {
            "node": {
                "type": "object",
                "properties": {"next": {"$ref": "#/$defs/node"}, "v": {"type": "integer"}},
            }
        },
        "$ref": "#/$defs/node",
    }
    assert validate_via_api(schema, {"next": {"next": {}, "v": 1}}).json()["valid"] is True
    r = validate_via_api(schema, {"next": {"v": "x"}})
    assert r.json()["valid"] is False
    assert r.json()["errors"][0]["instance_path"] == "/next/v"


# --------------------------------------------------------- 422: remote references


def test_remote_ref_rejected():
    for ref in (
        "http://example.com/schema.json",
        "https://example.com/s#/definitions/x",
        "other.json",
        "./relative.json#/x",
        "//example.com/protocol-relative",
    ):
        r = validate_via_api({"$ref": ref}, 1)
        assert r.status_code == 422, ref
        assert r.json()["detail"] == "remote reference not allowed"


def test_remote_ref_in_nested_schema_rejected():
    schema = {"properties": {"a": {"items": {"$ref": "http://x.example/s.json"}}}}
    r = validate_via_api(schema, {"a": [1]})
    assert r.status_code == 422


def test_remote_dynamic_ref_rejected():
    r = validate_via_api({"$dynamicRef": "http://x.example/s.json#node"}, {})
    assert r.status_code == 422


def test_ref_like_data_inside_const_is_not_a_reference():
    schema = {"const": {"$ref": "http://example.com/not-a-reference"}}
    r = validate_via_api(schema, {"$ref": "http://example.com/not-a-reference"})
    assert r.status_code == 200
    assert r.json()["valid"] is True


# --------------------------------------------------------- 422: invalid schema


def test_invalid_schemas_rejected():
    for schema in (
        {"type": "nonesuch"},
        {"pattern": "("},
        {"$ref": 5},
        {"properties": {"a": {"type": 3}}},
        "schema-as-string",
        42,
        ["not", "a", "schema"],
        None,
    ):
        r = validate_via_api(schema, 1)
        assert r.status_code == 422, schema
        assert r.json()["detail"] == "invalid schema"


# --------------------------------------------------------- 422: unresolvable local refs


def test_dangling_pointer_ref_rejected():
    r = validate_via_api({"$ref": "#/definitions/missing"}, 1)
    assert r.status_code == 422
    assert r.json()["detail"] == "unresolvable reference"


def test_unresolvable_ref_rejected_even_when_not_exercised():
    # The document never touches property "a"; the dangling ref still fails.
    schema = {"properties": {"a": {"$ref": "#/$defs/nope"}}}
    r = validate_via_api(schema, {})
    assert r.status_code == 422


def test_unknown_anchor_rejected():
    r = validate_via_api({"$ref": "#nosuchanchor"}, 1)
    assert r.status_code == 422


def test_pointer_through_scalar_rejected():
    r = validate_via_api({"$defs": {"x": 1}, "$ref": "#/$defs/x/y"}, 1)
    assert r.status_code == 422


def test_cyclic_ref_rejected():
    r = validate_via_api({"$ref": "#"}, 1)
    assert r.status_code == 422
    assert r.json()["detail"] == "unresolvable reference"


# --------------------------------------------------------- malformed requests


def test_missing_fields_yield_422():
    assert client.post("/validate", json={"schema": {}}).status_code == 422
    assert client.post("/validate", json={"document": 1}).status_code == 422
    assert client.post("/validate", json={}).status_code == 422


def test_unparsable_body_yields_422():
    r = client.post(
        "/validate", content=b"{", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422
