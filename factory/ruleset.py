"""Pure helpers for comparing rulesets and finding the rung entry.

Why a normal form: GitHub returns a ruleset with fields it assigns (id,
node_id, timestamps, links, current_user_can_bypass) and may return rules and
list members in an order other than the one sent. The comparison ADR-0024 §10
asks for is of enforcement, conditions, every rule and bypass_actors, not of
those. `normalize` keeps exactly the fields a PUT sends (name, target,
enforcement, conditions, rules, bypass_actors) and turns order-free lists into
mappings, so `diff` reports a change in meaning and nothing else.

Order that carries meaning is kept: a required-reviewers entry's
file_patterns, where a `!` pattern un-matches only the patterns before it.

Two rulesets, not one: the rung entry sits in the rung ruleset
(`main-protection`, l1.json and l2.json) and the wall entry in a ruleset of
its own (`class-wall`, wall.json). ADR-0024 §4 put both entries in one
ruleset, but GitHub refuses two required-reviewers entries for one team in
one ruleset ("Required reviewers must be unique", probe P1, 2026-11-12), and
the readiness note's P1 fallback is two rulesets. GitHub applies every
active ruleset on a branch together, so `wall_errors` checks that the wall
ruleset says nothing the rung ruleset does not, besides the wall entry.
"""

from __future__ import annotations

import copy
import json
from typing import Any

PUT_FIELDS = ("name", "target", "enforcement", "conditions", "rules", "bypass_actors")

# Stands in for a field GitHub did not return. bypass_actors is returned only
# to a token with write access to the ruleset; reading without it must count
# as a difference, never as "no bypass actor".
NOT_RETURNED = "<not returned>"


def put_body(ruleset: dict) -> dict:
    """The fields of a ruleset that a PUT sends, copied."""
    return {k: copy.deepcopy(ruleset[k]) for k in PUT_FIELDS if k in ruleset}


def reviewer_key(entry: dict) -> str:
    """Identity of a required-reviewers entry: its team and its patterns."""
    r = entry.get("reviewer", {})
    return json.dumps([r.get("type"), r.get("id"), entry.get("file_patterns")], separators=(",", ":"))


def _sorted_json(items: list) -> list:
    return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))


def _norm_params(rtype: str, params: dict) -> dict:
    p = copy.deepcopy(params)
    if rtype == "pull_request":
        if "required_reviewers" in p:
            entries: dict[str, Any] = {}
            for e in p["required_reviewers"]:
                k = reviewer_key(e)
                body = {kk: vv for kk, vv in e.items() if kk not in ("reviewer", "file_patterns")}
                if k in entries:  # the same team and patterns twice
                    k = k + "#dup"
                entries[k] = body
            p["required_reviewers"] = entries
        if "allowed_merge_methods" in p:
            p["allowed_merge_methods"] = sorted(p["allowed_merge_methods"])
        dr = p.get("dismissal_restriction")
        if isinstance(dr, dict) and isinstance(dr.get("allowed_actors"), list):
            dr["allowed_actors"] = _sorted_json(dr["allowed_actors"])
    if rtype == "required_status_checks" and "required_status_checks" in p:
        checks: dict[str, Any] = {}
        for c in p["required_status_checks"]:
            k = json.dumps([c.get("context"), c.get("integration_id")])
            checks[k] = {kk: vv for kk, vv in c.items() if kk not in ("context", "integration_id")}
        p["required_status_checks"] = checks
    return p


