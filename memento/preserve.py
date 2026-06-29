"""Artifact bundle preservation helpers for the archive/ tree."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from memento.config import detect_project, slugify
from memento.store import _append_under_heading, _has_heading
from memento.utils import sanitize_secrets

_SENSITIVE_NAME_RE = re.compile(
    r"(?i)(?:^|[._-])(?:env|secret|token|key|credential|passwd|password|pem|p12|pfx)(?:$|[._-])"
)
_METADATA_DIR = ".memento"
_MANIFEST_NAME = "manifest.json"
_INDEX_NAME = "index.md"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _atomic_write_json(path: Path, payload: dict) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _resolve_existing_path(value: str | Path | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value).expanduser()
    try:
        return candidate.resolve(strict=True)
    except OSError:
        return None


def _resolve_source_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve(strict=True)


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path == root or root in path.parents
    except OSError:
        return False


def _detect_git_branch(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "branch", "--show-current"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    branch = result.stdout.strip()
    return branch or None


def _normalize_project_slug(project: str | None, cwd: Path, branch: str | None) -> tuple[str | None, str | None]:
    if project:
        project_path = Path(project).expanduser()
        return slugify(project_path.name) or slugify(str(project_path)) or None, project
    detected_slug, _ticket = detect_project(str(cwd), branch)
    return (detected_slug or None), None


def _pick_bundle_slug(requested: str | None, title: str | None, source_name: str, archive_dir: Path) -> str:
    base = requested or title or source_name or "bundle"
    base_slug = slugify(base) or "bundle"
    slug = base_slug
    suffix = 2
    while (archive_dir / slug).exists():
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    return slug


def _copy_source(source: Path, destination_root: Path, move: bool) -> Path:
    destination_root.mkdir(parents=True, exist_ok=False)
    if source.is_dir():
        destination = destination_root / source.name
        if move:
            shutil.move(str(source), str(destination))
        else:
            shutil.copytree(source, destination)
        return destination

    destination = destination_root / source.name
    if move:
        shutil.move(str(source), str(destination))
    else:
        shutil.copy2(source, destination)
    return destination


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _maybe_sensitive(rel_path: str, path: Path) -> list[str]:
    warnings: list[str] = []
    if _SENSITIVE_NAME_RE.search(path.name) or any(_SENSITIVE_NAME_RE.search(part) for part in path.parts):
        warnings.append(f"filename suggests secrets: {rel_path}")
        return warnings

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return warnings

    if sanitize_secrets(raw) != raw:
        warnings.append(f"content may contain secrets: {rel_path}")
    return warnings


def _collect_files(bundle_root: Path) -> tuple[list[dict], list[str], list[str]]:
    files: list[dict] = []
    warnings: list[str] = []
    sensitive_files: list[str] = []

    for path in sorted(bundle_root.rglob("*"), key=lambda item: str(item.relative_to(bundle_root))):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle_root)
        if rel.parts and rel.parts[0] == _METADATA_DIR:
            continue
        rel_text = str(rel)
        entry = {
            "path": rel_text,
            "sha256": _hash_file(path),
            "size": path.stat().st_size,
        }
        files.append(entry)
        sensitive = _maybe_sensitive(rel_text, path)
        if sensitive:
            sensitive_files.append(rel_text)
            warnings.extend(sensitive)

    return files, warnings, sensitive_files


def _build_index_content(
    *,
    title: str,
    archive_rel: str,
    manifest_rel: str | None,
    source_path: str,
    source_kind: str,
    move: bool,
    file_count: int,
    project_slug: str | None,
    cwd: str | None,
    branch: str | None,
    session_id: str | None,
    description: str | None,
    tags: list[str],
    warnings: list[str],
) -> str:
    lines = [
        f"# {sanitize_secrets(title) or 'Preserved bundle'}",
        "",
        f"Archive: `{archive_rel}`",
        f"Source: `{sanitize_secrets(source_path)}`",
        f"Kind: {source_kind}",
        f"Mode: {'move' if move else 'copy'}",
        f"Files: {file_count}",
    ]
    if project_slug:
        lines.append(f"Project: `{sanitize_secrets(project_slug)}`")
    if cwd:
        lines.append(f"Cwd: `{sanitize_secrets(cwd)}`")
    if branch:
        lines.append(f"Branch: `{sanitize_secrets(branch)}`")
    if session_id:
        lines.append(f"Session: `{sanitize_secrets(session_id)}`")
    if tags:
        lines.append(f"Tags: {', '.join(sanitize_secrets(tag) for tag in tags if tag)}")
    if description:
        lines.append(f"Description: {sanitize_secrets(description)}")
    if manifest_rel:
        lines.append(f"Manifest: `{manifest_rel}`")
    if warnings:
        lines.extend(["", "## Warnings", *[f"- {warning}" for warning in warnings]])
    return "\n".join(lines) + "\n"


def _update_project_index(
    vault: Path,
    project_slug: str,
    bundle_title: str,
    bundle_index_rel: str,
    description: str | None,
) -> Path:
    project_dir = vault / "projects"
    project_dir.mkdir(parents=True, exist_ok=True)
    project_file = project_dir / f"{project_slug}.md"

    if project_file.exists():
        content = project_file.read_text(encoding="utf-8")
    else:
        content = "\n".join(
            [
                "---",
                f"title: {project_slug}",
                f"project: {project_slug}",
                "---",
                "",
                "## Notes",
                "",
                "## Sessions",
                "",
                "## Preserved bundles",
                "",
            ]
        )

    bundle_line = f"- [{sanitize_secrets(bundle_title)}](../{bundle_index_rel})"
    if description:
        bundle_line += f" — {sanitize_secrets(description)}"
    if _has_heading(content, "## Preserved bundles"):
        content = _append_under_heading(content, "## Preserved bundles", bundle_line)
    else:
        content = content.rstrip() + "\n\n## Preserved bundles\n\n" + bundle_line + "\n"

    _atomic_write_text(project_file, content)
    return project_file


def preserve_bundle(
    vault_path: str | Path,
    path: str,
    *,
    title: str | None = None,
    slug: str | None = None,
    project: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    move: bool = False,
    include_manifest: bool = True,
    link_project_index: bool = True,
    cwd: str = "",
    branch: str = "",
    session_id: str = "",
    transport: str = "stdio",
) -> dict:
    """Archive a file or directory bundle under ``archive/``.

    The bundle is copied by default. When ``move`` is true, the source is moved
    instead. A hidden metadata directory stores the bundle manifest and a small
    human-readable index note so the archive stays navigable without forcing the
    source into atomic-note shape.
    """
    vault = Path(vault_path)
    source = _resolve_source_path(path)

    cwd_path = _resolve_existing_path(cwd) or Path.cwd().resolve()
    branch_value = branch or _detect_git_branch(cwd_path)
    project_slug, _ = _normalize_project_slug(project, cwd_path, branch_value)
    source_kind = "directory" if source.is_dir() else "file"

    if transport != "stdio":
        allowed_roots = [cwd_path, vault.resolve()]
        if not any(_is_within(source, root) for root in allowed_roots):
            return {
                "error": "preserve path must be inside the server cwd or vault when using remote transport",
                "reason": "remote_path_rejected",
            }

    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}

    archive_dir = vault / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    bundle_title = (
        sanitize_secrets(title or (source.stem if source.is_file() else source.name))
        or source.name
        or "Preserved bundle"
    )
    bundle_slug = _pick_bundle_slug(slug, bundle_title, source.name or "bundle", archive_dir)
    bundle_root = archive_dir / bundle_slug

    metadata_dir = bundle_root / _METADATA_DIR
    manifest_path = metadata_dir / _MANIFEST_NAME
    index_path = metadata_dir / _INDEX_NAME

    try:
        preserved_root = _copy_source(source, bundle_root, move)
    except (OSError, shutil.Error) as exc:
        return {"error": f"Failed to preserve bundle: {type(exc).__name__}: {exc}"}

    files, warnings, sensitive_files = _collect_files(bundle_root)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    archive_rel = str(bundle_root.relative_to(vault))
    manifest_rel = str(manifest_path.relative_to(vault)) if include_manifest else None
    index_rel = str(index_path.relative_to(vault))

    manifest = {
        "title": bundle_title,
        "slug": bundle_slug,
        "source_path": str(source),
        "source_name": source.name,
        "source_kind": source_kind,
        "archive_path": archive_rel,
        "manifest_path": manifest_rel,
        "index_path": index_rel,
        "move": move,
        "created_at": now,
        "project": project,
        "project_slug": project_slug,
        "cwd": str(cwd_path) if cwd_path else None,
        "branch": branch_value,
        "session_id": session_id or None,
        "description": sanitize_secrets(description) if description else None,
        "tags": [sanitize_secrets(tag) for tag in (tags or []) if tag],
        "files": files,
        "file_count": len(files),
        "sensitive_files": sensitive_files,
        "warnings": sorted(set(warnings)),
        "preserved_root": str(preserved_root.relative_to(bundle_root)),
    }

    if include_manifest:
        _atomic_write_json(manifest_path, manifest)

    index_content = _build_index_content(
        title=bundle_title,
        archive_rel=archive_rel,
        manifest_rel=manifest_rel,
        source_path=str(source),
        source_kind=manifest["source_kind"],
        move=move,
        file_count=len(files),
        project_slug=project_slug,
        cwd=str(cwd_path) if cwd_path else None,
        branch=branch_value,
        session_id=session_id or None,
        description=description,
        tags=tags or [],
        warnings=manifest["warnings"],
    )
    _atomic_write_text(index_path, index_content)

    project_index_path = None
    if link_project_index and project_slug:
        project_index_path = _update_project_index(vault, project_slug, bundle_title, index_rel, description)

    return {
        "path": archive_rel,
        "archive_path": archive_rel,
        "manifest_path": manifest_rel,
        "index_path": index_rel,
        "project_index_path": str(project_index_path.relative_to(vault)) if project_index_path else None,
        "title": bundle_title,
        "slug": bundle_slug,
        "created": True,
        "moved": move,
        "source_path": str(source),
        "source_kind": manifest["source_kind"],
        "file_count": len(files),
        "warnings": manifest["warnings"],
        "sensitive_files": sensitive_files,
        "project": project,
        "project_slug": project_slug,
        "cwd": str(cwd_path) if cwd_path else None,
        "branch": branch_value,
        "session_id": session_id or None,
    }
