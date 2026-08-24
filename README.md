# JoyDigi HR

## Chạy hệ thống trên Windows

Cài hoặc cập nhật thư viện trong môi trường Django:

```powershell
cd C:\Users\PC\Downloads\joydigi-hr
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Khởi tạo cơ sở dữ liệu và chạy hệ thống:

```powershell
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
```

Face ID chạy trực tiếp trong Django, không cần khởi động dịch vụ hoặc cổng riêng.
Model được nạp một lần khi có yêu cầu nhận diện đầu tiên. Trong lần dùng đầu tiên,
InsightFace có thể tải model `buffalo_l` vào `~/.insightface` nên sẽ mất thêm thời
gian. Sau khi đăng nhập, đăng ký tại `/employee/face-id/` và chấm công tại
`/attendance/face/`.

Các cấu hình Face ID tùy chọn trong `.env`:

```dotenv
FACE_VERIFY_THRESHOLD=0.55
FACE_MODEL_NAME=buffalo_l
FACE_MODEL_ROOT=~/.insightface
FACE_DETECTION_SIZE=640
FACE_IMAGE_MAX_BYTES=5242880
```
