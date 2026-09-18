from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def merge(base, left, right):
    return client.post("/merge", json={"base": base, "left": left, "right": right})


def test_left_only_change_is_taken():
    r = merge({"a": 1, "b": 2}, {"a": 10, "b": 2}, {"a": 1, "b": 2})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": 10, "b": 2}}


def test_right_only_change_is_taken():
    r = merge({"a": 1}, {"a": 1}, {"a": 2})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": 2}}


def test_identical_change_on_both_sides():
    r = merge({"a": 1}, {"a": 5}, {"a": 5})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": 5}}


def test_non_overlapping_changes_merge():
    r = merge(
        {"a": 1, "b": 2},
        {"a": 10, "b": 2},
        {"a": 1, "b": 20},
    )
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": 10, "b": 20}}


def test_nested_objects_merge_recursively():
    r = merge(
        {"cfg": {"x": 1, "y": 2}},
        {"cfg": {"x": 100, "y": 2}},
        {"cfg": {"x": 1, "y": 200}},
    )
    assert r.status_code == 200
    assert r.json() == {"merged": {"cfg": {"x": 100, "y": 200}}}


def test_key_added_on_one_side():
    r = merge({"a": 1}, {"a": 1, "b": 2}, {"a": 1})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": 1, "b": 2}}


def test_both_sides_add_same_key_same_value():
    r = merge({}, {"a": 1}, {"a": 1})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": 1}}


def test_both_sides_add_objects_with_disjoint_keys():
    r = merge({}, {"a": {"x": 1}}, {"a": {"y": 2}})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": {"x": 1, "y": 2}}}


def test_key_deleted_on_one_side_and_untouched_on_other():
    r = merge({"a": 1, "b": 2}, {"b": 2}, {"a": 1, "b": 2})
    assert r.status_code == 200
    assert r.json() == {"merged": {"b": 2}}


def test_key_deleted_on_both_sides():
    r = merge({"a": 1}, {}, {})
    assert r.status_code == 200
    assert r.json() == {"merged": {}}


def test_conflicting_scalar_change_returns_409():
    r = merge({"a": 1}, {"a": 2}, {"a": 3})
    assert r.status_code == 409
    body = r.json()
    assert body["conflicts"] == [
        {
            "path": "/a",
            "base": {"present": True, "value": 1},
            "left": {"present": True, "value": 2},
            "right": {"present": True, "value": 3},
        }
    ]


def test_delete_vs_descendant_modify_returns_409():
    r = merge(
        {"a": {"x": 1}},
        {},                      # left deleted the subtree
        {"a": {"x": 2}},         # right modified a descendant
    )
    assert r.status_code == 409
    body = r.json()
    assert body["conflicts"] == [
        {
            "path": "/a",
            "base": {"present": True, "value": {"x": 1}},
            "left": {"present": False, "value": None},
            "right": {"present": True, "value": {"x": 2}},
        }
    ]


def test_delete_vs_modify_leaf_returns_409():
    r = merge(
        {"a": {"x": 1, "y": 2}},
        {"a": {"x": 1}},              # left deleted y
        {"a": {"x": 1, "y": 3}},      # right changed y
    )
    assert r.status_code == 409
    assert r.json()["conflicts"] == [
        {
            "path": "/a/y",
            "base": {"present": True, "value": 2},
            "left": {"present": False, "value": None},
            "right": {"present": True, "value": 3},
        }
    ]


def test_missing_is_distinguished_from_null():
    # left deletes the key, right sets it to null: both differ from base.
    r = merge({"a": 1}, {}, {"a": None})
    assert r.status_code == 409
    conflict = r.json()["conflicts"][0]
    assert conflict["path"] == "/a"
    assert conflict["left"] == {"present": False, "value": None}
    assert conflict["right"] == {"present": True, "value": None}


def test_null_change_on_one_side_merges():
    r = merge({"a": 1}, {"a": None}, {"a": 1})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": None}}


def test_arrays_compared_as_whole_one_side_changed():
    r = merge({"a": [1, 2, 3]}, {"a": [1, 2]}, {"a": [1, 2, 3]})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": [1, 2]}}


def test_arrays_compared_as_whole_both_changed():
    r = merge({"a": [1, 2, 3]}, {"a": [1, 2]}, {"a": [1, 2, 3, 4]})
    assert r.status_code == 409
    assert r.json()["conflicts"] == [
        {
            "path": "/a",
            "base": {"present": True, "value": [1, 2, 3]},
            "left": {"present": True, "value": [1, 2]},
            "right": {"present": True, "value": [1, 2, 3, 4]},
        }
    ]


def test_arrays_identical_change_merges():
    r = merge({"a": [1]}, {"a": [2, 2]}, {"a": [2, 2]})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": [2, 2]}}


def test_conflicts_sorted_by_json_pointer():
    r = merge(
        {"b": 1, "a": 1, "c": {"d": 1}},
        {"b": 2, "a": 2, "c": {"d": 2}},
        {"b": 3, "a": 3, "c": {"d": 3}},
    )
    assert r.status_code == 409
    paths = [c["path"] for c in r.json()["conflicts"]]
    assert paths == ["/a", "/b", "/c/d"]


def test_root_level_conflict_has_empty_pointer():
    r = merge(1, 2, 3)
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["path"] == ""


def test_root_level_merge():
    r = merge(1, 2, 1)
    assert r.status_code == 200
    assert r.json() == {"merged": 2}


def test_type_change_on_one_side_merges():
    r = merge({"a": 1}, {"a": {"x": 1}}, {"a": 1})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": {"x": 1}}}


def test_bool_is_not_confused_with_int():
    # left changed 1 -> true, right kept 1: left's change wins.
    r = merge({"a": 1}, {"a": True}, {"a": 1})
    assert r.status_code == 200
    assert r.json() == {"merged": {"a": True}}


def test_missing_request_fields_rejected():
    r = client.post("/merge", json={"base": {}, "left": {}})
    assert r.status_code == 422


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
