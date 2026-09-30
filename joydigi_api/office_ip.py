"""Keeping the office's public address on the attendance whitelist.

Phase AUTO-OFFICE-PUBLIC-IP-UPDATER.

The office modem gets a new public address whenever it restarts, and until
somebody edits the Allowed IP screen every employee is refused with
"Mạng hiện tại của bạn không được phép dùng để chấm công." An agent on one
fixed office machine posts to `/api/internal/attendance/update-office-ip/`
and this module decides what that does.

Three rules shape everything here.

**The address comes from the request, never from the body.** The caller
proves it is on the office network by *being* on it; it does not get to
assert an address. `attendance.methods.client_ip.resolve_attendance_client_ip`
is the one resolver, so the value written is the same value the attendance
gate will later compare against — if those two ever disagreed, this feature
would whitelist an address that does not admit anybody.

**A failed update must never lock anybody out.** Nothing here disables a
rule, empties a list, or removes the address currently in force. The worst
outcome of an agent that stops running, a wrong secret, or an unreachable
backend is that the whitelist keeps saying what it already said.

**It owns only what it added.** An administrator's manual entries — a VPN
range, a second office — are never pruned, even when one happens to equal an
address this feature also tracks. Ownership is recorded explicitly rather
than inferred from the value.
"""

import hmac
import ipaddress
import logging
from datetime import datetime, timedelta
from hashlib import sha256

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Where this feature keeps its own bookkeeping inside
#: `AttendanceAllowedIP.additional_data`. Deliberately beside `allowed_ips`
#: rather than replacing it: the gate and the admin screen keep reading the
#: list they always read, and neither knows this exists.
STATE_KEY = "office_ip_auto"

#: Auth headers. Named, not guessed, so the agent and the tests agree.
TIMESTAMP_HEADER = "HTTP_X_OFFICE_IP_TIMESTAMP"
NONCE_HEADER = "HTTP_X_OFFICE_IP_NONCE"
SIGNATURE_HEADER = "HTTP_X_OFFICE_IP_SIGNATURE"

#: Every auth failure answers with this one code. Which check failed is
#: written to the log, never to the response: an attacker probing the
#: endpoint learns nothing about which half of the credential was wrong.
UNAUTHORIZED = "UNAUTHORIZED"


def _setting(name, default=None):
    return getattr(settings, name, default)


def configured_secret():
    """The shared secret, or "" when the feature is switched off."""
    return (_setting("OFFICE_IP_UPDATER_SECRET", "") or "").strip()


def configured_company_id():
    """The one company this deployment updates, or 0 when unset.

    Pinned in the environment rather than named in the request, so a
    compromised office agent cannot retarget another company's whitelist —
    there is no field in which to name one. A deployment with several offices
    wants a secret per office and a `key_id` in the signed message; that is a
    deliberate extension, not something to leave half-open now.
    """
    try:
        return int(_setting("OFFICE_IP_UPDATER_COMPANY_ID", 0) or 0)
    except (TypeError, ValueError):
        return 0


def is_configured():
    return bool(configured_secret()) and configured_company_id() > 0


def max_skew_seconds():
    try:
        return int(_setting("OFFICE_IP_UPDATER_MAX_SKEW_SECONDS", 120) or 120)
    except (TypeError, ValueError):
        return 120


def previous_ttl():
    """How long the address being replaced stays valid alongside the new one."""
    try:
        minutes = int(_setting("OFFICE_IP_PREVIOUS_TTL_MINUTES", 120) or 120)
    except (TypeError, ValueError):
        minutes = 120
    return timedelta(minutes=max(0, minutes))


def expected_signature(secret, timestamp, nonce):
    """Hex HMAC-SHA256 over `timestamp.nonce`.

    The nonce is inside the signed message, so a captured request cannot be
    replayed under a fresh nonce, and the timestamp is inside it, so it
    cannot be replayed past the window either.
    """
    message = ("%s.%s" % (timestamp, nonce)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, sha256).hexdigest()


def _nonce_is_fresh(nonce):
    """True the first time a nonce is seen, False on any repeat.

    `cache.add` is atomic, so two simultaneous replays cannot both win. A
    cache that refuses the write is treated as a repeat — refusing an update
    leaves the current address in force, which is the safe direction.
    """
    key = "joydigi:office-ip:nonce:%s" % nonce
    ttl = max_skew_seconds() * 2 + 60
    try:
        return bool(cache.add(key, 1, ttl))
    except Exception:
        logger.warning("OFFICE_IP_NONCE_STORE_UNAVAILABLE: refusing the update")
        return False


