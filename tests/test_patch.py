from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def patch(document, operations):
    return client.post(
        "/patch", json={"document": document, "operations": operations}
    )


# ---------------------------------------------------------------- add


def test_add_new_object_key():
    r = patch({"a": 1}, [{"op": "add", "path": "/b", "value": 2}])
    assert r.status_code == 200
    assert r.json() == {"document": {"a": 1, "b": 2}}


def test_add_to_existing_object_key_replaces_value():
    # RFC 6902: add to an existing member behaves like replace.
    r = patch({"a": 1}, [{"op": "add", "path": "/a", "value": 9}])
    assert r.status_code == 200
    assert r.json() == {"document": {"a": 9}}


def test_add_array_insert_at_index():
    r = patch([1, 2, 3], [{"op": "add", "path": "/1", "value": 9}])
    assert r.status_code == 200
    assert r.json() == {"document": [1, 9, 2, 3]}


def test_add_array_insert_at_index_zero():
    r = patch([1], [{"op": "add", "path": "/0", "value": 0}])
    assert r.status_code == 200
    assert r.json() == {"document": [0, 1]}


def test_add_array_append_with_dash():
    r = patch([1, 2], [{"op": "add", "path": "/-", "value": 3}])
    assert r.status_code == 200
    assert r.json() == {"document": [1, 2, 3]}


def test_add_array_at_equal_length_is_append():
    r = patch([1, 2], [{"op": "add", "path": "/2", "value": 3}])
    assert r.status_code == 200
    assert r.json() == {"document": [1, 2, 3]}


def test_add_nested_path():
    r = patch(
        {"a": {"b": [1]}},
        [{"op": "add", "path": "/a/b/-", "value": 2}],
    )
    assert r.status_code == 200
    assert r.json() == {"document": {"a": {"b": [1, 2]}}}


def test_add_value_may_be_null():
    r = patch({"a": 1}, [{"op": "add", "path": "/b", "value": None}])
    assert r.status_code == 200
    assert r.json() == {"document": {"a": 1, "b": None}}


def test_add_replaces_whole_document_at_root():
    r = patch({"a": 1}, [{"op": "add", "path": "", "value": [1, 2]}])
    assert r.status_code == 200
    assert r.json() == {"document": [1, 2]}


def test_add_to_missing_parent_is_409():
    r = patch({"a": 1}, [{"op": "add", "path": "/x/y", "value": 1}])
    assert r.status_code == 409
    body = r.json()
    assert body["failed_index"] == 0
    assert body["path"] == "/x"
    assert "reason" in body


def test_add_array_index_out_of_bounds_is_409():
    r = patch([1], [{"op": "add", "path": "/5", "value": 2}])
    assert r.status_code == 409
    body = r.json()
    assert body["failed_index"] == 0
    assert body["path"] == "/5"
    assert "bounds" in body["reason"]


# ------------------------------------------------------------- remove


def test_remove_object_key():
    r = patch({"a": 1, "b": 2}, [{"op": "remove", "path": "/a"}])
    assert r.status_code == 200
    assert r.json() == {"document": {"b": 2}}


def test_remove_array_element_shifts_indices():
    r = patch([1, 2, 3], [{"op": "remove", "path": "/1"}])
    assert r.status_code == 200
    assert r.json() == {"document": [1, 3]}


def test_remove_missing_key_is_409():
    r = patch({"a": 1}, [{"op": "remove", "path": "/z"}])
    assert r.status_code == 409
    body = r.json()
    assert body["failed_index"] == 0
    assert body["path"] == "/z"


def test_remove_array_index_out_of_bounds_is_409():
    r = patch([1], [{"op": "remove", "path": "/3"}])
    assert r.status_code == 409
    assert r.json()["path"] == "/3"


def test_remove_root_is_409():
    r = patch({"a": 1}, [{"op": "remove", "path": ""}])
    assert r.status_code == 409
    body = r.json()
    assert body["path"] == ""
    assert body["failed_index"] == 0


# ------------------------------------------------------------ replace


def test_replace_object_value():
    r = patch({"a": 1}, [{"op": "replace", "path": "/a", "value": 2}])
    assert r.status_code == 200
    assert r.json() == {"document": {"a": 2}}


def test_replace_array_element():
    r = patch([1, 2, 3], [{"op": "replace", "path": "/1", "value": 9}])
    assert r.status_code == 200
    assert r.json() == {"document": [1, 9, 3]}


def test_replace_whole_document_at_root():
    r = patch({"a": 1}, [{"op": "replace", "path": "", "value": "x"}])
    assert r.status_code == 200
    assert r.json() == {"document": "x"}


