"""The clock: starts the class's timed workflows when GitHub does not.

Why: the detector, the agent's workflow and the scorecard each carry a cron
line, and in this organisation GitHub starts almost no run from one. Between
2026-11-15T22:05Z and 2026-11-16T03:45Z the detector's ten-minute cron and a
five-minute cron in a do-nothing probe workflow produced no `schedule` run
at all, while the same files ran when dispatched. By 2026-11-16T12:00Z each
had produced one: the detector at 07:13Z, of about seventy due, and the
probe at 11:43Z, of about a hundred due; the scorecard's daily line had
produced none at its hour. The documented causes (a disabled workflow, a
file off the default branch, sixty idle days, a fork, a suspended account)
do not apply, and GitHub's status page showed no incident. A second probe
showed that a job holding only `actions: write` can dispatch another
workflow, and itself.

So one long-running job keeps the time. It reads the cron line of each
workflow it is told about, sleeps, and at each minute a cron line names it
sends that workflow a `workflow_dispatch` on `main`. The cron lines stay
where they are and stay the single statement of the times. When its hours
are up the job dispatches this workflow again, with the time it had reached,
and ends; the next run starts from that time, so a minute that falls between
the two is still served.

It holds no App key and enters no environment. Its token can start and
cancel workflow runs in this repository and nothing else. Each workflow it
starts takes its own guard, its own environment and its own concurrency
group, exactly as on a cron or a hand dispatch.

What it cannot do: restart itself after a run that was cancelled or lost.
Then nothing runs until someone dispatches clock.yml again. That fails
safe: the merge step refuses to merge without a detector heartbeat younger
than thirty minutes, and it only runs when something starts it.

When GitHub does start a `schedule` run, that workflow runs twice at that
minute. The concurrency groups already put such runs in a line, and every
job is written to be run at any time.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import classfile
from .gh import ApiError, GitHub

ROOT = Path(__file__).resolve().parent.parent
SELF = "clock.yml"
STEP_SECONDS = 20          # how often the job looks at the time
HANDOFF_MAX_AGE = 15 * 60  # an older `since` is ignored: the chain was broken, not handed over
HANDOFF_TRIES = 5


class ClockError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_cron(expr: str) -> tuple[list[int], list[int] | None]:
    """(minutes, hours or None for every hour) of a five-field cron line.

    Only the forms this repository uses: a number or a comma list in the
    minute field; `*`, a number or a comma list in the hour field; `*` in
    the other three. Anything else is refused, so a cron line the clock
    cannot keep is found by the tests and at the job's start, not by a
    workflow that quietly never runs.
    """
    fields = str(expr).split()
    if len(fields) != 5:
        raise ClockError(f"cron {expr!r}: five fields expected")
    minute, hour, dom, month, dow = fields
    if (dom, month, dow) != ("*", "*", "*"):
        raise ClockError(f"cron {expr!r}: only `* * *` is kept in the last three fields")

    def numbers(field: str, top: int) -> list[int]:
        out = []
        for part in field.split(","):
            if not part.isdigit() or int(part) > top:
                raise ClockError(f"cron {expr!r}: {field!r} is not a list of numbers up to {top}")
            out.append(int(part))
        return sorted(set(out))

    return numbers(minute, 59), (None if hour == "*" else numbers(hour, 23))


def workflow_crons(name: str, root: Path = ROOT) -> list[tuple[list[int], list[int] | None]]:
    """Every cron line of one workflow file, parsed."""
    path = root / ".github" / "workflows" / name
    wf = classfile.parse_yaml(path.read_text())
    on = wf.get(True, wf.get("on")) if isinstance(wf, dict) else None   # YAML 1.1 reads `on` as true
    lines = (on or {}).get("schedule") if isinstance(on, dict) else None
    if not lines:
        raise ClockError(f"{name}: no cron line")
    return [parse_cron(line["cron"]) for line in lines]


def due(crons: list[tuple[list[int], list[int] | None]], after: datetime, upto: datetime) -> list[datetime]:
    """Each minute a cron line names, with after < minute <= upto (UTC)."""
    out = []
    t = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    upto = upto.astimezone(timezone.utc)
    while t <= upto:
        if any(t.minute in minutes and (hours is None or t.hour in hours) for minutes, hours in crons):
            out.append(t)
        t += timedelta(minutes=1)
    return out


def start_from(since: str | None, now: datetime) -> datetime:
    """Where this run takes up: the previous run's time when it is a real
    handover (parseable, not in the future, at most fifteen minutes old),
    otherwise now."""
    if not since:
        return now
    try:
        t = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return now
    if t > now or (now - t).total_seconds() > HANDOFF_MAX_AGE:
        return now
    return t


def dispatch(gh, repo: str, name: str, ref: str, inputs: dict | None = None) -> None:
    body: dict = {"ref": ref}
    if inputs:
        body["inputs"] = inputs
    gh.post(f"repos/{repo}/actions/workflows/{name}/dispatches", body)


def run(gh, repo: str, ref: str, targets: dict[str, list], *, since: str | None, run_seconds: int,
        now: Callable[[], datetime] = utcnow, sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print) -> datetime:
    """Keep the time for run_seconds, then hand over. Returns the time reached.

    A dispatch that GitHub refuses (a disabled workflow answers 4xx) is
    logged and the clock goes on: one workflow that cannot start must not
    stop the others. A handover that fails after five tries raises, so the
    run ends red and is seen.
    """
    started = now()
    last = start_from(since, started)
    log(f"clock: started {iso(started)}, taking up from {iso(last)}, for {run_seconds} s; "
        f"workflows: {', '.join(targets)}")
    end = started + timedelta(seconds=run_seconds)
    beat = started
    while True:
        t = now()
        for name, crons in targets.items():
            minutes = due(crons, last, t)
            if not minutes:
                continue
            try:
                dispatch(gh, repo, name, ref)
                log(f"clock: {iso(t)} dispatched {name} for {', '.join(m.strftime('%H:%M') for m in minutes)}")
            except ApiError as e:
                log(f"::warning::clock: {iso(t)} could not dispatch {name}: {e}")
        last = t
        if t >= end:
            break
        if (t - beat).total_seconds() >= 300:
            log(f"clock: {iso(t)} running")
            beat = t
        sleep(STEP_SECONDS)
    for attempt in range(1, HANDOFF_TRIES + 1):
        try:
            dispatch(gh, repo, SELF, ref, {"since": iso(last)})
            log(f"clock: handed over at {iso(last)}")
            return last
        except ApiError as e:
            log(f"::warning::clock: handover try {attempt} failed: {e}")
            if attempt < HANDOFF_TRIES:
                sleep(30)
    raise ClockError("the clock could not start its next run; dispatch clock.yml by hand")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--workflow", action="append", required=True,
                   help="a workflow file name under .github/workflows; its cron lines give the minutes")
    p.add_argument("--since", default="", help="the previous run's time (set by the handover)")
    p.add_argument("--run-minutes", type=int, default=300)
    p.add_argument("--ref", default="main")
    args = p.parse_args(argv)
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GH_TOKEN")
    if not repo or not token:
        print("clock: GITHUB_REPOSITORY and GH_TOKEN are required", file=sys.stderr)
        return 2
    try:
        targets = {name: workflow_crons(name) for name in args.workflow}
        run(GitHub(token), repo, args.ref, targets, since=args.since or None,
            run_seconds=args.run_minutes * 60, log=lambda line: print(line, flush=True))
    except ClockError as e:
        print(f"::error::{e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
