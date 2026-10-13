# TIẾP TỤC — Phong_DHue

**Phiên bản hiện tại: v23.6.28** (cập nhật 2026-10-13)

## Trạng thái: ĐANG CHẠY ỔN
- File chính: `viet_drama_V23.6_dashboard_thumbnail.py` (**LF-only**, py_compile OK).
- GitHub + ổ Z đã đồng bộ (12/12 MD5 khớp).
- **KHÔNG được ghi đè config thật** — test GUI phải so MD5 trước/sau.

## v23.6.28 — SMARTTRAFFIC: LƯU PAYLOAD BÀI LỖI 5xx ĐỂ ĐĂNG LẠI

**Vấn đề:** SmartTraffic 500 kéo dài > 3,5 phút (đã thử lại 4 lần cách 20/40/60/80s vẫn 500).
Tool dừng đúng như thiết kế, nhưng **truyện bị mắc kẹt**:
- Nút **"Đăng lại bài lỗi"** chỉ quét Adsconex (`find_pending_adsconex`) → **không nhặt được** bài SmartTraffic
- Không có file pending nào được ghi cho SmartTraffic → payload chỉ nằm rải trong
  `publish_payload_attempt_*.json`, tool không đọc lại

**Đã thêm:**
- `SMARTTRAFFIC_PENDING_FILE = "publish_pending_smarttraffic.json"`
- `smarttraffic_write_pending()` — ghi payload khi hết lượt thử mà vẫn 5xx
- `smarttraffic_clear_pending()` — xoá khi đăng thành công
- `find_pending_smarttraffic()` — liệt kê bài chưa đăng được (bỏ qua bài đã có `publish_result.json` có url)
- `republish_pending_smarttraffic()` — đăng lại với cùng cơ chế thử lại 20/40/60/80s + đổi tên video

**Nút "Đăng lại bài lỗi" + bước đăng lại cuối batch nay chạy theo net đang chọn:**
Adsconex → `find_pending_adsconex`; SmartTraffic → `find_pending_smarttraffic`.

**Đã test 5 tình huống:**

| Tình huống | Kết quả |
|---|---|
| Chưa có pending | `[]` |
| Ghi pending (server 500) | nhặt được **đúng payload** |
| Bài đã đăng rồi | **không** nhặt nữa |
| Đăng lại thành công (500,500,201) | xoá pending + ghi `publish_result.json` |
| Server vẫn 500 | **giữ** pending để bấm lại sau |

GUI build OK, config MD5 **không đổi**.

## v23.6.27 — FIX LOGO YOUTUBE KHÔNG BAO GIỜ HIỆN (dù đã tích)

**Triệu chứng:** bật "Vẽ logo YouTube", lưu, chạy xong — ảnh bìa đăng lên **không có logo**.

**Nguyên nhân:** 3 key logo bị đặt trong `FIXED_PRODUCTION_SETTINGS`, mà
`apply_fixed_production_settings()` **GHI ĐÈ** config. Nên mọi lựa chọn đều bị ép về
`False` / `play` / `30`.

**Đã đo (bằng chứng):**
- Tích `on_cover=True`, `style=logo`, `scale=45` → sau `apply` vẫn là `False` / `play` / `30`
- `_youtube_badge_settings()` trả `('', 0.0)` = **TẮT**
- Ảnh bìa thật đã đăng: **0 pixel đỏ** → không có logo

**Cách sửa:** bỏ 3 key khỏi `FIXED_PRODUCTION_SETTINGS`, giữ ở `DEFAULT_CONFIG` làm mặc định.

**Đã test sau khi sửa:**
- Tích `on_cover=True` → giữ nguyên `True`; `style=logo` → giữ `logo`; `scale=45` → giữ `45`
- `_youtube_badge_settings()` → `('logo', 0.45)`
- Ảnh bìa có logo thật: **10.574 px nút đỏ** ở kích thước 45%; cả kiểu `play` lẫn `logo` đều vẽ được
- GUI: 3 widget logo hiện đúng, `current_cfg()` lưu được `True`/`logo`/`45`
- Không tích → vẫn TẮT như cũ
- Config MD5 **không đổi** trước/sau test GUI

