"""Shared type definitions for memento vault.

Stubs for PR3 — will be used as return type annotations on MCP tools
and as parameter types in store/search once the full typing pass lands.
"""

from typing import TypedDict


class SearchResult(TypedDict, total=False):
    path: str
    title: str
    score: float
    snippet: str


class NoteMetadata(TypedDict, total=False):
    title: str
    note_type: str
    tags: list[str]
    certainty: int | None
    source: str
    date: str
    project: str | None
    branch: str | None
    session_id: str | None
    validity_context: str | None
    supersedes: str | None


class SessionMeta(TypedDict, total=False):
    cwd: str | None
    git_branch: str | None
    exchange_count: int
    user_messages: int
    files_edited: list[str]
    files_read: list[str]
    first_prompt: str | None
    last_outcome: str | None
