"""Mã ký ngắn hạn dùng cho màn hình QR chấm công."""

import time

from django.core import signing
from django.utils.crypto import salted_hmac


KIOSK_TOKEN_SALT = "joydigi-checkin-kiosk"
KIOSK_CODE_SALT = "joydigi-checkin-kiosk-number"
KIOSK_TOKEN_LIFETIME = 70


def make_kiosk_token():
    return signing.dumps(
        {"time_slot": int(time.time() // 30)},
        salt=KIOSK_TOKEN_SALT,
        compress=True,
    )


def valid_kiosk_token(token):
    if not token:
        return False
    try:
        payload = signing.loads(
            token,
            salt=KIOSK_TOKEN_SALT,
            max_age=KIOSK_TOKEN_LIFETIME,
        )
        current_slot = int(time.time() // 30)
        return abs(current_slot - int(payload.get("time_slot", -100))) <= 2
    except (signing.BadSignature, TypeError, ValueError):
        return False


def kiosk_numeric_code(token):
    """Trả về mã 6 số gắn với đúng mã QR đang hiển thị."""
    if not valid_kiosk_token(token):
        return ""
    digest = salted_hmac(KIOSK_CODE_SALT, token).hexdigest()
    return f"{int(digest[:12], 16) % 1_000_000:06d}"