**Logo áp cho CẢ HAI net** (không cần cấu hình riêng): SmartTraffic dùng `thumbnailUrl`,
Adsconex dùng `feature_image` — cả hai đều lấy từ `story["thumbnail_url"]` = **ảnh bìa đã vẽ logo**.
Chỉ cần tích 1 lần là cả hai net đều có.

**Kèm theo:** chuyển file về **LF-only** (lần merge trước làm file thành CRLF).

## v23.6.26 — FIX SmartTraffic HTTP 500 (lỗi TẠM THỜI, không phải lỗi nội dung)

**Triệu chứng:** đăng bài báo `SmartTraffic HTTP 500: {'success': False, 'status': 500, 'message': 'Internal Server Error'}`.
Tool gửi 2 lần (cách nhau **0,5s**) rồi dừng hẳn.

**Đã đo để tìm nguyên nhân:**
- Lấy **chính payload** trong thư mục output và gửi lại → **HTTP 201 trong 0,4 giây**.
- Payload tiếng Việt có dấu (60.870 ký tự, 10 chương, 9 `{{nextpage}}`) đăng **bình thường** —
  thử cả bản rút gọn và bản bỏ dấu, đều 201.
- Trong 30 lần đăng cũ: **24 thành công / 6 lỗi 500**, và cùng một thư mục có cả 500 lẫn 201.

→ **500 là lỗi tạm thời phía SmartTraffic**, KHÔNG phải do ngôn ngữ, độ dài hay ký tự có dấu.
Bản cũ chỉ gửi 2 lần cách nhau 0,5s nên một cơn lỗi tạm thời làm hỏng cả lượt chạy.

**Cách sửa:** gặp 5xx thì **gửi lại CHÍNH payload đó** (không đổi nội dung, không đổi slug) với
giãn cách tăng dần **20s → 40s → 60s → 80s** (tổng ~3,5 phút).
- `smarttraffic_retry_count` (mặc định `4`) và `smarttraffic_retry_delay` (mặc định `20`) nằm trong config,
  sửa được **không cần vào code**.
- Giữ nguyên slug nên nếu lần đầu server **đã lưu** bài rồi mới 500, lần sau sẽ là bài trùng chứ
  **không sinh `-2`/`-3`**.
- Hết lượt thử mà vẫn 5xx thì **dừng hẳn** — không thử "payload rút gọn" nữa, vì bản rút gọn dùng
  **cùng slug** nên dễ sinh bài trùng. Chờ vài phút rồi dùng nút đăng lại bài lỗi.

**Đã test:** giả lập server trả `500, 500, 201` → tool thử lại đúng 2 lần rồi **đăng thành công**.

## v23.6.25 — FIX LỖI "Caption chưa đạt giọng kể ngôi thứ nhất" KHI CHỌN NGÔN NGỮ KHÁC TIẾNG ANH

**Triệu chứng:** chọn ngôn ngữ `Vietnamese`, truyện viết xong 8/8 chapter (6.873 từ) nhưng caption
bị viết lại 2 lần rồi tool **dừng hẳn**:
`RuntimeError: Caption chưa đạt giọng kể ngôi thứ nhất; Blog.txt đã được lưu trong thư mục kết quả.`

**Nguyên nhân:** bộ kiểm tra caption **chỉ nhận đại từ tiếng Anh** — regex `\b(?:I|my|me|we|our)\b`
và đếm độ dài theo kiểu tiếng Anh. Caption tiếng Việt không có `I/my/me` → **luôn bị loại oan**,
dù caption viết đúng ngôi thứ nhất. Lỗi này có từ trước, chỉ lộ ra khi chọn ngôn ngữ khác.

**Cách sửa:**
- `FIRST_PERSON_MARKERS` — đại từ ngôi thứ nhất cho **đủ 16 ngôn ngữ**
  (`tôi/mình/em/con...`, `watashi/boku/ore...`, `je/moi/mon...`, `ich/mein...`, `yo/mi...` …).
