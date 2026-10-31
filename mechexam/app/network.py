"""Which networks an examination may be sat from.

The strongest thing you can do about a student looking an answer up is to
take the internet away from the room, and that is a matter for the wireless
router rather than for this code: a laptop running the portal on a local
network with no uplink leaves the students able to reach the exam and nothing
else. No watermark or overlay trick comes close to it.

What this module adds is the half the router cannot do on its own. A router
can stop a phone reaching the internet, but it cannot stop a phone leaving
the room's wifi altogether, switching to mobile data and reaching a portal
that is published on the internet. So the exam itself can be told which
networks it may be sat from, and the server refuses an attempt coming from
anywhere else.

A note on addresses behind a proxy. ``remote_addr`` is the address the
application actually sees. On a host that puts a proxy in front -- Railway,
PythonAnywhere -- that is the proxy, not the student, and every student would
appear to come from one address. That is exactly the case where this feature
should not be used, so rather than guess at ``X-Forwarded-For`` headers that
anyone can forge, the administration page reports what the server is seeing
and says plainly when the setting cannot be trusted.
"""
from __future__ import annotations

import ipaddress
import socket

from flask import request


def client_address() -> str:
    """The address this request came from, as the server sees it."""
    return (request.remote_addr or "").strip()


def parse_networks(raw: str) -> tuple[list, list[str]]:
    """Read a list of networks, returning what parsed and what did not.

    Accepts a CIDR block (``192.168.1.0/24``), a bare address
    (``192.168.1.50``, taken as that host alone) or a trailing-dot prefix
    (``192.168.1.``), because the last is what people actually write.
    """
    networks: list = []
    problems: list[str] = []

    for piece in (raw or "").replace(",", "\n").splitlines():
        text = piece.strip()
        if not text or text.startswith("#"):
            continue

        candidate = text
        if candidate.endswith("."):
            # "192.168.1." means that /24; "10.0." means that /16.
            parts = [p for p in candidate.split(".") if p]
            bits = len(parts) * 8
            candidate = ".".join(parts + ["0"] * (4 - len(parts))) + f"/{bits}"

        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            problems.append(text)

    return networks, problems


def address_allowed(address: str, raw: str) -> bool:
    """Is this address inside the allowed list? An empty list allows all."""
    networks, _ = parse_networks(raw)
    if not networks:
        return True
    try:
        who = ipaddress.ip_address((address or "").strip())
    except ValueError:
        # No readable address means no way to say it is inside the room, and
        # the safe answer for an access rule is no.
        return False
    return any(who in network for network in networks)


def describe(raw: str) -> str:
    """The allowed list in words, for a page to show."""
    networks, problems = parse_networks(raw)
    if not networks and not problems:
        return "anywhere"
    bits = [str(n) for n in networks]
    if problems:
        bits.append("could not read: " + ", ".join(problems))
    return "; ".join(bits)


def server_addresses() -> list[str]:
    """Every address of this machine a student's phone might reach it on.

    Worked out by asking the operating system which local address it would
    use to reach an outside host -- without sending anything -- which is the
    only reliable way to find the address on the wifi rather than a loopback
    or a container's own.
    """
    found: list[str] = []

    for probe in ("10.255.255.255", "192.168.255.255", "172.31.255.255",
                  "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 1))
            address = sock.getsockname()[0]
            if address and not address.startswith("127.") and address not in found:
                found.append(address)
        except OSError:
            continue
        finally:
            sock.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            address = info[4][0]
            if address and not address.startswith("127.") and address not in found:
                found.append(address)
    except OSError:
        pass

    return found


def suggested_networks() -> list[str]:
    """The /24 of each address this machine has, ready to paste in."""
    out = []
    for address in server_addresses():
        try:
            network = ipaddress.ip_network(address + "/24", strict=False)
        except ValueError:
            continue
        if str(network) not in out:
            out.append(str(network))
    return out
