import calendar
import logging
from datetime import date, datetime, timedelta, timezone

from django import template
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Case, CharField, F, Q, Value, When
from django.http import QueryDict
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceLateComeEarlyOut,
    AttendanceLateEarlyRequest,
    EmployeeShiftDay,
)
from attendance.methods.verification_proof import PROOF_TTL, issue_verification_proof
from attendance.views.clock_in_out import *
from attendance.views.clock_in_out import (
    perform_clock_in,
    perform_clock_out,
    validate_checkin_source,
)
from attendance.views.dashboard import (
    find_expected_attendances,
    find_late_come,
    find_on_time,
)
from attendance.views.views import *
from base.backends import ConfiguredEmailBackend
from base.methods import generate_pdf, is_company_leave, is_holiday, is_reportingmanager
from base.models import CheckInLocation, JoydigiMailTemplate, OfficeWifi
from employee.filters import EmployeeFilter
from employee.models import EmployeeFace
from employee.services.face_recognition import FaceRecognitionError, verify_face
from leave.models import LeaveRequest

logger = logging.getLogger(__name__)

from ...api_decorators.base.decorators import (
    manager_permission_required,
    permission_required,
)
from ...api_methods.base.methods import groupby_queryset, permission_based_queryset
from ...api_serializers.attendance.serializers import (
    AttendanceActivitySerializer,
    AttendanceLateComeEarlyOutSerializer,
    AttendanceLateEarlyRequestSerializer,
    AttendanceOverTimeSerializer,
    AttendanceRequestSerializer,
    AttendanceSerializer,
    MailTemplateSerializer,
    UserAttendanceDetailedSerializer,
    UserAttendanceListSerializer,
)

# Create your views here.


def query_dict(data):
    query_dict = QueryDict("", mutable=True)
    for key, value in data.items():
        if isinstance(value, list):
            for item in value:
                query_dict.appendlist(key, item)
        else:
            query_dict.update({key: value})
    return query_dict


def _attendance_evidence(request):
    """Pulls only the attendance-source evidence fields a mobile
    client may legitimately supply (Phase 6.1) — never `employee_id`,
    `company_id`, or any other identity/authority field; those always
    come from `request.user.employee_get`, never the body."""
    data = request.data if isinstance(request.data, dict) else {}
    fields = (
        "verification_proof",
        "qr_token",
        "numeric_code",
        "wifi_ssid",
        "wifi_bssid",
        "latitude",
        "longitude",
    )
    return {key: data[key] for key in fields if data.get(key) not in (None, "")}


class ClockInAPIView(APIView):
    """
    Allows authenticated employees to clock in, determining the correct shift and attendance date, including handling night shifts.

    Methods:
        post(request): Processes and records the clock-in time.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if request.user.employee_get.check_online():
            return Response({"message": "Already clocked-in"}, status=400)

        current_date = date.today()
        current_time = datetime.now().time()
        current_datetime = datetime.now()

        # Phase 6.1: no more legacy-geofencing fail-open block here —
        # that call always raised `AttributeError` against this
        # synthetic `Request` (it has no `.data`), which the old bare
        # `except: pass` silently swallowed as "check passed". Location/
        # Wi-Fi/QR/6-digit evidence now flows through `perform_clock_in`
        # -> `validate_checkin_source`, which fails closed.
        with transaction.atomic():
            attendance, allowed, reason = perform_clock_in(
                Request(
                    user=request.user,
                    date=current_date,
                    time=current_time,
                    datetime=current_datetime,
                    evidence=_attendance_evidence(request),
                )
            )

        if not allowed:
            return Response(
                {
                    "code": reason["code"] if reason else "VERIFICATION_REQUIRED",
                    "message": reason["message"] if reason else "Không thể chấm công vào.",
                },
                status=400,
            )
        return Response(
            {
                "message": "Clocked-In",
                "attendance_id": attendance.id if attendance else None,
                "clock_in": (
                    str(attendance.attendance_clock_in)
                    if attendance and attendance.attendance_clock_in
                    else None
                ),
            },
            status=200,
        )


class ClockOutAPIView(APIView):
    """
    Allows authenticated employees to clock out, updating the latest attendance record and handling early outs.

    Methods:
        post(request): Records the clock-out time.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.employee_get.check_online():
            return Response({"message": "Already clocked-out"}, status=400)

        current_date = date.today()
        current_time = datetime.now().time()
        current_datetime = datetime.now()

        # Phase 5.2: `perform_clock_out` is the pure-mutation half of the
        # web `clock_out()` helper (see its docstring) — it never renders
        # a template, so a genuinely unexpected exception here is a real
        # 500, not a false "already clocked-out" masking a successful
        # write. Wrapped in `transaction.atomic()` so a failure partway
        # through (mutation + early-out logic) can't leave a half-applied
        # checkout. Phase 6.1: no more legacy-geofencing fail-open block
        # (see `ClockInAPIView` docstring above — same bug, same fix).
        with transaction.atomic():
            attendance, allowed, reason = perform_clock_out(
                Request(
                    user=request.user,
                    date=current_date,
                    time=current_time,
                    datetime=current_datetime,
                    evidence=_attendance_evidence(request),
                )
            )

        if not allowed:
            return Response(
                {
                    "code": reason["code"] if reason else "VERIFICATION_REQUIRED",
                    "message": reason["message"] if reason else "Không thể chấm công ra.",
                },
                status=400,
            )
        return Response(
            {
                "message": "Clocked-Out",
                "attendance_id": attendance.id if attendance else None,
                "clock_out": (
                    str(attendance.attendance_clock_out)
                    if attendance and attendance.attendance_clock_out
                    else None
                ),
            },
            status=200,
        )


