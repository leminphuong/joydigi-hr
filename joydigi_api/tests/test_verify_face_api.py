"""
Phase 6.3C.1 — mobile JWT Camera AI (face) verification endpoint,
`POST /api/attendance/verify-face/`.

Deliberately mocks `employee.services.face_recognition.verify_face`
(via the name it's imported under in
`joydigi_api.api_views.attendance.views`) rather than requiring real
InsightFace inference or real biometric data — the engine itself is
existing, already-shipped code (`employee/services/face_recognition.py`),
not something this phase changes. These tests only prove the new
endpoint's own logic: enrollment gating, error-code mapping, proof
issuance, and — critically — that it never writes an Attendance row
itself.
"""

from datetime import date, datetime
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from attendance.methods.verification_proof import consume_verification_proof
from attendance.models import Attendance
from attendance.views.clock_in_out import clock_in_attendance_and_activity
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeFace, EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

_DUMMY_EMBEDDING = [0.1] * 512


def _fake_image():
    return SimpleUploadedFile("face.jpg", b"not-a-real-jpeg", content_type="image/jpeg")


class VerifyFaceAPITests(TestCase):
    def setUp(self):
        self.company = make_company("Face Co")
        self.user = make_user("faceuser", password="secret123")
        self.employee = make_employee(
            company=self.company,
            email="face@test.joydigi",
            user=self.user,
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Face Shift")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )
        self.today = date.today()
        day_name = self.today.strftime("%A").lower()
        self.day = EmployeeShiftDay.objects.get(day=day_name)
        EmployeeShiftSchedule.objects.get_or_create(
            shift_id=self.shift,
            day=self.day,
            defaults={
                "is_night_shift": False,
                "minimum_working_hour": "08:00",
                "start_time": "08:00:00",
                "end_time": "17:00:00",
            },
        )
        self.client = APIClient()

    def _authenticate(self):
        self.client.force_authenticate(user=self.user)

    def _enroll(self):
        return EmployeeFace.objects.create(
            employee=self.employee, embedding=_DUMMY_EMBEDDING
        )

    # 1. unauthenticated request rejected
    def test_unauthenticated_request_rejected(self):
        response = self.client.post(
            "/api/attendance/verify-face/", {"image": _fake_image()}, format="multipart"
        )
        self.assertEqual(response.status_code, 401)

    # 2, 3. enrolled employee + valid matching face -> success + proof present
    def test_matching_face_returns_proof(self):
        self._authenticate()
        self._enroll()
        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            return_value={"success": True, "verified": True, "score": 0.91},
        ):
            response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["verified"])
        self.assertEqual(response.data["method"], "camera_ai")
        self.assertIn("proof", response.data)
        self.assertTrue(response.data["proof"])
        self.assertEqual(response.data["expires_in"], 120)

    # 4. proof works with the existing attendance write architecture
    def test_proof_consumable_by_existing_verification_proof_machinery(self):
        self._authenticate()
        self._enroll()
        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            return_value={"success": True, "verified": True, "score": 0.91},
        ):
            response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        proof = response.data["proof"]
        method = consume_verification_proof(proof, self.employee.id)
        self.assertEqual(method, "camera_ai")

    # 5. proof is single-use
    def test_proof_is_single_use(self):
        self._authenticate()
        self._enroll()
        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            return_value={"success": True, "verified": True, "score": 0.91},
        ):
            response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        proof = response.data["proof"]
        first = consume_verification_proof(proof, self.employee.id)
        second = consume_verification_proof(proof, self.employee.id)
        self.assertEqual(first, "camera_ai")
        self.assertIsNone(second)

    # 6. proof expires
    def test_proof_expires(self):
        self._authenticate()
        self._enroll()
        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            return_value={"success": True, "verified": True, "score": 0.91},
        ):
            response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        proof = response.data["proof"]
        with mock.patch(
            "attendance.methods.verification_proof.PROOF_TTL", -1
        ):
            method = consume_verification_proof(proof, self.employee.id)
        self.assertIsNone(method)

    # 7. proof cannot be used by another employee
    def test_proof_cannot_be_used_by_another_employee(self):
        self._authenticate()
        self._enroll()
        other_user = make_user("otherfaceuser", password="secret123")
        other_employee = make_employee(
            company=self.company,
            email="otherface@test.joydigi",
            user=other_user,
        )
        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            return_value={"success": True, "verified": True, "score": 0.91},
        ):
            response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        proof = response.data["proof"]
        method = consume_verification_proof(proof, other_employee.id)
        self.assertIsNone(method)

    # 8. no EmployeeFace -> FACE_NOT_ENROLLED
    def test_no_enrollment_returns_face_not_enrolled(self):
        self._authenticate()
        response = self.client.post(
            "/api/attendance/verify-face/", {"image": _fake_image()}, format="multipart"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "FACE_NOT_ENROLLED")

    # 9. no image -> FACE_IMAGE_REQUIRED
    def test_no_image_returns_face_image_required(self):
        self._authenticate()
        self._enroll()
        response = self.client.post(
            "/api/attendance/verify-face/", {}, format="multipart"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "FACE_IMAGE_REQUIRED")

    # 10. invalid image -> controlled JSON error
    def test_invalid_image_is_a_controlled_error(self):
        self._authenticate()
        self._enroll()
        from employee.services.face_recognition import FaceRecognitionError

        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            side_effect=FaceRecognitionError(
                "bad", code="invalid_image", status_code=400
            ),
        ):
            response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "FACE_IMAGE_INVALID")

    # 11. no face detected -> controlled error
    def test_no_face_detected_is_controlled(self):
        self._authenticate()
        self._enroll()
        from employee.services.face_recognition import FaceRecognitionError

        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            side_effect=FaceRecognitionError(
                "none", code="face_not_detected", status_code=422
            ),
        ):
            response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "FACE_NOT_DETECTED")

    # 12. multiple faces -> controlled error
    def test_multiple_faces_is_controlled(self):
        self._authenticate()
        self._enroll()
        from employee.services.face_recognition import FaceRecognitionError

        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            side_effect=FaceRecognitionError(
                "many", code="multiple_faces", status_code=422
            ),
        ):
            response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "MULTIPLE_FACES_DETECTED")

    # 13. non-match -> FACE_NOT_MATCHED
    def test_non_matching_face_returns_face_not_matched(self):
        self._authenticate()
        self._enroll()
        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            return_value={"success": True, "verified": False, "score": 0.1},
        ):
            response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "FACE_NOT_MATCHED")

    # 14. AI engine failure -> controlled JSON 5xx
    def test_engine_unavailable_is_controlled_5xx(self):
        self._authenticate()
        self._enroll()
        from employee.services.face_recognition import FaceRecognitionError

        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            side_effect=FaceRecognitionError(
                "down", code="face_engine_unavailable", status_code=503
            ),
        ):
            response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "FACE_ENGINE_UNAVAILABLE")
        # No traceback/internal detail ever reaches the client.
        self.assertNotIn("Traceback", str(response.data))

    # 15. failed verification performs ZERO attendance write
    def test_failed_verification_performs_no_attendance_write(self):
        self._authenticate()
        self._enroll()
        before = Attendance.objects.count()
        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            return_value={"success": True, "verified": False, "score": 0.1},
        ):
            self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        self.assertEqual(Attendance.objects.count(), before)

    # 16. successful face verification ALONE performs ZERO attendance write
    def test_successful_verification_alone_performs_no_attendance_write(self):
        self._authenticate()
        self._enroll()
        before = Attendance.objects.count()
        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            return_value={"success": True, "verified": True, "score": 0.91},
        ):
            self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        self.assertEqual(Attendance.objects.count(), before)

    # 4 (extended) — the proof, once issued, actually clocks the
    # employee in through the real, unmodified clock-in endpoint —
    # proving Camera AI is wired exactly like Location/Wifi/QR from the
    # write endpoint's perspective, not a special case.
    def test_proof_completes_a_real_clock_in(self):
        self._authenticate()
        self._enroll()
        with mock.patch(
            "joydigi_api.api_views.attendance.views.verify_face",
            return_value={"success": True, "verified": True, "score": 0.91},
        ):
            verify_response = self.client.post(
                "/api/attendance/verify-face/",
                {"image": _fake_image()},
                format="multipart",
            )
        proof = verify_response.data["proof"]

        clock_in_response = self.client.post(
            "/api/attendance/clock-in/", {"verification_proof": proof}
        )
        self.assertEqual(clock_in_response.status_code, 200, clock_in_response.data)
        attendance = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertIsNotNone(attendance.attendance_clock_in)


class AttendancePolicyCameraAiTests(TestCase):
    """Step 8 — camera_ai.enabled reflects real EmployeeFace enrollment,
    not a hardcoded flag, and doesn't affect any other policy field."""

    def setUp(self):
        self.company = make_company("Policy Face Co")
        self.user = make_user("policyfaceuser", password="secret123")
        self.employee = make_employee(
            company=self.company,
            email="policyface@test.joydigi",
            user=self.user,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_camera_ai_disabled_without_enrollment(self):
        response = self.client.get("/api/attendance/policy/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["camera_ai"]["enabled"])

    def test_camera_ai_enabled_once_enrolled(self):
        EmployeeFace.objects.create(employee=self.employee, embedding=_DUMMY_EMBEDDING)
        response = self.client.get("/api/attendance/policy/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["camera_ai"]["enabled"])
