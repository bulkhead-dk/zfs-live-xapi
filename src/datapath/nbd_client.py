#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal NBD client for fetching dirty-bitmap extents from a
qemu-storage-daemon / qemu-dm NBD export (#111, foundation for
#108's consumer-side extent replay).

Why in-tree: qemu-img / nbdinfo / nbdcopy / libnbd aren't
installed on XCP-ng 8.3 hosts (verified on xcpng-target-1). The
NBD pieces we need are well-defined and stable since fixed-
newstyle was finalised -- about 200 lines of careful binary code.

Public surface:

    fetch_dirty_extents(dbg, socket_path, export_name,
                        bitmap_context) -> list[(offset, length)]

Returns the dirty ranges reported by NBD_CMD_BLOCK_STATUS with
the `qemu:dirty-bitmap:<name>` meta context. The export's size
is read out of the NBD_OPT_GO reply -- caller doesn't need to
know it up front.

What this client deliberately does NOT support:
  - TLS / encryption -- qemu's NBD server here is a Unix socket
    controlled by the same UID as the driver; filesystem
    permissions are the boundary.
  - Multiple meta contexts simultaneously. One context per
    connection is what the consumer needs.
  - NBD_CMD_WRITE / NBD_CMD_READ. The write-side merge-bitmap
    dance is #108's remaining work; that uses a separate
    writable export.
"""

import socket
import struct

# --- NBD protocol constants ---------------------------------------------
# Names match the canonical NBD spec (`man nbd-server`,
# qemu/docs/interop/nbd.txt).

NBDMAGIC = 0x4E42444D41474943  # b"NBDMAGIC"
IHAVEOPT = 0x49484156454F5054  # b"IHAVEOPT"
NBD_OPTION_REPLY_MAGIC = 0x3E889045565A9
NBD_REQUEST_MAGIC = 0x25609513
NBD_STRUCTURED_REPLY_MAGIC = 0x668E33EF

# Client-side flags (sent in handshake).
NBD_FLAG_C_FIXED_NEWSTYLE = 1 << 0

# Option types (NBD_OPT_*).
NBD_OPT_GO = 7
NBD_OPT_STRUCTURED_REPLY = 8
NBD_OPT_SET_META_CONTEXT = 10

# Option-reply types (NBD_REP_*).
NBD_REP_ACK = 1
NBD_REP_INFO = 3
NBD_REP_META_CONTEXT = 4
NBD_REP_FLAG_ERROR = 1 << 31

# NBD_INFO_* sub-types in option-reply payloads.
NBD_INFO_EXPORT = 0

# Commands (NBD_CMD_*).
NBD_CMD_BLOCK_STATUS = 7

# Structured-reply types.
NBD_REPLY_TYPE_NONE = 0
NBD_REPLY_TYPE_BLOCK_STATUS = 5
NBD_REPLY_TYPE_ERROR = (1 << 15) | 1
NBD_REPLY_FLAG_DONE = 1 << 0


class NBDProtocolError(Exception):
    """Anything wire-shape unexpected. Not OSError -- the consumer
    distinguishes "couldn't connect" (OSError, fall through) from
    "server spoke garbage" (this; log + give up)."""


# --- Wire helpers -------------------------------------------------------


def _recv_exact(sock, n):
    """Read exactly `n` bytes or raise. NBD's socket contract is
    byte-oriented; partial reads at boundaries are normal."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise NBDProtocolError(
                "short read: wanted {} more bytes, got EOF".format(n - len(buf))
            )
        buf.extend(chunk)
    return bytes(buf)


def _send_all(sock, data):
    sock.sendall(data)


# --- Handshake ----------------------------------------------------------


def _handshake(sock):
    """Read NBDMAGIC + IHAVEOPT + server flags; reply with client
    flags. Server flags returned for completeness; we don't act
    on them."""
    magic, opts_magic, server_flags = struct.unpack("!QQH", _recv_exact(sock, 18))
    if magic != NBDMAGIC:
        raise NBDProtocolError(
            "bad NBD magic: 0x{:x} (expected 0x{:x})".format(magic, NBDMAGIC)
        )
    if opts_magic != IHAVEOPT:
        raise NBDProtocolError("bad IHAVEOPT magic: 0x{:x}".format(opts_magic))
    _send_all(sock, struct.pack("!I", NBD_FLAG_C_FIXED_NEWSTYLE))
    return server_flags


# --- Option negotiation -------------------------------------------------


def _send_option(sock, opt, payload=b""):
    _send_all(sock, struct.pack("!QII", IHAVEOPT, opt, len(payload)) + payload)


