"""Phase ONLINE-2 — working away from the office, with permission.

An approved `RemoteWorkRequest` says an employee *may* work remotely on
a range of dates. It does not say they did, and this module is careful
about the difference throughout.

## The two questions, deliberately kept apart

**Check-in asks the request.** The office path runs first and is
untouched; only when it refuses on network grounds does
`approved_remote_request` get asked whether this employee holds
permission covering today. If they do, the punch is allowed and
recorded as `REMOTE`. If they do not, the original refusal stands,
byte for byte.

**Check-out asks the record, never the request.** It reads the
`CHECK_IN` evidence written for the session it is actually closing
(`remote_check_in_evidence`). This is the security property that makes
the feature safe: an employee holding an approved request who walks
into the office and checks in normally has an OFFICE session, and no
amount of approved permission lets that session be closed from 4G.
Asking "does this person have an approved request today?" at check-out
would grant exactly that bypass, which is why nothing here does it.

Reading the record rather than the request also settles the awkward
case honestly: permission revoked at lunchtime cannot strand a session
that is already open. The employee started the day with authority, the
evidence row says so, and they can close their day.

## What the client is allowed to contribute

Nothing that grants authority. A device may describe itself —
`wifi_ssid`, `wifi_bssid`, `latitude`, `longitude`, `accuracy` — and
those are stored because they are useful to a human reading the record
later. None of them is consulted to decide whether a punch is allowed.
An SSID is a string a phone reports about itself; for a remote punch
it is not expected to match `OfficeWifi` and is never treated as proof
of anything. Distance is computed here from coordinates, never
accepted as a number; the address is resolved from the employee's
peer by the Phase 3B resolver, never read from a payload.
"""

import ipaddress
import logging
from decimal import Decimal

from django.db import IntegrityError, transaction

from attendance.methods.client_ip import resolve_attendance_client_ip
from attendance.models import AttendanceEvidence, RemoteWorkRequest
from base.models import CheckInLocation

logger = logging.getLogger(__name__)

#: How `validate_checkin_source`'s evidence maps onto the stored
#: method code. The keys are the labels the existing code already
#: issues — `AttendanceVerifySourceView._METHODS` plus
#: `CAMERA_AI_METHOD` — so nothing new is invented on the way in.
PROOF_METHOD_CODES = {
    "wifi": AttendanceEvidence.METHOD_WIFI,
    "location": AttendanceEvidence.METHOD_LOCATION,
    "qr": AttendanceEvidence.METHOD_QR,
    "numeric_code": AttendanceEvidence.METHOD_NUMERIC_CODE,
    "camera_ai": AttendanceEvidence.METHOD_FACE,
}


def approved_remote_request(employee, work_date):
    """The permission covering `work_date` for `employee`, or None.

    All four conditions matter, and each closes a different hole:
    the request belongs to this employee (not one borrowed from a
    colleague), it was approved, it was not since canceled —
    `RemoteWorkRequest.request_status` reads `canceled` as "Rejected",
    there is no separate flag — and it is still active.

    `work_date` must be the server's own date for the session being
    opened. Callers pass the same instant the attendance row is
    written from, so a device with a wrong clock cannot widen its own
    permission.

    `.entire()` deliberately bypasses the company-scoped default
    manager: this runs on the mobile API path, where there is no
    selected company in the session, and the employee filter below is
    already the narrowest possible scope — the request must belong to
    the authenticated employee and to nobody else.
    """
    if employee is None or work_date is None:
        return None
    return (
        RemoteWorkRequest.objects.entire()
        .filter(
            employee_id=employee,
            approved=True,
            canceled=False,
            is_active=True,
            start_date__lte=work_date,
            end_date__gte=work_date,
        )
        .order_by("-id")
        .first()
    )


def remote_check_in_evidence(attendance):
    """The `REMOTE` check-in record for this session, or None.

    The only thing that may authorise a remote check-out. `None` means
    the session was not opened remotely — whatever permission the
    employee happens to hold today — and check-out must then follow
    the ordinary office rules.
    """
    if attendance is None or getattr(attendance, "pk", None) is None:
        return None
    return AttendanceEvidence.objects.filter(
        attendance=attendance,
        action=AttendanceEvidence.ACTION_CHECK_IN,
        attendance_mode=AttendanceEvidence.MODE_REMOTE,
    ).first()


