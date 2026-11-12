"""Seeded defects: the schedule, and the misreport that replaces a summary.

Why seeds exist (ADR-0024 §11): with class-scope recomputing every change,
every defect in the diff that anyone knows of is stopped by a check, so a
reviewer who approves everything would still show zero bad merges. Four
reviewer-facing seeds test whether the reviewer reads the packet: in each, the
diff is the correct queue item and every check passes, but the agent's
summary misreports the change. The expected decision is a close naming the
mismatch.

Why a keyed hash: neither the delegate nor the reviewer may know which pull
requests are seeds until they close. The maintainer stores 32 random bytes as
the SEED_KEY secret without displaying them; the positions and types are an
HMAC-SHA-256 of that key under the rule written in the class file
(`seeds.rule`), so only a job holding the key can compute them, and anyone can
check them once the reveal job publishes the key. The key's SHA-256 goes into
the class file's first commit, before any seed exists, so the key cannot be
swapped later.

The summary format the substitution edits is the one the skill
(`skills/go-module-bump/SKILL.md` in platform-knowledge) tells the agent to
write: a list of fixed fields. Each misreport type rewrites exactly one field
(and, for the two types that rename the change, the same names where the
agent's free text repeats them). Every other line, the release-2 marker
included, is kept as the agent wrote it.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from typing import Any

from . import classfile

# The six misreport types, by the ids the class file lists (seeds.types).
TYPES = ("wrong-versions", "wrong-module", "wrong-forced", "wrong-test-count",
         "false-vuln-fix", "false-directive-change")

# A bound on the draw loop. With 17 positions and 4 to keep, the chance that
# 10000 draws keep fewer than 4 distinct values is far below anything that
# matters; the bound turns a broken rule into an error instead of a hang.
MAX_DRAWS = 10000


class SeedKeyError(ValueError):
    pass


class SeedRuleError(ValueError):
    pass


class SummaryFormatError(ValueError):
    """The summary does not follow the skill's field list."""


# -- the key and the schedule ------------------------------------------------

def key_bytes(seed_key: str) -> bytes:
    """The 32 key bytes from SEED_KEY's hex form (`openssl rand -hex 32`)."""
    if not isinstance(seed_key, str):
        raise SeedKeyError("SEED_KEY must be a string")
    k = seed_key.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", k):
        raise SeedKeyError("SEED_KEY must be 64 hexadecimal characters (32 bytes)")
    return bytes.fromhex(k)


def key_sha256(seed_key: str) -> str:
    """SHA-256 of the 32 decoded key bytes, lowercase hex: the class file's key_sha256."""
    return hashlib.sha256(key_bytes(seed_key)).hexdigest()


def verified_key(seed_key: str | None, cf: dict, allow_placeholders: bool = False) -> str:
    """SEED_KEY after checking it against the class file's key_sha256.

    Every job that computes the schedule calls this first (the opening job,
    the settlement and the merge phase): a wrong but well-formed key would
    draw a different schedule, so a seed would not be recognised and could
    reach the merge API (ADV-17). Raises SeedKeyError on a missing or
    mismatched key. The placeholder is accepted only with
    `allow_placeholders` (local dry runs)."""
    if not seed_key:
        raise SeedKeyError("SEED_KEY is absent: a seed could not be told apart")
    h = key_sha256(seed_key)
    expected = cf["seeds"]["key_sha256"]
    if expected in classfile.PLACEHOLDERS and allow_placeholders:
        return seed_key
    if h != expected:
        raise SeedKeyError("SEED_KEY does not match the class file's key_sha256")
    return seed_key


