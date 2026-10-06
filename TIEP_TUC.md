# TIẾP TỤC — Phong_DHue

**Phiên bản hiện tại: v23.6.14** (06/10/2026)

## Trạng thái: ĐANG CHẠY ỔN

## Việc vừa xong (v23.6.14) — BỎ HOST CHẾT + VÁ 401/403

**Nguyên nhân gốc lỗi 403 `blogbio_verify_failed` (đã chốt bằng thực nghiệm):**
Tool có **host chết `usjusticereport.cfx.bz`** làm mặc định. Host này CHẾT THẬT:
- `GET /` → 200 (site tĩnh còn sống) NHƯNG mọi endpoint `/api/*` → **403 `blogbio_verify_failed` / `verify_status:502`** với MỌI token (kể cả token đúng).
- Cùng token đó bắn vào `dramanest.gigglelo.com` → **200**.
→ Máy nào chạy với config trống/mặc định sẽ trỏ vào host chết → 403 toàn bộ.

**Cách ly bằng chứng:** cùng 1 token: `dramanest.gigglelo.com` GET 200 / POST 422; `usjusticereport.cfx.bz` GET 403 / POST 403. Token rác cũng ra 403 y hệt ⇒ lỗi này là **host/tầng verify**, nhìn 403 KHÔNG phân biệt được token sai hay host chết.

### Đã vá
1. **Bỏ hẳn host chết** (12 chỗ) → `dramanest.gigglelo.com`. Chỉ dùng host điền trong tool.
2. **Vá 401/403:** token bị từ chối → **LƯU payload** để đăng lại (trước đây 42 bài 403 **mất trắng**), kèm thông báo rõ nguyên nhân. An toàn: bài 403 đã kiểm chứng KHÔNG hề được tạo (404) → đăng lại không sinh trùng.

### Việc còn lại cho máy lỗi
- Bấm "⬆ Cập nhật", hoặc sửa tab Adsconex: base URL = `https://dramanest.gigglelo.com/api`, site host = `dramanest.gigglelo.com`.
- Bấm **"Kiểm tra token"** (phải OK) → **"Đăng lại bài lỗi"**.

## Việc vừa xong (v23.6.14) — TỐI ƯU TỈ LỆ ĐĂNG ĐƯỢC BÀI

Bối cảnh: hôm 06/10 có khung giờ **73–88% bài bị 502** (12:20–17:15), 97 bài lỗi.
Nguyên nhân đã chốt: **Cloudflare `origin_bad_gateway`** trên zone `dramanest.gigglelo.com`
— origin quá tải **tức thời**, KHÔNG phải payload/tool/rate. Bằng chứng: 12/12 payload từng
502 replay lại → **201 OK 1.2–1.6s**; 10 POST nặng đồng thời → 201 cả 10; 107 bài 201 cùng ngày.

**Vấn đề thật sự làm "mất thời gian":** retry 3 lần × backoff 60/120s ≈ **4 phút/bài lỗi**.
100 bài lỗi = ~6 giờ chờ, mà origin sập hàng giờ thì chờ vài phút không cứu được bài nào.

### 1. Lưu payload khi lỗi → tự đăng lại sau (MỚI)
- Gặp `502/503/504/429/520–524` hoặc lỗi mạng → ghi `publish_pending_adsconex.json` vào
  `work_dir` (chứa payload + http_status + detail + saved_at + machine).
- **KHÔNG tốn Vision/Writer** — chỉ gửi lại đúng payload đã lưu.
- Đăng lại thành công → tự xoá dấu pending + tự ghi `publish_result.json` + `chapter_links.txt`
  + **tự đổi tên video** (đọc lại `story.json` + `source_video.json`).
- **An toàn, không sinh bài trùng:** 12/12 bài 502 trước đó đều trả `404` → 502 nghĩa là
  **chưa hề tạo bài**.

