"""`bourse bench run --final` preflight and provenance (ADR-0011, ADR-0051).

Refuses unless (1) the git worktree is clean, (2) the test manifest matches its pinned sha256
and every case regenerates to its manifest sha256, (3) every roster model is resident in Ollama
`/api/ps` with the digest pinned in the bench config. Each check is injectable for tests.
"""

import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bourse.bench.generator import GENERATOR_VERSION
from bourse.bench.split import MANIFEST_DIR, FinalGuard, SplitError, read_manifest, verify_cases
from bourse.contracts.bench import BenchConfig, Provenance
from bourse.grading.graders import GRADER_VERSION

OLLAMA_ENV_KEYS = (
    "OLLAMA_NUM_PARALLEL",
    "OLLAMA_MAX_LOADED_MODELS",
    "OLLAMA_KEEP_ALIVE",
    "OLLAMA_CONTEXT_LENGTH",
    "OLLAMA_FLASH_ATTENTION",
    "OLLAMA_KV_CACHE_TYPE",
)
LOCK_DIR = Path("data/bench")


class FinalRefused(Exception):
    """A --final precondition failed; the message names it."""


@dataclass(frozen=True)
class GitState:
    sha: str
    dirty_paths: tuple[str, ...]

    @property
    def dirty(self) -> bool:
        return bool(self.dirty_paths)


def git_state(cwd: Path | None = None) -> GitState:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout
    return GitState(sha, tuple(line[3:] for line in porcelain.splitlines() if line.strip()))


def resident_digests(ps_json: Mapping[str, Any]) -> dict[str, str]:
    """Ollama /api/ps body -> {model tag: digest}."""
    out: dict[str, str] = {}
    for m in ps_json.get("models") or []:
        name = str(m.get("name") or m.get("model") or "")
        digest = str(m.get("digest") or "")
        if name:
            out[name] = digest
    return out


def check_digests(
    roster: Iterable[str], pinned: Mapping[str, str], resident: Mapping[str, str]
) -> dict[str, str]:
    """Every roster model must be pinned in the config and resident with that digest."""
    out: dict[str, str] = {}
    for model in sorted(roster):
        want = pinned.get(model)
        if not want:
            raise FinalRefused(f"model_digests in the bench config does not pin {model}")
        got = resident.get(model)
        if got is None:
            raise FinalRefused(f"{model} is not resident in Ollama /api/ps (warm it first)")
        if got != want:
            raise FinalRefused(f"{model} digest {got} != pinned {want}")
        out[model] = got
    return out


def ollama_env(ollama_base_url: str, num_parallel: int) -> dict[str, str]:
    env = {k: os.environ[k] for k in OLLAMA_ENV_KEYS if k in os.environ}
    env["settings.ollama_base_url"] = ollama_base_url
    env["settings.ollama_num_parallel"] = str(num_parallel)
    return env


async def final_preflight(
    config: BenchConfig,
    *,
    roster: Iterable[str],
    fetch_ps: Callable[[], Awaitable[Mapping[str, Any]]],
    env: Mapping[str, str],
    git: Callable[[], GitState] = git_state,
    manifest_root: Path = MANIFEST_DIR,
) -> tuple[FinalGuard, Provenance]:
    if config.split != "test":
        raise FinalRefused("--final runs the test split only (config split must be 'test')")
    state = git()
    if state.dirty:
        shown = ", ".join(state.dirty_paths[:5])
        raise FinalRefused(f"git worktree is dirty ({len(state.dirty_paths)} paths: {shown})")
    try:
        manifest, sha = read_manifest("test", manifest_root)
        verify_cases(manifest)
    except SplitError as exc:
        raise FinalRefused(str(exc)) from exc
    digests = check_digests(roster, config.model_digests, resident_digests(await fetch_ps()))
    guard = FinalGuard(FinalGuard._token, sha)
    provenance = Provenance(
        final=True,
        git_sha=state.sha,
        git_dirty=False,
        manifest_sha256=sha,
        model_digests=digests,
        ollama_env=dict(env),
        generator_version=GENERATOR_VERSION,
        grader_version=GRADER_VERSION,
        config_sha256=config.sha256,
        at=datetime.now(UTC),
    )
    return guard, provenance


def dev_provenance(
    config: BenchConfig,
    *,
    manifest_sha256: str | None,
    env: Mapping[str, str],
    digests: Mapping[str, str],
    git: Callable[[], GitState] = git_state,
) -> Provenance:
    """Provenance of a non-final run: recorded, never enforced."""
    try:
        state = git()
        sha, dirty = state.sha, state.dirty
    except (OSError, subprocess.CalledProcessError):
        sha, dirty = "unknown", True
    return Provenance(
        final=False,
        git_sha=sha,
        git_dirty=dirty,
        manifest_sha256=manifest_sha256,
        model_digests=dict(digests),
        ollama_env=dict(env),
        generator_version=GENERATOR_VERSION,
        grader_version=GRADER_VERSION,
        config_sha256=config.sha256,
        at=datetime.now(UTC),
    )


def write_lock(run_dir: Path, provenance: Provenance) -> Path:
    """ADR-0011 lock file: commit sha and dirty flag (plus the rest of the provenance)."""
    path = run_dir / "final.lock.json"
    path.write_text(json.dumps(provenance.model_dump(mode="json"), indent=2) + "\n")
    return path


PREFLIGHT_FILE = "final.preflight.json"
PREFLIGHT_MAX_AGE_S = 6 * 3600.0


def write_preflight(run_dir: Path, provenance: Provenance) -> Path:
    """Host side: the git check needs .git, which the image does not contain."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / PREFLIGHT_FILE
    path.write_text(json.dumps(provenance.model_dump(mode="json"), indent=2) + "\n")
    return path


async def final_guard_from_preflight(
    config: BenchConfig,
    run_dir: Path,
    *,
    roster: Iterable[str],
    fetch_ps: Callable[[], Awaitable[Mapping[str, Any]]],
    manifest_root: Path = MANIFEST_DIR,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[FinalGuard, Provenance]:
    """Container side of --final: the host preflight must exist for this exact config, and the
    manifest and model digests are re-verified here against what the container sees."""
    path = run_dir / PREFLIGHT_FILE
    if not path.exists():
        raise FinalRefused(
            f"{path} missing: run `uv run bourse bench preflight --config ...` on the host first"
        )
    pre = Provenance.model_validate_json(path.read_text())
    if not pre.final or pre.git_dirty:
        raise FinalRefused(f"{path}: preflight is not a clean final preflight")
    if pre.config_sha256 != config.sha256:
        raise FinalRefused(f"{path}: config sha256 differs from the preflight")
    if (now() - pre.at).total_seconds() > PREFLIGHT_MAX_AGE_S:
        raise FinalRefused(f"{path}: preflight older than {PREFLIGHT_MAX_AGE_S / 3600:.0f} h")
    try:
        manifest, sha = read_manifest("test", manifest_root)
        verify_cases(manifest)
    except SplitError as exc:
        raise FinalRefused(str(exc)) from exc
    if sha != pre.manifest_sha256:
        raise FinalRefused("test manifest in this image differs from the host preflight")
    digests = check_digests(roster, config.model_digests, resident_digests(await fetch_ps()))
    if digests != pre.model_digests:
        raise FinalRefused("model digests changed since the host preflight")
    return FinalGuard(FinalGuard._token, sha), pre
