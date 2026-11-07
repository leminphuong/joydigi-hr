# M3 — Hướng dẫn quản trị viên: lấy dữ liệu thật từ JOYOKR (okr.joydigi.net)

> Bản kỹ thuật cho phía phát triển. Bản rút gọn từng bước cho hai quản trị viên (hosting, HR) kèm checklist bàn giao
> nằm trong tài liệu "M3 — Bàn giao dữ liệu OKR thật" (Claude Docs). SHA-256 tệp gửi đi: `joyokr_export.php`
> 210c5e1f…b439ab, `joyokr_export_queries.sql` c0b4c6a4…7493f.

Dành cho người **có quyền quản trị** hosting của `okr.joydigi.net`, tài khoản Cloudflare của `joydigi.net`, và
máy chủ HR `checkin.joydigi.net`.

* Mọi thao tác trên production là **chỉ đọc**: không sửa WordPress, không sửa CSDL, không tạo thư mục trong site,
  không chạy migrate.
* **Không ai gửi mật khẩu cho ai.** Không dán mật khẩu vào lệnh hay chat; dùng `~/.pgpass`, `--defaults-extra-file`
  hoặc nhập khi chương trình hỏi. Không dùng chuỗi kết nối nằm trong tài liệu Git (một tài liệu deploy cũ đã lộ
  chuỗi kết nối CSDL HR — xem mục 9).
* Phía phát triển **không tự đăng nhập máy chủ thật**. Mọi lệnh dưới đây do quản trị viên chạy.

## 0. Những gì đã biết và còn thiếu (06/11/2026)

| Mục | Trạng thái |
|---|---|
| Site | WordPress (có `wp-json`), theme JOYOKR, sau **Cloudflare** (proxy) |
| Máy chủ gốc | Trả header `x-turbo-charged-by: LiteSpeed` → máy chủ **LiteSpeed**. VPS HR chạy aaPanel **Nginx** và tài liệu chỉ ghi hai site WordPress khác (modbibi.com, modpuro.com) → nhiều khả năng **hosting khác**. **Chưa xác minh.** |
| SSH / WP-CLI | Chưa biết. Máy phát triển không có cấu hình SSH nào tới máy chủ này. |
| Thư mục WordPress, tiền tố bảng, thư mục uploads | Chưa biết — xác định theo mục 1, **không đoán**. |
| Bảng JOYOKR | `<prefix>joyokr_*` (11 bảng) trong CSDL MySQL/MariaDB của site |
| Tệp bằng chứng | Thư viện media của WordPress (`wp-content/uploads/…`) — hiện **công khai theo URL** (lỗi đã biết của JOYOKR) |

## 1. Xác định vị trí WordPress (quản trị viên hosting)

1. **Cloudflare** → `joydigi.net` → DNS: bản ghi `okr` trỏ tới IP/hostname gốc nào → đó là máy chủ cần đăng nhập.
   Không đăng IP gốc lên kênh công khai.
2. **Bảng điều khiển hosting** của máy chủ đó (cPanel, DirectAdmin, CyberPanel, hPanel…): mục *Domains / Websites*
   cho biết **thư mục gốc** (document root) của `okr.joydigi.net`. Trong thư mục đó phải có `wp-config.php`.
3. **Tiền tố bảng**: dòng `$table_prefix` trong `wp-config.php` (chỉ đọc), hoặc tên bảng trong phpMyAdmin
   (vd. `wp_joyokr_objectives` → tiền tố `wp_`). Phải thấy đủ 11 bảng `…joyokr_departments … …joyokr_activity_log`.
4. **Có SSH + WP-CLI không?** Trong SSH: `wp --info`. Có → làm **Cách A**. Không → **Cách B**.
5. **Thư mục uploads**: mặc định `<thư-mục-gốc>/wp-content/uploads`; Cách A tự báo (`source.uploads_basedir`).

Với Cách A, xác nhận đúng site trước khi xuất (không chạy plugin/theme):

