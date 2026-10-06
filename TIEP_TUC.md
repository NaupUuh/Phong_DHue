# TIẾP TỤC — Phong_DHue (Video Story Publisher VV23.6)

> Đọc file này trước khi sửa tool. Cập nhật lại mỗi khi rời tool.

## Trạng thái hiện tại (2026-10-06)
- **Version đang chạy:** v23.6.9
- **Repo GitHub:** https://github.com/NaupUuh/Phong_DHue
- **Thư mục máy:** `Phong_DHue`
- **Cách chạy:** bấm đúp `CHAY_Phong_DHue.bat` (hoặc `viet_drama_V23.6_dashboard_thumbnail.py`)
- **ĐÃ CÓ Adsconex** (port từ ADung, v23.6.6): tab Adsconex + ô "Net đăng bài"
  ở vùng Source (SmartTraffic mặc định / Adsconex).

## Đã làm xong
- Tách khỏi tool kia: config / crash log / cache riêng, không ghi đè nhau.
- `PROFILE_ID = "V23.6"` ở đầu file — đổi 1 dòng này là tách sang profile khác.
- Đã đưa lên GitHub + cơ chế tự cập nhật (giống News_Clip_Stitcher).
- Nút **⬆ Cập nhật** ở góc trên bên phải cửa sổ: bấm là tự tải bản mới → ghi đè → mở lại.
- `Cai_dat_may_moi.bat` cho máy mới (đã test thật: cài đủ 11 file).
- **Ô "Câu chèn trong tên file"** trong tab Cài đặt: mỗi dòng 1 câu, tool chọn
  ngẫu nhiên 1 câu thay chuỗi `full story` khi đặt tên file. Để trống → quay về
  `full story` như bản cũ.
- **Adsconex tu dong retry loi tam thoi (v23.6.9):** `publish_adsconex()` truoc day
  POST 1 lan roi bao loi -> 502 Cloudflare `origin_bad_gateway` lam MAT luot dang
  (do that 06/10: 35/124 bai bi 502 = 28%). Nay retry 502/503/504/429/520-524 +
  backoff theo `Retry-After` (mac dinh 60s, toi da 3 luot, tran 300s/luot).
  Cau hinh: `adsconex_retry_count` (3), `adsconex_retry_delay` (60) trong config.
  **500 va 401 KHONG retry** (co the da tao bai / loi that -> tranh bai trung).
  Log ro tung luot: `Adsconex HTTP 502 (loi tam thoi) | thu lai 2/3 sau 60s | ...`

## Tên file video (định dạng)
```
<caption>;<câu chèn ngẫu nhiên> <link bài không dấu chấm/slash>.mp4
```
Ví dụ thật (đã chạy đúng trên máy):
```
She Whispered Come On, Sasha At 2 AM - Then Her Dog Led Her To The Nursery Door
#SashaTheDog #NurseryNight #DogHero #TwoAM #MotherAndDog;WATCH THE NEXT PART HERE
httpsdrama.viralstory.bizarticle100989.mp4
```
- Giới hạn **219 ký tự** (có từ bản gốc, để an toàn với Windows + ổ mạng).
- Tên quá dài → cắt bớt tiêu đề. Muốn nới thì sửa hằng số giới hạn trong
  `safe_video_filename()` — **cần user quyết** (219 an toàn / 240 đủ ví dụ dài).

## Cơ chế tự cập nhật (đã test thật, PASS)
| File | Việc |
|---|---|
| `version.json` | version + notes + date (nguồn so sánh) |
| `updater.py` | tải zip GitHub → giải nén → ghi đè → backup `_backup_update/` |
| `release.py` | phát hành bản mới: `python release.py <ver> "<mô tả>"` |
| `_backup_update/` | backup tự động trước mỗi lần ghi đè (có thể rollback) |

**Khi có bản mới, máy khác chỉ cần:** mở tool → bấm **⬆ Cập nhật**.

