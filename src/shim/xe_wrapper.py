#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
`xe` CLI wrapper that intercepts `xe vdi-copy` between two `zfs-live`
SRs and routes the copy through our `Volume.copy` script (native
`zfs send | zfs receive` for cross-pool, `zfs clone` for same-pool)
instead of xapi's hardcoded `sparse_dd` path. Same-host (#86) and
cross-host (#88) variants share most of the wrapper machinery and
differ only in the data plane.

Why a wrapper at the CLI layer:

  xapi 25.6.0 has no `VDI_COPY` capability in its recognised feature
  list (verified via `strings /opt/xensource/bin/xapi`) -- it never
  consults the SM driver for `xe vdi-copy`. The data path is
  hardcoded to `Sm_fs_ops.copy_vdi` -> `sparse_dd`. The native
  daemon dispatches other operations via symlinks, but the
  `vdi-copy` operation never reaches SMAPI at all. The only fix
  is to intercept BEFORE xapi creates the dest VBD -- i.e. at the
  operator-facing CLI.

What the wrapper does:

  1. If `argv[1]` is not `vdi-copy`: `os.execv(/usr/bin/xe.real,
     argv)` -- zero-overhead passthrough.

  2. For `vdi-copy uuid=<src-vdi> sr-uuid=<dest-sr>` (the basic
     form): query both SR types via `xe.real`. If either side is
     not `zfs-live`, passthrough.

  3. Both `zfs-live`, **same host** (dest SR's PBD points at the
     local host): locate the volume plugin's directory, invoke
     `Volume.copy` directly with `(dbg, sr=<src-attach>, key=<src-vdi>,
     dest_sr=<dest-attach>)`. The script creates the new zvol and
     metabase row. Then `xe.real sr-scan uuid=<dest-sr>` so xapi
     ingests the new VDI.

  4. Both `zfs-live`, **cross-host** (#88): tunnel the data plane
     via `zfs send -c | ssh dest-host zfs receive` using the pool's
     existing root SSH trust, then SSH `xe sr-scan` on the
     destination so its xapi side picks up the new zvol. Same
     verification step as (3) -- print the new uuid only when
     end-to-end registration is confirmed.

  5. Any failure during the fast path: log to stderr and `os.execv`
     to `xe.real` with the original argv so the operator gets a
     working command in the worst case.

The wrapper does not introduce its own state. The metabase + sr-scan
are the source of truth for the new VDI's existence in xapi's view.

Incremental forms (`base-uuid=`, `into-vdi-uuid=`) are passthrough
today -- see #86 for the scoping rationale.
"""

from __future__ import print_function

import json
import os
import subprocess
import sys

# Candidate paths for the real `xe` binary, in priority order. The
# canonical XCP-ng layout has `xe` in `/opt/xensource/bin/` with a
# convenience symlink at `/usr/bin/xe` -- the installer puts our
# wrapper at whichever path the symlink resolves to and renames the
# original with a `.real` suffix at the same location.
_XE_REAL_CANDIDATES = (
    "/opt/xensource/bin/xe.real",
    "/usr/bin/xe.real",
)
VOLUME_ROOT = "/usr/libexec/zfs-live-plugins/volume"
SR_MOUNT_ROOT = "/var/run/sr-mount"

# `sr_metadata` ships with the rest of the shim package; the
# installer puts the wrapper at `/usr/bin/xe` (or whatever path the
# distro symlink resolves to) but the modules live under
# `/usr/lib/python3.6/site-packages/shim/`. Three import shapes
# need to work: (a) production -- wrapper invoked as `/usr/bin/xe`,
# add the install dir to sys.path; (b) tests -- `from shim import
# xe_wrapper`, the package import works directly; (c) dev runs from
# `src/shim/` -- sibling import.
try:
    from shim import sr_metadata  # type: ignore  # noqa: E402
except ImportError:
    try:
        import sr_metadata  # noqa: E402
    except ImportError:
        sys.path.insert(0, "/usr/lib/python3.6/site-packages/shim")
        import sr_metadata  # noqa: E402


def _find_xe_real():
    """First executable candidate path is the real xe binary. Tests
    can patch this via `_XE_REAL_CANDIDATES`."""
    for path in _XE_REAL_CANDIDATES:
        if os.access(path, os.X_OK):
            return path
    return None


# Tests rely on this being a module attribute they can patch -- keep
# the constant for backwards compatibility, but `_find_xe_real()` is
# the source of truth at runtime.
XE_REAL = _XE_REAL_CANDIDATES[0]


def _passthrough(argv=None):
    """Hand argv off to the real xe binary without further
    intervention. Uses execv so we don't add a Python process layer
    to the call stack -- the operator sees `xe.real`'s exit code and
    output directly. Never returns under normal operation; the
    `sys.exit()` after execv is a safety net for tests that mock
    `os.execv` away."""
    if argv is None:
        argv = sys.argv
    real = _find_xe_real()
    if real is None:
        # First-install bootstrap, or someone removed xe.real.
        # Fail loudly rather than silently no-oping.
        sys.stderr.write(
            "xe-wrapper: cannot passthrough: no xe.real binary "
            "found in {}\n"
            "Reinstall the driver or restore the real xe binary.\n".format(
                ", ".join(_XE_REAL_CANDIDATES)
            )
        )
        sys.exit(127)
    os.execv(real, [real] + list(argv[1:]))
    sys.exit(0)  # unreachable in prod; matters when execv is mocked


def _parse_kv_args(argv):
    """`xe` accepts arguments as `key=value` pairs. Parse them into
    a dict. Bare flags (rare in xe) are kept under their own key
    with value `True`. Unknown shapes pass through."""
    out = {}
    for arg in argv:
        if "=" in arg:
            key, _, val = arg.partition("=")
            out[key] = val
        else:
            out[arg] = True
    return out


def _xe_real(args, capture=True):
    """Invoke `xe.real` with the given args and return stdout
    stripped, or None on non-zero exit / missing binary."""
    real = _find_xe_real()
    if real is None:
        return None
    try:
        result = subprocess.check_output(
            [real] + args, stderr=subprocess.STDOUT, close_fds=True
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return result.decode("utf-8", errors="replace").strip()


def _sr_type(sr_uuid):
    """Return SR type (`zfs-live`, `lvm`, ...) or None if the SR
    doesn't exist or the query fails."""
    return _xe_real(["sr-param-get", "uuid=" + sr_uuid, "param-name=type"])


def _vdi_sr(vdi_uuid):
    """Return the SR uuid that hosts the given VDI, or None."""
    return _xe_real(["vdi-param-get", "uuid=" + vdi_uuid, "param-name=sr-uuid"])


def _vdi_param(vdi_uuid, param):
    return _xe_real(["vdi-param-get", "uuid=" + vdi_uuid, "param-name=" + param])


def _find_plugin_dir():
    """Find our volume plugin directory under VOLUME_ROOT. The
    canonical zfs-live plugin is `org.xen.xapi.storage.zfs-live`,
    but we resolve dynamically so a renamed plugin still works."""
    target = "zfs-live"
    if not os.path.isdir(VOLUME_ROOT):
        return None
    for name in os.listdir(VOLUME_ROOT):
        path = os.path.join(VOLUME_ROOT, name)
        if not os.path.isdir(path):
            continue
        if name.endswith("." + target) or name == target:
            return path
    return None


def _invoke_volume_copy(plugin_dir, src_sr_uuid, src_vdi_uuid, dest_sr_uuid, dbg):
    """Run `Volume.copy` directly via stdin JSON. Returns the new
    VDI's uuid on success, or None on failure."""
    script = os.path.join(plugin_dir, "Volume.copy")
    if not os.access(script, os.X_OK):
        return None
    payload = json.dumps(
        {
            "dbg": dbg,
            "sr": "file://" + os.path.join(SR_MOUNT_ROOT, src_sr_uuid),
            "key": src_vdi_uuid,
            "dest_sr": "file://" + os.path.join(SR_MOUNT_ROOT, dest_sr_uuid),
        }
    )
    try:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            [script, "--json"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        out, err = proc.communicate(payload.encode("utf-8"))
    except OSError as e:
        sys.stderr.write("xe-wrapper: failed to spawn Volume.copy: " "{}\n".format(e))
        return None
    if proc.returncode != 0:
        sys.stderr.write(
            "xe-wrapper: Volume.copy exited {}: {}\n".format(
                proc.returncode, err.decode("utf-8", errors="replace").strip()
            )
        )
        return None
    try:
        parsed = json.loads(out.decode("utf-8"))
    except ValueError:
        sys.stderr.write(
            "xe-wrapper: Volume.copy produced invalid JSON: {}\n".format(
                out.decode("utf-8", errors="replace").strip()
            )
        )
        return None
    # The script writes `xapi.success(...)` which produces
    # `{"Status": "Success", "Value": <volume_dict>}` -- Volume.copy's
    # value is a dict with `key`, `uuid`, `name`, etc.
    if parsed.get("Status") != "Success":
        sys.stderr.write(
            "xe-wrapper: Volume.copy reported failure: {}\n".format(json.dumps(parsed))
        )
        return None
    value = parsed.get("Value", {})
    return value.get("key") or value.get("uuid")


# --- Cross-host plumbing (#88) ---------------------------------------

XENSOURCE_INVENTORY = "/etc/xensource-inventory"

# Volume-plugin path of the `Volume.read_sr_metadata` helper script
# (#94). The cross-host fast path SSHes into the destination host and
# invokes this script to print the dest SR's metadata, instead of
# inlining a `cat <sr-mount>/meta.json` and tying us to the basename.
# The plugin dir is the same on both hosts in a pool (XCP-ng is
# uniform across pool members).
_VOLUME_PLUGIN_DIR = VOLUME_ROOT + "/org.xen.xapi.storage.zfs-live"
_REMOTE_READ_SR_METADATA = _VOLUME_PLUGIN_DIR + "/Volume.read_sr_metadata"

# Standard SSH options for pool-internal calls. xapi maintains a root
# SSH trust between hosts via `/etc/xensource/ssh_known_hosts` plus
# the per-pool keypair, so we can connect without prompting. The
# `LogLevel=ERROR` matches what we use everywhere else in the repo
# (#67) -- suppresses the "Permanently added" notice.
_SSH_OPTS = (
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "BatchMode=yes",
)


def _local_host_uuid():
    """Read the local host's UUID from /etc/xensource-inventory.

    Returns None if the file is missing or unparseable -- callers
    should treat that as "can't determine locality" and passthrough
    to xe.real rather than guess."""
    try:
        with open(XENSOURCE_INVENTORY, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("INSTALLATION_UUID="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except (IOError, OSError):
        return None
    return None


def _sr_host_uuid(sr_uuid):
    """Resolve the SR's PBD to the host UUID it's plugged on.

    A multi-PBD SR (cluster) returns the first one -- the wrapper
    treats anything other than `local` as "remote" so picking the
    first member is safe for the cross-host detection. Returns None
    on lookup failure (caller should passthrough)."""
    pbd_uuid = _xe_real(["pbd-list", "sr-uuid=" + sr_uuid, "params=uuid", "--minimal"])
    if not pbd_uuid:
        return None
    # `pbd-list ... --minimal` returns a comma-separated list when
    # multiple PBDs match; we only need any one host attached.
    pbd_uuid = pbd_uuid.split(",", 1)[0]
    return _xe_real(["pbd-param-get", "uuid=" + pbd_uuid, "param-name=host-uuid"])


def _host_address(host_uuid):
    """Get the IP/hostname for SSH access to a host."""
    return _xe_real(["host-param-get", "uuid=" + host_uuid, "param-name=address"])


def _ssh(dest_host, command, stdin=None, capture=True):
    """Run a command on a remote host via xapi's pool-internal root
    SSH trust. `command` is a list of argv parts (we don't shell-
    escape; keep arguments simple). Returns stdout-stripped on
    success, or None on any failure."""
    argv = ["ssh"] + list(_SSH_OPTS) + ["root@" + dest_host] + list(command)
    try:
        if stdin is not None:
            proc = subprocess.Popen(  # pylint: disable=consider-using-with
                argv,
                stdin=stdin,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
            out, _err = proc.communicate()
            if proc.returncode != 0:
                return None
            return (out or b"").decode("utf-8", errors="replace").strip()
        return (
            subprocess.check_output(argv, stderr=subprocess.STDOUT, close_fds=True)
            .decode("utf-8", errors="replace")
            .strip()
        )
    except (subprocess.CalledProcessError, OSError):
        return None


def _read_sr_zfs_path(sr_mount):
    """Read pool/dataset from an SR's metadata. The metadata file is
    JSON written by `util.update_sr_metadata` during SR.create
    (`src/volume/sr.py`). Returns (pool, dataset) or (None, None)
    on missing/malformed input."""
    meta = sr_metadata.read_sr_metadata(sr_mount)
    if meta is None:
        return None, None
    return meta.get("zpool"), meta.get("dataset")


def _read_remote_sr_zfs_path(dest_host, dest_sr_uuid):
    """Same as `_read_sr_zfs_path`, but the metadata file lives on
    the remote host so we invoke the `Volume.read_sr_metadata` helper
    over SSH. The helper hides the basename inside the volume-plugin
    install (#94), so the wrapper doesn't need to know it."""
    sr_mount = os.path.join(SR_MOUNT_ROOT, dest_sr_uuid)
    out = _ssh(dest_host, [_REMOTE_READ_SR_METADATA, sr_mount])
    if not out:
        return None, None
    try:
        meta = json.loads(out)
    except ValueError:
        return None, None
    return meta.get("zpool"), meta.get("dataset")


def _invoke_cross_host_copy(
    src_sr_uuid, src_vdi_uuid, dest_sr_uuid, dest_host_addr, dbg
):
    """Cross-host data plane: zfs send | ssh zfs receive.

    Steps:
      1. Resolve src/dest dataset paths from each SR's metadata file
         (local for src, SSHed for dest).
      2. Generate a new VDI uuid client-side (we own the namespace).
      3. Snapshot the source zvol with a transient token.
      4. `zfs send -c <snap> | ssh dest 'zfs receive
         <dest-pool>/<dest-dataset>/<new-uuid>'`.
      5. Cleanup the source-side snapshot.
      6. Caller runs `xe sr-scan` on the destination to register
         the new VDI in xapi's view.

    Returns the new VDI uuid on success, or None if any step fails.
    Cleanup on failure tears down the partial dest zvol AND the
    source snapshot so neither host is left with leaked state."""
    src_sr_mount = os.path.join(SR_MOUNT_ROOT, src_sr_uuid)
    src_pool, src_dataset = _read_sr_zfs_path(src_sr_mount)
    if not src_pool:
        sys.stderr.write(
            "xe-wrapper: cannot read source SR metadata at {} -- "
            "fall through.\n".format(sr_metadata.sr_metadata_path(src_sr_mount))
        )
        return None

    dest_pool, dest_dataset = _read_remote_sr_zfs_path(dest_host_addr, dest_sr_uuid)
    if not dest_pool:
        sys.stderr.write(
            "xe-wrapper: cannot read dest SR metadata via "
            "ssh root@{} -- fall through.\n".format(dest_host_addr)
        )
        return None

    import uuid as _uuid  # pylint: disable=import-outside-toplevel

    new_uuid = str(_uuid.uuid4())
    # Build zvol paths: pool/dataset/uuid or pool/uuid (pool-root SR)
    src_base = "{}/{}".format(src_pool, src_dataset) if src_dataset else src_pool
    dest_base = "{}/{}".format(dest_pool, dest_dataset) if dest_dataset else dest_pool
    src_zvol = "{}/{}".format(src_base, src_vdi_uuid)
    snap_name = "{}@xe-wrapper-{}".format(src_zvol, new_uuid)
    dest_zvol = "{}/{}".format(dest_base, new_uuid)

    # 3. Snapshot. If this fails the source is untouched.
    try:
        subprocess.check_call(
            ["zfs", "snapshot", snap_name], stderr=subprocess.STDOUT, close_fds=True
        )
    except (subprocess.CalledProcessError, OSError) as e:
        sys.stderr.write(
            "xe-wrapper: zfs snapshot {} failed: {}\n".format(snap_name, e)
        )
        return None

    def _cleanup_source_snap():
        try:
            subprocess.check_call(
                ["zfs", "destroy", snap_name], stderr=subprocess.DEVNULL, close_fds=True
            )
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    def _cleanup_dest_zvol():
        # Best-effort. Receive may have left a partial dataset; the
        # remote `zfs destroy -r` removes it idempotently.
        _ssh(dest_host_addr, ["zfs", "destroy", "-r", dest_zvol])

    # 4. send | recv pipeline. Use Popen so we can chain stdout ->
    # stdin between zfs send and ssh.
    try:
        send_argv = ["zfs", "send", "-c", snap_name]
        recv_argv = (
            ["ssh"]
            + list(_SSH_OPTS)
            + ["root@" + dest_host_addr, "zfs", "receive", dest_zvol]
        )
        send_proc = subprocess.Popen(  # pylint: disable=consider-using-with
            send_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True
        )
        try:
            recv_proc = subprocess.Popen(  # pylint: disable=consider-using-with
                recv_argv,
                stdin=send_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
            # Close our copy of the pipe so SIGPIPE reaches send if
            # recv dies first; otherwise send blocks forever on a
            # full pipe. After closing send.stdout, we can't call
            # `send_proc.communicate()` (Python 3.6's selector
            # rejects the closed fd) -- drain stderr ourselves and
            # use `wait()` for the exit code.
            send_proc.stdout.close()
            _, recv_err = recv_proc.communicate()
            send_err = send_proc.stderr.read() if send_proc.stderr else b""
            send_proc.wait()
        except BaseException:
            send_proc.kill()
            try:
                recv_proc.kill()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            raise
        if send_proc.returncode != 0 or recv_proc.returncode != 0:
            sys.stderr.write(
                "xe-wrapper: zfs send|recv pipeline failed "
                "(send rc={}, recv rc={}): send_err={!r} "
                "recv_err={!r}\n".format(
                    send_proc.returncode,
                    recv_proc.returncode,
                    (send_err or b"").decode(errors="replace"),
                    (recv_err or b"").decode(errors="replace"),
                )
            )
            _cleanup_dest_zvol()
            _cleanup_source_snap()
            return None
    except (OSError, subprocess.SubprocessError) as e:
        sys.stderr.write("xe-wrapper: pipeline error: {}\n".format(e))
        _cleanup_dest_zvol()
        _cleanup_source_snap()
        return None

    # 5. Source snapshot is no longer needed (cross-pool: dest is
    # fully independent of source after recv). Same contract as
    # Volume.copy's cross-pool path.
    _cleanup_source_snap()

    # 6. Register the new VDI in the destination SR's metabase via
    # `Volume.import_existing` (a small custom method we wire in
    # `src/volume/volume.py` for exactly this case). Without this
    # step the zvol exists on disk but xapi has no metabase row, so
    # `xe vdi-list uuid=<new>` returns nothing -- and `xe
    # vdi-introduce` itself fails because xapi tries to call
    # Volume.stat which can't find the row either.
    #
    # We pull the source VDI's name/description/size up-front via
    # the local xe.real, then SSH the dest host's plugin script
    # with that metadata. xapi sees the new VDI on the next
    # sr-scan.
    src_name = _vdi_param(src_vdi_uuid, "name-label") or ""
    src_descr = _vdi_param(src_vdi_uuid, "name-description") or ""
    src_size = _vdi_param(src_vdi_uuid, "virtual-size") or "0"
    register_payload = json.dumps(
        {
            "dbg": dbg,
            "sr": "file://" + os.path.join(SR_MOUNT_ROOT, dest_sr_uuid),
            "key": new_uuid,
            "name": src_name,
            "description": src_descr,
            "size": int(src_size or "0"),
            "sharable": False,
        }
    )
    register_script = (
        VOLUME_ROOT + "/" "org.xen.xapi.storage.zfs-live/Volume.import_existing"
    )
    register_argv = (
        ["ssh"]
        + list(_SSH_OPTS)
        + ["root@" + dest_host_addr, register_script, "--json"]
    )
    try:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            register_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        out, err = proc.communicate(register_payload.encode("utf-8"))
    except (OSError, subprocess.SubprocessError) as e:
        sys.stderr.write(
            "xe-wrapper: failed to spawn ssh for "
            "Volume.import_existing: {}\n".format(e)
        )
        _cleanup_dest_zvol()
        return None
    if proc.returncode != 0:
        sys.stderr.write(
            "xe-wrapper: Volume.import_existing failed (rc={}): "
            "{!r} {!r}\n".format(
                proc.returncode,
                (out or b"").decode(errors="replace"),
                (err or b"").decode(errors="replace"),
            )
        )
        _cleanup_dest_zvol()
        return None

    return new_uuid


def _is_basic_vdi_copy(args):
    """The basic `xe vdi-copy uuid=<src> sr-uuid=<dest>` form.

    Defer to the real binary for the incremental form
    (`base-uuid=`, `into-vdi-uuid=`) since those are CBT-driven and
    would need additional driver work to short-circuit. Same for any
    invocation involving cross-host fields -- out of scope per #86."""
    if "uuid" not in args or "sr-uuid" not in args:
        return False
    if "base-uuid" in args or "into-vdi-uuid" in args:
        return False
    # Strip the standard xapi `username=`/`password=` flags that any
    # `xe` invocation may carry -- they don't affect the routing
    # decision.
    extraneous = set(args) - {"uuid", "sr-uuid", "username", "password"}
    if extraneous:
        # Conservative: unknown flag -> passthrough. Avoid surprising
        # the operator by ignoring something like `online=true`.
        return False
    return True


def _try_fast_path(args):
    """Decide which fast path (if any) applies.

    Returns `(mode, src_sr, dest_sr, src_vdi, dest_host_addr)`:

      - `mode == "same-host"` -- both SRs zfs-live AND dest SR plugged
        on the local host. `dest_host_addr` is None.
      - `mode == "cross-host"` -- both SRs zfs-live AND dest SR plugged
        on a different host. `dest_host_addr` is the dest host's
        address (for SSH).
      - `mode is None` -- anything else; caller passes through to xe.real.
    """
    if not _is_basic_vdi_copy(args):
        return None, None, None, None, None

    src_vdi = args["uuid"]
    dest_sr = args["sr-uuid"]

    src_sr = _vdi_sr(src_vdi)
    if not src_sr:
        return None, None, None, None, None
    src_type = _sr_type(src_sr)
    dest_type = _sr_type(dest_sr)
    if src_type != "zfs-live" or dest_type != "zfs-live":
        return None, None, None, None, None

    # Same-host vs cross-host: compare the dest SR's PBD host with
    # the local host's UUID. Anything we can't determine -> fall
    # through to xe.real (operator gets sparse_dd's correct-but-slow
    # path rather than a wrapper guess).
    local_host = _local_host_uuid()
    dest_host = _sr_host_uuid(dest_sr)
    if not local_host or not dest_host:
        return None, None, None, None, None

    if dest_host == local_host:
        return "same-host", src_sr, dest_sr, src_vdi, None

    # Cross-host. Also require source-SR plugged here -- copying a
    # remote-source VDI is out of scope for this wrapper (we'd need
    # the source-host's pool unmounted on this side, which it isn't).
    src_host = _sr_host_uuid(src_sr)
    if src_host != local_host:
        return None, None, None, None, None

    dest_host_addr = _host_address(dest_host)
    if not dest_host_addr:
        return None, None, None, None, None
    return "cross-host", src_sr, dest_sr, src_vdi, dest_host_addr


def main(argv=None):
    if argv is None:
        argv = sys.argv
    if len(argv) < 2 or argv[1] != "vdi-copy":
        _passthrough(argv)  # exec; doesn't return

    # Graceful retirement (#232): if xapi on this host has grown
    # native VDI_COPY recognition (i.e. the dispatch gap from #89
    # has landed upstream), step aside -- `xe vdi-copy` will route
    # via xapi's native SMAPI path. Probe inline if no sidecar
    # exists (the sidecar was previously written by a proxy daemon;
    # startup is shelved since #319).
    xapi_compat = None  # pylint: disable=invalid-name
    try:
        from shim import xapi_compat  # type: ignore  # noqa: E402  pylint: disable=import-outside-toplevel
    except ImportError:
        try:
            import xapi_compat  # type: ignore  # noqa: E402  pylint: disable=import-outside-toplevel
        except ImportError:
            try:
                sys.path.insert(0, "/usr/lib/python3.6/site-packages/shim")
                import xapi_compat  # type: ignore  # noqa: E402  pylint: disable=import-outside-toplevel
            except ImportError:
                xapi_compat = None  # pylint: disable=invalid-name
    if xapi_compat is not None:
        cached = xapi_compat.get_cached()
        if cached.get("xapi_version") is None:
            try:
                compat = xapi_compat.probe()
                xapi_compat.set_cached(compat)
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        if not xapi_compat.is_gap_filled("VDI.copy"):
            sys.stderr.write(
                "xe-wrapper: VDI_COPY dispatch gap is fixed in "
                "this xapi version; passing through to xe.real\n"
            )
            _passthrough(argv)

    args = _parse_kv_args(argv[2:])
    mode, src_sr, dest_sr, src_vdi, dest_host_addr = _try_fast_path(args)
    if mode is None:
        _passthrough(argv)

    if mode == "same-host":
        plugin_dir = _find_plugin_dir()
        if plugin_dir is None:
            sys.stderr.write(
                "xe-wrapper: zfs-live plugin not found under {} -- "
                "falling back to xe.real\n".format(VOLUME_ROOT)
            )
            _passthrough(argv)

        new_uuid = _invoke_volume_copy(
            plugin_dir,
            src_sr,
            src_vdi,
            dest_sr,
            dbg="xe-wrapper:vdi-copy:{}->{}".format(src_vdi, dest_sr),
        )
        if not new_uuid:
            sys.stderr.write(
                "xe-wrapper: Volume.copy fast path failed; "
                "falling back to xe.real (sparse_dd)\n"
            )
            _passthrough(argv)
        # The same-host Volume.copy script wrote a row to the SR's
        # metabase. Below: sr-scan to ingest into xapi.
        scan_target_sr = dest_sr
        scan_via_ssh = None  # local sr-scan
    else:
        # cross-host
        new_uuid = _invoke_cross_host_copy(
            src_sr,
            src_vdi,
            dest_sr,
            dest_host_addr,
            dbg="xe-wrapper:vdi-copy:{}->{}@{}".format(
                src_vdi, dest_sr, dest_host_addr
            ),
        )
        if not new_uuid:
            sys.stderr.write(
                "xe-wrapper: cross-host fast path failed; "
                "falling back to xe.real (sparse_dd)\n"
            )
            _passthrough(argv)
        # Cross-host: sr-scan must run on the destination host
        # (where the SR is plugged + the metabase lives).
        scan_target_sr = dest_sr
        scan_via_ssh = dest_host_addr

    # Tell the right xapi side to scan the dest SR. `_xe_real`
    # returns None on failure and "" on success-with-no-output;
    # both are falsy, so discriminate explicitly.
    if scan_via_ssh is None:
        scan_out = _xe_real(["sr-scan", "uuid=" + scan_target_sr])
    else:
        scan_out = _ssh(scan_via_ssh, ["xe", "sr-scan", "uuid=" + scan_target_sr])
    if scan_out is None:
        # Scan command itself failed (xapi error, RPC transport,
        # missing xe.real). The VDI exists on disk and in our
        # metabase, but xapi doesn't know about it -- printing the
        # uuid would mislead the caller into thinking the operation
        # is complete. Surface the failure cleanly. The next
        # successful sr-scan (manual or automatic) will pick up the
        # zvol via the SR.ls orphan path, so no destructive cleanup
        # is needed here.
        sys.stderr.write(
            "xe-wrapper: Volume.copy succeeded but `xe sr-scan "
            "uuid={}` failed; the destination VDI exists on disk "
            "but xapi has not yet ingested it. Re-run `xe sr-scan` "
            "manually or wait for the periodic scan.\n".format(dest_sr)
        )
        return 1

    # Defensive verification: confirm xapi can now resolve the new
    # uuid. Catches the rare case where sr-scan returns success but
    # the VDI didn't materialise in xapi's view (e.g. metabase ->
    # xapi reconciliation race). `xe.real vdi-param-get` returns the
    # uuid back on success, None on failure.
    if _xe_real(["vdi-param-get", "uuid=" + new_uuid, "param-name=uuid"]) != new_uuid:
        sys.stderr.write(
            "xe-wrapper: Volume.copy succeeded but xapi cannot "
            "resolve the new VDI {} after sr-scan. Manual "
            "investigation needed.\n".format(new_uuid)
        )
        return 1

    # Match `xe vdi-copy`'s output format: bare new VDI uuid + newline.
    print(new_uuid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
