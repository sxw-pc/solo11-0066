"""Resumable, path/SHA-constrained retention purge for one local run.

Two complete strategies share one persisted purge intent:

* ``hard`` (语义A) records the intent and hides the run behind
  ``runs.status = 'purging'`` before touching bytes.  Every artifact must
  resolve inside ``output_root``, must not be a symlink, must still match its
  ledger SHA-256, and must not be referenced by another run.  Managed files are
  unlinked one by one; only after every managed file is gone does one SQLite
  transaction cascade the child rows and run and write a minimal tombstone.
  A crash mid-unlink resumes from the persisted manifest, re-checking the
  current SHA of every remaining file.

* ``quarantine`` (语义B) verifies the same path/SHA constraints, then atomically
  moves the whole run directory to ``output/quarantine/<run_id>/run`` before
  the same SQLite cleanup.  Bytes stay on local disk in an area the Web never
  serves, so an administrator can restore the directory or, after review,
  permanently discard it.

A conflict (path escape, symlink, shared path, hash mismatch, missing or
unverifiable state) stops the whole run cleanup before any byte is deleted and
leaves the intent in ``conflicted``; the Web keeps rejecting the run until the
conflict is resolved, aborted, or (for quarantine) restored.

Nothing here ever deletes the input CSV: the project does not own those files.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drift_attr import store
from drift_attr.errors import DriftError

STRATEGIES = ("hard", "quarantine")
RUN_DIR_KIND = "__run_dir__"
CONFLICT_CODES = {
    "conflict_path": "路径 resolve 后位于 output_root 之外或不在 run 目录内",
    "conflict_symlink": "路径是符号链接，拒绝跟随",
    "conflict_missing": "台账登记的产物在磁盘上不存在",
    "conflict_hash": "磁盘内容的 SHA-256 与台账登记不一致",
    "conflict_shared": "同一规范路径仍被另一个未清理 run 引用",
    "conflict_state": "数据库缺表或无法确认该对象当前状态",
}
_CONFLICT_ALIASES = {
    "path_escape": "conflict_path",
    "outside_run_dir": "conflict_path",
    "symlink": "conflict_symlink",
    "missing": "conflict_missing",
    "hash_mismatch": "conflict_hash",
    "shared_reference": "conflict_shared",
    "unverified": "conflict_state",
}


def _conflict_code(code: str) -> str:
    return _CONFLICT_ALIASES.get(code, code)


def iso_utc(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conflict(
    kind: str | None, code: str, path: str | None = None,
    detail: str | None = None, current_sha: str | None = None,
    recorded_sha: str | None = None,
) -> dict[str, Any]:
    code = _conflict_code(code)
    item: dict[str, Any] = {
        "kind": kind, "code": code,
        "reason": CONFLICT_CODES.get(code, code),
    }
    if path is not None:
        item["path"] = path
    if detail is not None:
        item["detail"] = detail
    if current_sha is not None:
        item["current_sha"] = current_sha
    if recorded_sha is not None:
        item["recorded_sha"] = recorded_sha
    return item


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_ledger_path(value: str, base_dir: Path) -> Path:
    raw = Path(value)
    candidate = raw if raw.is_absolute() else base_dir / raw
    return candidate.resolve()


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _shared_paths(
    db_path: str | Path, run_id: str, base_dir: Path
) -> dict[str, list[str]]:
    """Canonical paths that another ledger run still references."""
    shared: dict[str, list[str]] = {}
    for row in store.artifacts_of_other_runs(db_path, run_id):
        path = Path(row["path"])
        candidates = {str(path.resolve())}
        if not path.is_absolute():
            candidates.add(str((base_dir / path).resolve()))
        for key in candidates:
            shared.setdefault(key, []).append(row["run_id"])
    return shared


def _foreign_artifacts_under(
    shared: dict[str, list[str]], directory: Path
) -> dict[str, list[str]]:
    """Other-run artifacts whose canonical path lives inside ``directory``."""
    inside: dict[str, list[str]] = {}
    for path, runs in shared.items():
        try:
            resolved = Path(path).resolve()
        except OSError:
            continue
        if _inside(resolved, directory):
            inside[path] = runs
    return inside


def _shared_by_other_intents(
    db_path: str | Path, run_id: str
) -> dict[str, list[str]]:
    claimed: dict[str, list[str]] = {}
    for intent in store.list_purge_intents(db_path):
        if intent["run_id"] == run_id:
            continue
        for entry in intent["manifest"]:
            resolved = entry.get("resolved")
            if resolved:
                claimed.setdefault(resolved, []).append(intent["run_id"])
    return claimed


def classify_artifact(
    artifact: dict[str, Any], *, output_root: Path, base_dir: Path,
    shared_paths: dict[str, list[str]], allow_missing: bool,
) -> tuple[str, dict[str, Any]]:
    """Return state ``delete`` / ``already_absent`` / ``conflict`` plus details."""
    kind = artifact["kind"]
    recorded = artifact.get("sha256")
    raw = Path(artifact["path"])
    display = artifact["path"]
    absolute = raw if raw.is_absolute() else base_dir / raw
    if raw.is_symlink() or absolute.is_symlink():
        return "conflict", _conflict(kind, "symlink", display, None, None, recorded)
    try:
        resolved = absolute.resolve()
    except OSError as exc:
        return "conflict", _conflict(kind, "unverified", display, str(exc), None, recorded)
    if not _inside(resolved, output_root):
        return "conflict", _conflict(
            kind, "path_escape", display, f"解析到 {resolved}", None, recorded
        )
    sharing = shared_paths.get(str(resolved), [])
    if sharing:
        return "conflict", _conflict(
            kind, "shared_reference", display,
            "仍被 run " + ", ".join(sharing) + " 引用", None, recorded,
        )
    if not resolved.is_file():
        if allow_missing:
            return "already_absent", {
                "kind": kind, "path": display, "resolved": str(resolved),
                "sha256": recorded, "current_sha": None,
            }
        return "conflict", _conflict(kind, "missing", display, str(resolved), None, recorded)
    try:
        actual = _hash_file(resolved)
    except OSError as exc:
        return "conflict", _conflict(kind, "unverified", display, str(exc), None, recorded)
    if actual != recorded:
        return "conflict", _conflict(
            kind, "hash_mismatch", display,
            "磁盘内容与台账登记 SHA 不同", actual, recorded,
        )
    return "delete", {
        "kind": kind, "path": display, "resolved": str(resolved),
        "sha256": recorded, "current_sha": actual,
    }


def _detect_run_dir(
    run: dict[str, Any], artifacts: list[dict[str, Any]], root: Path, base: Path
) -> Path | None:
    candidates: list[Path] = []
    output_dir = run.get("output_dir")
    if output_dir:
        candidates.append(_resolve_ledger_path(str(output_dir), base) / run["run_id"])
    candidates.append(root / run["run_id"])
    for artifact in artifacts:
        raw = Path(artifact["path"])
        absolute = raw if raw.is_absolute() else base / raw
        candidates.append(absolute.parent)
    named = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.name != run["run_id"] or not _inside(resolved, root) or resolved == root:
            continue
        named.append(resolved)
        if resolved.is_dir() and not resolved.is_symlink():
            return resolved
    for resolved in named:
        return resolved
    return None


def _quarantine_target(root: Path, run_id: str) -> Path:
    return root / "quarantine" / run_id / "run"


def _new_plan(
    db_path: str | Path, run_id: str, run: dict[str, Any], *, strategy: str,
    base_dir: Path, root: Path, reason: str | None,
) -> dict[str, Any]:
    artifacts = store.get_artifacts(db_path, run_id)
    shared = _shared_paths(db_path, run_id, base_dir)
    plan: dict[str, Any] = {
        "run_id": run_id, "strategy": strategy, "resume": False, "state": "new",
        "reason": reason, "base_dir": str(base_dir), "output_root": str(root),
        "prior_status": run["status"], "counts": store.purge_child_counts(db_path, run_id),
        "artifacts": [], "managed": [], "conflicts": [],
    }
    if strategy == "hard":
        for artifact in artifacts:
            state, report = classify_artifact(
                artifact, output_root=root, base_dir=base_dir,
                shared_paths=shared, allow_missing=False,
            )
            if state == "conflict":
                plan["conflicts"].append(report)
            elif state == "delete":
                plan["managed"].append(report)
            plan["artifacts"].append({
                "kind": artifact["kind"], "path": artifact["path"],
                "recorded_sha": artifact["sha256"],
                "current_sha": report.get("current_sha"), "status": state,
            })
        return plan

    run_dir = _detect_run_dir(run, artifacts, root, base_dir)
    target = _quarantine_target(root, run_id)
    plan["quarantine_root"] = str(root / "quarantine")
    plan["quarantine_path"] = str(target)
    if run_dir is None:
        plan["conflicts"].append(_conflict(
            None, "unverified", None,
            "无法从 output_dir 或 artifact 路径确认 run 目录，拒绝整组隔离",
        ))
        return plan
    plan["quarantine_source"] = str(run_dir)
    if run_dir.is_symlink():
        plan["conflicts"].append(_conflict(None, "symlink", str(run_dir)))
        return plan
    if run_dir.exists() and not run_dir.is_dir():
        plan["conflicts"].append(_conflict(None, "unverified", str(run_dir), "不是目录"))
        return plan
    if target.exists():
        plan["conflicts"].append(_conflict(
            None, "unverified", str(target), "隔离目标已存在，拒绝覆盖",
        ))
        return plan
    # Moving the whole directory would also carry away any file another run
    # still references, so that is a conflict even if this run has no artifact
    # row for it.
    for foreign, runs in _foreign_artifacts_under(shared, run_dir).items():
        plan["conflicts"].append(_conflict(
            None, "shared_reference", foreign,
            "仍被 run " + ", ".join(sorted(set(runs))) + " 引用，拒绝整组移动",
        ))
    if plan["conflicts"]:
        return plan
    for artifact in artifacts:
        state, report = classify_artifact(
            artifact, output_root=root, base_dir=base_dir,
            shared_paths=shared, allow_missing=False,
        )
        resolved = report.get("resolved")
        if state == "delete" and resolved and not _inside(Path(resolved), run_dir):
            state, report = "conflict", _conflict(
                artifact["kind"], "outside_run_dir", artifact["path"],
                f"{resolved} 不在 {run_dir} 内", report.get("current_sha"),
                artifact["sha256"],
            )
        if state == "conflict":
            plan["conflicts"].append(report)
        elif state == "delete":
            report["rel"] = Path(report["resolved"]).relative_to(run_dir).as_posix()
            plan["managed"].append(report)
        plan["artifacts"].append({
            "kind": artifact["kind"], "path": artifact["path"],
            "recorded_sha": artifact["sha256"],
            "current_sha": report.get("current_sha"), "status": state,
        })
    return plan


def _resume_plan(
    db_path: str | Path, run_id: str, intent: dict[str, Any], *, strategy: str,
    base_dir: Path, root: Path,
) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "run_id": run_id, "strategy": strategy, "resume": True,
        "state": intent["state"], "reason": intent["reason"],
        "base_dir": intent["base_dir"], "output_root": intent["output_root"],
        "prior_status": intent["prior_status"],
        "counts": store.purge_child_counts(db_path, run_id),
        "artifacts": [], "managed": [], "conflicts": [],
        "quarantine_path": intent.get("quarantine_path"),
        "quarantine_root": intent.get("quarantine_root"),
    }
    shared = _shared_paths(db_path, run_id, Path(plan["base_dir"]))
    shared.update(_shared_by_other_intents(db_path, run_id))
    allow_missing = intent["state"] in {"deleting", "quarantining"}
    if strategy == "hard":
        for entry in intent["manifest"]:
            kind = entry.get("kind")
            recorded = entry.get("sha256")
            display = entry.get("path", "")
            if entry.get("state") == "conflict":
                # A conflict recorded in the intent blocks this attempt even if
                # the file later looks correct: abort and re-run to re-classify.
                plan["conflicts"].append(_conflict(
                    kind, "unverified", display,
                    "该产物在首次清理时已判定冲突；请 --abort 后重新发起清理",
                    None, recorded,
                ))
                plan["artifacts"].append({
                    "kind": kind, "path": display,
                    "recorded_sha": recorded, "current_sha": None, "status": "conflict",
                })
                continue
            if entry.get("resolved") is None:
                plan["conflicts"].append(_conflict(
                    kind, "unverified", display, "意图未记录可校验路径", None, recorded,
                ))
                plan["artifacts"].append({
                    "kind": kind, "path": display,
                    "recorded_sha": recorded, "current_sha": None, "status": "conflict",
                })
                continue
            artifact = {"kind": kind, "path": display, "sha256": recorded}
            state, report = classify_artifact(
                artifact, output_root=Path(plan["output_root"]), base_dir=Path(plan["base_dir"]),
                shared_paths=shared, allow_missing=allow_missing,
            )
            if state == "conflict":
                plan["conflicts"].append(report)
            elif state == "delete":
                plan["managed"].append(report)
            plan["artifacts"].append({
                "kind": kind, "path": display, "recorded_sha": recorded,
                "current_sha": report.get("current_sha"), "status": state,
            })
        return plan

    # quarantine
    target = Path(plan["quarantine_path"]) if plan["quarantine_path"] else None
    if target is None and intent.get("output_root"):
        target = _quarantine_target(Path(intent["output_root"]), run_id)
        plan["quarantine_path"] = str(target)
    source = Path(intent["manifest"][-1]["resolved"]) if (
        intent["manifest"] and intent["manifest"][-1].get("kind") == RUN_DIR_KIND
    ) else None
    plan["quarantine_source"] = str(source) if source else None
    quarantined = bool(target is not None and target.is_dir())
    if not quarantined and source is not None and source.exists():
        for foreign, runs in _foreign_artifacts_under(shared, source).items():
            plan["conflicts"].append(_conflict(
                None, "shared_reference", foreign,
                "仍被 run " + ", ".join(sorted(set(runs))) + " 引用，拒绝整组移动",
            ))
        if plan["conflicts"]:
            return plan
    # Once the directory has been moved there is no source tree left to scan;
    # artifacts are verified inside the quarantine copy by relative path.
    source = None if quarantined else source
    if target is None:
        plan["conflicts"].append(_conflict(None, "unverified", None, "意图缺少隔离路径"))
        return plan
    if not quarantined and (source is None or not source.exists()):
        if target.is_dir():
            quarantined = True
        else:
            raise DriftError(
                "E_PURGE_RESUME",
                "run 目录既不在原位置也不在隔离区，无法确认字节去向",
                path=str(source or target),
            )
    entries = [entry for entry in intent["manifest"] if entry.get("kind") != RUN_DIR_KIND]
    for entry in entries:
        kind = entry.get("kind")
        recorded = entry.get("sha256")
        if quarantined:
            rel = entry.get("rel")
            if not rel:
                plan["conflicts"].append(_conflict(
                    kind, "unverified", entry.get("path"), "意图缺少相对路径", None, recorded,
                ))
                plan["artifacts"].append({
                    "kind": kind, "path": entry.get("path", ""),
                    "recorded_sha": recorded, "current_sha": None, "status": "conflict",
                })
                continue
            candidate = target / rel
            resolved = candidate.resolve()
            if not _inside(resolved, target) or candidate.is_symlink():
                plan["conflicts"].append(_conflict(
                    kind, "path_escape", entry.get("path"), f"解析到 {resolved}", None, recorded,
                ))
                plan["artifacts"].append({
                    "kind": kind, "path": entry.get("path", ""), "recorded_sha": recorded,
                    "current_sha": None, "status": "conflict",
                })
                continue
            if not resolved.is_file():
                plan["conflicts"].append(_conflict(
                    kind, "missing", entry.get("path"), str(resolved), None, recorded,
                ))
                plan["artifacts"].append({
                    "kind": kind, "path": entry.get("path", ""), "recorded_sha": recorded,
                    "current_sha": None, "status": "conflict",
                })
                continue
            actual = _hash_file(resolved)
            if actual != recorded:
                plan["conflicts"].append(_conflict(
                    kind, "hash_mismatch", entry.get("path"),
                    "隔离副本内容与台账登记 SHA 不同", actual, recorded,
                ))
                plan["artifacts"].append({
                    "kind": kind, "path": entry.get("path", ""), "recorded_sha": recorded,
                    "current_sha": actual, "status": "conflict",
                })
                continue
            plan["artifacts"].append({
                "kind": kind, "path": entry.get("path", ""), "recorded_sha": recorded,
                "current_sha": actual, "status": "quarantined",
            })
            plan["managed"].append({
                "kind": kind, "path": entry.get("path", ""), "resolved": str(resolved),
                "sha256": recorded, "current_sha": actual, "rel": rel,
            })
        else:
            artifact = {"kind": kind, "path": entry.get("path", ""), "sha256": recorded}
            state, report = classify_artifact(
                artifact, output_root=Path(plan["output_root"]), base_dir=Path(plan["base_dir"]),
                shared_paths=shared, allow_missing=False,
            )
            if state == "delete" and source is not None and not _inside(
                Path(report["resolved"]), source
            ):
                state, report = "conflict", _conflict(
                    kind, "outside_run_dir", entry.get("path"),
                    f"{report['resolved']} 不在 {source} 内", report.get("current_sha"), recorded,
                )
            if state == "delete":
                report["rel"] = Path(report["resolved"]).relative_to(source).as_posix()
                plan["managed"].append(report)
            else:
                plan["conflicts"].append(report)
            plan["artifacts"].append({
                "kind": kind, "path": entry.get("path", ""), "recorded_sha": recorded,
                "current_sha": report.get("current_sha"), "status": state,
            })
    return plan


def _verify_quarantined(plan: dict[str, Any], target: Path) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    source = plan.get("quarantine_source")
    for item in plan["managed"]:
        rel = item.get("rel")
        if rel is None and source:
            try:
                rel = Path(item["resolved"]).relative_to(source).as_posix()
            except ValueError:
                rel = None
        if not rel:
            conflicts.append(_conflict(
                item["kind"], "unverified", item["path"], "无法定位隔离副本", None,
                item["sha256"],
            ))
            continue
        candidate = target / rel
        if candidate.is_symlink():
            conflicts.append(_conflict(
                item["kind"], "symlink", item["path"], str(candidate), None, item["sha256"],
            ))
            continue
        resolved = candidate.resolve()
        if not _inside(resolved, target):
            conflicts.append(_conflict(
                item["kind"], "path_escape", item["path"], f"解析到 {resolved}", None,
                item["sha256"],
            ))
            continue
        if not resolved.is_file():
            conflicts.append(_conflict(
                item["kind"], "missing", item["path"], str(resolved), None, item["sha256"],
            ))
            continue
        actual = _hash_file(resolved)
        if actual != item["sha256"]:
            conflicts.append(_conflict(
                item["kind"], "hash_mismatch", item["path"],
                "隔离副本内容与台账登记 SHA 不同", actual, item["sha256"],
            ))
    return conflicts


def build_plan(
    db_path: str | Path, run_id: str, *, strategy: str,
    base_dir: str | Path, output_root: str | Path, reason: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute everything a purge would do, without mutating any state."""
    if strategy not in STRATEGIES:
        raise DriftError("E_ARGUMENT", f"未知清理策略: {strategy}")
    store.migrate_purge_schema(db_path)
    database = Path(db_path).resolve()
    base = Path(base_dir).resolve()
    root = Path(output_root).resolve()
    run = store.get_run(database, run_id)
    intent = store.get_purge_intent(database, run_id)
    if run is None:
        tombstone = store.get_tombstone(database, run_id)
        if tombstone is not None and intent is not None and intent["state"] == "quarantined":
            # Byte cleanup is final; keep the intent so the administrator can
            # still choose --restore (before discard) or --discard.
            plan = _resume_plan(
                database, run_id, intent, strategy=strategy, base_dir=base, root=root,
            )
            plan["now"] = iso_utc(now)
            return plan
        if tombstone is not None:
            raise DriftError(
                "E_PURGE_STATE",
                f"run {run_id} 已清理（{tombstone['purged_at']}，策略 {tombstone['strategy']}）",
            )
        raise DriftError(
            "E_PURGE_NOT_FOUND", f"台账中不存在运行: {run_id}", path=str(database)
        )
    if intent is not None:
        if intent["strategy"] != strategy:
            raise DriftError(
                "E_PURGE_CONFLICT",
                f"run {run_id} 已有策略 {intent['strategy']} 的清理意图，不能改为 {strategy}",
            )
        plan = _resume_plan(
            database, run_id, intent, strategy=strategy, base_dir=base, root=root,
        )
    else:
        if run["status"] == "purging":
            raise DriftError(
                "E_PURGE_STATE",
                f"run {run_id} 处于 purging 状态但缺少持久化意图",
                path=str(database),
            )
        plan = _new_plan(
            database, run_id, run, strategy=strategy,
            base_dir=base, root=root, reason=reason,
        )
    plan["now"] = iso_utc(now)
    return plan


