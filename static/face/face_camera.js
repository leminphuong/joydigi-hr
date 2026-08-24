(function () {
    "use strict";

    let activeStream = null;

    function setStatus(element, message, kind) {
        element.textContent = message;
        element.dataset.kind = kind || "info";
    }

    async function start(video, statusElement) {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error("Trình duyệt không hỗ trợ camera hoặc trang chưa dùng HTTPS.");
        }
        activeStream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 960 } },
            audio: false,
        });
        video.srcObject = activeStream;
        await video.play();
        setStatus(statusElement, "Camera đã sẵn sàng. Đưa khuôn mặt vào giữa khung.", "info");
    }

    function capture(video) {
        if (!video.videoWidth || !video.videoHeight) {
            return Promise.reject(new Error("Camera chưa sẵn sàng."));
        }
        const maxWidth = 960;
        const scale = Math.min(1, maxWidth / video.videoWidth);
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(video.videoWidth * scale);
        canvas.height = Math.round(video.videoHeight * scale);
        const context = canvas.getContext("2d");
        context.translate(canvas.width, 0);
        context.scale(-1, 1);
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        return new Promise(function (resolve, reject) {
            canvas.toBlob(function (blob) {
                if (blob) resolve(blob);
                else reject(new Error("Không thể chụp ảnh từ camera."));
            }, "image/jpeg", 0.9);
        });
    }

    function stop() {
        if (activeStream) activeStream.getTracks().forEach(function (track) { track.stop(); });
        activeStream = null;
    }

    function location() {
        return new Promise(function (resolve) {
            if (!navigator.geolocation) return resolve(null);
            navigator.geolocation.getCurrentPosition(
                function (position) {
                    resolve({
                        latitude: position.coords.latitude,
                        longitude: position.coords.longitude,
                    });
                },
                function () { resolve(null); },
                { enableHighAccuracy: true, timeout: 6000, maximumAge: 30000 }
            );
        });
    }

    window.FaceCamera = { start: start, capture: capture, stop: stop, location: location, setStatus: setStatus };
    window.addEventListener("beforeunload", stop);
})();
