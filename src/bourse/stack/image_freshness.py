"""Stale-image guard for `bourse stack up --mode compose` (ADR-0050).

`docker compose up` reuses an existing `bourse:local` image even when src/ changed since the
build, so a live run can exercise old code. Before compose up we compare each built image's
creation time with the newest mtime of the files its Dockerfile copies (plus the Dockerfile
itself). Any input newer than the image -> refuse with `run docker compose build`.

A missing image is not stale: compose builds it on `up`. mtime is conservative: a checkout
that touches files without changing them also asks for a rebuild.
"""

from __future__ import annotations

import fnmatch
import shlex
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REBUILD_HINT = "run `docker compose build`"
ALWAYS_IGNORED = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git")


@dataclass(frozen=True)
class BuildSpec:
    image: str
    context: Path
    dockerfile: Path


@dataclass(frozen=True)
class StaleImage:
    image: str
    image_created: datetime
    newest_source: Path
    source_mtime: datetime

    def line(self) -> str:
        return (
            f"image {self.image} built {self.image_created.isoformat(timespec='seconds')} "
            f"is older than {self.newest_source} "
            f"(modified {self.source_mtime.isoformat(timespec='seconds')})"
        )


def build_specs(document: Mapping[str, Any], root: Path) -> list[BuildSpec]:
    """One BuildSpec per distinct image tag that compose builds (services with `build:`)."""
    seen: dict[str, BuildSpec] = {}
    for svc in document.get("services", {}).values():
        build = svc.get("build")
        image = svc.get("image")
        if not build or not image or image in seen:
            continue
        if isinstance(build, str):
            context, dockerfile = build, "Dockerfile"
        else:
            context = str(build.get("context", "."))
            dockerfile = str(build.get("dockerfile", "Dockerfile"))
        ctx = (root / context).resolve()
        seen[image] = BuildSpec(image=str(image), context=ctx, dockerfile=ctx / dockerfile)
    return list(seen.values())


def copy_sources(dockerfile_text: str) -> list[str]:
    """Context-relative sources of COPY/ADD lines (stage copies `--from=` excluded)."""
    out: list[str] = []
    for raw in dockerfile_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.split(maxsplit=1)[0].upper() not in ("COPY", "ADD"):
            continue
        parts = shlex.split(line)
        args = parts[1:]
        if any(a.startswith("--from") for a in args):
            continue
        args = [a for a in args if not a.startswith("--")]
        out.extend(args[:-1])  # last arg is the destination
    return out


def _ignored(rel: str, patterns: Iterable[str]) -> bool:
    parts = rel.split("/")
    if any(p in ALWAYS_IGNORED for p in parts):
        return True
    for pat in patterns:
        pat = pat.strip().rstrip("/")
        if not pat or pat.startswith("#"):
            continue
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.removeprefix("**/")):
            return True
        if any(fnmatch.fnmatch(p, pat.removeprefix("**/")) for p in parts):
            return True
        if rel.startswith(pat + "/"):
            return True
    return False


def newest_input(spec: BuildSpec) -> tuple[Path, float] | None:
    """(path, mtime) of the newest file the image is built from, or None if none exist."""
    ignore_file = spec.context / ".dockerignore"
    patterns = ignore_file.read_text().splitlines() if ignore_file.exists() else []
    candidates: list[Path] = []
    if spec.dockerfile.exists():
        candidates.append(spec.dockerfile)
        for src in copy_sources(spec.dockerfile.read_text()):
            path = spec.context / src
            if path.is_dir():
                candidates.extend(p for p in path.rglob("*") if p.is_file())
            elif path.is_file():
                candidates.append(path)
    best: tuple[Path, float] | None = None
    for path in candidates:
        rel = path.relative_to(spec.context).as_posix()
        if path != spec.dockerfile and _ignored(rel, patterns):
            continue
        mtime = path.stat().st_mtime
        if best is None or mtime > best[1]:
            best = (path, mtime)
    return best


def find_stale(
    specs: Iterable[BuildSpec],
    image_created: Callable[[str], datetime | None],
    newest: Callable[[BuildSpec], tuple[Path, float] | None] = newest_input,
) -> list[StaleImage]:
    """Images whose newest input file is newer than the image. Missing images are skipped."""
    stale: list[StaleImage] = []
    for spec in specs:
        created = image_created(spec.image)
        if created is None:
            continue
        found = newest(spec)
        if found is None:
            continue
        path, mtime = found
        source_at = datetime.fromtimestamp(mtime, tz=UTC)
        if source_at > created:
            rel = path.relative_to(spec.context) if path.is_relative_to(spec.context) else path
            stale.append(StaleImage(spec.image, created, rel, source_at))
    return stale


def docker_image_created(image: str) -> datetime | None:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Created}}", image],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return parse_docker_time(result.stdout.strip())


def parse_docker_time(raw: str) -> datetime:
    """Docker prints RFC3339 with nanoseconds (`2026-11-05T17:02:59.828441963Z`)."""
    text = raw.strip().replace("Z", "+00:00")
    if "." in text:
        head, rest = text.split(".", 1)
        frac, sep, tz = rest.partition("+") if "+" in rest else rest.partition("-")
        text = f"{head}.{frac[:6]}{sep}{tz}"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def stale_compose_images(root: Path | None = None) -> list[StaleImage]:
    from bourse.stack.topology import compose_document

    base = root or Path.cwd()
    return find_stale(build_specs(compose_document(), base), docker_image_created)
