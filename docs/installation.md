# Installation

## Prerequisites

- XCP-ng 8.3 (other XAPI-compatible hypervisors with SMAPIv3 support should work)
- Root SSH access to the host
- ZFS installed and a pool available (or raw devices to create one)
- `qemu-dp` package (installed automatically by the installer)

## Install

```bash
git clone https://git.bulkhead.dk/storage/zfs-live.git
cd zfs-live
bash installer.sh
```

The installer:
1. Installs required packages (`python3-xapi-storage`, `xcp-ng-xapi-storage-libs`, `qemu-dp`)
2. Deploys volume plugin scripts (zfs-live) and datapath plugin scripts (raw+qdisk)
3. Applies Python 2 compatibility fixes (XCP-ng 8.3 requirement)
4. Symlinks plugins into native xapi-storage-script root for SM registration
5. Installs the `xe` CLI wrapper for native VDI copy
6. Verifies the installation

## Ansible install (recommended for production)

```bash
ansible-playbook tools/ansible/install-driver.yml \
  -i <inventory> -l <target-host>
```

The Ansible playbook wraps `installer.sh` with pre-flight checks
(ZFS module loaded, xapi-storage-script running) and post-flight
assertions (driver registered, plugin sentinels exist). On failure,
it rolls back to the pre-install state automatically.

## Create an SR

### Whole pool

```bash
xe sr-create type=zfs-live name-label="ZFS Storage" \
  device-config:path=<pool-name>
```

### Under a child dataset

```bash
xe sr-create type=zfs-live name-label="ZFS Storage" \
  device-config:path=<pool-name>/<parent-dataset>
```

### With default compression

```bash
xe sr-create type=zfs-live name-label="ZFS Storage" \
  device-config:path=tank \
  device-config:compression=zstd
```

## Verify

```bash
# Check driver is registered
xe sm-list type=zfs-live

# Check SR is visible
xe sr-list type=zfs-live

# Check ZFS state
zpool list
zfs list
```

## Multi-host deployment

Install the driver on each host independently. Each host manages
its own local ZFS pool(s). SRs are local to the host — the ZFS
datasets do not need to be shared across hosts.

For cross-host VDI relocation, use `xe vdi-copy` (which uses
native ZFS send/receive when both SRs are zfs-live).

## Migrating from zfs-vol

If you were running the old `zfs-vol` driver (pre-v1.0) on the same
ZFS datasets, use `adopt=true` to re-register existing zvols.

**Important:** `adopt=true` registers ALL zvols in the specified
dataset into one SR. Each old SR must be adopted from its own
dataset path — typically `pool/<old-sr-uuid>`. Do NOT use the bare
pool name if multiple SRs share the same pool.

**Maintenance window required.** All VMs on affected SRs must be
shut down before starting.

### Step 1: Discover and record

Before touching anything, map each old SR to its ZFS dataset:

```bash
# List all zfs-vol SR datasets via the xcp:sr ZFS property
zfs get -H -r -t filesystem -o name,value xcp:sr \
  $(zpool list -Ho name) 2>/dev/null | grep -v $'\t-$'
```

This prints lines like `nvmepool/abc-123  abc-123` — the first
column is the dataset, the second is the old SR UUID. Record the
SR name, UUID, dataset path, and VDI count for each:

```bash
# Capture VDI count per SR before removing metadata
for sr in $(xe sr-list type=zfs-vol params=uuid --minimal | tr ',' ' '); do
  name=$(xe sr-param-get uuid=$sr param-name=name-label)
  count=$(xe vdi-list sr-uuid=$sr --minimal | tr ',' '\n' | grep -c .)
  echo "$name ($sr): $count VDIs"
done

# Capture VDI -> VM -> VBD mapping for reattach after migration
for sr in $(xe sr-list type=zfs-vol params=uuid --minimal | tr ',' ' '); do
  echo "=== SR $sr ==="
  for vdi in $(xe vdi-list sr-uuid=$sr params=uuid --minimal | tr ',' ' '); do
    for vbd in $(xe vbd-list vdi-uuid=$vdi --minimal | tr ',' ' '); do
      vm=$(xe vbd-param-get uuid=$vbd param-name=vm-uuid)
      dev=$(xe vbd-param-get uuid=$vbd param-name=userdevice)
      vm_name=$(xe vm-param-get uuid=$vm param-name=name-label 2>/dev/null)
      echo "  VDI=$vdi VM=$vm_name ($vm) device=$dev"
    done
  done
done
```

Save this output — you will need it in Step 5 to reattach VBDs.

### Step 2: Shut down and remove old SR metadata

```bash
# For each old zfs-vol SR:
# 1. Shut down all VMs using VDIs on this SR
# 2. Unplug all PBDs for this SR
for pbd in $(xe pbd-list sr-uuid=<old-sr-uuid> --minimal | tr ',' ' '); do
  xe pbd-unplug uuid=$pbd
done
# 3. Forget the SR (removes XAPI metadata, does NOT touch ZFS data)
xe sr-forget uuid=<old-sr-uuid>
```

**Use `sr-forget`, NOT `sr-destroy`.** `sr-destroy` calls the backend
driver which deletes the ZFS dataset. `sr-forget` only removes XAPI's
SR/VDI records — the ZFS datasets and zvols are untouched.

### Step 3: Install the v1.0 driver

```bash
# This also removes the old zfs-vol plugin directory and
# restarts the native daemon to deregister zfs-vol
bash installer.sh
```

Verify: `xe sm-list type=zfs-vol` should be empty.
Verify: `xe sm-list type=zfs-live` should show a UUID.

### Step 4: Adopt each dataset

```bash
# Adopt each SR from its specific dataset path:
xe sr-create type=zfs-live \
  name-label='My SR' \
  device-config:path=<pool>/<old-sr-dataset> \
  device-config:adopt=true

# Or for a pool-root SR (zvols directly under the pool):
xe sr-create type=zfs-live \
  name-label='My SR' \
  device-config:path=<pool> \
  device-config:adopt=true
```

This registers all zvols in that dataset into the new metabase.

```bash
# Trigger XAPI to ingest the new metabase
xe sr-scan uuid=<new-sr-uuid>
# Verify VDI count matches pre-migration inventory
xe vdi-list sr-uuid=<new-sr-uuid> --minimal | tr ',' '\n' | grep -c .
```

### Step 5: Reattach and boot

Reattach VBDs to VMs using your pre-migration mapping and boot
VMs one at a time. See issue #313 for the full checklist.

## Upgrading

Re-run the installer on the same host:

```bash
cd zfs-live
git pull
bash installer.sh
```

The installer is idempotent — it overwrites the plugin scripts
and restarts xapi-storage-script. Existing SRs and VDIs are not
affected.
