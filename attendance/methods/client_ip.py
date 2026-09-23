"""Phase 3B FINAL — whose network an attendance request came from.

Attendance can be restricted to the company's own network: an employee
checks in from the office Wi-Fi and not from home, not from 4G, not
through a VPN. The ranges live in `AttendanceAllowedIP`, per company,
and this module answers the one question that makes them mean anything —
*which public address is this request actually coming from?*

Getting that wrong is expensive in both directions. Answer "unknown" too
readily and nobody can check in; believe a header too readily and anyone
can type the office's address into a request and check in from a beach.

## What production actually looks like

    phone → Cloudflare → nginx → gunicorn on 127.0.0.1:8000 → Django

Measured on production, not assumed:

    REMOTE_ADDR       127.0.0.1
    X-Real-IP         a Cloudflare edge address
    X-Forwarded-For   <real client>, <Cloudflare edge>
    CF-Connecting-IP  <real client>

So Django never sees the employee's address directly. It only ever sees
loopback, and everything about the real client arrives as a header —
which is to say, as something a caller could have written themselves.

## The trust boundary

A header is worth reading only once it is known to have been *written by
the proxy*, and the only evidence of that available here is who Django is
talking to. Gunicorn binds `127.0.0.1:8000`, so the sole peer that can
open a connection to it is something on the same host — nginx. A request
arriving from any other address did not come through that proxy, and its
forwarded headers are then just input from a stranger.

Hence: forwarded headers are read **only** when `REMOTE_ADDR` is
loopback, and ignored entirely otherwise. Someone who reaches the
application directly and sends `CF-Connecting-IP: <the office>` is
answered from their own peer address, which is not the office.

`::1` is accepted alongside `127.0.0.1` because a host resolving
`localhost` to IPv6 would make nginx connect over IPv6 loopback, which is
the same boundary reached by a different route. Both are addresses no
off-host packet can carry: the kernel will not route loopback in from a
network interface.

## Which header

`CF-Connecting-IP`, and nothing else.

Cloudflare sets it to the address it accepted the connection from, one
value, always present on a request that passed through Cloudflare. Its
absence therefore means the request did not come through Cloudflare, and
answering "unknown" to that is correct rather than unhelpful.

`X-Forwarded-For` was considered as a fallback and rejected. Cloudflare
*appends* to whatever `X-Forwarded-For` the client sent, so the leading
entries are attacker-controlled, and the real client's position is only
knowable by counting from the right — which depends on nginx's
`proxy_add_x_forwarded_for` and on there being exactly one proxy in
front. That nginx configuration is not in this repository and cannot be
verified from here. A rule whose safety depends on a file nobody in this
project can read is not a rule worth having, so there is no fallback:
when `CF-Connecting-IP` is missing or malformed, this returns `None` and
the caller refuses the attendance.

`X-Real-IP` is not used either: production shows it holding the
Cloudflare edge address, not the employee's.

## Failing closed

`None` is returned for: an untrusted peer, a missing header, a malformed
address, and a header carrying more than one value. Every one of those
means "this cannot be established", and when the feature is switched on
that must refuse rather than admit. The caller turns `None` into the
existing `WIFI_NOT_ALLOWED` refusal — a controlled 400 in Vietnamese,
never a 500.

Nothing in this module is configured with a company's address. The ranges
come from `AttendanceAllowedIP` and stay there.
"""

import ipaddress

#: Addresses that prove the request came through this host's own reverse
#: proxy. Loopback only: gunicorn binds `127.0.0.1:8000`, so nothing off
#: this machine can be the peer, and no packet arriving on a network
#: interface may carry a loopback source address.
TRUSTED_PROXY_PEERS = frozenset({"127.0.0.1", "::1"})

#: The single header production's proxy chain provides, holding the
#: address Cloudflare accepted the connection from.
CLIENT_IP_HEADER = "HTTP_CF_CONNECTING_IP"


def _parse(raw):
    """A validated address, or None.

    A value carrying a comma is refused rather than split. This header
    holds one address by contract; several means something upstream is
    not what this module thinks it is, and guessing which one to believe
    is exactly the kind of guess that lets a chosen value through.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or "," in text:
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _meta_get(request):
    """`request.META.get`, if this object has a usable one.

    The synthetic `attendance.methods.utils.Request` has a `META` whose
    `get` answers with the default for every key, so it lands here and
    resolves to `None` — which is the right answer for a caller that
    never had a network peer to begin with.
    """
    meta = getattr(request, "META", None)
    getter = getattr(meta, "get", None)
    return getter if callable(getter) else None


def peer_is_trusted_proxy(request):
    """Whether this request's immediate peer is this host's proxy."""
    getter = _meta_get(request)
    if getter is None:
        return False
    peer = (getter("REMOTE_ADDR") or "").strip()
    return peer in TRUSTED_PROXY_PEERS


def resolve_attendance_client_ip(request):
    """The employee's public address, or None when it cannot be trusted.

    Two ways a caller can satisfy this:

    * it is a real Django/DRF request, and the address is resolved here
      from its peer and its headers;
    * it already resolved the address at that boundary and carried it
      across on a `client_ip` attribute — which is how the mobile API
      reaches `perform_clock_in`/`perform_clock_out` through the
      synthetic request, without the synthetic request having to carry
      headers, cookies or anything else it has no business holding.

    An already-resolved value is still parsed rather than taken as given,
    so a caller cannot smuggle a string past the validation by putting it
    on the attribute instead of in a header.
    """
    carried = getattr(request, "client_ip", None)
    if carried is not None:
        return _parse(carried)

    if not peer_is_trusted_proxy(request):
        # Either a direct connection, or a peer that is not this host's
        # proxy. Its forwarded headers are unverifiable input.
        return None

    getter = _meta_get(request)
    return _parse(getter(CLIENT_IP_HEADER))


def client_ip_is_allowed(client_ip, allowed_ips):
    """Whether `client_ip` falls inside any configured range.

    `allowed_ips` is the list stored on `AttendanceAllowedIP`. Entries
    are parsed with `strict=False`, so both a bare address and a prefix
    work and a lone IPv4 address behaves as that address `/32`. An entry
    that will not parse is skipped rather than raising — one bad row must
    not stop the rest of the list being honoured — and an address family
    that does not match simply does not contain the address, so an
    IPv4-only list refuses an IPv6 client instead of erroring.

    No customer's address appears in this file, and a test asserts that.

    With no usable client address, nothing is allowed.
    """
    if client_ip is None:
        return False
    for entry in allowed_ips or []:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except (ValueError, TypeError):
            continue
        if client_ip.version != network.version:
            continue
        if client_ip in network:
            return True
    return False
