"""Characterization tests for the shared frontmatter parser (MEM-166).

Fixture corpus covers: inline tags, block-style tags (the known bug this
ticket fixes -- legacy parsers only understood inline `[a, b]`), no
frontmatter, body-only `---`, unknown keys, and every schema field shape
(scalar, int, list). These fixtures are reused by the consumer migration
tests (test_tenet_graph.py, test_query.py, test_contradictions.py) so each
consumer's before/after behavior is asserted against the same corpus.
"""

from __future__ import annotations

from datetime import datetime, timezone

import memento.frontmatter as fm

# --- Fixture corpus ----------------------------------------------------------

INLINE_TAGS = """---
title: Inline tags note
type: discovery
tags: [alpha, beta, "quoted tag"]
certainty: 4
---

Body with inline tags.
"""

BLOCK_TAGS = """---
title: Block tags note
type: discovery
tags:
  - alpha
  - beta
  - quoted tag
certainty: 4
---

Body with block-style tags.
"""

NO_FRONTMATTER = """Just a body, no frontmatter block at all.

Some more text.
"""

BODY_DASHES = """---
title: Body has dashes
type: session
---

Body text.

---

More body text after a body-level dash fence.
"""

UNKNOWN_KEYS = """---
title: Unknown keys note
type: discovery
hand-added-key: some value
another_custom: 42
---

Body text.
"""

ALL_SCHEMA_FIELDS = """---
title: Full schema shape
type: decision
tags: [caching, redis]
source: session
origin: manual-note
certainty: 5
validity-context: only for staging
supersedes: "[[older-note]]"
synthesized_from:
  - src-one
  - src-two
project: my-api
project_path: /home/vic/my-api
branch: main
date: 2026-06-15T10:30
session_id: sess-123
repo_slug: my-api
citations: [{"file": "a.py", "anchor": "def f("}]
---

Full body.
"""


class TestSplitFrontmatter:
    def test_inline_tags_splits_cleanly(self):
        frontmatter, body = fm.split_frontmatter(INLINE_TAGS)
        assert "tags: [alpha, beta" in frontmatter
        assert body.strip() == "Body with inline tags."

    def test_no_frontmatter_returns_whole_text_as_body(self):
        frontmatter, body = fm.split_frontmatter(NO_FRONTMATTER)
        assert frontmatter == ""
        assert body == NO_FRONTMATTER

    def test_body_dashes_do_not_fabricate_or_truncate_frontmatter(self):
        frontmatter, body = fm.split_frontmatter(BODY_DASHES)
        assert "title: Body has dashes" in frontmatter
        assert "More body text after a body-level dash fence." in body
        # Only ONE frontmatter fence is consumed -- the body-level "---"
        # must still be present in body, not swallowed.
        assert body.count("---") == 1


class TestParse:
    def test_inline_tags(self):
        fields, body = fm.parse(INLINE_TAGS)
        assert fields["tags"] == ["alpha", "beta", "quoted tag"]
        assert fields["title"] == "Inline tags note"
        assert fields["certainty"] == "3" or fields["certainty"] == "4"
        assert body.strip() == "Body with inline tags."

    def test_block_tags_are_visible(self):
        """The ONE intended behavior change (MEM-166): block-style lists parse."""
        fields, body = fm.parse(BLOCK_TAGS)
        assert fields["tags"] == ["alpha", "beta", "quoted tag"]
        assert fields["title"] == "Block tags note"

    def test_no_frontmatter_returns_empty_fields(self):
        fields, body = fm.parse(NO_FRONTMATTER)
        assert fields == {}
        assert body == NO_FRONTMATTER

    def test_body_dashes_not_treated_as_frontmatter(self):
        fields, body = fm.parse(BODY_DASHES)
        assert fields["title"] == "Body has dashes"
        assert "More body text after a body-level dash fence." in body

    def test_unknown_keys_preserved(self):
        fields, _ = fm.parse(UNKNOWN_KEYS)
        assert fields["hand-added-key"] == "some value"
        assert fields["another_custom"] == "42"

    def test_all_schema_field_shapes(self):
        fields, body = fm.parse(ALL_SCHEMA_FIELDS)
        assert fields["title"] == "Full schema shape"
        assert fields["type"] == "decision"
        assert fields["tags"] == ["caching", "redis"]
        assert fields["source"] == "session"
        assert fields["origin"] == "manual-note"
        assert fields["certainty"] == "5"
        assert fields["validity-context"] == "only for staging"
        assert fields["supersedes"] == "[[older-note]]"
        assert fields["synthesized_from"] == ["src-one", "src-two"]
        assert fields["project"] == "my-api"
        assert fields["project_path"] == "/home/vic/my-api"
        assert fields["branch"] == "main"
        assert fields["date"] == "2026-06-15T10:30"
        assert fields["session_id"] == "sess-123"
        assert fields["repo_slug"] == "my-api"
        assert body.strip() == "Full body."


