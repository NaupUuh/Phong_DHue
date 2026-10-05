# Phong_DHue - Video Story Publisher V23.6

Tool tự động: **Video -> Story -> SmartTraffic** (đăng bài hàng loạt lên website).
GUI Python/Tkinter + faster-whisper + Vilao API + Cloudinary.

## Cài đặt máy mới (1 lần)

1. Chạy `Cai_dat_may_moi.bat` -> tự tải tool từ GitHub và giải nén vào
   thư mục `Phong_DHue\`.
2. Mở tool bằng `Mo_An.vbs` (chạy ẩn, không hiện cửa sổ cmd).
3. Lần đầu mở, tool hiện **FirstRunWizard** để nhập API key + Site ID +
   Cloudinary cho máy đó. Nhập xong bấm Save Settings.

Không cần cài git. Không cần Python cài sẵn cho bước cài — tool tự cài thư
viện Python khi mở lần đầu (`ensure_packages`). Chỉ cần Python 3.x đã cài
trên máy (khuyến nghị 3.13) + ffmpeg.

## Cập nhật bản mới

Mở tool -> bấm nút **"⬆ Cập nhật"** (cạnh "Chẩn đoán máy").

Tool tự đọc `version.json` trên repo, so với bản đang chạy, tải bản mới và
tự mở lại. **Cài đặt + API key + bài đã làm KHÔNG bị mất.**

Cập nhật bằng dòng lệnh (không cần GUI):

```bat
python updater.py            :: chi kiem tra
python updater.py --apply    :: cap nhat luon
```

## Cấu hình & dữ liệu riêng (không nằm trong repo)

| Thứ | Đường dẫn |
|---|---|
| Config + API key | `%USERPROFILE%\.video_story_publisher_V23.6.json` |
| Cache (audio, whisper log) | `%LOCALAPPDATA%\VideoStoryPublisher\V23.6\` |
| Log lỗi | `video_story_publisher_crash_V23.6.log` (cạnh tool) |

`config.json` và mọi `.video_story_publisher*.json` **không bao giờ** được
đẩy lên GitHub và **không bao giờ** bị updater ghi đè.

## Phát hành bản mới (chỉ dành cho người build)

```bat
python release.py 23.7.0 "Mo ta ngan thay doi"
```

Script tự: kiểm version mới > cũ -> ghi `version.json` -> ghi `APP_VERSION`
-> commit -> push. Máy khác chỉ cần bấm "⬆ Cập nhật".

## Lỗi thường gặp

- **`CERTIFICATE_VERIFY_FAILED` khi cập nhật**: đã xử lý sẵn — `updater.py`
  thử lần lượt `cacert.pem` (đóng gói kèm) -> certifi -> kho chứng chỉ
  Windows -> mặc định hệ thống.
- **Không tìm thấy FFmpeg**: vào tab **FFmpeg**, chọn đúng thư mục BIN chứa
  `ffmpeg.exe`.
- **Thiếu thư viện Python**: tool tự cài khi mở. Nếu mạng chặn, cài tay:
  `python -m pip install requests pillow opencv-python openai faster-whisper json-repair cloudinary`

## Bản quyền

Dùng nội bộ. Repo public để máy khác cập nhật không cần token.
