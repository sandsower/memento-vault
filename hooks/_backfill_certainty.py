#!/usr/bin/env python3
"""Backfill missing certainty fields in vault notes.

Usage: _backfill_certainty.py <notes_dir> [delay_seconds]

Inference rules:
  source: manual          -> certainty 3
  type: decision          -> certainty 3
  type: discovery, source: session -> certainty 2
  type: pattern, source: inception -> certainty 3
  type: tool              -> certainty 3
  default                 -> certainty 2
"""

import glob
import os
import re
import sys
import time


def infer_certainty(source, note_type):
    if source == "manual":
        return 3
    if note_type == "decision":
        return 3
    if note_type == "discovery" and source == "session":
        return 2
    if note_type == "pattern" and source == "inception":
        return 3
    if note_type == "tool":
        return 3
    return 2


def main():
    notes_dir = sys.argv[1]
    delay = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    if delay > 0:
        time.sleep(delay)

    patched = 0
    for f in glob.glob(os.path.join(notes_dir, "*.md")):
        text = open(f, encoding="utf-8").read()

        if re.search(r"^certainty:", text, re.MULTILINE):
            continue
        if not text.startswith("---"):
            continue

        fm_end = text.find("\n---", 4)
        if fm_end == -1:
            continue

        fm = text[4:fm_end]
        source_m = re.search(r"^source:\s*(\S+)", fm, re.MULTILINE)
        type_m = re.search(r"^type:\s*(\S+)", fm, re.MULTILINE)
        source = source_m.group(1) if source_m else None
        note_type = type_m.group(1) if type_m else None

        certainty = infer_certainty(source, note_type)

        new_text = text[:fm_end] + f"\ncertainty: {certainty}" + text[fm_end:]
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        patched += 1

    if patched:
        print(f"Backfilled certainty on {patched} notes", file=sys.stderr)


if __name__ == "__main__":
    main()
