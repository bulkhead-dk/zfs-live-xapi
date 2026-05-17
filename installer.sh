#!/bin/bash
#
# ZFS SMAPIv3 Driver Installer for XCP-ng.
#
# Usage:
#   ./installer.sh [install]        # Install the driver (default)
#   ./installer.sh remove           # Remove the driver from the host
#
# Install deploys:
#   1. Volume plugin (zfs-live) under /usr/libexec/zfs-live-plugins/
#   2. Datapath plugin (raw+qdisk)
#   3. Symlinks into native xapi-storage-script root (SM registration + dispatch)
#   4. xe CLI wrapper (native VDI copy between zfs-live SRs)
#
# Remove deletes all of the above and restarts xapi-storage-script.
#

set -e

# Paths — plugin scripts live under a separate root and are symlinked
# into the native xapi-storage-script daemon root for SM registration
# and dispatch. The native daemon discovers and dispatches all RPCs
# via these symlinks. See #283 for the architecture.
PLUGIN_ROOT="/usr/libexec/zfs-live-plugins"
VOLUME_DIR="${PLUGIN_ROOT}/volume/org.xen.xapi.storage.zfs-live"
DATAPATH_DIR="${PLUGIN_ROOT}/datapath/raw+qdisk"
SHIM_LIB_DIR="/usr/lib/python3.6/site-packages/shim"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# `/usr/bin/xe` is typically a symlink to `/opt/xensource/bin/xe`.
# We need to operate on the resolved binary location so `cp` doesn't
# follow the symlink and overwrite the real binary. The wrapper goes
# in at the resolved location; the existing `/usr/bin/xe` symlink
# (or file) keeps pointing at it.
if [[ -L /usr/bin/xe ]]; then
    XE_BIN="$(readlink -f /usr/bin/xe)"
else
    XE_BIN="/usr/bin/xe"
fi
XE_REAL="${XE_BIN}.real"

ACTION="${1:-install}"

# ── Help ──────────────────────────────────────────────────────────────

print_help() {
    cat <<'HELP'
zfs-live driver installer for XCP-ng.

Usage:
  ./installer.sh [install|remove|--help]

Actions:
  install   (default)  Install the driver. Deploys:
                         1. Volume plugin (zfs-live)
                         2. Datapath plugin (raw+qdisk)
                         3. Symlinks into native xapi-storage-script
                         4. xe CLI wrapper (native VDI copy)
                       Idempotent on re-run.

  remove               Remove the driver. Deletes plugin directories,
                       native-root symlinks, and xe wrapper. Existing
                       SRs are not touched (the ZFS datasets survive);
                       re-installing later picks them back up.

Flags:
  --help, -h, help     Print this message and exit.

Examples:
  ./installer.sh                  # default install
  ./installer.sh install          # explicit
  ./installer.sh remove           # uninstall the driver

Recommended operator path:
  The Ansible-driven install at tools/ansible/install-driver.yml wraps
  this script with pre-flight + post-flight assertions and atomic
  rollback.

Documentation:
  docs/installation.md   detailed install procedure
  docs/troubleshooting.md  diagnostic recipes
HELP
}

case "${ACTION}" in
    --help|-h|help)
        print_help
        exit 0
        ;;
esac

# ── Remove ────────────────────────────────────────────────────────────

