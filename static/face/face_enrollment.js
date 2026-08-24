(function () {
    "use strict";

    const root = document.getElementById("faceEnrollment");
    if (!root) return;

    const video = document.getElementById("faceVideo");
    const status = document.getElementById("faceStatus");
    const captureButton = document.getElementById("captureFace");
    const saveButton = document.getElementById("saveFace");
    const resetButton = document.getElementById("resetFace");
    const previewContainer = document.getElementById("facePreviews");
    const csrfToken = root.querySelector("[name=csrfmiddlewaretoken]").value;
    const instructions = ["nhìn thẳng", "hơi nghiêng trái", "hơi nghiêng phải"];
    let captures = [];
    let previewUrls = [];
    let cameraReady = false;

    function updateControls() {
        const index = captures.length;
        captureButton.disabled = index >= 3 || !cameraReady;
        saveButton.disabled = index !== 3;
        captureButton.textContent = index < 3
            ? "CHỤP ẢNH " + (index + 1) + "/3 — " + instructions[index].toUpperCase()
            : "ĐÃ CHỤP ĐỦ 3 ẢNH";
    }

    function reset() {
        previewUrls.forEach(URL.revokeObjectURL);
        previewUrls = [];
        captures = [];
        previewContainer.textContent = "";
        FaceCamera.setStatus(status, "Đưa khuôn mặt vào giữa khung và bắt đầu chụp.", "info");
        updateControls();
    }

    function addPreview(blob, index) {
        const wrapper = document.createElement("div");
        const image = document.createElement("img");
        const label = document.createElement("span");
        const url = URL.createObjectURL(blob);
        previewUrls.push(url);
        wrapper.className = "face-preview";
        image.src = url;
        image.alt = "Ảnh khuôn mặt " + (index + 1);
        label.textContent = instructions[index];
        wrapper.append(image, label);
        previewContainer.appendChild(wrapper);
    }

    captureButton.addEventListener("click", async function () {
        try {
            const blob = await FaceCamera.capture(video);
            const index = captures.length;
            captures.push(blob);
            addPreview(blob, index);
            FaceCamera.setStatus(
                status,
                captures.length === 3 ? "Đã chụp đủ 3 ảnh. Bạn có thể lưu Face ID." : "Ảnh đã chụp. Tiếp tục " + instructions[captures.length] + ".",
                captures.length === 3 ? "success" : "info"
            );
            updateControls();
        } catch (error) {
            FaceCamera.setStatus(status, error.message, "error");
        }
    });

    resetButton.addEventListener("click", reset);

    saveButton.addEventListener("click", async function () {
        if (captures.length !== 3) return;
        saveButton.disabled = true;
        captureButton.disabled = true;
        FaceCamera.setStatus(status, "Đang tạo Face ID từ 3 ảnh...", "busy");
        const form = new FormData();
        captures.forEach(function (blob, index) {
            form.append("images", blob, "face-" + (index + 1) + ".jpg");
        });
        try {
            const response = await fetch(root.dataset.registerUrl, {
                method: "POST",
                headers: { "X-CSRFToken": csrfToken },
                credentials: "same-origin",
                body: form,
            });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.message || "Không thể đăng ký khuôn mặt.");
            FaceCamera.setStatus(status, "✓ " + data.message, "success");
            FaceCamera.stop();
        } catch (error) {
            FaceCamera.setStatus(status, "✕ " + error.message, "error");
            updateControls();
        }
    });

    updateControls();
    FaceCamera.start(video, status).then(function () {
        cameraReady = true;
        updateControls();
    }).catch(function (error) {
        FaceCamera.setStatus(status, "✕ " + error.message, "error");
        captureButton.disabled = true;
    });
})();
