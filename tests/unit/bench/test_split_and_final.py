"""Frozen split, manifest tamper detection, and every --final refusal path (ADR-0051)."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bourse.bench.final import (
    FinalRefused,
    GitState,
    final_guard_from_preflight,
    final_preflight,
    write_preflight,
)
from bourse.bench.split import (
    FinalGuard,
    SplitError,
    load_cases,
    manifest_path,
    pin_path,
    read_manifest,
    verify_cases,
    write_manifest,
)
from bourse.contracts.bench import BenchConfig

MODEL = "qwen3.5:27b-q4_K_M"
DIGEST = "sha256:" + "a" * 64


def _cfg(**kw: Any) -> BenchConfig:
    base: dict[str, Any] = {"name": "final-t", "split": "test", "model_digests": {MODEL: DIGEST}}
    return BenchConfig.model_validate(base | kw)


def _clean() -> GitState:
    return GitState("f" * 40, ())


def _dirty() -> GitState:
    return GitState("f" * 40, ("src/x.py",))


def _ps(digest: str = DIGEST, name: str = MODEL) -> Any:
    async def fetch() -> Mapping[str, Any]:
        return {"models": [{"name": name, "digest": digest}]}

    return fetch


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_manifest("test", 6, "t-seed", tmp_path)
    write_manifest("dev", 6, "d-seed", tmp_path)
    return tmp_path


def test_write_and_verify_roundtrip(root: Path) -> None:
    manifest, sha = read_manifest("dev", root)
    assert len(verify_cases(manifest)) == 6
    assert pin_path("dev", root).read_text().strip() == sha


def test_test_manifest_is_written_once(root: Path) -> None:
    with pytest.raises(SplitError, match="written once"):
        write_manifest("test", 6, "other", root)
    with pytest.raises(SplitError, match="written once"):
        write_manifest("dev", 6, "other", root)
    write_manifest("dev", 4, "other", root, overwrite_dev=True)


def test_manifest_tamper_detected_by_pin(root: Path) -> None:
    path = manifest_path("test", root)
    data = json.loads(path.read_text())
    data["cases"] = data["cases"][:-1]
    path.write_text(json.dumps(data, indent=2) + "\n")
    with pytest.raises(SplitError, match="tampered"):
        read_manifest("test", root)


def test_case_drift_detected_even_with_repinned_manifest(root: Path) -> None:
    path = manifest_path("test", root)
    data = json.loads(path.read_text())
    data["cases"][2]["sha256"] = "0" * 64
    path.write_text(json.dumps(data, indent=2) + "\n")
    import hashlib

    pin_path("test", root).write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")
    manifest, _ = read_manifest("test", root)
    with pytest.raises(SplitError, match="sha256"):
        verify_cases(manifest)


def test_test_split_needs_a_final_guard(root: Path) -> None:
    with pytest.raises(SplitError, match="--final"):
        load_cases("test", root=root)
    cases, _ = load_cases("dev", root=root, limit=3)
    assert len(cases) == 3


def test_final_guard_cannot_be_forged() -> None:
    with pytest.raises(SplitError, match="final_preflight"):
        FinalGuard(object(), "x")


async def test_final_refuses_non_test_split(root: Path) -> None:
    with pytest.raises(FinalRefused, match="test split"):
        await final_preflight(
            _cfg(split="dev"), roster=[MODEL], fetch_ps=_ps(), env={}, git=_clean,
            manifest_root=root,
        )  # fmt: skip


async def test_final_refuses_dirty_worktree(root: Path) -> None:
    with pytest.raises(FinalRefused, match="dirty"):
        await final_preflight(
            _cfg(), roster=[MODEL], fetch_ps=_ps(), env={}, git=_dirty, manifest_root=root
        )


async def test_final_refuses_tampered_manifest(root: Path) -> None:
    pin_path("test", root).write_text("0" * 64 + "\n")
    with pytest.raises(FinalRefused, match="tampered"):
        await final_preflight(
            _cfg(), roster=[MODEL], fetch_ps=_ps(), env={}, git=_clean, manifest_root=root
        )


async def test_final_refuses_unpinned_model(root: Path) -> None:
    with pytest.raises(FinalRefused, match="does not pin"):
        await final_preflight(
            _cfg(model_digests={}), roster=[MODEL], fetch_ps=_ps(), env={}, git=_clean,
            manifest_root=root,
        )  # fmt: skip


async def test_final_refuses_model_not_resident(root: Path) -> None:
    with pytest.raises(FinalRefused, match="not resident"):
        await final_preflight(
            _cfg(), roster=[MODEL], fetch_ps=_ps(name="gemma4:12b"), env={}, git=_clean,
            manifest_root=root,
        )  # fmt: skip


async def test_final_refuses_digest_mismatch(root: Path) -> None:
    with pytest.raises(FinalRefused, match="digest"):
        await final_preflight(
            _cfg(), roster=[MODEL], fetch_ps=_ps("sha256:" + "b" * 64), env={}, git=_clean,
            manifest_root=root,
        )  # fmt: skip


async def test_final_ok_records_provenance_and_container_guard(root: Path, tmp_path: Path) -> None:
    cfg = _cfg()
    env = {"OLLAMA_NUM_PARALLEL": "2"}
    guard, prov = await final_preflight(
        cfg, roster=[MODEL], fetch_ps=_ps(), env=env, git=_clean, manifest_root=root
    )
    assert prov.final and not prov.git_dirty and prov.git_sha == "f" * 40
    assert prov.model_digests == {MODEL: DIGEST} and prov.ollama_env == env
    assert prov.manifest_sha256 == guard.manifest_sha256
    cases, _ = load_cases("test", guard=guard, root=root)
    assert len(cases) == 6

    run_dir = tmp_path / "run"
    write_preflight(run_dir, prov)
    g2, _ = await final_guard_from_preflight(
        cfg, run_dir, roster=[MODEL], fetch_ps=_ps(), manifest_root=root
    )
    assert g2.manifest_sha256 == prov.manifest_sha256


async def test_container_guard_refusals(root: Path, tmp_path: Path) -> None:
    cfg = _cfg()
    run_dir = tmp_path / "run"
    with pytest.raises(FinalRefused, match="missing"):
        await final_guard_from_preflight(
            cfg, run_dir, roster=[MODEL], fetch_ps=_ps(), manifest_root=root
        )
    _, prov = await final_preflight(
        cfg, roster=[MODEL], fetch_ps=_ps(), env={}, git=_clean, manifest_root=root
    )
    write_preflight(run_dir, prov)
    with pytest.raises(FinalRefused, match="config"):
        await final_guard_from_preflight(
            _cfg(name="other"), run_dir, roster=[MODEL], fetch_ps=_ps(), manifest_root=root
        )
    with pytest.raises(FinalRefused, match="older"):
        await final_guard_from_preflight(
            cfg, run_dir, roster=[MODEL], fetch_ps=_ps(), manifest_root=root,
            now=lambda: datetime.now(UTC) + timedelta(hours=7),
        )  # fmt: skip
    with pytest.raises(FinalRefused, match="digest"):
        await final_guard_from_preflight(
            cfg, run_dir, roster=[MODEL], fetch_ps=_ps("sha256:" + "c" * 64), manifest_root=root
        )
