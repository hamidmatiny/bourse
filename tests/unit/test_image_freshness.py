"""Stale-image guard for compose stack up (ADR-0050)."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bourse.stack.image_freshness import (
    BuildSpec,
    build_specs,
    copy_sources,
    find_stale,
    newest_input,
    parse_docker_time,
)

DOCKERFILE = """\
FROM ghcr.io/astral-sh/uv:0.11.26 AS uv
FROM python:3.11-slim
RUN apt-get update \\
    && apt-get install -y curl
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY alembic.ini otel-collector.yaml ./
"""
T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _touch(path: Path, at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x")
    os.utime(path, (at.timestamp(), at.timestamp()))


def _context(tmp: Path) -> BuildSpec:
    (tmp / "Dockerfile").write_text(DOCKERFILE)
    (tmp / ".dockerignore").write_text("data/\n**/__pycache__/\n")
    for rel in ("pyproject.toml", "uv.lock", "alembic.ini", "otel-collector.yaml", "src/a.py"):
        _touch(tmp / rel, T0)
    _touch(tmp / "Dockerfile", T0)
    return BuildSpec("bourse:local", tmp.resolve(), (tmp / "Dockerfile").resolve())


def test_copy_sources_skips_stage_copies_and_destination() -> None:
    assert copy_sources(DOCKERFILE) == [
        "pyproject.toml",
        "uv.lock",
        "src",
        "alembic.ini",
        "otel-collector.yaml",
    ]


def test_fresh_image_not_stale(tmp_path: Path) -> None:
    spec = _context(tmp_path)
    assert find_stale([spec], lambda _img: T0 + timedelta(minutes=1)) == []


def test_source_newer_than_image_is_stale(tmp_path: Path) -> None:
    spec = _context(tmp_path)
    _touch(tmp_path / "src" / "pkg" / "b.py", T0 + timedelta(minutes=5))
    stale = find_stale([spec], lambda _img: T0 + timedelta(minutes=1))
    assert len(stale) == 1
    assert stale[0].newest_source == Path("src/pkg/b.py")
    assert "bourse:local" in stale[0].line()


def test_dockerfile_newer_than_image_is_stale(tmp_path: Path) -> None:
    spec = _context(tmp_path)
    _touch(tmp_path / "Dockerfile", T0 + timedelta(minutes=5))
    stale = find_stale([spec], lambda _img: T0 + timedelta(minutes=1))
    assert [s.newest_source for s in stale] == [Path("Dockerfile")]


def test_ignored_and_uncopied_files_do_not_count(tmp_path: Path) -> None:
    spec = _context(tmp_path)
    later = T0 + timedelta(minutes=5)
    _touch(tmp_path / "src" / "__pycache__" / "a.cpython-311.pyc", later)
    _touch(tmp_path / "tests" / "unit" / "test_x.py", later)  # not COPY'd
    _touch(tmp_path / "data" / "logs" / "x.log", later)
    path, _mtime = newest_input(spec) or (None, 0.0)
    assert path is not None and path.name != "a.cpython-311.pyc"
    assert find_stale([spec], lambda _img: T0 + timedelta(minutes=1)) == []


def test_missing_image_is_not_stale(tmp_path: Path) -> None:
    spec = _context(tmp_path)
    _touch(tmp_path / "src" / "a.py", T0 + timedelta(minutes=5))
    assert find_stale([spec], lambda _img: None) == []


def test_build_specs_dedupes_images(tmp_path: Path) -> None:
    doc = {
        "services": {
            "a": {"image": "bourse:local", "build": {"context": ".", "dockerfile": "Dockerfile"}},
            "b": {"image": "bourse:local", "build": {"context": ".", "dockerfile": "Dockerfile"}},
            "pg": {"image": "postgres:16"},
        }
    }
    specs = build_specs(doc, tmp_path)
    assert [s.image for s in specs] == ["bourse:local"]
    assert specs[0].dockerfile == tmp_path.resolve() / "Dockerfile"


def test_parse_docker_time_nanoseconds() -> None:
    got = parse_docker_time("2026-11-05T17:02:59.828441963Z")
    assert got == datetime(2026, 11, 5, 17, 2, 59, 828441, tzinfo=UTC)


def test_real_topology_has_one_built_image() -> None:
    from bourse.stack.topology import compose_document

    specs = build_specs(compose_document(), Path.cwd())
    assert [s.image for s in specs] == ["bourse:local"]
