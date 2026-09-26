"""
push.py

Sending a notification to somebody's phone through Firebase Cloud
Messaging.

Everything here degrades quietly. Push is an extra channel on top of the
in-app notification, never a prerequisite: a machine without the Firebase
secret, without the SDK installed, or without network access must still
run Django, still record attendance, and still create the in-app
notification. So a missing configuration is a skipped send and a log line,
never an exception that reaches the scheduler.

The credentials come from `settings.FIREBASE_CREDENTIALS_FILE`, a path set
in the deployment environment. No service-account JSON belongs in this
repository, and none is read from anywhere else.
"""

import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

#: Firebase's own names for "this token is dead". A token that comes back
#: with one of these is deactivated rather than retried forever.
DEAD_TOKEN_ERRORS = ("NotRegistered", "InvalidRegistration", "registration-token-not-registered")

#: Phase NOTIFY-2. How a retired registration is actually recognised.
#:
#: `DEAD_TOKEN_ERRORS` above are legacy FCM response strings, and against
#: firebase-admin 6.9.0 they never match anything: the SDK raises
#: `messaging.UnregisteredError`, whose `str()` is the bare sentence
#: "Requested entity was not found." with no marker in it. Verified
#: against the installed SDK rather than assumed. The consequence was
#: that `_deactivate` could never fire and a device uninstalled months
#: ago was still being pushed to on every reminder, forever.
#:
#: Matched on the exception's class and code, which are the parts the SDK
#: actually guarantees. The strings are kept as a fallback for an older
#: SDK or a caller that raises a plain exception.
#:
#: `SenderIdMismatchError` belongs here because that token was issued for
#: a different Firebase project and will never work for this one. Matched
#: by class, never by its `PERMISSION_DENIED` code, which our own
#: credential losing access would also produce — and retiring every
#: device in the estate because of a server-side permission problem is
#: precisely the mistake this list exists to avoid.
DEAD_TOKEN_EXCEPTIONS = frozenset({"UnregisteredError", "SenderIdMismatchError"})
DEAD_TOKEN_CODES = frozenset({"NOT_FOUND"})


def is_dead_token_error(error):
    """Whether `error` means this registration will never work again.

    Everything transient must answer False: `UnavailableError`
    (UNAVAILABLE), `InternalError` (INTERNAL), `DeadlineExceededError`,
    `QuotaExceededError` (RESOURCE_EXHAUSTED) and `ThirdPartyAuthError`
    (UNAUTHENTICATED, an APNs credential problem of ours). A Firebase
    outage must not unregister everybody's phone.
    """
    if type(error).__name__ in DEAD_TOKEN_EXCEPTIONS:
        return True
    if getattr(error, "code", None) in DEAD_TOKEN_CODES:
        return True
    return any(marker in str(error) for marker in DEAD_TOKEN_ERRORS)

#: Phase NOTIFY-2. Why a push did or did not happen, as a value a caller
#: can count rather than a silent `return`.
#:
#: Before this, `send_to_user` answered "no active token" and "Firebase is
#: not configured" with the same `skipped=True` and no log line anywhere,
#: so a deployment with a missing credential and a deployment where
#: nobody had opened the app were indistinguishable from the outside —
#: and both looked exactly like a working system.
#:
#: These are returned, not logged here. `send_to_user` runs once per
#: employee per reminder, so logging inside it would put one line per
#: employee per minute into the journal. The scheduler jobs tally these
#: and log once per run; see `attendance.methods.reminders`.
STATUS_SENT = "PUSH_SEND_SUCCESS"
STATUS_FAILED = "PUSH_SEND_FAILED"
STATUS_NO_ACTIVE_TOKEN = "PUSH_SKIPPED_NO_ACTIVE_TOKEN"
STATUS_NOT_CONFIGURED = "PUSH_SKIPPED_FIREBASE_NOT_CONFIGURED"
STATUS_INVALID_TOKEN = "PUSH_INVALID_TOKEN"

_app_lock = threading.Lock()
_app = None
_app_attempted = False


