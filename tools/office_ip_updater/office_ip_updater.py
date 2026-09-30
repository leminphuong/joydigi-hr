"""Tell JoyDigi HR what the office's public address is now.

Phase AUTO-OFFICE-PUBLIC-IP-UPDATER. Runs on one fixed machine inside the
office — a PC that is on whenever people are checking in.

What it does: signs a timestamp and a nonce with the shared secret and POSTs
to the backend. That is all. It does **not** look up the public address
anywhere: the backend reads it from the request it receives, which is the
only version of that address anybody should trust, and is exactly the value
the attendance gate will compare against later.

What it never does: print the secret, write the secret to disk, keep
retrying forever, or report success it did not get.

Configuration, from the environment only:

    JOYDIGI_OFFICE_IP_URL     e.g. https://checkin.joydigi.net/api/internal/attendance/update-office-ip/
    JOYDIGI_OFFICE_IP_SECRET  the shared secret, same value as the server's
                              OFFICE_IP_UPDATER_SECRET

Exit codes, so Task Scheduler's "Last Run Result" means something:

    0  the office address is recorded (changed or already correct)
    1  configuration missing
    2  refused by the backend (bad or missing credential) — will not retry
    3  the backend could not be reached after the bounded retries
    4  the backend could not work out our address

Standard library only: nothing to install, nothing to keep updated on a
machine nobody logs into.
"""

import hashlib
import hmac
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request

#: Three tries over about half a minute, then give up until the next run.
#: The schedule is what provides persistence — retrying in a loop here would
#: turn one unreachable backend into a machine that never stops calling it.
ATTEMPTS = 3
BACKOFF_SECONDS = (5, 15)
TIMEOUT_SECONDS = 15


def log(message):
    """One line, timestamped, never containing the secret or the signature."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print("%s office-ip-updater: %s" % (stamp, message), flush=True)


def signed_headers(secret):
    """The credential: HMAC-SHA256 over `timestamp.nonce`.

    The nonce makes each call single-use on the server, and the timestamp
    bounds how long a captured call could be replayed even before that.
    """
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    message = ("%s.%s" % (timestamp, nonce)).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return {
        "X-Office-IP-Timestamp": timestamp,
        "X-Office-IP-Nonce": nonce,
        "X-Office-IP-Signature": signature,
        "Content-Type": "application/json",
        "User-Agent": "joydigi-office-ip-updater/1.0",
    }


def post_once(url, secret):
    """`(status, payload)`; `status` is None when the backend was unreachable.

    The body is empty on purpose. The server ignores any address a client
    sends, so sending one would only invite somebody to believe it matters.
    """
    request = urllib.request.Request(
        url, data=b"{}", headers=signed_headers(secret), method="POST"
    )
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=context
        ) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, _parse(body)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace") if error.fp else ""
        return error.code, _parse(body)
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as error:
        # Only the class name: a socket error's text can carry the host and
        # sometimes the proxy, and this log may be collected.
        log("unreachable (%s)" % type(error).__name__)
        return None, {}


def _parse(body):
    try:
        parsed = json.loads(body) if body else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main():
    url = (os.environ.get("JOYDIGI_OFFICE_IP_URL") or "").strip()
    secret = os.environ.get("JOYDIGI_OFFICE_IP_SECRET") or ""
    if not url or not secret:
        # Never echo which one is missing beyond its name — and never its
        # value.
        log("not configured: set JOYDIGI_OFFICE_IP_URL and JOYDIGI_OFFICE_IP_SECRET")
        return 1
    if not url.lower().startswith("https://"):
        log("refusing to send the credential over a non-HTTPS URL")
        return 1

    for attempt in range(1, ATTEMPTS + 1):
        status, payload = post_once(url, secret)

        if status == 200:
            log(
                "ok: status=%s current=%s previous_retained=%s rule_enabled=%s"
                % (
                    payload.get("status"),
                    payload.get("current"),
                    payload.get("previous_retained"),
                    payload.get("rule_enabled"),
                )
            )
            if payload.get("created_disabled"):
                log(
                    "note: the company had no IP rule, so one was created "
                    "DISABLED — an administrator must enable it deliberately"
                )
            if payload.get("rule_enabled") is False:
                log(
                    "note: the rule is disabled, so attendance is not "
                    "restricted by network at the moment"
                )
            return 0

        if status in (401, 403):
            # A wrong secret will be wrong again in five seconds. Stopping
            # here is what keeps a misconfigured machine from hammering the
            # endpoint every ten minutes forever.
            log("refused: credential rejected — check the shared secret")
            return 2

        if status == 404:
            log("refused: the endpoint is not enabled on this deployment")
            return 2

        if status == 400:
            log(
                "backend could not determine our address (%s) — is this "
                "machine reaching the site through Cloudflare?"
                % payload.get("code")
            )
            return 4

        if status is not None:
            log("unexpected response: HTTP %s" % status)

        if attempt < ATTEMPTS:
            delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            log("retrying in %ss (attempt %s of %s)" % (delay, attempt + 1, ATTEMPTS))
            time.sleep(delay)

    log("giving up until the next scheduled run; the whitelist was left as it was")
    return 3


if __name__ == "__main__":
    sys.exit(main())
