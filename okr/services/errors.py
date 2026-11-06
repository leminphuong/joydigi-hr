"""The one error type OKR services raise.

Codes are JOYOKR's ``WP_Error`` codes without the ``joyokr_`` prefix, so a
legacy client and this API name the same failure the same way. JOYOKR
answered business-rule violations with 422; the app contract
(``appcheckin/docs/okr_api_contract.md``) uses 400 for those, so they are
raised as 400 here.
"""

from __future__ import annotations

from typing import Optional


class OKRError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, fields: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "fields": self.fields}


def not_found(kind: str = "objective") -> OKRError:
    messages = {
        "objective": "Không tìm thấy Objective.",
        "key_result": "Không tìm thấy Key Result.",
        "cycle": "Không tìm thấy chu kỳ.",
        "comment": "Không tìm thấy bình luận.",
        "evidence": "Không tìm thấy bằng chứng.",
        "notification": "Không tìm thấy thông báo.",
    }
    return OKRError("not_found", messages.get(kind, "Không tìm thấy nội dung."), 404)


def forbidden(message: str = "Bạn không có quyền thay đổi OKR này.") -> OKRError:
    return OKRError("forbidden", message, 403)


def validation(fields: dict, message: str = "Dữ liệu chưa hợp lệ.") -> OKRError:
    return OKRError("validation_error", message, 400, fields)


def conflict(code: str, message: str) -> OKRError:
    return OKRError(code, message, 409)


def rule(code: str, message: str) -> OKRError:
    """A business rule JOYOKR reported as 422."""
    return OKRError(code, message, 400)
