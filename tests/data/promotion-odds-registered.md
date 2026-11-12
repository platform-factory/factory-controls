<!-- Copied verbatim from the M4 readiness note (platform-factory-concept, docs/build-log/claims-register.md, "Promotion odds"), registered 2026-11-11. tests/test_odds.py recomputes every cell. Do not edit: a change here is a change to the registered numbers. -->

**Promotion odds.** For independent changes that each fail, detectably, with
probability *p*, the chance that the frozen rule promotes the class: in M4,
where the streak must end within 22 items, and within 50 and 100 changes for
#24's comparison. The last column is the expected number of bad changes merged
unattended in M4's five L2 items, 1 − (1 − *p*)⁵, with demotion on the first
detected failure. It assumes the freeze lands before the next merge, which the
merge step's checks ensure. With no cap on changes, each promotion ends in one
detected bad unattended merge, after 1/*p* merges on average; the column is
M4's five-item cut of that. Changes in M4 are neither independent nor live,
and an undetected failure would add to the last column, up to five.

| *p* | earned promotion in M4 (within 22 items) | within 50 | within 100 | bad merges per promotion, 5 L2 items |
|---:|---:|---:|---:|---:|
| 1% | 83% | 99% | 100% | 0.05 |
| 2% | 69% | 97% | 100% | 0.10 |
| 5% | 39% | 82% | 98% | 0.23 |
| 10% | 15% | 46% | 78% | 0.41 |
| 13.9% | 6% | 25% | 51% | 0.53 |
| 20% | 2% | 8% | 19% | 0.67 |
| 30% | 0.1% | 0.8% | 2% | 0.83 |

The table was computed by the streak recurrence and agrees with #24's quoted
figures ("a class that fails 10% of the time is promoted within 50 changes 46%
of the time, and within 100 changes 78%"). The 50 and 100 columns cannot occur
in M4. No risk ceiling is claimed (ADR-0024 §8).
