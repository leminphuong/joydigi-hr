(function () {
    "use strict";

    const root = document.getElementById("faceAttendance");
    if (!root) return;

    const video = document.getElementById("faceVideo");
    const status = document.getElementById("faceStatus");
    const actionButton = document.getElementById("faceAttendanceButton");
    const csrfToken = root.querySelector("[name=csrfmiddlewaretoken]").value;

    function successLines(actionMessage, attendanceMessage) {
        status.textContent = "";
        status.dataset.kind = "success";
        ["✓ Xác thực khuôn mặt thành công", "✓ " + actionMessage, attendanceMessage]
            .filter(Boolean)
            .forEach(function (line) {
                const item = document.createElement("div");
                item.textContent = line;
                status.appendChild(item);
            });
    }

    actionButton.addEventListener("click", async function () {
        actionButton.disabled = true;
        try {
            const blob = await FaceCamera.capture(video);
            FaceCamera.setStatus(status, "Đang xác thực khuôn mặt...", "busy");
            const form = new FormData();
            form.append("image", blob, "attendance-face.jpg");
            const position = await FaceCamera.location();
            if (position) {
                form.append("latitude", position.latitude);
                form.append("longitude", position.longitude);
            }
            const response = await fetch(root.dataset.verifyUrl, {
                method: "POST",
                headers: { "X-CSRFToken": csrfToken },
                credentials: "same-origin",
                body: form,
            });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.message || "Không thể chấm công.");
            successLines(data.message, data.attendance_message);
            FaceCamera.stop();
            window.setTimeout(function () { window.location.assign(data.redirect_url || "/"); }, 1400);
        } catch (error) {
            FaceCamera.setStatus(status, "✕ " + error.message, "error");
            actionButton.disabled = false;
        }
    });

    FaceCamera.start(video, status).then(function () {
        actionButton.disabled = false;
    }).catch(function (error) {
        FaceCamera.setStatus(status, "✕ " + error.message, "error");
        actionButton.disabled = true;
    });
})();
