# TIẾP TỤC — Phong_DHue

**Cập nhật lần cuối:** 2026-10-20
**Phiên bản hiện tại:** **v23.6.36** (worktree + GitHub + ổ Z — tất cả khớp)
**File chính:** `viet_drama_V23.6_dashboard_thumbnail.py` (**LF-only**, single-file)

---

## ✅ VỪA XONG (v23.6.36 — fix Story Architecture bị cắt vì hết token)

**Triệu chứng:** hộp thoại đỏ
`Story architecture failed after 3 attempts: Story architecture: API response did not contain usable choices/message/content.`
Kèm `Response preview` có `"finish_reason": "length"` và `content` chỉ là đoạn suy nghĩ
(`We need to produce JSON with story architecture...`), không có JSON.

**Nguyên nhân:** model **reasoning** viết phần suy nghĩ trước, ăn hết `max_tokens=4000`
trước khi kịp ra JSON. Tool cũ **thử lại y nguyên `max_tokens` cũ** → lặp lại y hệt → hỏng cả 3 lần.

**Đã sửa trong `_writer_json_call`:**
1. Thêm `chat_finish_reason(response)` — đọc `finish_reason` từ cả SDK object lẫn dict.
2. Khi bị cắt → **tự động tăng gấp đôi** `max_tokens` (4000 → 8000 → 16000), không thử lại y nguyên.
3. Phát hiện content **không có JSON** → báo lỗi rõ ràng thay vì "không có choices".
4. Từ lần thử lại, thêm chỉ thị `Output ONLY the raw JSON object... Start your reply with { and end with }`.
5. Trần trên cùng qua key mới `writer_max_tokens_cap` (mặc định 16000) — để trong `DEFAULT_CONFIG`,
   **KHÔNG ghim** trong `FIXED_PRODUCTION_SETTINGS` (bài học: ghim là user không đổi được).

**Đã test:**
- Mô phỏng **đúng lỗi trong ảnh** → cứu được bài ở lần 3 với `max_tokens=16000` ✓
- Dừng đúng ở trần, không tăng vô hạn ✓
- Trường hợp bình thường (`finish_reason=stop`) vẫn gọi **1 lần**, giữ nguyên `max_tokens` ✓
- `py_compile` OK

**Lưu ý:** bản này làm trên nền **v23.6.35** (file trong thư mục đã lên .35 từ 15/10, git local còn .32).

---

## ✅ ĐÃ XONG (v23.6.35 — phần ngôn ngữ giống ADung)

Yêu cầu: *"phần ngôn ngữ sửa lại giống ADung cho a"*

**7 điểm đã sửa:**

1. **`_write_chapter_batch`** — thêm `+ language_instruction(effective_language(self.cfg))` vào cuối prompt.
   → **Đây là lỗ hổng lớn nhất**: trước đây bật ngôn ngữ vẫn viết chapter bằng tiếng Anh.
2. **`_write_one_chapter`** (chapter cứu hộ) — thêm `+ language_instruction(...)` tương tự.
3. **`_story_metadata`** — `Create metadata for an English serialized drama article.` → `lang_name = language_prompt_name(effective_language(self.cfg))` + `...written in {lang_name}.`
4. **GUI** — chuyển khối chọn ngôn ngữ từ **tab SmartTraffic** → **mục "1. Đầu vào"** (`LabelFrame(source, ...)`), nhãn `Ngôn ngữ truyện (áp cho mọi web/net đã chọn)`. Đúng như ADung: bật 1 lần áp cho **mọi net** (SmartTraffic / Adsconex / Slink).
5. **`_refresh_language_state`** — chép nguyên văn ADung (docstring tiếng Việt có dấu, hint `"Đang áp: X"`, note 3 dòng đầy đủ).
6. **`write_facebook_caption`** — bỏ 6 dòng code chết (`opening`, `verb_hit`, `plain`) mà ADung không có.
7. **Typo** `HIER WEITERSCHAuen` → `HIER WEITERSCHAUEN` (tiếng Đức).