- `NO_SPACE_LANGUAGES` — Nhật/Hàn/Trung/Thái **không dán cách bằng dấu cách**, nên đo độ dài bằng
  **ký tự** (200–4000) thay vì đếm "từ" (nếu không, caption tiếng Nhật 300 từ chỉ ~600 ký tự vẫn
  bị coi là quá ngắn).
- Câu cấm tiếng Anh (`screen went black`, `hard cut`...) **chỉ kiểm khi ngôn ngữ là tiếng Anh**.
- `CAPTION_REWRITE_NOTE` — câu nhắc viết lại cũng **dịch theo ngôn ngữ** (trước đây luôn nhắc
  bằng tiếng Anh, càng đẩy model về tiếng Anh).

**Prompt giữ nguyên nội dung, chỉ dịch vài câu nói về ngôn ngữ:** đo độ tương đồng **99,9%**;
16460 → 16474 ký tự (**chỉ +14**); 14 dòng thay đổi và **tất cả** đều là dòng nói về ngôn ngữ.

**Đã test:**
- Caption tiếng Việt **qua được** kiểm tra (11 đại từ ngôi thứ nhất, 9 đoạn) — trước đây bị loại.
- Caption tiếng Anh vẫn chạy y như cũ (17 đại từ).
- 16/16 ngôn ngữ có đủ markers + rewrite note + CTA.

## v23.6.24 — NGÔN NGỮ: BỎ HẲN CÂU GHIM TIẾNG ANH TRONG PROMPT

**Yêu cầu:** tích ngôn ngữ thì truyện chỉ theo **đúng một ngôn ngữ đã chọn**, mặc kệ video/kịch bản gốc là gì.

**Vấn đề của v23.6.23:** mới chỉ **thêm** chỉ thị ngôn ngữ, còn prompt gốc vẫn còn **6 câu ghim tiếng Anh**:

| Câu ghim cũ | Nằm ở |
|---|---|
| `Full Story và Caption viết bằng **English**.` | prompt chính |
| `Ưu tiên **natural American English**.` | prompt chính |
| `Tên tiếng Anh tự nhiên, dễ hiểu với người Mỹ...` | prompt chính |
| `...dành cho độc giả Mỹ/Âu...` | prompt chính |
| `Write the FULL BLOG STORY in natural American English` | addendum ngôi thứ nhất |
| `Use natural American English, about 180-300 words.` | prompt caption |

→ Model nhận **hai lệnh trái nhau** nên kết quả vẫn ra tiếng Anh.

**Cách sửa:** `strip_language_rules()` thay cả 6 câu bằng ngôn ngữ đã chọn **trước khi gửi model**.
Đã kiểm chứng: prompt gửi model **không còn câu ghim tiếng Anh nào**.

**Thêm `CAPTION_CTA`:** câu kết caption được dịch theo ngôn ngữ (đủ 16 ngôn ngữ) và **tự chuẩn hóa** —
nếu model trả về CTA tiếng Anh cũ thì tự bỏ và thay bằng CTA đúng ngôn ngữ (nếu không, caption sẽ
trộn ngôn ngữ: thân bài tiếng Nhật mà câu cuối tiếng Anh).

**KHÔNG tích = prompt y nguyên như cũ** — đã kiểm: `strip(prompt, "English")` giống hệt từng byte,
`language_instruction("English")` trả chuỗi rỗng. Không thể làm hỏng hành vi cũ.

**Đã test:**
- Tích `Japanese` → prompt không còn `American English` / `Mỹ/Âu` / `viết bằng **English**`; chỉ còn `Japanese`.
- Cả 16 ngôn ngữ đều có tên gọn (bỏ phần trong ngoặc) + CTA riêng.
- CTA chuẩn hóa đúng ở cả 3 tình huống (model trả CTA tiếng Anh cũ / không có CTA / CTA đúng sẵn).
- Config MD5 **không đổi** trước/sau test GUI.

## v23.6.23 — CHỌN NGÔN NGỮ CHO TRUYỆN (áp cho mọi câu chuyện + mọi chapter)

**Tab SmartTraffic → nút tích "Áp ngôn ngữ đã chọn cho mọi truyện" + ô chọn ngôn ngữ.**

16 ngôn ngữ: English, Vietnamese, Spanish, Portuguese, French, German, Italian, Indonesian,
Thai, Filipino, Hindi, Arabic, Turkish, Japanese, Korean, Chinese.

