"""diff 엔진 — append형 · update형 둘 다."""

from __future__ import annotations

from ingest import differ


def rec(key: str, **watch):
    return {"_key": key, "_watch": dict(watch), "value": key}


def test_added():
    diff = differ.diff_records(
        [rec("a")], [rec("a"), rec("b")], source="s", from_label="d1", to_label="d2"
    )
    assert diff["summary"] == {"added": 1, "removed": 0, "changed": 0}
    assert diff["added"][0]["_key"] == "b"
    assert diff["added"][0]["record"]["value"] == "b"


def test_removed():
    diff = differ.diff_records(
        [rec("a"), rec("b")], [rec("a")], source="s", from_label="d1", to_label="d2"
    )
    assert diff["summary"] == {"added": 0, "removed": 1, "changed": 0}
    assert diff["removed"][0]["_key"] == "b"


def test_changed_uses_watch_fields_only():
    """update형: _watch 안의 값이 바뀌면 changed. 다른 필드는 무시."""
    before = [{"_key": "a", "_watch": {"canceled": False}, "noise": 1}]
    after = [{"_key": "a", "_watch": {"canceled": True}, "noise": 2}]
    diff = differ.diff_records(before, after, source="s", from_label="d1", to_label="d2")

    assert diff["summary"] == {"added": 0, "removed": 0, "changed": 1}
    assert diff["changed"][0] == {
        "_key": "a",
        "field": "canceled",
        "before": False,
        "after": True,
    }


def test_append_only_source_produces_no_changed():
    """_watch 가 비어 있으면 append-only. changed 가 나오지 않습니다."""
    before = [{"_key": "a", "_watch": {}}]
    after = [{"_key": "a", "_watch": {}}, {"_key": "b", "_watch": {}}]
    diff = differ.diff_records(before, after, source="s", from_label="d1", to_label="d2")
    assert diff["summary"] == {"added": 1, "removed": 0, "changed": 0}


def test_multiple_watch_fields_produce_one_entry_each():
    before = [{"_key": "a", "_watch": {"rate": 3.1, "limit": 100}}]
    after = [{"_key": "a", "_watch": {"rate": 3.4, "limit": 200}}]
    diff = differ.diff_records(before, after, source="s", from_label="d1", to_label="d2")
    assert diff["summary"]["changed"] == 2
    assert [c["field"] for c in diff["changed"]] == ["limit", "rate"]


def test_first_run_has_no_previous():
    diff = differ.diff_records(
        None, [rec("a")], source="s", from_label="(none)", to_label="d1"
    )
    assert diff["summary"] == {"added": 1, "removed": 0, "changed": 0}
    assert diff["from"] == "(none)"


def test_records_without_key_are_ignored_by_the_index():
    idx = differ.index_by_key([{"_key": "a"}, {"no_key": 1}])
    assert list(idx) == ["a"]


def test_is_empty():
    same = [rec("a", canceled=False)]
    diff = differ.diff_records(same, same, source="s", from_label="d1", to_label="d2")
    assert differ.is_empty(diff) is True
