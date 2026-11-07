# Chuyển dữ liệu JOYOKR (WordPress) → Django OKR

Mục tiêu: Web `okr.joydigi.net` và AppCheckin2 cùng đọc/ghi **một** hệ Django OKR
(PostgreSQL), có đủ dữ liệu cũ của JOYOKR v1.6.45, không thất thoát.
Không ghi song song MySQL và PostgreSQL; WordPress chỉ là nguồn đọc cho tới khi cutover.

Công cụ (thư mục `okr/legacy_import/`):

| Thành phần | Việc làm |
|---|---|
| `joyokr_export.php` | Chạy bằng WP-CLI trên WordPress, **chỉ đọc**: `wp eval-file joyokr_export.php > joyokr-export.json` (in tự kiểm tra ra STDERR) |
| `joyokr_export_queries.sql` + `manage.py okr_import_wp assemble` | Khi không có SSH/WP-CLI: 16 câu SELECT chạy trong phpMyAdmin/Adminer, lưu CSV, ghép thành cùng định dạng |
| `manage.py okr_import_wp propose` | Đề xuất ánh xạ người dùng/phòng ban, **không ghi DB**, không tự xác minh |
| `manage.py okr_import_wp import` | Mặc định **chạy thử** (một transaction rồi rollback); `--apply --operator <user>` mới ghi |
| `manage.py okr_import_wp rollback --batch N` | Xoá đúng các bản ghi do batch N tạo; từ chối nếu dữ liệu mới đã phụ thuộc |
| `manage.py okr_inventory` | Đếm bản ghi OKR hiện có theo công ty (chỉ SELECT) |

## M1 — Ánh xạ bảng

Thời gian trong các bảng `joyokr_*` là **UTC** (`current_time('mysql', true)`), nhập giữ nguyên.
Ngoại lệ: chu kỳ mặc định do theme tạo lúc cài dùng giờ địa phương (lệch tối đa 7 giờ ở `created_at`).

| JOYOKR (`wp_joyokr_*`) | Django | Ghi chú |
|---|---|---|
| `wp_users` + meta `joyokr_employee_code` | `okr_identity_map` → `employee.Employee` | Chỉ dùng dòng **verified** do người duyệt đặt. Không tạo nhân viên, không chuyển mật khẩu |
| `departments` | `base.Department` (HR) qua file ánh xạ | Không tạo phòng ban. `manager_user_id` → `okr_department_role` (chỉ khi `--import-roles`), `code` → `legacy_wp_code` |
| `teams` | `okr_team` | `legacy_wp_id`; phòng ban, manager qua ánh xạ |
| `team_members` | `okr_team_member` | Khoá (team, nhân viên); người chưa ánh xạ bị bỏ (báo cáo) |
| `cycles` | `okr_cycle` | `parent_id` → `parent` (lượt 2). Trạng thái lạ → `closed` (cảnh báo) |
| `objectives` | `okr_objective` | `parent_objective_id` → `parent` (lượt 2); `deleted_at` → giữ, `approval_status=deleted`; `progress`, `weight`, `priority` giữ nguyên |
| `key_results` | `okr_key_result` | Giá trị bắt đầu/hiện tại/mục tiêu, trọng số, hạn, tiến độ giữ nguyên |
| `checkins` | `okr_checkin` | Lịch sử: tạo một lần, không sửa |
| `comments` | `okr_comment` | `object_type` objective/key_result; `parent_id` (lượt 2) |
| `evidence` + tệp đính kèm | `okr_evidence` + kho riêng `private_media/okr` | Tệp kiểm tra kích thước, SHA-256, chữ ký định dạng như upload mới; liên kết chỉ http(s). Tệp thiếu/sai → bỏ, báo lỗi |
| `notifications` | `okr_notification` | `reference_id` đổi sang id mới; `dedupe_key` thêm tiền tố `wp:`; người nhận chưa ánh xạ → bỏ |
| `activity_log` | `okr_activity` | Lịch sử duyệt/từ chối (lý do trong `metadata.reason`); hành động lạ → `updated` + `metadata.wp_action` |
| Tuỳ chọn `joyokr_*` | `okr_settings` | Chỉ khi `--import-settings`; **không** lấy Telegram token, SSO secret |
| Vai trò WP `joyokr_manager` | `okr_role` (manager) + `okr_role_event` | Chỉ khi `--import-roles` |
| Vai trò WP `administrator` | — | **Không cấp tự động.** Liệt kê trong báo cáo để cấp thủ công bằng `okr_grant_role` sau phê duyệt |