def _manifest_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    managed = {item["kind"]: item for item in plan["managed"]}
    base = Path(plan["base_dir"])
    manifest: list[dict[str, Any]] = []
    for item in plan["artifacts"]:
        kind = item["kind"]
        if kind in managed:
            entry = managed[kind]
            rel = entry.get("rel")
            if rel is None:
                rel = _manifest_rel(plan, entry)
            manifest.append({
                "kind": kind, "path": entry["path"], "resolved": entry["resolved"],
                "sha256": entry["sha256"], "state": "move" if plan["strategy"] == "quarantine"
                else "delete", "rel": rel,
            })
        else:
            raw = Path(item["path"])
            try:
                resolved = str((raw if raw.is_absolute() else base / raw).resolve())
            except OSError:
                resolved = None
            manifest.append({
                "kind": kind, "path": item["path"], "resolved": resolved,
                "sha256": item.get("recorded_sha"), "state": "conflict", "rel": None,
            })
    if plan["strategy"] == "quarantine" and plan.get("quarantine_source"):
        manifest.append({
            "kind": RUN_DIR_KIND, "path": plan["quarantine_source"],
            "resolved": plan["quarantine_source"], "sha256": None,
            "state": "move", "rel": None,
        })
    return manifest


def _manifest_rel(plan: dict[str, Any], entry: dict[str, Any]) -> str | None:
    source = plan.get("quarantine_source")
    if not source:
        return None
    resolved = entry.get("resolved")
    if not resolved:
        return None
    try:
        return Path(resolved).relative_to(Path(source)).as_posix()
    except ValueError:
        return None


