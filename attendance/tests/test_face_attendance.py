from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from employee.models import EmployeeFace
from employee.services.face_recognition import FaceRecognitionError
from joydigi.testkit.factories import make_company, make_employee, make_user


def camera_image():
    return SimpleUploadedFile(
        "attendance-face.jpg",
        b"camera-image",
        content_type="image/jpeg",
    )


class FaceAttendanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Face Attendance Co")
        cls.user = make_user("face-attendance")
        cls.employee = make_employee(
            company=cls.company,
            email="face-attendance@test.joydigi",
            user=cls.user,
        )
        cls.other_user = make_user("other-face-user")
        cls.other_employee = make_employee(
            company=cls.company,
            email="other-face@test.joydigi",
            user=cls.other_user,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def register_face(self):
        return EmployeeFace.objects.create(
            employee=self.employee,
            embedding=[1.0, 0.0],
        )

    def test_user_without_face_id_cannot_clock(self):
        response = self.client.post(
            reverse("face-attendance-verify"),
            {"image": camera_image()},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "face_not_registered")

    @patch("attendance.face_views.perform_clock_in")
    @patch("attendance.face_views.verify_face")
    def test_wrong_face_never_calls_attendance_logic(self, verify, perform_clock_in):
        self.register_face()
        verify.return_value = {"success": True, "verified": False, "score": 0.31}

        response = self.client.post(
            reverse("face-attendance-verify"),
            {"image": camera_image()},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "face_mismatch")
        perform_clock_in.assert_not_called()

    @patch("attendance.face_views.perform_clock_in")
    @patch("attendance.face_views.verify_face")
    @patch("attendance.face_views._is_clocked_in", return_value=False)
    def test_matching_face_runs_existing_check_in_and_ignores_frontend_employee_id(
        self, is_clocked_in, verify, perform_clock_in
    ):
        profile = self.register_face()
        verify.return_value = {"success": True, "verified": True, "score": 0.76}
        perform_clock_in.return_value = (SimpleNamespace(id=10), True, None)

        response = self.client.post(
            reverse("face-attendance-verify"),
            {
                "image": camera_image(),
                "employee_id": self.other_employee.pk,
                "action": "CHECK_OUT",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "CHECK_IN")
        self.assertEqual(verify.call_args.args[0], profile.embedding)
        perform_clock_in.assert_called_once()

    @patch("attendance.face_views.perform_clock_out")
    @patch("attendance.face_views.verify_face")
    @patch("attendance.face_views._is_clocked_in", return_value=True)
    def test_matching_face_runs_existing_check_out(
        self, is_clocked_in, verify, perform_clock_out
    ):
        self.register_face()
        verify.return_value = {"success": True, "verified": True, "score": 0.79}
        perform_clock_out.return_value = (SimpleNamespace(id=11), True, None)

        response = self.client.post(
            reverse("face-attendance-verify"),
            {"image": camera_image()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "CHECK_OUT")
        perform_clock_out.assert_called_once()

    @patch("attendance.face_views.perform_clock_in")
    @patch("attendance.face_views.verify_face")
    @patch("attendance.face_views._is_clocked_in", return_value=False)
    def test_attendance_source_rejection_preserves_reason_code(
        self, is_clocked_in, verify, perform_clock_in
    ):
        self.register_face()
        verify.return_value = {"success": True, "verified": True, "score": 0.81}
        perform_clock_in.return_value = (
            None,
            False,
            {
                "code": "VERIFICATION_REQUIRED",
                "message": "Vui lòng bật quyền vị trí để chấm công.",
            },
        )

        response = self.client.post(
            reverse("face-attendance-verify"),
            {"image": camera_image()},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["code"],
            "VERIFICATION_REQUIRED",
        )

    @patch("attendance.face_views.perform_clock_in")
    @patch("attendance.face_views.verify_face")
    def test_no_face_error_never_calls_attendance_logic(self, verify, perform_clock_in):
        self.register_face()
        verify.side_effect = FaceRecognitionError(
            "Không phát hiện khuôn mặt.",
            code="face_not_detected",
            status_code=422,
        )

        response = self.client.post(
            reverse("face-attendance-verify"),
            {"image": camera_image()},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "face_not_detected")
        perform_clock_in.assert_not_called()

    @patch("attendance.face_views.perform_clock_in")
    @patch("attendance.face_views.verify_face")
    def test_multiple_faces_error_never_calls_attendance_logic(self, verify, perform_clock_in):
        self.register_face()
        verify.side_effect = FaceRecognitionError(
            "Chỉ được có một khuôn mặt trong camera.",
            code="multiple_faces",
            status_code=422,
        )

        response = self.client.post(
            reverse("face-attendance-verify"),
            {"image": camera_image()},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "multiple_faces")
        perform_clock_in.assert_not_called()

    @patch("attendance.face_views.perform_clock_in")
    @patch("attendance.face_views.verify_face")
    def test_face_engine_unavailable_fails_closed(self, verify, perform_clock_in):
        self.register_face()
        verify.side_effect = FaceRecognitionError(
            "Không thể xác thực khuôn mặt. Vui lòng thử lại.",
            code="face_engine_unavailable",
            status_code=503,
        )

        response = self.client.post(
            reverse("face-attendance-verify"),
            {"image": camera_image()},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "face_engine_unavailable")
        perform_clock_in.assert_not_called()

    @patch("attendance.face_views._is_clocked_in", return_value=False)
    def test_refreshing_camera_page_preserves_server_decided_action(self, is_clocked_in):
        self.register_face()

        first = self.client.get(reverse("face-attendance"))
        second = self.client.get(reverse("face-attendance"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "CHỤP & CHECK-IN")