def _client_value(request, key):
    """One evidence value as the client supplied it, or None.

    Read the way `validate_checkin_source` reads its own evidence, so
    the synthetic API request and a real form post behave alike.
    """
    getter_get = getattr(getattr(request, "GET", None), "get", None)
    getter_post = getattr(getattr(request, "POST", None), "get", None)
    value = None
    if callable(getter_get):
        value = getter_get(key)
    if value in (None, "") and callable(getter_post):
        value = getter_post(key)
    if value in (None, ""):
        return None
    return value


def _field_max_length(name):
    """The real column width, read from the model rather than repeated.

    Derived so a future migration that widens or narrows a column
    cannot leave a stale literal here disagreeing with the database.
    """
    return AttendanceEvidence._meta.get_field(name).max_length


def _finite(raw):
    """`raw` as a finite float, or None. Never raises on client input.

    Rejects NaN and ±Infinity. `float("inf")` parses happily and then
    fails at the database, which is precisely the shape of failure
    this module must not hand downstream.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _coordinate(raw, limit):
    """A coordinate the column can actually hold, or None.

    Phase ONLINE-2H. Two checks, and they are not the same check.
    `-90 <= value <= 90` is what a latitude *means*; fitting
    `numeric(9, 6)` is what the column can *store*. A value can pass
    one and fail the other — `1234.5` is not a latitude and also does
    not fit — and only the second one decides whether PostgreSQL
    raises inside the punch transaction.

    Quantised to six decimal places, the column's own scale, so the
    stored number is the one this code checked rather than whatever a
    float-to-decimal conversion later produces.

    Dropping is deliberate, and so is dropping rather than clamping: a
    coordinate clamped to 90.0 would be a location the device never
    reported, written into a record whose whole purpose is to say what
    was observed. `None` says "not known", which is true.
    """
    value = _finite(raw)
    if value is None or not -limit <= value <= limit:
        return None
    return Decimal(str(round(value, 6)))


def _accuracy(raw):
    """A usable accuracy in metres, or None.

    Negative accuracy is not a reading, it is a bug or a probe, and a
    non-finite one cannot be stored. Neither is worth keeping; the
    field is descriptive and `None` is an honest answer.
    """
    value = _finite(raw)
    if value is None or value < 0:
        return None
    return value


def _text(raw, field_name):
    """A client string the column can hold, or None.

    Phase ONLINE-2H. Over-length values are dropped, never truncated.
    Truncation would invent an SSID that no device ever reported and
    store it in a record a human later reads as fact; the shorter lie
    is worse than the gap.

    Safe to drop because this runs *after* `validate_checkin_source`
    has already made its decision on the raw value — the SSID and
    BSSID here are observation, not the comparison that authorised
    anything, so nothing about the verdict can change by dropping
    them. See this module's header.
    """
    if raw is None:
        return None
    text = str(raw)
    max_length = _field_max_length(field_name)
    if max_length is not None and len(text) > max_length:
        return None
    return text


def _method_code(code):
    """A method code the field's own choices allow, or None."""
    if code is None:
        return None
    valid = {value for value, _label in AttendanceEvidence.METHOD_CHOICES}
    return code if code in valid else None


def evidence_method(request):
    """Which method this punch used, as a stored code, or None.

    Derived from the shape of the evidence actually present, in the
    same branch order as `validate_checkin_source`, so the two cannot
    disagree about what a request was.

    A punch made with a `verification_proof` records `None`. The proof
    does carry the server-issued label, but `validate_checkin_source`
    only returns it folded into a Vietnamese display string
    ("Đã xác thực trước (wifi)") with no machine-readable key, and
    this phase does not change that function. Exposing the label
    properly is an additive one-line change for a later phase;
    `method` is nullable and descriptive, so nothing depends on it.
    """
    if _client_value(request, "qr_token"):
        return AttendanceEvidence.METHOD_QR
    if _client_value(request, "numeric_code"):
        return AttendanceEvidence.METHOD_NUMERIC_CODE
    if _client_value(request, "wifi_ssid"):
        return AttendanceEvidence.METHOD_WIFI
    if _finite(_client_value(request, "latitude")) is not None:
        return AttendanceEvidence.METHOD_LOCATION
    return None