class TestGetScalar:
    def test_returns_stripped_quoted_value(self):
        frontmatter, _ = fm.split_frontmatter(ALL_SCHEMA_FIELDS)
        assert fm.get_scalar(frontmatter, "title") == "Full schema shape"
        assert fm.get_scalar(frontmatter, "supersedes") == "[[older-note]]"

    def test_missing_key_returns_none(self):
        frontmatter, _ = fm.split_frontmatter(ALL_SCHEMA_FIELDS)
        assert fm.get_scalar(frontmatter, "nonexistent") is None


class TestGetInt:
    def test_parses_int(self):
        frontmatter, _ = fm.split_frontmatter(ALL_SCHEMA_FIELDS)
        assert fm.get_int(frontmatter, "certainty") == 5

    def test_unparseable_returns_none(self):
        frontmatter, _ = fm.split_frontmatter(ALL_SCHEMA_FIELDS)
        assert fm.get_int(frontmatter, "title") is None

    def test_missing_returns_none(self):
        assert fm.get_int("", "certainty") is None


class TestGetBool:
    def test_true_values(self):
        for raw in ("true", "yes", "1", "TRUE", "Yes"):
            assert fm.get_bool(f"citation_stale: {raw}", "citation_stale") is True

    def test_false_and_missing(self):
        assert fm.get_bool("citation_stale: false", "citation_stale") is False
        assert fm.get_bool("", "citation_stale") is False


class TestGetDate:
    def test_parses_leading_date(self):
        frontmatter, _ = fm.split_frontmatter(ALL_SCHEMA_FIELDS)
        parsed = fm.get_date(frontmatter, "date")
        assert parsed == datetime(2026, 6, 15, tzinfo=timezone.utc)

    def test_missing_returns_none(self):
        assert fm.get_date("", "date") is None

    def test_unparseable_returns_none(self):
        assert fm.get_date("date: not-a-date", "date") is None


class TestGetList:
    def test_inline_list(self):
        frontmatter, _ = fm.split_frontmatter(INLINE_TAGS)
        assert fm.get_list(frontmatter, "tags") == ["alpha", "beta", "quoted tag"]

    def test_block_list(self):
        frontmatter, _ = fm.split_frontmatter(BLOCK_TAGS)
        assert fm.get_list(frontmatter, "tags") == ["alpha", "beta", "quoted tag"]

    def test_missing_key_returns_empty_list(self):
        frontmatter, _ = fm.split_frontmatter(INLINE_TAGS)
        assert fm.get_list(frontmatter, "nonexistent") == []

    def test_bare_scalar_is_not_coerced_into_a_list(self):
        assert fm.get_list("tags: solo", "tags") == []


class TestUnmanagedLines:
    def test_preserves_keys_not_in_managed_set(self):
        frontmatter, _ = fm.split_frontmatter(UNKNOWN_KEYS)
        preserved = fm.unmanaged_lines(frontmatter, managed_keys={"title", "type"})
        assert preserved == ["hand-added-key: some value", "another_custom: 42"]

    def test_default_managed_keys_preserves_everything(self):
        frontmatter, _ = fm.split_frontmatter(UNKNOWN_KEYS)
        preserved = fm.unmanaged_lines(frontmatter)
        assert "title: Unknown keys note" in preserved

    def test_preserves_block_list_continuation_lines(self):
        frontmatter, _ = fm.split_frontmatter(BLOCK_TAGS)
        preserved = fm.unmanaged_lines(frontmatter, managed_keys={"title", "type", "certainty"})
        assert preserved == ["tags:", "  - alpha", "  - beta", "  - quoted tag"]


class TestSerializeRoundTrip:
    def test_scalar_and_list_round_trip(self):
        fields = {"title": "Round Trip", "type": "discovery", "tags": ["a", "b"], "certainty": 4}
        text = fm.serialize(fields, "Body text.", key_order=["title", "type", "tags", "certainty"])

        reparsed_fields, reparsed_body = fm.parse(text)
        assert reparsed_fields["title"] == "Round Trip"
        assert reparsed_fields["type"] == "discovery"
        assert reparsed_fields["tags"] == ["a", "b"]
        assert reparsed_fields["certainty"] == "4"
        assert reparsed_body.strip() == "Body text."

    def test_none_values_are_omitted(self):
        text = fm.serialize({"title": "T", "origin": None}, "Body.")
        assert "origin" not in text

    def test_key_order_controls_emission_order(self):
        text = fm.serialize({"type": "discovery", "title": "T"}, "Body.", key_order=["title", "type"])
        lines = [line for line in text.splitlines() if ":" in line]
        assert lines[0].startswith("title:")
        assert lines[1].startswith("type:")


class TestKnownFieldsDriftLock:
    """Cross-checked against scripts/check_frontmatter_schema.py in
    tests/test_frontmatter_schema.py; this just asserts the table shape."""

    def test_known_fields_is_nonempty_and_contains_core_managed_keys(self):
        assert "title" in fm.KNOWN_FIELDS
        assert "tags" in fm.KNOWN_FIELDS
        assert fm.KNOWN_FIELDS["tags"] == "list"
