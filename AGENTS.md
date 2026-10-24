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

## vNext user-authorized scope (2026-10-25)

The current user explicitly asks to implement docs/vnext_spec.md, perform the full
2025-11-12/13 local acceptance plus an isolated Ubuntu student installation, and
merge only after acceptance. This authorizes the necessary orchestration, RMP API,
cache, comparison adapter and protected-baseline migration; it does not authorize
changes to the already-correct numerical/remaining-quantity/replay/LA semantics.

Production is run_hlp.py only: complete CSV1 ingest/group/emit, then single-side
CSV2, then CSV-only CSV3. The formal interval is now inclusive at BOTH ends:
data_start + 1h <= time <= data_end_exclusive - 1h. Raw fills use [start,end).
Only prepared-date diff files may be used; include needed buffer hours, never
outside-date or future-state rescue. Local source failures go into CSV diagnostics.
Fills gaps split, not invalidate. Known coin/direction errors affect their sequence;
unknown gaps close all open groups. Issue rows are excluded from business counts.

Each run has exactly six durable files: csv1/2/3.csv, run.log, progress.sqlite,
cache.sqlite. DELETE journals are temporary recovery data, never disable journaling
or delete hot journals for file-count cosmetics. No extra manifests, latest pointers,
source copies, lock files or exported raw-cache files. Do not delete user old runs.

Commit necessary caches, fsync full CSV records, then commit progress, then release
large results. Retain zero-size pending-remove orders only in internal cache. CSV2
has no full replay state. CSV3 opens no raw-data cache or original input directory.
True resumes unchanged input and preserves committed skipped/issues; False is a new
round after input repair. Incompatible code cannot force-resume old checkpoints.

31 protected numerical/replay/comparison/header source fragments remain identical
to e698b1c (header helper only renamed). tools/protected_engine.json replaces obsolete
whole-file guards because user-authorized interfaces and old CLI were changed.
See docs/PROTECTION_MIGRATION.md; never refresh these fragments merely to pass CI.
Historical validation files describe historical runs; write fresh evidence for this
version and clearly distinguish synthetic, real dataset, real ABCI and installation.
