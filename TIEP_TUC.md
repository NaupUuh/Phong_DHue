# TIẾP TỤC — Phong_DHue

**Phiên bản hiện tại: v23.6.10** (06/10/2026)

## Trạng thái: ĐANG CHẠY ỔN

## Việc vừa xong (v23.6.10)

### 1. Giới hạn tốc độ đăng Adsconex dùng chung nhiều máy (MỚI)
- **6 bài/phút TỔNG** cho mọi máy (≈60 chương/phút) — dưới trần 6–10 bài của Adsconex.
- Cơ chế: **mutex trên ổ Z** (`Z:\HQData-2\TOOLS TỔNG HỢP\_adsconex_rate\`), tạo file bằng
  `O_CREAT|O_EXCL` (atomic — đã stress 40 vòng × 6 luồng: 40/40 đúng 1 máy thắng).
- **Cửa sổ trượt 60s** + **giãn cách tối thiểu 60/N giây** (10s khi N=6) → không dồn cục (burst).
- **Chỉ dùng metadata** (tạo file + liệt kê thư mục), KHÔNG đọc nội dung file — vì RaiDrive
  **cache nội dung file** (đọc lại mất 0.000s → thiết kế đọc file trạng thái không đáng tin).
- Lỗi khi file khoá đã tồn tại trên RaiDrive là **`PermissionError` (Errno 13)**, KHÔNG phải
  `FileExistsError` → code phải bắt cả hai.
- Z lỗi/không có Z → **tự giãn cách tại máy** (`adsconex_rate_fallback_delay`, mặc định 15s).
- Cấu hình trong GUI tab **Adsconex**, khung "Giới hạn tốc độ đăng (dùng chung cho MỌI máy)":
  - `Số bài mỗi phút (tổng)` — mặc định **6**
  - `File trạng thái chung` — để trống = dùng đường dẫn mặc định trên Z
  - `Giãn cách khi Z lỗi (giây)` — mặc định **15**

### 2. Vá lỗi HTTP 422 `seo_description` (MỚI)
- **Nguyên nhân đã chứng minh:** quét 168 payload trong `Story Outputs` → **đúng 1 bài có `--`
  (double hyphen) trong `seo_description`, và đúng bài đó bị 422**; 167 bài còn lại không có `--`
  → đăng hết. Server Adsconex từ chối chuỗi chứa `--`.
- **Cách vá (2 lớp):**
  1. `clean_meta_text()` — làm sạch `seo_description` trước khi gửi: thay `--` → `-`, bỏ ký tự
     điều khiển, gộp khoảng trắng, cắt theo giới hạn.
  2. Nếu vẫn bị 422 đúng vào `seo_description` → **gỡ trường đó rồi đăng lại** (bài vẫn lên,
     chỉ thiếu mô tả SEO) thay vì mất cả bài.
- **KHÔNG gỡ** `seo_description` khi 422 vì trường khác (tránh mất dữ liệu vô cớ).

### 3. Retry 502 (từ v23.6.9, vẫn giữ)
- Tự động retry `502/503/504/429/520–524` + lỗi mạng, backoff theo `Retry-After` (mặc định 60s,
  tối đa 3 lượt, trần 300s). `500` và `401` **KHÔNG** retry (tránh bài trùng / vô ích).
- Đã chạy thật trên máy khác: log hiện `Adsconex HTTP 502 (loi tam thoi) | thu lai 2/3 sau 60s`.

## Lệnh hay dùng

```bash
PY="C:/Users/Admin/AppData/Local/Programs/Python/Python313/python.exe"
cd /c/Users/Admin/Desktop/Phong_DHue

# Phát hành bản mới (tự commit)
"$PY" release.py 23.6.X "mo ta"

# Push GitHub + sync ổ Z (12 file, đối chiếu MD5)
git push
"$PY" sync_z.py

# Kiểm tra cú pháp sau khi sửa
"$PY" -m py_compile viet_drama_V23.6_dashboard_thumbnail.py
```

## Test đã chạy (PASS)

- `t_rate2.py` (12 tiến trình tranh lượt trên Z thật): **12 bài giãn đều 10.6s**, cửa sổ trượt
  60s nhiều nhất **6** (đúng trần), 12 bài trong 121.8s = **5.9 bài/phút**.
- `t_seo422.py`: **7/7 PASS cả 2 file** (làm sạch `--`, bỏ ký tự điều khiển, không đổi chuỗi
  bình thường, cắt đúng max_len, đúng thứ tự xử lý 422, mô phỏng 422→gỡ→đăng lại OK, không gỡ
  nhầm khi 422 vì trường khác).
- `gui_smoke2.py`: **PASS cả 2** (khung + 3 ô cấu hình + 3 key config + gọi xin lượt trước POST).

## Việc còn lại / lưu ý

- **Chưa test end-to-end POST thật lên Adsconex** cho bản này (chỉ mô phỏng + test trên Z).
  Lần chạy batch tới nên xem log có dòng `Adsconex xin luot dang:` không.
- **Mỗi máy phải dùng 1 folder riêng** (user đã chốt) — nếu 2 máy cùng đăng 1 folder sẽ đăng trùng.
- Nếu Z không kết nối được, tool vẫn chạy nhưng **chỉ giãn cách tại máy** (15s) → nhiều máy có
  thể vượt trần chung. Đây là hành vi có chủ đích (fallback, không chặn việc).
- Backup bản cũ nằm trong `_backup_old/` (đuôi `.bak`).
- File này dùng **CRLF**.

## Bản vá đã dùng (trong `%LOCALAPPDATA%\Temp\facetest\`)

`patch_ads_rate.py` → `patch_ads_rate2.py` → `patch_ads_rate3.py` → `patch_ads_rate4.py` →
`patch_ads_rate5.py` → `patch_ads_rate6.py` → `fix_dup_waits.py` → `patch_seo422.py` →
`fix_flag_pos.py`
