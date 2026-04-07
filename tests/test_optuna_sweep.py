import sys
from pathlib import Path
from unittest.mock import patch

import optuna

sys.path.insert(0, str(Path(__file__).parent.parent / "benchmark"))


class TestDefineSearchSpace:
    def test_returns_dict(self):
        """define_search_space should return a config dict."""
        from optuna_sweep import define_search_space

        study = optuna.create_study()
        trial = study.ask()
        config = define_search_space(trial)

        assert isinstance(config, dict)
        assert "granularity" in config
        assert "retrieval_limit" in config
        assert "prf_enabled" in config

    def test_all_expected_keys_present(self):
        """All 11+ config keys should be present."""
        from optuna_sweep import define_search_space

        study = optuna.create_study()
        trial = study.ask()
        config = define_search_space(trial)

        expected_keys = [
            "granularity",
            "retrieval_limit",
            "recall_min_score",
            "recall_high_confidence",
            "prf_enabled",
            "prf_max_terms",
            "prf_top_docs",
            "reranker_top_k",
            "reranker_min_score",
        ]
        for key in expected_keys:
            assert key in config, f"Missing key: {key}"

    def test_values_in_range(self):
        """Suggested values should be within defined ranges."""
        from optuna_sweep import define_search_space

        study = optuna.create_study()
        trial = study.ask()
        config = define_search_space(trial)

        assert 3 <= config["retrieval_limit"] <= 20
        assert 0.0 <= config["recall_min_score"] <= 0.3
        assert config["granularity"] in ["session", "turn"]


class TestCreateObjective:
    def test_returns_callable(self):
        """create_objective should return a function."""
        from optuna_sweep import create_objective

        with patch("optuna_sweep.run_retrieval_eval", return_value={"ndcg@10": 0.75}):
            obj = create_objective("fake_path.json")
            assert callable(obj)

    def test_objective_returns_float(self):
        """Objective function should return a float score."""
        from optuna_sweep import create_objective

        mock_metrics = {"ndcg@10": 0.75, "mrr": 0.8, "recall@5": 0.9}

        with patch("optuna_sweep.run_retrieval_eval", return_value=mock_metrics):
            obj = create_objective("fake_path.json", metric="ndcg@10")
            study = optuna.create_study()
            trial = study.ask()
            score = obj(trial)
            assert isinstance(score, float)
            assert score == 0.75


class TestRunSweep:
    def test_creates_study_with_sqlite(self, tmp_path):
        """run_sweep should create an Optuna study with SQLite storage."""
        from optuna_sweep import run_sweep

        db_path = str(tmp_path / "test_sweep.db")

        mock_metrics = {"ndcg@10": 0.5}
        with patch("optuna_sweep.run_retrieval_eval", return_value=mock_metrics):
            study = run_sweep(
                dataset_path="fake.json",
                n_trials=3,
                db_path=db_path,
                study_name="test-sweep",
            )

        assert len(study.trials) == 3
        assert Path(db_path).exists()

    def test_resume_from_existing(self, tmp_path):
        """Running twice with same db should accumulate trials."""
        from optuna_sweep import run_sweep

        db_path = str(tmp_path / "resume_test.db")
        mock_metrics = {"ndcg@10": 0.5}

        with patch("optuna_sweep.run_retrieval_eval", return_value=mock_metrics):
            study1 = run_sweep("fake.json", n_trials=2, db_path=db_path, study_name="resume-test")
            assert len(study1.trials) == 2

            study2 = run_sweep("fake.json", n_trials=2, db_path=db_path, study_name="resume-test")
            assert len(study2.trials) == 4  # accumulated


class TestExportResults:
    def test_exports_json(self, tmp_path):
        """export_results should write a JSON file with top configs."""
        from optuna_sweep import export_results

        study = optuna.create_study(direction="maximize")
        study.optimize(lambda trial: trial.suggest_float("x", 0, 1), n_trials=5)

        output_path = str(tmp_path / "results.json")
        results = export_results(study, output_path=output_path, top_n=3)

        assert Path(output_path).exists()
        assert results["n_trials"] == 5
        assert len(results["top_configs"]) <= 3
        assert "best_value" in results
        assert "best_params" in results

    def test_top_configs_sorted_by_value(self, tmp_path):
        """Top configs should be sorted by score descending."""
        from optuna_sweep import export_results

        study = optuna.create_study(direction="maximize")
        study.optimize(lambda trial: trial.suggest_float("x", 0, 1), n_trials=10)

        output_path = str(tmp_path / "results.json")
        results = export_results(study, output_path=output_path, top_n=5)

        values = [c["value"] for c in results["top_configs"]]
        assert values == sorted(values, reverse=True)
