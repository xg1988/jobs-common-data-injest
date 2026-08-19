"""DB(Supabase PostgREST) 쓰기 경로.

실제 Supabase 를 부르지 않습니다. respx 로 PostgREST 를 흉내 냅니다.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from test_quality import FakeSource, apt, seed_latest  # noqa: F401

from ingest import db, pipeline, storage

BASE = "https://proj.supabase.co"


@pytest.fixture
def db_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", BASE)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-secret")
    monkeypatch.setattr(storage, "load_dotenv", lambda path=None: None)


def test_disabled_without_settings(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setattr(storage, "load_dotenv", lambda path=None: None)
    assert db.enabled() is False


def test_disabled_when_only_url_is_set(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", BASE)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setattr(storage, "load_dotenv", lambda path=None: None)
    assert db.enabled() is False


@respx.mock
def test_upsert_sends_service_role_key_and_merge_header(db_env):
    route = respx.post(f"{BASE}/rest/v1/mkt_apt_trade").mock(
        return_value=httpx.Response(201)
    )

    assert db.upsert("mkt_apt_trade", [{"key": "a"}], on_conflict="key") == 1

    req = route.calls[0].request
    assert req.headers["apikey"] == "service-role-secret"
    assert req.headers["authorization"] == "Bearer service-role-secret"
    assert "resolution=merge-duplicates" in req.headers["prefer"]
    assert req.url.params["on_conflict"] == "key"


@respx.mock
def test_upsert_batches_large_payloads(db_env, monkeypatch):
    monkeypatch.setattr(db, "BATCH_SIZE", 2)
    route = respx.post(f"{BASE}/rest/v1/t").mock(return_value=httpx.Response(201))

    assert db.upsert("t", [{"key": str(i)} for i in range(5)], on_conflict="key") == 5

    assert route.call_count == 3  # 2 + 2 + 1


@respx.mock
def test_upsert_raises_on_error_response(db_env):
    respx.post(f"{BASE}/rest/v1/t").mock(
        return_value=httpx.Response(400, text='{"message":"bad column"}')
    )
    with pytest.raises(db.DbError, match="bad column"):
        db.upsert("t", [{"key": "a"}], on_conflict="key")


@respx.mock
def test_empty_rows_make_no_request(db_env):
    route = respx.post(f"{BASE}/rest/v1/t").mock(return_value=httpx.Response(201))
    assert db.upsert("t", [], on_conflict="key") == 0
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# 파이프라인 연결
# ---------------------------------------------------------------------------


class DbSource(FakeSource):
    db_table = "mkt_thing"
    db_conflict_key = "key"
    db_event_table = "mkt_thing_event"

    def db_rows(self, records, *, collected_at):
        return [
            {"key": r["_key"], "price": r["price_manwon"], "last_seen_at": collected_at}
            for r in records
        ]

    def db_event_rows(self, diff, *, observed_on, observed_at):
        return [
            {"observed_on": observed_on, "event": "added", "key": e["_key"]}
            for e in diff["added"]
        ]


@respx.mock
def test_successful_run_writes_records_events_series_and_state(tmp_storage, db_env):
    routes = {
        name: respx.post(f"{BASE}/rest/v1/{name}").mock(
            return_value=httpx.Response(201)
        )
        for name in (
            "mkt_thing",
            "mkt_thing_event",
            "mkt_series_point",
            "mkt_source_state",
            "mkt_collection_run",
        )
    }

    records = [apt(price_manwon=180000 + i) for i in range(20)]
    result = pipeline.run_source(DbSource(records), run_date="2026-08-19")

    assert result.status == "ok"
    assert result.warnings == []
    for name, route in routes.items():
        assert route.called, f"{name} 에 안 씀"

    sent = routes["mkt_thing"].calls[0].request.read().decode()
    assert '"price": 180000' in sent.replace('"price":180000', '"price": 180000')


@respx.mock
def test_db_failure_does_not_lose_the_files(tmp_storage, db_env):
    """DB 가 죽어도 파일은 남고 run 은 ok 로 끝납니다.

    여기서 예외를 던지면 멀쩡히 받은 데이터까지 버리게 됩니다.
    """
    respx.post(url__startswith=f"{BASE}/rest/v1/").mock(
        return_value=httpx.Response(500, text="db down")
    )

    records = [apt(price_manwon=180000 + i) for i in range(20)]
    result = pipeline.run_source(DbSource(records), run_date="2026-08-19")

    assert result.status == "ok"
    assert any("DB 쓰기 실패" in w for w in result.warnings)
    assert storage.read_latest("fake")["record_count"] == 20  # 파일은 그대로


@respx.mock
def test_failed_run_still_reports_state_to_db(tmp_storage, db_env):
    """실패도 DB 에 남아야 소비자가 status 로 판단할 수 있습니다."""
    route = respx.post(f"{BASE}/rest/v1/mkt_source_state").mock(
        return_value=httpx.Response(201)
    )

    result = pipeline.run_source(DbSource([], fail=True), run_date="2026-08-19")

    assert result.status == "failed"
    assert route.called
    body = route.calls[0].request.read().decode()
    assert '"failed"' in body


@respx.mock
def test_sync_pushes_saved_files_without_calling_the_api(tmp_storage, db_env):
    records = [apt(price_manwon=180000 + i) for i in range(20)]
    pipeline.run_source(DbSource(records), run_date="2026-08-19")

    respx.reset()
    routes = {
        name: respx.post(f"{BASE}/rest/v1/{name}").mock(
            return_value=httpx.Response(201)
        )
        for name in ("mkt_thing", "mkt_series_point", "mkt_source_state")
    }

    written = pipeline.sync_to_db(DbSource([]))

    assert written == {"records": 20, "series": 1, "state": 1}
    for route in routes.values():
        assert route.called
