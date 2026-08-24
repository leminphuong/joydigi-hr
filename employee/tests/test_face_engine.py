from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import numpy as np

from employee.services.face_recognition import FaceEngine, FaceRecognitionError


class FakeCV2:
    IMREAD_COLOR = 1

    @staticmethod
    def imdecode(buffer, mode):
        del mode
        return object() if buffer.size else None


class FakeFaceApp:
    def __init__(self, faces):
        self.faces = faces

    def get(self, image):
        del image
        return self.faces


class FaceEngineTests(TestCase):
    def make_engine(self, faces, threshold=0.55):
        engine = FaceEngine(
            model_name="test-model",
            model_root="~/.insightface",
            detection_size=640,
            verify_threshold=threshold,
        )
        engine._cv2 = FakeCV2()
        engine._face_app = FakeFaceApp(faces)
        return engine

    @patch("employee.services.face_recognition._load_dependencies")
    def test_model_is_loaded_and_prepared_only_once(self, load_dependencies):
        face_analysis = MagicMock()
        model = face_analysis.return_value
        load_dependencies.return_value = (FakeCV2(), face_analysis)
        engine = self.make_engine([])
        engine._cv2 = None
        engine._face_app = None

        engine.load()
        engine.load()

        face_analysis.assert_called_once()
        self.assertEqual(
            face_analysis.call_args.kwargs["providers"],
            ["CPUExecutionProvider"],
        )
        self.assertEqual(
            face_analysis.call_args.kwargs["allowed_modules"],
            ["detection", "recognition"],
        )
        model.prepare.assert_called_once_with(ctx_id=-1, det_size=(640, 640))

    def test_no_face_has_specific_error(self):
        engine = self.make_engine([])

        with self.assertRaises(FaceRecognitionError) as context:
            engine.extract_embedding(b"image")

        self.assertEqual(context.exception.code, "face_not_detected")

    def test_multiple_faces_have_specific_error(self):
        face = SimpleNamespace(normed_embedding=np.array([1.0, 0.0]))
        engine = self.make_engine([face, face])

        with self.assertRaises(FaceRecognitionError) as context:
            engine.extract_embedding(b"image")

        self.assertEqual(context.exception.code, "multiple_faces")

    def test_embedding_is_normalized(self):
        face = SimpleNamespace(normed_embedding=np.array([3.0, 4.0]))
        engine = self.make_engine([face])

        embedding = engine.extract_embedding(b"image")

        self.assertAlmostEqual(float(np.linalg.norm(embedding)), 1.0, places=6)

    def test_verify_uses_configured_cosine_threshold(self):
        face = SimpleNamespace(normed_embedding=np.array([0.6, 0.8]))
        engine = self.make_engine([face], threshold=0.75)

        verified, score = engine.verify([1.0, 0.0], b"image")

        self.assertFalse(verified)
        self.assertAlmostEqual(score, 0.6, places=6)