def _recv_option_reply(sock):
    """Read one option-reply chunk. Returns (option, reply_type,
    payload)."""
    magic, option, reply_type, length = struct.unpack("!QIII", _recv_exact(sock, 20))
    if magic != NBD_OPTION_REPLY_MAGIC:
        raise NBDProtocolError("bad option-reply magic: 0x{:x}".format(magic))
    payload = _recv_exact(sock, length) if length else b""
    return option, reply_type, payload


def _opt_structured_reply(sock):
    """Enable structured replies -- required for
    NBD_REPLY_TYPE_BLOCK_STATUS responses in transmission phase."""
    _send_option(sock, NBD_OPT_STRUCTURED_REPLY)
    opt, rep, _ = _recv_option_reply(sock)
    if opt != NBD_OPT_STRUCTURED_REPLY or rep != NBD_REP_ACK:
        raise NBDProtocolError(
            "structured-reply negotiation failed: opt={} " "rep=0x{:x}".format(opt, rep)
        )


def _opt_set_meta_context(sock, export_name, context):
    """Bind one meta context (e.g. `qemu:dirty-bitmap:<name>`) so
    subsequent BLOCK_STATUS replies are status-of-that-context.
    Must be called BEFORE NBD_OPT_GO (option negotiation phase
    only).

    Returns the server-assigned context ID, used to match the
    BLOCK_STATUS reply's `context_id` field on the way back.
    """
    name = export_name.encode("utf-8")
    queries = [context.encode("utf-8")]
    payload = struct.pack("!I", len(name)) + name + struct.pack("!I", len(queries))
    for q in queries:
        payload += struct.pack("!I", len(q)) + q
    _send_option(sock, NBD_OPT_SET_META_CONTEXT, payload)

    context_id = None
    while True:
        opt, rep, body = _recv_option_reply(sock)
        if opt != NBD_OPT_SET_META_CONTEXT:
            raise NBDProtocolError("unexpected option in reply: {}".format(opt))
        if rep & NBD_REP_FLAG_ERROR:
            raise NBDProtocolError(
                "NBD_OPT_SET_META_CONTEXT failed: rep=0x{:x} "
                "body={!r}".format(rep, body)
            )
        if rep == NBD_REP_META_CONTEXT:
            # u32 context_id + name (rest of body).
            context_id = struct.unpack("!I", body[:4])[0]
            continue
        if rep == NBD_REP_ACK:
            break

    if context_id is None:
        raise NBDProtocolError(
            "server did not assign a context id for {}".format(context)
        )
    return context_id


def _opt_go(sock, export_name):
    """NBD_OPT_GO: select an export and finish handshake.

    Returns the export size in bytes (read from the NBD_REP_INFO
    chunk with info_type == NBD_INFO_EXPORT). Server may emit
    multiple INFO chunks; we collect the EXPORT one and ignore
    the rest. After this returns, the connection is in
    transmission phase -- only NBD_CMD_* allowed.
    """
    # Wire: u32 namelen + name + u16 nrequests + u16[nrequests]
    # info-type list. Empty list = "send your default set", which
    # always includes NBD_INFO_EXPORT.
    name = export_name.encode("utf-8")
    payload = struct.pack("!I", len(name)) + name + struct.pack("!H", 0)
    _send_option(sock, NBD_OPT_GO, payload)

    export_size = None
    while True:
        opt, rep, body = _recv_option_reply(sock)
        if opt != NBD_OPT_GO:
            raise NBDProtocolError("unexpected option in reply: {}".format(opt))
        if rep & NBD_REP_FLAG_ERROR:
            raise NBDProtocolError(
                "NBD_OPT_GO failed: rep=0x{:x} body={!r}".format(rep, body)
            )
        if rep == NBD_REP_INFO:
            info_type = struct.unpack("!H", body[:2])[0]
            if info_type == NBD_INFO_EXPORT and len(body) >= 12:
                # NBD_INFO_EXPORT: u64 export_size + u16 flags
                export_size = struct.unpack("!Q", body[2:10])[0]
            continue
        if rep == NBD_REP_ACK:
            break

    if export_size is None:
        raise NBDProtocolError("NBD_OPT_GO completed without NBD_INFO_EXPORT")
    return export_size


# --- Transmission phase: BLOCK_STATUS walk ------------------------------


def _request(sock, cmd, handle, offset, length, flags=0):
    _send_all(
        sock,
        struct.pack("!IHHQQI", NBD_REQUEST_MAGIC, flags, cmd, handle, offset, length),
    )


