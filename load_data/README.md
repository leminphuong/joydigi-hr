# Dữ liệu mẫu JOYDIGI

Thư mục này là điểm vào của chức năng **Nạp dữ liệu mẫu** trên trang đăng nhập.

1. Khai báo `DB_INIT_PASSWORD` trong tệp `.env`.
2. Khi cơ sở dữ liệu chưa được khởi tạo, chọn **Nạp dữ liệu JOYDIGI**.
3. Nhập đúng giá trị `DB_INIT_PASSWORD`.

Hệ thống tự tạo dữ liệu bằng `base.demo_data.modules.checkin`, gồm:

- Công ty `JOYDIGI`.
- 15 tài khoản: 1 quản trị viên, 2 trưởng nhóm và 12 nhân viên.
- Phòng ban, chức danh, ca hành chính, địa điểm và Wifi văn phòng.
- Dữ liệu chấm công của tháng hiện tại và tháng liền trước.
- Đơn nghỉ phép, yêu cầu chấm công ngoài bán kính, lịch làm việc và bảng tin.

Tài khoản quản trị có tên đăng nhập `admin`; mật khẩu chính là giá trị
`DB_INIT_PASSWORD`. Các tài khoản mẫu còn lại dùng mật khẩu `123456`.

Bộ tạo dữ liệu có thể chạy lại an toàn và không tạo trùng nhân viên hoặc bản
ghi chấm công.
