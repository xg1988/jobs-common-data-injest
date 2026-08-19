from __future__ import annotations

from pathlib import Path

import pytest

from ingest import storage

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """data/ 와 meta.json 을 임시 디렉터리로 돌린다.

    config/ 는 저장소의 실제 파일을 그대로 씁니다 (읽기만 하므로).
    """
    monkeypatch.setattr(storage, "ROOT", tmp_path)
    monkeypatch.setattr(storage, "DATA", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return tmp_path
