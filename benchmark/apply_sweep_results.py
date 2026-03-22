#!/usr/bin/env python3
"""
Apply Optuna sweep results to the Tenet retrieval config.

Reads the best config from a sweep results JSON and patches both:
1. benchmark/longmemeval_adapter.py get_default_config()
2. hooks/memento_utils.py DEFAULT_CONFIG

Usage:
    python benchmark/apply_sweep_results.py sweep_results.json
    python benchmark/apply_sweep_results.py sweep_results.json --dry-run
    python benchmark/apply_sweep_results.py sweep_results.json --trial 2  # use 2nd best instead of 1st
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Maps sweep param names → (file, config key, format)
# format: "int", "float", "bool", "str"
PARAM_MAP = {
    # Params that exist in both adapter and memento_utils
    "recall_high_confidence": ("both", "recall_high_confidence", "float"),
    "prf_enabled": ("both", "prf_enabled", "bool"),
    "prf_max_terms": ("both", "prf_max_terms", "int"),
    "prf_top_docs": ("both", "prf_top_docs", "int"),
    "reranker_top_k": ("both", "reranker_top_k", "int"),
    "reranker_min_score": ("both", "reranker_min_score", "float"),
    # Params only in memento_utils
    "recall_min_score": ("utils", "recall_min_score", "float"),
    "rrf_k": ("utils", "rrf_k", "int"),
    "pagerank_boost_weight": ("utils", "pagerank_boost_weight", "float"),
    "ppr_max_expanded": ("utils", "ppr_max_expanded", "int"),
    "ppr_min_score": ("utils", "ppr_min_score", "float"),
    "temporal_decay_half_life": ("utils", "temporal_decay_half_life", "int"),
    # Params only in adapter
    "granularity": ("adapter", "granularity", "str"),
    "retrieval_limit": ("adapter", "retrieval_limit", "int"),
}

UTILS_PATH = ROOT / "hooks" / "memento_utils.py"
ADAPTER_PATH = ROOT / "benchmark" / "longmemeval_adapter.py"


def format_value(value, fmt):
    """Format a value for Python source code."""
    if fmt == "bool":
        return "True" if value else "False"
    elif fmt == "int":
        return str(int(value))
    elif fmt == "float":
        # Round to reasonable precision
        if abs(value) < 0.01:
            return f"{value:.4f}"
        return f"{value:.2f}" if value != int(value) else f"{value:.1f}"
    elif fmt == "str":
        return f'"{value}"'
    return repr(value)


def patch_file(file_path, key, new_value, fmt, dry_run=False):
    """Patch a config value in a Python source file.

    Matches patterns like:
        "key": value,
        "key": value,  # comment
    """
    content = file_path.read_text()
    formatted = format_value(new_value, fmt)

    # Match the key in a dict literal: "key": <old_value>
    # Handles: "key": 0.55,  and  "key": 0.55,  # comment
    pattern = rf'("{key}":\s*)([^,\n]+)(,.*)'
    match = re.search(pattern, content)

    if not match:
        return False, f"Key '{key}' not found in {file_path.name}"

    old_value_str = match.group(2).strip()
    if old_value_str == formatted:
        return True, f"{key}: {formatted} (unchanged)"

    new_line = f'{match.group(1)}{formatted}{match.group(3)}'
    new_content = content[:match.start()] + new_line + content[match.end():]

    if not dry_run:
        file_path.write_text(new_content)

    return True, f"{key}: {old_value_str} → {formatted}"


def apply_config(best_params, dry_run=False):
    """Apply best params to source files."""
    changes = []

    for param_name, value in best_params.items():
        if param_name not in PARAM_MAP:
            changes.append(f"  SKIP {param_name} (not in PARAM_MAP)")
            continue

        target, config_key, fmt = PARAM_MAP[param_name]

        if target in ("both", "utils"):
            ok, msg = patch_file(UTILS_PATH, config_key, value, fmt, dry_run)
            changes.append(f"  {'[DRY] ' if dry_run else ''}memento_utils.py: {msg}")

        if target in ("both", "adapter"):
            ok, msg = patch_file(ADAPTER_PATH, config_key, value, fmt, dry_run)
            changes.append(f"  {'[DRY] ' if dry_run else ''}longmemeval_adapter.py: {msg}")

    return changes


def main():
    parser = argparse.ArgumentParser(description="Apply sweep results to config")
    parser.add_argument("results", help="Path to sweep_results.json")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    parser.add_argument("--trial", type=int, default=0, help="Which top config to use (0=best)")

    args = parser.parse_args()

    results = json.loads(Path(args.results).read_text())

    configs = results.get("top_configs", [])
    if not configs:
        print("No configs found in results file.")
        sys.exit(1)

    if args.trial >= len(configs):
        print(f"Trial {args.trial} not found (only {len(configs)} configs).")
        sys.exit(1)

    chosen = configs[args.trial]
    params = chosen["params"]
    score = chosen["value"]

    print(f"Applying config from trial #{chosen['trial_number']} (score: {score:.4f})")
    if args.dry_run:
        print("[DRY RUN — no files will be modified]\n")

    changes = apply_config(params, dry_run=args.dry_run)
    for c in changes:
        print(c)

    if not args.dry_run:
        print(f"\nDone. Config applied from trial #{chosen['trial_number']}.")
        print("Run the baseline again to verify:")
        print("  python benchmark/longmemeval_adapter.py --dataset data/longmemeval/longmemeval_s --mode retrieval")


if __name__ == "__main__":
    main()