```bash
cd <thư-mục-gốc>
wp --skip-plugins --skip-themes option get siteurl          # https://okr.joydigi.net
wp --skip-plugins --skip-themes option get stylesheet       # joyokr
wp --skip-plugins --skip-themes db query "SHOW TABLES LIKE '%joyokr\_%'"   # 11 dòng
```

Chạy `wp` bằng đúng người dùng sở hữu site (trên LiteSpeed/cPanel là chính tài khoản hosting), không dùng `root`.

## 2A. Xuất bằng SSH + WP-CLI (ưu tiên)

```bash
umask 077
mkdir -p ~/m3_export && cd ~/m3_export            # NGOÀI thư mục gốc của site
# tải joyokr_export.php (từ repo, okr/legacy_import/) vào ~/m3_export rồi:
wp --path=<thư-mục-gốc> --skip-plugins --skip-themes eval-file ~/m3_export/joyokr_export.php \
   > joyokr-export-$(date +%Y%m%d).json 2> export-summary.txt
cat export-summary.txt
```

Script chỉ chạy `SHOW TABLES` / `SELECT`; không đọc `user_pass`, token, số điện thoại, Telegram, SSO; không tạo thư mục
(`wp_upload_dir(null, false)`); lỗi PHP đi ra STDERR, không lẫn vào JSON. `export-summary.txt` ghi: số bảng tìm thấy
(phải 11/11), **số dòng từng bảng**, số tệp bằng chứng và số tệp mất, các **liên kết hỏng** (`BROKEN …`).

Kiểm tra tệp:

```bash
F=joyokr-export-*.json
jq -r '.format, .format_version' $F          # joyokr-export / 1
jq '.summary' $F                             # cùng nội dung export-summary.txt
grep -c user_pass $F                         # 0
```

(Hosting không có `jq`: dùng `php -r` như hai lệnh dưới — PHP luôn có trên máy chủ WordPress.)

Tệp bằng chứng — chỉ các tệp được tham chiếu, giữ nguyên thư mục con:

```bash
UPLOADS=$(php -r '$d=json_decode(file_get_contents("joyokr-export.json"),true); echo $d["source"]["uploads_basedir"];')
php -r '$d=json_decode(file_get_contents("joyokr-export.json"),true); foreach($d["attachments"] as $a) if($a["relative_path"]!=="") echo $a["relative_path"],"
";' > evidence-files.txt
tar -czf joyokr-evidence-$(date +%Y%m%d).tgz -C "$UPLOADS" -T evidence-files.txt 2> missing-evidence.txt
wc -l < evidence-files.txt; tar -tzf joyokr-evidence-*.tgz | wc -l      # hai số phải bằng nhau
```

## 2B. Không có SSH/WP-CLI: phpMyAdmin (hoặc Adminer) + File Manager

1. Mở `okr/legacy_import/joyokr_export_queries.sql`. Thay **mọi** `wp_` bằng tiền tố thật (kể cả `wp_capabilities`).
2. Trong phpMyAdmin chọn CSDL của site → tab **SQL** → dán **một** câu `SELECT` → *Go* → ở kết quả chọn
   **Export** → định dạng **CSV**, mã hoá UTF-8, phân cách `,`, bao `"`, NULL ghi `NULL`, **tích "Put columns names in
   the first row"** → lưu đúng tên tệp ghi trên câu lệnh (`departments.csv`, …, `options.csv`, `evidence-files.csv`).
   Có 16 câu → 16 tệp. Không bấm Export cả bảng `wp_users`/`wp_usermeta` (chứa hash mật khẩu, token phiên).
3. **File Manager** của hosting: tải về đúng các tệp liệt kê trong `evidence-files.csv`, **giữ nguyên thư mục con**
   tính từ `wp-content/uploads/` (vd. `2026/10/bao-cao.png`). Có thể chọn rồi *Compress* thành một tệp zip trong thư
   mục riêng ngoài site, tải về, rồi xoá tệp zip đó trên hosting. Không tự tạo đường link chia sẻ công khai.