Không nhập: meta `joyokr_phone`, `joyokr_telegram_chat_id`, `joyokr_avatar_id`, `joyokr_shift`,
mật khẩu, phiên đăng nhập. Phòng ban/quản lý trực tiếp của nhân viên lấy theo **HR**, không theo meta WordPress.

### Quy tắc danh tính

1. `propose` chỉ gợi ý trong **đúng công ty** đích: email + mã nhân viên cùng một người → `email_and_badge`;
   chỉ mã → `badge`; chỉ email → `email` (ghi chú “bắt buộc kiểm tra thủ công”); xung đột hoặc trùng nhiều → `unmatched`.
2. Mọi gợi ý ở trạng thái `proposed`. Người quản trị sửa file và đặt từng dòng thành `verified` (hoặc `rejected`
   khi xác nhận người đó không còn/không có nhân viên HR — liên kết để trống).
3. `import` dừng (không ghi gì) nếu còn người dùng/phòng ban **được dữ liệu tham chiếu** mà chưa quyết định.
   Người dùng WordPress đã bị xoá: liên kết để trống, ghi cảnh báo.
4. Một nhân viên chỉ được xác minh cho một tài khoản WordPress.

### Idempotent và delta

* Khoá là `legacy_wp_id` (duy nhất toàn hệ — một site JOYOKR).
* Chạy lại cùng file: không tạo, không sửa gì. File mới hơn (delta lúc cutover): cập nhật định nghĩa đã đổi,
  thêm check-in/hoạt động/bình luận/bằng chứng mới, cập nhật trạng thái đã đọc của thông báo.
* Bản ghi tạo trên Django (không có `legacy_wp_id`) **không bao giờ bị sửa**.

### Kiểm tra nguồn trước khi ghi (`okr/legacy_import/validate.py`)

Chạy trong `inspect` (không cần mapping) và trong mọi `import` (chạy thử và `--apply` như nhau), **trước** khi ghi
bất kỳ dòng nào. Không tự sửa nguồn, không tự bỏ bản ghi. Mỗi phát hiện có mã, bảng, ID WordPress.

| Mức | Mã (ví dụ) | Hệ quả |
|---|---|---|
| **ERROR** | `objective_parent_loop`, `objective_parent_missing`, `objective_cycle_missing`, `cycle_parent_missing/loop`, `cycle_dates_invalid`, `key_result_objective_missing`, `checkin_key_result_missing`, `comment_target_missing`, `evidence_key_result_missing`, `team_missing`, `scope_target_missing`, `department_not_mapped` (Objective cấp phòng ban), `key_result_vs_latest_checkin` (KR còn hiệu lực), `weight_total_invalid` (Objective đã gửi/đã duyệt), `approval_status_invalid`, `value_unknown`, `timestamp_unreadable`, `owner_missing`, `user_deleted_in_wordpress` (owner), `owner_not_verified` (owner bị `rejected`), `user_not_verified`, `objective_without_key_results` (Objective đã gửi/đã duyệt không có KR — JOYOKR 1.6.45 bắt buộc 2–4 KR khi gửi/duyệt, không có ngoại lệ), `evidence_file_missing`, `evidence_checksum_missing/mismatch`, `evidence_file_refused`, `evidence_link_invalid`, `table_missing` | Chặn toàn bộ, không ghi gì |
| **WARNING** | `user_rejected_in_mapping` (liên kết để trống theo quyết định), `user_deleted_in_wordpress` (tác giả, thành viên…), `key_result_progress_vs_formula`, `objective_progress_vs_key_results`, `key_result_value_without_checkin` (cũng liệt kê trong `reconciliation.key_results_without_checkin_history`), `key_result_count`, `weight_total_draft`, `approval_mismatch`, `objective_parent_other_cycle`, `comment_parent_missing`, `evidence_checkin_missing`, `team_department_missing`, `activity_target_missing`, `value_empty` | Nhập; quản trị viên phải xem |
| **INFO** | `deleted_in_wordpress` (nhập dưới dạng đã xoá), `notification_not_migrated`, `notification_reference_missing`, `activity_object_unsupported`, `activity_action_unknown`, `admin_not_granted` | Không ảnh hưởng |

Ngoài ra, nếu lúc ghi importer vẫn phải bỏ một dòng (model từ chối, `legacy_wp_id` thuộc công ty khác), cả lần chạy
bị từ chối và rollback. `--allow-unmapped` (không dùng cho cutover) chỉ hạ `user_not_verified` của liên kết không bắt buộc
xuống WARNING; owner vẫn là ERROR.

### Chốt chặn xung đột (hotfix M3, `okr/legacy_import/provenance.py`)

