# ZFS-Live — ZFS SMAPIv3 Storage Driver for XAPI

[![License](https://img.shields.io/badge/license-source--available%20(Moksha)-555555)](LICENSE.md) &nbsp;
[![Citation](https://img.shields.io/badge/cite-CITATION.cff-1f6feb)](CITATION.cff)

> **Canonical home: [`git.bulkhead.dk/storage/zfs-live`](https://git.bulkhead.dk/storage/zfs-live).** Issues and merge requests are tracked there. The `github.com/bulkhead-dk/zfs-live` repo is a read-only mirror.

A zvol-backed block storage driver for XAPI-compatible hypervisors that exposes native ZFS features (compression, copies, snapshots, clones) through the SMAPIv3 Volume API.

## Features

- **zvol-backed VDIs** — each VDI is a ZFS zvol named by UUID
- **Native ZFS properties** — compression, copies, sync, primarycache, logbias configurable per-VDI
- **Thick & thin provisioning** — controlled via `refreservation`
- **Instant snapshots & fast clones** — via ZFS snapshot/clone
- **Raw-qdisk datapath** — direct zvol access through qemu-dp
- **Changed Block Tracking (CBT)** — via QEMU dirty bitmaps
- **Crash recovery** — sanitize pass repairs interrupted resize operations
- **Native daemon dispatch** — symlinks into xapi-storage-script for SM registration and dispatch
- **xe CLI wrapper** — native ZFS send/receive for `xe vdi-copy` between zfs-live SRs

## Quick Start

md](INSTALL.md) guide.

```bash
# Copy driver to host and run installer
scp -r . xcpng:/tmp/zfs-live/
ssh xcpng 'bash /tmp/zfs-live/installer.sh'

# Create an SR on an existing ZFS pool
xe sr-create type=zfs-live name-label='ZFS SR' device-config:path=<pool-name>
```

An [Ansible install playbook](tools/ansible/install-driver.yml) is included for
automated deployment. Create an inventory file for your hosts and run:

```bash
ansible-playbook tools/ansible/install-driver.yml -i your-inventory.ini -l <host>
```

## Documentation

- [docs/quickstart.md](docs/quickstart.md) — get running in five minutes
- [docs/installation.md](docs/installation.md) — full install, update, and verify guide
- [docs/configuration.md](docs/configuration.md) — SR defaults, per-VDI properties, provisioning
- [docs/compression.md](docs/compression.md) — algorithm comparison, benchmarks, recommendations
- [docs/snapshots-and-clones.md](docs/snapshots-and-clones.md) — snapshot and clone workflows
- [docs/backup-integration.md](docs/backup-integration.md) — Xen Orchestra backup, CBT, export/import
- [docs/tuning.md](docs/tuning.md) — volblocksize, ARC, L2ARC, ZIL, recordsize guidance
- [docs/troubleshooting.md](docs/troubleshooting.md) — common issues and diagnostic commands
- [docs/known-limitations.md](docs/known-limitations.md) — platform constraints and workarounds
- [docs/uninstall.md](docs/uninstall.md) — remove the driver safely
- [docs/faq.md](docs/faq.md) — frequently asked questions

## Known Limitations

- **`xe vm-migrate ... vdi:` (live storage migration / SXM) is fully implemented in the driver but blocked by upstream xapi limitations.** `Storage_migrate.MigrateLocal.start` hardcodes `tapdisk_of_attach_info` which does not recognise qemu-storage-daemon backends. All driver-side SXM methods are implemented and tested. When the upstream patches land, SXM works automatically with no driver update.
- **`xe vdi-copy` between zfs-live SRs routes through the CLI wrapper** for native ZFS `send`/`receive` instead of xapi's `sparse_dd` fallback. The wrapper ships with the driver and is installed automatically.

## Architecture

The driver implements SMAPIv3 Volume API v5:

| File | Role |
|------|------|
| `src/volume/plugin.py` | Plugin.Query — declares driver capabilities |
| `src/volume/sr.py` | SR operations — create, destroy, attach, detach, ls, stat |
| `src/volume/volume.py` | Volume operations — create, destroy, resize, snapshot, clone, set/unset |
| `src/volume/zfs_operations.py` | ZFS CLI wrappers and utility functions |
| `src/volume/zfs_live.py` | Module callbacks, URI prefix helpers |
| `src/volume/libcow/` | CoW image format extensions |
| `src/datapath/` | Datapath plugin for raw zvol access via qemu-dp |

## License

Source-available, with free personal/noncommercial use and a paid commercial license. See [LICENSE.md](LICENSE.md) for the full terms:

- **Free for personal, educational, and noncommercial use** — homelabs, academic research, and any deployment that is not by or on behalf of a commercial entity.
- **270-day commercial evaluation** — try the driver in production-like conditions before committing to a license. No PO required during evaluation.
- **Commercial use requires a commercial license** from Moksha. *Commercial use* means any deployment by or on behalf of a commercial entity (corporation, partnership, government agency, or nonprofit operating commercial infrastructure). Pricing is revenue-tiered (per organisation, per year, unlimited hosts/VMs/sockets, no audits):

  | Annual revenue | License fee |
  |---------------|-------------|
  | Under €1M | Free |
  | €1M - €10M | €2,500/year |
  | €10M - €100M | €10,000/year |
  | €100M - €1B | €50,000/year |
  | €1B - €10B | €100,000/year |
  | Over €10B | Contact us |

**Contributions** are accepted under the same license terms as the project (inbound = outbound). By opening a pull request you agree your contribution is licensed under the terms of LICENSE.md.

For commercial licensing inquiries, contact `jakob@wolffhechel.dk`.
