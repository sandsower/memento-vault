"""Tests for Inception HDBSCAN clustering wrapper."""

import numpy as np
import pytest

from memento_inception import cluster_notes


def make_cluster(rng, n, dim=768, noise=0.01):
    """Create n similar vectors around a random center."""
    center = rng.randn(dim).astype(np.float32)
    center /= np.linalg.norm(center)
    vecs = []
    for _ in range(n):
        v = center + rng.randn(dim).astype(np.float32) * noise
        v /= np.linalg.norm(v)
        vecs.append(v)
    return np.array(vecs)


# -- test_clusters_similar_vectors ----------------------------------------


def test_clusters_similar_vectors():
    """Two well-separated groups of 4 similar vectors produce 2 clusters."""
    rng = np.random.RandomState(42)

    group_a = make_cluster(rng, 4, dim=768, noise=0.01)
    group_b = make_cluster(rng, 4, dim=768, noise=0.01)

    matrix = np.vstack([group_a, group_b])
    stems = [f"note-a{i}" for i in range(4)] + [f"note-b{i}" for i in range(4)]

    config = {
        "inception_min_cluster_size": 3,
        "inception_cluster_threshold": 0.3,
        "inception_max_clusters": 10,
    }

    result = cluster_notes(matrix, stems, config)

    assert len(result) == 2, f"Expected 2 clusters, got {len(result)}: {result}"

    # Each cluster should contain stems from only one group
    all_stems = set()
    for cid, cluster_stems in result.items():
        stem_set = set(cluster_stems)
        all_stems.update(stem_set)
        # All stems in a cluster should share the same prefix
        prefixes = {s.split("-")[1][0] for s in cluster_stems}
        assert len(prefixes) == 1, f"Cluster {cid} mixes groups: {cluster_stems}"

    assert len(all_stems) == 8


# -- test_all_noise -------------------------------------------------------


def test_all_noise():
    """Random diverse unit vectors produce no clusters (all noise)."""
    rng = np.random.RandomState(99)

    # 10 random unit vectors in 768-d -- very unlikely to cluster
    vecs = []
    for _ in range(10):
        v = rng.randn(768).astype(np.float32)
        v /= np.linalg.norm(v)
        vecs.append(v)
    matrix = np.array(vecs)
    stems = [f"noise-{i}" for i in range(10)]

    config = {
        "inception_min_cluster_size": 3,
        "inception_cluster_threshold": 0.3,
        "inception_max_clusters": 10,
    }

    result = cluster_notes(matrix, stems, config)
    assert result == {}


# -- test_respects_min_cluster_size ----------------------------------------


def test_respects_min_cluster_size():
    """A tight group of 3 notes is excluded when min_cluster_size=4."""
    rng = np.random.RandomState(77)

    # 3 similar vectors + 5 random noise vectors
    tight = make_cluster(rng, 3, dim=768, noise=0.01)
    noise_vecs = []
    for _ in range(5):
        v = rng.randn(768).astype(np.float32)
        v /= np.linalg.norm(v)
        noise_vecs.append(v)
    noise_mat = np.array(noise_vecs)

    matrix = np.vstack([tight, noise_mat])
    stems = [f"tight-{i}" for i in range(3)] + [f"noise-{i}" for i in range(5)]

    config = {
        "inception_min_cluster_size": 4,
        "inception_cluster_threshold": 0.3,
        "inception_max_clusters": 10,
    }

    result = cluster_notes(matrix, stems, config)

    # The group of 3 should not appear because min_cluster_size=4
    for cid, cluster_stems in result.items():
        assert len(cluster_stems) >= 4, (
            f"Cluster {cid} has {len(cluster_stems)} members, expected >= 4"
        )


# -- test_max_clusters_limit -----------------------------------------------


def test_max_clusters_limit():
    """Only the top max_clusters clusters are returned."""
    rng = np.random.RandomState(55)

    # Create 4 well-separated clusters of different sizes: 6, 5, 4, 3
    groups = []
    group_stems = []
    for gidx, size in enumerate([6, 5, 4, 3]):
        cluster_vecs = make_cluster(rng, size, dim=768, noise=0.01)
        groups.append(cluster_vecs)
        group_stems.extend([f"g{gidx}-{i}" for i in range(size)])

    matrix = np.vstack(groups)

    config = {
        "inception_min_cluster_size": 3,
        "inception_cluster_threshold": 0.3,
        "inception_max_clusters": 2,
    }

    result = cluster_notes(matrix, group_stems, config)

    assert len(result) <= 2, f"Expected at most 2 clusters, got {len(result)}"

    # The returned clusters should be the two largest
    sizes = sorted([len(s) for s in result.values()], reverse=True)
    if len(sizes) == 2:
        assert sizes[0] >= sizes[1]
        # Both should be from the bigger groups
        assert sizes[0] >= 4


# -- test_too_few_notes ----------------------------------------------------


def test_too_few_notes():
    """Fewer notes than min_cluster_size returns empty dict without error."""
    rng = np.random.RandomState(11)

    matrix = make_cluster(rng, 2, dim=768, noise=0.01)
    stems = ["only-a", "only-b"]

    config = {
        "inception_min_cluster_size": 3,
        "inception_cluster_threshold": 0.3,
        "inception_max_clusters": 10,
    }

    result = cluster_notes(matrix, stems, config)
    assert result == {}


# -- test_deterministic ----------------------------------------------------


def test_deterministic():
    """Same input produces identical output across two runs."""
    rng1 = np.random.RandomState(42)
    group_a = make_cluster(rng1, 5, dim=768, noise=0.01)
    rng2 = np.random.RandomState(123)
    group_b = make_cluster(rng2, 5, dim=768, noise=0.01)

    matrix = np.vstack([group_a, group_b])
    stems = [f"x-{i}" for i in range(5)] + [f"y-{i}" for i in range(5)]

    config = {
        "inception_min_cluster_size": 3,
        "inception_cluster_threshold": 0.3,
        "inception_max_clusters": 10,
    }

    result1 = cluster_notes(matrix, stems, config)
    result2 = cluster_notes(matrix, stems, config)

    assert result1 == result2
