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

The user requires this workflow for every completed project update:

**feature branch -> PR -> automatic tests and acceptance -> merge main -> version tag
and GitHub Release -> Ubuntu installs a specified released version.**

This replaces the previous direct-push-to-main policy. Routine branch creation,
commit, push, PR submission, merge after passing checks, tagging and publication
are authorized; do not repeatedly ask for routine permission.

1. Fetch main and work on a descriptive feature/fix/chore branch. Never push a
   completed change directly to main, bypass required checks, or force-push.
2. Run appropriate local checks, inspect all staged/new files, and push the branch.
   Create a PR to main and attach its URL to the task. Use the PR template.
3. Require `Ubuntu tests (3.10)`, `Ubuntu tests (3.12)` and `Acceptance` on the latest
   PR revision. Inspect failures and resolve discussions before squash merging.
   Do not claim historical real-data evidence was rerun by synthetic CI.
4. Bump VERSION and CHANGELOG in the PR for each new release. The first baseline is
   0.1.0; subsequent versions must be new semantic versions. Never move an existing tag.
5. Wait for CI on the exact merged main commit. Create an annotated vX.Y.Z tag
   matching VERSION on that tested commit, then push the tag. The Release workflow
   reruns validation and verifies main ancestry + successful main CI before publishing.
6. Verify the published Release, commit, source archive and SHA256SUMS. Only then
   recommend that explicit version for Ubuntu; production must not follow main.
   Keep outputs/caches outside the version directory, copy only local config values,
   and never force incompatible checkpoints across code versions.

See RELEASE.md for commands and migration rules. Do not create background watchers
or schedules from this standing policy. If authentication/CI/network blocks a step,
report the exact unfinished step and never claim merge or release succeeded.
Use configured credentials without writing tokens to URLs, files, or logs.
The user's request to preserve the RMP readers and original calculation engine
remains in force. Only explicit authorization to change a protected component may
justify updating its baseline, with precise comparison evidence in the PR.

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
