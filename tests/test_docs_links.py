"""Link-check README.md and docs/**/*.md: every relative link must resolve.

Scans README.md and every markdown file under docs/ for relative markdown
links (``[text](target)``), skipping external links (http(s)://, mailto:,
etc.) and fenced code blocks. For links with a ``#fragment``, the fragment
must match a GitHub-style heading anchor in the target file (or the source
file, for same-file anchors). Directory targets are accepted as long as the
directory exists, since GitHub renders a directory listing for those.

This is a documentation drift guard for MEM-165's docs/ restructure: it
would have caught every path that moved without its incoming links being
updated.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    return FENCE_RE.sub("", text)


def _github_anchor(heading_text: str) -> str:
    """Approximate GitHub's heading-to-anchor slug algorithm."""

    # Strip inline markdown formatting markers and links' visible text only.
    text = re.sub(r"`([^`]*)`", r"\1", heading_text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def _heading_anchors(text: str) -> set[str]:
    anchors: dict[str, int] = {}
    result = set()
    for match in HEADING_RE.finditer(_strip_code_fences(text)):
        slug = _github_anchor(match.group(2))
        count = anchors.get(slug, 0)
        anchors[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    return result


def _target_markdown_files() -> list[Path]:
    files = [REPO_ROOT / "README.md"]
    docs_dir = REPO_ROOT / "docs"
    files.extend(sorted(docs_dir.rglob("*.md")))
    return [f for f in files if f.exists()]


def _relative_links(md_path: Path) -> list[tuple[int, str]]:
    text = _strip_code_fences(md_path.read_text(encoding="utf-8"))
    links = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in LINK_RE.finditer(line):
            target = match.group(1).strip()
            if not target or target.startswith("<"):
                continue
            scheme_match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target)
            if scheme_match:
                continue  # http(s), mailto, etc. -- not this check's job.
            links.append((lineno, target))
    return links


def _iter_broken_links():
    for md_path in _target_markdown_files():
        rel_source = md_path.relative_to(REPO_ROOT)
        for lineno, target in _relative_links(md_path):
            path_part, _, fragment = target.partition("#")
            if path_part:
                resolved = (md_path.parent / path_part).resolve()
                if not resolved.exists():
                    yield f"{rel_source}:{lineno}: broken link target '{target}' -> {resolved}"
                    continue
                anchor_source = resolved
            else:
                # Same-file anchor.
                anchor_source = md_path

            if fragment and anchor_source.is_file() and anchor_source.suffix == ".md":
                anchors = _heading_anchors(anchor_source.read_text(encoding="utf-8"))
                if fragment not in anchors:
                    yield (
                        f"{rel_source}:{lineno}: anchor '#{fragment}' not found in "
                        f"{anchor_source.relative_to(REPO_ROOT)} (known anchors: {sorted(anchors)})"
                    )


def test_no_broken_relative_links_in_readme_and_docs():
    broken = list(_iter_broken_links())
    assert not broken, "Broken relative links found:\n" + "\n".join(broken)


@pytest.mark.parametrize("md_path", _target_markdown_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_markdown_files_are_readable(md_path: Path):
    assert md_path.read_text(encoding="utf-8") is not None