- **KHÔNG tích (mặc định)** → truyện viết **tiếng Anh** y như trước giờ. Prompt gửi model **y nguyên**,
  không thêm gì cả → không thể làm hỏng hành vi cũ.
- **Tích** → chèn chỉ thị ngôn ngữ vào **MỌI lời gọi Writer**, nên **tên truyện, meta title,
  meta description, keywords, TÊN từng chapter, NỘI DUNG từng chapter và caption Facebook** đều
  đúng ngôn ngữ đã chọn. Chỉ thị còn **ghi đè mọi câu lệnh "English / American English"** trong
  prompt gốc (kể cả khi kịch bản/video nguồn là tiếng Anh — phải dịch sang ngôn ngữ đích).
  Giữ nguyên tên riêng (tên nhân vật, địa danh).

**SỬA LỖI CHẶN QUAN TRỌNG:** trước đây `language` nằm trong `FIXED_PRODUCTION_SETTINGS`, mà
`apply_fixed_production_settings()` **ghi đè** config → nếu chỉ thêm ô chọn thì mọi lựa chọn đều
bị **âm thầm ép về `English`**. Đã bỏ ghim `language` khỏi khối đó.

**Đã test:**
- Không tích → prompt gửi model kết thúc đúng bằng prompt gốc, **không** chèn chỉ thị nào.
- Tích + Japanese → chỉ thị có mặt ở **cả 5 loại lời gọi** (Story architecture, batch chapter,
  metadata, Facebook caption, tiêu đề video); prompt gốc đặt **trước**, chỉ thị ngôn ngữ **sau**
  nên phần ghi đè thắng.
- Tích + English → không chèn gì (giữ nguyên hành vi cũ).
- GUI: bật/tắt làm ô chọn chuyển `disabled` ↔ `readonly` đúng; nhãn trạng thái đổi theo.
- Config MD5 **không đổi** trước/sau test GUI.

## v23.6.22 — THÊM LOGO YOUTUBE TRÊN ẢNH BÌA WEBSITE (port từ Phong_ADung)

Port **nguyên** từ Phong_ADung, đã đối chiếu **giống hệt từng byte** (6/6 khối: `_youtube_badge_font`,
`draw_youtube_badge`, `_youtube_badge_settings`, đoạn GUI `badge_box`, `preview_youtube_badge`,
và chỗ gọi trong `attach_local_images`).

**Tab Ảnh → khung "Logo YouTube trên ảnh bìa website":**
- Bật/tắt vẽ logo vào **CHÍNH GIỮA** ảnh bìa website 290×290.
- **Kiểu logo**: `play` = nút play đỏ (khuyến dùng, giống video chưa mở) / `logo` = thêm chữ "YouTube".
- **Kích thước**: % chiều rộng ảnh (kẹp 10–60%).
- Nút **"Xem trước logo"**: vẽ thử lên ảnh bìa của lần chạy gần nhất rồi mở file.

**Mặc định TẮT** — ảnh bìa giữ nguyên như trước, không ảnh hưởng luồng đăng bài.
**Ảnh bìa Facebook KHÔNG bị ảnh hưởng.**

**Đã test:** vẽ được cả 2 kiểu trên ảnh 290×290 (kiểm pixel: nút đỏ + tam giác trắng), có/không logo
khác nhau, GUI build OK (5 widget logo hiện đúng), config MD5 **không đổi** trước/sau test.

## v23.6.21 — VÁ "TOOL ĐỨNG RẤT LÂU / NHƯ TREO" Ở BƯỚC TRANSCRIPTION (port từ Phong_ADung)

**Triệu chứng:** log dừng ở `Transcribing 1/1: a.mp4`, đứng 2–3 phút, sau đó tiến trình
**CHẾT** (không có traceback Python). Nhìn như treo nhưng thực ra là crash native.

**Đo thật trên máy này** (Xeon E5-2673 v3, 48 logical CPU, video 30s, model small):

| | Thời gian |
|---|---|
| Bản cũ | **145–172s rồi chết** |
| Sau khi vá | **~20–37s** |