class AttendancePolicyView(APIView):
    """
    Phase 6.1 — read-only, JWT-authenticated view of which attendance
    methods the current employee's company has actually configured.
    Lets Flutter enable/disable method cards from backend truth
    instead of hardcoded assumptions. Exposes only booleans — never
    coordinates, QR signing material, or Wi-Fi identifiers themselves.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = request.user.employee_get
        company = employee.get_company()
        has_location = CheckInLocation.objects.filter(
            company_id=company, is_active=True
        ).exists()
        has_wifi = OfficeWifi.objects.filter(
            company_id=company, is_active=True
        ).exists()
        # Phase 6.3C.1: mobile enrollment doesn't exist yet (Step 7 —
        # web `/employee/face-id/` stays the only enrollment path), so
        # the tile can only ever be meaningful for an employee who
        # already has an `EmployeeFace` template. This intentionally
        # ignores company-level config (there is no company-level
        # Camera AI policy row anywhere in the codebase, unlike
        # location/wifi) — enrollment status is the only real gate.
        has_face = EmployeeFace.objects.filter(employee=employee).exists()
        return Response(
            {
                "location": {"enabled": has_location},
                "wifi": {"enabled": has_wifi},
                # QR and the 6-digit fallback are both minted per
                # `CheckInLocation` (see `base.checkin_tokens`), so
                # they're only meaningful once at least one is set up.
                "qr": {"enabled": has_location},
                "numeric_code": {"enabled": has_location},
                "camera_ai": {"enabled": has_face},
            },
            status=200,
        )


# Phase 6.3C.1: canonical Camera AI verification-proof method identifier —
# reused nowhere else, must never collide with "location"/"wifi"/"qr"/
# "numeric_code" (AttendanceVerifySourceView._METHODS).
CAMERA_AI_METHOD = "camera_ai"

# Maps employee.services.face_recognition.FaceRecognitionError.code to
# this endpoint's own stable mobile error contract (code, message,
# status). Any code not listed here (e.g. the engine's own internal
# "invalid_embedding"/"invalid_registered_embedding"/"invalid_face_result"
# invariant failures) falls back to FACE_ENGINE_UNAVAILABLE — those are
# never the caller's fault, so they're never worth a bespoke code.
_FACE_ERROR_RESPONSES = {
    "empty_image": ("FACE_IMAGE_REQUIRED", "Vui lòng chụp ảnh khuôn mặt.", 400),
    "invalid_image": ("FACE_IMAGE_INVALID", "Ảnh khuôn mặt không hợp lệ.", 400),
    "image_too_large": (
        "FACE_IMAGE_INVALID",
        "Ảnh khuôn mặt vượt quá dung lượng cho phép.",
        413,
    ),
    "unsupported_image_type": (
        "FACE_IMAGE_INVALID",
        "Định dạng ảnh không được hỗ trợ.",
        415,
    ),
    "face_not_detected": ("FACE_NOT_DETECTED", "Không phát hiện khuôn mặt.", 422),
    "multiple_faces": (
        "MULTIPLE_FACES_DETECTED",
        "Chỉ được có một khuôn mặt trong camera.",
        422,
    ),
    "face_engine_unavailable": (
        "FACE_ENGINE_UNAVAILABLE",
        "Không thể khởi tạo nhận diện khuôn mặt. Vui lòng thử lại.",
        503,
    ),
}
_DEFAULT_FACE_ERROR = (
    "FACE_ENGINE_UNAVAILABLE",
    "Không thể xác thực khuôn mặt. Vui lòng thử lại.",
    503,
)


class AttendanceVerifyFaceView(APIView):
    """
    Phase 6.3C.1 — dedicated mobile JWT endpoint for Camera AI (face)
    attendance verification (`POST /api/attendance/verify-face/`).

    Deliberately NOT folded into `AttendanceVerifySourceView` /
    `validate_checkin_source` (which only ever handles
    location/wifi/qr/numeric_code) — a shared dispatch point means any
    future Camera AI change risks regressing those already-working
    flows. This view has zero effect on that function or its
    `_METHODS` set.

    Reuses the exact same `EmployeeFace` biometric template and
    InsightFace engine (`employee.services.face_recognition`) as the
    existing web `/attendance/face/verify/` flow — no second AI
    implementation. Unlike that web endpoint, this one never calls
    `perform_clock_in`/`perform_clock_out` itself: on a real match it
    only issues the same short-lived, single-use, employee-bound
    `verification_proof` every other method already uses
    (`attendance.methods.verification_proof` — completely unmodified),
    so the client still finishes the write through the existing
    `/attendance/clock-in/` or `/attendance/clock-out/`, exactly like
    Location/Wifi/QR/numeric_code. The uploaded image is never
    persisted or logged — only read into memory, passed to the engine,
    and discarded when the request ends.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        employee = request.user.employee_get

        # Employee identity is server-derived only — never accept a
        # client-supplied employee_id to decide whose face template to
        # compare against.
        face_profile = EmployeeFace.objects.filter(employee=employee).first()
        if face_profile is None:
            return Response(
                {
                    "code": "FACE_NOT_ENROLLED",
                    "message": "Bạn chưa đăng ký Face ID.",
                },
                status=409,
            )

        image = request.FILES.get("image")
        if image is None:
            return Response(
                {
                    "code": "FACE_IMAGE_REQUIRED",
                    "message": "Vui lòng chụp ảnh khuôn mặt.",
                },
                status=400,
            )

        try:
            image_bytes = image.read()
        except Exception:
            return Response(
                {
                    "code": "FACE_IMAGE_INVALID",
                    "message": "Không đọc được ảnh khuôn mặt.",
                },
                status=400,
            )

        try:
            result = verify_face(
                face_profile.embedding,
                image_bytes,
                filename=image.name or "face.jpg",
                content_type=image.content_type or "image/jpeg",
            )
        except FaceRecognitionError as exc:
            code, message, status_code = _FACE_ERROR_RESPONSES.get(
                exc.code, _DEFAULT_FACE_ERROR
            )
            logger.warning(
                "FACE_VERIFY_MOBILE employee=%s result=FAILURE error=%s",
                getattr(employee, "id", None),
                exc.code,
            )
            return Response({"code": code, "message": message}, status=status_code)
        except Exception:
            logger.exception(
                "FACE_VERIFY_MOBILE employee=%s result=FAILURE error=unexpected",
                getattr(employee, "id", None),
            )
            code, message, status_code = _DEFAULT_FACE_ERROR
            return Response({"code": code, "message": message}, status=status_code)

        score = result.get("score")
        safe_score = round(score, 4) if isinstance(score, float) else None

        if not result.get("verified"):
            logger.info(
                "FACE_VERIFY_MOBILE employee=%s result=NOT_MATCHED score=%s",
                getattr(employee, "id", None),
                safe_score,
            )
            return Response(
                {"code": "FACE_NOT_MATCHED", "message": "Khuôn mặt không khớp."},
                status=403,
            )

        logger.info(
            "FACE_VERIFY_MOBILE employee=%s result=MATCHED score=%s",
            getattr(employee, "id", None),
            safe_score,
        )
        proof = issue_verification_proof(employee.id, CAMERA_AI_METHOD)
        return Response(
            {
                "verified": True,
                "method": CAMERA_AI_METHOD,
                "proof": proof,
                "expires_in": PROOF_TTL,
            },
            status=200,
        )