### Bẫy đã gặp: cache version.json của GitHub
`contents?ref=main` và `raw.githubusercontent` đều có thể trả bản CŨ vài giây
sau khi push → bấm Cập nhật ngay sẽ báo "đang là bản mới nhất" (SAI).
**Đã sửa:** updater hỏi commit mới nhất có đụng `version.json`, rồi đọc
`version.json` **ở đúng commit đó** (`?ref=<sha>`) — nội dung theo sha là bất
biến, không dính cache. Chỉ khi cách này lỗi mới rơi về 2 cách cũ.

### Phát hành bản mới (làm trên máy này)
```
cd Phong_DHue
python release.py 23.7.0 "Mô tả thay đổi"
```
Script tự: kiểm tra version lớn hơn → ghi `version.json` → ghi `APP_VERSION` → commit → push.

## Đường dẫn tài nguyên (đã tách, KHÔNG dùng chung với tool kia)
- Config: `C:\Users\<user>\.video_story_publisher_V23.6.json`
- Crash log: `Phong_DHue\video_story_publisher_crash_V23.6.log`
- Cache: `%LOCALAPPDATA%\VideoStoryPublisher\V23.6\`

## Việc còn lại
- **ĐÃ QUYẾT (2026-10-05): GIỮ giới hạn 219 ký tự** — user chọn an toàn với Windows
  + ổ mạng, chấp nhận việc tool cắt bớt tiêu đề khi quá dài. **KHÔNG nới lên 240.**
  Đừng đề xuất lại trừ khi user hỏi.
- **ĐÃ QUYẾT (2026-10-05): KHÔNG mã hoá / không che code.** User đã cân nhắc và
  bỏ qua. Đã test thật: PyArmor trial **KHÔNG** làm nổi file 266 KB (`out of
  license`, giới hạn ~32 KB); `.pyc` dễ dịch ngược; đóng gói `.exe` sẽ **làm chết
  nút Cập nhật** (updater ghi file `.py` rồi chạy lại `sys.executable + .py`).
  Repo vẫn để **public** như hiện tại. **Đừng đề xuất lại** trừ khi user hỏi.
- **Chưa rõ nguyên nhân:** file config ADung từng bị ghi đè thành 3 khoá
  (`vilao_api_key` giả `KEY_THAT_CUA_ANH`, `site_id`, `category`) lúc 10:43.
  Config đã tự khôi phục đủ 58 khoá + key thật. Không phải do test của mình.
  Nếu gặp lại: kiểm tra ngay file config trước khi chạy tool.

## Bằng chứng tool chạy ĐÚNG (lần chạy thật 11:44–11:50 ngày 05/10)
Output: `Z:\HQData-2\dũng dùng drama\AQ test\test\Story Outputs\`
- 2/3 video: `status: success`, bài **đã lên net thật** (HTTP 200):
  - `/article/100989` (slug `you-saved-us-the-dog-that-knew`)
  - `/article/100990` (slug `my-son-came-home-black-sedan-fifty-years`)
- Cả 2 file video **đã được đổi tên đúng định dạng** trong folder nguồn.
- 1/3 video: `failed` do server đăng bài trả **502/504 Bad Gateway** (lỗi phía
  máy chủ, KHÔNG phải lỗi tool). Gặp lại thì chạy lại video đó.
- Lưu ý: `chapter_verified: false` trong `publish_result.json` — cần xem lại nếu
  user phàn nàn bài không phân chương.
- Crash log chỉ có 2 dòng `open_output` FileNotFoundError (bấm "Mở output" khi
  thư mục trên ổ Z đã bị xoá) — không ảnh hưởng pipeline.

## Lệnh hay dùng
```
# chạy tool
Phong_DHue\CHAY_Phong_DHue.bat

# kiểm tra có bản mới không (không mở GUI)
python updater.py

# cập nhật bằng dòng lệnh (không cần mở GUI)
python updater.py --apply

# phát hành bản mới
python release.py 23.7.0 "Mô tả"

