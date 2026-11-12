# Promotion and fixture records

Each file here is one rung rise that a person signed: a promotion earned by
the frozen rule (`promotion-<id>.yaml`), or a rise that was not earned
(`fixture-<id>.yaml`). Each is added by a pull request the listed human
approves and merges; that approval is the signature. The owner then applies
the ruleset edit by hand, recorded before and after.

The detector reads the kind from each file and the merge time from its pull
request: it acts only on escapes after the latest record, and accepts the L2
ruleset only from the latest record until the next demotion. Fields and a
minimal example are in `docs/RECORDS.md`.
