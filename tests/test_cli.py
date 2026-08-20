"""CLI 가 임포트되고 모든 명령이 파서에 붙어 있는지.

이 파일이 없어서 `__main__.py` 의 문법 오류가 테스트를 전부 통과한 채
서버까지 갔습니다. 테스트가 한 번도 임포트하지 않는 모듈은 없는 것과
같습니다.
"""

from __future__ import annotations

import pytest

from ingest import __main__ as cli

COMMANDS = [
    "run", "backfill", "sync", "validate", "diff",
    "capture", "archive", "restore", "query", "list",
]


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_parses(command):
    parser = cli.build_parser()
    required = {
        "run": [], "list": [],
        "sync": [], "validate": ["--source", "x"],
        "capture": ["--source", "x"],
        "backfill": ["--source", "x", "--from", "2025-01", "--to", "2025-02"],
        "diff": ["--source", "x", "--from", "2025-01-01", "--to", "2025-01-02"],
        "restore": ["--source", "x", "--from", "2025-01", "--to", "2025-02"],
        "query": ["--source", "x", "--from", "2025-01", "--to", "2025-02"],
        "archive": [],
    }[command]

    args = parser.parse_args([command, *required])

    assert callable(args.func)


def test_archive_does_not_evict_unless_asked():
    """--evict 를 안 쓰면 절대 지우지 않아야 합니다. 기본값이 안전해야 합니다."""
    args = cli.build_parser().parse_args(["archive", "--source", "x"])

    assert args.evict is False
    assert args.hot_months == 12


def test_backfill_does_not_archive_unless_asked():
    args = cli.build_parser().parse_args(
        ["backfill", "--source", "x", "--from", "2025-01", "--to", "2025-02"]
    )

    assert args.archive is False


def test_query_excludes_canceled_by_default():
    """취소 거래가 기본으로 섞이면 취소된 신고가가 역대 최고가로 잡힙니다."""
    args = cli.build_parser().parse_args(
        ["query", "--source", "x", "--from", "2025-01", "--to", "2025-02"]
    )

    assert args.include_canceled is False


def test_months_between_rejects_a_backwards_range():
    with pytest.raises(SystemExit):
        cli._months_between("2025-06", "2025-01")


def test_months_between_crosses_the_year():
    assert cli._months_between("2025-11", "2026-02") == [
        "2025-11", "2025-12", "2026-01", "2026-02",
    ]