do_remove() {
    echo "=============================================="
    echo "  ZFS SMAPIv3 Driver — Removal"
    echo "=============================================="
    echo ""

    # Check we're on XCP-ng
    if [[ ! -d /usr/libexec/xapi-storage-script ]]; then
        echo "ERROR: /usr/libexec/xapi-storage-script not found"
        echo "This script must be run on an XCP-ng host"
        exit 1
    fi

    # Warn about active SRs
    ZFS_SR_UUIDS=$(xe sr-list type=zfs-live params=uuid --minimal 2>/dev/null | tr ',' '\n')
    if [ -n "${ZFS_SR_UUIDS}" ]; then
        echo "WARNING: Active zfs-live SRs detected:"
        for sr_uuid in ${ZFS_SR_UUIDS}; do
            sr_name=$(xe sr-param-get uuid="${sr_uuid}" param-name=name-label 2>/dev/null)
            echo "  - ${sr_name} (${sr_uuid})"
        done
        echo ""
        echo "Detach all VDIs and unplug all PBDs before removing."
        echo "Proceeding anyway in 5 seconds (Ctrl+C to abort)..."
        sleep 5
    fi

    echo "[1/5] Removing volume plugin: ${VOLUME_DIR}"
    if [ -d "${VOLUME_DIR}" ]; then
        rm -rf "${VOLUME_DIR}"
        echo "  Removed."
    else
        echo "  Not found — skipping."
    fi

    echo "[2/5] Removing datapath plugin: ${DATAPATH_DIR}"
    if [ -d "${DATAPATH_DIR}" ]; then
        rm -rf "${DATAPATH_DIR}"
        echo "  Removed."
    else
        echo "  Not found — skipping."
    fi

    echo "[3/5] Restoring /usr/bin/xe (unwrapping)"
    if [[ -e "${XE_REAL}" ]]; then
        rm -f "${XE_BIN}"
        mv "${XE_REAL}" "${XE_BIN}"
        echo "  Restored from ${XE_REAL}."
    else
        echo "  No wrapper detected — leaving ${XE_BIN} as-is."
    fi

    echo "[4/5] Removing native-root symlinks, shim libraries, and proxy leftovers"
    rm -f /usr/libexec/xapi-storage-script/volume/org.xen.xapi.storage.zfs-live
    rm -f /usr/libexec/xapi-storage-script/datapath/raw+qdisk
    systemctl stop xapi-storage-script-shim 2>/dev/null || true
    systemctl disable xapi-storage-script-shim 2>/dev/null || true
    rm -f /usr/sbin/xapi-storage-script-shim \
          /etc/systemd/system/xapi-storage-script-shim.service
    rm -rf "${SHIM_LIB_DIR}"
    systemctl daemon-reload 2>/dev/null || true

    echo "[5/5] Ensuring native xapi-storage-script is running"
    systemctl enable xapi-storage-script 2>/dev/null || true
    systemctl restart xapi-storage-script 2>/dev/null || true

    echo ""
    echo "=============================================="
    echo "  Removal Complete"
    echo "=============================================="
    echo ""
    echo "The SM record will be removed automatically when XAPI"
    echo "detects the missing plugin, or after an XAPI restart."
}

# ── Install ───────────────────────────────────────────────────────────