class AttendanceVerifySourceView(APIView):
    """
    Phase 6.1 — JWT-authenticated, validation-only endpoint: checks
    one piece of client-supplied attendance-source evidence
    (location/wifi/qr/numeric_code) against the employee's real
    company policy and, on success, issues a short-lived signed proof
    (see `attendance.methods.verification_proof`) instead of ever
    trusting a client-asserted "verified: true" at check-in/out time.
    This call never writes an Attendance row itself.
    """

    permission_classes = [IsAuthenticated]

    _METHODS = {"location", "wifi", "qr", "numeric_code"}

    def post(self, request):
        method = request.data.get("method") if isinstance(request.data, dict) else None
        if method not in self._METHODS:
            return Response(
                {
                    "code": "METHOD_NOT_ENABLED",
                    "message": "Phương thức chấm công không hợp lệ.",
                },
                status=400,
            )

        employee = request.user.employee_get
        company = employee.get_company()
        evidence_request = Request(
            user=request.user,
            date=date.today(),
            time=datetime.now().time(),
            datetime=None,
            evidence=_attendance_evidence(request),
        )
        result = validate_checkin_source(evidence_request, company)
        if not result["allowed"]:
            return Response(
                {
                    "code": result.get("code") or "VERIFICATION_REQUIRED",
                    "message": result["message"],
                },
                status=400,
            )

        proof = issue_verification_proof(employee.id, method)
        return Response(
            {
                "verified": True,
                "method": result.get("method"),
                "proof": proof,
                "expires_in": PROOF_TTL,
            },
            status=200,
        )


class AttendanceView(APIView):
    """
    Handles CRUD operations for attendance records.

    Methods:
        get_queryset(request, type): Returns filtered attendance records.
        get(request, pk=None, type=None): Retrieves a specific record or a list of records.
        post(request): Creates a new attendance record.
        put(request, pk): Updates an existing attendance record.
        delete(request, pk): Deletes an attendance record and adjusts related overtime if needed.
    """

    permission_classes = [IsAuthenticated]
    filterset_class = AttendanceFilters
    queryset = Attendance.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None, type=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return Attendance.objects.none()
        if type == "ot":

            condition = AttendanceValidationCondition.objects.first()
            minot = strtime_seconds("00:30")
            if condition is not None:
                minot = strtime_seconds(condition.minimum_overtime_to_approve)
                queryset = Attendance.objects.filter(
                    overtime_second__gte=minot,
                    attendance_validated=True,
                )

        elif type == "validated":
            queryset = Attendance.objects.filter(attendance_validated=True)
        elif type == "non-validated":
            queryset = Attendance.objects.filter(attendance_validated=False)
        else:
            queryset = Attendance.objects.all()
        user = request.user
        # checking user level permissions
        perm = "attendance.view_attendance"
        queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        return queryset

    def get(self, request, pk=None, type=None):
        # individual object workflow
        if pk:
            attendance = get_object_or_404(Attendance, pk=pk)
            scoped = permission_based_queryset(
                request.user,
                "attendance.view_attendance",
                Attendance.objects.filter(pk=pk),
                user_obj=True,
            )
            if not scoped.exists():
                return Response(
                    {
                        "error": _(
                            "You do not have permission to view this attendance record."
                        )
                    },
                    status=403,
                )
            serializer = AttendanceSerializer(instance=attendance)
            return Response(serializer.data, status=200)
        # permission based querysete
        attendances = self.get_queryset(request, type)
        # filtering queryset
        attendances_filter_queryset = self.filterset_class(
            request.GET, queryset=attendances
        ).qs
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(
                request, url, field_name, attendances_filter_queryset
            )
        # pagination workflow
        paginater = PageNumberPagination()
        page = paginater.paginate_queryset(attendances_filter_queryset, request)
        serializer = AttendanceSerializer(page, many=True)
        return paginater.get_paginated_response(serializer.data)

    @manager_permission_required("attendance.add_attendance")
    def post(self, request):
        serializer = AttendanceSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        employee_id = request.data.get("employee_id")
        attendance_date = request.data.get("attendance_date", date.today())
        if Attendance.objects.filter(
            employee_id=employee_id, attendance_date=attendance_date
        ).exists():
            return Response(
                {
                    "error": [
                        _(
                            "Attendance for this employee on the current date already exists."
                        )
                    ]
                },
                status=400,
            )
        return Response(serializer.errors, status=400)

    @method_decorator(permission_required("attendance.change_attendance"))
    def put(self, request, pk):
        try:
            attendance = Attendance.objects.get(id=pk)
        except Attendance.DoesNotExist:
            return Response({"detail": _("Attendance record not found.")}, status=404)

        serializer = AttendanceSerializer(instance=attendance, data=request.data)

        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)

        # Customize error message for unique constraint
        serializer_errors = serializer.errors
        if "non_field_errors" in serializer.errors:
            unique_error_msg = (
                "The fields employee_id, attendance_date must make a unique set."
            )
            if unique_error_msg in serializer.errors["non_field_errors"]:
                serializer_errors = {
                    "non_field_errors": [
                        "The employee already has attendance on this date."
                    ]
                }
        return Response(serializer_errors, status=400)

    @method_decorator(permission_required("attendance.delete_attendance"))
    def delete(self, request, pk):
        attendance = Attendance.objects.get(id=pk)
        month = attendance.attendance_date
        month = month.strftime("%B").lower()
        overtime = attendance.employee_id.employee_overtime.filter(month=month).last()
        if overtime is not None:
            if attendance.attendance_overtime_approve:
                # Subtract overtime of this attendance
                total_overtime = strtime_seconds(overtime.overtime)
                attendance_overtime_seconds = strtime_seconds(
                    attendance.attendance_overtime
                )
                if total_overtime > attendance_overtime_seconds:
                    total_overtime = total_overtime - attendance_overtime_seconds
                else:
                    total_overtime = attendance_overtime_seconds - total_overtime
                overtime.overtime = format_time(total_overtime)
                overtime.save()
            try:
                attendance.delete()
                return Response({"status", "deleted"}, status=200)
            except Exception as error:
                return Response({"error:", f"{error}"}, status=400)
        else:
            try:
                attendance.delete()
                return Response({"status", "deleted"}, status=200)
            except Exception as error:
                return Response({"error:", f"{error}"}, status=400)


