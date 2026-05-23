# FAQ

### What hypervisors does this support?

XCP-ng 8.3 is the tested and supported platform. Any XAPI-compatible
hypervisor with the SMAPIv3 Volume API v5 dispatch surface should
work, including Citrix XenServer 8.4.

### Does this work with Xen Orchestra (XO)?

Yes. Full backup, delta backup, continuous replication, and disaster
recovery all work. CBT (Changed Block Tracking) is supported for
incremental backups.

### Can I use an existing ZFS pool?

Yes. Point `device-config:path=` at an existing pool or dataset.
The driver creates a child dataset for the SR and manages zvols
within it. Your other datasets are not touched.

### Is the data encrypted?

ZFS native encryption is supported at the pool/dataset level. The
driver inherits whatever encryption is configured on the parent
dataset. Manage encryption with standard `zfs` commands before
creating the SR.

### What happens if the host crashes?

ZFS is a transactional filesystem — it never writes data in place.
On reboot, the pool imports cleanly. The driver's crash recovery
(`recover_vdi_vsize`) handles any interrupted resize operations
automatically.

### Can I migrate VMs between zfs-live SRs?

**Offline:** Yes — `xe vdi-copy` uses native ZFS send/receive.

**Live (SXM):** Not yet. The driver-side implementation is complete
but blocked by an upstream xapi limitation. See
[Known Limitations](known-limitations.md).

### What's the performance overhead vs raw ZFS?

Minimal. VDIs are raw zvols — no VHD chain, no qcow2 wrapping, no
coalesce process. The datapath is `qemu-storage-daemon` with direct
block access. Compression (if enabled) is handled by ZFS at the
kernel level.

### Can I use raidz?

Yes. The driver doesn't care about the pool topology. Use mirrors,
raidz, raidz2, raidz3 — whatever fits your redundancy requirements.
The driver only interacts with datasets and zvols.

### How do I check ZFS pool health?

```bash
zpool status       # health, errors, scrub status
zpool list         # capacity, fragmentation
zfs list           # dataset and zvol listing
```

### Where are the logs?

- Driver: `/var/log/SMlog`
- XAPI: `/var/log/xensource.log`
- Dispatch daemon: `journalctl -u xapi-storage-script`

See [Troubleshooting](troubleshooting.md) for diagnostic recipes.
