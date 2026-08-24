"""Self-service Face ID enrolment for the authenticated employee."""

import logging

import numpy as np
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from employee.models import EmployeeFace
from employee.services.face_recognition import FaceRecognitionError, extract_embedding
from joydigi.decorators import login_required

logger = logging.getLogger(__name__)


def _employee_log_id(employee):
    return employee.badge_id or employee.pk


@login_required
@require_GET
def face_registration_page(request):
    employee = request.user.employee_get
    return render(
        request,
        "employee/face_registration.html",
        {"face_registered": EmployeeFace.objects.filter(employee=employee).exists()},
    )


@login_required
@require_POST
def register_face(request):
    employee = request.user.employee_get
    images = request.FILES.getlist("images")
    timestamp = timezone.now().isoformat()

    if len(images) != 3:
        return JsonResponse(
            {
                "success": False,
                "message": "Vui lòng chụp đủ 3 ảnh khuôn mặt.",
                "error": {"code": "three_images_required"},
            },
            status=400,
        )

    try:
        embeddings = []
        for image in images:
            embeddings.append(
                extract_embedding(
                    image.read(),
                    filename=image.name or "face.jpg",
                    content_type=image.content_type or "image/jpeg",
                )
            )

        try:
            matrix = np.asarray(embeddings, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise FaceRecognitionError(
                "Face template trả về không hợp lệ.",
                code="invalid_embedding",
                status_code=502,
            ) from exc
        if matrix.ndim != 2 or matrix.shape[0] != 3 or matrix.shape[1] == 0:
            raise FaceRecognitionError(
                "Face template trả về không hợp lệ.",
                code="invalid_embedding",
                status_code=502,
            )
        if not np.isfinite(matrix).all():
            raise FaceRecognitionError(
                "Face template trả về không hợp lệ.",
                code="invalid_embedding",
                status_code=502,
            )

        template = np.mean(matrix, axis=0)
        norm = float(np.linalg.norm(template))
        if not np.isfinite(norm) or norm <= 0:
            raise FaceRecognitionError(
                "Không thể tạo Face ID từ các ảnh đã chụp.",
                code="invalid_embedding",
                status_code=422,
            )
        template = template / norm
        _, created = EmployeeFace.objects.update_or_create(
            employee=employee,
            defaults={"embedding": template.astype(float).tolist()},
        )
        logger.info(
            "FACE_ENROLL employee=%s action=%s timestamp=%s error=",
            _employee_log_id(employee),
            "REGISTER" if created else "RE_REGISTER",
            timestamp,
        )
        return JsonResponse(
            {
                "success": True,
                "message": (
                    "Đăng ký khuôn mặt thành công."
                    if created
                    else "Đăng ký lại khuôn mặt thành công."
                ),
                "registered": True,
            }
        )
    except FaceRecognitionError as exc:
        logger.warning(
            "FACE_ENROLL employee=%s action=REGISTER timestamp=%s error=%s",
            _employee_log_id(employee),
            timestamp,
            exc.code,
        )
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
                "error": {"code": exc.code},
            },
            status=exc.status_code,
        )
    except Exception:
        logger.exception(
            "FACE_ENROLL employee=%s action=REGISTER timestamp=%s error=unexpected",
            _employee_log_id(employee),
            timestamp,
        )
        return JsonResponse(
            {
                "success": False,
                "message": "Không thể đăng ký khuôn mặt. Vui lòng thử lại.",
                "error": {"code": "unexpected_error"},
            },
            status=500,
        )