class ValidateAttendanceView(APIView):
    """
    Validates an attendance record and sends a notification to the employee.

    Method:
        put(request, pk): Marks the attendance as validated and notifies the employee.
    """

    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        attendance = Attendance.objects.filter(id=pk).update(attendance_validated=True)
        attendance = Attendance.objects.filter(id=pk).first()
        try:
            notify.send(
                request.user.employee_get,
                recipient=attendance.employee_id.employee_user_id,
                verb=f"Your attendance for the date {attendance.attendance_date} is validated",
                verb_ar=f"تم تحقيق حضورك في تاريخ {attendance.attendance_date}",
                verb_de=f"Deine Anwesenheit für das Datum {attendance.attendance_date} ist bestätigt.",
                verb_es=f"Se valida tu asistencia para la fecha {attendance.attendance_date}.",
                verb_fr=f"Votre présence pour la date {attendance.attendance_date} est validée.",
                redirect="/attendance/view-my-attendance",
                icon="checkmark",
                api_redirect=f"/api/attendance/attendance?employee_id{attendance.employee_id}",
            )
        except:
            pass
        return Response(status=200)


class OvertimeApproveView(APIView):
    """
    Approves overtime for an attendance record and sends a notification to the employee.

    Method:
        put(request, pk): Marks the overtime as approved and notifies the employee.
    """

    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        try:
            attendance = Attendance.objects.filter(id=pk).update(
                attendance_overtime_approve=True
            )
        except Exception as E:
            return Response({"error": str(E)}, status=400)

        attendance = Attendance.objects.filter(id=pk).first()
        try:
            notify.send(
                request.user.employee_get,
                recipient=attendance.employee_id.employee_user_id,
                verb=f"Your {attendance.attendance_date}'s attendance overtime approved.",
                verb_ar=f"تمت الموافقة على إضافة ساعات العمل الإضافية لتاريخ {attendance.attendance_date}.",
                verb_de=f"Die Überstunden für den {attendance.attendance_date} wurden genehmigt.",
                verb_es=f"Se ha aprobado el tiempo extra de asistencia para el {attendance.attendance_date}.",
                verb_fr=f"Les heures supplémentaires pour la date {attendance.attendance_date} ont été approuvées.",
                redirect="/attendance/attendance-overtime-view",
                icon="checkmark",
                api_redirect="/api/attendance/attendance-hour-account/",
            )
        except:
            pass
        return Response(status=200)


class AttendanceRequestView(APIView):
    """
    Handles requests for creating, updating, and viewing attendance records.

    Methods:
        get(request, pk=None): Retrieves a specific attendance request by `pk` or a filtered list of requests.
        post(request): Creates a new attendance request.
        put(request, pk): Updates an existing attendance request.
    """

    serializer_class = AttendanceRequestSerializer
    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        if pk:
            attendance = get_object_or_404(Attendance, id=pk)
            scoped = filtersubordinates(
                request, Attendance.objects.filter(id=pk), "attendance.view_attendance"
            )
            if not scoped.exists():
                return Response(
                    {
                        "error": _(
                            "You do not have permission to view this attendance request."
                        )
                    },
                    status=403,
                )
            serializer = AttendanceRequestSerializer(instance=attendance)
            return Response(serializer.data, status=200)

        requests = Attendance.objects.filter(
            is_validate_request=True,
        )
        requests = filtersubordinates(
            request=request,
            perm="attendance.view_attendance",
            queryset=requests,
        )
        requests = requests | Attendance.objects.filter(
            employee_id__employee_user_id=request.user,
            is_validate_request=True,
        )
        request_filtered_queryset = AttendanceFilters(request.GET, requests).qs
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            # groupby workflow
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, request_filtered_queryset)

        pagenation = PageNumberPagination()
        page = pagenation.paginate_queryset(request_filtered_queryset, request)
        serializer = self.serializer_class(page, many=True)
        return pagenation.get_paginated_response(serializer.data)

    def post(self, request):
        from attendance.forms import NewRequestForm

        form = NewRequestForm(data=request.data)
        if form.is_valid():
            work_type = form.cleaned_data.get("work_type_id")

            if not WorkType.objects.filter(pk=getattr(work_type, "pk", None)).exists():
                form.cleaned_data["work_type_id"] = None

            if form.new_instance is not None:
                form.new_instance.save()

            return Response(form.data, status=200)
        employee_id = request.data.get("employee_id")
        attendance_date = request.data.get("attendance_date", date.today())
        if Attendance.objects.filter(
            employee_id=employee_id, attendance_date=attendance_date
        ).exists():
            return Response(
                {error: list(message) for error, message in form.errors.items()},
                status=400,
            )
        return Response(form.errors, status=404)

    def put(self, request, pk):
        from attendance.forms import AttendanceRequestForm

        attendance = Attendance.objects.get(id=pk)
        form = AttendanceRequestForm(data=request.data, instance=attendance)
        if form.is_valid():
            attendance = Attendance.objects.get(id=form.instance.pk)
            instance = form.save()
            instance.employee_id = attendance.employee_id
            instance.id = attendance.id
            work_type = form.cleaned_data.get("work_type_id")

            if not WorkType.objects.filter(pk=getattr(work_type, "pk", None)).exists():
                form.cleaned_data["work_type_id"] = None
            if attendance.request_type != "create_request":
                attendance.requested_data = json.dumps(instance.serialize())
                attendance.request_description = instance.request_description
                # set the user level validation here
                attendance.is_validate_request = True
                attendance.save()
            else:
                instance.is_validate_request_approved = False
                instance.is_validate_request = True
                instance.save()
            return Response(form.data, status=200)
        return Response(form.errors, status=404)