**BỐN nguyên nhân độc lập — đã vá hết:**

1. **Nạp model chờ mạng.** faster-whisper kiểm tra `huggingface.co` mỗi lần nạp model.
   Đo: **172s khi còn chờ mạng**, chỉ **3.4s khi `HF_HUB_OFFLINE=1`**.
   → Ép offline **ở ĐẦU file** và **trong `_WHISPER_CHILD`**. Bắt buộc phải trước khi
   import `faster_whisper`, vì `huggingface_hub` chỉ đọc biến này **lúc import**.

2. **Biến môi trường làm chậm 4–5 lần.** Bản cũ đặt `OMP/MKL/OPENBLAS_NUM_THREADS=2`
   (cả ở đầu file lẫn trong child). Đo: **không đặt gì → 28s**; `OPENBLAS=2` → **106s**;
   `OMP=2` → **145s**.
   → Bỏ hẳn cả ba. Điều khiển số thread bằng `cpu_threads` của `WhisperModel`
   (`max(4, min(12, cpu_total // 4))`).

3. **PyAV không khớp.** faster-whisper 1.2.1 gọi `av.open(..., metadata_errors=...)` nhưng
   PyAV 19 không nhận → `TypeError` mỗi lần transcribe đường dẫn.
   → **Luôn** xuất WAV 16k mono (`skip_audio_extract = False`), đọc bằng thư viện chuẩn
   `wave`, đưa **MẢNG numpy** cho `transcribe()` — không còn chạm tới PyAV.

4. **VAD làm segfault.** `vad_filter=True` nạp `onnxruntime`, mà `onnxruntime` trên máy này
   nổ `access violation` **ngay khi import**.
   → Tắt VAD (`vad_filter=False`).

**Thêm: khoá `ctranslate2==4.6.0` trong `ensure_packages()`.** Bản 4.6.1→4.8.2 **crash native**
khi nạp model (chỉ 4.6.0 chạy, ổn định 8/8 lần đo). `faster-whisper` chỉ đòi
`ctranslate2<5,>=4.0`, nên nếu nó được cài lại, pip sẽ kéo về **bản crash** → lỗi treo quay lại.
Nay tool tự kiểm tra và hạ về đúng 4.6.0 (kèm `setuptools<81` vì ct2 4.6.0 cần `pkg_resources`).

**Ghi chú kỹ thuật:** DHue dùng **tiến trình con cách ly** cho Whisper (`_WHISPER_CHILD`),
nên crash native chỉ làm hỏng 1 video thay vì sập cả tool — cơ chế này được giữ nguyên.
Bản vá chỉ sửa nội dung child (offline, bỏ biến thread, WAV+numpy, tắt VAD) và phần chuẩn bị
WAV ở tiến trình cha.

**Đã test:** `_WHISPER_CHILD` chạy độc lập → exit 0, 3 đoạn, không crash. Import module cha OK,
`HF_HUB_OFFLINE=1`, `OMP=None`, `OPENBLAS=None`.

