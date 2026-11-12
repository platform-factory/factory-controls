"""Load and validate the class file, `classes/go-module-bump.yaml`.

The class file holds every number the M4 readiness note froze. It is read by
the detector, the merge step, the scorecard and `class-scope` in svc-hello, so
an unknown or missing key is an error, never a default: a typo must not
silently become a different rule.

The note makes the class file's first commit the filled one, approved by the
listed human. So the repository is created without it: the same content with
its five placeholders waits at `templates/go-module-bump.yaml`, and the
first-commit pull request adds `classes/go-module-bump.yaml` filled and
deletes the template. Until then every CLI that runs the class treats the
missing file as it treats one with a placeholder: it does nothing, writes
nothing, and says so in one line (`require`, `NotReady`).

PyYAML is used when present. Without it the YAML is converted by `yq -o=json`
(present on GitHub-hosted runners), so nothing is installed at run time.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Values that stand in for facts that exist only later (the listed human's
# login after the owner's team change, the seed key's hash after the key is
# stored, the skill pin after its first release, the wall ruleset's id once
# it is created on svc-hello, disabled). `unfilled()` lists them, and every
# run that reads one does nothing while it remains.
PLACEHOLDERS = frozenset({
    "LISTED-HUMAN-LOGIN",
    "SEED-KEY-SHA256",
    "SKILL-TAG",
    "SKILL-COMMIT",
    "WALL-RULESET-ID",
})


class ClassFileError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("class file invalid: " + "; ".join(errors))


# -- where the class file is ----------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
CLASS_PATH = "classes/go-module-bump.yaml"
TEMPLATE_PATH = "templates/go-module-bump.yaml"
ALL_PLACEHOLDERS = None   # `require(needed=...)`: every placeholder blocks


class NotReady(Exception):
    """The class file is absent, or holds a placeholder this run needs. The
    run does nothing and writes nothing; the CLI exits 0 with one notice."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def committed_path(root: Path | None = None) -> Path:
    """The class file to validate offline: `classes/go-module-bump.yaml` once
    its first commit has landed, the template before."""
    root = ROOT if root is None else root
    real = root / CLASS_PATH
    return real if real.is_file() else root / TEMPLATE_PATH


def path_for_run(env: dict, *, allow_template: bool = False, root: Path | None = None) -> Path | None:
    """The class file a run reads, or None when there is none yet.

    CLASS_FILE names a copy when set (probe P4's `probe/p4-class.yaml`); a
    copy that is named but missing is an error, not an absent class file.
    Otherwise `classes/go-module-bump.yaml`; when that is absent, the template
    only for a local dry run that allows placeholders (`allow_template`)."""
    root = ROOT if root is None else root
    named = env.get("CLASS_FILE")
    if named:
        if not Path(named).is_file():
            raise ClassFileError([f"CLASS_FILE names {named}, which does not exist"])
        return Path(named)
    real = root / CLASS_PATH
    if real.is_file():
        return real
    return root / TEMPLATE_PATH if allow_template else None


def require(env: dict, needed: set[str] | None = ALL_PLACEHOLDERS, *, allow_placeholders: bool = False,
            root: Path | None = None) -> dict:
    """The class file for a run, or NotReady.

    `needed` lists the dotted paths whose placeholder stops this CLI (None:
    every placeholder); a path also covers everything under it
    (`rulesets` covers `rulesets.wall_ruleset_id`). `allow_placeholders` (local dry runs only) accepts
    them, and reads the template when the class file is absent."""
    path = path_for_run(env, allow_template=allow_placeholders, root=root)
    if path is None:
        raise NotReady(f"there is no {CLASS_PATH} yet: its first commit, filled and approved by the "
                       "listed human, has not landed")
    cf = load(path)
    blocking = [p for p in unfilled(cf) if needed is None or any(
        p == n or p.startswith(n + ".") or p.startswith(n + "[") for n in needed)]
    if blocking and not allow_placeholders:
        raise NotReady(f"the class file still holds placeholders at {', '.join(blocking)}")
    return cf


def notice(reason: str) -> str:
    """The one log line a run prints when it does nothing."""
    return f"::notice title=class file not ready::{reason}; this run does nothing and writes nothing"


def report_not_ready(reason: str, out: dict, outputs: dict | None = None, env: dict | None = None) -> int:
    """What a CLI does on NotReady: the notice on stderr (as the CLIs' other
    workflow commands), its usual one JSON object on stdout, the step
    outputs that mean "nothing done", and exit code 0."""
    import sys
    print(notice(reason), file=sys.stderr)
    print(json.dumps({**out, "ready": False, "reason": reason}, indent=2, sort_keys=True))
    gho = (env if env is not None else os.environ).get("GITHUB_OUTPUT")
    if gho and outputs:
        with open(gho, "a") as f:
            for k, v in outputs.items():
                f.write(f"{k}={v}\n")
    return 0