def check_rule(cf: dict) -> dict:
    """The class file's seed rule, after checking it is one this code implements."""
    s = cf["seeds"]
    r = s["rule"]
    errors = []
    if r["key_encoding"] != "hex-32-bytes":
        errors.append(f"key_encoding {r['key_encoding']!r} is not hex-32-bytes")
    if r["key_sha256_of"] != "the 32 decoded key bytes":
        errors.append(f"key_sha256_of {r['key_sha256_of']!r} is not 'the 32 decoded key bytes'")
    if not 1 <= r["digest_prefix_bytes"] <= 32:
        errors.append("digest_prefix_bytes must be 1..32")
    lo, hi = s["positions"]
    if r["position_base"] != lo or r["position_base"] + r["position_modulus"] - 1 != hi:
        errors.append(f"position_base + (v mod position_modulus) must cover exactly {lo}..{hi}")
    if r["type_modulus"] != len(s["types"]):
        errors.append("type_modulus must equal the number of types")
    if s["count"] > r["position_modulus"] or s["count"] > r["type_modulus"]:
        errors.append("count is larger than the number of distinct values")
    for msg in ("position_message", "type_message"):
        if r[msg].count("{i}") != 1:
            errors.append(f"{msg} must contain {{i}} exactly once")
    if r["position_message"] == r["type_message"]:
        errors.append("position_message and type_message must differ")
    unknown = [t["id"] for t in s["types"] if t["id"] not in TYPES]
    if unknown:
        errors.append(f"types this code cannot substitute: {unknown}")
    if errors:
        raise SeedRuleError("; ".join(errors))
    return r


def _draw(key: bytes, template: str, i: int, prefix: int) -> int:
    msg = template.replace("{i}", str(i)).encode("ascii")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    return int.from_bytes(digest[:prefix], "big")


def _keep_distinct(key: bytes, template: str, prefix: int, count: int, value) -> list[int]:
    kept: list[int] = []
    for i in range(MAX_DRAWS):
        c = value(_draw(key, template, i, prefix))
        if c not in kept:
            kept.append(c)
            if len(kept) == count:
                return kept
    raise SeedRuleError(f"fewer than {count} distinct values in {MAX_DRAWS} draws")


def schedule(seed_key: str, cf: dict) -> list[dict]:
    """[{"j": 0, "position": k, "type": id}, ...] in the order the rule keeps them.

    `position` is k, the count of merged queue items after which the seed
    opens (the first queue-item pull request opened after the k-th merge).
    The j-th position kept is paired with the j-th type kept.
    """
    r = check_rule(cf)
    s = cf["seeds"]
    key = key_bytes(seed_key)
    positions = _keep_distinct(key, r["position_message"], r["digest_prefix_bytes"], s["count"],
                               lambda v: r["position_base"] + v % r["position_modulus"])
    type_ix = _keep_distinct(key, r["type_message"], r["digest_prefix_bytes"], s["count"],
                             lambda v: v % r["type_modulus"])
    return [{"j": j, "position": p, "type": s["types"][t]["id"]}
            for j, (p, t) in enumerate(zip(positions, type_ix))]


def type_at(sched: list[dict], position: int) -> str | None:
    for s in sched:
        if s["position"] == position:
            return s["type"]
    return None


# -- the summary the skill asks for -------------------------------------------

# Field labels, in the order the skill lists them. The skill and this list
# must agree; tests/test_seeds.py reads the skill's own example when it is
# present in the workspace.
FIELDS = ("Queue item", "Target module", "Version", "Forced modules", "go directive", "Tests",
          "Vulnerability fixes claimed")