4. Ghi lại **giờ UTC** lúc chạy các câu lệnh (dùng cho `--exported-at`).
5. Phía phát triển ghép lại thành tệp xuất chuẩn (tự tính dung lượng, SHA-256 từng tệp):

```
manage.py okr_import_wp assemble --from-dir <thư-mục-csv> --uploads-dir <thư-mục-tệp> \
    --exported-at 2026-11-07T08:00:00Z --table-prefix <tiền-tố> --out joyokr-export.json
```

## 3. Bản sao PostgreSQL HR (máy chủ checkin.joydigi.net)

Mật khẩu lấy từ `~/.pgpass` (quyền `600`) hoặc nhập khi hỏi — không đặt trong lệnh, không lấy từ tài liệu Git:

```bash
umask 077
psql -h localhost -U <role> -d joydigi_hr -Atc 'select version()'           # ghi vào phiếu bàn giao
pg_dump -Fc -h localhost -U <role> -d joydigi_hr -f ~/m3_export/joydigi_hr-$(date +%Y%m%d).dump
pg_restore --list ~/m3_export/joydigi_hr-*.dump | grep -c -E 'TABLE DATA public (base_company|base_department|base_department_company_id|employee_employee|employee_employeeworkinformation|joydigi_auth_joydigiuser|joydigi_auth_joydigiuser_groups|auth_group) '
#   -> 8
pg_restore --list ~/m3_export/joydigi_hr-*.dump | grep -c 'TABLE DATA public okr_'
#   -> 16
```

Bản dump đầy đủ (`-Fc`, không `--exclude`) giữ nguyên ID công ty, phòng ban, nhân viên, tài khoản, nhóm quyền và các
bảng OKR hiện có — đó là các ID mà bảng ánh xạ dùng. `pg_dump` chỉ đọc; không dừng dịch vụ, không chạy migrate.

## 4. Checksum, mã hoá, chuyển giao

Danh sách tệp cần giao:

| Tệp | Cách A | Cách B |
|---|---|---|
| Dữ liệu OKR | `joyokr-export-YYYYMMDD.json` + `export-summary.txt` | 16 tệp `.csv` |
| Bằng chứng | `joyokr-evidence-YYYYMMDD.tgz` + `missing-evidence.txt` | zip các tệp trong `evidence-files.csv` |
| HR | `joydigi_hr-YYYYMMDD.dump` | như Cách A |
| Kèm | `SHA256SUMS`, phiếu bàn giao (mục 6) | như Cách A |

```bash
cd ~/m3_export && sha256sum * > SHA256SUMS
# Mã hoá trên MÁY TÍNH sau khi tải về (hosting thường không có 7z): 7-Zip → Add to archive → 7z,
# AES-256, tích "Encrypt file names". Trên Linux có 7z: 7z a -t7z -mhe=on -p m3-$(date +%Y%m%d).7z *
```

Trên Windows (máy thao tác Cách B), tạo checksum: `Get-FileHash -Algorithm SHA256 <tệp>`.