# đồng bộ lên ổ Z
python sync_z.py
```

### Fix 2026-10-06 (v23.6.8) — CRASH SO BAN TU MODEL ('could not convert string to float')
- **Trieu chung user gap:** dialog loi `could not convert string to float: '11.11'` tai `analyze_with_vision`.
- **ROOT CAUSE:** `float('11.11')` khong the loi -> chuoi that chua **dau cham FULLWIDTH U+FF0E**, nhin y het dau cham thuong. Model Vilao thinh thoang tra so kem ky tu Unicode.
- **FIX:** them `safe_float()` / `safe_int()` o module level: chuan hoa **NFKC** (fullwidth -> ASCII) + bo NBSP/zero-width + doi minus U+2212 + regex `_NUM_RE` cuu ca chuoi lan van ban ('11.11s - 15.20s'). Ap cho **MOI** field so tu model: `time_start/time_end` (batch Vision), `events` dang dict (chuan hoa tai cho), sort event, `_chapter_source_payload` (number/start/end), `nums`, `transcript_slice_for_time`, prompt f-string, `_write_one_chapter` setdefault, heading HTML dang web.
- **Bang chung:** chay lai DUNG code path cu -> ban CU crash y nguyen thong bao user gap, ban MOI chay sach (`start=11.11 end=15.20` deu la float). Test 16 dang so ban (fullwidth, '11.11s', '11,11', '~11.11', range, zero-width, None, dict, list) deu PASS.
- **File:** them ~18 cho `safe_float` + 7 cho `safe_int` moi file. CRLF giu nguyen (LF tran=0), compile OK.
- **Khong doi:** GUI, config, logic nghiep vu. E2E van PASS (12/12 khung co mat, thumb 290x290, cover 760x400).
- **Bai hoc chi tiet:** skill `grok-batch-video-tools` -> `references/dirty-numeric-parse.md`

### Fix 2026-10-06 (v23.6.7) — ANH BIA: THAY DETECTOR MAT (OpenCV 5.x) + UU TIEN CAM XUC
- **Yeu cau:** anh bia phai la anh CO NGUOI/nhan vat, NET nhat co the, cam xuc/drama cang cao cang tot.
- **ROOT CAUSE (quan trong nhat):** OpenCV **5.0.0** da **XOA HAN** `cv2.CascadeClassifier`
  va `cv2.data.haarcascades` -> `FACE_CASCADE = None` -> **tool MUA MAT hoan toan**; logic
  "uu tien anh co nguoi" bi vo hieu, chi con chon theo do net thuan -> anh bia hay ra canh
  KHONG CO NGUOI. (Khong phai loi cua ban cu — do OpenCV nang cap.)
- **FIX:** thay Haar cascade bang **YuNet `cv2.FaceDetectorYN`** (`face_detection_yunet_2023mar.onnx`,
  232 KB) + **model cam xuc** `facial_expression_recognition_mobilefacenet_2022july_int8bq.onnx`.
  - Model **tu tai runtime** ve `~/.video_story_publisher_models` (KHONG commit vao repo);
    URL chinh `media.githubusercontent.com/media/opencv/opencv_zoo/...`, URL du phong raw.
    (Luu y: `raw.githubusercontent` tra ve 131 byte = LFS pointer -> phai dung `media.`.)
  - **Offline fallback**: khong tai duoc model -> in canh bao, quay ve cham do net, KHONG crash.
  - **Thread-local detector** (`_FACE_TLS`) vi `extract_frames` chay ThreadPool.
- **Bang diem moi (thu tu uu tien ro rang):** CO NGUOI > DO NET > CAM XUC/DRAMA.
  `FACE_GATE=3600` > `SHARP_WEIGHT(3300)+DRAMA_WEIGHT(320)` => **moi khung co mat LUON thang
  khung khong mat**. Trong nhom co mat: mat to + o giua + drama cao thang.
  (Ban va v1 tung nhan `score *= 0.35` khi mo -> nhan ca phan thuong "co nguoi" -> khung mo
  co mat thua khung net khong mat. Da sua o v2: TACH thuong co nguoi ra khoi phat do net.)
- **Nhan cam xuc** (thu tu model tra ve): `angry, disgust, fearful, happy, neutral, sad, surprised`.
  Tien xu ly = **align 5 landmark STD + normalize**. `EMOTION_DRAMA`: angry/fearful 1.0,
  sad 0.92, surprised 0.82, disgust 0.68, happy 0.55, neutral 0.12.
- **Cat vuong 290x290 theo KHUON MAT** (`make_social_thumbnail`): mat ~45% tu tren xuong ->
  khong cat tran. Cover web 760x400 (`make_story_cover`) giu nguyen.
- **Hieu nang (do that, 84 khung/video):** `max_width=480` -> +2.1s/video so voi ban cu
  (trump 8.9->11.2s, Debt 8.4->10.3s). Chi rieng buoc quet khung; cac buoc khac (Whisper/TTS/render)
  ton hang phut nen khong dang ke. **Da thu `max_width=384`: nhanh hon 30% NHUNG lech khung
  chon o 1/4 video -> KHONG dung** (uutien chat luong anh bia). Giu 480.
- **E2E PASS (ca 2 tool):** 12/12 khung co mat, `find_person_thumbnail_frame` tim thay,
  thumbnail web 290x290 + cover 760x400 dung kich thuoc.
- **Backup ban cu:** `_backup_old/*.py.bak` (git + sync Z + updater deu bo qua nho `*.bak`).
- **Bump:** `APP_VERSION` 23.6.6 -> **23.6.7**. (ADung: 22.100.3 -> 22.100.4.)

### Fix 2026-10-06 (v23.6.6) — PORT TAB/NET ADSCONEX TU ADUNG SANG DHUE
- **Yeu cau:** ADung (V22.99) co tab + net Adsconex, DHue chua co -> port sang.
- **Nguyen tac:** port NGUYEN KHOI Adsconex, KHONG dung logic rieng cua DHue
  (Whisper, `find_person_thumbnail_frame()`, `_choose_source_path()`).
  `publish_smarttraffic` doi chieu bak: **giong 100%** (11462 ky tu, khong doi 1 ky tu).
- **Them vao DHue (24 patch):**
  - `DEFAULT_CONFIG` +7 khoa: `net_provider` (=SmartTraffic), `adsconex_api_key`,
    `adsconex_base_url`, `adsconex_site_host`, `adsconex_category` (=15),
    `adsconex_author`, `adsconex_apply_image_to_all`.
  - Helper: `published_adsconex_link()`, `build_adsconex_chapter_content()`,
    `active_publish_link()` (MOI — chon link theo net, DHue dung chung cho batch).
  - Pipeline: `_adsconex_headers()`, `publish_adsconex()`, `publish_current()`
    (route SmartTraffic/Adsconex) — dat truoc `publish_smarttraffic`.
  - GUI: tab **Adsconex** (token + Edit Key + base/site/author/category +
    "Lấy danh sách" + "Kiểm tra token" + "Mở trang API docs" + "Lấy link site"),
    o chon **"Net đăng bài"** o vung Source, `nb.bind` height 320 cho tab ads.
  - Batch/rename: `find_saved_publications(..., net_provider, site_host,
    adsconex_site_host)`; moi cho lay link doi sang `active_publish_link()`;
    `rename_published_video()` route theo net; `chapter_links_text` uu tien
    `chapter_links_text` tu response (Adsconex = 1 bai rieng moi chapter).
  - Queue: `adsconex_test`, `adsconex_categories` + `show_adsconex_categories()`.
  - GUIDE_TEXT muc 10: giai thich o "Net đăng bài".
- **Dac thu Adsconex:** `POST /api/posts` `mode="chapter"` -> server tu tach chuong
  theo marker `CHAPTER N - Title` + tu tao series; link `/blog/<slug>` (LAY TU RESPONSE,
  khong doan); `category` la so ID; header Chrome de qua Cloudflare.
- **Test THAT (khong doan):**
  - `smoke_adsconex_dhue.py`: import OK, 7 khoa config, link `/blog/<slug>`,
    content 2 marker, `active_publish_link` ca 2 net, route `publish_current`
    (SmartTraffic/Adsconex/mac dinh), **7 tab co Adsconex**, 3 panel
    (prompt/script/cau chen), widget + 4 method, combobox Net -> **TAT CA PASS**.
  - `test_publish_adsconex_dhue.py` (mock HTTP): payload dung (mode/permalink/
    category int/author/feature_image/apply_image_to_all), 2 file json ghi ra
    work_dir, `chapter_links_text` "Chapter 1/2: .../blog/...", HTTP 403 ->
    raise, thieu token -> raise, thieu posts -> raise, thumbnail http (khong
    https) -> bo qua feature_image, thieu marker -> chan TRUOC khi POST -> **PASS**.
  - GUI chay bang **config THAT** cua user: khong doi config (MD5 y nguyen),
    `net_provider` mac dinh SmartTraffic, category 15.
- **Bump:** `APP_VERSION` 23.6.5 -> **23.6.6**.

### Fix 2026-10-05 (v23.6.5) — GOM O "CAU CHEN" VAO TAB KICH BAN & PROMPT
- **Yeu cau:** o "Cau chen trong ten file" nam rieng o vung Source lam trang cao. Gom xuong tab
  "Kich ban & Prompt" thanh **3 o canh nhau**.
- **Cach lam:** xoa label+editor khoi `source`; them `phrase_panel` (LabelFrame "Câu chèn trong
  tên file") vao `prompt_tab` column=2 + `columnconfigure(2, weight=1, uniform="editors")`.
  `script_panel` padx `(5,0)` -> `(5,5)`. Editor height 6, width 32.
- **Giu nguyen bien:** `self.filename_phrase_editor`; save `cfg["filename_phrase_list"]` khong doi.
- **Chieu cao:** notebook prompt_tab = 190, panel h=180, reqh ~160 => khong bi cat (do bang `probe_h.py`).
- **Test:** `smoke_3panel_dhue.py` — 3 panel cot 0/1/2, mapped, save roundtrip dung.
- **Luu y CRLF:** file CRLF; `patch` khoi dai hay truot — cat nho tung khoi.

### Fix 2026-10-05 (v23.6.4) — CON LAN CHUOT
- **Trieu chung:** lan chuot o vung nen (ngoai cac o nhap) KHONG cuon trang; chi cuon duoc khi
  tro nam trong o Text/Listbox. Phai keo thanh truot tay moi xuong duoc.
- **Nguyen nhan:** `tk.Canvas` KHONG tu an su kien `<MouseWheel>` (khac `Text`/`Listbox`).
  Trang chi cuon duoc neu tro tinh co nam tren widget co scroll rieng -> vung nen chet.
- **FIX (3 phan):**
  1. Luu canvas: `self.content_canvas = canvas` (dong ~4059).
  2. `self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")` (dong ~4075).
  3. Them `_on_mousewheel()` + `_scroll_content()` (~dong 5248):
     - `winfo_containing()` -> di nguoc chuoi `master`; gap `Text`/`Listbox` -> `return None`
       (nhuong quyen cho o do tu cuon).
     - Chi cuon khi diem nam TRONG `content_canvas` (chan `bind_all` cuon nham khi popup mo).
     - `winfo_containing()` tra `None` -> fallback so sanh toa do voi khung canvas.
     - Het scroll (`first<=0 and last>=1`) -> `return None`, khong an su kien vo ich.
- **BAI HOC:** moi `tk.Canvas` lam khung cuon deu PHAI bind `<MouseWheel>` thu cong;
  dung `bind_all` + kiem tra widget duoi con tro de khong giat quyen cua `Text`/`Listbox`.
- Da test: ngoai o nhap (xuong+len) OK, trong Text nhuong quyen OK,
  trong popup khong cuon canvas chinh OK, khong crash log.
- Da release v23.6.4 + sync Z 12/12 MD5 khop.

## Lưu ý
- API key (Vilao / ElevenLabs / Gemini) **chỉ nhập trong ô GUI**, KHÔNG đọc từ `.env`, KHÔNG hardcode trong file.
- Config **không** bị cập nhật ghi đè — `updater.py` chỉ ghi đè file thuộc repo.
- Máy mới: chạy `Cai_dat_may_moi.bat` trước, sau đó chỉ cần bấm nút Cập nhật.
- Tool dùng chung `site_id` / API key với tool kia (do copy config) nhưng KHÔNG ghi đè nhau.