def _read_structured_chunks(sock, expected_handle):
    """Yield (chunk_type, body) until a DONE-flagged chunk arrives.
    Each BLOCK_STATUS request can return multiple BLOCK_STATUS
    chunks (the extent list, batched by qemu) plus a final
    NONE-chunk with the DONE flag."""
    while True:
        magic, flags, ctype, handle, length = struct.unpack(
            "!IHHQI", _recv_exact(sock, 20)
        )
        if magic != NBD_STRUCTURED_REPLY_MAGIC:
            raise NBDProtocolError("bad structured-reply magic: 0x{:x}".format(magic))
        if handle != expected_handle:
            raise NBDProtocolError(
                "handle mismatch: got {} expected {}".format(handle, expected_handle)
            )
        body = _recv_exact(sock, length) if length else b""
        yield ctype, body
        if flags & NBD_REPLY_FLAG_DONE:
            return


# qemu's per-reply size cap is somewhere around 2^32 - 1 bytes. We
# bound a single request's length well under that so the extent-
# list payload stays manageable on huge VDIs. 2 GiB lines up with
# common sparse-disk granularity assumptions.
_BLOCK_STATUS_CHUNK = 2 * 1024 * 1024 * 1024


def _walk_block_status(sock, context_id, export_size, chunk_size=_BLOCK_STATUS_CHUNK):
    """Issue BLOCK_STATUS requests covering [0, export_size) and
    yield (offset, length, status) for each extent reported under
    `context_id`. Caller filters status (1 = dirty for a
    dirty-bitmap context)."""
    handle = 0
    offset = 0
    while offset < export_size:
        length = min(chunk_size, export_size - offset)
        _request(sock, NBD_CMD_BLOCK_STATUS, handle, offset, length, flags=0)

        cur = offset
        for ctype, body in _read_structured_chunks(sock, handle):
            if ctype == NBD_REPLY_TYPE_NONE:
                continue
            if ctype == NBD_REPLY_TYPE_ERROR:
                # body: u32 error + u16 message_len + msg
                err_code = struct.unpack("!I", body[:4])[0]
                msg = body[6:].decode("utf-8", errors="replace")
                raise NBDProtocolError(
                    "BLOCK_STATUS error: code={} msg={}".format(err_code, msg)
                )
            if ctype != NBD_REPLY_TYPE_BLOCK_STATUS:
                continue
            # body: u32 context_id + [(u32 length, u32 status)]*
            ctx = struct.unpack("!I", body[:4])[0]
            descriptors = body[4:]
            if ctx != context_id:
                # Server should never return a different context,
                # but be defensive -- skip the chunk.
                continue
            for i in range(0, len(descriptors), 8):
                ext_len, status = struct.unpack("!II", descriptors[i : i + 8])
                yield cur, ext_len, status
                cur += ext_len

        handle += 1
        offset += length


# --- Public entry point -------------------------------------------------


def fetch_dirty_extents(dbg, socket_path, export_name, bitmap_context, timeout=5.0):
    """Connect to a qemu NBD server, select the dirty-bitmap meta
    context, and return all dirty extents as
    `[(offset, length), ...]`. Status == 1 (qemu's encoding of
    "dirty" for a dirty-bitmap context) is the filter; everything
    else is treated as clean.

    Args:
        dbg: Debug context (reserved; this module emits no logs
            today but accepts dbg symmetrically with the rest of
            the CBT-consumer surface).
        socket_path: Unix socket of the qemu NBD server (the
            `nbd_export.socket` field of an `export_bitmap`
            payload).
        export_name: Export name (the `nbd_export.export_name`
            field of the same payload).
        bitmap_context: e.g. `"qemu:dirty-bitmap:cbt-active"` --
            the `nbd_export.bitmap_context` field of the payload.
        timeout: Per-operation socket timeout. Connect / read /
            write all share this value.

    Raises:
        NBDProtocolError: any unexpected wire shape (server
            speaks garbage, magic mismatch, error reply).
        OSError: connect failed (caller's choice -- fall through
            to "next backup is full").
    """
    del dbg  # accepted for symmetry; reserved for future log lines
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
        _handshake(sock)
        # Order: structured-reply + set-meta-context BEFORE
        # opt-go. opt-go transitions to transmission phase, after
        # which only NBD_CMD_* are valid.
        _opt_structured_reply(sock)
        context_id = _opt_set_meta_context(sock, export_name, bitmap_context)
        export_size = _opt_go(sock, export_name)

        extents = []
        for offset, length, status in _walk_block_status(sock, context_id, export_size):
            if status == 1:
                extents.append((offset, length))
        return extents
    finally:
        try:
            sock.close()
        except OSError:
            pass