class AttendanceRequestApproveView(APIView):
    """
    Approves and updates an attendance request.

    Method:
        put(request, pk): Approves the attendance request, updates attendance records, and handles related activities.
    """

    permission_classes = [IsAuthenticated]

    @manager_permission_required("attendance.change_attendance")
    def put(self, request, pk):
        try:
            attendance = Attendance.objects.get(id=pk)
            prev_attendance_date = attendance.attendance_date
            prev_attendance_clock_in_date = attendance.attendance_clock_in_date
            prev_attendance_clock_in = attendance.attendance_clock_in
            attendance.attendance_validated = True
            attendance.is_validate_request_approved = True
            attendance.is_validate_request = False
            attendance.request_description = None
            attendance.save()
            if attendance.requested_data is not None:
                requested_data = json.loads(attendance.requested_data)
                requested_data["attendance_clock_out"] = (
                    None
                    if requested_data["attendance_clock_out"] == "None"
                    else requested_data["attendance_clock_out"]
                )
                requested_data["attendance_clock_out_date"] = (
                    None
                    if requested_data["attendance_clock_out_date"] == "None"
                    else requested_data["attendance_clock_out_date"]
                )
                Attendance.objects.filter(id=pk).update(**requested_data)
                # DUE TO AFFECT THE OVERTIME CALCULATION ON SAVE METHOD, SAVE THE INSTANCE ONCE MORE
                attendance = Attendance.objects.get(id=pk)
                attendance.save()
            if (
                attendance.attendance_clock_out is None
                or attendance.attendance_clock_out_date is None
            ):
                attendance.attendance_validated = True
                activity = AttendanceActivity.objects.filter(
                    employee_id=attendance.employee_id,
                    attendance_date=prev_attendance_date,
                    clock_in_date=prev_attendance_clock_in_date,
                    clock_in=prev_attendance_clock_in,
                )
                if activity:
                    activity.update(
                        employee_id=attendance.employee_id,
                        attendance_date=attendance.attendance_date,
                        clock_in_date=attendance.attendance_clock_in_date,
                        clock_in=attendance.attendance_clock_in,
                    )

                else:
                    AttendanceActivity.objects.create(
                        employee_id=attendance.employee_id,
                        attendance_date=attendance.attendance_date,
                        clock_in_date=attendance.attendance_clock_in_date,
                        clock_in=attendance.attendance_clock_in,
                    )
        except Exception as E:
            return Response({"error": str(E)}, status=400)
        return Response({"status": "approved"}, status=200)


class AttendanceRequestCancelView(APIView):
    """
    Cancels an attendance request.

    Method:
        put(request, pk): Cancels the attendance request, resetting its status and data, and deletes the request if it was a create request.
    """

    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        try:
            attendance = Attendance.objects.get(id=pk)
            if (
                attendance.employee_id.employee_user_id == request.user
                or is_reportingmanager(request)
                or request.user.has_perm("attendance.change_attendance")
            ):
                attendance.is_validate_request_approved = False
                attendance.is_validate_request = False
                attendance.request_description = None
                attendance.requested_data = None
                attendance.request_type = None

                attendance.save()
                if attendance.request_type == "create_request":
                    attendance.delete()
        except Exception as E:
            return Response({"error": str(E)}, status=400)
        return Response({"status": "success"}, status=200)


class AttendanceOverTimeView(APIView):
    """
    Manages CRUD operations for attendance overtime records.

    Methods:
        get(request, pk=None): Retrieves a specific overtime record by `pk` or a list of records with filtering and pagination.
        post(request): Creates a new overtime record.
        put(request, pk): Updates an existing overtime record.
        delete(request, pk): Deletes an overtime record.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        if pk:
            attendance_ot = get_object_or_404(AttendanceOverTime, pk=pk)
            scoped = filtersubordinates(
                request,
                AttendanceOverTime.objects.filter(pk=pk),
                "attendance.view_attendanceovertime",
            )
            if not scoped.exists():
                return Response(
                    {
                        "error": _(
                            "You do not have permission to view this overtime record."
                        )
                    },
                    status=403,
                )
            serializer = AttendanceOverTimeSerializer(attendance_ot)
            return Response(serializer.data, status=200)

        filterset_class = AttendanceOverTimeFilter(request.GET)
        queryset = filterset_class.qs
        self_account = queryset.filter(employee_id__employee_user_id=request.user)
        permission_based_queryset = filtersubordinates(
            request, queryset, "attendance.view_attendanceovertime"
        )
        queryset = permission_based_queryset | self_account
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            # groupby workflow
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, queryset)

        pagenation = PageNumberPagination()
        page = pagenation.paginate_queryset(queryset, request)
        serializer = AttendanceOverTimeSerializer(page, many=True)
        return pagenation.get_paginated_response(serializer.data)

    @manager_permission_required("attendance.add_attendanceovertime")
    def post(self, request):
        serializer = AttendanceOverTimeSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @manager_permission_required("attendance.change_attendanceovertime")
    def put(self, request, pk):
        attendance_ot = get_object_or_404(AttendanceOverTime, pk=pk)
        serializer = AttendanceOverTimeSerializer(
            instance=attendance_ot, data=request.data
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @method_decorator(permission_required("attendance.delete_attendanceovertime"))
    def delete(self, request, pk):
        attendance = get_object_or_404(AttendanceOverTime, pk=pk)
        attendance.delete()

        return Response({"message": _("Overtime deleted successfully")}, status=204)


class LateComeEarlyOutView(APIView):
    """
    Handles retrieval and deletion of late come and early out records.

    Methods:
        get(request, pk=None): Retrieves a list of late come and early out records with filtering.
        delete(request, pk=None): Deletes a specific late come or early out record by `pk`.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        data = LateComeEarlyOutFilter(request.GET)
        serializer = AttendanceLateComeEarlyOutSerializer(data.qs, many=True)
        return Response(serializer.data, status=200)

    def delete(self, request, pk=None):
        attendance = get_object_or_404(AttendanceLateComeEarlyOut, pk=pk)
        attendance.delete()
        return Response({"message": _("Attendance deleted successfully")}, status=204)


