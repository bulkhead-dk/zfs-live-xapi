# Changelog

All notable changes to `zfs-live` are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning 2.0](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-05-17

### Added

#### Driver

- **SMAPIv3 zvol-backed VDIs** with native ZFS property management (compression, copies, sync, primarycache, logbias) via `Volume.set` / `Volume.unset`; immutable properties (volblocksize, provisioning) rejected with current value. (#1, foundational)
- **Thick & thin provisioning** controlled via `refreservation` (>0 → thick, 0 → sparse zvol).
- **Instant snapshots & fast clones** via ZFS `snapshot` / `clone`; same-pool clones are CoW-shared until divergence.
- **Persistent CBT (Changed Block Tracking)** via QEMU dirty bitmaps with on-disk metadata under the SR mount at `<sr-mount>/.zfs-live/cbt/<vdi-uuid>.pickle`. Recovery survives `zfs umount`/`mount`, `xe pbd-cycle`, and `qemu-dp` restart. (#30 / PR #95, #100 / PR #101, #102 / PR #103, #104 / PR #105, #106 / PR #107, #117 / PR #118, #119 / PR #120, #110 / PRs #122 + #125, #126 / PR #127)
- **Datapath plugin** (`raw+qdisk`) with `qemu-storage-daemon` lifecycle, QMP protocol client, NBD socket fd-passing for inbound storage migration (SXM), CBT primitive bridging.
- **Crash recovery** via `zfs_live_vdi_sanitize()` — recovers interrupted resize operations where vsize is `None` in the metabase by querying ZFS directly.
- **Orphan detection** in `SR.ls` — scans for zvols not in the metabase, checks `fuser` and datapath pickle files to avoid flagging in-flight operations.
- **Structured-signal busy-retry path via `libzfs_core` ctypes bindings** (`src/volume/lzc.py`). Six phases of #246 landed: `lzc_destroy` (recursive + non-recursive) for dataset / zvol / snapshot teardown, `lzc_snapshot` + `lzc_clone` for the snapshot/clone primitives, and `lzc_create` (with `volsize` / `volblocksize` typed-uint64 nvlist) for the no-overrides thin-zvol create path. Retries classify on the kernel errno (`EBUSY`) directly instead of stderr substring matching — locale-independent by construction. Each migrated operation has its own per-op gate (`destroy_*` / `snapshot_*` / `clone_*` / `create_zvol_*` available()) so a host with a partial `libzfs_core` build degrades only the missing operation rather than the whole `lzc` surface. CLI fallback retained on every call site for older OpenZFS userland. Phase 7 (string-property overrides on `lzc_create` + `lzc_set_props`) was attempted and rolled back — PROP_TYPE_INDEX properties (`compression`, `sync`, `primarycache`, `logbias`) need a project-side property-name → uint64-enum mapping module before they can take the structured-signal path, and `lzc_set_props` is not an upstream `libzfs_core` symbol. The findings are pinned in lzc-migration "Phase 7 — rolled back; needs a property-validation layer first" so the next attempt has the foundation. (#246 / PRs #247, #248, #249, #250, #251, #252; phase 7 rollback record from PR #255)
- **OpenZFS feature detection at `SR.attach`** (`src/volume/zfs_features.py`). Per-SR sidecar at `/var/run/sr-mount/<sr-uuid>/zfs_features.json` captures the live `zfs version` and `zpool get all` feature flags (compression algorithms, encryption, block cloning, etc.) so the rest of the stack can answer capability questions without re-shelling. (#230 / PR #235)
- **Dynamic `Plugin.Query` + `SR.stat` capability surfaces** — `Plugin.Query`'s response now includes `binary_compression_algorithms`, `binary_default_compression`, `binary_supports_encryption`, and `binary_supports_block_cloning` config keys derived from the live `zfs_features.json` sidecar; `SR.stat`'s `health[1]` free-form string carries the per-pool capability summary (`zfs=2.1.15; on=[encryption, large_blocks, lz4, zstd]; off=[block_cloning]`). Operators reading either surface get an at-a-glance view of what the pool supports without `zpool get all`. (#231 / PR #236)

#### Per-plugin proxy — runs alongside native `xapi-storage-script`

- **Volume + datapath dispatch** (dispatcher). Per-plugin proxy registers on the `org.xen.xapi.storage.zfs-live` and `raw+qdisk` message-switch queues, closing dispatch gaps the native OCaml daemon can't handle (`VDI.similar_content`, `VDI.copy`, `VDI.add_to_sm_config`, `SR.scan2`, `DP.attach_info`). The native daemon continues handling all other plugins. (#82 / PR #83, #84 / PR #85, #283 / PR #285)
- **Phase 3 — `xe` CLI wrapper** for same-host `xe vdi-copy`. Routes through `Volume.copy` (~3s vs. ~12-15s via `sparse_dd`). (#86 / PR #87)
- **Phase 4 — Cross-host `xe vdi-copy`** via SSH-tunnelled `zfs send | zfs receive`, with automatic `Volume.import_existing` registration on the destination SR. (#88 / PR #93)
- **Phase 5 — `VDI.list_changed_blocks` shim**. (#116 / PR #121)
- **`SR.stat` SMAPIv2 translator** narrowed to the `Healthy` variant only — `Recovering`/`Unreachable`/`Unavailable` carry diagnostic strings. (#123 / PR #124)
- **`xapi_compat` startup probe** (`src/shim/xapi_compat.py`) — scans `/opt/xensource/bin/xapi` for recognised `VDI_*` feature strings and writes `/var/run/xapi-storage-script-shim/xapi_compat.json` with a `gaps[<method>]` boolean per `KNOWN_GAPS` entry. The probe inspects the `xapi` binary only; it does not inspect `xapi-storage-script`, so it cannot signal when an `xapi-storage-script` dispatch binding lands upstream. (#229 / PR #234)
- **Graceful shim retirement** for the `xe vdi-copy` / `VDI.copy` fast path — when `xapi_compat`'s probe reports `VDI.copy` as filled, the `xe` CLI wrapper's fast path (`src/shim/xe_wrapper.py`'s `is_gap_filled("VDI.copy")` check) lets the call go straight to the upstream `xe` binary instead of intercepting. **Note:** the dispatcher-side `VDI.list_changed_blocks` shim does **not** auto-retire — that surface depends on an `xapi-storage-script` binding the probe can't see, so retiring its gap-filler is a manual operator decision once an upstream `xapi-storage-script` release adds `S.VDI.list_changed_blocks` (see `Known issues` below + `docs/known-limitations.md` §3). The `xe_wrapper` also gains the same import-fallback ladder as `sr_metadata` so it degrades to the upstream `xe` binary rather than crashing on a partial-build host. (#232 / PR #237)

#### Operator automation — `tools/ansible/`

- **sr-create.yml / sr-destroy.yml** — declarative SR lifecycle with `vdi_defaults` dict expansion, idempotency contract (no-op on match, fail-fast on name+different-dataset collision), real `force=true` semantics (loops `xe vdi-destroy` before `xe sr-destroy`). (#139 / PR #140)
- **backup-vdi.yml / migrate-vdi.yml** — Ansible wrappers around zfs-backup and zfs-migrate. migrate-vdi.yml has optional `register_to_sr=<uuid>` that auto-runs `Volume.import_existing` + `xe sr-scan` so the migrated zvol becomes a first-class XAPI VDI without manual `xe vdi-introduce`. (#143 / PR #144)
- **deploy.yml orchestrator** — chains `upgrade-preflight` → `install-driver` → `smoke-test` → `sr-create` for one-command host bring-up with skip-flag matrix and plan summary. (#145 / PR #146)
- **teardown.yml orchestrator** — symmetric reverse chain (`sr-destroy` → `install-driver` remove). (#147 / PR #148)

#### Release engineering

- **License + README** — source-available with free personal/noncommercial use, revenue-tiered commercial licensing, 270-day commercial evaluation. (#15 / PR #75)
- **Public-canonical release topology** — `git.bulkhead.dk/storage/zfs-live` is the canonical public location; `github.com/bulkhead-dk/zfs-live` is a read-only push-mirror. README banner, `CONTRIBUTING.md` redirect, PR-redirect Action on the GitHub mirror. (#77 / PR #91)
- **CI test pipeline** — `pytest`, `shellcheck`, `ruff`, `ansible-lint` jobs on every push and PR targeting master. (#21 / PR #128, #129 / PR #132, #157 / PR #158)
- **GitHub Releases auto-mirror** — mirror-releases.yml extracts the `[X.Y.Z]` block from `CHANGELOG.md` via `tools/extract_changelog_block.py` and posts via `gh release create` when the tag arrives at the mirror. (#92 / PR #133)
- **`CITATION.cff`** for academic / institutional citation; pre-release placeholders (`version: "0.0.0-pre"`, today's `date-released`) bumped at tag-time per the runbook. (#179 / PR #180)
- **`SECURITY.md`** — vulnerability-disclosure policy (response SLAs, supported-line table). Pre-tag, the supported line is `master` only; the table re-issues with the stable-line policy once a tagged release exists. (#173 / PR #174)

#### Developer tooling

- **`Makefile`** — single entry point for `make test` / `make lint` / `make pre-commit` plus operator-workflow wrappers (`make HOST=... deploy` / `teardown` / `smoke-test` / etc.). Underlying tools stay directly invokable. (#167 / PR #168)
- **`Pipfile`** — pinned dev dependencies (`pytest`, `ruff`, `ansible-lint`, `pre-commit`, `ansible`) so contributors get the CI-matching toolchain in one `pipenv install --dev`. `Pipfile.lock` deliberately gitignored. (#181 / PR #182)
- **`.editorconfig`** — cross-editor indent / line-ending / trailing-whitespace defaults per file pattern. (#169 / PR #170)
- **`.gitattributes`** — line-endings normalise to LF on commit + checkout regardless of editor or local git config. (#186 / PR #187)
- **`installer.sh`** gains `--help` / `-h` / `help` action with usage output and exit `2` on unknown actions with helpful stderr message. (#190 / PR #191)

#### Documentation

- **`docs/`** — comprehensive reference: ZFS feature deep-dives (compression, dedup, scrub-self-healing, snapshots, send-receive design), tuning + properties, storage-driver comparison, SR creation flow, performance benchmarks, troubleshooting, upgrade guide, UEFI/NVMe limitation, upstream limitations ledger, XAPI mapping, XO backup verification.
- **`docs/faq.md`** — common newcomer questions on architecture, deployment, comparison, operations, project. (#192 / PR #193)
- **`docs/troubleshooting.md`** *Ansible Operator Workflow* section — pre-flight failures, install-rollback path, smoke-test orphan-checks, orchestrator skip-flag confusion, `migrate-vdi register_to_sr=...` quirks. (#163 / PR #164)
- **`docs/known-limitations.md` §3** — `VDI_CONFIG_CBT` / `VDI.list_changed_blocks` dispatch gap, with reproduction recipe and shim-side workaround pointer. (#196 / PR #197)
- **`docs/installation.md`** — corrected stale claim that SMAPIv3 plugins do not appear in `xe sm-list type=zfs-live`; on XCP-ng 8.3 they do once `xapi-storage-script` discovers them. (#159 / PR #160)
- **`CONTRIBUTING.md`** — contributor flow, CI gates, local-run recipes, pre-commit + Make-targets onboarding.
- **Pre-v1.0 doc link-rot + freshness sweep** — narrative drift around closed `#115` realigned across `docs/known-limitations.md` §3 (rewritten as "residual `xapi-storage-script` dispatch-wiring gap" instead of the closed `xapi`-side recognition gap), `docs/backup-integration.md`, cbt-no-writeback-design, shim, upstream-patches. Audit log committed under 2026-05-07-pre-v1.0-doc-link-rot. (#241 / PR #253)
- **`drafts/announcements/v1.0/`** — pre-written launch posts for the four channels listed in `#26`'s Announcements section (XCP-ng forum, LinkedIn, HN, shittrix-origin), each with a `<!-- POST-CHECKLIST -->` block for the operator to verify before posting. (#258 / PR #259)
- **Vendor-neutral positioning** across operator-facing docs — pre-v1.0 sweep that neutralised vendor-specific framing in the README + `docs/` tree, narrowing claims to what the driver actually delivers across the supported XAPI surface (XCP-ng + Citrix XenServer per epic #228). (#219 / PR #225)
- **Removed business-strategy docs** from `docs/` — pre-v1.0 cleanup of competitive-landscape, licensing-philosophy, and product-strategy. Those were marketing / monetisation analysis pages that didn't belong in operator-facing reference docs; the licensing decision recorded in `LICENSE.md` + `README.md` (#15 / PR #75) is the durable record, and `docs/faq.md` carries the operator-relevant licensing summary. (#226 / PR #227)
- **TODO / FIXME audit log** under 2026-05-05-todo-fixme — pre-v1.0 sweep of every `TODO` / `FIXME` marker in the source tree; one row resolved (the `dataset_is_mounted` bytes/str comparison caught alongside the #246 phase 3 walk), one tracked downstream (#243), five documented as false positives (intentional contract markers in the libcow + datapath surfaces). (#240 / PR #244)

### Fixed

- **SSH `Permanently added '...'` warning noise** during e2e harness runs. (#67 / PR #90)
- **`cbt_list_bitmaps` blockdev probe** — was returning `[]` for the new `--blockdev` qemu-dp config; now walks `query-named-block-nodes`. Caught by the lab harness's qemu-dp restart subtest. (#126 / PR #127)
- **PR #122's `[Unreleased]` overclaim** — narrowed to the on-disk durability half of #110; the cross-host recovery half landed via PR #125.
- **PR #124's `SR.stat` translator** — initially collapsed all states to unit-payload; narrowed to `Healthy` only, preserving diagnostic strings on `Recovering`/`Unreachable`/`Unavailable`.
- **`zfsutils.is_busy_error()` interim hardening** (zfsutils) — centralised the EBUSY-detection logic into one helper instead of inline substring matching at each `call_retry` site, widened the pattern set to include the canonical libc `strerror(EBUSY)` line (`Device or resource busy`) plus the ZFS-specific `is busy` / `resource busy` variants, preserved the `rc=2` short-circuit (CLI-invocation errors don't retry). Stepping stone toward the structured-signal path of #246; the helper deletes once the last `call_retry`-busy-retry caller migrates to `lzc`. (#243 / PR #245)

### Known issues

- **UEFI Linux VMs hang at boot when a `zfs-live` boot disk is attached at userdevice 0–3.** Where the guest supports Xen PV `blkfront`, moving the boot disk to userdevice 4 or higher clears the hang. Behaviour appears consistent with an upstream XCP-ng / QEMU / OVMF issue; root-cause attribution is under investigation. (#16)
- **`xe vm-migrate ... vdi:` (live storage migration)** of a running VM between local `zfs-live` SRs hits an upstream `xapi-storage-script` bug on XCP-ng 8.3 that returns `Unimplemented: VDI.similar_content` from its OCaml dispatch layer without invoking the driver's Python script. The driver-side acceptance criteria are met; the offline `xe vdi-copy` cross-host path and XO's backup/restore pipeline both work end-to-end through the same plumbing. See [`docs/known-limitations.md` §1](docs/known-limitations.md). (#78 / upstream tracker #80)
- **`xe vdi-copy` between `zfs-live` SRs** routes through the installed CLI wrapper for native ZFS `send`/`receive` (same-pool clone, cross-pool send/receive) because upstream `xapi-storage-script`'s OCaml feature-name table doesn't recognise `VDI_COPY` and never dispatches to the driver script natively. Without the wrapper, xapi falls back to `sparse_dd` (~15s vs. ~1s for a 100 MiB VDI). The wrapper is installed automatically by `installer.sh` (Phase 3: #86 / PR #87; Phase 4 cross-host: #88 / PR #93). See [`docs/known-limitations.md` §2](docs/known-limitations.md). (Upstream tracker #89)
- **`VDI.list_changed_blocks` operator surface** depends on the Phase 5 shim (#116 / PR #121). Re-verification on canonical XCP-ng 8.3 (`xapi-core-25.6.0-1.7.xcpng8.3`) on 2026-05-05 showed `xapi`'s `sm_features.ml` parser does recognise `VDI_CONFIG_CBT` end-to-end (#115 closed without an upstream patch); the residual gap is the missing `xapi-storage-script` `S.VDI.list_changed_blocks` binding, which the Phase 5 shim covers. Retiring the shim is a manual operator decision once an `xapi-storage-script` release adds the binding — `xapi_compat`'s probe (#229) inspects the `xapi` binary only and cannot signal automatic retirement for this gap. See [`docs/known-limitations.md` §3](docs/known-limitations.md).