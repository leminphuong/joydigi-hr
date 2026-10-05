"""JOYDIGI standard working week — Mon-Fri 08:00-17:00, weekend off.

One-off maintenance, driven by
`.github/workflows/apply-production-standard-shift.yml`. It is run through
`manage.py shell` so that it executes inside the project's own settings and
ORM, and so that `shell` in argv keeps every scheduler in this project from
starting a background job for its duration (attendance, base, employee, leave
and pg_backup all guard on that).

    APPLY_MODE=dry_run ./venv/bin/python manage.py shell \
        -c "exec(open('scripts/apply_standard_shift.py', encoding='utf-8').read())"

`APPLY_MODE` is required and must be exactly `dry_run` or `apply`. There is no
default: a mode this script does not recognise is an abort, not an apply.

WHY THIS EXISTS
---------------
The default shift was 08:30-17:30 with a schedule row on all seven days, and
that single piece of configuration produced three separate complaints at once:
check-in reminders arrived "around 08:30" (the engine is shift-relative and
correctly fired at start-5m/start+5m), reminders arrived at the weekend (a
shift with a weekend schedule row is a shift that works weekends), and a day
with an 08:25 arrival and a 17:16 departure showed orange with "Đi muộn = 0"
(against an 08:30-17:30 shift that arrival is on time and that departure is
early). Nothing in the reminder engine, the late/early rule or the calendar
needs changing — they all read the shift. This changes the shift.

The four reminder moments are NOT written anywhere in here. They are derived
from the new shift times and the engine's own `REMINDER_LEAD`/`REMINDER_GRACE`,
so the printed expectation cannot drift away from what the engine will do.

WHAT IT TOUCHES
---------------
`EmployeeShiftSchedule` (five updates, two deletes) and `CompanyLeaves` (two
links), for one company and one shift. Nothing else — `GraceTime` and
`CheckInPolicy` are read and reported, never written.

It never writes to Attendance, AttendanceLateComeEarlyOut, Notification,
OvertimeRequest or RemoteWorkRequest, and STEP 8 proves that by content
checksum rather than by assertion: a count alone would miss an UPDATE.

WHY DELETE AND NOT `is_active=False` FOR THE WEEKEND ROWS
---------------------------------------------------------
Disabling them would be a silent no-op.
`attendance.methods.reminders._schedules_for` reads
`EmployeeShiftSchedule.objects.filter(day__day__in=...)`, and `.filter()` never
applies the manager's is_active rule — the `all()` override that does apply it
returns early when there is no thread-local request, which is exactly the
scheduler's situation. An inactive weekend row would still produce weekend
reminders. Deletion is the only thing that works, and both rows are printed in
full before they go, so they can be recreated by hand if ever needed.

EXIT CODES
----------
0  success (dry run OK, applied OK, or already applied — nothing to do)
2  aborted, nothing was written
3  written but the after-state or the history check did not verify

Rehearsed end to end against a database holding the exact pre-state: dry run,
apply, the abort gate (one row perturbed) and the idempotence gate.
"""

import hashlib
import os
import sys
from datetime import datetime, time, timedelta

from django.db import transaction

from attendance.methods.reminders import REMINDER_GRACE, REMINDER_LEAD
from attendance.models import (
    Attendance,
    AttendanceLateComeEarlyOut,
    GraceTime,
    OvertimeRequest,
    RemoteWorkRequest,
)
from base.models import (
    CheckInPolicy,
    Company,
    CompanyLeaves,
    EmployeeShift,
    EmployeeShiftSchedule,
)
from employee.models import EmployeeWorkInformation
from notifications.models import Notification

# --------------------------------------------------------------------- target
COMPANY_NAME = "JOYDIGI"
SHIFT_NAME = "Ca hành chính"

NEW_START = time(8, 0)
NEW_END = time(17, 0)

#: The shape production must already be in. Anything else aborts.
EXPECTED_OLD_START = time(8, 30)
EXPECTED_OLD_END = time(17, 30)

WORKING_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
WEEKEND_DAYS = ("saturday", "sunday")

#: `base.models.WEEK_DAYS` is Monday='0' … Sunday='6', the same numbering as
#: `datetime.date.weekday()`, so 5 and 6 are Saturday and Sunday. Asserted
#: below rather than trusted.
WEEKEND_WEEK_DAY_CODES = ("5", "6")
WEEK_NAME = {"0": "Mon", "1": "Tue", "2": "Wed", "3": "Thu", "4": "Fri", "5": "Sat", "6": "Sun"}

