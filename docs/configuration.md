# Configuration

## SR-level defaults

Set ZFS properties at SR creation time. All VDIs created in the SR
inherit these values through ZFS's dataset hierarchy.

```bash
xe sr-create type=zfs-live name-label="ZFS Storage" \
  device-config:path=tank \
  device-config:compression=zstd \
  device-config:copies=2 \
  device-config:sync=standard
```

### Supported SR defaults

| Property | Values | Default |
|----------|--------|---------|
| `compression` | `off`, `on`, `lz4`, `zstd`, `gzip`, `zle`, `lzjb` | pool default |
| `copies` | `1`, `2`, `3` | `1` |
| `sync` | `standard`, `always`, `disabled` | `standard` |
| `primarycache` | `all`, `metadata`, `none` | `all` |
| `logbias` | `latency`, `throughput` | `latency` |
| `secondarycache` | `all`, `metadata`, `none` | `all` |

## Per-VDI overrides

Change properties on individual VDIs after creation:

```bash
# Set compression on a specific VDI
xe vdi-param-set uuid=<vdi-uuid> sm-config:compression=lz4

# Remove an override (inherit from SR/pool)
xe vdi-param-remove uuid=<vdi-uuid> param-name=sm-config param-key=compression
```

### Mutable properties (can be changed anytime)

`compression`, `copies`, `sync`, `primarycache`, `secondarycache`, `logbias`

### Immutable properties (set at SR creation, cannot be changed per-VDI)

`volblocksize` — set via `device-config:volblocksize=<size>` at SR
creation time. All VDIs in the SR use this block size.

`provisioning` — determined by `refreservation` (thick if >0, thin if 0).

## Viewing current properties

```bash
# VDI properties show in xe vdi-param-list
xe vdi-param-list uuid=<vdi-uuid> | grep sm-config

# Or query ZFS directly on the host
zfs get compression,copies,sync <pool>/<sr-uuid>/<vdi-uuid>
```

## Provisioning

VDIs are **thin-provisioned** by default (sparse zvols). Physical
space is allocated only as data is written.

The `SR.ls` / `xe vdi-list` response reports both `virtual_size`
(the VDI's capacity as seen by the VM) and `physical_utilisation`
(actual space consumed on disk).
