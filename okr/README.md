# OKR module

Objectives, Key Results, check-ins and approvals, ported from the JOYOKR WordPress
theme v1.6.45. The work is staged as gates; this file describes what exists so far.

## Boundary

- **Ownership.** The module owns only tables named `okr_*`. It reads
  `base.Company`, `base.Department` and `employee.Employee` and never alters them.
- **Company scoping.** Every row has a mandatory `company`. Saves are refused
  when a referenced cycle, objective, team, department or employee belongs to
  another company. Queries go through `.for_company(company)`.
  `JoydigiCompanyManager` is not used (see SEC-04).
- **Employee foreign keys and HR account deletion.** These FKs follow the
  contract of `employee.services.account_deletion`:

  | Field | `on_delete` | Effect when HR deletes the employee |
  |---|---|---|
  | `owner`, `author`, `actor` | `PROTECT`, nullable | Reference is detached (set to NULL); OKR history is kept |
  | `OKRTeamMember.employee` | `CASCADE` | Membership is removed together with the employee |
  | `OKRRole.employee`, `OKRNotification.recipient` | `CASCADE` | The person's OKR roles and inbox go with them |
  | Team / department `manager` | `SET_NULL` / `CASCADE` | Reference is cleared or the role is removed |

- **History.** Check-ins and activity are append-only: edits and deletes raise
  `AppendOnlyError`. Objectives, key results and comments are soft-deleted
  through `deleted_at`. The default manager hides soft-deleted rows;
  `all_objects` shows them.
- **Evidence files.** Files live in `OKR_PRIVATE_MEDIA_ROOT` (default
  `<BASE_DIR>/private_media/okr`), outside the public `/media/` tree. The
  storage refuses to build URLs.
- **Numbers.** All numbers are `Decimal`:
  - `DECIMAL(7,2)` for percentages and weights;
  - `DECIMAL(18,4)` for metric values.

  SQLite keeps only about 15 significant digits, so full precision needs PostgreSQL.
- **Legacy import.** `legacy_wp_id` columns, `OKRIdentityMap` and
  `OKRMigrationBatch` support a later, separately approved import.

## Business rules (Gate 2, `okr/services/`)

Ported from JOYOKR v1.6.45 and checked in the same order, with the same
error codes (without the `joyokr_` prefix):

- **`calculator.py`**: KR progress, weighted objective progress, expected
  progress, health, rating. Arithmetic is float like PHP, and rounding
  reproduces PHP `round($x, 2)`. `tests/golden/joyokr_calculator.json` holds
  the results of JOYOKR's own PHP code, and the tests compare against it.
- **`access.py`**: who may view, manage, edit or approve.
  - Every active employee has JOYOKR's employee rights.
  - Manager and admin rights come only from `OKRRole` rows in the same
    company. HR admin or superuser status does not grant them.
  - "Manages" means one of three things:
    - the person's HR reporting manager;
    - the `OKRDepartmentRole` holder for the department;
    - the team manager.
- **`okrs.py`**: create, update and delete objectives and KRs; submit,
  approve and reject as one unit; check-in; weight normalisation.
  - Check-ins are idempotent through `client_request_id`.
- **`cycles.py`**:
  - cycle create and update, with no overlap between cycles of the same
    type;
  - status changes: draft→active, active→closed, closed→active;
  - cloning into an empty draft cycle.
- **`notifications.py`**: in-app notifications.
  - A `dedupe_key` is stored only once.
  - Approval requests are settled once handled.
  - E-mail and Telegram delivery are not ported.
- **`engagement.py`**: comments, the activity timeline, and evidence.
  - Allowed evidence files: JPEG, PNG, WebP, PDF, Word and Excel, up to
    10 MB. The file's content must match its extension.
- **`reports.py`**: the personal dashboard, the team report and the
  approval queue.

JOYOKR answered rule violations with 422; here they are 400, as the app
contract requires.

## REST API (Gate 3, `okr/api/`)

`/api/okr/` is mounted from `OkrConfig.ready()`, so no project URL file is
edited. The contract is `appcheckin/docs/okr_api_contract.md`.

- **Authentication**: the project's JWT (`SessionVersionJWTAuthentication`).
  Every permission check runs in the services.
- **Errors**: always `{code, message, fields}`.
- **Not visible**: rows from another company, or ones the caller may not
  see, return 404.
- **Evidence files**: downloaded only through
  `/api/okr/evidence/<id>/download/`, after a visibility check.

### Web endpoints (Phase 1 of the web integration)

These let the JOYOKR web UI run on Django. They are listed in the contract
doc's "Web endpoints" section and tested in `tests/test_web_api.py`.

- **Authentication**: the same views also accept the HR web login session,
  with CSRF checked on every write; JWT keeps working for the app.
- **Services**:
  - `services/organization.py` reads people and departments from HR, and
    manages OKR teams, OKR roles, each department's OKR manager and the
    company settings.
  - `services/reports.py` adds the company dashboard, the report with its
    trend, the alignment tree, the check-in queue and the check-in
    management view.
- **No second user system**: OKR never creates users or departments. HR owns
  them.

## Pilot gate (`okr/pilot.py`)

Two environment settings (read by `joydigi/settings/base.py` at start; a
change takes effect when the service reloads):

| Setting | Values | Effect |
|---|---|---|
| `OKR_ACCESS_MODE` | `all` (default) / `pilot` | `all`: every active employee of a company, as before. `pilot`: only the listed employees. Any other value closes OKR to everyone (logged). |
| `OKR_PILOT_EMPLOYEE_IDS` | HR employee ids, comma separated | Who may use OKR in `pilot` mode. Empty: nobody. |

In `pilot` mode `OKRViewer.for_user` refuses everyone else with
`403 {"code": "okr_not_enabled"}`, so every `/api/okr/` endpoint, the
`/okr/` web (access-denied page) and the HR menu entry are closed — not just
hidden. HR superusers and OKR roles do not bypass it, and it never grants a
role. Objective/key result owners, team members, department OKR managers and
OKR roles must be pilot employees (400 with a warning otherwise); the people
pickers list only them; OKR notifications to anyone else are not stored.
HR, attendance and requests are not affected.

Turn on: `OKR_ACCESS_MODE=pilot` and `OKR_PILOT_EMPLOYEE_IDS=12,57,301` in the
server's `.env`, then restart the service. Add or remove someone: edit the
list, restart. Turn off: `OKR_ACCESS_MODE=all` (or remove it), restart.

## Running locally (isolated)

Use a settings module that points at a throwaway database (SQLite is fine for
development; production uses PostgreSQL). Then:

```bash
python manage.py migrate okr
python manage.py test okr
```

Never run these against the production database.