def _load_app():
    """
    The Firebase app, initialised once, or None when it cannot be.

    Guarded by a lock because several scheduler threads can reach this at
    the same moment, and `firebase_admin.initialize_app` raises if it runs
    twice. Failure is remembered so a misconfigured deployment logs once
    rather than on every reminder.
    """
    global _app, _app_attempted

    with _app_lock:
        if _app is not None or _app_attempted:
            return _app
        _app_attempted = True

        credentials_file = getattr(settings, "FIREBASE_CREDENTIALS_FILE", "")
        if not credentials_file:
            # Phase NOTIFY-2: warning, not info. This is a feature being
            # switched off, and it happens exactly once per process
            # because `_app_attempted` is already set — so there is no
            # spam to weigh against being able to find it. The value is
            # never logged, only the fact that it is empty.
            logger.warning(
                "%s: FIREBASE_CREDENTIALS_FILE is not set; push is disabled "
                "and only in-app notifications will be created.",
                STATUS_NOT_CONFIGURED,
            )
            return None

        try:
            import firebase_admin
            from firebase_admin import credentials
        except ImportError:
            logger.warning(
                "firebase-admin is not installed; push notifications are "
                "disabled and only in-app notifications will be created."
            )
            return None

        try:
            if firebase_admin._apps:
                _app = firebase_admin.get_app()
            else:
                _app = firebase_admin.initialize_app(
                    credentials.Certificate(credentials_file)
                )
        except Exception as error:
            # Bad path, malformed JSON, revoked key — all the same to us.
            # Only the exception's class name: the message can quote the
            # credential path, and a log line is a wider audience than
            # this deserves. Once per process, like the branch above.
            logger.error(
                "PUSH_FIREBASE_INIT_FAILED: Firebase could not be "
                "initialised (%s); push is disabled and only in-app "
                "notifications will be created.",
                type(error).__name__,
            )
            return None
        return _app


def is_configured():
    """Whether a push has any chance of being delivered."""
    return _load_app() is not None


def active_tokens_for(user):
    """The live registration tokens belonging to one user, newest last."""
    from joydigi_api.models import PushDeviceToken

    return list(
        PushDeviceToken.objects.filter(user=user, is_active=True).order_by("id")
    )


def _deactivate(token_values):
    """Retire tokens Firebase has told us are dead."""
    from joydigi_api.models import PushDeviceToken

    if not token_values:
        return
    PushDeviceToken.objects.filter(token__in=token_values).update(is_active=False)


def send_to_user(user, title, body, data=None):
    """
    Push one notification to every device this user has registered.

    Returns a tally rather than raising: a reminder that could not be
    pushed is not a reason to skip the next employee, and the in-app
    notification has already been recorded by the caller either way. One
    dead token does not affect the others — each is sent individually and
    accounted for on its own.
    """
    tally = {
        "sent": 0,
        "failed": 0,
        "deactivated": 0,
        "skipped": False,
        # Phase NOTIFY-2: the reason, alongside the counts. `skipped`
        # alone could not tell "nobody has opened the app" from "this
        # deployment has no Firebase credential", and those need very
        # different responses.
        "status": None,
    }

    tokens = active_tokens_for(user)
    if not tokens:
        tally["skipped"] = True
        tally["status"] = STATUS_NO_ACTIVE_TOKEN
        return tally

    app = _load_app()
    if app is None:
        tally["skipped"] = True
        tally["status"] = STATUS_NOT_CONFIGURED
        return tally

    from firebase_admin import messaging

    dead = []
    for device in tokens:
        message = messaging.Message(
            token=device.token,
            notification=messaging.Notification(title=title, body=body),
            # Strings only: FCM rejects a data payload with other types,
            # and a rejected payload would lose the whole notification.
            data={key: str(value) for key, value in (data or {}).items()},
        )
        try:
            messaging.send(message, app=app)
            tally["sent"] += 1
        except Exception as error:
            tally["failed"] += 1
            if is_dead_token_error(error):
                # Firebase says this registration no longer exists. Only
                # these three markers retire a token; a timeout or a 5xx
                # falls to the branch below and the device is kept, so a
                # Firebase outage cannot unregister everybody's phone.
                dead.append(device.token)
                logger.info(
                    "%s: retiring device %s (%s)",
                    STATUS_INVALID_TOKEN,
                    device.pk,
                    type(error).__name__,
                )
            else:
                # The device primary key, never the token: a token is a
                # credential for pushing to somebody's handset and must
                # not reach a log line.
                logger.warning(
                    "%s: push to device %s failed (%s)",
                    STATUS_FAILED,
                    device.pk,
                    type(error).__name__,
                )

    _deactivate(dead)
    tally["deactivated"] = len(dead)
    tally["status"] = STATUS_SENT if tally["sent"] else STATUS_FAILED
    return tally
