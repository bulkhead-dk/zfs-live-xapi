# Compression

ZFS compression is transparent to the VM — the guest sees
uncompressed data, ZFS handles compression at the block layer.

## Setting compression

### At SR creation (all VDIs inherit)

```bash
xe sr-create type=zfs-live name-label="ZFS Storage" \
  device-config:path=tank \
  device-config:compression=zstd
```

### Per-VDI override

```bash
xe vdi-param-set uuid=<vdi-uuid> sm-config:compression=lz4
```

### Remove override (inherit from SR)

```bash
xe vdi-param-remove uuid=<vdi-uuid> param-name=sm-config param-key=compression
```

## Algorithms

| Algorithm | Speed | Ratio | Recommendation |
|-----------|-------|-------|----------------|
| `lz4` | Fastest | Good | General-purpose, low CPU |
| `zstd` | Fast | Best | Default recommendation |
| `gzip` | Slow | Very good | Archival / cold storage |
| `zle` | Fastest | Minimal | Zero-length encoding only |
| `lzjb` | Fast | Fair | Legacy, prefer lz4 |
| `off` | N/A | None | Disable compression |
| `on` | Fast | Good | Alias for the pool default algorithm |

## Recommendations

- **`zstd`** for most workloads — best ratio-to-speed tradeoff
- **`lz4`** for latency-sensitive workloads (databases, low-latency VMs)
- **`off`** for already-compressed data (encrypted volumes, compressed media)

## Checking compression

```bash
# On the host
zfs get compression,compressratio <pool>/<sr-uuid>/<vdi-uuid>
```

The `compressratio` property shows the effective compression ratio
for that zvol. A ratio of `2.00x` means the data occupies half the
physical space.