class AttendanceActivityView(APIView):
    """
    Retrieves attendance activity records.

    Method:
        get(request, pk=None): Retrieves a list of all attendance activity records.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        data = AttendanceActivity.objects.all()
        serializer = AttendanceActivitySerializer(data, many=True)
        return Response(serializer.data, status=200)


class TodayAttendance(APIView):
    """
    Provides the ratio of marked attendances to expected attendances for the current day.

    Method:
        get(request): Calculates and returns the attendance ratio for today.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):

        today = datetime.today()
        week_day = today.strftime("%A").lower()

        on_time = find_on_time(request, today=today, week_day=week_day)
        late_come = find_late_come(start_date=today)
        late_come_obj = len(late_come)

        marked_attendances = late_come_obj + on_time

        expected_attendances = find_expected_attendances(week_day=week_day)
        marked_attendances_ratio = 0
        if expected_attendances != 0:
            marked_attendances_ratio = (
                f"{(marked_attendances / expected_attendances) * 100:.2f}"
            )

        return Response(
            {"marked_attendances_ratio": marked_attendances_ratio}, status=200
        )


class OfflineEmployeesCountView(APIView):
    """
    Retrieves the count of active employees who have not clocked in today.

    Method:
        get(request): Returns the number of active employees who are not yet clocked in.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        is_manager = (
            EmployeeWorkInformation.objects.filter(
                reporting_manager_id=request.user.employee_get
            )
            .only("id")
            .exists()
        )

        if request.user.has_perm("employee.view_employee") or is_manager:
            count = (
                EmployeeFilter({"not_in_yet": date.today()})
                .qs.exclude(employee_work_info__isnull=True)
                .filter(is_active=True)
                .count()
            )
            return Response({"count": count}, status=200)
        return Response(
            {"error": _("Permission denied")}, status=status.HTTP_403_FORBIDDEN
        )


class OfflineEmployeesListView(APIView):
    """
    Lists active employees who have not clocked in today, including their leave status.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        employee = getattr(user, "employee_get", None)
        today = date.today()

        # Manager access: get employees reporting to current user
        managed_employee_ids = EmployeeWorkInformation.objects.filter(
            reporting_manager_id=employee
        ).values_list("employee_id", flat=True)

        # Superusers or users with view permission see all employees
        if user.has_perm("employee.view_employee"):
            base_queryset = Employee.objects.all()
        elif managed_employee_ids.exists():
            base_queryset = Employee.objects.filter(id__in=managed_employee_ids)
        else:
            return Response(
                {"error": _("Permission denied")}, status=status.HTTP_403_FORBIDDEN
            )

        # Apply filtering for offline employees
        filtered_qs = (
            EmployeeFilter({"not_in_yet": today}, queryset=base_queryset)
            .qs.exclude(employee_work_info__isnull=True)
            .filter(is_active=True)
            .select_related("employee_work_info")  # optimize joins
        )

        # Get leave status for the filtered employees
        leave_status = self.get_leave_status(filtered_qs)

        pagenation = PageNumberPagination()
        page = pagenation.paginate_queryset(leave_status, request)
        return pagenation.get_paginated_response(page)

    def get_leave_status(self, queryset):

        today = date.today()
        queryset = queryset.distinct()
        # Annotate each employee with their leave status
        employees_with_leave_status = queryset.annotate(
            leave_status=Case(
                # Define different cases based on leave requests and attendance
                When(
                    leaverequest__start_date__lte=today,
                    leaverequest__end_date__gte=today,
                    leaverequest__status="approved",
                    then=Value("On Leave"),
                ),
                When(
                    leaverequest__start_date__lte=today,
                    leaverequest__end_date__gte=today,
                    leaverequest__status="requested",
                    then=Value("Waiting Approval"),
                ),
                When(
                    leaverequest__start_date__lte=today,
                    leaverequest__end_date__gte=today,
                    then=Value("Canceled / Rejected"),
                ),
                When(
                    employee_attendances__attendance_date=today, then=Value("Working")
                ),
                default=Value("Expected working"),  # Default status
                output_field=CharField(),
            ),
            job_position_id=F("employee_work_info__job_position_id"),
        ).values(
            "employee_first_name",
            "employee_last_name",
            "leave_status",
            "employee_profile",
            "id",
            "job_position_id",
        )

        for employee in employees_with_leave_status:

            if employee["employee_profile"]:
                employee["employee_profile"] = (
                    settings.MEDIA_URL + employee["employee_profile"]
                )
        return employees_with_leave_status


