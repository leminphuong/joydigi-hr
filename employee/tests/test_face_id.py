from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from employee.models import EmployeeFace
from employee.services.face_recognition import FaceRecognitionError
from joydigi.testkit.factories import make_company, make_employee, make_user


def camera_image(name="face.jpg"):
    return SimpleUploadedFile(name, b"camera-image", content_type="image/jpeg")


class FaceRegistrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Face Registration Co")
        cls.user = make_user("face-register")
        cls.employee = make_employee(
            company=cls.company,
            email="face-register@test.joydigi",
            user=cls.user,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_registration_page_is_available_after_refresh(self):
        response = self.client.get(reverse("face-registration"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Đăng ký khuôn mặt")

    @patch("employee.face_views.extract_embedding")
    def test_registers_average_normalized_template_for_current_user(self, extract):
        extract.side_effect = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]

        response = self.client.post(
            reverse("register-face"),
            {"images": [camera_image("one.jpg"), camera_image("two.jpg"), camera_image("three.jpg")]},
        )

        self.assertEqual(response.status_code, 200)
        profile = EmployeeFace.objects.get(employee=self.employee)
        self.assertEqual(profile.embedding, [1.0, 0.0])

    @patch("employee.face_views.extract_embedding")
    def test_re_registration_overwrites_own_template(self, extract):
        profile = EmployeeFace.objects.create(employee=self.employee, embedding=[1.0, 0.0])
        extract.side_effect = [[0.0, 1.0], [0.0, 1.0], [0.0, 1.0]]

        response = self.client.post(
            reverse("register-face"),
            {"images": [camera_image("one.jpg"), camera_image("two.jpg"), camera_image("three.jpg")]},
        )

        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        self.assertEqual(profile.embedding, [0.0, 1.0])
        self.assertEqual(EmployeeFace.objects.filter(employee=self.employee).count(), 1)

    @patch("employee.face_views.extract_embedding")
    def test_registration_reports_no_face(self, extract):
        extract.side_effect = FaceRecognitionError(
            "Không phát hiện khuôn mặt.",
            code="face_not_detected",
            status_code=422,
        )

        response = self.client.post(
            reverse("register-face"),
            {"images": [camera_image("one.jpg"), camera_image("two.jpg"), camera_image("three.jpg")]},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "face_not_detected")
        self.assertFalse(EmployeeFace.objects.filter(employee=self.employee).exists())

    @patch("employee.face_views.extract_embedding")
    def test_registration_reports_multiple_faces(self, extract):
        extract.side_effect = FaceRecognitionError(
            "Chỉ được có một khuôn mặt trong camera.",
            code="multiple_faces",
            status_code=422,
        )

        response = self.client.post(
            reverse("register-face"),
            {"images": [camera_image("one.jpg"), camera_image("two.jpg"), camera_image("three.jpg")]},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "multiple_faces")
