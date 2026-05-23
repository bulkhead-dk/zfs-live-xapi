# Troubleshooting

## Log locations

| Log | Path | What it shows |
|-----|------|---------------|
| Driver logs | `/var/log/SMlog` | Volume + datapath plugin invocations |
| XAPI logs | `/var/log/xensource.log` | xapi-storage-script dispatch traces |
| Dispatch daemon | `journalctl -u xapi-storage-script` | Native daemon dispatch messages |
| ZFS state | `zpool status`, `zfs list` | Pool health and dataset tree |

## Common errors

### `xe sr-create` fails with "Storage_error (S(Unknown_error))"

The native dispatch daemon may not be running:

```bash
systemctl status xapi-storage-script
systemctl restart xapi-storage-script
```

### `xe sr-create` fails with "ZFS pool not imported"

The ZFS pool isn't imported on this host:

```bash
zpool import <pool-name>
```

### VDI create fails with "No space left on device"

The ZFS pool is full:

```bash
zpool list          # check free space
zfs list -t volume  # check zvol sizes
```

### VM start fails with "VDI.epoch_begin" or similar

Restart the native dispatch daemon:

```bash
systemctl restart xapi-storage-script
```

### `xe sr-scan` fails

Check if the SR dataset exists and is mounted:

```bash
zfs list <pool>/<sr-uuid>
zfs mount <pool>/<sr-uuid>
```

### VDI destroy fails with "Device or resource busy"

A qemu-storage-daemon still has the zvol open. The driver retries
automatically (30 attempts, 0.5s each). If it persists:

```bash
# Find what's using the zvol
fuser /dev/zvol/<pool>/<sr-uuid>/<vdi-uuid>

# Kill the stale daemon
kill <pid>
```

## Diagnostic commands

```bash
# Check driver registration
xe sm-list type=zfs-live

# List all zfs-live SRs
xe sr-list type=zfs-live

# Check pool health
zpool status

# List all zvols (VDIs)
zfs list -t volume

# Check native daemon status
systemctl status xapi-storage-script

# View recent driver logs
grep "SMAPIv3" /var/log/SMlog | tail -20

# View dispatch daemon trace
journalctl -u xapi-storage-script --no-pager | tail -50
```

## Getting help

File issues at the project repository. Include:
- XCP-ng version (`cat /etc/xcp-ng-release`)
- Driver version (`grep version CITATION.cff`)
- The relevant section of `/var/log/SMlog`
- The `xe` command that failed and its full error output
