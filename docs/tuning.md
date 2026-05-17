# Tuning

## Pool-level settings

Set these on the ZFS pool BEFORE creating the SR.

### volblocksize

The block size for zvols. Affects I/O alignment and space efficiency.
Set at SR creation time via `device-config:volblocksize`:

```bash
xe sr-create type=zfs-live name-label="ZFS Storage" \
  device-config:path=tank \
  device-config:volblocksize=8192
```

| Workload | Recommended | Why |
|----------|------------|-----|
| General VM | `8192` (8K, default) | Matches most filesystem block sizes |
| Database (large records) | `16384`–`65536` | Reduces metadata overhead |
| Sequential I/O | `65536`–`131072` | Better throughput |

**Cannot be changed after SR creation.** All VDIs in the SR use
the same volblocksize.

### ARC (Adaptive Replacement Cache)

ZFS uses system RAM as a read cache. By default it claims up to
50% of RAM. On a hypervisor, tune it down to leave memory for VMs:

```bash
# Limit ARC to 4 GB (example)
echo 4294967296 > /sys/module/zfs/parameters/zfs_arc_max

# Persist across reboot
echo "options zfs zfs_arc_max=4294967296" > /etc/modprobe.d/zfs.conf
```

### L2ARC (SSD read cache)

Add an SSD as a read cache device:

```bash
zpool add <pool> cache /dev/nvme0n1p1
```

Useful when the ARC is constrained but you have fast SSDs available.

### SLOG (ZFS Intent Log)

Add a fast device for synchronous write acceleration:

```bash
zpool add <pool> log mirror /dev/nvme0n1p2 /dev/nvme1n1p2
```

Only helps when `sync=always` is set on the dataset. The default
`sync=standard` uses the SLOG only for O_SYNC/fsync writes.

## Per-SR settings

Set via `device-config` at SR creation:

```bash
xe sr-create type=zfs-live name-label="ZFS Storage" \
  device-config:path=tank \
  device-config:compression=zstd \
  device-config:sync=standard \
  device-config:primarycache=all
```

See [Configuration](configuration.md) for the full property table.

## Monitoring

```bash
# Pool I/O stats
zpool iostat <pool> 5     # 5-second intervals

# ARC hit rate
arc_summary                # or: cat /proc/spl/kstat/zfs/arcstats

# Per-zvol I/O
zfs get -t volume all <pool>/<sr-uuid>/<vdi-uuid>
```
