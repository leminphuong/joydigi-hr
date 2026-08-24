"""In-process OpenCV + InsightFace face recognition for Django."""

import logging
import math
import os
import threading

import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)


class FaceRecognitionError(Exception):
    """A safe, user-displayable face-recognition error."""

    def __init__(self, message, *, code="face_recognition_error", status_code=422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _load_dependencies():
    """Import heavy optional dependencies only when recognition is first used."""
    import cv2
    from insightface.app import FaceAnalysis

    return cv2, FaceAnalysis


class FaceEngine:
    """Own one long-lived CPU model instance inside a Django process."""

    def __init__(
        self,
        *,
        model_name,
        model_root,
        detection_size,
        verify_threshold,
    ):
        self.model_name = model_name
        self.model_root = model_root
        self.detection_size = detection_size
        self.verify_threshold = verify_threshold
        self._cv2 = None
        self._face_app = None
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    @property
    def loaded(self):
        return self._cv2 is not None and self._face_app is not None

    def load(self):
        """Load detection and recognition models once per Django process."""
        if self.loaded:
            return

        with self._load_lock:
            if self.loaded:
                return
            try:
                cv2_module, face_analysis_class = _load_dependencies()
                face_app = face_analysis_class(
                    name=self.model_name,
                    root=os.path.expanduser(self.model_root),
                    allowed_modules=["detection", "recognition"],
                    providers=["CPUExecutionProvider"],
                )
                face_app.prepare(
                    ctx_id=-1,
                    det_size=(self.detection_size, self.detection_size),
                )
            except Exception as exc:
                logger.exception("Unable to load the local Face ID model")
                raise FaceRecognitionError(
                    "Không thể khởi tạo nhận diện khuôn mặt. Vui lòng thử lại.",
                    code="face_engine_unavailable",
                    status_code=503,
                ) from exc

            self._cv2 = cv2_module
            self._face_app = face_app
            logger.info(
                "Face ID model loaded model=%s provider=CPUExecutionProvider",
                self.model_name,
            )

    def extract_embedding(self, image_bytes):
        """Decode an image, require exactly one face, and return an L2 vector."""
        self.load()
        if not image_bytes:
            raise FaceRecognitionError(
                "Vui lòng chụp ảnh khuôn mặt.",
                code="empty_image",
                status_code=400,
            )

        buffer = np.frombuffer(image_bytes, dtype=np.uint8)
        image = self._cv2.imdecode(buffer, self._cv2.IMREAD_COLOR)
        if image is None:
            raise FaceRecognitionError(
                "Ảnh khuôn mặt không hợp lệ.",
                code="invalid_image",
                status_code=400,
            )

        with self._inference_lock:
            faces = self._face_app.get(image)
        if len(faces) == 0:
            raise FaceRecognitionError(
                "Không phát hiện khuôn mặt.",
                code="face_not_detected",
                status_code=422,
            )
        if len(faces) > 1:
            raise FaceRecognitionError(
                "Chỉ được có một khuôn mặt trong camera.",
                code="multiple_faces",
                status_code=422,
            )

        raw_embedding = getattr(faces[0], "normed_embedding", None)
        if raw_embedding is None:
            raise FaceRecognitionError(
                "Không thể trích xuất Face ID.",
                code="invalid_embedding",
                status_code=500,
            )
        embedding = np.asarray(raw_embedding, dtype=np.float32)
        if embedding.ndim != 1 or embedding.size == 0 or not np.isfinite(embedding).all():
            raise FaceRecognitionError(
                "Không thể trích xuất Face ID.",
                code="invalid_embedding",
                status_code=500,
            )
        norm = float(np.linalg.norm(embedding))
        if not math.isfinite(norm) or norm <= 0:
            raise FaceRecognitionError(
                "Không thể trích xuất Face ID.",
                code="invalid_embedding",
                status_code=500,
            )
        return embedding / norm

    @staticmethod
    def cosine_similarity(first_embedding, second_embedding):
        try:
            first = np.asarray(first_embedding, dtype=np.float32)
            second = np.asarray(second_embedding, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise FaceRecognitionError(
                "Face ID đã đăng ký không hợp lệ.",
                code="invalid_registered_embedding",
                status_code=400,
            ) from exc
        if first.ndim != 1 or second.ndim != 1 or first.shape != second.shape:
            raise FaceRecognitionError(
                "Face ID đã đăng ký không hợp lệ.",
                code="invalid_registered_embedding",
                status_code=400,
            )
        if not np.isfinite(first).all() or not np.isfinite(second).all():
            raise FaceRecognitionError(
                "Face ID đã đăng ký không hợp lệ.",
                code="invalid_registered_embedding",
                status_code=400,
            )
        denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
        if not math.isfinite(denominator) or denominator <= 0:
            raise FaceRecognitionError(
                "Face ID đã đăng ký không hợp lệ.",
                code="invalid_registered_embedding",
                status_code=400,
            )
        return float(np.dot(first, second) / denominator)

    def verify(self, registered_embedding, image_bytes):
        camera_embedding = self.extract_embedding(image_bytes)
        score = self.cosine_similarity(registered_embedding, camera_embedding)
        return score >= self.verify_threshold, score


_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = FaceEngine(
                    model_name=settings.FACE_MODEL_NAME,
                    model_root=settings.FACE_MODEL_ROOT,
                    detection_size=settings.FACE_DETECTION_SIZE,
                    verify_threshold=settings.FACE_VERIFY_THRESHOLD,
                )
    return _engine


def _validate_image(image_bytes, content_type):
    if not image_bytes:
        raise FaceRecognitionError(
            "Vui lòng chụp ảnh khuôn mặt.",
            code="empty_image",
            status_code=400,
        )
    if len(image_bytes) > settings.FACE_IMAGE_MAX_BYTES:
        raise FaceRecognitionError(
            "Ảnh khuôn mặt vượt quá dung lượng cho phép.",
            code="image_too_large",
            status_code=413,
        )
    if content_type not in {"image/jpeg", "image/jpg", "image/png", "image/webp"}:
        raise FaceRecognitionError(
            "Định dạng ảnh không được hỗ trợ.",
            code="unsupported_image_type",
            status_code=415,
        )


def extract_embedding(image_bytes, *, filename="face.jpg", content_type="image/jpeg"):
    del filename  # Kept in the public signature for the existing Django views.
    _validate_image(image_bytes, content_type)
    embedding = _get_engine().extract_embedding(image_bytes)
    return embedding.astype(float).tolist()


def verify_face(
    registered_embedding,
    image_bytes,
    *,
    filename="face.jpg",
    content_type="image/jpeg",
):
    del filename
    _validate_image(image_bytes, content_type)
    verified, score = _get_engine().verify(registered_embedding, image_bytes)
    if not math.isfinite(score):
        raise FaceRecognitionError(
            "Kết quả nhận diện khuôn mặt không hợp lệ.",
            code="invalid_face_result",
            status_code=500,
        )
    return {"success": True, "verified": bool(verified), "score": float(score)}