**Cách kiểm tra nhanh khi nghi "treo":** mở Task Manager xem tiến trình `python` còn không.
Nếu tiến trình **biến mất** → crash native (xem `whisper_logs\`), không phải treo.

## v23.6.20 — FIX LỖI 502 KHI ĐĂNG BÀI LÊN ADSCONEX (nguyên nhân gốc đã đo được)

**Triệu chứng:** mọi bài 10 chương đều `Adsconex HTTP 502 origin_bad_gateway` sau ~12s, dù payload đúng, token đúng, host đúng.

**Nguyên nhân gốc (đo trên site thật, không suy đoán):** origin có **timeout xử lý ~12 giây**.
POST nào server cần >12s để tạo xong chương → Cloudflare trả 502. Yếu tố quyết định là
**số chương trong 1 request**, không phải số ký tự:

| Request | Kết quả đo được |
|---|---|
| 1–4 chương (mọi kích thước tới 75KB) | **201**, 4–12s |
| 5 chương | **502** |
| 10 chương | **502** (11/12 lần) |
| ≥20 chương | **502** với mọi token |

Số ký tự **không** phải giới hạn thật: 1 request 75KB/10 chương → 201 khi origin khoẻ;
75KB/4 chương → 201 liên tục. Nên bản vá cũ (chia theo 36.000 ký tự) chia 10 chương thành
2 phần 5 chương — **cả 2 phần vẫn >4 chương → vẫn 502**. Đó là lý do "chia nhỏ" cũ vô hiệu.

**Bằng chứng phụ:** token trong config (`Hue6789` / Viral Heat Drama) luôn 502 với ≥5 chương;
token của `admin` cùng payload → 201. Tầng verify xử lý token của user chậm hơn hẳn.
→ Vẫn nên đổi token admin nếu có, nhưng **không cần thiết** sau bản vá.

**Đã vá:**
1. `adsconex_split_chunks(content, max_chars, max_chapters)` — thêm giới hạn **số chương**
   (mặc định 3/request, ô "Số chương/lần gửi" trong tab Adsconex).
2. `adsconex_series_id_for_slug()` — đổi sang `GET /api/series/<slug>/posts` (tra CHÍNH XÁC,
   ~1.5s). Hàm cũ quét `GET /series` vốn bị **cắt ở 5000 dòng** (site có >10.800 series) →
   series mới không bao giờ thấy → phần 2+ tạo **series riêng, chương bị tách**.
3. `_adsconex_send_all(payload, resume)` — đăng TIẾP khi dở dang. `resume` tính theo
   **SỐ CHƯƠNG** (không phải số phần). Nếu đã đủ chương → **không đăng lại** (tránh series trùng).
4. `adsconex_write_pending(..., extra=...)` — lưu `series_id` + số chương đã lên; giữ lại
   tiến độ của bản ghi cũ khi ghi đè (trước đây bị mất → đăng lại từ đầu → trùng chương).
5. `find_pending_adsconex()` trả `(work_dir, payload, resume)`; dùng `adsconex_series_info()`
   để biết series đã có bao nhiêu chương thật trên site.
6. `adsconex_series_exists()` — kiểm tra chính xác theo slug, tránh đăng lại bài đã live.

**Đã test THẬT (không dùng response giả):**
- Truyện 10 chương / 75.000 ký tự → chia **4 request (3+3+3+1)** → **10/10 chương lên đúng 1 series**,
  đúng thứ tự chapter 1→10. `GET /api/series/<slug>/posts` → `total=10`.
- **Đăng dở dang rồi đăng tiếp** (test kỹ nhất, có 2 vòng sửa lỗi):
  đăng 4/10 chương → ghi pending → "Đăng lại bài lỗi" → **10/10 chương, giữ NGUYÊN series cũ,
  đúng thứ tự 1→10, KHÔNG trùng chương**.
  - Lỗi đã bắt được khi test: khi đăng tiếp, chunk ĐẦU TIÊN không mang `series_id` → server
    **tạo series mới** cho nó, các chunk sau mới gộp vào series cũ → site ra 7 chương và
    chương 5–7 mang **tên của chương 8–10**. Đã vá: khi đăng tiếp, **mọi** chunk (kể cả chunk 1)
    đều phải mang `series_id`; không lấy được thì DỪNG, không gửi.
- Bài đã đủ chương → không gửi gì, không tạo series trùng (đã kiểm tra lại trên site: vẫn 10 chương).
- 12 request liên tiếp 3 chương/request → **12/12 = 201** (0 lỗi).

## BẢN GẦN ĐÂY
| Bản | Nội dung |
|---|---|
| v23.6.28 | **SmartTraffic: lưu payload bài lỗi 5xx để đăng lại** (trước đây bị mất — nút "Đăng lại bài lỗi" chỉ quét Adsconex) |
| v23.6.27 | **Fix logo YouTube không bao giờ hiện** (3 key logo bị ghim trong FIXED_PRODUCTION_SETTINGS nên bị ghi đè; đã bỏ ghim — logo áp cho cả SmartTraffic và Adsconex) || v23.6.26 | **Fix SmartTraffic HTTP 500** (lỗi tạm thời phía server; thử lại chính payload đó với giãn cách 20/40/60/80s thay vì gửi 2 lần cách 0,5s) || v23.6.25 | **Fix caption khi chọn ngôn ngữ khác tiếng Anh** (bộ kiểm tra chỉ nhận đại từ tiếng Anh nên loại oan caption tiếng Việt; thêm markers + độ dài + câu nhắc viết lại cho đủ 16 ngôn ngữ) || v23.6.24 | **Ngôn ngữ: bỏ hẳn câu ghim tiếng Anh trong prompt** (tích ngôn ngữ = truyện chỉ theo đúng 1 ngôn ngữ, mặc kệ video gốc; thêm CTA dịch theo ngôn ngữ) || v23.6.23 | **Chọn ngôn ngữ cho truyện** (nút tích + 16 ngôn ngữ ở tab SmartTraffic; áp cho mọi truyện + mọi chapter; không tích = tiếng Anh như cũ) || v23.6.22 | **Thêm logo YouTube trên ảnh bìa website** (port nguyên từ Phong_ADung, giống hệt từng byte; mặc định TẮT) || v23.6.21 | **Vá treo Transcription**: ép HF offline + bỏ OMP/OPENBLAS_NUM_THREADS + tránh PyAV bằng WAV/numpy + tắt VAD; khoá ctranslate2==4.6.0 (145s -> ~20-37s) || v23.6.20 | **Fix 502 Adsconex**: chia theo SỐ CHƯƠNG (timeout origin ~12s) + tra series_id chính xác + đăng tiếp khi dở dang |
| v23.6.16 | Port phần đăng bài Adsconex sang bản DHue (theo đúng file anh gửi, KHÔNG sửa logic riêng) |
| v23.6.17 | **Fix TREO khi đăng bài do LỆCH ĐỒNG HỒ** (3 lớp: giờ máy, bỏ mark tương lai, chờ tối đa 25s) |
| v23.6.19 | **Vá crash PATH** — `configure_ffmpeg()` chỉ thêm ffmpeg vào PATH 1 lần (trước: mỗi video thêm 1 lần → nổ `ValueError` ở ~lần 454) |
| v23.6.18 | **Không lưu thư mục frames** — tự xoá ảnh trung gian; có ô tick "Giữ lại thư mục frames" |

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


## 2026-10-07 - FIX TOOL TREO khi dang bai (LECH DONG HO) - v22.100.13 / v23.6.17
Trieu chung: log dung ngay sau dong "Adscene phan 2/2 | 29139 ky tu", tool treo >7 phut
khong bao loi. KHONG phai loi mang/origin.

NGUYEN NHAN GOC: tran toc do dang dung chung tren o Z tinh gian cach bang mtime cua file
mark. Hai may lech dong ho (do that: 45 giay) -> mtime cua mark do may kia ghi nam o
TUONG LAI so voi dong ho may nay -> `since_last = now - max(inside)` AM -> wait_s luon lon
-> `_adsconex_take_shared_slot` lap `while True` mai mai -> TREO vinh vien.
Chung minh: goi thang `_adsconex_claim_slot` tren o Z that -> luon tra ("wait", ~50s).

DA VA (3 lop):
1. Gian cach tinh theo DONG HO CUA CHINH MAY (`_adsconex_local_last_post`), khong dung
   mtime cua mark (khong so sanh duoc giua cac may).
2. Bo qua mark co mtime o TUONG LAI khi dem cua so (khong keo ve now, khong xoa).
3. `_adsconex_take_shared_slot` gioi han cho toi da 25s (ADSCONEX_RATE_SLOT_WAIT_MAX)
   -> qua han thi tra "fallback" (gian cach tai may) thay vi treo vo han.

DA TEST THAT tren o Z (khong phai doc code):
- Truoc va: claim_slot luon ("wait", 40-55s), take_shared_slot treo vo han.
- Sau va: xin duoc luot trong 0.2-13s; mo phong 8 mark toan bo o tuong lai +45s -> xin
  luot trong 0.00s, khong treo.

Luu y moi truong: dong ho may em (Ken-Physical-Server) tung CHAM 45s; da bat w32time
(`sc config w32time start= auto` + `net start w32time` + `w32tm /resync`) -> lech +0.1s.
NEN dong bo dong ho tren MOI may chay tool.
