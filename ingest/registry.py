"""소스 등록 · 조회."""

from __future__ import annotations

from ingest.base import Source

_REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    """Source 서브클래스를 등록하는 데코레이터."""
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"{cls.__name__} 에 name 이 없습니다.")
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise ValueError(f"소스 이름 중복: {name}")
    _REGISTRY[name] = cls
    return cls


def _load_builtin() -> None:
    """ingest.sources 패키지를 import 해 데코레이터를 발동시킨다."""
    import ingest.sources  # noqa: F401


def names() -> list[str]:
    _load_builtin()
    return sorted(_REGISTRY)


def get(name: str) -> type[Source]:
    _load_builtin()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"등록되지 않은 소스: {name} (등록된 소스: {', '.join(names()) or '없음'})"
        ) from None


def create(name: str, config: dict | None = None) -> Source:
    cls = get(name)
    return cls(config or {})  # type: ignore[call-arg]