Mỗi batch `--apply` thành công lưu **ảnh chụp** (`stats.baseline`) các trường được bảo vệ của từng dòng nó đã
tạo/cập nhật/xác nhận. Lần nhập sau so sánh **ba chiều** cho mỗi dòng đã có: *đã nhập* (ảnh chụp) / *Django hiện tại*
/ *file xuất*. Không dựa vào timestamp.

| Loại xung đột | Khi nào |
|---|---|
| `changed_in_django` | Django ≠ ảnh chụp (sửa trên Web/App, duyệt, check-in làm đổi giá trị/tiến độ, xoá mềm) |
| `django_checkins` | KR đã có check-in tạo trên Django (kể cả cùng giá trị) — giá trị KR thuộc về lịch sử Django |
| `wordpress_change_on_django_data` | WordPress đổi một dòng đã có dữ liệu Django gắn vào (KR/Objective con, bình luận, bằng chứng, hoạt động) |
| `deleted_in_django` | Dòng từng được nhập đã bị xoá trên Django (bằng chứng, thành viên team, vai trò bị thu hồi…) — nhập lại sẽ “hồi sinh” |
| `no_provenance` | Dòng có `legacy_wp_id` nhưng không có ảnh chụp và file xuất khác giá trị hiện tại |
| `value_vs_latest_checkin` | Sau cập nhật, giá trị KR sẽ khác check-in mới nhất |
| `stale_export` | File xuất cũ hơn file đã áp dụng |

* **Fail-closed:** `--apply` chạy trước một lượt thử đầy đủ với cùng quy tắc; có xung đột → dừng, không ghi gì
  (kể cả tệp). Lượt ghi kiểm tra lại; xung đột phát hiện ở bất kỳ đâu → rollback toàn bộ transaction và xoá tệp đã ghi.
* **Không có `--force`.** Xử lý xung đột thủ công cần phương án riêng được phê duyệt.
* Không phải xung đột: bình luận/bằng chứng/Objective mới tạo trên Django khi WordPress không đổi dòng cha (được giữ nguyên);
  thông báo đã đọc trên Django giữ trạng thái đã đọc; vai trò trưởng phòng do Django gán giữ nguyên (`kept_existing`).
* Bảng ánh xạ danh tính chỉ cập nhật khi quyết định thay đổi (chạy lại = `unchanged`).
* `rollback`: trả lại giá trị cũ cho dòng batch đã **cập nhật** (từ `stats.before`), xoá dòng đã tạo; từ chối nếu
  còn batch mới hơn của cùng công ty, hoặc Django đã sửa dòng của batch, hoặc dữ liệu mới phụ thuộc — khi đó chỉ còn
  khôi phục backup CSDL + tệp bằng chứng. Batch tạo trước hotfix không có ảnh chụp: cập nhật không được hoàn tác.

### Đối soát (báo cáo JSON `--report`)

Số dòng nguồn vs đã nhập theo bảng; KR có tiến độ khác công thức (giữ số WordPress, liệt kê để duyệt);
Objective có tiến độ khác bình quân trọng số KR; KR có giá trị hiện tại khác check-in cuối;
người dùng/phòng ban chưa ánh xạ; người dùng đã xoá; ứng viên admin; bản ghi mồ côi; số bản ghi Django gốc giữ nguyên.

## Quy trình

**M2 (local, xong):** công cụ + test trên SQLite với dữ liệu giả cùng schema.

**M3 — diễn tập (cần phê duyệt + file thật):**
1. Quản trị viên WordPress chạy `joyokr_export.php` (chỉ đọc) và sao chép `wp-content/uploads` (chỉ phần tệp bằng chứng).
   File xuất chứa email/tên nhân viên: lưu mã hoá, không đưa vào Git.
2. Khôi phục bản sao PostgreSQL production vào máy cô lập; chạy `okr_inventory`.
3. `propose` → người quản trị duyệt file ánh xạ.
4. `import` (chạy thử) → xử lý mọi lỗi → `import --apply` → đối soát → `import --apply` lần 2 (phải 0 tạo mới)
   → `rollback` → khôi phục → chạy Web và Flutter trên cùng backend diễn tập.

**M4 — cutover (cần phê duyệt thời điểm):** backup PostgreSQL + WordPress + uploads và **thử khôi phục**;
đóng ghi WordPress (chế độ chỉ đọc); xuất lần cuối; `import --apply`; đối soát; mở Django cho người dùng.

**Rollback:** `okr_import_wp rollback --batch N --apply` khi chưa có dữ liệu mới; nếu đã có, khôi phục backup
PostgreSQL (và mở lại ghi WordPress).