* Mật khẩu giải nén gửi **kênh khác** kênh gửi tệp, cho đúng một người nhận.
* Không gửi qua GitHub, email công khai, nhóm chat, ổ chia sẻ chung. Không đặt trong thư mục repo.
* Bên nhận giải nén vào `C:\AppCheckin2\m3_input\` và kiểm tra: `sha256sum -c SHA256SUMS` (Git Bash). Sai → gửi lại.
* Xoá `~/m3_export` trên máy chủ sau khi bên nhận xác nhận; xoá bản ở máy diễn tập khi M4 kết thúc.

## 5. Bảng xác minh tài khoản (`review.csv`)

Phía phát triển tạo bảng sau khi khôi phục bản sao HR vào PostgreSQL cô lập:
`manage.py okr_import_wp propose joyokr-export.json --company <id> --out review.csv`.

Người có thẩm quyền (HR) điền **từng dòng**: `target_id` (ID nhân viên/phòng ban HR, đúng công ty), `status`
(`verified` / `rejected`), `notes` (căn cứ, người xác minh, ngày).

* `matched_by = email` (chỉ trùng email) **phải kiểm tra tay**; không xác minh chỉ vì trùng email.
* Một nhân viên chỉ `verified` cho **một** tài khoản WordPress. Không tạo nhân viên/phòng ban mới để ghép.
* **Owner của Objective/KR bắt buộc `verified`.** `rejected` hoặc chưa quyết định cho owner là ERROR (chặn import).
  Owner đã nghỉ việc: cần quyết định riêng (mục 7a).
* Không cấp Admin OKR qua bảng này. Hai tài khoản tham chiếu của dự án phải có dòng `verified` riêng.
* Lưu dạng **CSV UTF-8**, không đổi tên cột.

## 6. Phiếu bàn giao

Người xuất; thời điểm xuất (UTC); cách A hay B; máy chủ/thư mục gốc/tiền tố bảng đã dùng; phiên bản theme JOYOKR;
số dòng 11 bảng; số người dùng; số tệp bằng chứng và số tệp mất; số liên kết hỏng (Cách A: `export-summary.txt`);
phiên bản PostgreSQL production; các giá trị SHA-256; WordPress có còn nhận dữ liệu sau thời điểm xuất không.

## 7. Phía phát triển làm khi nhận đủ (PostgreSQL cô lập 127.0.0.1, không production)

1. Kiểm tra SHA-256; (Cách B) `assemble`.
2. **Inventory + inspect**: `okr_import_wp inspect EXPORT --uploads-dir … --report inspect.json` → số dòng, liên kết,
   tệp, và **kiểm tra nguồn** ERROR/WARNING/INFO (`wp_migration.md`).
3. Khôi phục bản dump HR (cùng phiên bản chính) → `okr_inventory` (trước).
4. `propose` → `review.csv` → **chờ HR duyệt** (mục 5).
5. Chạy thử `import` (dry-run) → đối soát; ERROR phải được xử lý (7a) trước khi đi tiếp.
6. Đạt → `import --apply` trên **môi trường diễn tập**; đối soát; nhập lần 2 (0 tạo, 0 sửa); provenance; quyền;
   tệp bằng chứng; Web + App qua API; `pg_dump` + `private_media/okr` → rollback → khôi phục.
7. Báo cáo M3. Production không thay đổi.

### 7a. Khi kiểm tra nguồn báo ERROR

Công cụ **không tự sửa dữ liệu nguồn, không tự bỏ bản ghi, không có cờ force**. Mỗi ERROR có mã, bảng, ID WordPress.
Người có thẩm quyền chọn: sửa trên JOYOKR rồi **xuất lại** (cần phê duyệt riêng vì là WordPress production), hoặc phê
duyệt một cơ chế loại khỏi phạm vi (chưa có, cần quyết định trước).

## 8. Bằng chứng sau khi chuyển sang Django

Tệp được lưu vào kho riêng `private_media/okr` trên máy chủ HR (Nginx chỉ phục vụ `/media/`; `private_media/` nằm
trong `.gitignore`). Chỉ tải được qua `/api/okr/evidence/<id>/download/` sau khi kiểm tra quyền xem (người công ty
khác nhận 404). Không đăng lại tệp hay đường link bằng chứng ở nơi công khai. Mỗi lần backup HR phải gồm cả
`private_media/okr`.

## 9. An toàn

* Đổi mật khẩu CSDL HR đang lộ trong tài liệu deploy cũ trong Git trước M4.
* Theme JOYOKR có tệp xuất "mật khẩu mặc định" chứa mật khẩu dạng chữ (đã báo trong audit 03/11): không dùng chức năng
  đó; nên đổi mật khẩu các tài khoản liên quan.
* Các gói dữ liệu chứa thông tin cá nhân: chỉ người được phân công giữ; ghi lại ai nhận, khi nào; xoá sau M4.