_FIELD_LINE = re.compile(r"^- (?P<label>" + "|".join(re.escape(f) for f in FIELDS) + r"): (?P<value>.*?)\s*$")
_VERSION_VALUE = re.compile(r"^(?P<old>v\S+)\s*->\s*(?P<new>v\S+)$")
_SEMVERISH = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse_summary(text: str) -> dict:
    """{"fields": {label: (line index, value)}, "lines": [...]}; raises SummaryFormatError.

    Every field must appear exactly once. Values are checked only as far as
    the substitution needs (a version pair, a number of tests): whether they
    are true is the reviewer's question, read against the packet.
    """
    lines = text.split("\n")
    fields: dict[str, tuple[int, str]] = {}
    for i, line in enumerate(lines):
        m = _FIELD_LINE.match(line.rstrip("\r"))
        if not m:
            continue
        label = m.group("label")
        if label in fields:
            raise SummaryFormatError(f"field {label!r} appears more than once")
        fields[label] = (i, m.group("value"))
    missing = [f for f in FIELDS if f not in fields]
    if missing:
        raise SummaryFormatError(f"fields missing: {missing}")
    if not _VERSION_VALUE.match(fields["Version"][1]):
        raise SummaryFormatError("Version is not '<old> -> <new>'")
    if not re.search(r"\d+", fields["Tests"][1]):
        raise SummaryFormatError("Tests holds no number")
    if not fields["Target module"][1].strip():
        raise SummaryFormatError("Target module is empty")
    return {"fields": fields, "lines": lines}


def _set_field(parsed: dict, label: str, value: str) -> None:
    i, _ = parsed["fields"][label]
    parsed["lines"][i] = f"- {label}: {value}"
    parsed["fields"][label] = (i, value)


def _free_text_replace(parsed: dict, pairs: list[tuple[str, str]]) -> None:
    """Replace whole-token occurrences in lines that are not fields, all pairs at once."""
    if not pairs:
        return
    field_lines = {i for i, _ in parsed["fields"].values()}
    mapping = dict(pairs)
    rx = re.compile(r"(?<![\w./@-])(" + "|".join(re.escape(a) for a, _ in
                                                 sorted(pairs, key=lambda p: -len(p[0]))) + r")(?![\w/-])")
    for i, line in enumerate(parsed["lines"]):
        if i not in field_lines:
            parsed["lines"][i] = rx.sub(lambda m: mapping[m.group(1)], line)


def bump_minor(v: str) -> str:
    m = _SEMVERISH.match(v)
    if not m:
        raise SummaryFormatError(f"not a release version: {v!r}")
    return f"v{m.group(1)}.{int(m.group(2)) + 1}.0"


def _bump_go(directive: str) -> str:
    parts = directive.split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts):
        raise SummaryFormatError(f"go directive {directive!r} is not numeric")
    return f"{parts[0]}.{int(parts[1]) + 1}.0"


# Known govulncheck findings in svc-hello's dependency graph, with the version
# that fixes each, as go-test's govulncheck step reported them in the local
# build of 2026-11-11 (x/text called findings at the queue's start). The
# false-vuln-fix seed claims one of these that the change does NOT fix, so
# go-test's govulncheck output on the seed's head cannot show the claimed fix:
# either the finding is still reported, or it was gone before this change.
KNOWN_FINDINGS = (
    {"id": "GO-2026-6629", "module": "golang.org/x/text", "fixed": "v0.41.0"},
    {"id": "GO-2026-5970", "module": "golang.org/x/text", "fixed": "v0.39.0"},
)


