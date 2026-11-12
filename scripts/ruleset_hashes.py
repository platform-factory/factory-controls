#!/usr/bin/env python3
"""Print the SHA-256 of the committed ruleset JSON (L1, L2, the wall) for the class file.

The class file records the three hashes, so a reader can tell that the JSON
the detector compares against is the JSON the listed human approved. The
hash is of the file's bytes as committed, not of a re-serialisation. There
are two rulesets on svc-hello (the rung ruleset, at L1 or L2, and the wall
ruleset) because GitHub refuses two required-reviewers entries for one team
in one ruleset (probe P1; the readiness note's fallback).

    python3 scripts/ruleset_hashes.py            # prints the three class-file lines
    python3 scripts/ruleset_hashes.py --check    # exits 1 if the class file disagrees

Before the class file's first commit, --check reads its template
(templates/go-module-bump.yaml), which carries the same three hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WHICH = ("l1", "l2", "wall")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--l1", default=str(ROOT / "rulesets/svc-hello/l1.json"))
    ap.add_argument("--l2", default=str(ROOT / "rulesets/svc-hello/l2.json"))
    ap.add_argument("--wall", default=str(ROOT / "rulesets/svc-hello/wall.json"))
    ap.add_argument("--check", action="store_true",
                    help="compare with the class file instead of printing")
    ap.add_argument("--class-file", default=None,
                    help="default: classes/go-module-bump.yaml, or its template before the first commit")
    args = ap.parse_args(argv)
    hashes = {w: file_sha256(Path(getattr(args, w))) for w in WHICH}
    if not args.check:
        for w in WHICH:
            print(f"  {w}_sha256: \"{hashes[w]}\"")
        return 0
    sys.path.insert(0, str(ROOT))
    from factory import classfile  # noqa: E402
    path = args.class_file or classfile.committed_path(ROOT)
    print(f"class file: {Path(path).relative_to(ROOT) if Path(path).is_relative_to(ROOT) else path}")
    cf = classfile.load(path)
    ok = True
    for w in WHICH:
        same = cf["rulesets"][f"{w}_sha256"] == hashes[w]
        ok = ok and same
        print(f"{w} {hashes[w]} {'ok' if same else 'MISMATCH'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
