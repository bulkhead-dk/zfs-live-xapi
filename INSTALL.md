# ZFS SMAPIv3 Driver — Installation Guide

## Prerequisites

- XAPI host with ZFS installed (verified on XCP-ng 8.3 and Citrix XenServer 8.4.0)
- SSH root access to the host
- A ZFS pool available for SR creation (or raw devices to create one)

### Required Packages

The installer handles these automatically, but for manual installs:

```bash
yum install -y python3-xapi-storage xcp-ng-xapi-storage-libs qemu-dp
```

---

## Full Install

### Option 1: SCP + Run on Host

From the development machine:

```bash
# Copy the driver directory to the host
scp -r . xcpng:/tmp/zfs-live/

# Run the installer on the host
ssh xcpng 'bash /tmp/zfs-live/installer.sh'
```

### Option 2: Ansible (recommended)

```bash
ansible-playbook tools/ansible/install-driver.yml \
  -i <your-inventory.ini> \
  -l <target-host>
```

### What the Installer Does

1. Installs required packages (`python3-xapi-storage`, `xcp-ng-xapi-storage-libs`, `qemu-dp`)
2. Copies volume plugin files to `/usr/libexec/zfs-live-plugins/volume/org.xen.xapi.storage.zfs-live/`
3. Copies datapath plugin files to `/usr/libexec/zfs-live-plugins/datapath/raw+qdisk/`
4. Creates symlinks for all SMAPIv3 API entry points
5. Symlinks plugin directories into the native `xapi-storage-script` daemon root for SM registration and dispatch
6. Installs the `xe` CLI wrapper for native ZFS send/receive on `xe vdi-copy`
7. Applies Python 2 compatibility fixes (XCP-ng 8.3 uses Python 2 for SMAPIv3)
8. Restarts the native `xapi-storage-script` daemon to discover the symlinked plugins
9. Prints PBD replug commands if existing zfs-live SRs are found

---

## Updating a Single File

When only one file has changed (e.g., a bug fix), a full reinstall is unnecessary.
Copy the changed file directly and set permissions.

### Volume Plugin Files

Destination: `/usr/libexec/zfs-live-plugins/volume/org.xen.xapi.storage.zfs-live/`

```bash
# Example: update volume.py
scp src/volume/volume.py xcpng:/tmp/volume.py
ssh xcpng 'cp /tmp/volume.py /usr/libexec/zfs-live-plugins/volume/org.xen.xapi.storage.zfs-live/volume.py && chmod +x /usr/libexec/zfs-live-plugins/volume/org.xen.xapi.storage.zfs-live/volume.py'
```

| Local File | Remote Destination |
|------------|-------------------|
| `src/volume/sr.py` | sr |
| `src/volume/volume.py` | volume |
| `src/volume/zfs_operations.py` | zfs_operations |
| `src/volume/zfs_live.py` | zfs_live |
| `src/volume/plugin.py` | plugin |
| `src/volume/libcow/__init__.py` | __init__ |
| `src/volume/libcow/imageformat.py` | imageformat |

### Datapath Plugin Files

Destination: `/usr/libexec/zfs-live-plugins/datapath/raw+qdisk/`

```bash
# Example: update datapath.py
scp src/datapath/datapath.py xcpng:/tmp/datapath.py
ssh xcpng 'cp /tmp/datapath.py /usr/libexec/zfs-live-plugins/datapath/raw+qdisk/datapath.py && chmod +x /usr/libexec/zfs-live-plugins/datapath/raw+qdisk/datapath.py'
```

| Local File | Remote Destination |
|------------|-------------------|
| `src/datapath/datapath.py` | `.../datapath/raw+qdisk/datapath.py` |
| `src/datapath/plugin.py` | `.../datapath/raw+qdisk/plugin.py` |
| `src/datapath/qemudisk_raw.py` | `.../datapath/raw+qdisk/qemudisk_raw.py` |
| `src/datapath/nbd_proxy.py` | `.../datapath/raw+qdisk/nbd_proxy.py` |

### After Updating

Restart the native daemon to pick up the change:

```bash
ssh xcpng 'systemctl restart xapi-storage-script'
```

---

## Uninstall

### Full Removal