def nearest_location_and_distance(company, latitude, longitude):
    """`(location, metres)` for the nearest active office, or `(None, None)`.

    Uses the company's existing `CheckInLocation` rows and the
    existing Haversine helper — the same numbers the radius check
    already works with, so a distance stored here means exactly what
    it means everywhere else. Imported inside the function because
    `attendance.views.clock_in_out` imports this package; at call time
    the module is long since loaded.

    This is measurement, not a gate. A remote punch is authorised by
    the approved request, and being 12km from the office is the
    expected result rather than a problem.
    """
    if latitude is None or longitude is None or company is None:
        return None, None
    from attendance.views.clock_in_out import _distance_meters

    # Phase ONLINE-2H: the coordinates arrive as `Decimal`, because that
    # is what the column stores, but `Decimal` and `float` do not mix in
    # arithmetic — `float - Decimal` raises. Coerced once here rather
    # than relying on the loop's `except`, which would silently turn a
    # type error into "no nearest location" and lose the distance.
    latitude = float(latitude)
    longitude = float(longitude)

    locations = list(
        CheckInLocation.objects.filter(company_id=company, is_active=True)
    )
    if not locations:
        return None, None
    nearest = None
    nearest_distance = None
    for location in locations:
        try:
            distance = _distance_meters(
                latitude, longitude, float(location.latitude), float(location.longitude)
            )
        except (TypeError, ValueError):
            continue
        if nearest_distance is None or distance < nearest_distance:
            nearest, nearest_distance = location, distance
    if nearest is None:
        return None, None
    return nearest, max(0, int(round(nearest_distance)))


def _resolved_client_ip(request):
    """The employee's address as text, or None — Phase 3B only."""
    resolved = resolve_attendance_client_ip(request)
    if resolved is None:
        return None
    try:
        return str(ipaddress.ip_address(str(resolved)))
    except ValueError:
        return None


def _apply(evidence, values):
    """Write `values` onto `evidence` through the model's own save."""
    for field, value in values.items():
        setattr(evidence, field, value)
    evidence.save()
    return evidence


def _upsert_evidence(attendance, action, values):
    """One row per `(attendance, action)`, without locking anything.

    Phase ONLINE-2H. This replaces `update_or_create`, which reads
    like the obvious tool and is not: Django 5.2 implements it as
    `self.select_for_update().get_or_create(...)`
    (`django/db/models/query.py`), so every punch would take a row
    lock. This project forbids `FOR UPDATE` on the attendance path for
    a concrete reason — a previous release took production down with
    it — and "the lock is harmless here" is an argument that stays
    true only until somebody gives this model a company-scoped
    manager, at which point `DISTINCT` plus `FOR UPDATE` reproduces
    that outage exactly.

    So: plain `SELECT`, then `UPDATE` or `INSERT`.

    The race this leaves open is the honest one. Two requests can both
    find nothing and both try to insert; the unique constraint lets
    one win, and the loser catches `IntegrityError` and updates the
    winner's row instead. That is the same outcome the lock would have
    produced, reached a step later.

    Two details make the recovery safe rather than a swallowed error:

    * the `INSERT` sits in its own `transaction.atomic()` savepoint,
      so a failed insert does not poison the caller's punch
      transaction — without it, PostgreSQL would refuse every
      subsequent statement in that transaction;
    * the `IntegrityError` is only absorbed when a row for this exact
      `(attendance, action)` then turns out to exist. If it does not,
      the constraint that fired was a different one — a bad foreign
      key, say — and it is re-raised untouched.
    """
    existing = AttendanceEvidence.objects.filter(
        attendance=attendance, action=action
    ).first()
    if existing is not None:
        return _apply(existing, values)
    try:
        with transaction.atomic():
            return AttendanceEvidence.objects.create(
                attendance=attendance, action=action, **values
            )
    except IntegrityError:
        raced = AttendanceEvidence.objects.filter(
            attendance=attendance, action=action
        ).first()
        if raced is None:
            raise
        return _apply(raced, values)