def test_replace_missing_target_is_409():
    r = patch({"a": 1}, [{"op": "replace", "path": "/z", "value": 2}])
    assert r.status_code == 409
    assert r.json()["path"] == "/z"


# ----------------------------------------------------------------- test


def test_test_passes_on_equal_value():
    r = patch({"a": [1, 2]}, [{"op": "test", "path": "/a", "value": [1, 2]}])
    assert r.status_code == 200
    assert r.json() == {"document": {"a": [1, 2]}}


def test_test_root_document():
    r = patch({"a": 1}, [{"op": "test", "path": "", "value": {"a": 1}}])
    assert r.status_code == 200


def test_test_failure_is_409_with_index_and_path():
    operations = [
        {"op": "test", "path": "/a", "value": 1},
        {"op": "replace", "path": "/a", "value": 2},
        {"op": "test", "path": "/a", "value": 3},
    ]
    r = patch({"a": 1}, operations)
    assert r.status_code == 409
    body = r.json()
    assert body["failed_index"] == 2
    assert body["path"] == "/a"
    assert "test failed" in body["reason"]


def test_test_missing_path_is_409():
    r = patch({"a": 1}, [{"op": "test", "path": "/z", "value": None}])
    assert r.status_code == 409
    assert r.json()["failed_index"] == 0


def test_test_uses_json_equality_int_and_float():
    r = patch({"a": 1}, [{"op": "test", "path": "/a", "value": 1.0}])
    assert r.status_code == 200


def test_test_distinguishes_bool_from_int():
    r = patch({"a": True}, [{"op": "test", "path": "/a", "value": 1}])
    assert r.status_code == 409


# ------------------------------------------------- pointer escaping / root


def test_pointer_tilde_one_for_slash_key():
    doc = {"a/b": 1}
    r = patch(doc, [{"op": "replace", "path": "/a~1b", "value": 2}])
    assert r.status_code == 200
    assert r.json() == {"document": {"a/b": 2}}


def test_pointer_tilde_zero_for_tilde_key():
    doc = {"~x": 1}
    r = patch(doc, [{"op": "remove", "path": "/~0x"}])
    assert r.status_code == 200
    assert r.json() == {"document": {}}


def test_pointer_escaped_slashes_and_tildes():
    doc = {"/~": 1}
    r = patch(doc, [{"op": "add", "path": "/~1~0y", "value": 2}])
    assert r.status_code == 200
    assert r.json() == {"document": {"/~": 1, "/~y": 2}}


def test_pointer_root_empty_string():
    r = patch(42, [{"op": "test", "path": "", "value": 42}])
    assert r.status_code == 200


# ------------------------------------------------------------- atomicity


def test_operations_run_in_order():
    operations = [
        {"op": "add", "path": "/b", "value": 2},
        {"op": "replace", "path": "/a", "value": 10},
        {"op": "add", "path": "/c", "value": 3},
    ]
    r = patch({"a": 1}, operations)
    assert r.status_code == 200
    assert r.json() == {"document": {"a": 10, "b": 2, "c": 3}}


def test_failure_returns_no_partial_results_and_document_is_untouched():
    operations = [
        {"op": "add", "path": "/b", "value": 2},
        {"op": "remove", "path": "/missing"},
    ]
    r = patch({"a": 1}, operations)
    assert r.status_code == 409
    body = r.json()
    assert set(body) == {"detail", "failed_index", "path", "reason"}
    assert body["failed_index"] == 1
    assert body["path"] == "/missing"
    assert "document" not in body


def test_failed_patch_does_not_mutate_input_document():
    doc = {"a": [1, 2]}
    operations = [
        {"op": "add", "path": "/a/-", "value": 3},
        {"op": "remove", "path": "/x"},
    ]
    r = patch(doc, operations)
    assert r.status_code == 409
    assert doc == {"a": [1, 2]}


def test_added_values_are_deep_copied():
    nested = {"x": 1}
    r = patch({}, [{"op": "add", "path": "/k", "value": nested}])
    assert r.status_code == 200
    assert r.json() == {"document": {"k": {"x": 1}}}
    assert nested == {"x": 1}


def test_later_operations_see_earlier_insertions_in_arrays():
    operations = [
        {"op": "add", "path": "/0", "value": "a"},
        {"op": "add", "path": "/0", "value": "b"},
        {"op": "test", "path": "/0", "value": "b"},
        {"op": "remove", "path": "/1"},
    ]
    r = patch([1], operations)
    assert r.status_code == 200
    assert r.json() == {"document": ["b", 1]}


# --------------------------------------------------------- 422 validation


def test_missing_value_on_add_is_422():
    r = patch({}, [{"op": "add", "path": "/a"}])
    assert r.status_code == 422
    assert r.json()["failed_index"] == 0


