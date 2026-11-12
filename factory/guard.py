"""Workflow runs do nothing until the class file is there and filled.

Why: agent.yml, detector.yml and scorecard.yml start on their cron as soon
as `main` is pushed, and `main` is pushed without the class file: its first
commit is the filled one the listed human approves, and until then only the
template (`templates/go-module-bump.yaml`, five placeholders) exists. A
class file that still holds a placeholder is treated the same. Until both
are settled a run must change nothing: no App token minted, no environment
entered, no write to svc-hello, to the records branch or to an issue. So
every workflow that runs the class (the three scheduled ones, fixtures.yml
and reveal.yml) starts with a job that runs this module, holding no secret
and only `contents: read`, and every other job needs it and runs only when
it reports ready.

It prints one notice line (stderr) and one JSON object (stdout), writes
`ready=true|false` to $GITHUB_OUTPUT, and exits 0 either way. It exits
non-zero only when a class file that is there cannot be loaded, or CLASS_FILE
names a copy that is missing, which are real failures.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import classfile


def check(env: dict, root: Path | None = None) -> dict:
    """{"ready": bool, "reason": str | None, "unfilled": [paths] | None}.
    Not ready when the class file is absent (`unfilled` is None) or holds
    any placeholder."""
    try:
        classfile.require(env, root=root)
    except classfile.NotReady as e:
        path = classfile.path_for_run(env, root=root)
        return {"ready": False, "reason": e.reason,
                "unfilled": classfile.unfilled(classfile.load(path)) if path else None}
    return {"ready": True, "reason": None, "unfilled": []}


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(prog="python3 -m factory.guard",
                                 description="Is the class file there and filled? Writes ready=true|false.")
    ap.parse_args(argv)
    try:
        out = check(env)
    except (OSError, ValueError) as e:   # ClassFileError is a ValueError
        print(f"::error::the class file does not load: {e}", file=sys.stderr)
        return 1
    if out["ready"]:
        print("::notice title=class file ready::the class file is there and filled; this run goes on",
              file=sys.stderr)
    else:
        print(classfile.notice(out["reason"]), file=sys.stderr)
    print(json.dumps(out, sort_keys=True))
    gho = env.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a") as f:
            f.write(f"ready={'true' if out['ready'] else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