### 2. Tự đăng lại ở cuối batch (MỚI)
- Sau khi batch chạy xong, tool quét `Story Outputs`, đăng lại mọi bài còn pending.
- Cập nhật lại `rows` (bài lỗi → thành công) nên `batch_result_*.json` + `net_titles_*.txt`
  phản ánh đúng.
- **Dừng sớm:** 3 bài liên tiếp vẫn lỗi → dừng (origin chưa hồi), giữ nguyên payload cho lần sau.
- Bỏ qua folder **đã đăng thành công** (`publish_result.json` có url/id).

### 3. Nút "Đăng lại bài lỗi" trong tab Adsconex (MỚI)
- Chọn folder batch ở ô đường dẫn → bấm nút → xác nhận → chạy nền.
- Kiểm tra trước: net phải là Adsconex + phải có token; báo rõ số bài tìm thấy.
- Xong hiện hộp thoại: thành công bao nhiêu, còn lỗi bao nhiêu (bấm lại sau vài phút).

### 4. Circuit breaker — cắt cơn chờ vô ích (MỚI)
- **2 bài lỗi tạm thời LIÊN TIẾP** → các bài sau **chỉ thử 1 lần** rồi lưu payload luôn.
- Biến đếm là **trạng thái chung cả tiến trình** (`_adsconex_fail_streak` + Lock), KHÔNG phải
  thuộc tính instance — vì batch tạo `StoryPipeline` mới cho mỗi video, dùng instance sẽ bị reset.
- Thành công → streak về 0. Chỉ `502/503/504/429/520–524` + lỗi mạng mới tính là lỗi;
  **422/401/500 KHÔNG** kích breaker.

### 5. Retry 502 (từ v23.6.9, vẫn giữ)
- Retry `502/503/504/429/520–524` + lỗi mạng, backoff theo `Retry-After` (mặc định 60s,
  tối đa 3 lượt, trần 300s). `500`/`401` **KHÔNG** retry.

### 6. Các bản vá cũ vẫn giữ
- **Rate-limit 6 bài/phút tổng** cho mọi máy (mutex `O_CREAT|O_EXCL` trên Z) — v23.6.10.
- **Vá 422 `seo_description`** (chuỗi chứa `--`): làm sạch + gỡ trường rồi đăng lại — v23.6.10.

## Cấu trúc code (quan trọng khi sửa tiếp)

- `_adsconex_send(payload)` — tách ra thành method riêng: POST + retry + xin lượt + đếm breaker.
  **Trả `(r, data, payload)`, KHÔNG raise** — caller tự quyết định theo `r.status_code`
  (`r is None` = lỗi mạng sau khi hết lượt thử).
- `publish_adsconex()` và `republish_pending()` **dùng chung** `_adsconex_send`.
- `publish_adsconex` khi lỗi tạm thời → `adsconex_write_pending` rồi `raise RuntimeError`
  (để batch ghi nhận bài lỗi như cũ).

## Test đã chạy (PASS)

- **7/7 test DHue** (dùng response giả, KHÔNG tạo bài rác trên site thật):
  1. pending roundtrip (ghi/đọc/xoá) OK.
  2. đăng lại thành công 201 → có url, ghi `publish_result.json`, xoá pending, 1 POST.
  3. 502 rồi hồi → retry trong cùng bài → 201, streak về 0.
  4. origin sập: bài 1–2 = 2 call/6.6s → **bài 3–4 = 1 call/1.6s**, payload đều được lưu.
  5. folder **đã đăng thành công** bị bỏ qua khi quét pending.
  6. 422 `seo_description` → gỡ trường → 201; **422 không kích breaker**.
  7. lỗi mạng → lưu payload.
- **ADung v22.100.8:** đăng lại thật lên site → **HTTP 201, 10 chapters, 4.7s**;
  `GET /posts/<slug>` → **200**; 4/4 bài đăng lại thành công.
- `py_compile` OK cả 2; CRLF giữ nguyên (**6864 CRLF / 0 LF-only**).

## Lệnh hay dùng