class CheckingStatus(APIView):
    """
    Checks and provides the current attendance status for the authenticated user.

    Method:
        get(request): Returns the attendance status, duration at work, and clock-in time if available.
    """

    permission_classes = [IsAuthenticated]

    @classmethod
    def _format_seconds(cls, seconds):
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60
        return f"{hours:02}:{minutes:02}:{seconds:02}"

    def get(self, request):
        attendance_activity = (
            AttendanceActivity.objects.filter(employee_id=request.user.employee_get)
            .order_by("-id")
            .first()
        )
        duration = None
        work_seconds = request.user.employee_get.get_forecasted_at_work()[
            "forecasted_at_work_seconds"
        ]
        duration = CheckingStatus._format_seconds(int(work_seconds))
        status = False
        clock_in_time = None

        today = datetime.now()
        attendance_activity_first = (
            AttendanceActivity.objects.filter(
                employee_id=request.user.employee_get, clock_in_date=today
            )
            .order_by("in_datetime")
            .first()
        )
        if attendance_activity:
            try:
                clock_in_time = attendance_activity_first.clock_in.strftime("%I:%M %p")
                if attendance_activity.clock_out_date:
                    status = False
                else:
                    status = True
                    return Response(
                        {
                            "status": status,
                            "duration": duration,
                            "clock_in": clock_in_time,
                        },
                        status=200,
                    )
            except:
                return Response(
                    {"status": status, "duration": duration, "clock_in": clock_in_time},
                    status=200,
                )
        return Response(
            {"status": status, "duration": duration, "clock_in_time": clock_in_time},
            status=200,
        )


class MailTemplateView(APIView):
    """
    Retrieves a list of recruitment mail templates.

    Method:
        get(request): Returns all recruitment mail templates.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        instances = JoydigiMailTemplate.objects.all()
        serializer = MailTemplateSerializer(instances, many=True)
        return Response(serializer.data, status=200)


class ConvertedMailTemplateConvert(APIView):
    """
    Renders a recruitment mail template with data from a specified employee.

    Method:
        put(request): Renders the mail template body with employee and user data and returns the result.
    """

    permission_classes = [IsAuthenticated]

    def put(self, request):
        template_id = request.data.get("template_id", None)
        employee_id = request.data.get("employee_id", None)
        employee = Employee.objects.filter(id=employee_id).first()
        bdy = JoydigiMailTemplate.objects.filter(id=template_id).first()
        template_bdy = template.Template(bdy.body)
        context = template.Context(
            {"instance": employee, "self": request.user.employee_get}
        )
        render_bdy = template_bdy.render(context)
        return Response(render_bdy)


class OfflineEmployeeMailsend(APIView):
    """
    Sends an email with attachments and rendered templates to a specified employee.

    Method:
        post(request): Renders email templates with employee and user data, attaches files, and sends the email.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        employee_id = request.POST.get("employee_id")
        subject = request.POST.get("subject", "")
        bdy = request.POST.get("body", "")
        other_attachments = request.FILES.getlist("other_attachments")
        attachments = [
            (file.name, file.read(), file.content_type) for file in other_attachments
        ]
        email_backend = ConfiguredEmailBackend()
        host = email_backend.dynamic_username
        employee = Employee.objects.get(id=employee_id)
        template_attachment_ids = request.POST.getlist("template_attachments")
        bodys = list(
            JoydigiMailTemplate.objects.filter(
                id__in=template_attachment_ids
            ).values_list("body", flat=True)
        )
        for html in bodys:
            # due to not having solid template we first need to pass the context
            template_bdy = template.Template(html)
            context = template.Context(
                {"instance": employee, "self": request.user.employee_get}
            )
            render_bdy = template_bdy.render(context)
            attachments.append(
                (
                    "Document",
                    generate_pdf(render_bdy, {}, path=False, title="Document").content,
                    "application/pdf",
                )
            )

        template_bdy = template.Template(bdy)
        context = template.Context(
            {"instance": employee, "self": request.user.employee_get}
        )
        render_bdy = template_bdy.render(context)

        email = EmailMessage(
            subject,
            render_bdy,
            host,
            [employee.employee_work_info.email],
        )
        email.content_subtype = "html"

        email.attachments = attachments
        try:
            email.send()
            if employee.employee_work_info.email:
                return Response(f"Mail sent to {employee.get_full_name()}")
            else:
                return Response(f"Email not set for {employee.get_full_name()}")
        except Exception as e:
            return Response("Something went wrong")


