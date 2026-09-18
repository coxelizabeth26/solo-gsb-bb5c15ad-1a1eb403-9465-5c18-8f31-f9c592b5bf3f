import hashlib
import json

from fastapi.testclient import TestClient

from app.canonicalize import canonicalize
from app.main import app

client = TestClient(app)


def canonicalize_via_api(document):
    return client.post("/canonicalize", json={"document": document})


def post_raw(body):
    return client.post(
        "/canonicalize", content=body, headers={"Content-Type": "application/json"}
    )


# --------------------------------------------------------- basic structure


def test_no_whitespace_and_colon_comma_form():
    r = canonicalize_via_api({"b": 1, "a": 2})
    assert r.status_code == 200
    assert r.json()["canonical"] == '{"a":2,"b":1}'


def test_empty_containers_and_scalars():
    assert canonicalize({}) == "{}"
    assert canonicalize([]) == "[]"
    assert canonicalize(None) == "null"
    assert canonicalize(True) == "true"
    assert canonicalize(False) == "false"
    assert canonicalize("x") == '"x"'
    assert canonicalize(1) == "1"


def test_array_order_is_preserved():
    assert canonicalize([3, 1, 2, {"z": 1, "a": 2}]) == '[3,1,2,{"a":2,"z":1}]'


def test_objects_sorted_recursively_inside_arrays_and_objects():
    doc = {"b": [{"d": 1, "c": 2}], "a": {"f": 3, "e": 4}}
    assert canonicalize(doc) == '{"a":{"e":4,"f":3},"b":[{"c":2,"d":1}]}'


def test_root_may_be_any_json_value():
    for value, expected in [
        ([1, 2], "[1,2]"),
        ("hi", '"hi"'),
        (42, "42"),
        (True, "true"),
        (False, "false"),
        (None, "null"),
    ]:
        r = canonicalize_via_api(value)
        assert r.status_code == 200
        assert r.json()["canonical"] == expected


def test_response_shape():
    r = canonicalize_via_api({"a": 1})
    body = r.json()
    assert set(body) == {"canonical", "sha256"}
    assert body["canonical"] == '{"a":1}'
    assert body["sha256"] == hashlib.sha256(b'{"a":1}').hexdigest()


def test_sha256_is_of_utf8_canonical_bytes():
    doc = {"€": "日本語"}
    canonical = canonicalize(doc)
    r = canonicalize_via_api(doc)
    assert r.json()["sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ----------------------------------------------------------- determinism


def test_same_document_byte_identical_every_time():
    docs = [
        {"z": [1, {"y": 2, "x": [True, None, False]}], "a": "€\n"},
        {"1": 1, "": 0, "a": 2, "aa": 3, "ab": 4},
        list(range(50)),
    ]
    for doc in docs:
        results = {canonicalize(json.loads(json.dumps(doc))) for _ in range(10)}
        assert len(results) == 1
        r1 = canonicalize_via_api(doc)
        r2 = canonicalize_via_api(doc)
        assert r1.json() == r2.json()


def test_input_key_order_does_not_matter():
    a = canonicalize_via_api({"a": 1, "b": 2, "c": 3}).json()
    b = canonicalize_via_api({"c": 3, "b": 2, "a": 1}).json()
    assert a == b
    assert a["canonical"] == '{"a":1,"b":2,"c":3}'


# ------------------------------------------------- RFC 8785 sample document


def test_rfc_sample_document():
    # RFC 8785 Section 3.2.2/3.2.3 sample.
    doc = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
        "string": "€$\nA'B\"\\\\\"/",
        "literals": [None, True, False],
    }
    assert canonicalize(doc) == (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )


# ----------------------------------------------------- number serialization


def test_minus_zero_serializes_as_zero():
    assert canonicalize(-0.0) == "0"
    assert canonicalize([-0.0, 0.0]) == "[0,0]"


def test_integer_like_doubles_have_no_dot_zero():
    assert canonicalize(1.0) == "1"
    assert canonicalize(100.0) == "100"
    assert canonicalize(9007199254740992) == "9007199254740992"


def test_exponent_thresholds_match_ecmascript():
    assert canonicalize(1e-6) == "0.000001"
    assert canonicalize(9.999999999999997e-7) == "9.999999999999997e-7"
    assert canonicalize(1e-7) == "1e-7"
    assert canonicalize(0.002) == "0.002"
    assert canonicalize(1e20) == "100000000000000000000"
    assert canonicalize(1e21) == "1e+21"
    assert canonicalize(1e22) == "1e+22"
    assert canonicalize(1e30) == "1e+30"
    assert canonicalize(1e-27) == "1e-27"


def test_exponent_is_not_zero_padded():
    assert canonicalize(5e-324) == "5e-324"
    assert canonicalize(-5e-324) == "-5e-324"
    assert canonicalize(1.7976931348623157e308) == "1.7976931348623157e+308"


