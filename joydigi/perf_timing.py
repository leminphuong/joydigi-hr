"""Phase GLOBAL-ADMIN-PERF-B — TEMPORARY request-path timing.

=====================================================================
 TEMPORARY. DELETE ONCE THE ~20s ADMIN REQUEST HAS BEEN LOCATED.
=====================================================================

Phase A narrowed the problem by elimination, using a diagnostic page
that goes through the whole middleware chain and then does almost
nothing: it answered in 264 ms end to end, 9 ms of which was its own
work. Production's cache turned out to be `LocMemCache` at hundredths
of a millisecond, `SELECT 1` took 0.21 ms, and the biggest table the
admin reads has 169 rows. So the twenty seconds are not Redis, not
PostgreSQL latency, and not data volume — and they are not the shared
middleware either, because the diagnostic passes through all of it.

What the diagnostic does *not* do is resolve a heavy admin view, build
a queryset, run eleven context processors, or render the generic table.
That is the remaining space, and this module measures it — by splitting
one request into boundaries that do not overlap and add up to the whole:

    pre    request-phase middleware, plus URL resolution
    ctx    the view's own work: get_queryset, get_context_data,
           everything up to the moment context processors finish
    tpl    template rendering after that moment
    post   response-phase middleware: session save, activity log
    total  the whole thing, inside Django

`ctx` and `tpl` together are the view; `pre + view + post` is `total`.
Whichever of the four is twenty seconds is the answer, and the other
three then say what to leave alone.

Two rules this had to obey, because breaking either would measure a
different program than the one being diagnosed:

*Nothing is evaluated early.* Django querysets are lazy, so a timer
placed "around the queryset" would force it at a point production never
does and move work between boundaries. Instead every boundary here is a
moment that already exists in the request — a middleware entering, a
context processor running, a view returning — so the work stays exactly
where it was and only the clock is new.

*No query is added or repeated.* SQL is counted through
`connection.execute_wrapper`, Django's supported hook for exactly this:
it observes each execute in place. It cannot cause one.

Cache calls are deliberately *not* timed. Phase A measured production's
cache at 0.02–0.06 ms per operation, so it is already excluded, and the
only way to time it here would be to patch a method on an object shared
by every thread in the process — a real risk, to re-answer a question
that has an answer.

Safety: the header carries numbers and fixed metric names, nothing else.
No SQL, no parameters, no ids, no paths, no cache keys, no exception
text. It is sent only to a caller who holds `attendance.add_attendance`
— the same permission that guards the Phase 3B.1 diagnostic — so timing
is never handed to an anonymous visitor.

To remove: delete this module, its two `MIDDLEWARE` entries and its
context processor entry in `joydigi/settings/base.py`, and
`joydigi/tests/test_perf_timing.py`.
"""

import logging
from time import perf_counter

from django.db import connection

logger = logging.getLogger(__name__)

#: Where the timings live for the duration of one request.
REQUEST_ATTR = "_joydigi_perf_timing"

#: Who may see them.
TIMING_PERMISSION = "attendance.add_attendance"


def _now():
    return perf_counter()


def _ms(start, end):
    if start is None or end is None:
        return None
    return round((end - start) * 1000, 2)


def timing_context_mark(request):
    """Context processor: record the instant context processors finish.

    Registered **last**, so by the time it runs every other processor
    has already done its work. Returns an empty mapping, so it adds
    nothing to any template's context and cannot change a rendering.

    Only the first call is recorded. A page that renders several
    templates binds several contexts, and the first one is the boundary
    that matters — after it, everything left is rendering.
    """
    timings = getattr(request, REQUEST_ATTR, None)
    if timings is not None and timings.get("t_ctx_done") is None:
        timings["t_ctx_done"] = _now()
    return {}


class PerfTimingMiddleware:
    """Registered twice: once outermost, once innermost.

    The outer copy owns the request's timing record and writes the
    header. The inner copy sits directly against the view, so the
    instant it hands control on is the instant the view begins, and the
    instant it gets control back is the instant the view has returned —
    which is what separates the view's own time from the response-phase
    middleware that runs after it.

    Both copies are the same class. Which role an instance plays is
    decided by whether the record already exists, so the two
    `MIDDLEWARE` entries stay identical and neither can be added without
    the other quietly doing the wrong thing.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        timings = getattr(request, REQUEST_ATTR, None)
        if timings is None:
            return self._outermost(request)
        return self._innermost(request, timings)

    # -- outermost -----------------------------------------------------

    def _outermost(self, request):
        timings = {
            "t_start": _now(),
            "t_view_start": None,
            "t_ctx_done": None,
            "t_view_end": None,
            "sql_ms": 0.0,
            "sql_count": 0,
        }
        setattr(request, REQUEST_ATTR, timings)

        def observe(execute, sql, params, many, context):
            """Time one execute, in place. Never issues one."""
            started = _now()
            try:
                return execute(sql, params, many, context)
            finally:
                timings["sql_ms"] += (_now() - started) * 1000
                timings["sql_count"] += 1

        try:
            with connection.execute_wrapper(observe):
                response = self.get_response(request)
        except Exception:
            # Measuring must never turn a failure into a different
            # failure: let the original propagate untouched.
            raise

        timings["t_end"] = _now()
        self._attach_header(request, response, timings)
        return response

    # -- innermost -----------------------------------------------------

    def _innermost(self, request, timings):
        timings["t_view_start"] = _now()
        try:
            return self.get_response(request)
        finally:
            timings["t_view_end"] = _now()

    # -- header --------------------------------------------------------

    def _attach_header(self, request, response, timings):
        """Add `Server-Timing`, for an administrator only.

        Everything here is wrapped: a diagnostic that can break a
        response is worse than no diagnostic. A failure leaves the
        response exactly as the application built it.
        """
        try:
            user = getattr(request, "user", None)
            if user is None or not user.has_perm(TIMING_PERMISSION):
                return
            header = self._format(timings)
            if header:
                response["Server-Timing"] = header
        except Exception:
            logger.debug("perf timing header skipped", exc_info=True)

    def _format(self, timings):
        start = timings.get("t_start")
        view_start = timings.get("t_view_start")
        ctx_done = timings.get("t_ctx_done")
        view_end = timings.get("t_view_end")
        end = timings.get("t_end")

        parts = [
            # request-phase middleware below the outer copy, plus routing
            ("pre", _ms(start, view_start)),
            # view's own work, up to the moment context processors end
            ("ctx", _ms(view_start, ctx_done)),
            # template rendering, from that moment until the view returns
            ("tpl", _ms(ctx_done, view_end)),
            # whole view, for responses that render no template at all
            ("view", _ms(view_start, view_end)),
            # response-phase middleware: session save, activity log
            ("post", _ms(view_end, end)),
            # the whole request, inside Django
            ("total", _ms(start, end)),
            # time spent inside database executes
            ("sql", round(timings.get("sql_ms") or 0.0, 2)),
        ]
        rendered = [
            f"{name};dur={value}" for name, value in parts if value is not None
        ]
        # A count, not a duration. `Server-Timing` has no field for one
        # and a separate header would be a second thing to remember to
        # delete, so it rides here under an unmistakable name.
        rendered.append(f"sqlcount;dur={timings.get('sql_count', 0)}")
        return ", ".join(rendered)