do_install() {
    echo "=============================================="
    echo "  ZFS SMAPIv3 Driver Installer"
    echo "=============================================="
    echo ""

    # Check we're on XCP-ng
    if [[ ! -d /usr/libexec/xapi-storage-script ]]; then
        echo "ERROR: /usr/libexec/xapi-storage-script not found"
        echo "This script must be run on an XCP-ng host"
        exit 1
    fi

    # Check ZFS is available
    if ! command -v zfs &> /dev/null; then
        echo "ERROR: ZFS not installed"
        echo "Install ZFS first: yum install zfs"
        exit 1
    fi

    echo "[1/7] Installing required packages"
# Install SMAPIv3 Python libraries (Python 2 based on XCP-ng 8.3)
# Also install qemu-dp for raw-qdisk datapath support
yum install -y python3-xapi-storage xcp-ng-xapi-storage-libs qemu-dp 2>/dev/null || {
    echo "WARNING: Could not install packages automatically"
    echo "Please install manually: yum install python3-xapi-storage xcp-ng-xapi-storage-libs qemu-dp"
}

# Clean up legacy zfs-vol plugin (pre-v1.0 installs registered under
# the zfs-vol type name). Safe to remove — the v1.0 driver registers
# as zfs-live via symlinks into the native daemon root.
OLD_ZFSVOL="/usr/libexec/xapi-storage-script/volume/org.xen.xapi.storage.zfs-vol"
if [ -d "${OLD_ZFSVOL}" ]; then
    echo "    Removing legacy zfs-vol plugin: ${OLD_ZFSVOL}"
    rm -rf "${OLD_ZFSVOL}"
fi

echo "[2/7] Installing volume plugin to: ${VOLUME_DIR}"
mkdir -p "${VOLUME_DIR}"

# Copy volume plugin files
cp "${SCRIPT_DIR}/src/volume/plugin.py" "${VOLUME_DIR}/"
cp "${SCRIPT_DIR}/src/volume/plugin_query.py" "${VOLUME_DIR}/"
cp "${SCRIPT_DIR}/src/volume/sr.py" "${VOLUME_DIR}/"
cp "${SCRIPT_DIR}/src/volume/volume.py" "${VOLUME_DIR}/"
cp "${SCRIPT_DIR}/src/volume/zfs_live.py" "${VOLUME_DIR}/"
cp "${SCRIPT_DIR}/src/volume/zfs_operations.py" "${VOLUME_DIR}/"
cp "${SCRIPT_DIR}/src/volume/zfs_features.py" "${VOLUME_DIR}/"
cp "${SCRIPT_DIR}/src/volume/lzc.py" "${VOLUME_DIR}/"
cp "${SCRIPT_DIR}/src/volume/sr_capability_summary.py" "${VOLUME_DIR}/"

cp "${SCRIPT_DIR}/src/shim/sr_metadata.py" "${VOLUME_DIR}/"
cp "${SCRIPT_DIR}/src/shim/read_sr_metadata.py" "${VOLUME_DIR}/"

# Copy libcow extensions
mkdir -p "${VOLUME_DIR}/libcow"
cp "${SCRIPT_DIR}/src/volume/libcow/__init__.py" "${VOLUME_DIR}/libcow/"
cp "${SCRIPT_DIR}/src/volume/libcow/imageformat.py" "${VOLUME_DIR}/libcow/"

# Make executable
chmod +x "${VOLUME_DIR}/plugin.py"
chmod +x "${VOLUME_DIR}/sr.py"
chmod +x "${VOLUME_DIR}/volume.py"
chmod +x "${VOLUME_DIR}/zfs_live.py"

# Create volume plugin symlinks
cd "${VOLUME_DIR}"
ln -sf plugin.py Plugin.Query
ln -sf plugin.py Plugin.diagnostics

ln -sf sr.py SR.create
ln -sf sr.py SR.destroy
ln -sf sr.py SR.attach
ln -sf sr.py SR.detach
ln -sf sr.py SR.ls
ln -sf sr.py SR.stat
ln -sf sr.py SR.set_name
ln -sf sr.py SR.set_description
ln -sf sr.py SR.probe

ln -sf volume.py Volume.create
ln -sf volume.py Volume.destroy
ln -sf volume.py Volume.resize
ln -sf volume.py Volume.stat
ln -sf volume.py Volume.snapshot
ln -sf volume.py Volume.clone
ln -sf volume.py Volume.set
ln -sf volume.py Volume.unset
ln -sf volume.py Volume.set_name
ln -sf volume.py Volume.set_description
ln -sf volume.py Volume.similar_content
ln -sf volume.py Volume.list_changed_blocks
ln -sf volume.py Volume.copy
ln -sf volume.py Volume.import_existing
ln -sf read_sr_metadata.py Volume.read_sr_metadata
chmod +x "${VOLUME_DIR}/read_sr_metadata.py"

echo "[3/7] Installing datapath plugin to: ${DATAPATH_DIR}"
mkdir -p "${DATAPATH_DIR}"

# Copy datapath plugin files
cp "${SCRIPT_DIR}/src/datapath/plugin.py" "${DATAPATH_DIR}/"
cp "${SCRIPT_DIR}/src/datapath/datapath.py" "${DATAPATH_DIR}/"
cp "${SCRIPT_DIR}/src/datapath/qemudisk_raw.py" "${DATAPATH_DIR}/"
cp "${SCRIPT_DIR}/src/datapath/nbd_proxy.py" "${DATAPATH_DIR}/"
cp "${SCRIPT_DIR}/src/datapath/nbd_client.py" "${DATAPATH_DIR}/"
cp "${SCRIPT_DIR}/src/datapath/cbt_consumer.py" "${DATAPATH_DIR}/"
cp "${SCRIPT_DIR}/src/volume/zfs_features.py" "${DATAPATH_DIR}/"
cp "${SCRIPT_DIR}/src/datapath/__init__.py" "${DATAPATH_DIR}/__init__.py"

# Create datapath import bridge so volume scripts can
# `from datapath import qemudisk_raw` etc. at runtime.
ln -sfn "${DATAPATH_DIR}" "${VOLUME_DIR}/datapath"

# Make executable
chmod +x "${DATAPATH_DIR}/plugin.py"
chmod +x "${DATAPATH_DIR}/datapath.py"

# Create datapath plugin symlinks
cd "${DATAPATH_DIR}"
ln -sf plugin.py Plugin.Query
ln -sf datapath.py Datapath.attach
ln -sf datapath.py Datapath.activate
ln -sf datapath.py Datapath.deactivate
ln -sf datapath.py Datapath.detach
ln -sf datapath.py Datapath.open
ln -sf datapath.py Datapath.close
ln -sf datapath.py Datapath.import_activate

echo "[4/7] Skipping runtime directories (managed by xenopsd)"

echo "[5/7] Applying Python 2 compatibility fixes"
# XCP-ng 8.3 SMAPIv3 libs are Python 2 only. Rewrite shebangs for
# plugin scripts so the native daemon forks them under Python 2.
for f in "${VOLUME_DIR}"/*.py "${VOLUME_DIR}"/libcow/*.py "${DATAPATH_DIR}"/*.py; do
    if [[ -f "$f" ]]; then
        sed -i '1s|python3|python2|' "$f"
        grep -q "coding:" "$f" || sed -i '1a# -*- coding: utf-8 -*-' "$f"
    fi
done

echo "[6/7] Installing shim libraries + native daemon symlinks"
# Shared libraries (sr_metadata, xapi_compat) used by xe wrapper
# and volume scripts at runtime.
mkdir -p "${SHIM_LIB_DIR}"
cp "${SCRIPT_DIR}/src/shim/__init__.py"    "${SHIM_LIB_DIR}/"
cp "${SCRIPT_DIR}/src/shim/xapi_compat.py" "${SHIM_LIB_DIR}/"
cp "${SCRIPT_DIR}/src/shim/sr_metadata.py" "${SHIM_LIB_DIR}/"

# Remove any leftover proxy daemon from prior installs.
systemctl stop xapi-storage-script-shim 2>/dev/null || true
systemctl disable xapi-storage-script-shim 2>/dev/null || true
rm -f /usr/sbin/xapi-storage-script-shim \
      /etc/systemd/system/xapi-storage-script-shim.service
rm -f "${SHIM_LIB_DIR}/daemon.py" "${SHIM_LIB_DIR}/dispatcher.py" \
      "${SHIM_LIB_DIR}/ms_client.py" "${SHIM_LIB_DIR}/state.py"

# Symlink the full plugin directory into the native daemon root.
# The native OCaml daemon discovers plugins by scanning its root,
# calls Plugin.Query for SM registration, and dispatches ALL RPCs
# by forking the symlinked scripts — including VDI.list_changed_blocks
# (CBT), which was verified working on both 25.6.0 and 26.1.3.
NATIVE_VOLUME="/usr/libexec/xapi-storage-script/volume/org.xen.xapi.storage.zfs-live"
rm -rf "${NATIVE_VOLUME}"
ln -sfn "${VOLUME_DIR}" "${NATIVE_VOLUME}"

NATIVE_DATAPATH="/usr/libexec/xapi-storage-script/datapath/raw+qdisk"
rm -rf "${NATIVE_DATAPATH}"
ln -sfn "${DATAPATH_DIR}" "${NATIVE_DATAPATH}"

systemctl daemon-reload

# Restart the native daemon to discover the symlinked plugin.
systemctl restart xapi-storage-script
sleep 3

if ! systemctl is-active --quiet xapi-storage-script 2>/dev/null; then
    systemctl enable --now xapi-storage-script
fi

if systemctl is-active --quiet xapi-storage-script; then
    echo "  Native daemon running (dispatches via symlinked plugin)."
else
    echo "  WARNING: native daemon not running."
    echo "    systemctl status xapi-storage-script"
fi

# ── xe CLI wrapper ────────────────────────────────────────────────────
# Routes `xe vdi-copy` between two zfs-live SRs through our
# Volume.copy script (native ZFS clone / send-receive) instead of
# xapi's hardcoded sparse_dd path (#89). Falls through to xe.real
# for everything else.
echo "[7/7] Installing /usr/bin/xe wrapper for native zfs-live VDI copy"
if [[ ! -e "${XE_REAL}" ]]; then
    cp -p "${XE_BIN}" "${XE_REAL}"
fi
cp "${SCRIPT_DIR}/src/shim/xe_wrapper.py" "${XE_BIN}"
chmod +x "${XE_BIN}"
if "${XE_BIN}" host-list --minimal >/dev/null 2>&1; then
    echo "  Wrapper installed; passthrough verified."
else
    echo "  WARNING: wrapper smoke test failed; restoring xe.real."
    rm -f "${XE_BIN}"
    mv "${XE_REAL}" "${XE_BIN}"
fi

echo ""
echo "Verifying installation"
echo ""
echo "Volume plugin:"
# shellcheck disable=SC2012  # ls output is for human verification, not parsing
ls -la "${VOLUME_DIR}/" | head -20
echo ""
echo "Datapath plugin:"
ls -la "${DATAPATH_DIR}/"
echo ""

# Check if SM capabilities need re-registration.
ZFS_SR_UUIDS=$(xe sr-list type=zfs-live params=uuid --minimal 2>/dev/null | tr ',' '\n')

if [ -n "${ZFS_SR_UUIDS}" ]; then
    echo ""
    echo "  NOTE: To register new driver capabilities, replug each zfs-live SR"
    echo "  during a maintenance window (no VMs using the SR):"
    echo ""
    for sr_uuid in ${ZFS_SR_UUIDS}; do
        pbd_uuid=$(xe pbd-list sr-uuid="${sr_uuid}" params=uuid --minimal 2>/dev/null)
        sr_name=$(xe sr-param-get uuid="${sr_uuid}" param-name=name-label 2>/dev/null)
        echo "    xe pbd-unplug uuid=${pbd_uuid}  # ${sr_name}"
        echo "    xe pbd-plug uuid=${pbd_uuid}"
        echo ""
    done
    echo "  Or restart XAPI if all SRs have active VMs:"
    echo "    systemctl restart xapi"
else
    echo "  No zfs-live SRs found — SM record will be created on first SR create."
fi

echo ""
echo "  Registered SM capabilities:"
xe sm-list type=zfs-live params=features 2>/dev/null || echo "  (SM record not yet available — will appear after first SR create)"

echo ""
echo "=============================================="
echo "  Installation Complete"
echo "=============================================="
echo ""
echo "To create a ZFS SR (whole-pool):"
echo "  xe sr-create type=zfs-live name-label='ZFS SR' \\"
echo "    device-config:path=<pool-name>"
echo ""
echo "Or under a child dataset:"
echo "  xe sr-create type=zfs-live name-label='ZFS SR' \\"
echo "    device-config:path=<pool-name>/<parent-dataset>"
echo ""
echo "The driver requires device-config:path= and manages datasets"
echo "within the pool. Pool creation is the operator's responsibility:"
echo "  zpool create -f <pool-name> /dev/sdb /dev/sdc   # before sr-create"
echo ""
}

# ── Dispatch ──────────────────────────────────────────────────────────

case "${ACTION}" in
    install)
        do_install
        ;;
    remove)
        do_remove
        ;;
    *)
        echo "$(basename "$0"): unknown action '${ACTION}'." >&2
        echo "Run './$(basename "$0") --help' for usage." >&2
        exit 2
        ;;
esac
