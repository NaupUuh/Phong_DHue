# TIẾP TỤC — Phong_DHue (Video Story Publisher VV23.6)

> Đọc file này trước khi sửa tool. Cập nhật lại mỗi khi rời tool.

## Trạng thái hiện tại (2026-10-05)
- **Version đang chạy:** v23.6.3
- **Repo GitHub:** https://github.com/NaupUuh/Phong_DHue
- **Thư mục máy:** `Phong_DHue`
- **Cách chạy:** bấm đúp `CHAY_Phong_DHue.bat` (hoặc `viet_drama_V23.6_dashboard_thumbnail.py`)

## Đã làm xong
- Tách khỏi tool kia: config / crash log / cache riêng, không ghi đè nhau.
- `PROFILE_ID = "V23.6"` ở đầu file — đổi 1 dòng này là tách sang profile khác.
- Đã đưa lên GitHub + cơ chế tự cập nhật (giống News_Clip_Stitcher).
- Nút **⬆ Cập nhật** ở góc trên bên phải cửa sổ: bấm là tự tải bản mới → ghi đè → mở lại.
- `Cai_dat_may_moi.bat` cho máy mới (đã test thật: cài đủ 11 file).
- **Ô "Câu chèn trong tên file"** trong tab Cài đặt: mỗi dòng 1 câu, tool chọn
  ngẫu nhiên 1 câu thay chuỗi `full story` khi đặt tên file. Để trống → quay về
  `full story` như bản cũ.

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

## Lưu ý
- API key (Vilao / ElevenLabs / Gemini) **chỉ nhập trong ô GUI**, KHÔNG đọc từ `.env`, KHÔNG hardcode trong file.
- Config **không** bị cập nhật ghi đè — `updater.py` chỉ ghi đè file thuộc repo.
- Máy mới: chạy `Cai_dat_may_moi.bat` trước, sau đó chỉ cần bấm nút Cập nhật.
- Tool dùng chung `site_id` / API key với tool kia (do copy config) nhưng KHÔNG ghi đè nhau.
