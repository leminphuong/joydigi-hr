"""Face-gated attendance endpoint for the currently authenticated employee."""

import logging
from datetime import date, timedelta

from django.contrib.messages import get_messages
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from attendance.models import Attendance
from attendance.views.clock_in_out import perform_clock_in, perform_clock_out
from employee.models import EmployeeFace
from employee.services.face_recognition import FaceRecognitionError, verify_face
from joydigi.decorators import login_required

logger = logging.getLogger(__name__)


def _employee_log_id(employee):
    return employee.badge_id or employee.pk


def _is_clocked_in(employee):
    today = date.today()
    yesterday = today - timedelta(days=1)
    return Attendance.objects.filter(
        employee_id=employee,
        attendance_date__gte=yesterday,
        attendance_date__lte=today,
        attendance_clock_out_date__isnull=True,
    ).exists()


def _action_for(employee):
    return "CHECK_OUT" if _is_clocked_in(employee) else "CHECK_IN"


def _last_message(request, fallback):
    queued = [str(message) for message in get_messages(request)]
    return queued[-1] if queued else fallback


@login_required
@require_GET
def face_attendance_page(request):
    employee = request.user.employee_get
    face_registered = EmployeeFace.objects.filter(employee=employee).exists()
    return render(
        request,
        "attendance/face_attendance.html",
        {
            "face_registered": face_registered,
            "attendance_action": _action_for(employee),
        },
    )


@login_required
@require_POST
def face_attendance_verify(request):
    """Verify 1:1, then let Django choose and execute check-in or check-out."""
    employee = request.user.employee_get
    action = _action_for(employee)
    timestamp = timezone.now().isoformat()
    face_profile = EmployeeFace.objects.filter(employee=employee).first()

    if face_profile is None:
        logger.warning(
            "FACE_VERIFY employee=%s action=%s verified=False face_score= timestamp=%s error=face_not_registered",
            _employee_log_id(employee),
            action,
            timestamp,
        )
        return JsonResponse(
            {
                "success": False,
                "message": "Bạn chưa đăng ký Face ID.",
                "error": {"code": "face_not_registered"},
                "register_url": reverse("face-registration"),
            },
            status=409,
        )

    image = request.FILES.get("image")
    if image is None:
        return JsonResponse(
            {
                "success": False,
                "message": "Vui lòng chụp ảnh khuôn mặt.",
                "error": {"code": "empty_image"},
            },
            status=400,
        )

    try:
        result = verify_face(
            face_profile.embedding,
            image.read(),
            filename=image.name or "face.jpg",
            content_type=image.content_type or "image/jpeg",
        )
        score = float(result.get("score", 0.0))
        if not result.get("verified", False):
            logger.info(
                "FACE_VERIFY employee=%s action=%s verified=False face_score=%.4f timestamp=%s error=face_mismatch",
                _employee_log_id(employee),
                action,
                score,
                timestamp,
            )
            return JsonResponse(
                {
                    "success": False,
                    "verified": False,
                    "score": score,
                    "message": "Khuôn mặt không khớp.",
                    "error": {"code": "face_mismatch"},
                },
                status=403,
            )

        with transaction.atomic():
            # Serialise simultaneous camera submissions for this Face ID, then
            # determine the action again from server-side attendance state.
            locked_profile = (
                EmployeeFace.objects.select_for_update()
                .select_related("employee")
                .get(pk=face_profile.pk)
            )
            employee = locked_profile.employee
            action = _action_for(employee)
            if action == "CHECK_OUT":
                attendance, allowed, reason = perform_clock_out(request)
            else:
                attendance, allowed, reason = perform_clock_in(request)

            if not allowed or attendance is None:
                reason = reason or {}
                message = reason.get("message") or _last_message(
                    request, "Không thể chấm công vào lúc này. Vui lòng thử lại."
                )
                error_code = reason.get("code") or "attendance_rejected"
                transaction.set_rollback(True)
                logger.warning(
                    "FACE_VERIFY employee=%s action=%s verified=True face_score=%.4f timestamp=%s error=%s",
                    _employee_log_id(employee),
                    action,
                    score,
                    timestamp,
                    error_code,
                )
                return JsonResponse(
                    {
                        "success": False,
                        "verified": True,
                        "score": score,
                        "message": message,
                        "error": {"code": error_code},
                    },
                    status=400,
                )

        attendance_message = _last_message(request, "")
        action_label = "Check-in" if action == "CHECK_IN" else "Check-out"
        logger.info(
            "FACE_VERIFY employee=%s action=%s verified=True face_score=%.4f timestamp=%s error=",
            _employee_log_id(employee),
            action,
            score,
            timestamp,
        )
        return JsonResponse(
            {
                "success": True,
                "verified": True,
                "score": score,
                "action": action,
                "message": f"{action_label} thành công.",
                "attendance_message": attendance_message,
                "redirect_url": reverse("home-page"),
            }
        )
    except FaceRecognitionError as exc:
        logger.warning(
            "FACE_VERIFY employee=%s action=%s verified=False face_score= timestamp=%s error=%s",
            _employee_log_id(employee),
            action,
            timestamp,
            exc.code,
        )
        return JsonResponse(
            {
                "success": False,
                "verified": False,
                "message": str(exc),
                "error": {"code": exc.code},
            },
            status=exc.status_code,
        )
    except Exception:
        logger.exception(
            "FACE_VERIFY employee=%s action=%s verified=False face_score= timestamp=%s error=unexpected",
            _employee_log_id(employee),
            action,
            timestamp,
        )
        return JsonResponse(
            {
                "success": False,
                "verified": False,
                "message": "Không thể xác thực khuôn mặt. Vui lòng thử lại.",
                "error": {"code": "unexpected_error"},
            },
            status=500,
        )
