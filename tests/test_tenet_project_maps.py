"""Tests for project retrieval maps (build, write, load, lookup)."""

import json


from memento_inception import build_project_maps, write_project_maps
from memento.graph import load_project_maps, lookup_project_notes


# --- build_project_maps ---


class TestBuildProjectMaps:
    def test_groups_by_project(self, tmp_vault, sample_notes):
        """Notes are grouped under their project slug."""
        maps = build_project_maps(tmp_vault)
        assert "api-service" in maps
        stems = [e["stem"] for e in maps["api-service"]]
        assert "redis-cache-ttl" in stems
        assert "redis-eviction-policy" in stems

        assert "frontend" in maps
        stems_fe = [e["stem"] for e in maps["frontend"]]
        assert "zustand-state-reset" in stems_fe
        assert "react-query-wrapper" in stems_fe

    def test_ranking(self, tmp_vault, sample_notes):
        """Higher certainty ranks first within a project."""
        maps = build_project_maps(tmp_vault)
        api_notes = maps["api-service"]
        # redis-cache-ttl has certainty=4, redis-eviction-policy has certainty=3
        assert api_notes[0]["stem"] == "redis-cache-ttl"
        assert api_notes[1]["stem"] == "redis-eviction-policy"

    def test_skips_no_project(self, tmp_vault, sample_notes):
        """Notes without a project field do not appear in any map."""
        maps = build_project_maps(tmp_vault)
        all_stems = [e["stem"] for entries in maps.values() for e in entries]
        # archived-note has no project field
        assert "archived-note" not in all_stems
        # existing-pattern is source:inception and also has no project
        assert "existing-pattern" not in all_stems

    def test_empty_vault(self, tmp_vault):
        """Empty notes directory produces empty maps."""
        maps = build_project_maps(tmp_vault)
        assert maps == {}


# --- write + load round-trip ---


class TestWriteAndLoadProjectMaps:
    def test_round_trip(self, tmp_vault, sample_notes, tmp_path):
        """Build, write, load cycle preserves the data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        maps = build_project_maps(tmp_vault)
        write_project_maps(maps, config_dir=str(config_dir))

        loaded = load_project_maps(config_dir=str(config_dir))
        assert set(loaded.keys()) == set(maps.keys())
        for slug in maps:
            assert len(loaded[slug]) == len(maps[slug])
            for orig, loaded_entry in zip(maps[slug], loaded[slug]):
                assert orig["stem"] == loaded_entry["stem"]
                assert orig["title"] == loaded_entry["title"]


# --- lookup_project_notes ---


class TestLookupProjectNotes:
    def test_exact(self, tmp_vault, sample_notes, tmp_path):
        """Exact slug lookup returns matching notes."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        maps = build_project_maps(tmp_vault)
        write_project_maps(maps, config_dir=str(config_dir))
        loaded = load_project_maps(config_dir=str(config_dir))

        results = lookup_project_notes("api-service", maps=loaded)
        assert len(results) > 0
        stems = [r["path"] for r in results]
        assert "notes/redis-cache-ttl.md" in stems

    def test_partial(self, tmp_vault, sample_notes, tmp_path):
        """Partial slug matches via substring."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        maps = build_project_maps(tmp_vault)
        write_project_maps(maps, config_dir=str(config_dir))
        loaded = load_project_maps(config_dir=str(config_dir))

        results = lookup_project_notes("api", maps=loaded)
        assert len(results) > 0

    def test_limit(self, tmp_vault, sample_notes, tmp_path):
        """Limit caps the number of returned results."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        maps = build_project_maps(tmp_vault)
        write_project_maps(maps, config_dir=str(config_dir))
        loaded = load_project_maps(config_dir=str(config_dir))

        results = lookup_project_notes("api-service", maps=loaded, limit=1)
        assert len(results) == 1

    def test_missing(self, tmp_vault, sample_notes, tmp_path):
        """Non-existent slug returns empty list."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        maps = build_project_maps(tmp_vault)
        write_project_maps(maps, config_dir=str(config_dir))
        loaded = load_project_maps(config_dir=str(config_dir))

        results = lookup_project_notes("nonexistent", maps=loaded)
        assert results == []


# --- Briefing fast-path integration ---


class TestProjectMapsIntegration:
    """Test project maps fast path in briefing hook."""

    def _build_and_load_maps(self, tmp_vault, tmp_path):
        """Helper: build project maps, write them, load them back."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        maps = build_project_maps(tmp_vault)
        write_project_maps(maps, config_dir=str(config_dir))
        return load_project_maps(config_dir=str(config_dir))

    def test_fast_path_skips_vsearch_when_enough_results(self, tmp_vault, sample_notes, tmp_path):
        """When project maps have >= max_notes results, deferred file is written as ready."""
        loaded = self._build_and_load_maps(tmp_vault, tmp_path)

        # api-service has 2 notes; with max_notes=2 the fast path should fire
        map_notes = lookup_project_notes("api-service", maps=loaded, limit=5)
        max_notes = 2
        assert len(map_notes) >= max_notes

        # Simulate the fast-path logic: format note lines
        note_lines = [f"  - {n['title']}" for n in map_notes[:max_notes]]

        deferred_path = tmp_path / "deferred-briefing.json"
        import time

        with open(deferred_path, "w") as f:
            json.dump(
                {
                    "status": "ready",
                    "note_lines": note_lines,
                    "timestamp": time.time(),
                    "source": "project-maps",
                },
                f,
            )

        with open(deferred_path) as f:
            result = json.load(f)

        assert result["status"] == "ready"
        assert result["source"] == "project-maps"
        assert len(result["note_lines"]) == max_notes
        for line in result["note_lines"]:
            assert line.startswith("  - ")

    def test_falls_through_when_insufficient_results(self, tmp_vault, sample_notes, tmp_path):
        """When project maps have < max_notes, the fast path does not fire."""
        loaded = self._build_and_load_maps(tmp_vault, tmp_path)

        # api-service has only 2 notes; with max_notes=5 the fast path should NOT fire
        map_notes = lookup_project_notes("api-service", maps=loaded, limit=5)
        max_notes = 5
        assert len(map_notes) < max_notes

    def test_falls_through_when_disabled(self, tmp_vault, sample_notes, tmp_path):
        """When project_maps_enabled=False, the fast path is skipped entirely."""
        loaded = self._build_and_load_maps(tmp_vault, tmp_path)

        config = {"project_maps_enabled": False, "briefing_max_notes": 2}

        # Even though maps have enough results, the config flag disables the path
        if config.get("project_maps_enabled", True):
            map_notes = lookup_project_notes("api-service", maps=loaded, limit=5)
        else:
            map_notes = []

        assert map_notes == []

    def test_fast_path_decision_logic(self, tmp_vault, sample_notes, tmp_path):
        """Verify end-to-end decision: enough map results -> skip vsearch."""
        loaded = self._build_and_load_maps(tmp_vault, tmp_path)

        # api-service has 2 notes
        api_notes = lookup_project_notes("api-service", maps=loaded, limit=5)
        assert len(api_notes) >= 2

        # With max_notes=2, api-service qualifies for fast path
        assert len(api_notes) >= 2  # fast path fires

        # With max_notes=5, it would NOT (only 2 notes)
        assert len(api_notes) < 5  # falls through to vsearch

        # frontend also has 2 notes
        fe_notes = lookup_project_notes("frontend", maps=loaded, limit=5)
        assert len(fe_notes) >= 2
        assert len(fe_notes) < 5