def describe_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """JSON-serialisable dry-run/commit summary for the CLI and tests."""
    counts = {table: plan["counts"].get(table, 0) for table in store.PURGE_TABLES}
    return {
        "run_id": plan["run_id"],
        "strategy": plan["strategy"],
        "resume": plan["resume"],
        "state": plan["state"],
        "output_root": plan["output_root"],
        "sqlite": {"tables": counts, "rows": sum(counts.values())},
        "artifacts": plan["artifacts"],
        "conflicts": plan["conflicts"],
        "quarantine_path": plan.get("quarantine_path"),
        "retention": {
            "hard": "删除数据库明细与受管产物字节，仅保留最小墓碑",
            "quarantine": "整组目录移入隔离区保留字节，删除数据库明细并保留墓碑",
        }[plan["strategy"]],
    }


def _conflict_result(run_id: str, strategy: str, conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_id": run_id, "strategy": strategy, "state": "conflicted",
        "conflicts": conflicts, "deleted": [], "missing": [],
    }


def _rmdir_if_empty(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def _delete_managed(
    plan: dict[str, Any], on_deleted: Any = None
) -> tuple[list[str], list[dict[str, Any]]]:
    conflicts: list[dict[str, Any]] = []
    deleted: list[str] = []
    for item in plan["managed"]:
        target = Path(item["resolved"])
        if not target.is_file():
            continue
        current = _hash_file(target)
        if current != item["sha256"]:
            conflicts.append(_conflict(
                item["kind"], "hash_mismatch", item["path"],
                "清理中途产物内容已变化，未删除", current, item["sha256"],
            ))
            continue
        target.unlink()
        deleted.append(item["kind"])
        if on_deleted is not None:
            on_deleted(item["kind"])
        _rmdir_if_empty(target.parent)
    if plan.get("quarantine_source"):
        _rmdir_if_empty(Path(plan["quarantine_source"]))
    return deleted, conflicts


def _apply_progress(db_path: str | Path, run_id: str, items: list[str]) -> None:
    try:
        intent = store.get_purge_intent(db_path, run_id)
        progress = list(intent.get("progress", [])) if intent else []
        progress.extend(items)
        store.update_purge(db_path, run_id, progress=progress)
    except DriftError:
        pass


def _move_directory(source: Path, destination: Path) -> None:
    if destination.exists():
        raise DriftError("E_PURGE_STATE", "隔离目标已存在，拒绝覆盖", path=str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.stat().st_dev != source.stat().st_dev:
        raise DriftError(
            "E_PURGE_STATE",
            "隔离区与 run 目录不在同一文件系统，无法原子移动",
            path=str(destination),
        )
    os.replace(source, destination)
    _rmdir_if_empty(source.parent)


def _tombstone_shas(intent: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"kind": item["kind"], "sha256": item.get("sha256")}
        for item in intent["manifest"]
        if item.get("kind") != RUN_DIR_KIND and item.get("state") != "conflict"
        and item.get("sha256")
    ]


def execute(
    db_path: str | Path, run_id: str, *, strategy: str, reason: str,
    base_dir: str | Path, output_root: str | Path, now: datetime | None = None,
) -> dict[str, Any]:
    """Run or resume a purge; idempotent at every stage.

    Returns a result with state ``complete`` or ``conflicted``.  A recorded
    conflict is a normal, inspectable outcome; ``DriftError`` is reserved for
    unrecoverable or unsafe states.
    """
    database = Path(db_path).resolve()
    store.migrate_purge_schema(database)
    existing = store.get_purge_intent(database, run_id)
    if store.get_run(database, run_id) is None:
        tombstone = store.get_tombstone(database, run_id)
        if tombstone is not None and existing is None:
            return {
                "run_id": run_id, "strategy": tombstone["strategy"], "state": "complete",
                "already": True, "conflicts": [], "deleted": [], "missing": [],
                "tombstone": tombstone,
            }
    plan = build_plan(
        database, run_id, strategy=strategy, base_dir=base_dir,
        output_root=output_root, reason=reason, now=now,
    )
    timestamp = plan["now"]
    if plan["resume"]:
        intent = store.get_purge_intent(database, run_id)
        assert intent is not None
    else:
        intent = store.begin_purge(
            database, run_id=run_id, strategy=strategy, reason=reason or "retention",
            prior_status=plan["prior_status"], base_dir=plan["base_dir"],
            output_root=plan["output_root"],
            quarantine_root=plan.get("quarantine_root"),
            manifest=_manifest_from_plan(plan), conflicts=plan["conflicts"],
            now=timestamp,
        )
    if plan["conflicts"]:
        store.update_purge(database, run_id, state="conflicted",
                           conflicts=plan["conflicts"], now=timestamp)
        return _conflict_result(run_id, strategy, plan["conflicts"])

    deleted: list[str] = []
    if strategy == "hard":
        if intent["state"] != "deleting":
            store.update_purge(database, run_id, state="deleting", now=timestamp)
        deleted, conflicts = _delete_managed(
            plan, on_deleted=lambda kind: _apply_progress(
                database, run_id, [kind],
            ),
        )
        if conflicts:
            store.update_purge(database, run_id, state="conflicted",
                               conflicts=conflicts, now=timestamp)
            return _conflict_result(run_id, strategy, conflicts)
        store.finish_purge(
            database, run_id=run_id, purged_at=timestamp, strategy=strategy,
            reason=reason or intent["reason"],
            artifact_shas=_tombstone_shas(intent), conflicts=[],
        )
    else:
        target = Path(plan["quarantine_path"])
        source = Path(plan["quarantine_source"]) if plan.get("quarantine_source") else None
        if intent["state"] == "quarantined":
            # Resumed after the filesystem + SQLite steps already succeeded:
            # only re-verify the quarantined copy and finish idempotently.
            conflicts = _verify_quarantined(plan, target)
            if conflicts:
                store.update_purge(database, run_id, state="conflicted",
                                   conflicts=conflicts, now=timestamp)
                return _conflict_result(run_id, strategy, conflicts)
            return {
                "run_id": run_id, "strategy": strategy, "state": "complete",
                "already": True, "conflicts": [], "deleted": [], "missing": [],
                "quarantine_path": str(target),
                "tombstone": store.get_tombstone(database, run_id),
            }
        if source is not None and source.exists():
            store.update_purge(database, run_id, state="quarantining", now=timestamp)
            _move_directory(source, target)
            deleted = [item["kind"] for item in plan["managed"]]
        store.update_purge(database, run_id, state="quarantined",
                           quarantine_path=str(target), now=timestamp)
        conflicts = _verify_quarantined(plan, target)
        if conflicts:
            store.update_purge(database, run_id, state="conflicted",
                               conflicts=conflicts, now=timestamp)
            return _conflict_result(run_id, strategy, conflicts)
        store.finish_purge(
            database, run_id=run_id, purged_at=timestamp, strategy="quarantine",
            reason=reason or intent["reason"],
            artifact_shas=_tombstone_shas(intent), conflicts=[], keep_intent=True,
        )
    return {
        "run_id": run_id, "strategy": strategy, "state": "complete",
        "conflicts": [], "deleted": deleted, "missing": [],
        "quarantine_path": plan.get("quarantine_path") if strategy == "quarantine" else None,
        "tombstone": store.get_tombstone(database, run_id),
    }


def restore_quarantine(
    db_path: str | Path, run_id: str, *, base_dir: str | Path,
    output_root: str | Path, now: datetime | None = None,
) -> dict[str, Any]:
    """语义B: move a quarantined run directory back and abandon the purge."""
    database = Path(db_path).resolve()
    store.migrate_purge_schema(database)
    intent = store.get_purge_intent(database, run_id)
    if intent is None:
        if store.get_tombstone(database, run_id) is not None:
            raise DriftError(
                "E_PURGE_STATE", f"run {run_id} 已写入墓碑，不能再恢复", path=str(database)
            )
        raise DriftError("E_PURGE_NOT_FOUND", f"不存在清理意图: {run_id}", path=str(database))
    if intent["strategy"] != "quarantine":
        raise DriftError("E_PURGE_STATE", "只有隔离策略的清理可以恢复原目录", path=str(database))
    if intent["state"] not in {"pending", "conflicted", "quarantining", "quarantined"}:
        raise DriftError(
            "E_PURGE_STATE", f"清理状态 {intent['state']} 不能恢复", path=str(database)
        )
    if intent["state"] == "quarantined" and not (
        intent.get("quarantine_path") and Path(intent["quarantine_path"]).is_dir()
    ):
        raise DriftError(
            "E_PURGE_STATE",
            "隔离副本已不存在，无法恢复；请用 --discard 完成墓碑",
            path=str(database),
        )
    run = store.get_run(database, run_id)
    if run is None:
        raise DriftError(
            "E_PURGE_STATE",
            f"run {run_id} 的数据库明细已删除，只能弃置或取证，不能恢复为 run",
            path=str(database),
        )
    root = Path(intent["output_root"])
    destination = root / run_id
    quarantine = Path(intent["quarantine_path"]) if intent.get("quarantine_path") else None
    if quarantine is not None and quarantine.exists():
        if destination.exists():
            raise DriftError(
                "E_PURGE_STATE", "原 run 目录已被占用，拒绝覆盖", path=str(destination)
            )
        if destination.parent.stat().st_dev != quarantine.stat().st_dev:
            raise DriftError("E_PURGE_STATE", "隔离区与原目录不在同一文件系统", path=str(destination))
        os.replace(quarantine, destination)
        _rmdir_if_empty(quarantine.parent)
        _rmdir_if_empty(root / "quarantine" / run_id)
        _rmdir_if_empty(root / "quarantine")
    elif not destination.is_dir():
        raise DriftError(
            "E_PURGE_STATE", "隔离副本与原目录都不存在，无法恢复", path=str(quarantine)
        )
    store.restore_run_after_purge(database, run_id, intent["prior_status"])
    return {
        "run_id": run_id, "state": "restored", "strategy": "quarantine",
        "path": str(destination), "restored_at": iso_utc(now),
    }


def abort_purge(
    db_path: str | Path, run_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Abandon a not-yet-destructive intent and make the run visible again."""
    database = Path(db_path).resolve()
    store.migrate_purge_schema(database)
    intent = store.get_purge_intent(database, run_id)
    if intent is None:
        raise DriftError("E_PURGE_NOT_FOUND", f"不存在清理意图: {run_id}", path=str(database))
    if intent["strategy"] == "hard" and (
        intent["state"] in {"deleting", "quarantining"} or intent.get("progress")
    ):
        raise DriftError(
            "E_PURGE_STATE",
            "硬删除已开始，无法回滚已删除的字节；请用 --commit 续跑完成清理",
            path=str(database),
        )
    if intent["state"] == "quarantined":
        raise DriftError(
            "E_PURGE_STATE", "目录已在隔离区，请使用 --restore 或 --discard", path=str(database)
        )
    store.restore_run_after_purge(database, run_id, intent["prior_status"])
    return {"run_id": run_id, "state": "aborted", "aborted_at": iso_utc(now)}


def discard_quarantine(
    db_path: str | Path, run_id: str, *, base_dir: str | Path,
    output_root: str | Path, now: datetime | None = None,
) -> dict[str, Any]:
    """语义B final step: after review, permanently delete quarantined bytes."""
    database = Path(db_path).resolve()
    store.migrate_purge_schema(database)
    intent = store.get_purge_intent(database, run_id)
    if intent is None:
        if store.get_tombstone(database, run_id) is not None:
            return {
                "run_id": run_id, "state": "complete", "strategy": "quarantine",
                "already": True, "tombstone": store.get_tombstone(database, run_id),
            }
        raise DriftError("E_PURGE_NOT_FOUND", f"不存在清理意图: {run_id}", path=str(database))
    if intent["strategy"] != "quarantine" or intent["state"] not in {
        "quarantined", "conflicted"
    }:
        raise DriftError(
            "E_PURGE_STATE", f"清理状态 {intent['state']} 不能永久丢弃", path=str(database)
        )
    if not intent.get("quarantine_path") or not Path(intent["quarantine_path"]).is_dir():
        raise DriftError("E_PURGE_STATE", "隔离副本不存在，无法永久丢弃", path=str(database))
    quarantine = Path(intent["quarantine_path"])
    _rm_within(quarantine, Path(intent["output_root"]))
    _rmdir_if_empty(quarantine.parent)
    _rmdir_if_empty(Path(intent["output_root"]) / "quarantine")
    timestamp = iso_utc(now)
    store.finish_purge(
        database, run_id=run_id, purged_at=timestamp, strategy="quarantine",
        reason=intent["reason"], artifact_shas=_tombstone_shas(intent),
        conflicts=intent["conflicts"], keep_intent=False,
    )
    return {
        "run_id": run_id, "state": "complete", "strategy": "quarantine",
        "discarded": str(quarantine), "tombstone": store.get_tombstone(database, run_id),
    }


def _rm_within(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise DriftError("E_PURGE_CONFLICT", "拒绝删除符号链接", path=str(path))
    resolved = path.resolve()
    if resolved == root or not _inside(resolved, root):
        raise DriftError("E_PURGE_CONFLICT", "拒绝删除 output_root 之外的路径", path=str(path))
    if resolved.is_dir():
        shutil.rmtree(resolved)
    elif resolved.exists():
        resolved.unlink()


def status(db_path: str | Path, run_id: str) -> dict[str, Any]:
    database = Path(db_path).resolve()
    store.migrate_purge_schema(database)
    intent = store.get_purge_intent(database, run_id)
    tombstone = store.get_tombstone(database, run_id)
    run = store.get_run(database, run_id)
    return {
        "run_id": run_id,
        "run_status": run["status"] if run else None,
        "intent": intent,
        "tombstone": tombstone,
    }