def test_fixed_form_around_unit():
    assert canonicalize(0.1) == "0.1"
    assert canonicalize(10.5) == "10.5"
    assert canonicalize(-0.0000033333333333333333) == "-0.0000033333333333333333"
    assert canonicalize(1424953923781206.25) == "1424953923781206.2"
    assert canonicalize(333333333.33333325) == "333333333.33333325"


def test_large_integers_round_to_doubles_like_ecmascript():
    # 2**68, serialized fixed per the threshold, matches RFC Appendix B.
    assert canonicalize(295147905179352825856) == "295147905179352830000"
    assert canonicalize(999999999999999700000) == "999999999999999700000"
    assert canonicalize(1234567890123456789) == "1234567890123456800"


def test_big_integer_beyond_double_range_is_422():
    r = canonicalize_via_api(10**400)
    assert r.status_code == 422


def test_negative_numbers_and_signs():
    assert canonicalize(-1) == "-1"
    assert canonicalize(-1.5) == "-1.5"
    assert canonicalize(-1e21) == "-1e+21"


# -------------------------------------------------------- string escaping


def test_only_required_string_escapes():
    # / is not escaped; single quote is not escaped.
    assert canonicalize("a/b'c") == '"a/b\'c"'
    assert canonicalize("back\\slash") == '"back\\\\slash"'
    assert canonicalize('q"uote') == '"q\\"uote"'


def test_control_characters_use_short_escapes():
    assert canonicalize("\b\t\n\f\r") == '"\\b\\t\\n\\f\\r"'


def test_other_control_characters_use_lowercase_uhhhh():
    assert canonicalize("\x00\x01\x1f") == '"\\u0000\\u0001\\u001f"'
    assert canonicalize("\x0f") == '"\\u000f"'


def test_non_ascii_emitted_as_utf8_not_escaped():
    assert canonicalize("€") == '"€"'
    assert canonicalize("😀") == '"😀"'
    assert canonicalize("日本語") == '"日本語"'


# ----------------------------------------------------------- key ordering


def test_keys_sorted_by_utf16_code_units_rfc_example():
    # RFC 8785 Section 3.2.3 sorting test data. Names sort by UTF-16
    # code units, which differs from Unicode code-point order for
    # astral characters like the emoji (surrogate pair 0xD83D 0xDE00).
    doc = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "😀": "Emoji: Grinning Face",
        "": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    assert list(json.loads(canonicalize(doc)).keys()) == [
        "\r",
        "1",
        "",
        "ö",
        "€",
        "😀",
        "דּ",
    ]


def test_key_prefix_shorter_sorts_first():
    assert list(json.loads(canonicalize({"ab": 1, "aa": 2, "a": 3, "": 0}))) == [
        "",
        "a",
        "aa",
        "ab",
    ]


def test_astral_plane_keys_sort_by_surrogate_units():
    # U+1F600 encodes to UTF-16 units D83D DE00, hence sorts after the
    # BMP character U+FB33 would -- but here check it sorts before a
    # hypothetical U+E000? Use two astral keys to verify pair-wise.
    keys = ["\U0001f600", "\U0001f4a9", "a"]
    doc = dict.fromkeys(keys, 1)
    ordered = list(json.loads(canonicalize(doc)).keys())
    assert ordered == ["a", "\U0001f4a9", "\U0001f600"]


def test_object_key_is_itself_escaped():
    doc = {"a\nb": 1, "a": 2}
    assert canonicalize(doc) == '{"a":2,"a\\nb":1}'


# ----------------------------------------------------------- JSON semantics


def test_bool_not_confused_with_numbers():
    assert canonicalize([True, False, 1, 0, None]) == "[true,false,1,0,null]"
    assert canonicalize({"true": True}) == '{"true":true}'


def test_null_literal():
    assert canonicalize({"x": None}) == '{"x":null}'


# --------------------------------------------------------------- 422 cases


def test_missing_document_field_is_422():
    r = client.post("/canonicalize", json={"not_document": 1})
    assert r.status_code == 422


def test_empty_body_is_422():
    r = post_raw(b"")
    assert r.status_code == 422


def test_unparseable_json_is_422():
    r = post_raw(b'{"document": }')
    assert r.status_code == 422


def test_trailing_garbage_is_422():
    r = post_raw(b'{"document": 1} junk')
    assert r.status_code == 422


def test_wrong_content_type_body_is_422():
    r = client.post("/canonicalize", content="not json", headers={})
    assert r.status_code == 422


def test_422_error_body_shape_for_unnormalizable_number():
    r = canonicalize_via_api(10**400)
    body = r.json()
    assert body["detail"] == "document cannot be canonicalized"
    assert "reason" in body


# ------------------------------------------------- other endpoints intact


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


def test_patch_still_works():
    r = client.post(
        "/patch",
        json={
            "document": {"a": [1]},
            "operations": [{"op": "add", "path": "/a/-", "value": 2}],
        },
    )
    assert r.status_code == 200
    assert r.json() == {"document": {"a": [1, 2]}}