```bash
PY="C:/Users/Admin/AppData/Local/Programs/Python/Python313/python.exe"
cd /c/Users/Admin/Desktop/Phong_DHue

# Phát hành bản mới (tự commit + push)
"$PY" release.py 23.6.X "mo ta"

# Sync ổ Z (đối chiếu MD5)
"$PY" sync_z.py

# Kiểm tra cú pháp sau khi sửa
"$PY" -m py_compile viet_drama_V23.6_dashboard_thumbnail.py
```

## Việc còn lại / lưu ý

- **Chưa test end-to-end POST thật** cho DHue v23.6.14 (test bằng response giả để không tạo
  bài rác). Lần chạy batch tới xem log có dòng `Adsconex xin luot dang:` không.
- **Mỗi máy phải dùng 1 folder riêng** — 2 máy cùng đăng 1 folder sẽ đăng trùng.
- Nếu Z không kết nối được, tool **chỉ giãn cách tại máy** (15s) → nhiều máy có thể vượt trần chung
  (hành vi có chủ đích, không chặn việc).
- Backup bản cũ nằm trong `_backup_old/` (đuôi `.bak`).
- **File này dùng CRLF** — patch phải giữ CRLF. Lưu ý: `Path.read_text()` tự dịch CRLF→LF nên
  **đếm bằng `read_text()` sẽ ra 0 CRLF sai**; phải dùng `io.open(..., newline="")`.

## Bản vá đã dùng

- `%LOCALAPPDATA%\Temp\ads502\patch_scripts\DHue\_patch502.py` (port từ ADung, 10 điểm).
- Test: `%LOCALAPPDATA%\Temp\ads502\test_dhue.py`.


## 2026-10-07 (moi) - Fix Mo_An.vbs
- LOI: 'Microsoft VBScript compilation error: Expected end of statement' tai dong 20
  khi double-click Mo_An.vbs.
- NGUYEN NHAN: VBScript KHONG co escape \" nhu C/JS. Muon 1 dau " trong chuoi phai
  viet "" (gap doi).
  SAI : sh.Run """ & bat & """ hidden", 0, False
  DUNG: sh.Run """" & bat & """", 0, False
- DA TEST: cscript //nologo -> exit 0, goi dung .bat (tao marker). Da push GitHub.
- Neu gap lai loi nay o may khac: chay updater.py hoac chep de Mo_An.vbs ban moi.


## 2026-10-07 (moi) - Port fix dang bai tu ADung sang DHue (v23.6.16)
- DHue truoc chi co fix Mo_An.vbs; phan dang bai van la ban CU (chi nhat 401/403,
  khong chia nho request, khong chong dang trung).
- Da port 7 thay doi tu ADung:
  1. adsconex_split_chunks()          - chia content theo moc '<p>CHAPTER N - ...</p>'
  2. _adsconex_payload_slug()         - lay slug tu permalink
  3. _adsconex_headers(cfg)           - header Chrome dung chung
  4. adsconex_series_id_for_slug()    - lay series_id de gop cac phan vao 1 series
  5. adsconex_existing_slugs()        - GET /series -> tranh dang trung
  6. StoryPipeline._adsconex_send_all - gui nhieu phan, phan 2+ kem series_id,
     phan loi -> tra payload GOC de luu lai dang lai
  7. find_pending_adsconex(output_root, cfg) - nhat ca 502/503/504/429/network
- publish_adsconex + republish_pending: doi self._adsconex_send -> _adsconex_send_all
- 2 cho goi find_pending_adsconex: truyen them cfg
- Sua them: nut 'Lay link site' (https://https://) va NameError lambda _update_fail
- DA TEST THAT (khong phai doc code): chia 3/6/12 chuong -> ghep lai KHOP content goc,
  moi phan <=36000, phan 2+ co series_id; loi phan 3/6 -> tra payload goc;
  find_pending nhat dung 502 cu + 403 cu, bo qua bai da dang/200/422/da-live.
- Backup ban cu: _backup_old/viet_drama_V23.6_dashboard_thumbnail.py.v23.6.15.bak