class UserAttendanceView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserAttendanceDetailedSerializer

    def get(self, request):
        employee_id = request.user.employee_get.id

        attendance_queryset = Attendance.objects.filter(
            employee_id=employee_id
        ).order_by("-attendance_date")

        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(attendance_queryset, request)

        serializer = self.serializer_class(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class AttendanceTypeAccessCheck(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        employee_id = user.employee_get.id

        if user.has_perm("attendance.view_attendance"):
            return Response(status=200)

        is_manager = (
            EmployeeWorkInformation.objects.filter(reporting_manager_id=employee_id)
            .only("id")
            .exists()
        )

        if is_manager:
            return Response(status=200)

        return Response(
            {"error": _("Permission denied")}, status=status.HTTP_403_FORBIDDEN
        )


class UserAttendanceDetailedView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        attendance = get_object_or_404(Attendance, pk=id)
        if attendance.employee_id == request.user.employee_get:
            serializer = UserAttendanceDetailedSerializer(attendance)
            return Response(serializer.data, status=200)
        return Response(
            {"error": _("Permission denied")}, status=status.HTTP_403_FORBIDDEN
        )


class TimesheetMonthView(APIView):
    """
    Read-only monthly timesheet aggregation for the authenticated
    employee (Phase 3A). One request instead of the client stitching
    together `my-attendance/`, an employee-scoped late/early query, and
    approved leave itself.

    Employee is always derived from `request.user.employee_get` — never
    from a client-supplied id. This is deliberate: the Phase 3A backend
    audit found `LateComeEarlyOutView` (`late-come-early-out-view/`)
    accepts an arbitrary `employee_id` query param with no ownership
    check, letting any authenticated user read another employee's
    late/early records. This view does not reuse that endpoint or its
    filter — it queries `AttendanceLateComeEarlyOut` directly, scoped
    to `employee_id=employee` set server-side.

    Holiday/company-leave-day detection reuses the same real backend
    logic the `Attendance` model itself uses (`base.methods.is_holiday`
    and `base.methods.is_company_leave`) rather than guessing at
    weekday/weekend rules.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = request.user.employee_get

        try:
            year = int(request.GET["year"])
            month = int(request.GET["month"])
        except (KeyError, TypeError, ValueError):
            return Response(
                {"error": _("year and month are required integers.")}, status=400
            )
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            return Response({"error": _("Invalid year or month.")}, status=400)

        last_day = calendar.monthrange(year, month)[1]
        month_start = date(year, month, 1)
        month_end = date(year, month, last_day)

        attendance_by_date = {
            a.attendance_date: a
            for a in Attendance.objects.filter(
                employee_id=employee,
                attendance_date__gte=month_start,
                attendance_date__lte=month_end,
            )
        }

        late_early_qs = AttendanceLateComeEarlyOut.objects.filter(
            employee_id=employee,
            attendance_id__attendance_date__gte=month_start,
            attendance_id__attendance_date__lte=month_end,
        ).values_list("attendance_id__attendance_date", "type")
        late_dates = {d for d, t in late_early_qs if t == "late_come"}
        early_dates = {d for d, t in late_early_qs if t == "early_out"}

        approved_leaves = LeaveRequest.objects.filter(
            employee_id=employee, status="approved", start_date__lte=month_end
        ).filter(Q(end_date__gte=month_start) | Q(end_date__isnull=True))
        leave_dates = set()
        for leave_request in approved_leaves:
            span_start = max(leave_request.start_date, month_start)
            span_end = min(leave_request.end_date or leave_request.start_date, month_end)
            current = span_start
            while current <= span_end:
                leave_dates.add(current)
                current += timedelta(days=1)

        days = []
        present_days = 0
        worked_seconds_total = 0
        overtime_seconds_total = 0
        current = month_start
        while current <= month_end:
            attendance = attendance_by_date.get(current)
            holiday = is_holiday(current, employee)
            company_leave = is_company_leave(current)
            days.append(
                {
                    "date": current.isoformat(),
                    "checkIn": attendance.attendance_clock_in.isoformat()
                    if attendance and attendance.attendance_clock_in
                    else None,
                    "checkOut": attendance.attendance_clock_out.isoformat()
                    if attendance and attendance.attendance_clock_out
                    else None,
                    "workedHour": attendance.attendance_worked_hour
                    if attendance
                    else None,
                    "overtime": attendance.attendance_overtime if attendance else None,
                    "isHoliday": bool(holiday),
                    "isCompanyLeave": bool(company_leave),
                    "isLate": current in late_dates,
                    "isEarly": current in early_dates,
                    "isLeave": current in leave_dates,
                    "isValidated": attendance.attendance_validated
                    if attendance
                    else None,
                }
            )
            if attendance:
                present_days += 1
                worked_seconds_total += attendance.at_work_second or 0
                overtime_seconds_total += attendance.overtime_second or 0
            current += timedelta(days=1)

        summary = {
            "presentDays": present_days,
            "leaveDays": len(leave_dates),
            "lateCount": len(late_dates),
            "earlyCount": len(early_dates),
            "workedSeconds": worked_seconds_total,
            "overtimeSeconds": overtime_seconds_total,
        }

        return Response(
            {"year": year, "month": month, "summary": summary, "days": days},
            status=200,
        )


class LateEarlyRequestListCreateAPIView(APIView):
    """
    Phase UI-4C.1. GET lists only the authenticated employee's own
    late/early requests; POST creates one for that same employee.
    Employee identity always comes from ``request.user.employee_get`` —
    never from the request body (Step 5/6 of the phase spec).
    """

    permission_classes = [IsAuthenticated]
    serializer_class = AttendanceLateEarlyRequestSerializer

    def get(self, request):
        employee = request.user.employee_get
        queryset = AttendanceLateEarlyRequest.objects.entire().filter(
            employee_id=employee
        ).order_by("-id")
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request)
        serializer = self.serializer_class(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        employee = request.user.employee_get
        serializer = self.serializer_class(data=request.data)
        if serializer.is_valid():
            # employee_id is read_only on the serializer (Step 6: never
            # trust it from the client) — supplied here from the
            # authenticated session only.
            instance = serializer.save(employee_id=employee)
            return Response(
                self.serializer_class(instance).data, status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LateEarlyRequestDetailAPIView(APIView):
    """Phase UI-4C.1. Read-only detail — strictly the owner's own request."""

    permission_classes = [IsAuthenticated]
    serializer_class = AttendanceLateEarlyRequestSerializer

    def get(self, request, pk):
        employee = request.user.employee_get
        instance = AttendanceLateEarlyRequest.objects.entire().filter(
            pk=pk, employee_id=employee
        ).first()
        if instance is None:
            return Response(
                {"error": _("Late/early request not found.")},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(self.serializer_class(instance).data, status=200)


class LateEarlyRequestCancelAPIView(APIView):
    """
    Phase UI-4C.1. The employee's own cancel action — sets
    ``canceled=True`` (the project's established rejected/cancelled
    state, same convention as ShiftRequest). No Attendance/Timesheet/
    AttendanceLateComeEarlyOut side effect (Step 3: request creation and
    lifecycle changes never touch those).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        employee = request.user.employee_get
        instance = AttendanceLateEarlyRequest.objects.entire().filter(
            pk=pk, employee_id=employee
        ).first()
        if instance is None:
            return Response(
                {"error": _("Late/early request not found.")},
                status=status.HTTP_404_NOT_FOUND,
            )
        if instance.canceled:
            return Response({"status": "success"}, status=200)
        instance.canceled = True
        instance.approved = False
        instance.save()
        return Response({"status": "success"}, status=200)