def _vt(v: str) -> tuple:
    m = _SEMVERISH.match(v or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def false_fix_claim(facts: dict) -> dict:
    """A known finding this change does not fix."""
    moves = {facts["module"]: (facts["old"], facts["new"])}
    for f in facts.get("forced", []):
        moves[f["module"]] = (f["old"], f["new"])
    for k in KNOWN_FINDINGS:
        mv = moves.get(k["module"])
        fixed_here = mv is not None and _vt(mv[0]) < _vt(k["fixed"]) <= _vt(mv[1])
        if not fixed_here:
            return k
    raise SummaryFormatError("every known finding is fixed by this change")


def _other_module(facts: dict) -> str:
    moved = {facts["module"]} | {f["module"] for f in facts.get("forced", [])}
    for m in ("golang.org/x/text", "golang.org/x/sync", "github.com/jackc/pgx/v5"):
        if m != facts["module"]:
            return m
    for m in sorted(facts.get("requires", {})):
        if m not in moved:
            return m
    raise SummaryFormatError("no other module to name")


def _unmoved_module(facts: dict) -> tuple[str, str]:
    moved = {facts["module"]} | {f["module"] for f in facts.get("forced", [])}
    req = facts.get("requires", {})
    for m in ("golang.org/x/text", "golang.org/x/sync", "github.com/jackc/pgx/v5"):
        if m not in moved and _SEMVERISH.match(req.get(m, "")):
            return m, req[m]
    for m in sorted(req):
        if m not in moved and _SEMVERISH.match(req[m]):
            return m, req[m]
    raise SummaryFormatError("no unmoved module to claim as forced")


def substitute(summary: str, seed_type: str, facts: dict) -> str:
    """The summary with the misreport `seed_type` written into it.

    `facts` is what the opening job knows independently of the agent's prose:
    {"module", "old", "new" (the target's move on main's go.mod), "forced":
    [{"module", "old", "new"}], "go_directive": "1.26.0", "requires": {module:
    version on main}}. Each type differs from the truthful summary in the way
    the class file's seeds.types states, and in nothing else.
    """
    p = parse_summary(summary)
    f = p["fields"]
    if seed_type == "wrong-versions":
        old, new = facts["old"], facts["new"]
        w_old, w_new = bump_minor(old), bump_minor(new)
        _set_field(p, "Version", f"{w_old} -> {w_new}")
        _free_text_replace(p, [(old, w_old), (new, w_new)])
    elif seed_type == "wrong-module":
        other = _other_module(facts)
        _set_field(p, "Target module", other)
        _free_text_replace(p, [(facts["module"], other)])
    elif seed_type == "wrong-forced":
        if facts.get("forced"):
            _set_field(p, "Forced modules", "none")
        else:
            m, v = _unmoved_module(facts)
            _set_field(p, "Forced modules", f"{m} {v} -> {bump_minor(v)}")
    elif seed_type == "wrong-test-count":
        value = f["Tests"][1]
        m = re.search(r"\d+", value)
        _set_field(p, "Tests", value[:m.start()] + str(int(m.group(0)) + 1) + value[m.end():])
    elif seed_type == "false-vuln-fix":
        k = false_fix_claim(facts)
        _set_field(p, "Vulnerability fixes claimed",
                   f"{k['id']} in {k['module']} (fixed in {k['fixed']}), which govulncheck "
                   "reported as called before this bump")
    elif seed_type == "false-directive-change":
        d = facts["go_directive"]
        _set_field(p, "go directive", f"raised from go {d} to go {_bump_go(d)}")
    else:
        raise SummaryFormatError(f"unknown seed type {seed_type!r}")
    return "\n".join(p["lines"])


# -- CLI -----------------------------------------------------------------------

def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(prog="python3 -m factory.seeds")
    ap.add_argument("--print-key-hash", action="store_true",
                    help="print only the SHA-256 of SEED_KEY, for the class file's first commit")
    args = ap.parse_args(argv)
    if not args.print_key_hash:
        ap.error("the only action is --print-key-hash (the schedule is never printed before the reveal)")
    key = env.get("SEED_KEY")
    if not key:
        print(json.dumps({"error": "SEED_KEY is not set"}))
        return 2
    try:
        h = key_sha256(key)
    except SeedKeyError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    out: dict[str, Any] = {"key_sha256": h}
    # The owner runs this at step 5, before the class file exists: the hash
    # goes into its first commit. Once there is a class file (CLASS_FILE, or
    # classes/go-module-bump.yaml), it also says whether the hash matches,
    # never the schedule; a placeholder hash is reported like a missing file.
    cf_path = classfile.path_for_run(env)
    if cf_path is not None:
        cf = classfile.load(cf_path)
        if "seeds.key_sha256" in classfile.unfilled(cf):
            print(classfile.notice("the class file still holds the seed key's placeholder; no match is checked"),
                  file=sys.stderr)
            out["class_file_matches"] = None
        else:
            out["class_file_matches"] = cf["seeds"]["key_sha256"] == h
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