# Expected shape. A dict maps each required key to its expected shape; a type
# is checked with isinstance; a list of one shape means "a list of these".
_SEMVER = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")

SCHEMA: dict[str, Any] = {
    "schema": int,
    "class": str,
    "repo": str,
    "paths": [str],
    "queue": [{"module": str, "version": str}],
    "n": int,
    "window_weeks": int,
    "min_release_age_days": int,
    "human_logins": [str],
    "escape_a_minutes": int,
    "detector_stale_minutes": int,
    "freeze_pass_minutes": int,
    "review_median_pass_minutes": int,
    "review_median_falsifier_minutes": int,
    "concurrency": int,
    "rungs": {"l1": int, "l2": int, "max_rung": int, "wall_minimum_approvals": int},
    "schedules": {"detector_minutes": [int], "agent_minutes": [int]},
    "seeds": {
        "count": int,
        "positions": [int],
        "types": [{"id": str, "summary": str}],
        "rule": {
            "words": str,
            "key_encoding": str,
            "position_message": str,
            "type_message": str,
            "digest_prefix_bytes": int,
            "position_base": int,
            "position_modulus": int,
            "type_modulus": int,
            "pairing": str,
            "key_sha256_of": str,
        },
        "key_sha256": str,
    },
    "agent": {
        "app": str,
        "model": str,
        "harness": {"name": str, "version": str, "platform": str, "sha256": str},
        "builder_image": str,
        "govulncheck": {"module": str, "version": str},
        "app_token_action": {"repo": str, "version": str, "commit": str},
    },
    "skill": {"repo": str, "path": str, "tag": str, "commit": str},
    "rulesets": {
        "ruleset_id": int,
        "ruleset_name": str,
        "l1_path": str,
        "l1_sha256": str,
        "l2_path": str,
        "l2_sha256": str,
        "wall_ruleset_id": "id",
        "wall_ruleset_name": str,
        "wall_path": str,
        "wall_sha256": str,
        "repository_rulesets": [{"id": "id", "name": str, "source_type": str,
                                 "source": str, "target": str}],
    },
    "labels": {"human": [str], "machinery": [str]},
    "variables": {"force_inconclusive": str, "inject_post_merge": str},
    "checks": {
        "integration_id": int,
        "class_scope": {"name": str, "workflow": str, "job": str},
        "go_test": {"name": str, "workflow": str, "job": str},
    },
}


def _check(value: Any, shape: Any, path: str, errors: list[str]) -> None:
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            errors.append(f"{path or '<root>'}: expected a mapping")
            return
        for k in shape:
            if k not in value:
                errors.append(f"{path}{'.' if path else ''}{k}: missing")
        for k in value:
            if k not in shape:
                errors.append(f"{path}{'.' if path else ''}{k}: unknown key")
        for k, sub in shape.items():
            if k in value:
                _check(value[k], sub, f"{path}{'.' if path else ''}{k}", errors)
    elif isinstance(shape, list):
        if not isinstance(value, list):
            errors.append(f"{path}: expected a list")
            return
        for i, item in enumerate(value):
            _check(item, shape[0], f"{path}[{i}]", errors)
    elif shape == "id":
        # A GitHub id, or a placeholder until the object exists.
        if not ((isinstance(value, int) and not isinstance(value, bool)) or value in PLACEHOLDERS):
            errors.append(f"{path}: expected an id or a placeholder, got {value!r}")
    else:
        # bool is an int subclass; a YAML `true` where a number belongs is an error.
        if shape is int and isinstance(value, bool):
            errors.append(f"{path}: expected int, got bool")
        elif not isinstance(value, shape):
            errors.append(f"{path}: expected {shape.__name__}, got {type(value).__name__}")