def verify_request(request):
    """`(ok, reason)` — whether this request carries a valid credential.

    `reason` is for the log only. The caller must not put it in a response.
    """
    secret = configured_secret()
    if not secret:
        return False, "NOT_CONFIGURED"

    meta = request.META
    timestamp = (meta.get(TIMESTAMP_HEADER) or "").strip()
    nonce = (meta.get(NONCE_HEADER) or "").strip()
    signature = (meta.get(SIGNATURE_HEADER) or "").strip()
    if not timestamp or not nonce or not signature:
        return False, "MISSING_HEADERS"
    if len(nonce) < 8 or len(nonce) > 128:
        # A nonce has to be long enough to be unique and short enough not to
        # be a way to fill the cache.
        return False, "BAD_NONCE"

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False, "BAD_TIMESTAMP"
    skew = abs(int(timezone.now().timestamp()) - sent_at)
    if skew > max_skew_seconds():
        return False, "STALE_TIMESTAMP"

    # Constant time, and only after the cheap checks — a timing difference on
    # the comparison is the one thing worth protecting here.
    if not hmac.compare_digest(
        expected_signature(secret, timestamp, nonce), signature
    ):
        return False, "BAD_SIGNATURE"

    if not _nonce_is_fresh(nonce):
        return False, "REPLAYED_NONCE"

    return True, "OK"


def as_single_host_cidr(client_ip):
    """The resolved address as the exact one-host range to store.

    `/32` for IPv4 and `/128` for IPv6, because a whitelist entry should
    admit the office and nothing adjacent to it. `client_ip_is_allowed`
    parses entries with `strict=False`, so this form is read back exactly as
    written.
    """
    if client_ip is None:
        return None
    try:
        address = ipaddress.ip_address(str(client_ip))
    except ValueError:
        return None
    if address.is_loopback or address.is_unspecified:
        # Loopback means the resolver fell back to the proxy itself. Writing
        # that would whitelist the server, not the office.
        return None
    return "%s/%d" % (address, address.max_prefixlen)


def mask(entry):
    """An address safe to put in a log line: enough to compare, not to reuse."""
    if not entry:
        return "none"
    text = str(entry).split("/")[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return "invalid"
    if address.version == 4:
        octets = text.split(".")
        return "%s.%s.x.x" % (octets[0], octets[1])
    return "%s:…:x" % text.split(":")[0]


def _parse_moment(value):
    if not value:
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # Written by an earlier version, or edited by hand. Treating it as
        # "no expiry recorded" keeps the stand-in address in place, which is
        # the direction that does not lock anybody out.
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def apply_office_ip(additional_data, new_cidr, now=None, ttl=None):
    """Work out the whitelist after the office reports `new_cidr`.

    Returns `(additional_data, outcome)` without touching the database, so
    the decision can be reasoned about and tested on its own. `outcome` is
    one of `initialised`, `rotated`, `unchanged`.

    Rotation is what keeps people working through a modem restart: the
    address being replaced stays valid for `ttl`, so an employee who checked
    in on the old address minutes ago is not locked out mid-morning. After
    that it is pruned — but only if this feature is the one that added it.
    """
    now = now or timezone.now()
    ttl = previous_ttl() if ttl is None else ttl

    data = dict(additional_data or {})
    allowed = list(data.get("allowed_ips") or [])
    state = dict(data.get(STATE_KEY) or {})
    owned = set(state.get("owned") or [])

    current = state.get("current")
    previous = state.get("previous")
    previous_expires_at = _parse_moment(state.get("previous_expires_at"))

    # An expired stand-in goes, unless it is still doing a job (it is the new
    # address, or it is somehow also the current one) or an administrator
    # added it by hand.
    if previous and previous_expires_at and now >= previous_expires_at:
        if previous not in (new_cidr, current):
            if previous in owned and previous in allowed:
                allowed = [entry for entry in allowed if entry != previous]
            owned.discard(previous)
        previous = None
        previous_expires_at = None

    if current == new_cidr:
        outcome = "unchanged"
    elif current is None:
        current = new_cidr
        outcome = "initialised"
    else:
        previous = current
        previous_expires_at = now + ttl
        current = new_cidr
        outcome = "rotated"

    # Both live entries must be present. Appending only what is missing means
    # an entry an administrator already had is left exactly where it was, and
    # is never marked as this feature's to remove.
    for entry in (current, previous):
        if not entry:
            continue
        if entry not in allowed:
            allowed.append(entry)
            owned.add(entry)

    # And nothing else of ours survives. Without this, a modem that changed
    # address several times in quick succession left every one of them on the
    # list for good: each rotation replaced `previous`, and the address it
    # displaced stopped being tracked while staying whitelisted. The contract
    # is the two most recent addresses, so an owned entry that is neither is
    # removed now rather than waiting for an expiry that will never come.
    live = {entry for entry in (current, previous) if entry}
    for entry in sorted(owned - live):
        allowed = [existing for existing in allowed if existing != entry]
        owned.discard(entry)

    state.update(
        {
            "current": current,
            "previous": previous,
            "previous_expires_at": (
                previous_expires_at.isoformat() if previous_expires_at else None
            ),
            "updated_at": now.isoformat(),
            "updates": int(state.get("updates") or 0) + 1,
            "owned": sorted(owned),
        }
    )
    data["allowed_ips"] = allowed
    data[STATE_KEY] = state
    return data, outcome