def record_evidence(
    *,
    attendance,
    action,
    attendance_mode,
    request,
    company,
    captured_at,
    remote_work_request=None,
):
    """Persist what the server observed for this punch.

    An upsert, because `unique(attendance, action)` is a real
    constraint and a second punch of the same kind against the same
    day is a real situation: `Attendance` is unique per employee per
    date, so an employee who checks in, checks out and checks in again
    lands on the same row. Overwriting keeps the record describing the
    session that is currently open, which is the one check-out reads.

    Every client-derived value is normalised to something the column
    can actually hold before it goes anywhere near the database — see
    `_text`, `_coordinate` and `_accuracy`. This is not tidiness. The
    write happens inside the caller's punch transaction, so a value
    PostgreSQL refuses does not merely lose the evidence: it aborts
    the transaction and the employee cannot clock in. SQLite accepts
    over-long strings silently, so the test suite alone would never
    show it.
    """
    latitude = _coordinate(_client_value(request, "latitude"), 90)
    longitude = _coordinate(_client_value(request, "longitude"), 180)
    # A lone coordinate locates nothing, and storing half of one
    # invites a later reader to plot it against a default.
    if latitude is None or longitude is None:
        latitude = longitude = None
    location, distance = nearest_location_and_distance(company, latitude, longitude)
    values = {
        "attendance_mode": attendance_mode,
        "remote_work_request": remote_work_request,
        "method": _method_code(evidence_method(request)),
        "wifi_ssid": _text(_client_value(request, "wifi_ssid"), "wifi_ssid"),
        "wifi_bssid": _text(_client_value(request, "wifi_bssid"), "wifi_bssid"),
        "latitude": latitude,
        "longitude": longitude,
        "accuracy": _accuracy(_client_value(request, "accuracy")),
        "distance_meters": distance,
        "location": location,
        "client_ip": _resolved_client_ip(request),
        "captured_at": captured_at,
    }
    return _upsert_evidence(attendance, action, values)


def record_session_start(
    *, attendance, request, company, captured_at, remote_work_request=None
):
    """Phase ONLINE-2F. Record how the session now open was authorised.

    Called after **every** successful check-in, office or remote, and
    that is the whole point of it.

    `unique(attendance, action)` means one check-in row per
    `Attendance`, and `Attendance` is unique per employee per date —
    so an employee who works the morning from home, closes the day,
    and then comes into the office after lunch lands back on the same
    row. Writing evidence only for remote punches left the morning's
    `REMOTE` check-in in place, describing a session that had already
    ended, and `remote_check_in_evidence` would go on believing it:
    the afternoon's office session could then be closed from mobile
    data. Recording the office punch too is what keeps the row
    describing the session that is actually open.

    The invariant, stated once: **the `CHECK_IN` row always describes
    the most recently opened session of this `Attendance`.** Not its
    history — `unique(attendance, action)` makes history impossible
    here by design, and this phase does not change that.

    The previous session's `CHECK_OUT` row is removed for the same
    reason. Left behind it would claim the newly opened session was
    already closed, which is the same kind of lie in the other
    direction. Nothing reads it for authority — `remote_check_in_
    evidence` filters on `action=CHECK_IN`, and no serializer, API or
    template reads this model at all — so removing it cannot change
    any decision the system makes.
    """
    if attendance is None or getattr(attendance, "pk", None) is None:
        return None
    mode = (
        AttendanceEvidence.MODE_REMOTE
        if remote_work_request is not None
        else AttendanceEvidence.MODE_OFFICE
    )
    evidence = record_evidence(
        attendance=attendance,
        action=AttendanceEvidence.ACTION_CHECK_IN,
        attendance_mode=mode,
        request=request,
        company=company,
        captured_at=captured_at,
        remote_work_request=remote_work_request,
    )
    AttendanceEvidence.objects.filter(
        attendance=attendance, action=AttendanceEvidence.ACTION_CHECK_OUT
    ).delete()
    return evidence