def normalize(ruleset: dict) -> dict:
    out: dict[str, Any] = {}
    for k in PUT_FIELDS:
        out[k] = copy.deepcopy(ruleset.get(k, NOT_RETURNED))
    if isinstance(out["conditions"], dict):
        for cond in out["conditions"].values():
            if isinstance(cond, dict):
                for kk, vv in list(cond.items()):
                    if isinstance(vv, list):
                        cond[kk] = sorted(vv, key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(out["bypass_actors"], list):
        out["bypass_actors"] = _sorted_json(out["bypass_actors"])
    if isinstance(out["rules"], list):
        rules: dict[str, Any] = {}
        for r in out["rules"]:
            t = r.get("type")
            k = t
            n = 2
            while k in rules:  # a rule type twice is itself a difference
                k = f"{t}#{n}"
                n += 1
            rules[k] = _norm_params(t, r.get("parameters", {})) if "parameters" in r else {}
        out["rules"] = rules
    return out


def diff(expected: Any, actual: Any, path: tuple = ()) -> list[dict]:
    """Every leaf where two normal forms differ: [{"path": [...], "expected", "actual"}]."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        out: list[dict] = []
        for k in sorted(set(expected) | set(actual)):
            if k not in expected:
                out.append({"path": list(path + (k,)), "expected": NOT_RETURNED, "actual": actual[k]})
            elif k not in actual:
                out.append({"path": list(path + (k,)), "expected": expected[k], "actual": NOT_RETURNED})
            else:
                out.extend(diff(expected[k], actual[k], path + (k,)))
        return out
    if expected != actual:
        return [{"path": list(path), "expected": expected, "actual": actual}]
    return []


class RungError(ValueError):
    pass


def rung_path(l1: dict, l2: dict) -> list:
    """The one normal-form path where L1 and L2 differ: the rung entry's minimum_approvals.

    Raises RungError unless they differ in exactly that one field, from 1 to 0.
    """
    d = diff(normalize(l1), normalize(l2))
    if len(d) != 1:
        raise RungError(f"l1 and l2 must differ in exactly one field, found {len(d)}: {d}")
    p = d[0]["path"]
    if not (len(p) == 5 and p[0] == "rules" and p[1] == "pull_request"
            and p[2] == "required_reviewers" and p[4] == "minimum_approvals"):
        raise RungError(f"l1 and l2 differ outside a required-reviewers entry's minimum_approvals: {p}")
    if not (d[0]["expected"] == 1 and d[0]["actual"] == 0):
        raise RungError(f"rung entry must be 1 at L1 and 0 at L2, found {d[0]}")
    return p


def find_rung_entries(body: dict, path: list) -> list[dict]:
    """The entries of a ruleset body (PUT or GET form) that are the rung entry."""
    key = path[3]
    found = []
    for rule in body.get("rules", []) if isinstance(body.get("rules"), list) else []:
        if rule.get("type") != "pull_request":
            continue
        for e in rule.get("parameters", {}).get("required_reviewers", []):
            if reviewer_key(e) == key:
                found.append(e)
    return found


def wall_errors(rung: dict, wall: dict, wall_minimum: int) -> list[str]:
    """Why a wall ruleset body does not layer cleanly with a rung ruleset
    body; empty when it does. The wall holds one `pull_request` rule whose
    parameters equal the rung ruleset's except `required_reviewers`, which
    holds one entry, at `wall_minimum`, for another set of patterns than the
    rung ruleset's one entry; the same target and conditions; active; no
    bypass actor. So the two together say what one ruleset with both entries
    would have said."""
    errors = []
    rung_pr = [r for r in rung.get("rules", []) if r.get("type") == "pull_request"]
    wall_rules = wall.get("rules", [])
    if len(rung_pr) != 1:
        return ["the rung ruleset must hold one pull_request rule"]
    if [r.get("type") for r in wall_rules] != ["pull_request"]:
        return ["the wall ruleset must hold exactly one rule, pull_request"]
    rp, wp = rung_pr[0].get("parameters", {}), wall_rules[0].get("parameters", {})
    rr, wr = rp.get("required_reviewers", []), wp.get("required_reviewers", [])
    if len(rr) != 1:
        errors.append(f"the rung ruleset must hold one required-reviewers entry, found {len(rr)}")
    if len(wr) != 1:
        errors.append(f"the wall ruleset must hold one required-reviewers entry, found {len(wr)}")
    elif rr and reviewer_key(wr[0]) == reviewer_key(rr[0]):
        errors.append("the wall entry has the rung entry's patterns")
    elif wr[0].get("minimum_approvals") != wall_minimum:
        errors.append(f"the wall entry's minimum_approvals is {wr[0].get('minimum_approvals')}, not {wall_minimum}")
    others = diff(_norm_params("pull_request", {k: v for k, v in rp.items() if k != "required_reviewers"}),
                  _norm_params("pull_request", {k: v for k, v in wp.items() if k != "required_reviewers"}))
    if others:
        errors.append(f"the pull_request parameters differ besides required_reviewers: {others}")
    for k in ("target", "conditions"):
        if rung.get(k) != wall.get(k):
            errors.append(f"the two rulesets' {k} differ")
    if wall.get("enforcement") != "active":
        errors.append("the wall ruleset's enforcement is not active")
    if wall.get("bypass_actors") != []:
        errors.append("the wall ruleset has bypass actors")
    return errors


def path_str(path: list) -> str:
    return "/".join(str(p) for p in path)
