# SUBSTRATE-7 -- Session Highlights (2026-05-19 -> 2026-05-23)

## Headline Numbers

- **PRs merged:** 8 (#332 pending, #334, #337, #338, #339, #342, #343 + 3 direct master commits)
- **Issues filed:** 5 (#333, #335, #336, #340, #341)
- **Issues closed:** 6 (#316, #317, #333, #335, #340, #341)
- **Open issues at end:** 5 (#80, #89, #284, #298, #336)
- **Test suite:** 537 passed, 1 skipped (24 fuzz tests added)
- **v1.0.1 shipped** to internal Gitea, public Forgejo, and GitHub
- **Bugs found by static analysis:** 4 (all fixed in v1.0.1)

---

## What Shipped

### Pre-commit Hooks -- 15 Linters Wired (PR #337, issue #335)
- Added 7 new hooks: black, pylint, mypy, bandit, yamllint, markdownlint, codespell
- Retained 8 existing: trailing-whitespace, end-of-file-fixer, check-yaml, check-merge-conflict, check-added-large-files, ruff, shellcheck, ansible-lint
- markdownlint uses language_version: "20.19.1" for automatic Node 20 via nodeenv
- All tools pipenv-managed, CONTRIBUTING.md and Pipfile updated
- New config files: .yamllint, .markdownlint.yaml, .codespellrc
- Codespell typos fixed: re-use->reuse, invokable->invocable
- 6 review rounds

### Fuzzing Harnesses -- 24 Property Tests (PR #338, issue #316)
- hypothesis-based fuzz testing across all 4 driver input surfaces
- SR config parsing: 3 tests (~1200 inputs/seed)
- Volume API (set/unset/create/resize): 7 tests incl. thick provisioning
- Datapath API (attach/detach/activate/deactivate): 5 tests + missing-device error path
- xe wrapper args + config value helper: 4 tests
- Subprocess isolation to prevent xapi stub leakage
- Real API method calls via types.ModuleType stubs for class inheritance
- Fuzz report: docs/fuzz-report-2026-05-22.md (6 seeds, ~51k inputs, 0 findings)
- 8 review rounds

### Static Analysis -- pyright + semgrep + tightened mypy (PR #339, issue #317)
- pyright 1.1.x (basic mode): 0 errors, 81 warnings
- semgrep (auto + security-audit): 0 findings (5 pickle suppressions)
- mypy tightened: check_untyped_defs, warn_return_any, warn_redundant_casts, warn_unused_ignores
- Both tools wired into pre-commit (17 hooks total)
- Static analysis report: docs/static-analysis-report-2026-05-22.md
- Formal verification evaluated and deferred (property testing provides sufficient confidence)
- 4 review rounds

### Bug Fixes Found by Static Analysis (PR #343, issue #340)
- 4 bugs in zfs send|recv pipeline error handlers
- recv_proc/recv possibly-unbound in exception handlers (NameError if Popen fails)
- send_proc.stdout/send.stdout None access without guard (AttributeError)
- Properly tracked: findings reverted from tooling PR, filed as separate issue, fixed in dedicated PR
- E2E verified on xcpng-target-4 (VDI lifecycle + VDI copy + zfs send|receive integrity)
- 1 review round

### v1.0.1 Patch Release
- Tag on internal Gitea at d70c662
- Public tree pushed to Forgejo + GitHub with clean diff
- Only the 4 bug fixes + type:ignore cleanup in the source diff
- CHANGELOG.md and CITATION.cff updated

### README Pricing Fix (PR #334, issue #333)
- Stale pricing table in README.md (old tiers: €500-€25k, wrong breakpoints)
- Fixed to match LICENSE.md (€2,500-€100k, correct breakpoints)
- public-templates/README.md now uses <!-- PRICING_TABLE_FROM_LICENSE --> marker
- prepare-public-tree.sh injects pricing from LICENSE.md at build time
- Single source of truth for pricing across all surfaces
- 1 review round

### Publish Infrastructure Fixes
- prepare-public-tree.sh: removed find -exec rm -rf that nuked .env and local files
- Now uses git rm -rf (tracked files only) + explicit git add from TMPDIR
- PUBLISH.md created with full publish runbook including GitHub deep-dive
- Hotfix publish workflow documented (same version tag, branch-only push)

---

## Bugs Found & Fixed

| PR | Bug | Severity | How Found |
|----|-----|----------|-----------|
| #343 | recv_proc.kill() on unbound variable in xe_wrapper exception handler | Medium | pyright reportPossiblyUnboundVariable |
| #343 | send_proc.stdout.close() without None guard in xe_wrapper | Low | pyright reportOptionalMemberAccess |
| #343 | recv.kill() on unbound variable in zfs_operations exception handler | Medium | pyright reportPossiblyUnboundVariable |
| #343 | send.stdout.close() without None guard in zfs_operations | Low | pyright reportOptionalMemberAccess |
| master | prepare-public-tree.sh nuking .env and all local files on every publish | High | user report (data loss) |

---

## Process Improvements

### Findings Must Not Be Bundled with Tooling PRs
Bug fixes found by static analysis were initially bundled in the tooling PR (#339). Reverted and re-done properly: findings documented in the report, code fixes tracked in a separate issue (#340) with their own PR (#343). The audit trail is now clean: analysis report -> issue -> fix PR -> E2E verification.

### Publish Script No Longer Destructive
The prepare-public-tree.sh script was silently destroying untracked local files (.env, SUBSTRATE-*.md, tmp-scripts/) on every publish. Fixed by replacing `find -exec rm -rf` with `git rm -rf` (tracked files only) and explicit `git add` from the TMPDIR contents.

---

## Test Infrastructure

- 17 pre-commit hooks (all pass)
- 24 hypothesis property tests (~8500 inputs/run)
- pyright (0 errors, 81 warnings), semgrep (0 findings), mypy (0 errors)
- E2E verified on xcpng-target-4: full VDI lifecycle, VDI copy integrity, zfs send|receive pipeline

---

## Remaining Open Issues

| # | Title | Category |
|---|-------|----------|
| 336 | CI pipeline -- run all linters on PR creation | infra |
| 298 | Rebuild xenopsd from source with SXM NBD patch | SXM post-v1.0 |
| 284 | upstream: Storage_migrate hardcoded for tapdisk | upstream tracker |
| 89 | upstream: route VDI.copy through SM driver | upstream tracker |
| 80 | upstream: wire VDI.similar_content dispatch | upstream tracker |

Plus PR #332 (upstream issue draft for #284) still in review.

v1.0.1 shipped. Zero launch blockers.