```bash
ssh xcpng 'bash /tmp/zfs-live/installer.sh remove'
```

This removes:
- Volume and datapath plugin scripts
- Symlinks from the native daemon root
- `xe` CLI wrapper (restored to the original `xe` binary)

Or manually:

```bash
ssh xcpng 'rm -rf /usr/libexec/zfs-live-plugins && rm -f /usr/libexec/xapi-storage-script/volume/org.xen.xapi.storage.zfs-live /usr/libexec/xapi-storage-script/datapath/raw+qdisk && systemctl restart xapi-storage-script'
```

**Warning:** Detach all VDIs and unplug all PBDs for zfs-live SRs before removing.

---

## Verifying the Installation

### Check Plugin Registration

```bash
ssh xcpng 'xe sm-list type=zfs-live'
```

If the SM record does not appear, the plugin has not been discovered yet. Try:

```bash
ssh xcpng 'systemctl restart xapi-storage-script && sleep 3 && xe sm-list type=zfs-live'
```

### Check Plugin Files

```bash
# Volume plugin
ssh xcpng 'ls -la /usr/libexec/zfs-live-plugins/volume/org.xen.xapi.storage.zfs-live/'

# Datapath plugin
ssh xcpng 'ls -la /usr/libexec/zfs-live-plugins/datapath/raw+qdisk/'
```

### Check Services

```bash
# Native daemon (handles all dispatch via symlinked plugins)
ssh xcpng 'systemctl status xapi-storage-script'
```

### Check Logs

```bash
# Plugin invocation logs
ssh xcpng 'tail -50 /var/log/SMlog'

# xapi dispatch traces
ssh xcpng 'grep -i "zfs\|zvol\|raw-qdisk" /var/log/xensource.log | tail -30'
```

---

## Creating a ZFS SR

After installation, create an SR from an existing ZFS pool. The
driver requires `device-config:path=<pool>[/<parent-dataset>]` —
it manages datasets *within* an existing pool rather than owning
the pool itself, so the operator creates the zpool first via
`zpool create` and then points the SR at it:

```bash
# Whole-pool SR
xe sr-create type=zfs-live name-label='ZFS SR' \
  device-config:path=<pool-name>

# Or a child dataset under the pool
xe sr-create type=zfs-live name-label='ZFS SR' \
  device-config:path=<pool-name>/<parent-dataset>
```

The driver does not accept `device-config:device=...` or
`device-config:zpool=...` — those keys are silently ignored. Pool
creation (`zpool create -f tank /dev/sdb /dev/sdc`) is the
operator's responsibility before `sr-create`.

---

## Directory Structure Reference

```
Development (this repo)                  XAPI Host
──────────────────────                   ──────────

./  (repo root)
└── src/
    ├── volume/
    │   ├── sr.py                    →  /usr/libexec/zfs-live-plugins/volume/org.xen.xapi.storage.zfs-live/sr.py
    │   ├── volume.py                →  .../volume/org.xen.xapi.storage.zfs-live/volume.py
    │   ├── zfs_operations.py        →  .../volume/org.xen.xapi.storage.zfs-live/zfs_operations.py
    │   ├── zfs_live.py              →  .../volume/org.xen.xapi.storage.zfs-live/zfs_live.py
    │   ├── plugin.py                →  .../volume/org.xen.xapi.storage.zfs-live/plugin.py
    │   └── libcow/
    │       ├── __init__.py          →  .../volume/org.xen.xapi.storage.zfs-live/libcow/__init__.py
    │       └── imageformat.py       →  .../volume/org.xen.xapi.storage.zfs-live/libcow/imageformat.py
    └── datapath/
        ├── datapath.py              →  /usr/libexec/zfs-live-plugins/datapath/raw+qdisk/datapath.py
        ├── plugin.py                →  .../datapath/raw+qdisk/plugin.py
        ├── qemudisk_raw.py          →  .../datapath/raw+qdisk/qemudisk_raw.py
        └── nbd_proxy.py             →  .../datapath/raw+qdisk/nbd_proxy.py
```

Note: The host uses `raw+qdisk` (plus) in the datapath directory name. The install script
handles the mapping from the source layout to the host paths.
