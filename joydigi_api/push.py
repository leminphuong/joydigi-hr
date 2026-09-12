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
            logger.info(
                "FIREBASE_CREDENTIALS_FILE is not set; push notifications are "
                "disabled and only in-app notifications will be created."
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
            logger.error("Firebase could not be initialised: %s", error)
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
    tally = {"sent": 0, "failed": 0, "deactivated": 0, "skipped": False}

    tokens = active_tokens_for(user)
    if not tokens:
        tally["skipped"] = True
        return tally

    app = _load_app()
    if app is None:
        tally["skipped"] = True
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
            if any(marker in str(error) for marker in DEAD_TOKEN_ERRORS):
                dead.append(device.token)
            else:
                logger.warning(
                    "push to device %s failed: %s", device.pk, error
                )

    _deactivate(dead)
    tally["deactivated"] = len(dead)
    return tally
