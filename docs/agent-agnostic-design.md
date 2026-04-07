# Agent-Agnostic Design

## PR1 status

PR1 is substantially complete.

Implemented:
- `memento/` package extraction:
  - `memento/config.py`
  - `memento/utils.py`
  - `memento/search.py`
  - `memento/graph.py`
  - `memento/store.py`
  - `memento/llm.py`
- `hooks/memento_utils.py` reduced to a compatibility re-export shim
- shared LLM abstraction via `llm_complete()`
- structured triage note extraction using Python-managed vault writes
- inception and deep recall routed through `memento.llm`

## Completed batches

- `B1`: config extraction and package skeleton
- `B2`: search, graph, and utils extraction
- `B3`: store extraction plus note-writing helpers
- `B4`: LLM backend abstraction and tests
- `B5`: LLM call-site refactors
- `B6`: direct hook and test imports away from `memento_utils` where practical

## Remaining tail work

- manual SessionEnd smoke test for the new triage path
- optional cleanup of test loader shims for hyphenated hook filenames

## Deviations from the original plan

- `memento_utils.py` remains as a backwards-compatibility shim instead of disappearing entirely
- the planned design doc path did not exist in this checkout, so this file was created as the PR1 record
- some tests still use loader shims for hook files with hyphenated filenames; that is separate from the `memento_utils` extraction

## Notes

- runtime directory selection now verifies writability and falls back safely when `$XDG_RUNTIME_DIR` is present but unusable
- final quality gates for this branch should use:
  - `ruff check memento hooks tests`
  - `ruff format --check .`
  - `.venv/bin/pytest -q`
