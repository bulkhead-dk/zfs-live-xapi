# `zfs_live_driver` Ansible role

Install or remove the `zfs-live` SMAPIv3 driver on an XCP-ng host. Wraps `installer.sh` with pre-flight, post-flight, and **atomic install rollback** semantics.

## Usage

The thin wrapper at `tools/ansible/install-driver.yml` invokes this role and is what most operators use directly:

```bash
ansible-playbook tools/ansible/install-driver.yml \
  -i <your-inventory.ini> \
  -l <target-host>
```

For composition into other playbooks (e.g. deploy.yml, teardown.yml), invoke the role directly:

```yaml
- hosts: all
  gather_facts: false
  roles:
    - role: zfs_live_driver
      vars:
        driver_action: install
```

## Action modes

| `driver_action` | Effect |
|----------|--------|
| `install` (default) | Tar current plugin state to `{{ backup_dir }}/pre-install-<epoch>.tar.gz`, run `installer.sh install`, run install-mode post-flight. **On failure**, restore plugin state from the tarball, restart `xapi-storage-script`, fail with the original cause. |
| `remove` | Run `installer.sh remove`, run remove-mode post-flight. No rollback (inverting a destructive op is messier than re-attempting). |

## Variables

See main.yml. Most operators only override `driver_action`. Other knobs:

| Variable | Default | Purpose |
|----------|---------|---------|
| `deploy_path` | `/tmp/zfs-live` | Where the source tree gets staged before `installer.sh` runs. |
| `backup_dir` | `/var/cache/zfs-live-backup` | Pre-install state snapshots land here. Survives across runs for manual rollback. |
| `backup_retention_days` | `7` | Old snapshots get rotated by rotate_backups.yml. |
| `plugin_volume_dir` / `plugin_datapath_dir` | `/usr/libexec/zfs-live-plugins/{volume/...,datapath/raw+qdisk}` | Dirs that backup_state.yml tars and restore_state.yml restores. |
| `plugin_volume_sentinel` / `plugin_datapath_sentinel` | `<dir>/Volume.create` / `<dir>/Datapath.attach` | Files post-flight asserts after install. |

## Atomic rollback

On install, before `installer.sh install` runs, backup_state.yml tars the current **plugin directories** (`plugin_volume_dir`, `plugin_datapath_dir`) to a timestamped tarball under `backup_dir`. If `installer.sh` exits non-zero, or if any post-flight assertion fails, restore_state.yml runs from the rescue block: stop the native daemon, wipe the (potentially partial) plugin dirs and native-root symlinks, extract the tarball back to `/`, restart the daemon, and report the post-restore driver-registration state.

**Scope:** The backup covers plugin scripts only. On restore, symlinks into the native daemon root are recreated automatically if the restored plugin dirs exist. On a fresh host where installer.sh fails early, the rollback clears partial plugin state and any symlinks; the host returns to "no driver registered." The original failure surfaces in the play recap so the operator sees what went wrong.

Manual rollback is also supported: backups stay in `backup_dir` for `backup_retention_days` (default 7), so an operator who notices a regression an hour later can:

```bash
systemctl stop xapi-storage-script
rm -rf /usr/libexec/zfs-live-plugins/volume/org.xen.xapi.storage.zfs-live \
       /usr/libexec/zfs-live-plugins/datapath/raw+qdisk
rm -f /usr/libexec/xapi-storage-script/volume/org.xen.xapi.storage.zfs-live \
      /usr/libexec/xapi-storage-script/datapath/raw+qdisk
tar xzf /var/cache/zfs-live-backup/pre-install-<epoch>.tar.gz -C /
systemctl start xapi-storage-script
```

## What it does *not* do

- **Per-file content checksum comparison** between source-tree and installed files. `installer.sh` rewrites Python shebangs at install time (per #141's investigation), so source-tree-vs-installed sha256 wouldn't match even on a clean install. Out of scope for the same reason it was out of scope for upgrade-preflight.yml's drift_check.
- **Rollback on `driver_action=remove`.** Inverting a destructive op is messier than re-attempting it. If `installer.sh remove` fails, re-run `driver_action=install` then re-attempt remove.
- **Multi-version side-by-side install.** The role targets the canonical install path; manage version drift via VM snapshots or by overriding `deploy_path`.

## Tracking

Spun out from #149 (parent: #23 automation umbrella).
