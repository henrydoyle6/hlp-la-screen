# Project workflow

This repository contains the Ubuntu HLP screener, shared RMP extraction backend,
original order-book replay/comparison entry, tests and compact validation evidence.
The user-facing entry is `run_hlp.py`; editable deployment settings remain at its top.

## GitHub repository

Owner/repository: `henrydoyle6/hlp-la-screen`.
Remote: `https://github.com/henrydoyle6/hlp-la-screen.git`.
The user explicitly selected this account and requested a public repository so
classmates can read the code. Public read access does not authorize adding writers.

## Standing user authorization (2026-10-24)

The user explicitly requested creating this GitHub repository and pushing every
subsequent completed code update. After finishing a requested code change:

1. Run checks appropriate to the change; the full suite is
   `python -m unittest discover -s . -p 'test_*.py'` in the project environment.
2. Inspect the staged diff and ensure no raw data, caches, run outputs or secrets
   are included. Explicitly review newly added files.
3. Create a descriptive Git commit and push the completed update to the configured
   `origin`, normally branch `main`, without asking for routine commit/push approval.
4. Verify local HEAD equals the remote branch and report the commit and validation.

Do not silently turn ordinary editing into a background file watcher or schedule.
If authentication or network access blocks pushing, keep the local commit, explain
what remains, and never claim the remote was updated. Fetch and inspect unexpected
remote changes; never force-push or rewrite published history as a shortcut.
Use the user's configured Git credentials, never put tokens in remote URLs/files.

## Calculation invariants

Preserve existing episode rules, original diff replay, LA and exact comparison.
Apply the entire target block. Missing fills split episodes; missing replay data
skips affected episodes. Never rescue with a future snapshot or round mismatches.
Keep original snapshot + coin extraction routing and success-only cache commits.
Simulation must not mutate the historical book. Release episode book/LA results
only after successful CSV writes and flush, keeping needed manager cache entries.
Do not modify `test_order_book_replay.py` merely to make validation pass.

## Repository contents

Track source, tests, documentation and compact validation evidence only. Raw RMP,
fills, diff archives, CSV/JSONL run results, runtime caches, virtual environments
and private credentials belong outside this repository and remain ignored.
`SOURCE_SHA256.json` is generated archive metadata and is not versioned here;
Git commits identify the tracked source revision.

## Current CSV stages and prepared-date semantics (2026-10-24)

Production execution uses run_hlp.py -> csv_pipeline.py. CSV1 is the immutable,
completed episode source for CSV2. CSV3 consumes only CSV1 and CSV2, with no fills,
RMP, snapshot metadata discovery, or diff reader calls. Stage statuses are separate:
CSV1 grouped; CSV2 reconstruction ok/skipped; CSV3 final execution/LA status.
Dates describe prepared UTC days inclusive. Formal start is data start + one hour;
formal end is data end-exclusive - one hour (exclusive). Do not fetch outside hours.

Keep resumable output directories including result/.state and their latest pointer.
Commit progress only after successful fsync; verify committed prefixes and upstream
hashes; roll back only uncommitted tails. Do not accept corrupt checkpoints, force
mixed-version resumes, retry committed skipped rows silently, or overwrite prior runs.
Test crash recovery, forward replay-state restoration (including zero quantities),
and CSV3 with raw readers blocked when changing orchestration. RMP reading and the
original replay/LA/comparison engine remain protected for this refactor.
