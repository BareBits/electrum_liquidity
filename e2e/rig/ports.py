"""Random free-port allocation.

Each service binds a distinct random free port. We obtain ports by binding to
port 0 and reading back the kernel-assigned port. To guarantee the ports in a
single batch are distinct, we hold all the sockets open until every port in the
batch has been assigned, then close them together.
"""

from __future__ import annotations

import socket


def free_ports(count: int) -> list[int]:
    """Return ``count`` distinct free TCP ports on the loopback interface.

    There is an unavoidable (tiny) race between releasing a port here and a
    service binding it; for a local dev rig it is negligible. Holding the
    sockets until the whole batch is allocated removes the far more likely
    intra-batch collision.
    """
    if count < 1:
        raise ValueError("count must be >= 1")
    held: list[socket.socket] = []
    ports: list[int] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            held.append(sock)
            ports.append(sock.getsockname()[1])
    finally:
        for sock in held:
            sock.close()
    return ports


def free_port() -> int:
    """Return a single free TCP port on the loopback interface."""
    return free_ports(1)[0]


def port_is_free(port: int) -> bool:
    """Best-effort check that ``port`` can currently be bound on loopback."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()