**Đã verify:**
- **9/9 hàm ngôn ngữ** giống hệt ADung (AST segment compare)
- **12/12 hằng số ngôn ngữ** giống hệt
- Test thật GUI: khối nằm trong "1. Đầu vào", combo 15 ngôn ngữ, hint/note đúng, `language_enable_check` đã bỏ
- Test thật logic: `_write_chapter_batch` / `_write_one_chapter` / `_story_metadata` đều nhận `language_instruction`; khi TẮT thì không thêm gì (giữ nguyên hành vi cũ)
- `py_compile` OK

**Lưu ý cho anh:** lỗi #1+#2 nghĩa là **từ trước tới nay bật ngôn ngữ vẫn ra chapter tiếng Anh** ở DHue. Giờ mới đúng.

---

## ⚠️ CÒN TREO

1. **Slink chưa có `active_publish_link()` riêng** → chạy lại cùng folder có thể đăng trùng. *Anh có muốn vá luôn không?*
2. **2 bài cũ hôm nay vẫn gắn nhãn `en`** — có sửa lại qua API không?
3. **File Temp tải thử** — có dọn không?
4. **35 series test rác** trên Adsconex (API không có route xóa).
5. **Máy Admin vẫn 502.**
6. Câu hỏi cũ: có gộp option `single` không; có đổi tool sang xuất 1200x630 không.

---

## 🔧 QUY ƯỚC BẮT BUỘC

- **KHÔNG BAO GIỜ in giá trị credential** (token Adsconex/dramanest/Telegram/fableon, Gemini, Vilao, Pexels, ELEVENLABS, mật khẩu site fableon) — chỉ nêu TÊN.
- **"giữ đúng file anh gửi, bản 23.6 là chạy kiểu khác"** — không sửa logic riêng DHue khi port.
- **Test API chỉ dùng GET hoặc POST payload THIẾU field bắt buộc** (POST đủ field TẠO BÀI THẬT).
- File chính là **CRLF**; đọc/ghi `newline=""`, ghi lại bằng `bytes` + `.replace("\n","\r\n")`.
- Backup **phải lấy từ `git show HEAD:<file>`**; tên backup KHÔNG lặp `.py`.
- Phần đọc video (Whisper/ffmpeg/OpenCV/Vision) **giữ nguyên byte-for-byte** (máy Xeon).
- Trước khi rời tool: ghi TIEP_TUC.md → commit → push → sync Z.
- Config thật `~/.video_story_publisher_V23.6.json` **KHÔNG được ghi đè**.

---

## 📁 ĐƯỜNG DẪN

- Worktree: `C:/Users/Admin/Desktop/Phong_DHue`
- GitHub: https://github.com/NaupUuh/Phong_DHue (v23.6.35, commit `935785b`)
- Ổ Z: `Z:/HQData-2/TOOLS TỔNG HỢP/TOOLS UPDATE CUỐI/Phong_DHue/`
- Config: `~/.video_story_publisher_V23.6.json`
- Backup: `_backup_old/`
- Đối chiếu chuẩn: `C:/Users/Admin/Desktop/Phong_ADung/viet_drama_V22.99_stable_folder.py` (v22.100.22)

---

## 🧩 TRẠNG THÁI TOOL

- **3 net:** SmartTraffic / Adsconex / Slink (combobox + tab riêng)
- **8 tab:** Kịch bản & Prompt, Vilao AI, SmartTraffic, Adsconex, Slink, Image hosting, FFmpeg, Hướng dẫn
- **Logo badge:** `play` (mặc định) / `logo` / `white` (asset PNG thật)
- **Ngôn ngữ:** mặc định **TẮT** (`language_enabled: False`) — bật thì áp cho **mọi net** + mọi chapter + metadata + caption + tên file
- **Chapter gen mode:** `batch` (mặc định) / `half`
- Slink: `https://fableon.treeiq.biz/api`, site_id `724`