EXPECTED_SCHEDULE_UPDATES = 5
EXPECTED_SCHEDULE_DELETES = 2

DRY_RUN_OK = "PRODUCTION_SHIFT_DRY_RUN_OK"
APPLIED_OK = "PRODUCTION_STANDARD_SHIFT_APPLIED"
ALREADY_APPLIED = "PRODUCTION_SHIFT_ALREADY_APPLIED"
ABORTED = "ABORTED_NO_CHANGES"


def rule(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def fmt(value):
    return value.strftime("%H:%M") if value is not None else "-"


def die(code, *lines):
    """Stop without having written anything, loudly enough for a CI log."""
    rule(ABORTED)
    for line in lines:
        print("  !! %s" % line)
    print("\n  Nothing was changed. Report this output before applying anything.")
    print("\n" + ABORTED)
    sys.exit(code)


# ------------------------------------------------------------------ the mode
mode = (os.environ.get("APPLY_MODE") or "").strip()
if mode not in ("dry_run", "apply"):
    die(
        2,
        "APPLY_MODE must be exactly 'dry_run' or 'apply'; got %r." % mode,
        "There is no default — an unrecognised mode never applies.",
    )
DRY_RUN = mode == "dry_run"

rule("MODE")
print("  APPLY_MODE = %s  (%s)" % (mode, "read-only" if DRY_RUN else "WILL WRITE"))

# The numbering this script and the reminder engine both depend on. If this
# ever fails, every weekday comparison below is off by some amount and the
# weekend is not the weekend.
if WEEKEND_WEEK_DAY_CODES != (str(datetime(2026, 10, 3).weekday()), str(datetime(2026, 10, 4).weekday())):
    die(2, "WEEK_DAYS numbering is not Monday=0 … Sunday=6; refusing to guess which day is the weekend.")


# ---------------------------------------------------------- history snapshots
HISTORY_SPEC = (
    (
        Attendance,
        (
            "pk",
            "attendance_date",
            "attendance_clock_in",
            "attendance_clock_out",
            "attendance_validated",
            "attendance_worked_hour",
        ),
        "Attendance",
    ),
    (
        AttendanceLateComeEarlyOut,
        ("pk", "attendance_id", "type"),
        "AttendanceLateComeEarlyOut",
    ),
    (Notification, ("pk", "timestamp", "verb"), "Notification"),
    (OvertimeRequest, ("pk", "approved", "canceled"), "OvertimeRequest"),
    (RemoteWorkRequest, ("pk", "approved", "canceled"), "RemoteWorkRequest"),
)


def fingerprint(model, fields, label):
    """Count + sha256 over ordered row content.

    Read-only, and deliberately not a count on its own: a count cannot see an
    UPDATE. Only a truncated digest is printed, so no row content reaches the
    log.
    """
    rows = model.objects.all().order_by("pk").values_list(*fields)
    digest = hashlib.sha256(
        "\n".join("|".join(str(v) for v in row) for row in rows.iterator()).encode("utf-8")
    ).hexdigest()[:16]
    return {"label": label, "count": rows.count(), "sha": digest}


def history_snapshot():
    return [fingerprint(model, fields, label) for model, fields, label in HISTORY_SPEC]


# ======================================================================
# STEP 1 — resolve the target, read-only
# ======================================================================
rule("STEP 1 — RESOLVE TARGET (read-only)")

# `.strip()` and case-insensitivity on purpose: a name differing only by case
# or a trailing space is the same company, and aborting on that would be a
# false alarm. Two genuinely different matches still abort.
companies = [
    c
    for c in Company.objects.all()
    if (c.company or "").strip().casefold() == COMPANY_NAME.casefold()
]
print("  companies matching %r : %d" % (COMPANY_NAME, len(companies)))
for c in companies:
    print("    id=%s name=%r" % (c.pk, c.company))
if len(companies) != 1:
    die(
        2,
        "expected exactly one company named %r, found %d" % (COMPANY_NAME, len(companies)),
        "ambiguous or missing target — this script edits one company only",
    )
company = companies[0]

shifts = [
    s
    for s in EmployeeShift.objects.entire().all()
    if (s.employee_shift or "").strip().casefold() == SHIFT_NAME.casefold()
]
print("  shifts matching %r : %d" % (SHIFT_NAME, len(shifts)))
for s in shifts:
    print(
        "    id=%s name=%r companies=%s grace_time_id=%s"
        % (
            s.pk,
            s.employee_shift,
            sorted(s.company_id.values_list("pk", flat=True)),
            s.grace_time_id_id,
        )
    )
if len(shifts) != 1:
    die(
        2,
        "expected exactly one shift named %r, found %d" % (SHIFT_NAME, len(shifts)),
        "duplicate shift names are exactly the ambiguity this gate exists for",
    )
shift = shifts[0]

shift_companies = sorted(shift.company_id.values_list("pk", flat=True))
if shift_companies and company.pk not in shift_companies:
    die(
        2,
        "shift id=%s does not belong to company id=%s (companies=%s)"
        % (shift.pk, company.pk, shift_companies),
        "refusing to edit another company's shift",
    )

employees_using_shift = EmployeeWorkInformation.objects.entire().filter(shift_id=shift).count()

# ---------------------------------------------------------------- schedule
schedule_rows = list(
    EmployeeShiftSchedule.objects.entire()
    .filter(shift_id=shift)
    .select_related("day")
    .order_by("pk")
)
schedules = {}
duplicates = []
for row in schedule_rows:
    day = row.day.day if row.day else None
    if day in schedules:
        duplicates.append(day)
    schedules[day] = row
if duplicates:
    die(
        2,
        "more than one schedule row for the same weekday: %s" % sorted(set(duplicates)),
        "a duplicate schedule is ambiguous — which row is the shift?",
    )

# ---------------------------------------------------------------- grace
shift_grace = shift.grace_time_id
default_graces = list(GraceTime.objects.filter(is_default=True))
policy = CheckInPolicy.objects.filter(company_id=company).first()

# The precedence the late/early rule itself uses: shift GraceTime, then
# CheckInPolicy.late_threshold_minutes, then the default GraceTime.
if shift_grace is not None and shift_grace.is_active:
    grace_in_secs = shift_grace.allowed_time_in_secs or 0
    grace_out_secs = shift_grace.allowed_time_in_secs or 0
    grace_in_source = "shift GraceTime pk=%s" % shift_grace.pk
    grace_out_source = grace_in_source
elif policy is not None and policy.late_threshold_minutes is not None:
    grace_in_secs = int(policy.late_threshold_minutes) * 60
    grace_out_secs = 0
    grace_in_source = "CheckInPolicy.late_threshold_minutes=%s" % policy.late_threshold_minutes
    grace_out_source = "none (no GraceTime)"
else:
    active_default = next((g for g in default_graces if g.is_active), None)
    grace_in_secs = (active_default.allowed_time_in_secs or 0) if active_default else 0
    grace_out_secs = grace_in_secs
    grace_in_source = (
        "default GraceTime pk=%s" % active_default.pk if active_default else "none -> 0"
    )
    grace_out_source = grace_in_source

# ---------------------------------------------------------------- weekly off
weekly_off = {}
for code in WEEKEND_WEEK_DAY_CODES:
    rows = [
        cl
        for cl in CompanyLeaves.objects.entire().all().prefetch_related("company_id")
        if str(cl.based_on_week_day) == code
    ]
    weekly_off[code] = rows

# ======================================================================
# STEP 2 — BEFORE, in the shape the phase asks for
# ======================================================================
rule("STEP 2 — BEFORE")
print("BEFORE:")
print("  company=%s (id=%s)" % (company.company, company.pk))
print("  shift=%s (id=%s)" % (shift.employee_shift, shift.pk))
print("  employees_using_shift=%s" % employees_using_shift)

print("\nSCHEDULE:")
for day in WORKING_DAYS + WEEKEND_DAYS:
    row = schedules.get(day)
    if row is None:
        print("  %-10s NO SCHEDULE ROW" % day.capitalize())
    else:
        print(
            "  %-10s %s -> %s  night=%s min_hour=%s is_active=%s (pk=%s)"
            % (
                day.capitalize(),
                fmt(row.start_time),
                fmt(row.end_time),
                row.is_night_shift,
                row.minimum_working_hour,
                row.is_active,
                row.pk,
            )
        )
unexpected = sorted(set(schedules) - set(WORKING_DAYS) - set(WEEKEND_DAYS))
if unexpected:
    print("  other weekdays present: %s" % unexpected)

print("\nGRACE:")
print(
    "  shift_grace=%s"
    % (
        "pk=%s active=%s secs=%s" % (shift_grace.pk, shift_grace.is_active, shift_grace.allowed_time_in_secs)
        if shift_grace
        else "NONE"
    )
)
print(
    "  default_grace=%s"
    % (
        ", ".join(
            "pk=%s active=%s secs=%s" % (g.pk, g.is_active, g.allowed_time_in_secs)
            for g in default_graces
        )
        or "NONE"
    )
)
print(
    "  late_threshold_minutes=%s"
    % (policy.late_threshold_minutes if policy else "NO CheckInPolicy FOR THIS COMPANY")
)
print("  grace_in  source=%s -> %d min" % (grace_in_source, grace_in_secs // 60))
print("  grace_out source=%s -> %d min" % (grace_out_source, grace_out_secs // 60))

print("\nCOMPANY_LEAVES:")
for code in WEEKEND_WEEK_DAY_CODES:
    applies = []
    for cl in weekly_off[code]:
        linked = sorted(cl.company_id.values_list("pk", flat=True))
        every_week = cl.based_on_week is None
        applies.append(
            "pk=%s week=%s is_active=%s companies=%s"
            % (cl.pk, "EVERY" if every_week else cl.based_on_week, cl.is_active, linked or "ALL")
        )
    print("  %s=%s" % (WEEK_NAME[code], "; ".join(applies) or "NONE"))

# ======================================================================
# STEP 3 — precondition gate
# ======================================================================
rule("STEP 3 — PRECONDITION")

present = {day: schedules[day] for day in WORKING_DAYS + WEEKEND_DAYS if day in schedules}
old_shape = len(present) == 7 and all(
    row.start_time == EXPECTED_OLD_START and row.end_time == EXPECTED_OLD_END
    for row in present.values()
)
new_shape = sorted(present) == sorted(WORKING_DAYS) and all(
    present[day].start_time == NEW_START and present[day].end_time == NEW_END
    for day in WORKING_DAYS
)

if new_shape:
    print("  ALREADY APPLIED — Mon-Fri %s-%s, no weekend schedule." % (fmt(NEW_START), fmt(NEW_END)))
    rule("NOTHING TO DO")
    print("  The shift is already in the standard shape. No write was attempted.")
    print("\n" + ALREADY_APPLIED)
    sys.exit(0)

if not old_shape:
    die(
        2,
        "pre-state does not match the expected %s-%s on all seven days"
        % (fmt(EXPECTED_OLD_START), fmt(EXPECTED_OLD_END)),
        "found: %s"
        % {
            day: "%s-%s" % (fmt(row.start_time), fmt(row.end_time))
            for day, row in sorted(present.items())
        },
    )
print(
    "  matches the expected pre-state: %s-%s on all seven days."
    % (fmt(EXPECTED_OLD_START), fmt(EXPECTED_OLD_END))
)

# ======================================================================
# STEP 4 — ROWS_TO_CHANGE
# ======================================================================
rule("STEP 4 — ROWS_TO_CHANGE")

updates = [schedules[day] for day in WORKING_DAYS]
deletes = [schedules[day] for day in WEEKEND_DAYS if day in schedules]

#: What `CompanyLeaves` work is outstanding, decided here so the dry run
#: reports exactly what the apply will do.
leave_plan = []
for code in WEEKEND_WEEK_DAY_CODES:
    every_week = [cl for cl in weekly_off[code] if cl.based_on_week is None]
    inert = [cl for cl in weekly_off[code] if cl.based_on_week == ""]
    if inert and not every_week:
        # `_company_weekly_off` treats `based_on_week is None` as every week and
        # compares anything else against the week-of-month, so an empty string
        # matches no week at all. Normalising it would silently switch the
        # weekend off for every company linked to that row, so this is a human
        # decision, not one to guess at.
        die(
            2,
            "CompanyLeaves pk=%s for %s has based_on_week='' (empty, not NULL)"
            % (inert[0].pk, WEEK_NAME[code]),
            "that row matches no week and is possibly shared with other companies;"
            " normalising it is not this script's call",
        )
    if not every_week:
        leave_plan.append({"code": code, "action": "create+link", "row": None})
        continue
    row = every_week[0]
    linked = set(row.company_id.values_list("pk", flat=True))
    if company.pk in linked:
        if row.is_active:
            leave_plan.append({"code": code, "action": "already correct", "row": row})
        elif linked == {company.pk}:
            leave_plan.append({"code": code, "action": "reactivate", "row": row})
        else:
            die(
                2,
                "CompanyLeaves pk=%s for %s is inactive and shared with companies %s"
                % (row.pk, WEEK_NAME[code], sorted(linked)),
                "reactivating it would change another company's calendar",
            )
    else:
        leave_plan.append({"code": code, "action": "link", "row": row})

print("ROWS_TO_CHANGE:")
print("  schedule_updates=%d" % len(updates))
for row in updates:
    print(
        "    pk=%s %-10s %s-%s -> %s-%s"
        % (
            row.pk,
            row.day.day,
            fmt(row.start_time),
            fmt(row.end_time),
            fmt(NEW_START),
            fmt(NEW_END),
        )
    )
print("  schedule_deletes=%d" % len(deletes))
for row in deletes:
    print(
        "    pk=%s %-10s %s-%s night=%s min_hour=%s companies=%s  (full row, so it can be recreated)"
        % (
            row.pk,
            row.day.day,
            fmt(row.start_time),
            fmt(row.end_time),
            row.is_night_shift,
            row.minimum_working_hour,
            sorted(row.company_id.values_list("pk", flat=True)),
        )
    )
print("  company_leave_changes=%d" % sum(1 for p in leave_plan if p["action"] != "already correct"))
for plan in leave_plan:
    print(
        "    %s weekday=%s based_on_week=NULL : %s%s"
        % (
            WEEK_NAME[plan["code"]],
            plan["code"],
            plan["action"],
            "" if plan["row"] is None else " (pk=%s)" % plan["row"].pk,
        )
    )
print("  GraceTime=[]       # read and reported, never written")
print("  CheckInPolicy=[]   # read and reported, never written")

if len(updates) != EXPECTED_SCHEDULE_UPDATES or len(deletes) != EXPECTED_SCHEDULE_DELETES:
    die(
        2,
        "expected %d updates and %d deletes, planned %d and %d"
        % (EXPECTED_SCHEDULE_UPDATES, EXPECTED_SCHEDULE_DELETES, len(updates), len(deletes)),
    )

# ======================================================================
# STEP 5 — what the change implies, all of it derived
# ======================================================================
rule("STEP 5 — DERIVED EXPECTATION (nothing below is hardcoded)")

ANY_DAY = datetime(2026, 1, 5)  # a Monday; only the clock part is used
start_at = datetime.combine(ANY_DAY.date(), NEW_START)
end_at = datetime.combine(ANY_DAY.date(), NEW_END)

print("  reminders, from the shift and the engine's own windows:")
print("    REMINDER_LEAD=%s  REMINDER_GRACE=%s" % (REMINDER_LEAD, REMINDER_GRACE))
print("    check-in  #1  %s   (start - REMINDER_LEAD)" % fmt(start_at - REMINDER_LEAD))
print("    check-in  #2  %s   (start + REMINDER_GRACE)" % fmt(start_at + REMINDER_GRACE))
print("    check-out #1  %s   (end   - REMINDER_LEAD)" % fmt(end_at - REMINDER_LEAD))
print("    check-out #2  %s   (end   + REMINDER_GRACE)" % fmt(end_at + REMINDER_GRACE))

late_after = start_at + timedelta(seconds=grace_in_secs)
early_before = end_at - timedelta(seconds=grace_out_secs)
print("  attendance verdicts, from the shift and the grace above:")
print("    on time while check_in  <= %s ; late after that" % fmt(late_after))
print("    not early while check_out >= %s ; early before that" % fmt(early_before))
print("  weekend: no schedule row and a weekly off day -> no reminder, no working day")

# ======================================================================
# STEP 6 — history BEFORE
# ======================================================================
rule("STEP 6 — HISTORY FINGERPRINT BEFORE")
before = history_snapshot()
for f in before:
    print("  %-28s count=%-8d sha=%s" % (f["label"], f["count"], f["sha"]))

if DRY_RUN:
    rule("DRY RUN COMPLETE — NOTHING WAS WRITTEN")
    print("  Everything above is what an apply WOULD change.")
    print("  Re-run the workflow with mode=apply to perform it.")
    print("\n" + DRY_RUN_OK)
    sys.exit(0)

# ======================================================================
# STEP 7 — apply, in one transaction
# ======================================================================
rule("STEP 7 — APPLYING (transaction.atomic)")
changed = {"schedule_updated": 0, "schedule_deleted": 0, "company_leaves_changed": 0}

with transaction.atomic():
    for row in updates:
        day = row.day.day
        row.start_time = NEW_START
        row.end_time = NEW_END
        row.is_night_shift = False
        row.save(update_fields=["start_time", "end_time", "is_night_shift"])
        changed["schedule_updated"] += 1
        print("  updated pk=%s %-10s -> %s-%s" % (row.pk, day, fmt(NEW_START), fmt(NEW_END)))

    for row in deletes:
        pk, day = row.pk, row.day.day
        row.delete()
        changed["schedule_deleted"] += 1
        print("  deleted pk=%s %-10s (the weekend has no shift)" % (pk, day))

    for plan in leave_plan:
        code = plan["code"]
        row = plan["row"]
        action = plan["action"]
        if action == "create+link":
            row = CompanyLeaves.objects.entire().create(
                based_on_week=None, based_on_week_day=code, is_active=True
            )
            row.company_id.add(company)
            changed["company_leaves_changed"] += 1
        elif action == "link":
            # `.add()` only ever adds: no other company is removed from a
            # shared row.
            row.company_id.add(company)
            changed["company_leaves_changed"] += 1
        elif action == "reactivate":
            row.is_active = True
            row.save(update_fields=["is_active"])
            changed["company_leaves_changed"] += 1
        print(
            "  CompanyLeaves %s weekday=%s pk=%s action=%s companies_now=%s is_active=%s"
            % (
                WEEK_NAME[code],
                code,
                row.pk,
                action,
                sorted(row.company_id.values_list("pk", flat=True)),
                row.is_active,
            )
        )

# ======================================================================
# STEP 8 — verify after write
# ======================================================================
rule("STEP 8 — VERIFY AFTER WRITE")
after_rows = {
    r.day.day: r
    for r in EmployeeShiftSchedule.objects.entire().filter(shift_id=shift).select_related("day")
}
ok = True
for day in WORKING_DAYS:
    row = after_rows.get(day)
    good = row is not None and row.start_time == NEW_START and row.end_time == NEW_END
    ok = ok and good
    print(
        "  %-10s %s"
        % (
            day.capitalize(),
            "%s -> %s  OK" % (fmt(NEW_START), fmt(NEW_END))
            if good
            else "MISMATCH: %s" % (row and "%s-%s" % (fmt(row.start_time), fmt(row.end_time))),
        )
    )
for day in WEEKEND_DAYS:
    good = day not in after_rows
    ok = ok and good
    print("  %-10s %s" % (day.capitalize(), "NO SCHEDULE  OK" if good else "STILL PRESENT — NOT OK"))

for code in WEEKEND_WEEK_DAY_CODES:
    good = (
        CompanyLeaves.objects.entire()
        .filter(based_on_week=None, based_on_week_day=code, company_id=company, is_active=True)
        .exists()
    )
    ok = ok and good
    print(
        "  CompanyLeaves %s every week for %s: %s"
        % (WEEK_NAME[code], company.company, "OK" if good else "MISSING")
    )

print("  derived reminders now: %s / %s / %s / %s" % (
    fmt(start_at - REMINDER_LEAD),
    fmt(start_at + REMINDER_GRACE),
    fmt(end_at - REMINDER_LEAD),
    fmt(end_at + REMINDER_GRACE),
))

# ======================================================================
# STEP 9 — history AFTER
# ======================================================================
rule("STEP 9 — HISTORY FINGERPRINT AFTER")
after = history_snapshot()
history_ok = True
for b, a in zip(before, after):
    same = b["count"] == a["count"] and b["sha"] == a["sha"]
    history_ok = history_ok and same
    print(
        "  %-28s count %d->%d  sha %s->%s  %s"
        % (b["label"], b["count"], a["count"], b["sha"], a["sha"], "UNCHANGED" if same else "CHANGED !!")
    )

rule("RESULT")
print("  rows changed                 : %s" % changed)
print("  after-state matches expected : %s" % ok)
print("  history untouched            : %s" % history_ok)

if ok and history_ok:
    print("\n" + APPLIED_OK)
    sys.exit(0)

print("\n  FAILED — review the output above. The write was committed; the verify did not pass.")
sys.exit(3)