def test_missing_value_on_replace_is_422():
    r = patch({"a": 1}, [{"op": "replace", "path": "/a"}])
    assert r.status_code == 422


def test_missing_value_on_test_is_422():
    r = patch({}, [{"op": "test", "path": ""}])
    assert r.status_code == 422


def test_value_on_remove_is_422():
    r = patch({"a": 1}, [{"op": "remove", "path": "/a", "value": 1}])
    assert r.status_code == 422
    assert r.json()["path"] == "/a"


def test_unknown_op_is_422():
    r = patch({}, [{"op": "move", "path": "/a", "from": "/b"}])
    assert r.status_code == 422
    assert r.json()["failed_index"] == 0


def test_missing_op_is_422():
    r = patch({}, [{"path": "/a", "value": 1}])
    assert r.status_code == 422


def test_missing_path_is_422():
    r = patch({}, [{"op": "add", "value": 1}])
    assert r.status_code == 422


def test_non_string_path_is_422():
    r = patch({}, [{"op": "add", "path": 1, "value": 1}])
    assert r.status_code == 422


def test_dash_forbidden_on_replace_is_422():
    r = patch([1], [{"op": "replace", "path": "/-", "value": 2}])
    assert r.status_code == 422
    assert r.json()["failed_index"] == 0
    assert r.json()["path"] == "/-"


def test_dash_forbidden_on_remove_is_422():
    r = patch([1], [{"op": "remove", "path": "/-"}])
    assert r.status_code == 422


def test_dash_forbidden_on_test_is_422():
    r = patch([1], [{"op": "test", "path": "/-", "value": 1}])
    assert r.status_code == 422


def test_pointer_not_starting_with_slash_is_422():
    r = patch({}, [{"op": "add", "path": "a", "value": 1}])
    assert r.status_code == 422


def test_pointer_bad_tilde_escape_is_422():
    r = patch({}, [{"op": "add", "path": "/a~2", "value": 1}])
    assert r.status_code == 422


def test_pointer_dangling_tilde_is_422():
    r = patch({}, [{"op": "add", "path": "/a~", "value": 1}])
    assert r.status_code == 422


def test_leading_zero_array_index_is_422():
    r = patch([1], [{"op": "remove", "path": "/01"}])
    assert r.status_code == 422


def test_non_numeric_array_index_is_422():
    r = patch([1], [{"op": "remove", "path": "/x"}])
    assert r.status_code == 422


def test_non_numeric_array_index_in_parent_path_is_422():
    r = patch([1], [{"op": "add", "path": "/x/y", "value": 1}])
    assert r.status_code == 422


def test_missing_parent_in_object_is_409():
    r = patch({"a": 1}, [{"op": "add", "path": "/x/y", "value": 1}])
    assert r.status_code == 409


def test_traversing_scalar_parent_is_409():
    r = patch({"a": 1}, [{"op": "add", "path": "/a/b", "value": 1}])
    assert r.status_code == 409
    body = r.json()
    assert body["path"] == "/a/b"
    assert body["failed_index"] == 0


def test_dash_mid_path_is_not_traversable():
    r = patch([1], [{"op": "add", "path": "/-/x", "value": 1}])
    assert r.status_code == 409
    assert r.json()["path"] == "/-"


def test_same_token_is_key_on_object_but_index_on_array():
    # "/01" is a perfectly legal object member name.
    r = patch({"01": 1}, [{"op": "remove", "path": "/01"}])
    assert r.status_code == 200
    assert r.json() == {"document": {}}


def test_invalid_operation_anywhere_reports_its_index():
    operations = [
        {"op": "add", "path": "/a", "value": 1},
        {"op": "frobnicate", "path": "/a"},
    ]
    r = patch({}, operations)
    assert r.status_code == 422
    assert r.json()["failed_index"] == 1


def test_empty_operations_returns_document_unchanged():
    r = patch({"a": 1}, [])
    assert r.status_code == 200
    assert r.json() == {"document": {"a": 1}}


def test_missing_request_fields_rejected():
    r = client.post("/patch", json={"document": {}})
    assert r.status_code == 422


def test_operations_must_be_array():
    r = client.post(
        "/patch", json={"document": {}, "operations": {"op": "add"}}
    )
    assert r.status_code == 422


def test_operation_items_must_be_objects():
    r = patch({}, ["add"])
    assert r.status_code == 422
    assert r.json()["failed_index"] == 0


# -------------------------------------------------- other endpoints intact


def test_health_unchanged():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_merge_still_works():
    r = client.post(
        "/merge",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 1}},
    )
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": 2}}
