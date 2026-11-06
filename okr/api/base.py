"""Shared plumbing for the OKR API: the caller's OKR identity, the error
shape and pagination from ``appcheckin/docs/okr_api_contract.md``.

Every error leaves as ``{"code", "message", "fields"}`` with a Vietnamese
message. Authentication is the project's JWT; ``IsAuthenticated`` only gets
a request in the door, and every business check runs in ``okr.services``.
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import exceptions
from rest_framework.authentication import SessionAuthentication
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from joydigi_api.auth import SessionVersionJWTAuthentication
from okr.services.access import OKRViewer
from okr.services.errors import OKRError

TWO_PLACES = Decimal("0.01")

_GENERIC = {
    400: ("bad_request", "Dữ liệu gửi lên không hợp lệ."),
    401: ("not_authenticated", "Vui lòng đăng nhập."),
    403: ("forbidden", "Bạn không có quyền thực hiện thao tác này."),
    404: ("not_found", "Không tìm thấy dữ liệu."),
    405: ("method_not_allowed", "Thao tác không được hỗ trợ."),
    406: ("not_acceptable", "Định dạng phản hồi không được hỗ trợ."),
    413: ("too_large", "Dữ liệu gửi lên quá lớn."),
    415: ("unsupported_media_type", "Định dạng dữ liệu không được hỗ trợ."),
    429: ("throttled", "Bạn thao tác quá nhanh, vui lòng thử lại sau."),
}


class OKRPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class OKRAPIView(APIView):
    # The app sends its JWT; the web pages ride on the HR web login session.
    # JWT stays first so an anonymous call gets 401 (not 403), and DRF's
    # session authentication enforces CSRF on every unsafe method.
    authentication_classes = [SessionVersionJWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.viewer = OKRViewer.for_user(request.user)
        if not self.viewer.is_active:
            raise OKRError("inactive", "Tài khoản đã ngừng hoạt động nên không dùng được OKR.", 403)

    def handle_exception(self, exc):
        if isinstance(exc, OKRError):
            return Response(exc.as_dict(), status=exc.status)
        # DRF answers its own exceptions and Django's Http404 /
        # PermissionDenied (anything else is re-raised as a 500); rewrite
        # those bodies into the contract shape.
        response = super().handle_exception(exc)
        code, message = _GENERIC.get(response.status_code, ("error", "Đã có lỗi xảy ra."))
        if isinstance(exc, exceptions.PermissionDenied) and str(exc.detail).startswith("CSRF Failed"):
            code, message = "csrf_failed", "Phiên làm việc thiếu mã bảo vệ hoặc đã hết hạn. Vui lòng tải lại trang."
        fields = {}
        if isinstance(exc, exceptions.ValidationError) and isinstance(exc.detail, dict):
            # One message per field, as the services report them.
            fields = {
                key: str(value[0]) if isinstance(value, list) and value else str(value)
                for key, value in exc.detail.items()
            }
        response.data = {"code": code, "message": message, "fields": fields}
        return response

    # -- helpers --------------------------------------------------------------

    def paginate(self, items, render=None, *, render_page=None):
        """One DRF page of ``items``; ``render`` maps one item, ``render_page``
        a whole page (for batch lookups)."""
        paginator = OKRPagination()
        page = list(paginator.paginate_queryset(items, self.request, view=self))
        results = render_page(page) if render_page else [render(item) for item in page]
        return paginator.get_paginated_response(results)

    def int_param(self, name: str, *, required: bool = False):
        raw = self.request.query_params.get(name)
        if raw in (None, ""):
            if required:
                raise OKRError("validation_error", "Dữ liệu chưa hợp lệ.", 400, {name: "Thiếu tham số."})
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value < 1:
            raise OKRError("validation_error", "Dữ liệu chưa hợp lệ.", 400, {name: "Giá trị không hợp lệ."})
        return value

    def body_int(self, name: str):
        raw = self.request.data.get(name)
        if raw in (None, ""):
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value < 1 or isinstance(raw, bool):
            raise OKRError("validation_error", "Dữ liệu chưa hợp lệ.", 400, {name: "Giá trị không hợp lệ."})
        return value


def percent(value):
    """Percent or weight as the contract's two-decimal string."""
    return None if value is None else str(Decimal(value).quantize(TWO_PLACES))


def metric(value):
    """Metric value as a plain decimal string without exponent or trailing zeros."""
    if value is None:
        return None
    text = format(Decimal(value).normalize(), "f")
    return "0" if text in ("-0", "") else text


def person(employee):
    if employee is None:
        return None
    return {"id": employee.pk, "name": employee.get_full_name()}
