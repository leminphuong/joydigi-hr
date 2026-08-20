"""Mã ký ngắn hạn dùng cho màn hình QR chấm công."""

import time

from django.core import signing


KIOSK_TOKEN_SALT = "joydigi-checkin-kiosk"
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
