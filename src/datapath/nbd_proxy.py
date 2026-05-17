#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SCM_RIGHTS fd receiver and bidirectional proxy for inbound storage migration.

XAPI's nbd_handler sends a network file descriptor via SCM_RIGHTS over a
Unix domain socket.  This module receives that fd and proxies data between
it and the qemu-storage-daemon's NBD socket, enabling live storage
migration (SXM) from SMAPIv1 sources.

Protocol (from XAPI's perspective, see storage_migrate.ml nbd_handler):
  1. Call import_activate -> get listening socket path
  2. Connect to listening socket
  3. send_fd_substring: dp string as data + HTTP fd as SCM_RIGHTS
  4. Receiver proxies between received fd and NBD socket
"""

import os
import select
import signal
import socket
import struct


def receive_scm_fd(listener_sock):
    """Accept a connection and receive an fd via SCM_RIGHTS.

    XAPI's nbd_handler connects to the listener socket and sends:
    - Regular data: the dp (datapath) string
    - Ancillary data: the HTTP connection fd via SCM_RIGHTS

    Args:
        listener_sock: A listening Unix domain socket

    Returns:
        Tuple of (fd, dp_string) where fd is the received file descriptor
        and dp_string is the datapath identifier string.

    Raises:
        RuntimeError: If no fd was received via SCM_RIGHTS
    """
    conn, _ = listener_sock.accept()
    try:
        # recvmsg: receive both regular data and ancillary (SCM_RIGHTS) data
        # Buffer 4096 is generous for the dp string.
        # Ancillary buffer: space for one file descriptor.
        msg, ancdata, _flags, _addr = conn.recvmsg(
            4096, socket.CMSG_SPACE(struct.calcsize("i"))
        )

        dp_string = msg.decode("utf-8", errors="replace")

        nbd_fd = None
        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
                nbd_fd = struct.unpack("i", cmsg_data[: struct.calcsize("i")])[0]
                break

        if nbd_fd is None:
            raise RuntimeError("No file descriptor received via SCM_RIGHTS")

        return (nbd_fd, dp_string)
    finally:
        conn.close()


def proxy_fds(fd1, fd2, bufsize=65536):
    """Bidirectional proxy between two file descriptors.

    Shuttles data between fd1 and fd2 using select() until one side
    closes or an error occurs.  Closes both fds on exit.

    Args:
        fd1: First file descriptor (int)
        fd2: Second file descriptor (int)
        bufsize: Read buffer size in bytes
    """
    try:
        while True:
            readable, _, exceptional = select.select([fd1, fd2], [], [fd1, fd2], 30.0)

            if exceptional:
                break

            if not readable:
                continue  # timeout, loop back

            for r in readable:
                w = fd2 if r == fd1 else fd1
                try:
                    data = os.read(r, bufsize)
                except OSError:
                    return
                if not data:
                    return  # EOF

                offset = 0
                while offset < len(data):
                    try:
                        written = os.write(w, data[offset:])
                    except OSError:
                        return
                    if written == 0:
                        return
                    offset += written
    finally:
        for fd in (fd1, fd2):
            try:
                os.close(fd)
            except OSError:
                pass


def start_scm_daemon(scm_path, nbd_target_path):
    """Create an SCM listening socket and fork a daemon to handle it.

    Creates a Unix domain listening socket at scm_path, then forks a
    child process that accepts one connection, receives an fd via
    SCM_RIGHTS, and proxies between it and the NBD target socket.

    The listener socket is created and bound BEFORE forking so it is
    ready when XAPI connects (the parent returns the path immediately).

    Args:
        scm_path: Where to create the listening socket
        nbd_target_path: qemu-storage-daemon's NBD socket path

    Returns:
        PID of the daemon process
    """
    # Clean up stale socket
    try:
        os.unlink(scm_path)
    except OSError:
        pass

    # Create and bind listening socket BEFORE forking so it is ready
    # when the parent returns the path to XAPI.
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(scm_path)
    listener.listen(1)

    pid = os.fork()
    if pid > 0:
        # Parent: close our copy of the listener, return child PID.
        listener.close()
        return pid

    # --- Child process below ---
    try:
        # Detach from parent session
        os.setsid()

        # Close stdin/stdout/stderr so we don't corrupt the parent's
        # JSON output to xapi-storage-script.
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)

        # Reset signal handlers
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)

        _run_scm_proxy(listener, nbd_target_path)
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    finally:
        try:
            listener.close()
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        try:
            os.unlink(scm_path)
        except OSError:
            pass
        os._exit(0)
    return 0  # unreachable; satisfies pylint R1710


def _run_scm_proxy(listener, nbd_target_path):
    """Run the SCM proxy in the child process.

    Accepts one connection, receives the HTTP fd via SCM_RIGHTS,
    connects to qemu-storage-daemon's NBD socket, and proxies
    between them until one side closes.
    """
    # Receive fd from XAPI's nbd_handler
    nbd_fd, _dp_string = receive_scm_fd(listener)
    listener.close()

    # Connect to qemu-storage-daemon's NBD socket
    nbd_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    nbd_sock.connect(nbd_target_path)
    # Detach the fd from the socket object so proxy_fds can manage it.
    nbd_local_fd = os.dup(nbd_sock.fileno())
    nbd_sock.close()

    # Bidirectional proxy -- proxy_fds closes both fds on exit
    proxy_fds(nbd_fd, nbd_local_fd)
