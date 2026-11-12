#!/usr/bin/env python3
"""Print the SHA-256 of the committed L1 and L2 ruleset JSON for the class file.

The class file records both hashes, so a reader can tell that the JSON the
detector compares against is the JSON the listed human approved. The hash is
of the file's bytes as committed, not of a re-serialisation.

    python3 scripts/ruleset_hashes.py            # prints the two class-file lines
    python3 scripts/ruleset_hashes.py --check    # exits 1 if the class file disagrees

Before the class file's first commit, --check reads its template
(templates/go-module-bump.yaml), which carries the same two hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--l1", default=str(ROOT / "rulesets/svc-hello/l1.json"))
    ap.add_argument("--l2", default=str(ROOT / "rulesets/svc-hello/l2.json"))
    ap.add_argument("--check", action="store_true",
                    help="compare with the class file instead of printing")
    ap.add_argument("--class-file", default=None,
                    help="default: classes/go-module-bump.yaml, or its template before the first commit")
    args = ap.parse_args(argv)
    h1, h2 = file_sha256(Path(args.l1)), file_sha256(Path(args.l2))
    if not args.check:
        print(f"  l1_sha256: \"{h1}\"")
        print(f"  l2_sha256: \"{h2}\"")
        return 0
    sys.path.insert(0, str(ROOT))
    from factory import classfile  # noqa: E402
    path = args.class_file or classfile.committed_path(ROOT)
    print(f"class file: {Path(path).relative_to(ROOT) if Path(path).is_relative_to(ROOT) else path}")
    cf = classfile.load(path)
    ok = cf["rulesets"]["l1_sha256"] == h1 and cf["rulesets"]["l2_sha256"] == h2
    print(f"l1 {h1} {'ok' if cf['rulesets']['l1_sha256'] == h1 else 'MISMATCH'}")
    print(f"l2 {h2} {'ok' if cf['rulesets']['l2_sha256'] == h2 else 'MISMATCH'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