def _semantic(cf: dict, errors: list[str]) -> None:
    """Rules a type check cannot express."""
    q = cf.get("queue")
    if isinstance(q, list):
        if len(q) != 25:
            errors.append(f"queue: expected 25 items, found {len(q)}")
        seen = set()
        last: dict[str, tuple] = {}
        for i, item in enumerate(q):
            if not isinstance(item, dict):
                continue
            v, m = item.get("version", ""), item.get("module", "")
            if not isinstance(v, str) or not _SEMVER.match(v):
                errors.append(f"queue[{i}].version: not a release tag vX.Y.Z: {v!r}")
                continue
            if (m, v) in seen:
                errors.append(f"queue[{i}]: duplicate {m}@{v}")
            seen.add((m, v))
            t = semver(v)
            if m in last and t <= last[m]:
                errors.append(f"queue[{i}]: {m} versions must rise in queue order")
            last[m] = t
    if cf.get("paths") not in (None, ["go.mod", "go.sum"]):
        errors.append("paths: the class's files are go.mod and go.sum at the root, nothing else")
    hl = cf.get("human_logins")
    if isinstance(hl, list) and len(hl) != 1:
        errors.append("human_logins: exactly one listed human login")
    seeds = cf.get("seeds")
    if isinstance(seeds, dict):
        pos = seeds.get("positions")
        if isinstance(pos, list) and (len(pos) != 2 or pos[0] > pos[1]):
            errors.append("seeds.positions: [lowest, highest]")
        k = seeds.get("key_sha256")
        if isinstance(k, str) and k not in PLACEHOLDERS and not _HEX64.match(k):
            errors.append("seeds.key_sha256: 64 lowercase hex characters or the placeholder")
        types = seeds.get("types")
        if isinstance(types, list) and len({t.get("id") for t in types if isinstance(t, dict)}) != len(types):
            errors.append("seeds.types: ids must be distinct")
    rs = cf.get("rulesets")
    if isinstance(rs, dict):
        for k in ("l1_sha256", "l2_sha256", "wall_sha256"):
            if isinstance(rs.get(k), str) and not _HEX64.match(rs[k]):
                errors.append(f"rulesets.{k}: 64 lowercase hex characters")
        # The listing GitHub must return is the rung ruleset and the wall
        # ruleset, by the ids and names above (both places filled alike).
        listed = rs.get("repository_rulesets")
        if isinstance(listed, list) and all(isinstance(x, dict) for x in listed):
            want = sorted([(str(rs.get("ruleset_id")), rs.get("ruleset_name")),
                           (str(rs.get("wall_ruleset_id")), rs.get("wall_ruleset_name"))])
            got = sorted((str(x.get("id")), x.get("name")) for x in listed)
            if got != want:
                errors.append("rulesets.repository_rulesets: must list exactly the rung ruleset and the wall "
                              "ruleset, by the ids and names above")
    agent = cf.get("agent")
    if isinstance(agent, dict):
        img = agent.get("builder_image")
        if isinstance(img, str) and "@sha256:" not in img:
            errors.append("agent.builder_image: must be pinned by digest")
        h = agent.get("harness")
        if isinstance(h, dict) and isinstance(h.get("sha256"), str) and not _HEX64.match(h["sha256"]):
            errors.append("agent.harness.sha256: 64 lowercase hex characters")
        ata = agent.get("app_token_action")
        if isinstance(ata, dict) and isinstance(ata.get("commit"), str) and not _HEX40.match(ata["commit"]):
            errors.append("agent.app_token_action.commit: a full commit SHA")
    sk = cf.get("skill")
    if isinstance(sk, dict) and isinstance(sk.get("commit"), str):
        c = sk["commit"]
        if c not in PLACEHOLDERS and not _HEX40.match(c):
            errors.append("skill.commit: a full commit SHA or the placeholder")


def semver(v: str) -> tuple[int, int, int]:
    m = _SEMVER.match(v)
    if not m:
        raise ValueError(f"not a release tag: {v!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def parse_yaml(text: str) -> Any:
    """YAML text to Python, with PyYAML or, without it, `yq -o=json`."""
    try:
        import yaml  # type: ignore
    except ImportError:
        yq = shutil.which("yq")
        if not yq:
            raise RuntimeError("neither PyYAML nor yq is available to read YAML")
        out = subprocess.run([yq, "-o=json", "."], input=text.encode(),
                             capture_output=True, check=True).stdout
        return json.loads(out)
    return yaml.safe_load(text)


def load(path_or_text: str | os.PathLike) -> dict:
    """The validated class file. Raises ClassFileError on unknown or missing keys."""
    if isinstance(path_or_text, os.PathLike) or (
            isinstance(path_or_text, str) and "\n" not in path_or_text and Path(path_or_text).is_file()):
        text = Path(path_or_text).read_text()
    else:
        text = str(path_or_text)
    cf = parse_yaml(text)
    errors: list[str] = []
    _check(cf, SCHEMA, "", errors)
    if isinstance(cf, dict):
        _semantic(cf, errors)
    if errors:
        raise ClassFileError(errors)
    return cf


def queue(cf: dict) -> list[dict]:
    """[{"position": 1, "module": ..., "version": ...}, ...] in queue order."""
    return [{"position": i + 1, "module": it["module"], "version": it["version"]}
            for i, it in enumerate(cf["queue"])]


def unfilled(cf: dict) -> list[str]:
    """Dotted paths of every value still equal to a placeholder."""
    found: list[str] = []

    def walk(v: Any, path: str) -> None:
        if isinstance(v, dict):
            for k, sub in v.items():
                walk(sub, f"{path}.{k}" if path else k)
        elif isinstance(v, list):
            for i, sub in enumerate(v):
                walk(sub, f"{path}[{i}]")
        elif isinstance(v, str) and v in PLACEHOLDERS:
            found.append(path)

    walk(cf, "")
    return found


def agent_bot_login(cf: dict) -> str:
    """The login GitHub gives the agent App's bot user."""
    return f"{cf['agent']['app']}[bot]"
