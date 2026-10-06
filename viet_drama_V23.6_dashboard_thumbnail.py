# -*- coding: utf-8 -*-
"""
Video -> Story -> SmartTraffic
--------------------------------
Features
- Select one video or a folder of videos.
- Extract representative frames with ffmpeg/OpenCV.
- Transcribe audio locally with faster-whisper.
- Send transcript + frames to a Vilao OpenAI-compatible API.
- Build a chaptered English story.
- Pick one local frame for thumbnail + one frame per chapter.
- Export HTML/JSON locally.
- Optionally publish the article to SmartTraffic.
- Optionally upload images to Cloudinary before publishing.

IMPORTANT
1) Vilao model IDs/provider availability can differ by subscription.
   Keep the model fields editable and use the exact API model ID shown in your Vilao subscription.
2) SmartTraffic needs PUBLIC image URLs for thumbnail/chapter images.
   This script supports Cloudinary as the image host. If Cloudinary is not configured,
   the story can still be generated/exported but image URLs cannot be published correctly.
3) Install ffmpeg and make sure ffmpeg.exe is in PATH.
"""

import os
import errno

# Limit native CPU library thread pools before importing NumPy/OpenCV/Whisper.
# Each video already has parallel work; unbounded native threads can exhaust RAM.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")

# V20: hide the black console window on Windows when the tool is opened normally.
# The GUI remains visible. If you need debugging, run with --show-console.
def _hide_console_window():
    if os.name != "nt" or "--show-console" in sys.argv:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            SW_HIDE = 0
            user32.ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass

import re
import sys
import platform
import struct

import json
import time
import base64
import shutil
import queue
import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import webbrowser
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------
# Auto-install Python packages
# ---------------------------
REQUIRED = {
    "requests": "requests",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "openai": "openai",
    "faster_whisper": "faster-whisper",
    "json_repair": "json-repair",
}
OPTIONAL = {
    "cloudinary": "cloudinary",
}

def ensure_packages():
    """
    Auto-install all Python packages required by this tool.
    Uses the same Python interpreter that is currently running the app.
    """
    import importlib
    import subprocess
    import sys
    import time

    required = [
        # pip_name, import_name
        ("requests", "requests"),
        ("opencv-python", "cv2"),
        ("numpy", "numpy"),
        ("Pillow", "PIL"),
        ("openai", "openai"),
        ("faster-whisper", "faster_whisper"),
        ("json-repair", "json_repair"),
        ("cloudinary", "cloudinary"),
    ]

    optional = [
        # Helpful for some environments / faster media handling.
        ("av", "av"),
    ]

    def missing(import_name):
        try:
            importlib.import_module(import_name)
            return False
        except Exception:
            return True

    def pip_install(pip_name):
        print(f"[SETUP] Installing: {pip_name}")
        cmd = [
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check",
            "--no-input",
            "--upgrade",
            pip_name
        ]
        subprocess.check_call(cmd)

    # Ensure pip itself is available.
    try:
        import pip  # noqa
    except Exception:
        print("[SETUP] pip not found. Running ensurepip...")
        subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])

    install_errors = []

    for pip_name, import_name in required:
        if missing(import_name):
            try:
                pip_install(pip_name)
            except Exception as e:
                install_errors.append((pip_name, str(e)))

    # Optional dependencies should never stop startup.
    for pip_name, import_name in optional:
        if missing(import_name):
            try:
                pip_install(pip_name)
            except Exception as e:
                print(f"[SETUP] Optional package failed: {pip_name} | {e}")

    # Verify required imports again.
    still_missing = []
    for pip_name, import_name in required:
        if missing(import_name):
            still_missing.append(pip_name)

    if still_missing:
        details = "\n".join(f"- {name}" for name in still_missing)
        if install_errors:
            details += "\n\nInstall errors:\n" + "\n".join(
                f"- {name}: {err}" for name, err in install_errors
            )
        raise RuntimeError(
            "Không thể tự cài một số thư viện bắt buộc:\n" + details +
            "\n\nHãy kiểm tra Internet, quyền cài đặt Python hoặc antivirus/firewall."
        )

    print("[SETUP] All required Python packages are ready.")

ensure_packages()

import requests
import cv2
import numpy as np
from PIL import Image, ImageTk
from openai import OpenAI
from faster_whisper import WhisperModel
from json_repair import repair_json

_WHISPER_LOCK = threading.RLock()
_WHISPER_CACHE = {}  # Shared within this app process; prevents one model per video worker.
_WHISPER_CHILD = r'''
import json, os, sys
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
from faster_whisper import WhisperModel
source, model_name, device, compute, beam, output = sys.argv[1:]
model = WhisperModel(model_name, device=device, compute_type=compute, cpu_threads=2, num_workers=1)
segments, info = model.transcribe(source, vad_filter=True, beam_size=int(beam),
                                  word_timestamps=False, condition_on_previous_text=False)
data = [{"start": float(s.start), "end": float(s.end), "text": s.text} for s in segments]
with open(output, "w", encoding="utf-8") as handle:
    json.dump(data, handle, ensure_ascii=False)
'''

try:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_AVAILABLE = True
except Exception:
    CLOUDINARY_AVAILABLE = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


# ---------------------------------------------------------------------------
# PROFILE RIENG CHO TUNG TOOL
# Tool nay nam trong thu muc rieng (Phong_ADung / Phong_DHue). De 2 tool chay
# song song ma KHONG ghi de lan nhau, moi tool dung config / crash log / cache
# rieng theo PROFILE_ID duoi day.
# Neu copy tool sang thu muc khac, chi can doi PROFILE_ID la du.
# ---------------------------------------------------------------------------
PROFILE_ID = "V23.6"

_APP_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "VideoStoryPublisher" / PROFILE_ID

APP_NAME = "Video Story Publisher - GPT-6 Sol V23.4"
CONFIG_FILE = Path.home() / ".video_story_publisher_V23.6.json"

CRASH_LOG_FILE = Path.cwd() / "video_story_publisher_crash_V23.6.log"

# Phiên bản tool. updater.py đọc dòng này để so với version.json trên GitHub;
# release.py tự ghi lại mỗi lần phát hành bản mới.
APP_VERSION = "23.6.6"

def _write_crash_log(title: str, exc_type=None, exc_value=None, exc_tb=None, extra: str = ""):
    """Write fatal/unhandled errors to a persistent text file."""
    try:
        import traceback
        import datetime
        with CRASH_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write("\n" + "=" * 90 + "\n")
            f.write(f"{datetime.datetime.now().isoformat(sep=' ', timespec='seconds')} | {title}\n")
            f.write("=" * 90 + "\n")
            if extra:
                f.write(extra + "\n")
            if exc_type is not None:
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
            f.flush()
    except Exception:
        pass

def _global_excepthook(exc_type, exc_value, exc_tb):
    import traceback
    print("\n" + "=" * 90)
    print("UNHANDLED APPLICATION ERROR")
    print("=" * 90)
    traceback.print_exception(exc_type, exc_value, exc_tb)
    print(f"\nCrash log saved to: {CRASH_LOG_FILE}")
    print("=" * 90)
    _write_crash_log("UNHANDLED APPLICATION ERROR", exc_type, exc_value, exc_tb)

sys.excepthook = _global_excepthook

if hasattr(threading, "excepthook"):
    def _thread_excepthook(args):
        import traceback
        print("\n" + "=" * 90)
        print(f"UNHANDLED THREAD ERROR | thread={getattr(args.thread, 'name', '?')}")
        print("=" * 90)
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
        print(f"\nCrash log saved to: {CRASH_LOG_FILE}")
        print("=" * 90)
        _write_crash_log(
            f"UNHANDLED THREAD ERROR | thread={getattr(args.thread, 'name', '?')}",
            args.exc_type, args.exc_value, args.exc_traceback
        )
    threading.excepthook = _thread_excepthook



FIXED_PRODUCTION_SETTINGS = {
    # AI / video
    "use_writer_for_vision": False,
    "whisper_model": "small",
    "whisper_device": "cpu",
    "frames_per_video": "12",
    "vision_retry_count": "4",
    "vision_retry_delay": "12",
    "vision_image_width": "768",
    "vision_image_quality": "70",
    "fast_whisper_beam_size": "1",

    # Story
    "language": "English",
    "fast_story_mode": True,
    "min_story_chapters": "6",
    "max_story_chapters": "10",
    "target_words_per_chapter": "500",
    "chapter_tolerance_percent": "10",
    "single_request_story": False,
    "parallel_chapter_writing": True,
    "chapter_workers": "4",
    "chapter_retry_count": "3",
    "min_words_each_chapter": "450",
    "max_words_each_chapter": "550",

    # SmartTraffic site/category/page break are user-editable (defaults live in DEFAULT_CONFIG).

    # Image / thumbnail
    "facebook_thumbnail_mode": True,
    "thumbnail_width": "290",
    "thumbnail_height": "290",
    "cloudinary_workers": "4",
    "require_thumbnail_before_publish": True,
}

def apply_fixed_production_settings(cfg: dict) -> dict:
    cfg = dict(cfg or {})
    cfg.update(FIXED_PRODUCTION_SETTINGS)
    return cfg


DEFAULT_STORY_PROMPT = 'Bạn là biên tập viên chuyên phát triển **hook drama video khoảng 15 giây** thành một câu chuyện dài nhiều chương dành cho độc giả Mỹ/Âu, đặc biệt nhóm trung niên và lớn tuổi.\n\nĐầu vào có thể là:\n\n* ý tưởng drama;\n* hook 15 giây;\n* kịch bản video 15 giây;\n* hoặc một câu chuyện ngắn cần mở rộng.\n\nMục tiêu cuối cùng là tạo ra:\n\n**Hook 15s → Story Architecture → Full Multi-Chapter Story → QC → Theme Story Format → Facebook Caption → File xuất bản**\n\n---\n\n# 1. QUY TẮC CỐT LÕI\n\nVideo, Caption và Blog phải thuộc **cùng một canon**.\n\nPhải giữ nhất quán:\n\n* tên nhân vật;\n* tuổi;\n* nghề nghiệp;\n* quan hệ;\n* địa điểm;\n* timeline;\n* sự kiện trong hook;\n* nguyên nhân xung đột;\n* các chi tiết quan trọng.\n\nNếu hook/video đã được duyệt hoặc đã sản xuất, coi nội dung đó là **LOCKED**.\n\nKhông tự ý viết lại hook chỉ vì có phương án khác hay hơn.\n\nCó thể xây dựng:\n\n* những gì xảy ra trước hook;\n* nguyên nhân dẫn tới hook;\n* hậu quả sau hook;\n* bí mật;\n* escalation;\n* climax;\n* payoff.\n\n---\n\n# 2. STORY ARCHITECTURE — BẮT BUỘC TRƯỚC KHI VIẾT DÀI\n\nTrước khi viết full story, phải thiết kế Story Architecture.\n\nBao gồm:\n\n**Story Title**\n\nTên tiếng Anh tự nhiên, dễ hiểu với người Mỹ, tạo tò mò nhưng không spoil kết thúc.\n\n**Expected Chapters**\n\nChọn khoảng **6–10 chapter** tùy độ dày thật của câu chuyện.\n\nKhông ép đủ 10 chương.\n\nNếu nội dung chỉ đủ cho 6–7 chapter thì dùng 6–7.\n\nNếu câu chuyện có nhiều lớp xung đột và reveal hợp lý thì có thể dùng 9–10.\n\n**Chapter Architecture**\n\nVới từng chapter, xác định ngắn gọn:\n\n* chức năng của chapter;\n* xung đột hoặc câu hỏi chính;\n* thay đổi quan trọng;\n* reveal/twist nếu có;\n* điểm kết chapter.\n\n**Major Story Beats**\n\nXác định trước:\n\n* setup;\n* escalation;\n* major reveals;\n* climax;\n* major twist nếu có;\n* final payoff.\n\n### Quy tắc\n\nCấu trúc 10 chapter chỉ là **khung tham khảo**, không phải công thức.\n\nNếu dùng 10 chương, có thể tham khảo:\n\nCh1: Hook / Setup\nCh2–3: Relationship / Conflict\nCh4–6: Escalation / Reveals\nCh7–8: Climax build\nCh9: Major turn hoặc major reveal\nCh10: Payoff\n\nNhưng twist KHÔNG bắt buộc nằm Chapter 9.\n\nVới 6–7 chapter, escalation và reveal phải xuất hiện sớm hơn.\n\nVới 9–10 chapter, có thể phát triển nhiều lớp bí mật hơn.\n\nVị trí twist phải phục vụ **retention và logic câu chuyện**, không phục vụ công thức.\n\n### Workflow duyệt\n\nMặc định:\n\n**Viết Story Architecture trước → dừng để tôi duyệt → sau khi duyệt mới viết Full Story.**\n\nNếu tôi yêu cầu chạy toàn bộ một lượt thì có thể tự triển khai tiếp mà không dừng.\n\n---\n\n# 3. CẤU TRÚC FULL STORY\n\nFull Story thông thường gồm:\n\n**6–10 chapter**\n\nMục tiêu khoảng:\n\n**500–800 từ/chapter**\n\nCó thể ngắn hơn hoặc dài hơn 1 chút, nếu dài hơn không được vượt quá 900 từ / chapter\n\nƯu tiên theo thứ tự:\n\n**Retention → Logic → Emotion → Readability → Word Count**\n\nNếu một chapter không mang lại ít nhất một trong những thứ sau:\n\n* xung đột mới;\n* thông tin mới;\n* thay đổi cảm xúc;\n* thay đổi quan hệ;\n* một quyết định;\n* một reveal;\n* thay đổi cách độc giả hiểu tình huống;\n* lý do mạnh để đọc tiếp;\n\nthì chapter đó không nên tồn tại.\n\n---\n\n# 4. MỖI CHAPTER PHẢI CÓ MINI-ARC\n\nKhông được dùng chapter chỉ để giải thích.\n\nMỗi chapter cần có:\n\n**Setup nhỏ → Development → Change / Discovery → Forward Pull**\n\nTức là chapter phải mở một vấn đề, phát triển nó và khiến trạng thái câu chuyện thay đổi.\n\nCuối chapter ưu tiên một **soft cliffhanger** như:\n\n* một cuộc gọi;\n* một người xuất hiện;\n* một quyết định;\n* một phát hiện;\n* một tài liệu;\n* một câu nói;\n* một bằng chứng;\n* một câu hỏi mới;\n* một thông tin làm thay đổi tình hình.\n\nKhông sử dụng cliffhanger giả tạo kiểu:\n\n""""You won\'t believe what happened next.""""\n\nCliffhanger phải xuất phát tự nhiên từ nội dung.\n\n---\n\n# 5. CHAPTER 1 VÀ HOOK VIDEO\n\nChapter 1 phải nối trực tiếp với hook/video 15 giây.\n\nKhông kể lại toàn bộ video dài dòng.\n\nChỉ nhắc đủ để người đọc nhận ra:\n\n**""""Đây chính là câu chuyện tôi vừa xem.""""**\n\nSau đó nhanh chóng đưa câu chuyện tiến về phía trước.\n\nKhông dùng cấu trúc máy móc:\n\nHook → flashback dài nhiều chương → quay lại hiện tại.\n\nƯu tiên storytelling tuyến tính và dễ theo dõi.\n\nChỉ sử dụng flashback khi thật sự cần thiết.\n\n---\n\n# 6. ESCALATION, REVEAL VÀ TWIST\n\nMiddle chapters không được chỉ liên tục thêm sự kiện.\n\nThông tin mới nên khiến độc giả **thay đổi cách nhìn** về:\n\n* một nhân vật;\n* một mối quan hệ;\n* động cơ;\n* nguyên nhân sự việc;\n* người đáng tin;\n* những gì thực sự đã xảy ra.\n\nReveal có thể xuất hiện sớm nếu nó mở ra một conflict mới mạnh hơn.\n\nReveal có thể giữ đến gần cuối nếu việc trì hoãn tạo payoff tốt hơn.\n\nNhưng nếu giữ twist muộn thì phải gieo dấu hiệu từ trước.\n\nKhông sử dụng twist hoàn toàn không có setup.\n\nKhông front-load twist chỉ để gây sốc.\n\nKhông delay twist chỉ vì muốn kéo dài câu chuyện.\n\n---\n\n# 7. NHÂN VẬT CHÍNH PHẢI CÓ AGENCY\n\nNhân vật chính không được dành toàn bộ câu chuyện để:\n\nchịu đựng → khóc → chờ người khác tới cứu.\n\nỞ phần sau câu chuyện, họ phải chủ động làm điều gì đó thay đổi cục diện.\n\nVí dụ:\n\n* tìm bằng chứng;\n* đối đầu;\n* đưa ra quyết định;\n* rời khỏi mối quan hệ;\n* bảo vệ người khác;\n* điều tra sự thật;\n* công khai thông tin;\n* gọi đúng người;\n* từ chối tiếp tục chịu đựng;\n* tự mình thực hiện hành động quyết định.\n\nNgười khác có thể hỗ trợ, nhưng protagonist phải có vai trò trong việc tạo ra kết quả.\n\n---\n\n# 8. PAYOFF CUỐI\n\nKhông kết truyện ngay sau major reveal.\n\nSau reveal phải có **payoff**.\n\nEnding cần giải quyết:\n\n* central conflict;\n* hậu quả của protagonist;\n* hậu quả của antagonist hoặc người gây xung đột;\n* các twist đã gieo;\n* relationship arc;\n* câu hỏi lớn của câu chuyện.\n\nNgười đọc phải cảm thấy câu chuyện đã **đi đến nơi đến chốn**.\n\nCó thể kết bằng một cảm xúc hoặc suy ngẫm nhẹ.\n\nKhông biến đoạn cuối thành bài giảng đạo đức.\n\n---\n\n# 9. TONE VÀ VĂN PHONG\n\nFull Story và Caption viết bằng **English**.\n\nƯu tiên **natural American English**.\n\nĐối tượng chính:\n\n* US;\n* Europe;\n* middle-aged readers;\n* older readers.\n\nVăn phong cần:\n\n* rõ ràng;\n* đời thường;\n* dễ theo dõi;\n* cảm xúc nhưng không melodramatic;\n* câu tương đối ngắn;\n* dialogue thực tế;\n* ít slang;\n* ít ẩn dụ;\n* ít câu văn màu mè;\n* ít giải thích tâm lý trực tiếp.\n\nTránh cảm giác:\n\n* AI-generated;\n* soap opera quá mức;\n* cố tình giật gân;\n* nhân vật chỉ nói để giải thích cốt truyện.\n\nƯu tiên **show through action and dialogue**.\n\n---\n\n# 10. LOGIC & CONTINUITY QC\n\nSau khi viết Full Story, phải tự kiểm tra trước khi xuất bản.\n\nKiểm tra:\n\n* tên nhân vật;\n* tuổi;\n* nghề nghiệp;\n* quan hệ;\n* địa điểm;\n* timeline;\n* thời gian di chuyển;\n* động cơ;\n* quyền hạn của nhân vật;\n* bằng chứng;\n* cuộc gọi;\n* tài liệu;\n* camera;\n* nhân chứng;\n* tiền bạc;\n* thông tin nhân vật biết hoặc chưa biết;\n* setup trước reveal;\n* nguyên nhân và hậu quả;\n* payoff cuối.\n\nĐặc biệt kiểm tra coincidence.\n\nKhông để một người, tài liệu, cuộc gọi hoặc bằng chứng xuất hiện đúng lúc chỉ để cứu cốt truyện mà không có lý do.\n\nNếu phát hiện plot hole, tự sửa trước khi xuất bản.\n\nKhông hiển thị phần QC nội bộ trừ khi tôi yêu cầu.\n\n---\n\n# 11. FORMAT THEME STORY / TINYMCE\n\nTên toàn bộ câu chuyện được dùng làm **Post Title**.\n\nKhông thêm lại title vào body nếu hệ thống không cần.\n\nMỗi chapter trong body phải bắt đầu chính xác theo dạng:\n\nChapter 1 - Chapter Title\n\nChapter 2 - Chapter Title\n\nChapter 3 - Chapter Title\n\nGiữa hai chapter phải có đúng:\n\n{{nextpage}}\n\nSeparator phải đứng **một mình trên một dòng**.\n\nFormat bắt buộc:\n\nChapter 1 - Chapter Title\n\n[nội dung chapter]\n\n{{nextpage}}\n\nChapter 2 - Chapter Title\n\n[nội dung chapter]\n\n{{nextpage}}\n\nChapter 3 - Chapter Title\n\n...\n\n\nKhông thay đổi cú pháp.\n\nKhông thêm khoảng trắng vào bên trong separator.\n\nKhông thêm:\n\nSERIES:\n\nKhông thêm các dòng kiểu:\n\nPosted Sep...\n\nKhông thêm metadata của website.\n\n---\n\n# 12. FILE BLOG.TXT\n\nKhi được yêu cầu xuất file Blog.txt, phải giữ nguyên chính xác:\n\nChapter N - Chapter Title\n\nvà\n\n{{nextpage}}\n\nKhông convert separator.\n\nKhông xóa separator.\n\nKhông thêm HTML.\n\nMục đích là để Theme Story tự:\n\n* chia page;\n* nhận chapter;\n* tạo navigation/table of contents.\n\n---\n\n# 13. FACEBOOK CAPTION\n\nCaption là một đầu ra riêng và viết **sau khi story đã hoàn thiện**.\n\nCaption phải đồng bộ trực tiếp với hook/video 15 giây.\n\nCaption nên:\n\n* mở bằng tình huống mạnh;\n* nhắc đúng cảnh người xem vừa thấy;\n* tạo open loop;\n* không spoil major reveal;\n* không spoil major twist;\n* không spoil payoff;\n* khiến người xem muốn đọc Full Story.\n\nCuối caption có CTA tự nhiên, ưu tiên:\n\n**Read the full story — link in the comments.**\n\nKhông cần giải thích toàn bộ câu chuyện trong caption.\n\nCaption chỉ có nhiệm vụ:\n\n**Hook → Curiosity → Click**\n\n---\n\n# 14. CONTINUITY VIDEO → CAPTION → STORY\n\nTrước khi hoàn thiện Caption, kiểm tra lại:\n\n**VIDEO HOOK = CAPTION = FULL STORY**\n\nKhông được khác nhau về:\n\n* tên;\n* tuổi;\n* quan hệ;\n* địa điểm;\n* sự kiện;\n* thoại quan trọng;\n* vật thể quan trọng;\n* nguyên nhân của hook;\n* tone câu chuyện.\n\nCaption không được tạo ra một phiên bản câu chuyện khác với Blog.\n\n---\n\n# 15. THỨ TỰ WORKFLOW CHUẨN\n\nLuôn xử lý theo thứ tự:\n\n**1. Hook / Input Drama**\n\n↓\n\n**2. Story Architecture**\n\n* Story Title\n* số chapter đề xuất\n* chức năng từng chapter\n* escalation\n* reveal/twist\n* climax\n* payoff\n\n↓\n\n**3. Duyệt Outline**\n\n↓\n\n**4. Write Full Story**\n\n6–10 chapter tùy nội dung.\n\n↓\n\n**5. QC Logic & Continuity**\n\n↓\n\n**6. Format Theme Story / TinyMCE**\n\n`Chapter N - ...`\n\n*\n\n`{{nextpage}}`\n\n↓\n\n**7. Write Facebook Caption**\n\n↓\n\n**8. Export Files nếu được yêu cầu**\n\n---\n\n# 16. OUTPUT Ở GIAI ĐOẠN STORY ARCHITECTURE\n\nKhi nhận ý tưởng/hook lần đầu, trả:\n\n## STORY TITLE\n\n[Title]\n\n## STORY ARCHITECTURE\n\n**Recommended length:** X Chapters\n\n### Chapter 1 - [Title]\n\nFunction:\nMain development:\nEnd pull:\n\n### Chapter 2 - [Title]\n\nFunction:\nMain development:\nEnd pull:\n\n...\n\n## MAJOR REVEALS / TWISTS\n\n[ngắn gọn]\n\n## CLIMAX\n\n[ngắn gọn]\n\n## PAYOFF\n\n[ngắn gọn]\n\nSau đó dừng để duyệt, trừ khi tôi yêu cầu triển khai full story luôn.\n\n---\n\n# 17. OUTPUT CUỐI CÙNG SAU KHI DUYỆT\n\n## STORY TITLE\n\n[Story title]\n\n## FULL BLOG STORY\n\nChapter 1 - [Chapter title]\n\n[Story]\n\n{{nextpage}}\n\nChapter 2 - [Chapter title]\n\n[Story]\n\n...\n\nChapter cuối không có `{{nextpage}}` phía sau.\n\n## FACEBOOK CAPTION\n\n[Caption hoàn chỉnh]\n\nNếu tôi yêu cầu xuất file, tạo riêng:\n\n**StoryTitle_Blog.txt**\n\nvà\n\n**StoryTitle_Caption.txt**\n\nBlog.txt phải giữ nguyên format TinyMCE.\n\n---\n\n# MỤC TIÊU CUỐI CÙNG\n\nKhông tối ưu cho việc đạt đủ 10 chapter.\n\n\nKhông tối ưu cho số lượng twist.\n\nTối ưu cho:\n\n**Người xem bị cuốn bởi hook 15 giây → bấm đọc → mỗi chapter đều tạo lý do đọc tiếp → cách hiểu câu chuyện liên tục thay đổi → protagonist chủ động thay đổi cục diện → climax đủ mạnh → payoff thỏa mãn → độc giả đọc đến cuối.**'


FIRST_PERSON_STORY_RULES = """# BẮT BUỘC: BLOG KỂ CHUYỆN NGÔI TÔI NHƯ MỘT NGƯỜI ĐANG KỂ CHO NGƯỜI KHÁC NGHE

Write the FULL BLOG STORY in natural American English from ONE consistent first-person narrator at the center of the hook. Let that person tell an attentive listener what happened, what they knew at the time, what they felt, and what they learned later. Choose the narrator based on the actual supplied script or inferred video script; do not copy names or events from examples. Use I / me / my naturally throughout all chapters, including their opening paragraphs. Keep the narrator and tense consistent. The Facebook caption must use the SAME narrator and facts.

# ĐỊNH DẠNG MỖI CÂU MỘT ĐOẠN
Write warm, direct, believable narrated prose with the reading rhythm of the supplied example. Put EACH narrative sentence in its own paragraph, followed by ONE blank line before the next sentence. Put EACH spoken line of dialogue in its own paragraph with one blank line after it. A brief standalone fragment is allowed where it sounds natural; vary sentence length and keep transitions clear. Never merge several sentences into a dense paragraph. Keep this as a story told by the first-person narrator, with actions in context, not camera directions or screenplay labels. Avoid melodramatic all-caps, rhetorical filler, and repetitive cliffhangers. This paragraph format applies to every chapter body and the Facebook caption; chapter headings and page-break markers stay on separate lines.

First-person viewpoint is limited: the narrator may tell only what they witnessed, heard, remembered, read, or learned later from a named person, message, recording, or document. Attribute offscreen events. Do not present another character's private thoughts or unseen actions as direct knowledge. Preserve characters, relationships, locations, chronology, stakes and outcome from the supplied script; if a video is also attached with a supplied script, its frames are ONLY for selecting images, not for creating or changing plot facts.

If only a video is supplied, use the extracted video_script.txt as the source of established hook facts; develop the subsequent fiction without claiming invented details were seen in the video. Keep recorded durations realistic: 47 seconds of footage cannot contain several minutes of conversation. Reveal longer backstory through later recollection, dialogue, documents, or discovery.

Write 6-10 chapters when supported by the story, about 500-800 words per chapter. Each chapter should read like a continuous narrated scene with a meaningful change and a reason to continue. Keep chapter breaks in the exporter format only. Before finishing, check each chapter for first-person continuity, natural paragraph rhythm, plausible timing, and consistency with the caption. Return JSON exactly as requested by the tool; these rules govern the prose inside each chapter's body, not the JSON structure."""

DEFAULT_STORY_PROMPT += "\n\n" + FIRST_PERSON_STORY_RULES

# Exact text of the previous bundled addendum, used only to update previously
# saved prompts without discarding the user's edits to the rest of the prompt.
PREVIOUS_FIRST_PERSON_STORY_RULES = FIRST_PERSON_STORY_RULES.replace(
    "# ĐỊNH DẠNG MỖI CÂU MỘT ĐOẠN\nWrite warm, direct, believable narrated prose with the reading rhythm of the supplied example. Put EACH narrative sentence in its own paragraph, followed by ONE blank line before the next sentence. Put EACH spoken line of dialogue in its own paragraph with one blank line after it. A brief standalone fragment is allowed where it sounds natural; vary sentence length and keep transitions clear. Never merge several sentences into a dense paragraph. Keep this as a story told by the first-person narrator, with actions in context, not camera directions or screenplay labels. Avoid melodramatic all-caps, rhetorical filler, and repetitive cliffhangers. This paragraph format applies to every chapter body and the Facebook caption; chapter headings and page-break markers stay on separate lines.",
    "Use warm, direct, believable storytelling with full paragraphs, usually 2-5 sentences each. Give actions context and let emotion come through decisions, concrete sensations, and restrained reflection. Weave dialogue into the narrator's account, with clear speakers; do not present an endless list of isolated lines or a screenplay transcript. A short standalone sentence is fine for an exceptional turn, but never make it the default rhythm. Avoid melodramatic all-caps, camera directions, montage language, rhetorical filler, and repeated cliffhanger fragments.",
)

# Câu chèn giữa caption và link bài viết trong TÊN FILE video.
# Mỗi dòng là 1 câu; tool chọn ngẫu nhiên 1 câu cho mỗi video.
# Để trống = giữ nguyên "full story" như bản cũ.
DEFAULT_FILENAME_PHRASES = "\n".join([
    "PART 2 & FULL ENDING",
    "WATCH THE NEXT PART HERE",
    "WHAT HAPPENS NEXT? WATCH HERE",
    "WATCH THE FULL DRAMA HERE",
    "SEE HOW IT ALL ENDS",
    "CONTINUE WATCHING HERE",
])

DEFAULT_CONFIG = {
    "vilao_base_url": "https://api.vilao.ai/v1",
    "vilao_api_key": "",
    "vision_model": "gpt-5.5",
    "writer_model": "gpt-6-sol",
    "story_prompt_text": DEFAULT_STORY_PROMPT,
    "filename_phrase_list": DEFAULT_FILENAME_PHRASES,
    "smarttraffic_api_key": "",
    "site_id": "120",
    "category_id": "1747",
    "site_host": "drama.viralstory.biz",
    "net_provider": "SmartTraffic",
    "adsconex_api_key": "",
    "adsconex_base_url": "https://usjusticereport.cfx.bz/api",
    "adsconex_site_host": "usjusticereport.cfx.bz",
    "adsconex_category": "15",
    "adsconex_author": "",
    "adsconex_apply_image_to_all": True,
    "language": "English",
    "whisper_model": "small",
    "whisper_device": "cpu",
    "frames_per_video": "12",
    "max_vision_frames": "12",
    "vision_batch_size": "6",
    "vision_retry_count": "4",
    "vision_retry_delay": "12",
    "vision_image_width": "768",
    "vision_image_quality": "70",
    "chapter_image_candidates": "9",
    "chapter_image_search_seconds": "1.8",
    "prefer_faces_for_chapter_images": True,
    "cloudinary_cloud_name": "",
    "cloudinary_api_key": "",
    "cloudinary_api_secret": "",
    "auto_publish": False,
    "use_writer_for_vision": False,
    "fast_story_mode": True,
    "min_story_chapters": "6",
    "max_story_chapters": "10",
    "target_words_per_chapter": "500",
    "chapter_generation_mode": "separate",
    "chapter_tolerance_percent": "15",
    "chapter_workers": "4",
    "parallel_chapter_writing": True,
    "fast_writer_batches": True,
    "single_request_story": False,
    "min_words_each_chapter": "450",
    "max_words_each_chapter": "550",
    "chapter_retry_count": "3",
    "pagebreak_mode": "<!--nextpage-->",
    "continue_if_image_upload_fails": True,
    "facebook_thumbnail_mode": True,
    "thumbnail_width": "290",
    "thumbnail_height": "290",
    "require_thumbnail_before_publish": True,
    "fast_whisper_beam_size": "1",
    "skip_audio_extract": True,
    "frame_workers": "4",
    "vision_parallel_workers": "2",
    "cloudinary_workers": "4",
    "ffmpeg_bin": r"C:\\ffmpeg-9.0.1-essentials_build\\bin",
}

GUIDE_TEXT = """
HƯỚNG DẪN VIDEO STORY PUBLISHER

1. Chọn video, nhập kịch bản, hoặc dùng cả hai.
   Chỉ video: Whisper và Vision đọc nội dung, xuất video_script.txt rồi
   gửi kịch bản suy ra này cho Writer. Có kịch bản: Writer chỉ dùng kịch bản
   đã nhập, bỏ qua Whisper và Vision. Video đi kèm chỉ dùng trích ảnh.
2. Sửa prompt mẫu trong tab này khi cần, bấm Save Settings để giữ lại.
3. Nhập Vilao API Key và chọn Writer model (mặc định gpt-6-sol).
   Vision model chỉ dùng nếu không có kịch bản đầu vào.
4. Bật Viết nhanh để chạy hai nhóm chapter đồng thời theo Story Architecture.
   Tắt nếu muốn mỗi nhóm nhận đầy đủ đoạn kết của nhóm trước.
5. Kết quả: Blog.txt cho TinyMCE, Caption.txt riêng, story.json và article.html.
   Video tự lấy ảnh bìa và ảnh từng chương từ chính video; ảnh Facebook
   290x290 được lưu riêng. Chỉ có kịch bản: đăng bài dạng chữ, không cần ảnh.
6. Blog.txt dùng {{nextpage}} trên một dòng riêng. Khi gửi HTML bằng API,
   marker chính là <!--nextpage--> (HTML comment). V21.3 fix khi marker text
   bị site hiển thị nguyên chữ trong bài viết. Tool tự fallback {{nextpage}}
   nếu API không paginate.
7. Theo tài liệu API, chỉ website dùng theme story mới tự chia chương.
   Tool kiểm tra Site ID/host trước khi đăng và URL ?c=2 sau khi đăng.
   Nếu còn nguyên dấu ngắt hoặc toàn bộ các chương, hãy bật theme story
   trên website; API publish chỉ tạo bài, không chuyển theme của website.
 8. Nut "Cap nhat" (goc phai thanh nut): kiem tra ban moi tren GitHub va tu
    dong cap nhat. Cai dat + API key + bai da lam KHONG bi mat khi cap nhat.
 9. O "Cau chen trong ten file": moi dong 1 cau, tool chon ngau nhien 1 cau
    dat giua caption va link bai viet trong TEN FILE video (vi du
    "...;PART 2 & FULL ENDING https...article123.mp4"). De trong = "full story".
    Luu y: ten file Windows khong cho phep cac ky tu dac biet (? * : / gach
    cheo nguoc < > |) nen tool tu thay bang dau cach.
10. O "Net dang bai" o vung Dau vao: chon SmartTraffic (mac dinh) hoac
    Adsconex. Chon Adsconex thi bai dang sang site Blogbio, moi chapter la
    mot bai rieng va link dang /blog/<slug>; token + category nhap trong tab
    Adsconex. Doi net KHONG lam mat API key cua net kia.
"""




# ---------------------------
# Helpers
# ---------------------------
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

def machine_diagnostics(cfg: dict) -> str:
    """Collect comparable, non-secret diagnostics from the actual Python process."""
    import importlib.metadata as metadata
    lines = [f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"Windows: {platform.platform()}",
             f"Python: {sys.version.split()[0]} ({struct.calcsize('P') * 8} bit)",
             f"Python executable: {sys.executable}",
             f"Whisper: {cfg.get('whisper_model', 'small')} / {cfg.get('whisper_device', 'cpu')}",
             f"OMP_NUM_THREADS: {os.environ.get('OMP_NUM_THREADS', '')}",
             f"MKL_NUM_THREADS: {os.environ.get('MKL_NUM_THREADS', '')}"]
    for pkg in ("numpy", "opencv-python", "faster-whisper", "ctranslate2", "torch", "av"):
        try:
            lines.append(f"{pkg}: {metadata.version(pkg)}")
        except metadata.PackageNotFoundError:
            lines.append(f"{pkg}: not installed")
    if os.name == "nt":
        try:
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                            ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                            ("avail_ext", ctypes.c_ulonglong)]
            mem = MemoryStatus()
            mem.length = ctypes.sizeof(mem)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                gib = 1024 ** 3
                lines += [f"Physical RAM: {mem.total_phys / gib:.2f} GiB",
                          f"Physical available: {mem.avail_phys / gib:.2f} GiB",
                          f"Commit limit: {mem.total_page / gib:.2f} GiB",
                          f"Commit available: {mem.avail_page / gib:.2f} GiB",
                          f"Process virtual available: {mem.avail_virtual / gib:.2f} GiB"]
        except Exception as exc:
            lines.append(f"Memory probe error: {exc}")
    return "\n".join(lines) + "\n"

def find_unpublished_videos(folder: Path) -> List[Path]:
    """Scan the selected directory and its children; skip renamed finished videos."""
    return sorted((p for p in folder.rglob("*") if p.is_file()
                   and p.suffix.lower() in VIDEO_EXTS
                   and not re.search(r";[^;]*article\d+$", p.stem, re.I)),
                  key=lambda p: str(p).casefold())

def find_saved_publications(output_root: Path, videos: List[Path], net_provider: str = "SmartTraffic",
                            site_host: str = "drama.viralstory.biz",
                            adsconex_site_host: str = "usjusticereport.cfx.bz") -> dict:
    """Find existing article IDs for source videos before any new publish POST."""
    from collections import Counter
    name_counts = Counter(v.name.casefold() for v in videos)
    found = {str(v.resolve()).casefold(): [] for v in videos}
    exact = {key: [] for key in found}
    for work_dir in output_root.glob("_story_output_*"):
        result_path = work_dir / "publish_result.json"
        if not result_path.is_file():
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            use_ads = str(net_provider or "").strip().lower().startswith("ads")
            existing_link = (published_adsconex_link(result, adsconex_site_host) if use_ads
                             else published_article_link(result, site_host))
            if not existing_link:
                continue
            source_path = work_dir / "source_video.json"
            if source_path.is_file():
                source = json.loads(source_path.read_text(encoding="utf-8"))
                key = str(Path(source["path"]).resolve()).casefold()
                if key in found:
                    exact[key].append((work_dir, result))
            else:
                # Older builds only recorded the basename. Match it only if
                # that name occurs once in the selected batch.
                timeline_path = work_dir / "timeline.json"
                timeline = json.loads(timeline_path.read_text(encoding="utf-8")) if timeline_path.is_file() else []
                if len(timeline) == 1:
                    name = str(timeline[0].get("video") or "").casefold()
                    if name_counts[name] == 1:
                        for video in videos:
                            if video.name.casefold() == name:
                                found[str(video.resolve()).casefold()].append((work_dir, result))
                                break
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # An exact saved path beats all older records that stored only a basename.
    # Folder timestamps sort in chronological order; choose the last publish
    # when retries created several articles for one input video.
    return {key: sorted(exact[key] or matches, key=lambda item: item[0].name, reverse=True)
            for key, matches in found.items() if exact[key] or matches}

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update(saved)
            # Add the storyteller style once to prompts saved by older builds,
            # preserving every custom instruction that the user already wrote.
            if not saved.get("first_person_prompt_migrated"):
                existing_prompt = str(cfg.get("story_prompt_text") or "").strip()
                if "# BẮT BUỘC: BLOG KỂ CHUYỆN NGÔI TÔI" not in existing_prompt:
                    cfg["story_prompt_text"] = existing_prompt + "\n\n" + FIRST_PERSON_STORY_RULES
                cfg["first_person_prompt_migrated"] = True
            if not saved.get("single_sentence_format_migrated"):
                existing_prompt = str(cfg.get("story_prompt_text") or "").strip()
                if "# ĐỊNH DẠNG MỖI CÂU MỘT ĐOẠN" not in existing_prompt:
                    if PREVIOUS_FIRST_PERSON_STORY_RULES in existing_prompt:
                        existing_prompt = existing_prompt.replace(
                            PREVIOUS_FIRST_PERSON_STORY_RULES, FIRST_PERSON_STORY_RULES
                        )
                    else:
                        # Preserve user edits even if they changed the old addendum.
                        existing_prompt += "\n\n" + FIRST_PERSON_STORY_RULES
                    cfg["story_prompt_text"] = existing_prompt
                cfg["single_sentence_format_migrated"] = True
            # Prior builds silently persisted {{nextpage}} for API requests.
            # Migrate once to the raw-HTML marker documented for the API;
            # subsequent user changes to the page-break selector are respected.
            if not saved.get("pagebreak_api_migrated"):
                if saved.get("pagebreak_mode", "{{nextpage}}") == "{{nextpage}}":
                    cfg["pagebreak_mode"] = "<!--nextpage-->"
                cfg["pagebreak_api_migrated"] = True
            # Migrate the former shipped default once, while preserving future edits.
            if not saved.get("writer_gpt6_migrated"):
                if saved.get("writer_model") in (None, "gpt-5.5", "gpt-5.6-sol"):
                    cfg["writer_model"] = "gpt-6-sol"
                cfg["writer_gpt6_migrated"] = True
            # Earlier builds hid this field and routed Vision through Writer.
            if saved.get("vision_model") in (None, "gpt-4o"):
                cfg["vision_model"] = "gpt-5.5"
            if str(saved.get("max_vision_frames", "")) == "36":
                cfg["max_vision_frames"] = "12"
            # V10 migration: old versions used max=8 and had no minimum.
            if "min_story_chapters" not in saved:
                cfg["min_story_chapters"] = "7"
            if str(saved.get("max_story_chapters", "")) in ("", "8"):
                cfg["max_story_chapters"] = "10"
        except Exception:
            pass
    return cfg

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

def safe_slug(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:180] or f"story-{int(time.time())}"

def published_article_link(result: dict, site_host: str) -> str:
    """Use the numeric article ID returned by THIS publish, never the requested slug."""
    if not isinstance(result, dict):
        return ""
    candidates = [result]
    for obj in candidates:
        for key in ("data", "article", "result"):
            if isinstance(obj.get(key), dict) and obj[key] not in candidates:
                candidates.append(obj[key])
    article_id = next((str(obj[key]).strip() for obj in candidates for key in ("id", "article_id", "articleId")
                       if str(obj.get(key) or "").strip().isdigit()), "")
    if not article_id:
        # Some APIs return an ID-based URL without an explicit id field.
        for obj in candidates:
            found = re.search(r"/article/(\d+)(?:[/?#]|$)", str(obj.get("url") or obj.get("article_url") or ""))
            if found:
                article_id = found.group(1)
                break
    host = urlsplit("https://" + re.sub(r"^https?://", "", str(site_host).strip().strip("/"))).netloc
    return f"https://{host}/article/{article_id}" if host and article_id else ""

def published_adsconex_link(result: dict, site_host: str) -> str:
    """Link bai Adsconex/Blogbio dang /blog/<slug>. Lay tu response, khong doan."""
    if not isinstance(result, dict):
        return ""
    host = urlsplit("https://" + re.sub(r"^https?://", "", str(site_host).strip().strip("/"))).netloc
    if not host:
        return ""
    candidates = [result]
    for obj in list(candidates):
        for key in ("data", "post", "result", "series", "posts", "chapters"):
            nested = obj.get(key) if isinstance(obj, dict) else None
            if isinstance(nested, dict) and nested not in candidates:
                candidates.append(nested)
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict) and item not in candidates:
                        candidates.append(item)
    for obj in candidates:
        url = str(obj.get("link") or obj.get("url") or obj.get("permalink") or "").strip()
        if url.startswith("http"):
            return url
        if url.startswith("/"):
            return f"https://{host}{url}"
    for obj in candidates:
        slug = str(obj.get("slug") or "").strip()
        if slug:
            return f"https://{host}/blog/{slug}"
    return ""

def build_adsconex_chapter_content(story: dict) -> str:
    """Dung content cho POST /api/posts mode=chapter.

    Moi chapter la 1 dong 'CHAPTER N - Title'; phan truoc marker dau tien
    tro thanh mo ta series. Khong dung {{nextpage}} nhu SmartTraffic.
    """
    chapters = story.get("chapters", []) or []
    intro = compact_meta_text(story.get("meta_description") or story.get("summary") or "", 600)
    parts = []
    if intro:
        parts.append(f"<p>{html_escape(intro)}</p>")
    for idx, ch in enumerate(chapters):
        n = ch.get("number", idx + 1)
        raw_title = str(ch.get("title") or "").strip()
        raw_title = re.sub(rf"^chapter\s*{n}\s*[-:–—]*\s*", "", raw_title, flags=re.I).strip()
        if not raw_title:
            raw_title = "The Story Takes a Dangerous Turn"
        parts.append(f"<p>CHAPTER {n} - {html_escape(raw_title)}</p>")
        parts.append(paragraphize(chapter_body_only(ch.get("body", ""), n)))
    return "\n".join(parts)

def active_publish_link(result: dict, cfg: dict) -> str:
    """Link bai theo net dang chon: Adsconex (/blog/<slug>) hoac SmartTraffic (/article/<id>)."""
    cfg = cfg or {}
    provider = str(cfg.get("net_provider") or "SmartTraffic").strip().lower()
    if provider.startswith("ads"):
        return published_adsconex_link(result, cfg.get("adsconex_site_host", ""))
    return published_article_link(result, cfg.get("site_host", ""))

def choose_filename_phrase(cfg: dict) -> str:
    """Chọn ngẫu nhiên 1 câu trong ô 'Câu chèn trong tên file'. Trống = 'full story'."""
    import random
    raw = str(cfg.get("filename_phrase_list") or "").replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", ln).strip(" .;") for ln in raw.split("\n")]
    lines = [ln for ln in lines if ln]
    return random.choice(lines) if lines else "full story"

def safe_video_filename(post_text: str, article_url: str, suffix: str, max_length: int = 219,
                        phrase: str = "full story") -> str:
    """Limit the entire filename, including extension, to 219 characters.
    'phrase' là câu ngẫu nhiên chèn giữa caption và link bài viết."""
    import unicodedata
    safe_url = article_url.replace(":", "").replace("/", "")
    clean_phrase = re.sub(r'[<>:"/\\|?*\x00-\x1f;]', " ", str(phrase or ""))
    clean_phrase = re.sub(r"\s+", " ", clean_phrase).strip(" .;")
    ending = f";{clean_phrase} {safe_url}" if clean_phrase else f";{safe_url}"
    # Some WebDAV/SMB servers limit UTF-8 bytes, even when Windows reports
    # a character count below 219. Keep the filename ASCII and URL intact.
    post_text = post_text.replace("’", "'").replace("‘", "'").replace("—", "-").replace("–", "-")
    post_text = unicodedata.normalize("NFKD", post_text).encode("ascii", "ignore").decode("ascii")
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", post_text)
    title = re.sub(r"\s+", " ", title).strip(" .;")
    remaining = max_length - len(ending) - len(suffix)
    if remaining < 1:
        raise ValueError("Link bài viết quá dài để đặt tên video trong 219 ký tự.")
    if len(title) > remaining:
        # Shorten the hook first, then drop optional tags. Never break the
        # agreed hook + at least 3 hashtags + ;<phrase> + ID format.
        match = re.search(r"((?:\s+#[A-Za-z0-9_]+){3,5})$", title)
        if match:
            tags = match.group(1).split()
            hook = title[:match.start()].strip(" .;")
            while len(tags) > 3 and len(" ".join(tags)) + 2 > remaining:
                tags.pop()
            if len(" ".join(tags)) + 2 > remaining:
                tags = ["#story", "#drama", "#news"]
            room = remaining - len(" ".join(tags)) - 1
            if room < 1:
                raise ValueError("Không đủ chỗ cho hook, 3 hashtag và link bài viết.")
            hook = hook[:room].rstrip(" .;") or "Story"
            title = f"{hook} {' '.join(tags)}"
        else:
            title = title[:remaining].rstrip(" .;")
    filename = title + ending + suffix
    if len(filename) > max_length:
        raise ValueError(f"Tên file video dài {len(filename)} ký tự, vượt mức {max_length}.")
    return filename

def chapter_links_text(article_url: str, chapter_count: int) -> str:
    """Return the article link and its numbered chapter pages for copying."""
    url = (article_url or "").strip()
    if not url:
        return ""
    count = max(0, int(chapter_count))
    parts = urlsplit(url)
    base_query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                  if key.lower() != "c"]
    base_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(base_query), parts.fragment))
    lines = [f"Chapter 1: {base_url}"]
    for number in range(2, count + 1):
        chapter_url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                 urlencode(base_query + [("c", str(number))]), parts.fragment))
        lines.append(f"Chapter {number}: {chapter_url}")
    return "\n".join(lines) + "\n"

def _sanitize_hidden_chars(name: str) -> str:
    """Strip zero-width and bidirectional Unicode control chars that confuse
    Windows/WebDAV filesystem paths. Preserves visible diacritics like đ, ả, ề."""
    bad = {
        "\u200b", "\u200c", "\u200d", "\u200e", "\u200f",
        "\ufeff",  # BOM / zero-width no-break space
        "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u2060", "\u2061", "\u2062", "\u2063", "\u2064",
    }
    cleaned = "".join(ch for ch in (name or "") if ch not in bad)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "output"


def _safe_write_text(target: Path, data, **kwargs) -> bool:
    """Write text/data to `target`. If the write fails (WebDAV PermissionError,
    locked file, etc.) fall back to a sibling cache under
    %LOCALAPPDATA%/VideoStoryPublisher/_write_fallback so the run still produces
    output. The fallback path bypasses Path.write_text and uses the built-in
    open() so it works even when the WebDAV driver layer breaks pathlib writes.
    Returns True on success at the original path, False if only fallback used."""
    encoding = kwargs.get("encoding", "utf-8")
    payload = data if isinstance(data, str) else (data.decode(encoding) if isinstance(data, (bytes, bytearray)) else str(data))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(str(target), "w", encoding=encoding, errors=kwargs.get("errors", "strict"), newline=kwargs.get("newline", None)) as f:
            f.write(payload)
        return True
    except (PermissionError, OSError) as primary_err:
        try:
            cache_root = Path(
                os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local")
            ) / "VideoStoryPublisher" / PROFILE_ID / "_write_fallback"
            mirror = cache_root / target.name
            mirror.parent.mkdir(parents=True, exist_ok=True)
            with open(str(mirror), "w", encoding=encoding) as f:
                f.write(payload)
            print(f"[FALLBACK] Cannot write '{target}': {primary_err}; cached at {mirror}")
        except Exception as mirror_err:
            print(f"[FALLBACK FAIL] Both original and cache failed for '{target}': {mirror_err}")
        return False


def natural_sort_key(p: Path):
    return [int(s) if s.isdigit() else s.lower() for s in re.split(r"(\d+)", p.name)]

def list_videos(path: Path) -> List[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in VIDEO_EXTS else []
    files = [p for p in path.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    return sorted(files, key=natural_sort_key)

FFMPEG_BIN = ""

def configure_ffmpeg(ffmpeg_bin: str = ""):
    """Configure ffmpeg/ffprobe without requiring Windows PATH."""
    global FFMPEG_BIN
    ffmpeg_bin = (ffmpeg_bin or "").strip().strip('"')
    candidates = []

    if ffmpeg_bin:
        candidates.append(Path(ffmpeg_bin))

    # Common locations + the path currently used by this project.
    candidates += [
        Path(r"C:\ffmpeg-9.0.1-essentials_build\bin"),
        Path(r"C:\ffmpeg\bin"),
    ]

    for folder in candidates:
        if (folder / "ffmpeg.exe").exists():
            FFMPEG_BIN = str(folder)
            # Make subprocess/OpenCV child processes able to see it as well.
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")
            return str(folder)

    # PATH fallback
    ff = shutil.which("ffmpeg")
    if ff:
        FFMPEG_BIN = str(Path(ff).parent)
        return FFMPEG_BIN

    FFMPEG_BIN = ""
    return ""

def exe_path(name: str) -> str:
    if FFMPEG_BIN:
        p = Path(FFMPEG_BIN) / (name + ".exe")
        if p.exists():
            return str(p)
    return shutil.which(name) or name


def auto_detect_ffmpeg_bin(configured: str = "") -> str:
    """
    Auto-detect FFmpeg on Windows.
    Priority:
    1) configured path
    2) app folder / ffmpeg / bin
    3) common user path C:\\ffmpeg\\bin
    4) PATH
    """
    import shutil
    from pathlib import Path

    candidates = []

    if configured:
        candidates.append(Path(configured))

    try:
        app_dir = Path(__file__).resolve().parent
        candidates += [
            app_dir / "ffmpeg" / "bin",
            app_dir / "bin",
            app_dir,
        ]
    except Exception:
        pass

    candidates += [
        Path(r"C:\ffmpeg\bin"),
        Path(r"C:\ffmpeg-9.0.1-essentials_build\bin"),
        Path(r"C:\Program Files\ffmpeg\bin"),
    ]

    for folder in candidates:
        try:
            if (folder / "ffmpeg.exe").exists() and (folder / "ffprobe.exe").exists():
                return str(folder)
        except Exception:
            pass

    ffmpeg_exe = shutil.which("ffmpeg")
    ffprobe_exe = shutil.which("ffprobe")
    if ffmpeg_exe and ffprobe_exe:
        return str(Path(ffmpeg_exe).parent)

    return configured or ""

def check_ffmpeg(ffmpeg_bin: str = ""):
    folder = configure_ffmpeg(ffmpeg_bin)
    ffmpeg = exe_path("ffmpeg")
    ffprobe = exe_path("ffprobe")
    if not folder or not Path(ffmpeg).exists():
        raise RuntimeError(
            "Không tìm thấy FFmpeg. Vào tab 'Cài đặt / Hướng dẫn', chọn đúng thư mục BIN chứa ffmpeg.exe.\\n"
            r"Ví dụ: C:\ffmpeg-9.0.1-essentials_build\bin"
        )
    return ffmpeg, ffprobe

def run_cmd(cmd: List[str]):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-3000:])
    return p.stdout

def ffprobe_duration(video: Path) -> float:
    ffprobe_exe = exe_path("ffprobe")
    if not ffprobe_exe or not Path(ffprobe_exe).exists():
        cap = cv2.VideoCapture(str(video))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        cap.release()
        return max(frames / fps, 1.0)
    cmd = [
        ffprobe_exe, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video)
    ]
    out = run_cmd(cmd).strip()
    return max(float(out), 1.0)

def extract_audio(video: Path, wav_path: Path):
    ffmpeg_exe, _ = check_ffmpeg(FFMPEG_BIN)
    cmd = [
        ffmpeg_exe, "-y", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(wav_path)
    ]
    run_cmd(cmd)

def cv2_imread_unicode(path: Path):
    """
    Windows-safe OpenCV read for paths containing Vietnamese / Unicode characters.
    cv2.imread() can fail on some Windows/OpenCV builds when the path contains
    characters such as Đ, ả, ề, etc.
    """
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def cv2_imwrite_unicode(path: Path, image, quality: int = 88) -> bool:
    """
    Windows-safe OpenCV write for Unicode paths.
    Encodes in memory first, then writes bytes with ndarray.tofile().
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ok, encoded = cv2.imencode(
            ".jpg",
            image,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
        )
        if not ok:
            return False
        encoded.tofile(str(path))
        return path.exists() and path.stat().st_size > 0
    except Exception:
        return False



def _load_face_cascade():
    try:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            return None
        return cascade
    except Exception:
        return None


FACE_CASCADE = _load_face_cascade()


def image_quality_metrics_from_frame(frame) -> dict:
    if frame is None:
        return {"score": 0.0, "sharpness": 0.0, "brightness": 0.0, "faces": 0, "face_area_ratio": 0.0}

    h, w = frame.shape[:2]
    if h <= 0 or w <= 0:
        return {"score": 0.0, "sharpness": 0.0, "brightness": 0.0, "faces": 0, "face_area_ratio": 0.0}

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    exposure_score = max(0.0, 1.0 - abs(brightness - 128.0) / 128.0)
    if brightness < 35 or brightness > 225:
        exposure_score *= 0.25

    faces = []
    if FACE_CASCADE is not None:
        try:
            scale = min(1.0, 720.0 / max(w, h))
            detect_img = gray if scale >= 1.0 else cv2.resize(gray, (max(1, int(w*scale)), max(1, int(h*scale))))
            detected = FACE_CASCADE.detectMultiScale(
                detect_img, scaleFactor=1.12, minNeighbors=5, minSize=(36, 36)
            )
            inv = 1.0 / scale
            faces = [(int(x*inv), int(y*inv), int(fw*inv), int(fh*inv)) for x, y, fw, fh in detected]
        except Exception:
            faces = []

    face_area_ratio = 0.0
    center_bonus = 0.0
    if faces:
        x, y, fw, fh = max(faces, key=lambda r: r[2] * r[3])
        face_area_ratio = min(1.0, (fw * fh) / float(w * h))
        cx = x + fw / 2.0
        cy = y + fh / 2.0
        dx = abs(cx - w / 2.0) / (w / 2.0)
        dy = abs(cy - h / 2.0) / (h / 2.0)
        center_bonus = max(0.0, 1.0 - (dx * 0.65 + dy * 0.35))

    sharp_component = min(sharpness, 1800.0)
    face_bonus = 0.0
    if faces:
        face_bonus = 380.0 + min(520.0, face_area_ratio * 2600.0) + center_bonus * 180.0

    score = sharp_component * 0.78 + exposure_score * 180.0 + face_bonus
    if sharpness < 55:
        score *= 0.30
    elif sharpness < 100:
        score *= 0.60

    return {
        "score": float(score),
        "sharpness": sharpness,
        "brightness": brightness,
        "faces": len(faces),
        "face_area_ratio": float(face_area_ratio),
    }


def image_quality_score(path: Path) -> float:
    img = cv2_imread_unicode(path)
    if img is None:
        return 0.0
    return image_quality_metrics_from_frame(img)["score"]


def choose_best_frame_near_time(video: Path, center_second: float, search_seconds: float = 1.8, candidate_count: int = 9):
    duration = ffprobe_duration(video)
    candidate_count = max(3, min(int(candidate_count), 15))
    search_seconds = max(0.4, min(float(search_seconds), 4.0))

    times = [
        center_second - search_seconds + (2 * search_seconds) * i / (candidate_count - 1)
        for i in range(candidate_count)
    ]

    cap = cv2.VideoCapture(str(video))
    best = None
    best_metrics = None
    best_time = None

    for sec in times:
        sec = max(0.0, min(float(sec), max(duration - 0.02, 0.0)))
        cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        metrics = image_quality_metrics_from_frame(frame)
        if best is None or metrics["score"] > best_metrics["score"]:
            best = frame.copy()
            best_metrics = metrics
            best_time = sec

    cap.release()
    return best, best_time, best_metrics


def extract_frames(video: Path, out_dir: Path, count: int, prefix: str) -> List[Dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = ffprobe_duration(video)
    start = dur * 0.025
    end = dur * 0.975
    if end <= start:
        start, end = 0, dur

    if count <= 1:
        times = [(start + end) / 2]
    else:
        times = [start + (end - start) * (i / max(count - 1, 1)) for i in range(count)]

    frames = []
    for idx, target_sec in enumerate(times):
        frame, chosen_sec, metrics = choose_best_frame_near_time(
            video, target_sec, search_seconds=1.25, candidate_count=7
        )
        if frame is None:
            continue

        h, w = frame.shape[:2]
        max_w = 1920
        if w > max_w:
            scale = max_w / w
            frame = cv2.resize(frame, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_LANCZOS4)

        fp = out_dir / f"{prefix}_{idx:03d}_{int((chosen_sec or target_sec)*1000):010d}.jpg"
        if not cv2_imwrite_unicode(fp, frame, quality=95):
            continue

        frames.append({
            "path": str(fp),
            "video": video.name,
            "second": float(chosen_sec if chosen_sec is not None else target_sec),
            "quality": float(metrics["score"] if metrics else image_quality_score(fp)),
            "sharpness": float(metrics["sharpness"] if metrics else 0.0),
            "faces": int(metrics["faces"] if metrics else 0),
            "face_area_ratio": float(metrics["face_area_ratio"] if metrics else 0.0),
        })
    return frames

def file_to_data_url(path: Path) -> str:
    mime = "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"

def strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()

def parse_json_lenient(text: str) -> dict:
    """
    Parse model JSON defensively.
    GPT-compatible gateways sometimes return:
    - markdown code fences
    - text before/after JSON
    - missing commas / unescaped quotes
    json-repair handles most recoverable formatting errors.
    """
    raw = strip_code_fences(text or "")
    attempts = [raw]

    m = re.search(r"\{.*\}", raw, re.S)
    if m and m.group(0) != raw:
        attempts.append(m.group(0))

    last_error = None
    for candidate in attempts:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception as e:
            last_error = e

        try:
            repaired = repair_json(candidate, return_objects=True)
            if isinstance(repaired, dict):
                return repaired
            if isinstance(repaired, str):
                obj = json.loads(repaired)
                if isinstance(obj, dict):
                    return obj
        except Exception as e:
            last_error = e

    raise ValueError(f"Không thể parse JSON từ model. Lỗi cuối: {last_error}")


def extract_chat_content(response, label: str = "API") -> str:
    """
    Robustly extract assistant text from OpenAI-compatible gateways.

    Supports:
    - SDK object: response.choices[0].message.content
    - dict-like response: response["choices"][0]["message"]["content"]
    - content arrays where each item has text/content
    - gateways that unexpectedly return None/empty choices

    Raises a clear error instead of "'NoneType' object is not subscriptable".
    """
    if response is None:
        raise RuntimeError(f"{label}: API returned None (empty response object).")

    # SDK/object style
    try:
        choices = getattr(response, "choices", None)
        if choices:
            first = choices[0]
            msg = getattr(first, "message", None)
            if msg is not None:
                content = getattr(msg, "content", None)
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, str):
                            parts.append(item)
                        elif isinstance(item, dict):
                            t = item.get("text") or item.get("content")
                            if t:
                                parts.append(str(t))
                        else:
                            t = getattr(item, "text", None) or getattr(item, "content", None)
                            if t:
                                parts.append(str(t))
                    joined = "\n".join(parts).strip()
                    if joined:
                        return joined
    except Exception:
        pass

    # Dict style
    if isinstance(response, dict):
        choices = response.get("choices")
        if choices:
            first = choices[0] or {}
            msg = first.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        t = item.get("text") or item.get("content")
                        if t:
                            parts.append(str(t))
                joined = "\n".join(parts).strip()
                if joined:
                    return joined

    # Last-resort serialization to aid debugging.
    try:
        if hasattr(response, "model_dump"):
            raw = response.model_dump()
        elif hasattr(response, "dict"):
            raw = response.dict()
        else:
            raw = str(response)
    except Exception:
        raw = repr(response)

    preview = str(raw)
    if len(preview) > 4000:
        preview = preview[:4000] + "...[truncated]"

    raise RuntimeError(
        f"{label}: API response did not contain usable choices/message/content.\n\n"
        f"Response preview:\n{preview}"
    )

def html_escape(s: str) -> str:
    import html
    return html.escape(s or "")

def format_story_sentences(text: str) -> str:
    """Keep one sentence or dialogue turn per paragraph in exported stories."""
    pieces = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        # A model may use a single newline instead of a blank line.
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            start = 0
            for match in re.finditer(r'[.!?]+[”"’]?\s+(?=[“"A-Z])', line):
                previous_word = re.search(r"\b([A-Za-z]+)$", line[start:match.start()].rstrip())
                if previous_word and previous_word.group(1).lower() in {"mr", "mrs", "ms", "dr", "st", "jr", "sr"}:
                    continue
                sentence = line[start:match.start()].strip() + match.group().rstrip()
                if sentence:
                    pieces.append(sentence)
                start = match.end()
            remaining = line[start:].strip()
            if remaining:
                pieces.append(remaining)
    return "\n\n".join(pieces)

def chapter_body_only(body: str, number: int) -> str:
    """Discard a chapter heading repeated inside model prose; exporter owns headings."""
    body = str(body or "").strip()
    return re.sub(
        rf"\A(?:\#{{1,6}}\s*)?(?:\*\*)?chapter\s*{number}\s*[-:–—]\s*[^\n]+\n*",
        "", body, count=1, flags=re.I,
    ).strip()

def paragraphize(text: str) -> str:
    text = format_story_sentences(text)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        paras = [text]
    return "\n\n".join(
        f'<p style="margin:0 0 1.25em 0">{html_escape(p)}</p>' for p in paras if p
    )

def choose_best_frame(
    frames: List[Dict],
    start_global: float,
    end_global: float,
    used_paths: Optional[set] = None
) -> Optional[Dict]:
    used_paths = used_paths or set()
    candidates = [
        f for f in frames
        if start_global <= f.get("global_second", 0) <= end_global
        and f.get("path") not in used_paths
    ]
    if not candidates:
        candidates = [f for f in frames if f.get("path") not in used_paths]
    if not candidates:
        return None

    with_faces = [f for f in candidates if int(f.get("faces", 0) or 0) > 0]
    pool = with_faces if with_faces else candidates
    return max(
        pool,
        key=lambda x: (
            float(x.get("quality", 0) or 0),
            float(x.get("sharpness", 0) or 0),
            float(x.get("face_area_ratio", 0) or 0),
        )
    )


def make_social_thumbnail(src_path: Path, out_path: Path, width: int = 290, height: int = 290) -> Path:
    """Resize then crop a square below 300px, centered on the detected face."""
    size = max(64, min(int(width), 299))
    with Image.open(src_path) as source:
        img = source.convert("RGB")
    cx, cy = img.width / 2, img.height / 2
    if FACE_CASCADE is not None:
        try:
            gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
            scale = min(1.0, 640.0 / max(img.size))
            small = cv2.resize(gray, (max(1, round(img.width * scale)),
                                      max(1, round(img.height * scale))))
            faces = FACE_CASCADE.detectMultiScale(small, scaleFactor=1.1,
                                                  minNeighbors=5, minSize=(24, 24))
            if len(faces):
                x, y, w, h = max(faces, key=lambda box: int(box[2]) * int(box[3]))
                cx, cy = (x + w / 2) / scale, (y + h / 2) / scale
        except Exception:
            pass
    lanczos = getattr(Image, "Resampling", Image).LANCZOS
    ratio = size / min(img.size)
    resized = img.resize((max(size, round(img.width * ratio)),
                          max(size, round(img.height * ratio))), lanczos)
    left = max(0, min(resized.width - size, round(cx * ratio - size / 2)))
    top = max(0, min(resized.height - size, round(cy * ratio - size / 2)))
    result = resized.crop((left, top, left + size, top + size))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(out_path, "JPEG", quality=92, optimize=True)
    return out_path


def find_person_thumbnail_frame(videos: List[Path], timeline: List[Dict], out_path: Path) -> Optional[Dict]:
    """Search additional evenly spaced moments when the normal frame scan found no face."""
    if FACE_CASCADE is None:
        return None
    best_frame, best_metrics, best_video, best_second = None, None, None, 0.0
    for video, entry in zip(videos, timeline):
        duration = float(entry.get("duration", 0) or 0)
        if duration <= 0:
            continue
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            cap.release()
            continue
        try:
            # Bounded scan: avoid decoding the whole video or exhausting CPU/RAM.
            for i in range(24):
                second = duration * (i + .5) / 24
                cap.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                h, w = frame.shape[:2]
                if max(h, w) > 960:
                    ratio = 960 / max(h, w)
                    frame = cv2.resize(frame, (round(w * ratio), round(h * ratio)))
                metrics = image_quality_metrics_from_frame(frame)
                if metrics["faces"] and (best_metrics is None or metrics["score"] > best_metrics["score"]):
                    best_frame, best_metrics, best_video, best_second = frame.copy(), metrics, video, second
        finally:
            cap.release()
    if best_frame is None or not cv2_imwrite_unicode(out_path, best_frame, quality=95):
        return None
    return {"path": str(out_path), "video": best_video.name, "second": best_second,
            "quality": best_metrics["score"], "faces": best_metrics["faces"]}


def make_story_cover(src_path: Path, out_path: Path) -> Path:
    """Create the website's 760x400 cover without cutting off the source image."""
    from PIL import ImageEnhance, ImageFilter
    width, height = 760, 400
    with Image.open(src_path) as source:
        picture = source.convert("RGB")
    try:
        lanczos = Image.Resampling.LANCZOS
    except AttributeError:
        lanczos = Image.LANCZOS
    ratio = max(width / picture.width, height / picture.height)
    background = picture.resize((max(width, round(picture.width * ratio)),
                                 max(height, round(picture.height * ratio))), lanczos)
    left = (background.width - width) // 2
    top = (background.height - height) // 2
    background = background.crop((left, top, left + width, top + height))
    background = ImageEnhance.Brightness(background.filter(ImageFilter.GaussianBlur(16))).enhance(0.55)
    foreground = picture.copy()
    foreground.thumbnail((width, height), lanczos)
    background.paste(foreground, ((width - foreground.width) // 2,
                                  (height - foreground.height) // 2))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    background.save(out_path, "JPEG", quality=93, optimize=True)
    return out_path


def normalize_story_html_for_publish(html_text: str) -> str:
    """
    Conservative cleanup before sending content to SmartTraffic.
    Keeps only the simple HTML we generate, removes control chars and
    normalizes page-break markers onto their own lines.
    """
    if not html_text:
        return ""

    # Remove control characters except tab/newline/carriage return.
    html_text = "".join(
        ch for ch in html_text
        if ch in "\t\n\r" or ord(ch) >= 32
    )

    # Keep the text marker as a standalone line so it survives HTML cleanup.
    html_text = html_text.replace("\r\n", "\n").replace("\r", "\n")
    html_text = re.sub(r"\s*\{\{nextpage\}\}\s*", "\n\n{{nextpage}}\n\n", html_text)
    html_text = re.sub(r"\s*<!--\s*nextpage\s*-->\s*", "\n\n<!--nextpage-->\n\n", html_text, flags=re.I)

    # Avoid accidental NUL / BOM.
    html_text = html_text.replace("\x00", "").replace("\ufeff", "")
    return html_text.strip()


def compact_meta_text(value, max_len: int) -> str:
    value = "" if value is None else str(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_len]



def count_words(text: str) -> int:
    if not text:
        return 0
    # Good enough for English prose generated by this tool.
    return len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE))


def transcript_slice_for_time(transcript: str, start_second: float, end_second: float, padding: float = 45.0) -> str:
    """
    Select transcript lines whose global timestamp falls around a chapter's time window.
    Expected prefix format: [000123.45s] ...
    """
    lo = max(0.0, float(start_second) - padding)
    hi = float(end_second) + padding
    out = []
    for line in (transcript or "").splitlines():
        m = re.match(r"\[(\d+(?:\.\d+)?)s\]", line)
        if not m:
            continue
        t = float(m.group(1))
        if lo <= t <= hi:
            out.append(line)
    return "\n".join(out)



def image_to_api_data_url(path: Path, max_width: int = 768, quality: int = 70) -> str:
    """
    Make a much smaller JPEG specifically for Vision API upload.
    This does NOT alter the original local frame used later for thumbnail/chapter images.
    """
    img = Image.open(path).convert("RGB")
    try:
        lanczos = Image.Resampling.LANCZOS
    except AttributeError:
        lanczos = Image.LANCZOS

    max_width = max(320, min(int(max_width), 1280))
    quality = max(45, min(int(quality), 90))

    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, int(img.height * ratio))), lanczos)

    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{data}"


def is_transient_provider_error(exc_or_text) -> bool:
    s = str(exc_or_text).lower()
    transient_terms = [
        "service_unavailable",
        "service unavailable",
        "overloaded",
        "upstream provider",
        "internal error",
        "temporarily unavailable",
        "timeout",
        "timed out",
        "gateway timeout",
        "502",
        "503",
        "504",
        "api_error",
        "rate limit",
        "429",
    ]
    return any(term in s for term in transient_terms)



@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    video: str
    global_start: float
    global_end: float


class StoryPipeline:
    def __init__(self, cfg: dict, log_cb=None, progress_cb=None):
        self.cfg = cfg
        self.log = log_cb or (lambda x: None)
        self.progress = progress_cb or (lambda v, s="": None)
        configure_ffmpeg(cfg.get("ffmpeg_bin", ""))
        self.client = OpenAI(
            api_key=cfg["vilao_api_key"].strip(),
            base_url=cfg["vilao_base_url"].strip().rstrip("/"),
        )
        self.work_dir: Optional[Path] = None
        self.transcript_segments: List[TranscriptSegment] = []
        self.frames: List[Dict] = []
        self.story_json: Optional[dict] = None
        self.source_videos: List[Path] = []
        self.video_timeline: List[Dict] = []
        self.two_video_mode = False
        self.script_only = False
        self.audio_unavailable = False
        self.run_started_at = time.perf_counter()
        self.step_started_at = None

    def _fmt_seconds(self, seconds: float) -> str:
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _log(self, s):
        elapsed = self._fmt_seconds(time.perf_counter() - self.run_started_at)
        now = time.strftime("%H:%M:%S")
        self.log(f"[{now}] [+{elapsed}] {s}")

    def _step_start(self, name: str):
        self.step_started_at = time.perf_counter()
        self._log(f"START: {name}")

    def _step_end(self, name: str):
        if self.step_started_at is None:
            self._log(f"DONE: {name}")
            return
        took = self._fmt_seconds(time.perf_counter() - self.step_started_at)
        self._log(f"DONE: {name} | took {took}")
        self.step_started_at = None

    def transcribe_videos(self, videos: List[Path]) -> Tuple[str, List[Dict]]:
        """Fast transcription path: transcribe video files directly, avoiding WAV extraction."""
        self._step_start("Transcription")
        whisper_name = self.cfg.get("whisper_model", "small").strip() or "small"
        whisper_device = self.cfg.get("whisper_device", "cpu").strip().lower() or "cpu"
        if whisper_device not in ("cpu", "cuda"):
            whisper_device = "cpu"

        compute_type = "int8" if whisper_device == "cpu" else "float16"
        beam_size = max(1, min(5, int(self.cfg.get("fast_whisper_beam_size", 1))))
        skip_audio_extract = bool(self.cfg.get("skip_audio_extract", True))
        # Windows WebDAV drives can crash the native video decoder. Feed
        # Whisper a WAV stored on the local disk instead of the Z: video.
        if os.name == "nt" and videos and str(videos[0].drive).upper() not in ("C:", "D:"):
            skip_audio_extract = False

        self._log(
            f"Loading faster-whisper: {whisper_name} | device={whisper_device} | "
            f"compute={compute_type} | beam={beam_size} | direct_video={skip_audio_extract}"
        )
        cumulative = 0.0
        transcript_lines = []
        timeline = []

        for i, video in enumerate(videos, 1):
            self.progress(10 + i / max(len(videos), 1) * 25, f"Transcribing {video.name}")
            self._log(f"Transcribing {i}/{len(videos)}: {video.name}")
            duration = ffprobe_duration(video)

            temp_wav = None
            source_path = video
            if not skip_audio_extract:
                local_audio_root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VideoStoryPublisher" / PROFILE_ID / "audio"
                temp_wav = local_audio_root / f"{uuid.uuid4().hex}.wav"
                temp_wav.parent.mkdir(parents=True, exist_ok=True)
                extract_audio(video, temp_wav)
                source_path = temp_wav

            started = time.perf_counter()
            try:
                # A native CTranslate2/MKL crash cannot be caught in-process.
                # Isolate it so an access violation only fails this video.
                with _WHISPER_LOCK:
                    from types import SimpleNamespace
                    diagnostic_root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VideoStoryPublisher" / PROFILE_ID / "whisper_logs"
                    diagnostic_root.mkdir(parents=True, exist_ok=True)
                    output_file = diagnostic_root / f"transcript_{uuid.uuid4().hex}.json"
                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    attempts = [(whisper_name, False)]
                    if whisper_device == "cpu":
                        attempts.append(("base", True))
                    for candidate, safe_cpu in attempts:
                        output_file.unlink(missing_ok=True)
                        args = [sys.executable, "-X", "faulthandler", "-c", _WHISPER_CHILD,
                                str(source_path), candidate, whisper_device, compute_type,
                                str(beam_size), str(output_file)]
                        child_env = os.environ.copy()
                        if safe_cpu:
                            # Constrain CPU dispatch and avoid MKL's packed
                            # GEMM path only in the isolated child process.
                            child_env.update(CT2_FORCE_CPU_ISA="GENERIC", CT2_USE_MKL="0",
                                             CT2_PACKED_GEMM="0", OMP_NUM_THREADS="1",
                                             MKL_NUM_THREADS="1", CT2_VERBOSE="1")
                            self._log("Whisper lỗi; thử lại model base với CPU GENERIC, tắt MKL.")
                        try:
                            process = subprocess.run(args, capture_output=True, text=True,
                                                     errors="replace", timeout=1200, env=child_env)
                        except subprocess.TimeoutExpired as exc:
                            raise RuntimeError(f"Whisper quá thời gian 20 phút: {video.name}") from exc
                        if process.returncode == 0 and output_file.is_file():
                            data = json.loads(output_file.read_text(encoding="utf-8"))
                            output_file.unlink(missing_ok=True)
                            segments = [SimpleNamespace(**entry) for entry in data]
                            break
                        details = (process.stderr or process.stdout or "").strip()[-3000:]
                        error_file = diagnostic_root / f"whisper_error_{uuid.uuid4().hex[:8]}.txt"
                        error_file.write_text(
                            f"Video: {video}\nModel: {candidate}\nCPU GENERIC/no MKL: {safe_cpu}\n"
                            f"Exit code: {process.returncode}\n{details}\n",
                            encoding="utf-8")
                        self._log(f"Whisper {candidate} lỗi (exit {process.returncode}); chi tiết: "
                                  f"{error_file}")
                    else:
                        raise RuntimeError(f"Whisper crash trên video {video.name}; "
                                           f"xem file whisper_error trong {diagnostic_root}")
            except RuntimeError as e:
                msg = str(e)
                if "mkl_malloc" in msg.lower() or "failed to allocate memory" in msg.lower():
                    raise RuntimeError(
                        "Whisper/MKL thiếu bộ nhớ khi xử lý video. Đóng các bản tool chạy song song, "
                        "đóng ứng dụng ngốn RAM và thử 1 luồng video. Chi tiết: " + msg
                    ) from e
                if "cublas" in msg.lower() or "cudnn" in msg.lower() or "cuda" in msg.lower():
                    raise RuntimeError(
                        "Whisper đang cố dùng CUDA nhưng máy thiếu thư viện NVIDIA CUDA/cuBLAS. "
                        "Đặt Whisper device = cpu rồi chạy lại.\n\n" + msg
                    ) from e
                raise
            finally:
                if temp_wav:
                    try:
                        temp_wav.unlink()
                    except Exception:
                        pass

            for seg in segments:
                txt = (seg.text or "").strip()
                if not txt:
                    continue
                gs = cumulative + float(seg.start)
                ge = cumulative + float(seg.end)
                self.transcript_segments.append(
                    TranscriptSegment(float(seg.start), float(seg.end), txt, video.name, gs, ge)
                )
                transcript_lines.append(
                    f"[{gs:09.2f}s] [{video.name} {seg.start:07.2f}-{seg.end:07.2f}] {txt}"
                )

            timeline.append({
                "video": video.name,
                "global_start": cumulative,
                "global_end": cumulative + duration,
                "duration": duration,
            })
            cumulative += duration
            self._log(
                f"DONE transcript {i}/{len(videos)} | "
                f"{self._fmt_seconds(time.perf_counter()-started)}"
            )

        self._step_end("Transcription")
        return "\n".join(transcript_lines), timeline

    def sample_frames(self, videos: List[Path], timeline: List[Dict]):
        """Extract more candidates for short clips to maximize image quality."""
        self._step_start("Frame extraction")
        base_per_video = max(4, int(self.cfg.get("frames_per_video", 12)))
        workers = max(1, min(8, int(self.cfg.get("frame_workers", 4))))
        frame_dir = self.work_dir / "frames"
        all_frames = []

        def job(i_video):
            i, video = i_video
            duration = float(timeline[i].get("duration", 0) or 0)
            if duration <= 20:
                per_video = max(base_per_video, 30)
            elif duration <= 45:
                per_video = max(base_per_video, 24)
            else:
                per_video = max(base_per_video, 18)

            self._log(f"Frame scan {video.name} | duration={duration:.1f}s | candidate slots={per_video}")
            frs = extract_frames(video, frame_dir, per_video, f"v{i+1:04d}")
            offset = float(timeline[i].get("global_start", 0) or 0)
            for f in frs:
                f["global_second"] = offset + float(f.get("second", 0) or 0)
            return i, frs

        if len(videos) > 1 and workers > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(videos))) as ex:
                futures = [ex.submit(job, (i, v)) for i, v in enumerate(videos)]
                done = 0
                for fut in as_completed(futures):
                    _, frs = fut.result()
                    all_frames.extend(frs)
                    done += 1
                    self.progress(36 + done / len(videos) * 14, f"Frames {done}/{len(videos)}")
        else:
            for i, video in enumerate(videos):
                _, frs = job((i, video))
                all_frames.extend(frs)
                self.progress(36 + (i+1) / len(videos) * 14, f"Frames {i+1}/{len(videos)}")

        all_frames.sort(key=lambda x: x.get("global_second", 0))
        self.frames = all_frames
        self._step_end("Frame extraction")
        return all_frames

    def _vision_model_name(self) -> str:
        vision = self.cfg.get("vision_model", "").strip()
        writer = self.cfg.get("writer_model", "").strip()
        use_writer = bool(self.cfg.get("use_writer_for_vision", False))
        return writer if use_writer and writer else vision

    def check_model_access(self):
        """Fail before transcription/frame extraction when a model is unavailable."""
        writer = self.cfg.get("writer_model", "").strip()
        vision = self._vision_model_name()
        has_script = bool(self.cfg.get("input_script", "").strip())
        if not writer or (not vision and not has_script):
            raise RuntimeError("Hãy nhập Writer model và Vision model trong tab Vilao AI.")
        for label, model in (("Vision", vision), ("Writer", writer)):
            if label == "Vision" and has_script:
                self._log("Có kịch bản: bỏ qua kiểm tra Vision model và phân tích Vision.")
                continue
            if label == "Writer" and model == vision:
                continue
            try:
                # A successful HTTP response confirms permission. Some reasoning
                # models legitimately return empty content with tiny token budgets.
                self.client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "Reply OK."}],
                    max_tokens=24,
                )
                self._log(f"Vilao {label}: access OK ({model})")
            except Exception as exc:
                if "403" in str(exc) or "FORBIDDEN" in str(exc).upper():
                    raise RuntimeError(
                        f"Vilao chưa cấp quyền dùng {label} model '{model}' cho API key này. "
                        "Hãy đăng ký model trên Vilao hoặc thay bằng model có quyền truy cập "
                        "trong tab Vilao AI; bấm 'Test model access' để kiểm tra."
                    ) from exc
                raise RuntimeError(f"Vilao {label} model '{model}' không dùng được: {exc}") from exc

    def analyze_with_vision(self, transcript: str, timeline: List[Dict]) -> dict:
        """
        V15: Folder-safe Vision pipeline.

        Instead of sending one huge request containing dozens of images, split
        frames into small batches, analyze each batch, then merge the batch
        analyses in one final TEXT-ONLY request.

        This dramatically reduces payload size and avoids 504/upstream-overloaded
        errors on Vilao/provider gateways.
        """
        self._step_start("Vision analysis")

        max_frames = max(6, int(self.cfg.get("max_vision_frames", 36)))
        batch_size = max(2, min(10, int(self.cfg.get("vision_batch_size", 6))))
        retry_count = max(1, min(8, int(self.cfg.get("vision_retry_count", 4))))
        retry_delay = max(2, int(self.cfg.get("vision_retry_delay", 12)))
        api_width = max(320, min(1280, int(self.cfg.get("vision_image_width", 768))))
        api_quality = max(45, min(90, int(self.cfg.get("vision_image_quality", 70))))

        # Remove stale/missing frame paths before Vision.
        existing_frames = []
        missing_count = 0
        for fr in self.frames:
            p = Path(fr.get("path", ""))
            if p.exists() and p.is_file() and p.stat().st_size > 0:
                existing_frames.append(fr)
            else:
                missing_count += 1

        if missing_count:
            self._log(
                f"WARNING: skipped {missing_count} missing/invalid frame files before Vision."
            )

        self.frames = existing_frames

        # Balanced frame quota across both source videos when two-video mode is active.
        if self.two_video_mode and len(self.source_videos) >= 2:
            selected = []
            quotas = [max_frames // 2, max_frames - max_frames // 2]
            for video, quota in zip(self.source_videos[:2], quotas):
                group = [f for f in self.frames if f.get("video") == video.name]
                group = sorted(group, key=lambda x: x.get("quality", 0), reverse=True)
                selected.extend(group[:quota])
        else:
            selected = sorted(self.frames, key=lambda x: x.get("quality", 0), reverse=True)[:max_frames]

        selected = sorted(selected, key=lambda x: x.get("global_second", 0))

        if not selected:
            raise RuntimeError(
                "Không có frame hợp lệ để gửi Vision. Nếu đường dẫn video/folder có "
                "ký tự tiếng Việt/Unicode, V22 đã sửa lỗi này; hãy chạy lại bằng V22."
            )

        selected_model = self._vision_model_name()

        # Full transcript is too large for each batch. We use nearby transcript lines.
        transcript_lines = transcript.splitlines()

        def transcript_near_batch(batch, padding_seconds=150.0, max_chars=26000):
            times = [float(x.get("global_second", 0)) for x in batch]
            lo = max(0.0, min(times) - padding_seconds)
            hi = max(times) + padding_seconds
            out = []
            for line in transcript_lines:
                mt = re.match(r"\[(\d+(?:\.\d+)?)s\]", line)
                if not mt:
                    continue
                t = float(mt.group(1))
                if lo <= t <= hi:
                    out.append(line)
            joined = "\n".join(out)
            if len(joined) > max_chars:
                lines2 = joined.splitlines()
                step = max(1, len(lines2) // 800)
                joined = "\n".join(lines2[::step][:800])
            return joined

        batches = [
            selected[i:i + batch_size]
            for i in range(0, len(selected), batch_size)
        ]

        self._log(
            f"Vision folder-safe mode | model={selected_model} | "
            f"frames={len(selected)} | batches={len(batches)} | "
            f"batch_size={batch_size} | API image={api_width}px q{api_quality}"
        )

        batch_results = []
        vision_workers = max(1, min(3, int(self.cfg.get("vision_parallel_workers", 2))))
        result_lock = threading.Lock()
        completed_batches = {"n": 0}

        def run_vision_batch(batch_index, batch):
            t0 = min(float(f.get("global_second", 0)) for f in batch)
            t1 = max(float(f.get("global_second", 0)) for f in batch)
            nearby_transcript = transcript_near_batch(batch)

            prompt = f"""
Analyze this SMALL chronological batch from a longer serialized video story.
Batch: {batch_index}/{len(batches)}
Time range: {t0:.2f}s - {t1:.2f}s
Nearby transcript:
{nearby_transcript}

Return STRICT JSON only with keys: batch, time_start, time_end, characters, events, visual_notes, uncertainties.
Do not invent unsupported events. Keep chronology. Use neutral role labels if names are uncertain.
""".strip()

            content = [{"type": "text", "text": prompt}]
            approx_image_bytes = 0
            for fr in batch:
                data_url = image_to_api_data_url(Path(fr["path"]), max_width=api_width, quality=api_quality)
                approx_image_bytes += len(data_url)
                content.append({"type": "text", "text": f"Frame time={fr['global_second']:.2f}s source={fr['video']}"})
                content.append({"type": "image_url", "image_url": {"url": data_url}})

            self._log(
                f"Vision batch {batch_index}/{len(batches)} | {len(batch)} frames | "
                f"~{approx_image_bytes/1024/1024:.2f} MB"
            )

            last_err = None
            for attempt in range(1, retry_count + 1):
                try:
                    if attempt > 1:
                        wait_s = retry_delay * (2 ** (attempt - 2))
                        self._log(f"Vision batch {batch_index} retry {attempt}/{retry_count} after {wait_s}s")
                        time.sleep(wait_s)
                    rsp = self.client.chat.completions.create(
                        model=selected_model,
                        messages=[
                            {"role": "system", "content": "Analyze video frames accurately. Return one valid JSON object only."},
                            {"role": "user", "content": content},
                        ],
                        temperature=0.1,
                        max_tokens=3000,
                    )
                    raw = extract_chat_content(rsp, f"Vilao Vision batch {batch_index}")
                    try:
                        obj = parse_json_lenient(raw)
                    except Exception:
                        repair_rsp = self.client.chat.completions.create(
                            model=selected_model,
                            messages=[
                                {"role": "system", "content": "Repair to valid JSON only."},
                                {"role": "user", "content": raw[:18000]},
                            ],
                            temperature=0,
                            max_tokens=3000,
                        )
                        obj = parse_json_lenient(extract_chat_content(repair_rsp, "Vision JSON repair"))
                    with result_lock:
                        completed_batches["n"] += 1
                        d = completed_batches["n"]
                        self.progress(50 + d / max(len(batches), 1) * 15, f"Vision batch {d}/{len(batches)}")
                    self._log(f"DONE Vision batch {batch_index}/{len(batches)}")
                    return batch_index, obj
                except Exception as e:
                    last_err = e
                    self._log(f"Vision batch {batch_index} attempt {attempt} failed: {e}")
                    if attempt >= retry_count or not is_transient_provider_error(e):
                        break
            raise RuntimeError(f"Vision batch {batch_index} failed: {last_err}")

        if len(batches) > 1 and vision_workers > 1:
            self._log(f"Vision batches parallel workers={vision_workers}")
            temp = []
            with ThreadPoolExecutor(max_workers=min(vision_workers, len(batches))) as ex:
                futures = [ex.submit(run_vision_batch, i+1, b) for i, b in enumerate(batches)]
                for fut in as_completed(futures):
                    temp.append(fut.result())
            temp.sort(key=lambda x: x[0])
            batch_results = [x[1] for x in temp]
        else:
            for i, b in enumerate(batches, 1):
                _, obj = run_vision_batch(i, b)
                batch_results.append(obj)

        # The Writer's architecture call already merges visual evidence with
        # the complete transcript. Avoid a second, slow Vision text merge.
        events = []
        characters = []
        for batch in batch_results:
            if not isinstance(batch, dict):
                continue
            for ch in batch.get("characters", []) if isinstance(batch.get("characters"), list) else []:
                if ch not in characters:
                    characters.append(ch)
            batch_events = batch.get("events", [])
            if not isinstance(batch_events, list):
                batch_events = [batch_events] if batch_events else []
            for item in batch_events:
                if isinstance(item, dict):
                    events.append(item)
                elif item:
                    events.append({
                        "start_second": float(batch.get("time_start", 0) or 0),
                        "end_second": float(batch.get("time_end", 0) or 0),
                        "description": str(item),
                    })
        analysis = {
            "working_title": self.source_videos[0].stem if self.source_videos else "Video Story",
            "characters": characters,
            "summary": "\n".join(json.dumps(b, ensure_ascii=False)[:5000] for b in batch_results),
            "events": events,
            "ending": str(events[-1].get("description", "")) if events else "",
            "uncertainties": [],
            "video_batches": batch_results,
        }
        self._log("Vision evidence ready; Writer will merge it into Story Architecture.")
        self._step_end("Vision analysis")
        return analysis

    def build_inferred_video_script(self, analysis: dict, transcript: str) -> str:
        """Put audio and Vision observations in one auditable script for Writer."""
        lines = ["VIDEO SCRIPT — inferred from video audio and visible frames.",
                 "Only the following observed details are established; uncertain details remain uncertain."]
        characters = analysis.get("characters") or []
        if characters:
            lines.extend(["", "CHARACTERS / IDENTIFIABLE ROLES:",
                          json.dumps(characters, ensure_ascii=False)])
        events = [event for event in analysis.get("events", []) if isinstance(event, dict)]
        def event_second(event):
            try:
                return float(event.get("start_second", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        if events:
            lines.extend(["", "VISIBLE EVENTS IN CHRONOLOGICAL ORDER:"])
            for event in sorted(events, key=event_second):
                description = str(event.get("description") or event.get("event") or "").strip()
                if description:
                    lines.append(f"[{event_second(event):.1f}s] {description}")
        elif analysis.get("summary"):
            lines.extend(["", "VISUAL OBSERVATIONS:", str(analysis["summary"])[:10000]])
        if transcript.strip():
            lines.extend(["", "SPOKEN WORDS / TRANSCRIPT:", transcript[:16000]])
        uncertainties = analysis.get("uncertainties") or []
        if uncertainties:
            lines.extend(["", "UNCERTAINTIES (DO NOT PRESENT AS OBSERVED FACT):",
                          json.dumps(uncertainties, ensure_ascii=False)])
        if len(lines) <= 2:
            raise RuntimeError("Vision và transcript không cung cấp đủ dữ liệu để viết kịch bản từ video.")
        return "\n".join(lines).strip() + "\n"

    def _new_writer_client(self):
        """Create a fresh API client for each parallel writer job."""
        return OpenAI(
            api_key=self.cfg["vilao_api_key"].strip(),
            base_url=self.cfg["vilao_base_url"].strip().rstrip("/"),
            timeout=180.0,
            max_retries=0,
        )

    def _writer_json_call(self, label: str, prompt: str, max_tokens: int = 5000, temperature: float = 0.4) -> dict:
        """Robust GPT/Vilao JSON call with retries and fresh client per attempt."""
        retries = max(2, min(5, int(self.cfg.get("chapter_retry_count", 3) or 3)))
        last_error = None

        for attempt in range(1, retries + 1):
            try:
                client = self._new_writer_client()
                self._log(f"{label}: API attempt {attempt}/{retries}")
                rsp = client.chat.completions.create(
                    model=self.cfg["writer_model"].strip(),
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Follow the user's story rules below for plot, style and continuity. "
                                "This automated run explicitly requests the entire story in one run; "
                                "create the architecture first, then continue without waiting for approval. "
                                "Return STRICT valid JSON matching the schema in the user request.\n\n"
                                + self.cfg["story_prompt_text"]
                                + (
                                    "\n\nAPPROVED VIDEO SCRIPT — LOCKED SOURCE FOR THE HOOK. "
                                    "The chapter scenes and caption must match these characters, "
                                    "actions, relationships and reveal:\n"
                                    + self.cfg["input_script"].strip()
                                    if self.cfg.get("input_script", "").strip() else ""
                                )
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                raw = extract_chat_content(rsp, label)
                obj = parse_json_lenient(raw)
                if not isinstance(obj, dict):
                    raise RuntimeError(f"{label}: parsed response is not a JSON object")
                return obj
            except Exception as e:
                last_error = e
                self._log(f"WARNING {label} attempt {attempt} failed: {e}")
                if attempt < retries:
                    delay = min(18, 3 * (2 ** (attempt - 1)))
                    time.sleep(delay)

        raise RuntimeError(f"{label} failed after {retries} attempts: {last_error}")

    def _chapter_source_payload(self, analysis: dict, transcript: str, plan: List[Dict]) -> List[Dict]:
        chapter_sources = []
        for idx, ch in enumerate(plan, 1):
            start_s = float(ch.get("start_second", 0) or 0)
            end_s = float(ch.get("end_second", start_s + 1) or (start_s + 1))
            excerpt = transcript_slice_for_time(transcript, start_s, end_s, padding=45.0)
            if len(excerpt) > 10000:
                lines = excerpt.splitlines()
                step = max(1, len(lines) // 320)
                excerpt = "\n".join(lines[::step][:320])
            chapter_sources.append({
                "number": int(ch.get("number", idx) or idx),
                "title": str(ch.get("title") or ""),
                "start_second": start_s,
                "end_second": end_s,
                "summary": str(ch.get("summary") or ""),
                "source_video": str(ch.get("source_video") or ""),
                "transcript_excerpt": excerpt,
            })
        return chapter_sources

    def _write_chapter_batch(self, analysis: dict, batch: List[Dict], prior_chapters=None) -> List[Dict]:
        nums = [int(x["number"]) for x in batch]
        global_context = {
            "working_title": analysis.get("working_title", ""),
            "characters": analysis.get("characters", []),
            "summary": analysis.get("summary", ""),
            "events": analysis.get("events", []),
            "ending": analysis.get("ending", ""),
            "story_architecture": analysis.get("story_architecture", {}),
            "uncertainties": analysis.get("uncertainties", []),
            # V21.3: keep the user-supplied script attached to every chapter
            # batch so the Writer expands *this exact script* into chapters.
            # Without this the Writer only sees the 16 KB summary slice and
            # drifts away from the supplied hook / characters.
            "approved_video_script": analysis.get("approved_video_script", ""),
            "inferred_video_script": analysis.get("video_script", ""),
        }
        prompt = f"""
Write ONLY Chapters {nums[0]}-{nums[-1]} of one serialized story.

GLOBAL STORY CONTEXT:
{json.dumps(global_context, ensure_ascii=False)}

CHAPTER SOURCE MATERIAL:
{json.dumps(batch, ensure_ascii=False)}

ALREADY WRITTEN (keep facts and chronology consistent):
{json.dumps(prior_chapters or [], ensure_ascii=False)}

STRICT REQUIREMENTS:
- Return EXACTLY {len(batch)} chapters: {nums}.
- Each chapter should be 500-800 words and never more than 900 words.
- Write the chapter body entirely as the SAME first-person narrator's natural story to another person.
- Put each narrative sentence and each spoken line in its own paragraph, with one blank line between paragraphs. Keep a natural first-person story rhythm, not screenplay labels.
- Never narrate private thoughts or offscreen events unless this narrator explains how they later learned them.
- Begin Chapter 1 directly after the video hook; keep the architecture's plot and ending consistent.
- The video supplies the hook and known facts; expand its fictional story according to the selected prompt.
- If source_video is present, obey it strictly.
- V21.3: when 'approved_video_script' is non-empty in GLOBAL STORY CONTEXT,
  treat it as the SOURCE OF TRUTH. Every character name, location, relationship,
  event and dialogue in the script must appear in the chapters exactly as the
  script states. Do not invent new names, do not contradict the script, and do
  not change the genre / era / outcome. The story architecture and chapter
  summaries may add drama, but every chapter body MUST stay inside the world
  the script already established.
- Every chapter title must be unique, dramatic, curiosity-driven, and specific to its events.
- Never use a generic title such as "Chapter 3" or "The Beginning".
- Do not put "Chapter N" inside the title field.
- Preserve character identities, relationships, motivations and chronology.
- Give the protagonist meaningful decisions, earned reveals, and a resolved final payoff.
- Do not summarize; write full narrative prose with scenes, reactions and transitions.
- Return valid JSON only.

JSON SCHEMA:
{{
  "chapters": [
    {{
      "number": {nums[0]},
      "title": "Compelling title",
      "start_second": 0.0,
      "end_second": 0.0,
      "body": "about 500 words"
    }}
  ]
}}
""".strip()
        obj = self._writer_json_call(
            f"Writer chapters {nums[0]}-{nums[-1]}",
            prompt,
            max_tokens=5200,
            temperature=0.42,
        )
        chapters = obj.get("chapters") or []
        return [x for x in chapters if isinstance(x, dict)]

    def _write_one_chapter(self, analysis: dict, source: Dict, continuity: str = "") -> Dict:
        n = int(source.get("number", 0) or 0)
        prompt = f"""
Write ONLY Chapter {n} of a serialized story.

GLOBAL CONTEXT:
{json.dumps({
    "working_title": analysis.get("working_title", ""),
    "characters": analysis.get("characters", []),
    "summary": analysis.get("summary", ""),
    "ending": analysis.get("ending", ""),
    "story_architecture": analysis.get("story_architecture", {}),
    "approved_video_script": analysis.get("approved_video_script", ""),
    "inferred_video_script": analysis.get("video_script", ""),
}, ensure_ascii=False)}

SOURCE FOR CHAPTER {n}:
{json.dumps(source, ensure_ascii=False)}

CONTINUITY FROM NEARBY CHAPTERS:
{continuity[-1800:]}

REQUIREMENTS:
- Write exactly one Chapter {n}.
- 500-800 words, maximum 900; preserve the architecture and nearby continuity.
- Keep the SAME first-person narrator. Put one narrative sentence or one spoken line per paragraph, separated by one blank line; no screenplay labels.
- Unique click-enticing title tied to actual events.
- Do not include "Chapter {n}" in the title field.
- Expand the video hook into fiction with earned events, proactive characters and a complete payoff.
- V21.3: when 'approved_video_script' is non-empty, treat it as the SOURCE OF TRUTH.
  Every character name, location, relationship, event and dialogue in the script
  must appear in the chapters exactly as the script states. Do not invent new
  names, do not contradict the script, and do not change the genre / era / outcome.
- Return STRICT JSON only:
{{"chapter": {{"number": {n}, "title": "...", "start_second": {float(source.get('start_second',0) or 0)}, "end_second": {float(source.get('end_second',0) or 0)}, "body": "..."}}}}
""".strip()
        obj = self._writer_json_call(
            f"Writer emergency Chapter {n}",
            prompt,
            max_tokens=3000,
            temperature=0.4,
        )
        ch = obj.get("chapter")
        if not isinstance(ch, dict):
            arr = obj.get("chapters") or []
            ch = arr[0] if arr and isinstance(arr[0], dict) else None
        if not isinstance(ch, dict) or not str(ch.get("body") or "").strip():
            raise RuntimeError(f"Chapter {n}: API returned no usable chapter")
        ch["number"] = n
        ch.setdefault("start_second", source.get("start_second", 0))
        ch.setdefault("end_second", source.get("end_second", 0))
        return ch

    def _story_metadata(self, analysis: dict, chapter_titles: List[str]) -> dict:
        prompt = f"""
Create metadata for an English serialized drama article.

SOURCE ANALYSIS:
{json.dumps({
    "working_title": analysis.get("working_title", ""),
    "summary": analysis.get("summary", ""),
    "characters": analysis.get("characters", []),
}, ensure_ascii=False)}

CHAPTER TITLES:
{json.dumps(chapter_titles, ensure_ascii=False)}

Return STRICT JSON only:
{{
  "title": "engaging article title",
  "slug": "url-slug",
  "meta_title": "SEO title",
  "meta_description": "SEO description",
  "keywords": "keyword1, keyword2, keyword3",
  "summary": "short article summary"
}}
""".strip()
        try:
            return self._writer_json_call("Writer metadata", prompt, max_tokens=1500, temperature=0.35)
        except Exception as e:
            self._log(f"WARNING metadata API failed; using local fallback: {e}")
            title = str(analysis.get("working_title") or "A Story No One Saw Coming").strip()
            summary = str(analysis.get("summary") or "").strip()
            return {
                "title": title,
                "slug": safe_slug(title),
                "meta_title": title[:90],
                "meta_description": summary[:240],
                "keywords": "serialized drama, story",
                "summary": summary,
            }

    @staticmethod
    def _normalize_architecture(raw: dict) -> Optional[dict]:
        """Accept common JSON outline shapes without discarding a valid plan."""
        if not isinstance(raw, dict):
            return None
        containers = [raw]
        for key in ("story_architecture", "architecture", "outline", "plan"):
            nested = raw.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
        for container in containers:
            chapters = None
            for key in ("chapters", "chapter_plan", "chapter_architecture", "chapter_outline"):
                value = container.get(key)
                if isinstance(value, dict):
                    value = list(value.values())
                if isinstance(value, list) and value:
                    chapters = value
                    break
            if not isinstance(chapters, list) or not 6 <= len(chapters) <= 10:
                continue
            fixed = []
            for i, item in enumerate(chapters, 1):
                if isinstance(item, str):
                    item = {"title": item}
                if not isinstance(item, dict):
                    break
                item = dict(item)
                title = next((str(item.get(k)).strip() for k in
                              ("title", "chapter_title", "name", "heading")
                              if item.get(k)), "")
                if not title:
                    break
                item["title"] = re.sub(rf"^chapter\s*{i}\s*[-:–—]*\s*", "", title, flags=re.I).strip() or title
                item["number"] = i
                item["function"] = str(item.get("function") or item.get("purpose") or "")
                item["main_development"] = str(item.get("main_development") or
                                               item.get("development") or item.get("summary") or "")
                item["end_pull"] = str(item.get("end_pull") or item.get("cliffhanger") or "")
                fixed.append(item)
            if len(fixed) != len(chapters):
                continue
            return {
                "story_title": str(raw.get("story_title") or raw.get("title") or
                                   container.get("story_title") or container.get("title") or "").strip(),
                "narrator": str(raw.get("narrator") or container.get("narrator") or "").strip(),
                "chapters": fixed,
                "major_reveals": raw.get("major_reveals") or container.get("major_reveals") or [],
                "climax": raw.get("climax") or container.get("climax") or "",
                "payoff": raw.get("payoff") or container.get("payoff") or "",
            }
        return None

    def _make_story_architecture(self, analysis: dict, transcript: str) -> dict:
        """Plan the story from the video hook before any chapters are written."""
        source = {
            "approved_video_script": self.cfg.get("input_script", "").strip(),
            "inferred_video_script": analysis.get("video_script", ""),
            "working_title": analysis.get("working_title", ""),
            "characters": analysis.get("characters", []),
            "summary": "" if analysis.get("video_script") else analysis.get("summary", ""),
            "events": analysis.get("events", []),
            "uncertainties": analysis.get("uncertainties", []),
        }
        prompt = (
            "Create the STORY ARCHITECTURE first for this video hook. Choose 6 to 10 "
            "chapters according to the actual plot density. The tool is running the full "
            "story now, so proceed automatically without asking for approval. "
            "Choose ONE narrator who was directly involved in the hook and will tell "
            "EVERY chapter in first person. Return only JSON with story_title, narrator, "
            "chapters (array of objects with number, "
            "title, function, main_development, end_pull), major_reveals, climax, payoff. "
            "If an approved video script is given, it is the locked canon for the hook; "
            "do not change its cast, events or reveal. Keep Chapter 1 tied to the video's "
            "first scene; design a complete fictional "
            "continuation with setup for each reveal.\n\nVIDEO ANALYSIS:\n"
            + json.dumps(source, ensure_ascii=False)
            + "\n\nVIDEO TRANSCRIPT:\n" + transcript[:24000]
        )
        raw = self._writer_json_call("Story architecture", prompt, max_tokens=4000)
        _safe_write_text(
            self.work_dir / "story_architecture_raw.json",
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        architecture = self._normalize_architecture(raw)
        if architecture is None:
            self._log("Dàn ý trả về không đúng 6-10 chapter; yêu cầu model sửa cấu trúc một lần.")
            repair_prompt = (
                "Correct this story outline into one JSON object with 6-10 chapters. "
                "Keep the approved hook and plot facts. Keep one first-person narrator. "
                "Return keys story_title, narrator, chapters, "
                "major_reveals, climax, payoff. Each item in chapters needs title, function, "
                "main_development and end_pull. No prose outside JSON.\n\n"
                "ORIGINAL OUTLINE:\n" + json.dumps(raw, ensure_ascii=False)[:18000]
                + "\n\nAPPROVED HOOK:\n" + str(source.get("approved_video_script") or
                                               source.get("summary") or "")[:5000]
            )
            try:
                repaired = self._writer_json_call("Story architecture repair", repair_prompt,
                                                  max_tokens=2700, temperature=0.1)
                _safe_write_text(
                    self.work_dir / "story_architecture_repair.json",
                    json.dumps(repaired, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                architecture = self._normalize_architecture(repaired)
            except Exception as exc:
                self._log(f"WARNING: Không sửa được dàn ý bằng API: {exc}")
        if architecture is None:
            self._log("WARNING: Dùng khung 7 chương để tiếp tục; writer sẽ viết theo hook và prompt đã nhập.")
            beats = [
                ("The Moment Everything Changed", "Establish the exact video hook"),
                ("The Accusation", "Show the immediate conflict and relationships"),
                ("What No One Noticed", "Find a credible clue already seeded in the hook"),
                ("A New Question", "Escalate the conflict through the protagonist's choices"),
                ("The Hidden Motive", "Reveal a supported motive with earlier setup"),
                ("The Choice", "Let the protagonist take decisive action"),
                ("What Came After", "Resolve the conflict and deliver a complete payoff"),
            ]
            architecture = {
                "story_title": str(raw.get("story_title") or raw.get("title") or
                                   analysis.get("working_title") or "A Story Unfolds"),
                "narrator": str(raw.get("narrator") or "").strip(),
                "chapters": [
                    {"number": i, "title": title, "function": purpose,
                     "main_development": purpose, "end_pull": "Continue the planned story"}
                    for i, (title, purpose) in enumerate(beats, 1)
                ],
                "major_reveals": raw.get("major_reveals") or [],
                "climax": raw.get("climax") or "",
                "payoff": raw.get("payoff") or "",
            }
        _safe_write_text(
            self.work_dir / "story_architecture.json",
            json.dumps(architecture, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return architecture

    def write_story(self, analysis: dict, transcript: str) -> dict:
        """
        Write the architecture first, then write its 6-10 chapters in small batches.
        - Retry each batch independently.
        - Missing chapters are regenerated individually in parallel.
        - Short chapters are expanded individually instead of failing the whole article.
        """
        self._step_start("Story writing")

        architecture = self._make_story_architecture(analysis, transcript)
        analysis = dict(analysis, story_architecture=architecture)
        architecture_chapters = architecture["chapters"]
        chapter_count = len(architecture_chapters)
        plan = architecture_chapters
        # Assign each architecture chapter a corresponding video time window.
        if chapter_count:
            events = analysis.get("events") or []
            max_end = 0.0
            for e in events:
                try:
                    max_end = max(max_end, float(e.get("end_second", 0) or 0))
                except Exception:
                    pass
            if max_end <= 0:
                for line in transcript.splitlines():
                    mt = re.match(r"\[(\d+(?:\.\d+)?)s\]", line)
                    if mt:
                        max_end = max(max_end, float(mt.group(1)))
            if max_end <= 0 and self.video_timeline:
                max_end = max(float(x.get("global_end", 0) or 0) for x in self.video_timeline)
            if max_end <= 0:
                max_end = 80.0

            old_plan = list(plan)
            plan = []
            span = max_end / chapter_count
            for i in range(chapter_count):
                old = old_plan[i] if i < len(old_plan) and isinstance(old_plan[i], dict) else {}
                plan.append({
                    "number": i + 1,
                    "title": str(old.get("title") or ""),
                    "start_second": i * span,
                    "end_second": (i + 1) * span if i < chapter_count - 1 else max_end,
                    "summary": " | ".join(str(old.get(k) or "") for k in ("function", "main_development", "end_pull")),
                })

        # Spread chapter image windows across both source videos, preserving plot order.
        if self.two_video_mode and len(self.video_timeline) >= 2:
            first_count = (chapter_count + 1) // 2
            for i, old in enumerate(plan):
                video_index = 0 if i < first_count else 1
                video_chapters = first_count if video_index == 0 else chapter_count - first_count
                local_index = i if video_index == 0 else i - first_count
                tl = self.video_timeline[video_index]
                gs = float(tl.get("global_start", 0) or 0)
                ge = float(tl.get("global_end", gs) or gs)
                span = max(0.01, (ge - gs) / video_chapters)
                old["start_second"] = gs + span * local_index
                old["end_second"] = gs + span * (local_index + 1)
                old["source_video"] = self.source_videos[video_index].name
            self._log(f"2-video mapping: Video 1 -> {first_count} chapters; Video 2 -> {chapter_count-first_count} chapters")

        chapter_sources = self._chapter_source_payload(analysis, transcript, plan)
        batches = [chapter_sources[i:i + 2] for i in range(0, chapter_count, 2)]

        self.progress(70, f"Writing {chapter_count} chapters")
        fast_batches = bool(self.cfg.get("fast_writer_batches", True))
        self._log(f"Writer: {len(batches)} batches; "
                  f"{'2 parallel batches per wave' if fast_batches else 'sequential'}; retries enabled")

        collected = {}
        batch_errors = []
        api_started = time.perf_counter()

        def prior_context():
            return [
                {"number": n, "title": ch.get("title", ""),
                 "ending": str(ch.get("body") or "")[-1200:]}
                for n, ch in sorted(collected.items())[-3:]
            ]

        def receive_batch(batch, result=None, error=None):
            nums = [x["number"] for x in batch]
            if error is not None:
                batch_errors.append((nums, str(error)))
                self._log(f"WARNING batch Ch{nums[0]}-{nums[-1]} failed: {error}")
                return
            for ch in result:
                try:
                    n = int(ch.get("number", 0) or 0)
                except Exception:
                    continue
                if n in nums and str(ch.get("body") or "").strip():
                    collected[n] = ch
            self._log(f"Batch Ch{nums[0]}-{nums[-1]} finished | got {len(result)} chapter object(s)")

        if fast_batches:
            for wave in range(0, len(batches), 2):
                wave_batches = batches[wave:wave + 2]
                context = prior_context()
                with ThreadPoolExecutor(max_workers=len(wave_batches)) as ex:
                    futures = {ex.submit(self._write_chapter_batch, analysis, batch, context): batch
                               for batch in wave_batches}
                    for fut in as_completed(futures):
                        batch = futures[fut]
                        try:
                            receive_batch(batch, result=fut.result())
                        except Exception as exc:
                            receive_batch(batch, error=exc)
        else:
            for batch in batches:
                try:
                    receive_batch(batch, result=self._write_chapter_batch(analysis, batch, prior_context()))
                except Exception as exc:
                    receive_batch(batch, error=exc)

        self._log(
            f"Writer phase finished in {self._fmt_seconds(time.perf_counter()-api_started)} | "
            f"usable chapters={len(collected)}/{chapter_count}"
        )

        # Missing chapters: regenerate EACH missing chapter separately and in parallel.
        missing = [n for n in range(1, chapter_count + 1) if n not in collected]
        if missing:
            self._log(f"Missing chapters after batch phase: {missing}. Starting individual recovery calls...")
            with ThreadPoolExecutor(max_workers=min(4, len(missing))) as ex:
                futs = {}
                for n in missing:
                    source = chapter_sources[n - 1]
                    continuity = ""
                    if n - 1 in collected:
                        continuity += str(collected[n - 1].get("body") or "")[-900:]
                    if n + 1 in collected:
                        continuity += "\n" + str(collected[n + 1].get("body") or "")[:900]
                    futs[ex.submit(self._write_one_chapter, analysis, source, continuity)] = n
                for fut in as_completed(futs):
                    n = futs[fut]
                    try:
                        collected[n] = fut.result()
                        self._log(f"Recovered Chapter {n} successfully")
                    except Exception as e:
                        self._log(f"WARNING individual recovery Chapter {n} failed: {e}")

        # Final emergency sequential pass: never give up on the whole article because one chapter is missing.
        missing = [n for n in range(1, chapter_count + 1) if n not in collected]
        for n in missing:
            last = None
            for emergency_attempt in range(1, 3):
                try:
                    self._log(f"FINAL recovery Chapter {n} attempt {emergency_attempt}/2")
                    collected[n] = self._write_one_chapter(analysis, chapter_sources[n - 1], "")
                    last = None
                    break
                except Exception as e:
                    last = e
                    time.sleep(3 * emergency_attempt)
            if last is not None:
                raise RuntimeError(
                    f"Chapter {n} could not be generated after batch + parallel recovery + final retry. "
                    f"Last error: {last}"
                )

        chapters = [collected[n] for n in range(1, chapter_count + 1)]

        # Normalize number/time and titles.
        def clean_title(raw_title: str, number: int, body: str) -> str:
            title = (raw_title or "").strip()
            title = re.sub(rf"^chapter\s*{number}\s*[-:–—]*\s*", "", title, flags=re.I).strip()
            if not title or re.fullmatch(r"chapter\s*\d+|\d+", title, flags=re.I):
                words = re.findall(r"[A-Za-z0-9'’-]+", body or "")
                title = " ".join(words[:8]).title() if words else "The Secret No One Expected"
            return title[:100].strip()

        for i, ch in enumerate(chapters, 1):
            src = chapter_sources[i - 1]
            ch["number"] = i
            ch["start_second"] = float(src.get("start_second", 0) or 0)
            ch["end_second"] = float(src.get("end_second", 0) or 0)
            ch["title"] = clean_title(str(ch.get("title") or ""), i, str(ch.get("body") or ""))
            body = str(ch.get("body") or "").strip()
            # Chapter headings and page breaks are inserted by the tool only.
            body = re.sub(r"(?im)^\s*(?:\{\{nextpage\}\}|<!--nextpage-->)\s*$", "", body)
            body = re.sub(rf"(?i)^chapter\s*{i}\s*[-:–—]\s*[^\n]+\n*", "", body).strip()
            ch["body"] = body

        # Repair short chapters in parallel. This is optional and only runs when needed.
        short_nums = [i for i, ch in enumerate(chapters, 1) if count_words(str(ch.get("body") or "")) < 500]
        if short_nums:
            self._log(f"Short chapters detected {short_nums}; expanding them with individual API calls...")
            with ThreadPoolExecutor(max_workers=min(4, len(short_nums))) as ex:
                futs = {
                    ex.submit(self._write_one_chapter, analysis, chapter_sources[n - 1], str(chapters[n-2].get("body") or "")[-900:] if n > 1 else ""): n
                    for n in short_nums
                }
                for fut in as_completed(futs):
                    n = futs[fut]
                    try:
                        new_ch = fut.result()
                        if count_words(str(new_ch.get("body") or "")) > count_words(str(chapters[n-1].get("body") or "")):
                            new_ch["number"] = n
                            new_ch["start_second"] = chapter_sources[n-1]["start_second"]
                            new_ch["end_second"] = chapter_sources[n-1]["end_second"]
                            new_ch["title"] = clean_title(str(new_ch.get("title") or ""), n, str(new_ch.get("body") or ""))
                            chapters[n - 1] = new_ch
                            self._log(f"Expanded Chapter {n} successfully")
                    except Exception as e:
                        self._log(f"WARNING expansion Chapter {n} failed; keeping existing text: {e}")

        # Metadata is a small independent call; failure never loses the story.
        metadata = self._story_metadata(analysis, [ch.get("title", "") for ch in chapters])
        title = str(architecture.get("story_title") or metadata.get("title") or analysis.get("working_title") or "A Story No One Saw Coming").strip()
        story = {
            "title": title,
            "slug": safe_slug(str(metadata.get("slug") or title)),
            "meta_title": str(metadata.get("meta_title") or title),
            "meta_description": str(metadata.get("meta_description") or metadata.get("summary") or "")[:300],
            "keywords": str(metadata.get("keywords") or "serialized drama, story"),
            "summary": str(metadata.get("summary") or analysis.get("summary") or ""),
            "chapters": chapters,
            "story_architecture": architecture,
        }

        total_words = 0
        for i, ch in enumerate(chapters, 1):
            wc = count_words(str(ch.get("body") or ""))
            ch["word_count"] = wc
            total_words += wc
            self._log(f"Chapter {i}/{chapter_count} actual length: {wc:,} words")
        story["total_word_count"] = total_words
        self._log(f"Story complete: {chapter_count}/{chapter_count} chapters | {total_words:,} total words")

        self._step_end("Story writing")
        return story

    def _best_direct_chapter_frame(self, chapter: dict, chapter_index: int) -> Optional[Dict]:
        if not self.source_videos or not self.video_timeline:
            return None

        s = float(chapter.get("start_second", 0) or 0)
        e = float(chapter.get("end_second", s + 0.5) or (s + 0.5))
        mid = (s + e) / 2.0

        video = None
        tl = None
        for v, t in zip(self.source_videos, self.video_timeline):
            gs = float(t.get("global_start", 0) or 0)
            ge = float(t.get("global_end", gs) or gs)
            if gs <= mid <= ge + 0.001:
                video, tl = v, t
                break
        if video is None:
            return None

        offset = float(tl.get("global_start", 0) or 0)
        duration = float(tl.get("duration", 0) or 0)
        ls = max(0.0, s - offset)
        le = min(max(ls + 0.05, e - offset), duration)

        samples = 18
        times = [ls + (le - ls) * i / max(samples - 1, 1) for i in range(samples)]

        cap = cv2.VideoCapture(str(video))
        best = None
        best_metrics = None
        best_sec = None

        for sec in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            metrics = image_quality_metrics_from_frame(frame)
            score = float(metrics.get("score", 0) or 0)
            if float(metrics.get("sharpness", 0) or 0) < 70:
                score *= 0.35
            if int(metrics.get("faces", 0) or 0) > 0:
                score += 250.0
            metrics = dict(metrics)
            metrics["final_score"] = score
            if best is None or score > best_metrics["final_score"]:
                best = frame.copy()
                best_metrics = metrics
                best_sec = sec

        cap.release()
        if best is None:
            return None

        h, w = best.shape[:2]
        if w > 1920:
            scale = 1920 / w
            best = cv2.resize(best, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LANCZOS4)

        out_dir = self.work_dir / "chapter_images"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"chapter_{chapter_index:02d}.jpg"
        if not cv2_imwrite_unicode(out_path, best, quality=97):
            return None

        return {
            "path": str(out_path),
            "video": video.name,
            "local_second": float(best_sec or 0),
            "sharpness": float(best_metrics.get("sharpness", 0) or 0),
            "faces": int(best_metrics.get("faces", 0) or 0),
            "quality": float(best_metrics.get("final_score", 0) or 0),
        }

    def attach_local_images(self, story: dict):
        # Image data from the writer is never used in a chapter, including in
        # video-only runs where frame extraction returned no usable frames.
        for ch in story.get("chapters", []):
            ch.pop("image_local", None)
            ch.pop("image_url", None)
        if not self.frames:
            self._log("WARNING: Không có khung hình dùng được để tạo ảnh bìa.")
            return story
        # Prefer a detected person anywhere in the video, including opening/ending frames.
        people = [f for f in self.frames if int(f.get("faces", 0) or 0) > 0]
        if not people and self.source_videos:
            try:
                found = find_person_thumbnail_frame(
                    self.source_videos, self.video_timeline,
                    self.work_dir / "thumbnail_person_frame.jpg")
                if found:
                    people = [found]
                    self._log(f"Ảnh bìa: phát hiện người ở {found['video']} ({found['second']:.1f}s)")
            except Exception as exc:
                self._log(f"Không thể quét thêm khuôn mặt; dùng khung hình sẵn có: {exc}")
        max_t = max(f["global_second"] for f in self.frames)
        middle = [f for f in self.frames if max_t * 0.1 <= f["global_second"] <= max_t * 0.9]
        pool = people or middle or self.frames
        if not people:
            self._log("WARNING: Không phát hiện khuôn mặt trong các khung đã lấy; dùng khung rõ nhất.")
        thumb = max(pool, key=lambda f: float(f.get("quality", 0) or 0))
        original_thumb = Path(thumb["path"])
        story["thumbnail_original_local"] = str(original_thumb)
        cover_path = make_social_thumbnail(original_thumb, self.work_dir / "thumbnail_website_290x290.jpg")
        story["thumbnail_local"] = str(cover_path)
        story["thumbnail_size"] = "290x290"
        self._log("Website thumbnail: 290x290px | ưu tiên có người, cắt vuông theo khuôn mặt")

        if bool(self.cfg.get("facebook_thumbnail_mode", True)):
            social_thumb = self.work_dir / "thumbnail_facebook_290x290.jpg"
            make_social_thumbnail(original_thumb, social_thumb)
            story["facebook_thumbnail_local"] = str(social_thumb)
            self._log("Facebook thumbnail: 290x290px | cắt vuông theo khuôn mặt")
        return story

    def upload_cloudinary(self, story: dict) -> dict:
        self._step_start("Image upload")
        cloud = self.cfg.get("cloudinary_cloud_name", "").strip()
        key = self.cfg.get("cloudinary_api_key", "").strip()
        secret = self.cfg.get("cloudinary_api_secret", "").strip()
        continue_on_error = bool(self.cfg.get("continue_if_image_upload_fails", True))
        workers = max(1, min(8, int(self.cfg.get("cloudinary_workers", 4))))

        if not (cloud and key and secret):
            self._log("Cloudinary not configured; keeping images local only.")
            story["image_upload_status"] = "skipped_not_configured"
            self._step_end("Image upload")
            return story

        if cloud.lower() in {"root", "default", "admin", "cloudinary"}:
            msg = f"Cloudinary cloud name '{cloud}' không hợp lệ."
            story["image_upload_status"] = "failed_invalid_cloud_name"
            story["image_upload_error"] = msg
            self._step_end("Image upload")
            if continue_on_error:
                return story
            raise RuntimeError(msg)

        if not CLOUDINARY_AVAILABLE:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "cloudinary"])

        import cloudinary
        import cloudinary.uploader
        cloudinary.config(cloud_name=cloud, api_key=key, api_secret=secret, secure=True)
        folder = f"video_story/{safe_slug(story.get('title','story'))}-{uuid.uuid4().hex[:10]}"
        self.progress(84, "Uploading images")

        tasks = []
        thumb_local = story.get("thumbnail_local")
        if thumb_local:
            tasks.append(("thumbnail", 0, thumb_local, "thumbnail"))

        def upload_one(task):
            kind, idx, path, public_id = task
            r = cloudinary.uploader.upload(
                path, folder=folder, public_id=public_id, overwrite=True, resource_type="image"
            )
            url = (r.get("secure_url") or "").strip()
            if not url.startswith("https://"):
                raise RuntimeError(f"Cloudinary did not return secure_url for {public_id}")
            return kind, idx, url

        try:
            self._log(f"Uploading {len(tasks)} images | workers={min(workers, max(len(tasks),1))}")
            results = []
            if len(tasks) > 1 and workers > 1:
                with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as ex:
                    futures = [ex.submit(upload_one, t) for t in tasks]
                    for fut in as_completed(futures):
                        results.append(fut.result())
            else:
                results = [upload_one(t) for t in tasks]

            for kind, idx, url in results:
                if kind == "thumbnail":
                    story["thumbnail_url"] = url
                    self._log(f"Thumbnail URL READY: {url}")

            story["image_upload_status"] = "success"
            self._step_end("Image upload")
            return story
        except Exception as e:
            msg = str(e)
            self._log(f"WARNING: Cloudinary upload failed: {msg}")
            story["image_upload_status"] = "failed"
            story["image_upload_error"] = msg
            self._step_end("Image upload")
            if continue_on_error:
                return story
            raise

    def build_blog_text(self, story: dict) -> str:
        """Plain TinyMCE story with the exact Theme Story page separator."""
        parts = []
        for idx, chapter in enumerate(story.get("chapters", []), 1):
            title = re.sub(rf"^chapter\s*{idx}\s*[-:–—]*\s*", "", str(chapter.get("title") or ""), flags=re.I).strip()
            parts.append(f"Chapter {idx} - {title}\n\n{format_story_sentences(chapter_body_only(chapter.get('body'), idx))}")
        return "\n\n{{nextpage}}\n\n".join(parts) + "\n"

    def write_facebook_caption(self, analysis: dict, story: dict, script: str) -> str:
        """Tell the hook as an in-world, first-person story with a true blog open loop."""
        chapter_context = [
            {"number": i, "title": ch.get("title", ""),
             "opening": str(ch.get("body") or "")[:500],
             "ending": str(ch.get("body") or "")[-450:]}
            for i, ch in enumerate(story.get("chapters", []), 1)
        ]
        source = {
            "approved_video_script": script,
            "video_hook_analysis": analysis.get("summary", ""),
            "story_title": story.get("title", ""),
            "story_architecture": story.get("story_architecture", {}),
            "written_chapter_context": chapter_context,
        }
        prompt = (
            "Write the final FACEBOOK CAPTION as an immersive FIRST-PERSON personal story, "
            "spoken by the character at the center of the hook. If an elderly woman is "
            "endangered, let HER tell it as 'I', 'my son', 'my granddaughter', etc. "
            "Identify the correct narrator from THIS story; never copy names, relationships "
            "or events from any other example.\n"
            "Use natural American English, about 180-300 words. Put EACH sentence or dialogue "
            "line in its own paragraph with one blank line before the next. Start inside the dangerous event, with the narrator's "
            "fear and physical experience. Then reveal the rescue, the unfair accusation and "
            "the phone/evidence only to the extent already shown in the approved video. "
            "Use brief dialogue and varied sentence lengths. Finish with an emotional open "
            "loop about a specific deeper stake that actually exists in the completed Blog. "
            "Tease the consequence; do not reveal the later payoff.\n"
            "This is prose told by a person, NOT a video synopsis or shooting script. "
            "Never say 'the screen went black', 'hard cut', 'the video ends', 'the audience', "
            "'the viewer', or describe a camera. A character may naturally mention a phone "
            "recording if that occurs in the story. Do not invent precise durations, secret "
            "money/house schemes, or other details unless supported by the approved script "
            "or completed Blog. No chapter titles, hashtags or post metadata. "
            "End EXACTLY with: Read the full story — link in the comments.\n"
            "Return STRICT JSON only: {\"caption\": \"first-person caption with \\n\\n between paragraphs\"}.\n\n"
            "STORY CONTEXT:\n" + json.dumps(source, ensure_ascii=False)
        )
        for attempt in range(2):
            caption = str(self._writer_json_call("Facebook caption", prompt, max_tokens=1400).get("caption") or "").strip()
            words = len(caption.split())
            pronouns = re.findall(r"\b(?:I|my|me|we|our)\b", caption, flags=re.I)
            opening = caption.split("\n\n", 1)[0]
            if (caption and 150 <= words <= 330 and caption.count("\n\n") >= 4
                    and len(pronouns) >= 4
                    and re.search(r"\b(?:I|my|me|we|our)\b", opening, flags=re.I)
                    and not re.search(r"screen went black|hard cut|the video ends|the viewer", caption, flags=re.I)):
                cta = "Read the full story — link in the comments."
                if not caption.endswith(cta):
                    caption = caption.rstrip().removesuffix("Read the full story - link in the comments.").rstrip()
                    caption += "\n\n" + cta
                return caption
            self._log(f"Caption chưa đúng giọng kể/độ dài (lần {attempt + 1}/2); viết lại.")
            prompt += ("\n\nREWRITE: First-person personal confession, 180-300 words, "
                       "one sentence or dialogue line per paragraph, each separated by a blank line; no scene or camera descriptions. "
                       "Keep the same established facts and open loop.")
        raise RuntimeError("Caption chưa đạt giọng kể ngôi thứ nhất; Blog.txt đã được lưu trong thư mục kết quả.")

    def build_html(self, story: dict, pagebreak_marker: Optional[str] = None) -> str:
        parts = []
        chapters = story.get("chapters", [])
        marker = pagebreak_marker or "{{nextpage}}"
        if marker not in ("{{nextpage}}", "<!--nextpage-->"):
            raise ValueError("Unsupported chapter marker")

        for idx, ch in enumerate(chapters):
            # Put the page-break immediately BEFORE Chapter 2, 3, 4...
            if idx > 0:
                parts.append(marker)
            n = ch.get("number", idx+1)
            raw_title = str(ch.get("title") or "").strip()
            raw_title = re.sub(rf"^chapter\s*{n}\s*[-:–—]*\s*", "", raw_title, flags=re.I).strip()
            if not raw_title:
                raw_title = "The Story Takes a Dangerous Turn"
            title = html_escape(raw_title)
            # V21.3-fix: keep inline !important styles on the chapter heading.
            # drama.viralstory.biz's story theme parses Heading + inline style to
            # detect chapter boundaries and build the chapter navigation bar
            # (Chapter 1 / 9 + progress). The {{nextpage}} marker (text) is the
            # correct marker here; the HTML comment variant hides the marker from
            # the theme's regex and breaks pagination.
            parts.append(
                '<h2 style="font-size:1.3em !important;font-weight:700 !important;'
                'line-height:1.35;margin:1.15em 0 .65em">'
                f'<strong style="font-weight:700 !important">Chapter {n} - {title}</strong>'
                '</h2>'
            )
            parts.append(paragraphize(chapter_body_only(ch.get("body", ""), n)))
        return "\n\n".join(parts)

    def verify_published_chapters(self, article_url: str, chapter_count: int) -> Optional[bool]:
        """Check the public Chapter 2 page; None means it could not be checked."""
        if not article_url or chapter_count < 2:
            return None
        parts = urlsplit(article_url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "c"]
        url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                         urlencode(query + [("c", "2")]), parts.fragment))
        try:
            response = requests.get(url, timeout=12)
            if response.status_code != 200:
                self._log(f"Không kiểm tra được Chương 2: HTTP {response.status_code} | {url}")
                return None
            html_text = response.text
            if re.search(r"\{\{nextpage\}\}", html_text, flags=re.I):
                self._log("CHƯA TÁCH CHƯƠNG: website đang hiển thị nguyên chữ {{nextpage}}.")
                return False
            headings = re.findall(r"<h[12]\b[^>]*>(.*?)</h[12]>", html_text, flags=re.I | re.S)
            titles = [re.sub(r"<[^>]+>", " ", heading).strip() for heading in headings]
            if any(re.search(r"\bChapter\s*1\s*[-:–—]", title, flags=re.I) for title in titles) and any(
                re.search(r"\bChapter\s*2\s*[-:–—]", title, flags=re.I) for title in titles
            ):
                self._log("CHƯA TÁCH CHƯƠNG: trang ?c=2 vẫn hiển thị cả Chương 1 và 2.")
                return False
            if any(re.search(r"\bChapter\s*2\s*(?:/|of|[-:–—])", title, flags=re.I) for title in titles):
                self._log(f"Đã xác nhận trang Chương 2: {url}")
                return True
            self._log(f"Không xác nhận được giao diện Chương 2; hãy mở URL để kiểm tra: {url}")
            return None
        except requests.RequestException as exc:
            self._log(f"Không truy cập được trang Chương 2 để kiểm tra: {exc}")
            return None

    def check_publish_site(self, headers: dict) -> None:
        """Confirm the configured site ID points to the intended host."""
        try:
            response = requests.get("https://apiv3.smarttraffic.app/api/publish/sites",
                                    headers={"Accept": "application/json", "Authorization": headers["Authorization"]},
                                    timeout=15)
            if response.status_code != 200:
                self._log(f"Không kiểm tra được danh sách site: HTTP {response.status_code}")
                return
            listed = response.json()
            if isinstance(listed, dict):
                listed = listed.get("data", listed.get("sites", []))
            if isinstance(listed, dict):
                listed = listed.get("items", [])
            if not isinstance(listed, list):
                return
            selected = next((site for site in listed if isinstance(site, dict)
                             and str(site.get("id")) == str(self.cfg.get("site_id"))), None)
            if not selected:
                raise RuntimeError(f"Site ID {self.cfg.get('site_id')} không thuộc danh sách site của API key.")
            status = str(selected.get("status") or "").strip().lower()
            if status and status != "active":
                raise RuntimeError(f"Site ID {self.cfg.get('site_id')} đang ở trạng thái {status}, không thể đăng bài.")
            actual_host = str(selected.get("host") or "").strip().lower().strip("/")
            configured_host = str(self.cfg.get("site_host") or "").strip().lower().strip("/")
            configured_host = re.sub(r"^https?://", "", configured_host)
            if actual_host and configured_host and actual_host != configured_host:
                raise RuntimeError(
                    f"Site ID {self.cfg.get('site_id')} trỏ đến {actual_host}, nhưng Site host đang nhập là "
                    f"{configured_host}. Hãy sửa Site ID hoặc Site host trước khi đăng."
                )
            theme = selected.get("theme") or selected.get("site_theme")
            if isinstance(theme, dict):
                theme = theme.get("name") or theme.get("slug")
            theme_name = str(theme).strip().lower() if theme else ""
            if theme_name and theme_name != "story":
                raise RuntimeError(
                    f"Site {actual_host} đang dùng theme '{theme_name}'. "
                    "Theo tài liệu API, CHỈ theme 'story' mới tự tách chapter theo menu. "
                    "Hãy vào Dashboard SmartTraffic, đổi theme site sang 'story' trước khi đăng."
                )
            if not theme_name:
                self._log(
                    f"Site ID {self.cfg.get('site_id')} | host={actual_host} | "
                    "API không trả theme. Tool vẫn tiếp tục; nếu bài không tách chapter hãy kiểm tra theme 'story' trên website."
                )
        except requests.RequestException as exc:
            self._log(f"Không kiểm tra được Site ID qua API: {exc}")

    # ============================================================
    # ADSCONEX / BLOGBIO — POST /api/posts mode=chapter
    # ============================================================
    def _adsconex_headers(self, token: str) -> dict:
        """Header Chrome de qua Cloudflare (thieu sec-ch-ua/Sec-Fetch bi chan 1010)."""
        base = str(self.cfg.get("adsconex_base_url") or "https://usjusticereport.cfx.bz/api").strip().rstrip("/")
        host = urlsplit(base).netloc or "usjusticereport.cfx.bz"
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
            "Referer": f"https://{host}/admin/api-docs",
            "Origin": f"https://{host}",
            "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="24", "Google Chrome";v="141"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def publish_adsconex(self, story: dict) -> dict:
        """Dang bai len Adsconex (nen Blogbio) o che do chapter.

        Server tu tach chuong theo marker 'CHAPTER N - Title' va tu tao series.
        """
        self._step_start("Adsconex publish")
        token = str(self.cfg.get("adsconex_api_key", "") or "").strip()
        if not token:
            raise RuntimeError("Adsconex API token đang trống. Nhập trong tab Adsconex.")

        base = str(self.cfg.get("adsconex_base_url") or "https://usjusticereport.cfx.bz/api").strip().rstrip("/")
        endpoint = f"{base}/posts"
        headers = self._adsconex_headers(token)

        chapters = story.get("chapters", []) or []
        if not chapters:
            raise RuntimeError("Truyện chưa có chapter nào để đăng dạng series.")

        content = build_adsconex_chapter_content(story)
        missing = [n for n in range(1, len(chapters) + 1)
                   if f"CHAPTER {n} - " not in content]
        if missing:
            raise RuntimeError(
                f"Content thiếu marker chapter {missing}; đã dừng đăng để tránh bài lỗi."
            )

        thumb = (story.get("thumbnail_url") or "").strip()
        payload = {
            "title": compact_meta_text(story.get("title") or "Untitled Story", 240),
            "content": content,
            "permalink": safe_slug(story.get("slug") or story.get("title") or "story"),
            "mode": "chapter",
            "skip_intro": True,
        }
        if thumb.startswith("https://"):
            payload["feature_image"] = thumb
            if bool(self.cfg.get("adsconex_apply_image_to_all", True)):
                payload["apply_image_to_all"] = True
        author = str(self.cfg.get("adsconex_author", "") or "").strip()
        if author:
            payload["author"] = author
        raw_category = str(self.cfg.get("adsconex_category", "") or "").strip()
        if raw_category.isdigit():
            payload["category"] = int(raw_category)
        seo_title = compact_meta_text(story.get("meta_title") or story.get("title") or "", 240)
        seo_desc = compact_meta_text(story.get("meta_description") or "", 500)
        keywords = compact_meta_text(story.get("keywords") or "", 500)
        if seo_title:
            payload["seo_title"] = seo_title
        if seo_desc:
            payload["seo_description"] = seo_desc
        if keywords:
            payload["seokeyword"] = keywords

        if self.work_dir:
            try:
                _safe_write_text(self.work_dir / "publish_payload_adsconex.json",
                                 json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

        self._log(
            f"Adsconex POST {endpoint} | chapters={len(chapters)} | "
            f"thumbnail={'YES' if 'feature_image' in payload else 'NO'} | "
            f"category={payload.get('category', 'none')}"
        )
        try:
            r = requests.post(endpoint, headers=headers, json=payload, timeout=180)
        except Exception as e:
            raise RuntimeError(f"Adsconex network error: {e}")

        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:2000]}

        if self.work_dir:
            try:
                _safe_write_text(self.work_dir / "publish_response_adsconex.json",
                                 json.dumps({"http_status": r.status_code, "response": data},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

        if r.status_code not in (200, 201):
            detail = json.dumps(data, ensure_ascii=False)[:800] if isinstance(data, (dict, list)) else str(data)[:800]
            raise RuntimeError(f"Adsconex HTTP {r.status_code}: {detail}")

        posts = data.get("posts") if isinstance(data, dict) else None
        if not isinstance(posts, list) or not posts:
            raise RuntimeError(
                "Adsconex không trả danh sách chapter. Response: "
                + (json.dumps(data, ensure_ascii=False)[:800] if isinstance(data, (dict, list)) else str(data)[:800])
            )
        site_host = self.cfg.get("adsconex_site_host", "")
        link = published_adsconex_link({"posts": posts}, site_host)
        if not link:
            first = posts[0] if isinstance(posts[0], dict) else {}
            link = str(first.get("link") or first.get("url") or "").strip()

        # Adsconex tao MOT BAI RIENG cho moi chapter (khong dung ?c=N nhu
        # SmartTraffic), nen liet ke link tung bai theo dung thu tu server tra.
        chapter_links = []
        for idx, post in enumerate(posts, 1):
            if not isinstance(post, dict):
                continue
            one = published_adsconex_link({"posts": [post]}, site_host)
            if one:
                chapter_links.append(f"Chapter {idx}: {one}")
        chapter_links_text_value = "\n".join(chapter_links) + ("\n" if chapter_links else "")

        self._log(f"Adsconex accepted | HTTP {r.status_code} | chapters={len(posts)} | url={link}")
        self._step_end("Adsconex publish")
        return {
            "url": link,
            "id": (posts[0] or {}).get("postId") if isinstance(posts[0], dict) else None,
            "chapters": posts,
            "chapter_links": chapter_links,
            "chapter_links_text": chapter_links_text_value,
            "count": len(posts),
            "provider": "Adsconex",
            "response": data,
        }

    def publish_current(self, story: dict) -> dict:
        """Chon net theo cai dat trong GUI: SmartTraffic hoac Adsconex."""
        provider = str(self.cfg.get("net_provider") or "SmartTraffic").strip().lower()
        if provider.startswith("ads"):
            return self.publish_adsconex(story)
        return self.publish_smarttraffic(story)

    def publish_smarttraffic(self, story: dict) -> dict:
        """Publish without ever silently dropping the thumbnail."""
        self._step_start("SmartTraffic publish")
        api_key = self.cfg.get("smarttraffic_api_key", "").strip()
        if not api_key:
            raise RuntimeError("SmartTraffic API key is empty.")

        thumb = (story.get("thumbnail_url") or "").strip()
        require_thumb = bool(self.cfg.get("require_thumbnail_before_publish", True))
        if require_thumb and not self.script_only and not thumb.startswith("https://"):
            raise RuntimeError(
                "Bài có video nhưng chưa có thumbnailUrl công khai. "
                "Hãy kiểm tra ảnh trích từ video và Cloudinary trước khi đăng."
            )

        endpoint = "https://apiv3.smarttraffic.app/api/publish/articles"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.check_publish_site(headers)
        # V21.3-fix: drama.viralstory.biz's story theme expects:
        #   - {{nextpage}} text marker (the HTML comment variant strips out and
        #     breaks pagination)
        #   - Inline !important styles on the <h2> chapter heading (the theme
        #     uses those style props to detect a chapter break and build the
        #     'Chapter X / Y' header, progress bar and sidebar navigation)
        # Both must be present. {{nextpage}} gets rendered into the theme's
        # pagination list; bare inline <h2> without it leaves chapters invisible.
        preferred = "{{nextpage}}"
        alternate = "<!--nextpage-->"

        def make_payload(marker: str, include_seo: bool) -> dict:
            payload = {
                "site_id": int(self.cfg["site_id"]),
                "category_id": int(self.cfg["category_id"]),
                "title": compact_meta_text(story.get("title") or "Untitled Story", 240),
                "content": normalize_story_html_for_publish(
                    self.build_html(story, pagebreak_marker=marker)
                ),
                "slug": safe_slug(story.get("slug") or story.get("title") or "story"),
                "status": "published",
            }
            # Thumbnail is intentionally kept in EVERY attempt.
            if thumb.startswith("https://"):
                payload["thumbnailUrl"] = thumb
            if include_seo:
                payload["meta_title"] = compact_meta_text(story.get("meta_title") or story.get("title") or "", 240)
                payload["meta_description"] = compact_meta_text(story.get("meta_description") or "", 500)
                payload["keywords"] = compact_meta_text(story.get("keywords") or "", 500)
            return payload

        attempts = [
            (preferred, True, "full / preferred marker"),
            (preferred, False, "core / preferred marker"),
            # V21.3: removed alternate-marker fallback attempts here. They used to
            # re-post the same story with the other page-break marker when the
            # first attempt's chapter verification failed, which silently
            # created -2/-3 duplicate slugs on SmartTraffic.
        ]

        last_error = None
        for attempt_no, (marker, include_seo, label) in enumerate(attempts, 1):
            payload = make_payload(marker, include_seo)
            chapter_count = len(story.get("chapters", []))
            content_lines = payload["content"].splitlines()
            if (
                payload["content"].count(marker) != chapter_count - 1
                or any(line.strip() != marker for line in content_lines if marker in line)
                or ("<!--nextpage-->" if marker == "{{nextpage}}" else "{{nextpage}}") in payload["content"]
            ):
                raise RuntimeError("Nội dung HTML gửi API chưa đúng định dạng phân chương; đã dừng đăng bài.")
            if self.work_dir:
                try:
                    _safe_write_text(
                        self.work_dir / f"publish_payload_attempt_{attempt_no}.json",
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
                    )
                except Exception:
                    pass

            self._log(
                f"SmartTraffic attempt {attempt_no}/{len(attempts)} | {label} | "
                f"chapters={len(story.get('chapters',[]))} | thumbnail={'YES' if 'thumbnailUrl' in payload else 'NO'}"
            )
            try:
                r = requests.post(endpoint, headers=headers, json=payload, timeout=120)
            except Exception as e:
                last_error = f"Network/request error: {e}"
                break

            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}

            if self.work_dir:
                try:
                    _safe_write_text(
                        self.work_dir / f"publish_response_attempt_{attempt_no}.json",
                        json.dumps({"http_status": r.status_code, "response": data}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

            if r.status_code in (200, 201):
                if isinstance(data, dict) and not data.get("url"):
                    nested = data.get("data")
                    if isinstance(nested, dict):
                        data["url"] = nested.get("url") or nested.get("article_url") or ""
                    if not data.get("url"):
                        host = str(self.cfg.get("site_host") or "").strip().strip("/")
                        host = re.sub(r"^https?://", "", host, flags=re.I)
                        if host:
                            data["url"] = f"https://{host}/article/{payload['slug']}"
                            self._log("API không trả URL; dựng đường dẫn từ Site host và slug để kiểm tra ?c=2.")
                self._log(
                    f"SmartTraffic API accepted | HTTP {r.status_code} | marker={marker} | "
                    f"thumbnail={'YES' if thumb.startswith('https://') else 'NO'} | url={data.get('url','')}"
                )
                # V21.3-fix: ATTEMPT 1 SUCCEEDED -> PUBLISH IS COMPLETE.
                # Stop the loop here. Do not iterate over remaining attempts;
                # doing so creates a -2/-3 slug on SmartTraffic and posts the
                # SAME story 2-4 times per click. Chapter-verification retry
                # is handled in a separate conditional below; it is the only
                # legitimate reason to publish a second time in this run.
                data["chapter_marker"] = marker
                data["chapter_verified"] = self.verify_published_chapters(
                    str(data.get("url") or ""), chapter_count
                )
                # V21.3: NEVER repost on chapter-verify failure. The previous
                # retries silently created -2/-3 slug duplicates on SmartTraffic
                # because every alternate-marker POST is a brand new article.
                # The user can manually edit the slug and rebuild later.
                if data["chapter_verified"] is False:
                    self._log(
                        "CẢNH BÁO: bài đã đăng nhưng website KHÔNG tách chương tự động. "
                        "Đã đăng 1 bài duy nhất (giữ nguyên); KHÔNG retry để tránh duplicate. "
                        "Hãy kiểm tra theme 'story' trên website hoặc sửa bài trong CMS."
                    )
                self._step_end("SmartTraffic publish")
                return data

            last_error = f"SmartTraffic HTTP {r.status_code}: {data}"
            self._log(last_error)
            if r.status_code in (400, 401, 403, 404, 429):
                break
            if r.status_code < 500:
                break
            if attempt_no < len(attempts):
                time.sleep(0.5)

        raise RuntimeError(f"{last_error}\n\nRequest/response debug đã được lưu trong output folder.")

    def _republish_with_alternate_marker(self, story: dict, marker: str, headers: dict) -> Optional[dict]:
        """V21.1: Re-post the same story with a different page-break marker so the
        story theme on the website can paginate even when the first marker fails."""
        endpoint = "https://apiv3.smarttraffic.app/api/publish/articles"
        slug = safe_slug(story.get("slug") or story.get("title") or "story")
        thumb = (story.get("thumbnail_url") or "").strip()

        def make_payload(include_seo: bool) -> dict:
            payload = {
                "site_id": int(self.cfg["site_id"]),
                "category_id": int(self.cfg["category_id"]),
                "title": compact_meta_text(story.get("title") or "Untitled Story", 240),
                "content": normalize_story_html_for_publish(
                    self.build_html(story, pagebreak_marker=marker)
                ),
                "slug": slug,
                "status": "published",
            }
            if thumb.startswith("https://"):
                payload["thumbnailUrl"] = thumb
            if include_seo:
                payload["meta_title"] = compact_meta_text(story.get("meta_title") or story.get("title") or "", 240)
                payload["meta_description"] = compact_meta_text(story.get("meta_description") or "", 500)
                payload["keywords"] = compact_meta_text(story.get("keywords") or "", 500)
            return payload

        chapter_count = len(story.get("chapters", []))
        for include_seo, label in [(True, "full SEO"), (False, "core")]:
            payload = make_payload(include_seo)
            self._log(f"RETRY với marker '{marker}' | {label}")
            try:
                r = requests.post(endpoint, headers=headers, json=payload, timeout=120)
            except Exception as e:
                self._log(f"RETRY network error: {e}")
                continue
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}
            if r.status_code in (200, 201):
                if isinstance(data, dict) and not data.get("url"):
                    nested = data.get("data")
                    if isinstance(nested, dict):
                        data["url"] = nested.get("url") or nested.get("article_url") or ""
                    if not data.get("url"):
                        host = str(self.cfg.get("site_host") or "").strip().strip("/")
                        host = re.sub(r"^https?://", "", host, flags=re.I)
                        if host:
                            data["url"] = f"https://{host}/article/{slug}"
                data["chapter_marker"] = marker
                verified = self.verify_published_chapters(str(data.get("url") or ""), chapter_count)
                data["chapter_verified"] = verified
                if verified is not False:
                    self._log(f"RETRY thành công với marker '{marker}' | {data.get('url','')}")
                    return data
                self._log(f"RETRY marker '{marker}' đăng OK nhưng vẫn chưa tách chapter.")
        return None

    def rename_published_video(self, video: Path, story: dict, result: dict, script: str = "") -> str:
        """Rename the original video only when this publish has a real numeric article ID."""
        provider = str(self.cfg.get("net_provider") or "SmartTraffic").strip().lower()
        if provider.startswith("ads"):
            link = published_adsconex_link(result, self.cfg.get("adsconex_site_host", ""))
        else:
            link = published_article_link(result, self.cfg.get("site_host", ""))
        if not link:
            self._log("Chưa nhận được ID bài viết từ API; giữ tên video, không đoán link từ slug.")
            return ""
        try:
            prompt = (
                "Write one short English Facebook video title based on the actual VIDEO HOOK. "
                "Make a specific curiosity gap without revealing the outcome or inventing facts. "
                "Write a fuller, natural title of about 110-140 characters when the hook has enough detail. "
                "Add 3 to 5 relevant, short English hashtags. The complete VIDEO FILENAME, including "
                "hashtags, full story article ID link and .mp4 extension, must stay at or below 219 characters; "
                "the application will trim the hook first if needed. "
                "No URLs or semicolons. Return JSON: {\"title\":\"...\",\"hashtags\":[\"#...\",\"#...\",\"#...\"]}.\n"
                + json.dumps({"video_script": script[:3500], "story_title": story.get("title", ""),
                              "caption_start": str(story.get("facebook_caption", ""))[:650]}, ensure_ascii=False)
            )
            generated = self._writer_json_call("Facebook video title", prompt, max_tokens=250)
            title = re.sub(r"[;#\r\n]+", " ", str(generated.get("title") or ""))
            title = re.sub(r"\s+", " ", title).strip()[:160].rstrip(" .")
            tags = generated.get("hashtags") or []
            if isinstance(tags, str):
                tags = tags.split()
            tags = ["#" + m.group(1) for tag in tags if (m := re.fullmatch(r"#?([A-Za-z0-9_]{2,25})", str(tag).strip()))]
            tags = list(dict.fromkeys(tags))[:5]
            if len(tags) < 3:
                tags = ["#story", "#drama", "#fullstory"]
            if not title:
                raise ValueError("AI returned an empty title")
        except Exception as exc:
            self._log(f"Không tạo được tên bằng AI ({exc}); dùng tiêu đề bài viết.")
            title = re.sub(r"[;#\r\n]+", " ", str(story.get("title") or video.stem)).strip()[:160]
            tags = ["#story", "#drama", "#fullstory"]
        post_text = f"{title} {' '.join(tags)}"
        phrase = choose_filename_phrase(self.cfg)
        exact_text = f"{post_text};{phrase} {link}"
        filename = safe_video_filename(post_text, link, video.suffix, phrase=phrase)
        self._log(f"Câu chèn tên file: {phrase}")
        self._log(f"Tên file mới: {len(filename)}/219 ký tự | {filename}")
        # On Windows, a mapped drive can reject a full path above MAX_PATH
        # even when its basename is below the requested 219 characters.
        # Leave a character for the terminating NUL (maximum path 259).
        path_limit = 259 - len(str(video.parent)) - 1
        if os.name == "nt" and path_limit < 219:
            self._log(f"Thư mục dài {len(str(video.parent))} ký tự; giới hạn tên video còn {path_limit} ký tự theo đường dẫn Windows.")
        first_limit = min(219, path_limit) if os.name == "nt" else 219
        attempts = list(dict.fromkeys(x for x in (first_limit, 180, 150, 120) if x <= first_limit))
        errors = []
        if not attempts:
            errors.append(f"Thư mục quá dài để chứa hook, hashtag và link ID: {video.parent}")
        for limit in attempts:
            try:
                filename = safe_video_filename(post_text, link, video.suffix, max_length=limit, phrase=phrase)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            destination = video.with_name(filename)
            try:
                if not video.is_file():
                    raise FileNotFoundError(f"Không tìm thấy video nguồn: {video}. Kiểm tra ổ mạng và xem video có bị chuyển/đổi tên bởi luồng khác không.")
                if video != destination:
                    if destination.exists():
                        error = f"Tên video đích đã tồn tại: {destination}"
                        errors.append(error)
                        self._log(error)
                        break
                    video.rename(destination)
                _safe_write_text(self.work_dir / "facebook_video_name.txt", exact_text + "\n", encoding="utf-8")
                self._log(f"Đã đổi tên video: {destination.name} | link ID: {link}")
                return str(destination)
            except OSError as exc:
                error = (f"Tên {len(filename)} ký tự | {type(exc).__name__} | "
                         f"winerror={getattr(exc, 'winerror', None)} | errno={exc.errno} | {exc}")
                errors.append(error)
                self._log(f"Không đổi được tên video: {error}")
                # WinError 3 can mean the destination is too long on a mapped
                # drive; only retry if the source and parent still exist.
                path_may_be_long = (getattr(exc, "winerror", None) == 3 and
                                    len(str(destination)) >= 259 and video.is_file() and video.parent.is_dir())
                if not path_may_be_long and getattr(exc, "winerror", None) != 206 and exc.errno != errno.ENAMETOOLONG:
                    break
        _safe_write_text(self.work_dir / "rename_error.txt",
                         f"Video: {video}\nBài đã đăng: {link}\n" + "\n".join(errors) + "\n",
                         encoding="utf-8")
        return ""

    def run(self, input_sources, auto_publish=False):
        if not self.cfg.get("vilao_api_key", "").strip():
            raise RuntimeError("Vilao API key is empty.")
        if not self.cfg.get("story_prompt_text", "").strip():
            raise RuntimeError("Prompt đang trống. Hãy điền trong tab Kịch bản & Prompt.")
        if isinstance(input_sources, (str, Path)):
            input_sources = [Path(input_sources)]
        input_sources = list(input_sources or [])
        script = self.cfg.get("input_script", "").strip()
        if not script and not input_sources:
            raise RuntimeError("Hãy nhập kịch bản hoặc chọn một video.")
        if len(input_sources) > 1:
            raise RuntimeError("Chỉ chọn một video cho mỗi lần chạy.")
        videos = []
        if input_sources:
            video = Path(input_sources[0])
            if not video.is_file() or video.suffix.lower() not in VIDEO_EXTS:
                raise RuntimeError("Video không tồn tại hoặc định dạng không được hỗ trợ.")
            videos = [video]

        self.script_only = not bool(videos)
        if videos:
            check_ffmpeg(self.cfg.get("ffmpeg_bin", ""))
        if not self.cfg.get("batch_models_checked"):
            self.check_model_access()

        self.source_videos = videos
        self.two_video_mode = False

        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        script_path = self.cfg.get("input_script_path", "")
        source_base = videos[0].parent if videos else (Path(script_path).parent if script_path and Path(script_path).is_file() else Path.cwd())
        # A batch shares one output root; single runs keep their results under
        # a dedicated folder beside the selected source video.
        output_base = Path(self.cfg.get("story_output_root") or (source_base / "Story Outputs"))
        # V21.1: sanitize hidden Unicode chars from folder names so WebDAV / Windows
        # drives don't reject the write with PermissionError. Workdir stays under the
        # original parent when possible so the result still lives next to the source.
        clean_base = Path(_sanitize_hidden_chars(output_base.name))
        if clean_base.name != output_base.name:
            self._log(f"WARNING: Stripped hidden Unicode chars from output folder: '{output_base.name}' -> '{clean_base.name}'")
            output_base = output_base.parent / clean_base.name
            try:
                output_base.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                self._log(f"WARNING: Cannot use sanitized output folder ({exc}); falling back to LOCALAPPDATA cache.")
                output_base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local")) \
                    / "VideoStoryPublisher" / PROFILE_ID / "_runs"
                output_base.mkdir(parents=True, exist_ok=True)
        self.work_dir = output_base / f"_story_output_{stamp}"
        try:
            self.work_dir.mkdir(parents=True, exist_ok=False)
        except (PermissionError, OSError) as exc:
            # V21.1: WebDAV refusal. Move the whole workdir to %LOCALAPPDATA% cache
            # and keep running so the user still gets Blog.txt / frames.
            self._log(f"WARNING: Cannot create work_dir at '{self.work_dir}' ({exc}); using LOCALAPPDATA cache.")
            fallback = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local")) \
                / "VideoStoryPublisher" / PROFILE_ID / "_runs" / f"_story_output_{stamp}"
            fallback.mkdir(parents=True, exist_ok=True)
            self.work_dir = fallback
        if videos:
            _safe_write_text(self.work_dir / "source_video.json",
                             json.dumps({"path": str(videos[0].resolve()), "name": videos[0].name,
                                         "size": videos[0].stat().st_size}, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        combined_prompt = self.cfg["story_prompt_text"].strip()
        if script:
            combined_prompt += "\n\nAPPROVED VIDEO SCRIPT — LOCKED SOURCE FOR THE HOOK:\n" + script
            _safe_write_text(self.work_dir / "input_script.txt", script, encoding="utf-8")
        _safe_write_text(self.work_dir / "prompt_used.txt", combined_prompt, encoding="utf-8")

        if script and videos:
            self._log("Video + kịch bản: kịch bản cho Writer; bỏ qua Whisper/Vision, video dùng để lấy ảnh.")
        elif script:
            self._log("Chỉ kịch bản: bỏ qua Whisper/Vision/FFmpeg, đăng truyện dạng chữ.")
        else:
            self._log("Chỉ video: Whisper/Vision phân tích nội dung, trích ảnh từ video.")
        self.progress(5, "Preparing")

        if script:
            transcript = ""
            timeline = []
            if videos:
                duration = ffprobe_duration(videos[0])
                timeline = [{"video": videos[0].name, "global_start": 0.0,
                             "global_end": duration, "duration": duration}]
        else:
            disable_whisper = (Path(os.environ.get("LOCALAPPDATA") or Path.home()) /
                               "VideoStoryPublisher" / PROFILE_ID / "whisper_native_crash.txt")
            try:
                if disable_whisper.is_file():
                    raise RuntimeError("Whisper trên máy này từng bị access violation; "
                                       f"đang tránh crash lại ({disable_whisper}).")
                transcript, timeline = self.transcribe_videos(videos)
            except RuntimeError as exc:
                if "Whisper" not in str(exc) and "whisper" not in str(exc):
                    raise
                self.audio_unavailable = True
                if "Whisper crash" in str(exc):
                    disable_whisper.parent.mkdir(parents=True, exist_ok=True)
                    disable_whisper.write_text(str(exc) + "\n", encoding="utf-8")
                self._log(f"WARNING: {exc}. Dùng hình ảnh video để phân tích; không có transcript âm thanh.")
                transcript = ""
                duration = ffprobe_duration(videos[0])
                timeline = [{"video": videos[0].name, "global_start": 0.0,
                             "global_end": duration, "duration": duration}]
        # A supplied script must be the Writer's sole story source. Keep video
        # timing out of chapter planning; restore it later for image selection.
        self.video_timeline = [] if script else timeline

        if videos:
            self._log(f"Single-video mode | {videos[0].name}")

        _safe_write_text(self.work_dir / "transcript.txt", transcript, encoding="utf-8")
        _safe_write_text(self.work_dir / "timeline.json", json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")

        if videos:
            self.sample_frames(videos, timeline)

        if script:
            self._log("Dùng kịch bản đã nhập làm nguồn sự kiện chính; gửi cùng prompt cho Writer.")
            analysis = {
                "working_title": Path(script_path).stem if script_path else "Script Story",
                "summary": script[:16000],
                "characters": [],
                "events": [],
                "ending": "",
                "uncertainties": [],
                "approved_video_script": script,
            }
        else:
            analysis = self.analyze_with_vision(transcript, timeline)
            if self.audio_unavailable:
                visual_events = [e for e in analysis.get("events", [])
                                 if isinstance(e, dict) and str(e.get("description") or "").strip()]
                if len(self.frames) < 4 or len(visual_events) < 2:
                    raise RuntimeError("Whisper trên máy bị crash và ảnh video không đủ sự kiện rõ ràng "
                                       "để viết bài an toàn. Chưa đăng net; hãy cung cấp kịch bản video "
                                       "hoặc sửa môi trường Whisper.")
                analysis.setdefault("uncertainties", []).append(
                    "Audio transcription unavailable: do not attribute any dialogue or spoken claim to the video.")
                self._log("VISION-ONLY: chỉ dùng các sự kiện nhìn thấy; không suy đoán lời thoại trong video.")
            script = self.build_inferred_video_script(analysis, transcript)
            if self.audio_unavailable:
                script = ("AUDIO UNAVAILABLE: Whisper crashed on this computer. No spoken words "
                          "or narration from the video are known. Use only visually observed events; "
                          "do not invent video dialogue or identify unseen speakers.\n\n" + script)
            analysis["video_script"] = script
            analysis["summary"] = script[:16000]
            _safe_write_text(self.work_dir / "video_script.txt", script, encoding="utf-8")
            _safe_write_text(
                self.work_dir / "prompt_used.txt",
                combined_prompt + "\n\nINFERRED VIDEO SCRIPT — OBSERVED SOURCE:\n" + script,
                encoding="utf-8",
            )
            self._log("Đã lưu video_script.txt từ Whisper/Vision; Writer dùng kịch bản này để viết truyện.")
        _safe_write_text(self.work_dir / "analysis.json", json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")

        story = self.write_story(analysis, transcript)
        _safe_write_text(self.work_dir / "Blog.txt", self.build_blog_text(story), encoding="utf-8")
        caption = self.write_facebook_caption(analysis, story, script)
        story["facebook_caption"] = caption
        _safe_write_text(self.work_dir / "Caption.txt", caption + "\n", encoding="utf-8")
        if videos:
            self.video_timeline = timeline
            story = self.attach_local_images(story)
        else:
            # A script-only run is a text article, even if the model happened
            # to include image fields in its JSON output.
            for key in ("thumbnail_url", "thumbnail_local", "thumbnail_original_local"):
                story.pop(key, None)
            for chapter in story.get("chapters", []):
                chapter.pop("image_url", None)
                chapter.pop("image_local", None)

        _safe_write_text(self.work_dir / "story_before_upload.json",
                     json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")

        if videos:
            story = self.upload_cloudinary(story)
        self.story_json = story

        html = self.build_html(story)
        _safe_write_text(self.work_dir / "article.html", html, encoding="utf-8")
        _safe_write_text(self.work_dir / "story.json",
                         json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")

        publish_result = None
        if auto_publish:
            # V21.3: Skip when a previous successful publish_result.json is on
            # disk in this work_dir; otherwise re-running on the same folder
            # silently creates a -2 slug on SmartTraffic.
            prior = None
            try:
                prior_path = self.work_dir / "publish_result.json"
                if prior_path.exists():
                    prior = json.loads(prior_path.read_text(encoding="utf-8") or "{}")
            except Exception:
                prior = None
            if isinstance(prior, dict) and (prior.get("url") or prior.get("id")):
                self._log(
                    f"V21.3 Auto-publish SKIP — bài này đã đăng ở {prior.get('url','')} "
                    "(đã có publish_result.json). Xoá file đó để đăng lại."
                )
                publish_result = prior
            else:
                publish_result = self.publish_current(story)
            _safe_write_text(self.work_dir / "publish_result.json",
                             json.dumps(publish_result, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                _safe_write_text(
                    self.work_dir / "chapter_links.txt",
                    str(publish_result.get("chapter_links_text") or "")
                    or chapter_links_text(publish_result.get("url", ""), len(story.get("chapters", []))),
                    encoding="utf-8"
                )
            except Exception:
                pass

        renamed_video = self.rename_published_video(videos[0], story, publish_result, script) if videos and publish_result else ""
        if renamed_video:
            self.source_videos = [Path(renamed_video)]
        total_took = self._fmt_seconds(time.perf_counter() - self.run_started_at)
        self._log(f"ALL DONE | total runtime {total_took}")
        self.progress(100, f"Done | total {total_took}")

        return {
            "work_dir": str(self.work_dir),
            "story": story,
            "html": html,
            "publish_result": publish_result,
            "renamed_video": renamed_video,
        }


# ---------------------------
# GUI
# ---------------------------
class FirstRunWizard(tk.Toplevel):
    """
    V21.2 first-run setup dialog.

    Shown on any machine whose saved config is missing any of the required
    API keys / site info / image hosting credentials. The wizard guides the
    user through pasting each one, runs a live connectivity test where possible,
    then writes the result back to the per-user config so the main GUI tabs
    reflect it immediately. Everything saved lives under
    C:\\Users\\<user>\\.video_story_publisher.json — never shared between machines.
    """

    PAGES = ["welcome", "vilao", "smarttraffic", "site", "cloudinary", "finish"]

    REQUIRED_FIELDS = [
        "vilao_api_key",
        "smarttraffic_api_key",
        "site_id",
        "category_id",
        "site_host",
        "cloudinary_cloud_name",
        "cloudinary_api_key",
        "cloudinary_api_secret",
    ]

    def __init__(self, master):
        super().__init__(master)
        self.master_app = master
        self.title(f"{APP_NAME} - First-run setup")
        self.geometry("700x620")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Strings pulled from current cfg; populated if a previous run left them.
        self.vilao_key = tk.StringVar(value=str(master.cfg.get("vilao_api_key", "")).strip())
        self.smarttraffic_key = tk.StringVar(value=str(master.cfg.get("smarttraffic_api_key", "")).strip())
        self.vilao_writer = tk.StringVar(value=str(master.cfg.get("writer_model", "gpt-6-sol")).strip())
        self.vilao_vision = tk.StringVar(value=str(master.cfg.get("vision_model", "gpt-5.5")).strip())
        self.site_id = tk.StringVar(value=str(master.cfg.get("site_id", "120")).strip())
        self.category_id = tk.StringVar(value=str(master.cfg.get("category_id", "1747")).strip())
        self.site_host = tk.StringVar(value=str(master.cfg.get("site_host", "drama.viralstory.biz")).strip())
        self.cloud_name = tk.StringVar(value=str(master.cfg.get("cloudinary_cloud_name", "")).strip())
        self.cloud_key = tk.StringVar(value=str(master.cfg.get("cloudinary_api_key", "")).strip())
        self.cloud_secret = tk.StringVar(value=str(master.cfg.get("cloudinary_api_secret", "")).strip())

        self.vilao_status = tk.StringVar(value="Chưa test")
        self.smarttraffic_status = tk.StringVar(value="Chưa test")
        self.cloudinary_status = tk.StringVar(value="Chưa test")
        self.show_vilao = tk.BooleanVar(value=False)
        self.show_st = tk.BooleanVar(value=False)
        self.show_cloud = tk.BooleanVar(value=False)
        self.page_index = 0

        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)
        self.body = ttk.Frame(container)
        self.body.pack(fill="both", expand=True)

        nav = ttk.Frame(container)
        nav.pack(fill="x", pady=(12, 0))
        self.back_btn = ttk.Button(nav, text="← Quay lại", command=self._go_back)
        self.back_btn.pack(side="left")
        ttk.Button(nav, text="Bỏ qua", command=self._on_close).pack(side="left", padx=(8, 0))
        self.next_btn = ttk.Button(nav, text="Tiếp →", command=self._go_next)
        self.next_btn.pack(side="right")

        self._render()

    def _render(self):
        for child in self.body.winfo_children():
            child.destroy()
        name = self.PAGES[self.page_index]
        if name == "welcome":
            self._render_welcome()
        elif name == "vilao":
            self._render_vilao()
        elif name == "smarttraffic":
            self._render_smarttraffic()
        elif name == "site":
            self._render_site()
        elif name == "cloudinary":
            self._render_cloudinary()
        elif name == "finish":
            self._render_finish()
        self._update_nav()

    def _update_nav(self):
        is_last = self.page_index == len(self.PAGES) - 1
        self.back_btn.state(["disabled"] if self.page_index == 0 else ["!disabled"])
        self.next_btn.configure(text="Hoàn tất" if is_last else "Tiếp →")
        if is_last:
            try:
                self.next_btn.state(["!disabled"])
            except Exception:
                pass

    def _render_welcome(self):
        ttk.Label(self.body, text="Lần đầu chạy trên máy này", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Separator(self.body).pack(fill="x", pady=8)
        ttk.Label(
            self.body,
            text=(
                "Mỗi máy cần nhập riêng các key và thông tin site. Tool sẽ KHÔNG dùng key "
                "của máy khác và KHÔNG chia sẻ key sang máy khác.\n\n"
                "Bạn sẽ nhập theo thứ tự:"
            ),
            justify="left", wraplength=640,
        ).pack(anchor="w", pady=(0, 6))
        ttk.Label(self.body, text="1. Vilao AI key + Writer model + Vision model", foreground="#222").pack(anchor="w")
        ttk.Label(self.body, text="2. SmartTraffic API key + kiểm tra được phép đăng", foreground="#222").pack(anchor="w")
        ttk.Label(self.body, text="3. Site ID / Category ID / Site host (mặc định drama.viralstory.biz)", foreground="#222").pack(anchor="w")
        ttk.Label(self.body, text="4. Cloudinary (cloud name + API key + secret) — bắt buộc nếu có video", foreground="#222").pack(anchor="w")
        ttk.Label(
            self.body,
            text="\nSau khi 'Hoàn tất', toàn bộ thông tin được lưu vào file .video_story_publisher.json "
            "trong thư mục home của USER HIỆN TẠI (C:\\Users\\<tên bạn>\\).",
            foreground="#444", wraplength=640, justify="left",
        ).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            self.body,
            text="Có thể bấm 'Bỏ qua' để dùng config hiện tại (nếu máy đã có).",
            foreground="#666", wraplength=640, justify="left",
        ).pack(anchor="w", pady=(4, 0))

    def _render_vilao(self):
        ttk.Label(self.body, text="Bước 1/4 - Vilao AI API key", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Separator(self.body).pack(fill="x", pady=8)

        row1 = ttk.Frame(self.body); row1.pack(fill="x", pady=4)
        ttk.Label(row1, text="API key:", width=12).pack(side="left")
        entry = ttk.Entry(row1, textvariable=self.vilao_key, show="*", width=46)
        entry.pack(side="left", padx=(4, 4), fill="x", expand=True)
        ttk.Checkbutton(row1, text="Hiện", variable=self.show_vilao,
                        command=lambda: entry.configure(show="" if self.show_vilao.get() else "*")).pack(side="left")

        row2 = ttk.Frame(self.body); row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="Writer model:", width=12).pack(side="left")
        ttk.Entry(row2, textvariable=self.vilao_writer, width=24).pack(side="left", padx=(4, 12))
        ttk.Label(row2, text="Vision model:", width=12).pack(side="left")
        ttk.Entry(row2, textvariable=self.vilao_vision, width=18).pack(side="left", padx=(4, 0))

        actions = ttk.Frame(self.body); actions.pack(fill="x", pady=(8, 4))
        ttk.Button(actions, text="Test kết nối Vilao", command=self._test_vilao).pack(side="left")
        ttk.Label(actions, textvariable=self.vilao_status).pack(side="left", padx=10)

        ttk.Label(self.body, text="Tip: Test gọi 'Reply OK.' với 24 token. 401/403 = key sai hoặc chưa có quyền model.",
                  foreground="#666", wraplength=580, justify="left").pack(anchor="w", pady=(6, 0))

    def _render_smarttraffic(self):
        ttk.Label(self.body, text="Bước 2/4 - SmartTraffic API key", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Separator(self.body).pack(fill="x", pady=8)

        row = ttk.Frame(self.body); row.pack(fill="x", pady=4)
        ttk.Label(row, text="API key:", width=12).pack(side="left")
        entry = ttk.Entry(row, textvariable=self.smarttraffic_key, show="*", width=46)
        entry.pack(side="left", padx=(4, 4), fill="x", expand=True)
        ttk.Checkbutton(row, text="Hiện", variable=self.show_st,
                        command=lambda: entry.configure(show="" if self.show_st.get() else "*")).pack(side="left")

        actions = ttk.Frame(self.body); actions.pack(fill="x", pady=(8, 4))
        ttk.Button(actions, text="Test SmartTraffic", command=self._test_smarttraffic).pack(side="left")
        ttk.Label(actions, textvariable=self.smarttraffic_status).pack(side="left", padx=10)

        ttk.Label(self.body, text="Tip: Test gọi GET /publish/sites để xác nhận key và đếm số site được phép đăng.",
                  foreground="#666", wraplength=580, justify="left").pack(anchor="w", pady=(6, 0))

    def _render_site(self):
        ttk.Label(self.body, text="Bước 3/4 - SmartTraffic site info", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Separator(self.body).pack(fill="x", pady=8)

        ttk.Label(
            self.body,
            text="Mỗi máy có thể gán vào site/category khác nhau. Mặc định đã điền sẵn cho drama.viralstory.biz.",
            foreground="#444", wraplength=640, justify="left",
        ).pack(anchor="w", pady=(0, 8))

        row1 = ttk.Frame(self.body); row1.pack(fill="x", pady=4)
        ttk.Label(row1, text="Site ID:", width=14).pack(side="left")
        ttk.Entry(row1, textvariable=self.site_id, width=14).pack(side="left", padx=(4, 4))
        ttk.Label(row1, text="(số, vd 120)", foreground="#666").pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(self.body); row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="Category ID:", width=14).pack(side="left")
        ttk.Entry(row2, textvariable=self.category_id, width=14).pack(side="left", padx=(4, 4))
        ttk.Label(row2, text="(số, vd 1747)", foreground="#666").pack(side="left", padx=(6, 0))

        row3 = ttk.Frame(self.body); row3.pack(fill="x", pady=4)
        ttk.Label(row3, text="Site host:", width=14).pack(side="left")
        ttk.Entry(row3, textvariable=self.site_host, width=42).pack(side="left", padx=(4, 4), fill="x", expand=True)

        ttk.Label(
            self.body,
            text="Để biết chính xác Site ID / Category ID / host: truy cập "
                 "https://dashboard.smarttraffic.app/admin/api-keys và xem danh sách site.",
            foreground="#666", wraplength=640, justify="left",
        ).pack(anchor="w", pady=(8, 0))

    def _render_cloudinary(self):
        ttk.Label(self.body, text="Bước 4/4 - Cloudinary (image hosting)", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Separator(self.body).pack(fill="x", pady=8)

        ttk.Label(
            self.body,
            text="Cloudinary dùng để upload ảnh bìa và ảnh từng chapter lên URL công khai. "
                 "Nếu đăng bài có video thì 3 field này BẮT BUỘC.",
            foreground="#444", wraplength=640, justify="left",
        ).pack(anchor="w", pady=(0, 8))

        row1 = ttk.Frame(self.body); row1.pack(fill="x", pady=4)
        ttk.Label(row1, text="Cloud name:", width=14).pack(side="left")
        ttk.Entry(row1, textvariable=self.cloud_name, width=44).pack(side="left", padx=(4, 4), fill="x", expand=True)

        row2 = ttk.Frame(self.body); row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="API key:", width=14).pack(side="left")
        entry = ttk.Entry(row2, textvariable=self.cloud_key, show="*", width=44)
        entry.pack(side="left", padx=(4, 4), fill="x", expand=True)
        ttk.Checkbutton(row2, text="Hiện", variable=self.show_cloud,
                        command=lambda: entry.configure(show="" if self.show_cloud.get() else "*")).pack(side="left")

        row3 = ttk.Frame(self.body); row3.pack(fill="x", pady=4)
        ttk.Label(row3, text="API secret:", width=14).pack(side="left")
        entry3 = ttk.Entry(row3, textvariable=self.cloud_secret, show="*", width=44)
        entry3.pack(side="left", padx=(4, 4), fill="x", expand=True)
        ttk.Checkbutton(row3, text="Hiện", variable=self.show_cloud,
                        command=lambda: entry3.configure(show="" if self.show_cloud.get() else "*")).pack(side="left")

        actions = ttk.Frame(self.body); actions.pack(fill="x", pady=(8, 4))
        ttk.Button(actions, text="Test Cloudinary", command=self._test_cloudinary).pack(side="left")
        ttk.Label(actions, textvariable=self.cloudinary_status).pack(side="left", padx=10)

        ttk.Label(
            self.body,
            text="Tip: Test sẽ gọi POST /image/upload với 1 ảnh 1x1 để xác nhận credential đúng.",
            foreground="#666", wraplength=640, justify="left",
        ).pack(anchor="w", pady=(6, 0))

    def _render_finish(self):
        ttk.Label(self.body, text="Hoàn tất - tổng kết", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Separator(self.body).pack(fill="x", pady=8)

        vilao_state = "đã nhập" if self.vilao_key.get().strip() else "trống"
        st_state = "đã nhập" if self.smarttraffic_key.get().strip() else "trống"
        cloud_ok = bool(self.cloud_name.get().strip() and self.cloud_key.get().strip() and self.cloud_secret.get().strip())
        site_ok = bool(self.site_id.get().strip() and self.category_id.get().strip() and self.site_host.get().strip())

        ttk.Label(self.body, text=f"• Vilao AI key: {vilao_state}", font=("Segoe UI", 10)).pack(anchor="w", pady=4)
        ttk.Label(self.body, text=f"  Writer: {self.vilao_writer.get().strip() or 'gpt-6-sol'}").pack(anchor="w")
        ttk.Label(self.body, text=f"  Vision: {self.vilao_vision.get().strip() or 'gpt-5.5'}").pack(anchor="w")
        ttk.Label(self.body, text=f"  Status: {self.vilao_status.get()}").pack(anchor="w")
        ttk.Label(self.body, text=f"• SmartTraffic key: {st_state}").pack(anchor="w", pady=4)
        ttk.Label(self.body, text=f"  Status: {self.smarttraffic_status.get()}").pack(anchor="w")
        ttk.Label(self.body, text=f"• Site info: {'đủ' if site_ok else 'thiếu'}").pack(anchor="w", pady=4)
        if site_ok:
            ttk.Label(self.body, text=f"  Site ID / Cat ID / Host: {self.site_id.get()} / {self.category_id.get()} / {self.site_host.get()}").pack(anchor="w")
        ttk.Label(self.body, text=f"• Cloudinary: {'đủ 3 field' if cloud_ok else 'thiếu'}").pack(anchor="w", pady=4)
        if cloud_ok:
            ttk.Label(self.body, text=f"  Cloud: {self.cloud_name.get()}").pack(anchor="w")
        ttk.Label(self.body, text=f"  Status: {self.cloudinary_status.get()}").pack(anchor="w")

        missing = []
        if not self.vilao_key.get().strip(): missing.append("Vilao key")
        if not self.smarttraffic_key.get().strip(): missing.append("SmartTraffic key")
        if not site_ok: missing.append("Site info")
        if not cloud_ok: missing.append("Cloudinary (3 field)")

        if missing:
            ttk.Label(
                self.body,
                text=f"\n⚠ Còn thiếu: {', '.join(missing)}. Wizard sẽ lưu những gì đã nhập, "
                     "nhưng bạn nên nhập tiếp trong tab tương ứng trước khi chạy.",
                foreground="#a04000", wraplength=640, justify="left",
            ).pack(anchor="w", pady=(10, 0))
        else:
            ttk.Label(
                self.body,
                text="\nĐủ thông tin. Bấm 'Hoàn tất' để lưu vào file config (chỉ máy này dùng).",
                foreground="#0a7a0a", wraplength=640, justify="left",
            ).pack(anchor="w", pady=(10, 0))
        ttk.Label(self.body, text="\nBấm 'Hoàn tất' để lưu key vào file config (chỉ máy này dùng).",
                  foreground="#444", wraplength=580, justify="left").pack(anchor="w", pady=(10, 0))

    def _go_back(self):
        if self.page_index > 0:
            self.page_index -= 1
            self._render()

    def _go_next(self):
        if self.page_index < len(self.PAGES) - 1:
            self.page_index += 1
            self._render()
        else:
            self._commit_and_close()

    def _on_close(self):
        if self.page_index == 0:
            self.destroy()
            return
        if messagebox.askyesno(
            APP_NAME,
            "Đóng wizard? Key đã nhập sẽ KHÔNG được lưu. Bạn có thể chạy lại wizard bằng cách xóa file .video_story_publisher.json trong thư mục home user.",
            parent=self,
        ):
            self.destroy()

    def _test_vilao(self):
        key = self.vilao_key.get().strip()
        if not key:
            self.vilao_status.set("Trống - hãy dán key trước")
            return
        writer = self.vilao_writer.get().strip() or "gpt-6-sol"
        vision = self.vilao_vision.get().strip() or "gpt-5.5"
        base_url = str(self.master_app.cfg.get("vilao_base_url") or "https://api.vilao.ai/v1").strip().rstrip("/")
        self.vilao_status.set("Đang test…")
        self.update_idletasks()
        try:
            client = OpenAI(api_key=key, base_url=base_url, timeout=30.0, max_retries=0)
            ok_writer, ok_vision, errors = [], [], []
            for label, model in (("Writer", writer), ("Vision", vision)):
                try:
                    rsp = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": "Reply OK."}],
                        max_tokens=16,
                    )
                    raw = extract_chat_content(rsp, f"Vilao {label} test")
                    if raw.strip():
                        (ok_writer if label == "Writer" else ok_vision).append(model)
                except Exception as exc:
                    errors.append(f"{label} '{model}': {exc}")
            if ok_writer:
                msg = "OK"
                if ok_vision:
                    msg += f" + Vision ({ok_vision[0]})"
                if errors:
                    msg += f" | Lỗi: {'; '.join(errors)[:120]}"
                self.vilao_status.set(msg)
            else:
                self.vilao_status.set(f"FAIL: {'; '.join(errors)[:200]}")
        except Exception as exc:
            self.vilao_status.set(f"FAIL: {exc}")

    def _test_smarttraffic(self):
        key = self.smarttraffic_key.get().strip()
        if not key:
            self.smarttraffic_status.set("Trống - hãy dán key trước")
            return
        self.smarttraffic_status.set("Đang test…")
        self.update_idletasks()
        try:
            headers = {"Accept": "application/json", "Authorization": f"Bearer {key}"}
            r = requests.get("https://apiv3.smarttraffic.app/api/publish/sites",
                             headers=headers, timeout=15)
            if r.status_code == 200:
                sites = r.json()
                count = len(sites) if isinstance(sites, list) else 0
                self.smarttraffic_status.set(f"OK - {count} site được phép đăng")
            elif r.status_code in (401, 403):
                self.smarttraffic_status.set(f"FAIL {r.status_code}: key không hợp lệ hoặc hết hạn")
            else:
                self.smarttraffic_status.set(f"HTTP {r.status_code}: {r.text[:140]}")
        except Exception as exc:
            self.smarttraffic_status.set(f"FAIL: {exc}")

    def _test_cloudinary(self):
        cn = self.cloud_name.get().strip()
        ck = self.cloud_key.get().strip()
        cs = self.cloud_secret.get().strip()
        if not (cn and ck and cs):
            self.cloudinary_status.set("Trống - hãy điền đủ 3 field trước")
            return
        self.cloudinary_status.set("Đang test…")
        self.update_idletasks()
        try:
            try:
                import cloudinary
                import cloudinary.uploader as cld_up
            except Exception as ie:
                self.cloudinary_status.set(f"Cần cài cloudinary: {ie}")
                return
            cloudinary.config(
                cloud_name=cn, api_key=ck, api_secret=cs, secure=True,
            )
            # 1x1 transparent PNG inline
            tiny = (
                b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
            )
            import base64, io
            data = base64.b64decode(tiny)
            r = cld_up.upload("data:image/png;base64,{}".format(
                base64.b64encode(data).decode("ascii")), resource_type="image"
            )
            url = (r or {}).get("secure_url") or (r or {}).get("url") or ""
            public_id = (r or {}).get("public_id") or ""
            # best-effort cleanup
            try:
                if public_id:
                    cloudinary.uploader.destroy(public_id)
            except Exception:
                pass
            if url:
                self.cloudinary_status.set(f"OK - upload thành công ({url[:48]}…)")
            else:
                self.cloudinary_status.set("FAIL: upload trả về rỗng")
        except Exception as exc:
            msg = str(exc)
            if "Invalid credentials" in msg or "unauthorized" in msg.lower():
                self.cloudinary_status.set("FAIL: credential sai hoặc account bị khoá")
            else:
                self.cloudinary_status.set(f"FAIL: {msg[:160]}")

    def _commit_and_close(self):
        updates = {}
        if self.vilao_key.get().strip():
            updates["vilao_api_key"] = self.vilao_key.get().strip()
        if self.smarttraffic_key.get().strip():
            updates["smarttraffic_api_key"] = self.smarttraffic_key.get().strip()
        if self.vilao_writer.get().strip():
            updates["writer_model"] = self.vilao_writer.get().strip()
        if self.vilao_vision.get().strip():
            updates["vision_model"] = self.vilao_vision.get().strip()
        if self.site_id.get().strip():
            updates["site_id"] = self.site_id.get().strip()
        if self.category_id.get().strip():
            updates["category_id"] = self.category_id.get().strip()
        if self.site_host.get().strip():
            updates["site_host"] = self.site_host.get().strip()
        if self.cloud_name.get().strip():
            updates["cloudinary_cloud_name"] = self.cloud_name.get().strip()
        if self.cloud_key.get().strip():
            updates["cloudinary_api_key"] = self.cloud_key.get().strip()
        if self.cloud_secret.get().strip():
            updates["cloudinary_api_secret"] = self.cloud_secret.get().strip()

        if updates:
            self.master_app.cfg.update(updates)
            save_config(self.master_app.cfg)
            for k, v in updates.items():
                if k in self.master_app.vars:
                    self.master_app.vars[k].set(v)
            messagebox.showinfo(
                APP_NAME,
                f"Đã lưu {len(updates)} field cho máy này vào:\n" + str(CONFIG_FILE),
                parent=self,
            )
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1100x740")
        self.minsize(360, 190)
        self.resizable(True, True)

        self.cfg = apply_fixed_production_settings(load_config())
        self.input_path = tk.StringVar()
        self.batch_workers = tk.StringVar(value="2")
        self.script_path = None
        self.status_text = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0)

        self.run_started = None
        self.run_finished = None
        self.run_total = self.run_success = self.run_failed = 0
        self.run_is_batch = False
        self.run_active = False
        self.compact_override = None
        self.summary_text = tk.StringVar(value="Thành công: 0   |   Lỗi: 0   |   Tổng: 0")
        self.timing_text = tk.StringVar(value="Chưa bắt đầu")
        self.eta_text = tk.StringVar(value="Dự kiến hoàn tất: —")

        self.vars = {}
        for k, v in self.cfg.items():
            if k in ("story_prompt_text", "filename_phrase_list"):
                continue  # Edited in the multiline boxes instead.
            if isinstance(v, bool):
                self.vars[k] = tk.BooleanVar(value=v)
            else:
                self.vars[k] = tk.StringVar(value=str(v))

        self.q = queue.Queue()
        self.last_result = None
        self._build()
        self.after(100, self._poll_queue)
        # V21.1 first-run wizard: each new machine pastes its own API keys once.
        self.after(400, self._maybe_show_first_run_wizard)


    def _maybe_show_first_run_wizard(self):
        """Show the first-run wizard when any of the required credentials is missing.
        Required: Vilao key, SmartTraffic key, site id/category/host, Cloudinary trio."""
        required = [
            "vilao_api_key",
            "smarttraffic_api_key",
            "site_id",
            "category_id",
            "site_host",
            "cloudinary_cloud_name",
            "cloudinary_api_key",
            "cloudinary_api_secret",
        ]
        missing = [k for k in required if not str(self.cfg.get(k, "")).strip()]
        if missing:
            # Always show on a clean install or when Cloudinary is missing.
            # For other machines that already have Vilao + SmartTraffic + site but
            # only lack Cloudinary, the wizard still appears so the operator can
            # paste the per-machine Cloudinary credentials.
            FirstRunWizard(self)

    def report_callback_exception(self, exc, val, tb):
        """Keep GUI alive and show/log Tkinter callback exceptions."""
        import traceback
        print("\n" + "=" * 90)
        print("TKINTER CALLBACK ERROR")
        print("=" * 90)
        traceback.print_exception(exc, val, tb)
        print(f"\nCrash log saved to: {CRASH_LOG_FILE}")
        print("=" * 90)
        _write_crash_log("TKINTER CALLBACK ERROR", exc, val, tb)
        try:
            messagebox.showerror(
                APP_NAME,
                f"Tool gặp lỗi nhưng vẫn giữ cửa sổ mở.\n\n"
                f"{val}\n\n"
                f"Chi tiết đã lưu tại:\n{CRASH_LOG_FILE}"
            )
        except Exception:
            pass

    def _build(self):
        dashboard = ttk.LabelFrame(self, text="Tiến độ toàn bộ", padding=5)
        dashboard.pack(fill="x", padx=5, pady=3)
        headline = ttk.Frame(dashboard)
        headline.pack(fill="x")
        ttk.Label(headline, textvariable=self.summary_text,
                  font=("Segoe UI", 11, "bold")).pack(side="left", fill="x", expand=True)
        self.view_btn = ttk.Button(headline, text="Thu gọn", command=self._toggle_view)
        self.view_btn.pack(side="right")
        ttk.Progressbar(dashboard, variable=self.progress_var, maximum=100).pack(fill="x", pady=2)
        ttk.Label(dashboard, textvariable=self.timing_text).pack(anchor="w")
        ttk.Label(dashboard, textvariable=self.eta_text).pack(anchor="w")
        self.dashboard_status = ttk.Label(dashboard, textvariable=self.status_text, wraplength=340)
        self.dashboard_status.pack(anchor="w")
        self.content_shell = ttk.Frame(self)
        self.content_shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(self.content_shell, highlightthickness=0)
        self.content_canvas = canvas
        vertical = ttk.Scrollbar(self.content_shell, orient="vertical", command=canvas.yview)
        horizontal = ttk.Scrollbar(self.content_shell, orient="horizontal", command=canvas.xview)
        self.content_shell.rowconfigure(0, weight=1)
        self.content_shell.columnconfigure(0, weight=1)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        top = ttk.Frame(canvas, padding=6)
        content_id = canvas.create_window((0, 0), window=top, anchor="nw")
        top.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(
            content_id, width=max(1080, event.width)))
        # Canvas does not consume the wheel by itself: bind it app-wide and
        # route to the page unless the pointer is over a self-scrolling widget.
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.bind("<Configure>", self._resize_view, add="+")
        self.after(1000, self._tick_timing)

        # ---------------- SOURCE ----------------
        source = ttk.LabelFrame(top, text="1. Đầu vào: video, kịch bản hoặc cả hai", padding=10)
        source.pack(fill="x")
        source.columnconfigure(1, weight=1)

        ttk.Label(source, text="Video (tùy chọn)", width=26).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(source, textvariable=self.input_path).grid(row=0, column=1, sticky="ew", padx=(6, 8), pady=4)
        ttk.Button(source, text="Chọn video", command=self.select_video).grid(row=0, column=2, padx=4, pady=4)
        ttk.Button(source, text="Chọn folder", command=self.select_folder).grid(row=0, column=3, padx=4, pady=4)
        ttk.Button(source, text="Xóa", command=lambda: self.input_path.set("")).grid(row=0, column=4, padx=4, pady=4)

        ttk.Label(source, text="Số video chạy cùng lúc (1–4)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(source, textvariable=self.batch_workers, values=("1", "2", "3", "4"),
                     state="readonly", width=7).grid(row=1, column=1, sticky="w", padx=(6, 8), pady=4)

        ttk.Label(source, text="Net đăng bài").grid(row=1, column=2, sticky="e", padx=(12, 6), pady=4)
        ttk.Combobox(source, textvariable=self.vars["net_provider"],
                     values=("SmartTraffic", "Adsconex"), state="readonly", width=16).grid(
                         row=1, column=3, sticky="w", pady=4)

        ttk.Label(
            source,
            text="Chỉ video: Whisper/Vision đọc nội dung. Có kịch bản: Writer dùng kịch bản; video (nếu có) dùng lấy ảnh.",
            foreground="#555"
        ).grid(row=2, column=0, columnspan=5, sticky="w", pady=(5, 0))

        ttk.Label(source, text="Có video: ảnh bìa và ảnh chương lấy từ video. Chỉ kịch bản: bài dạng chữ, vẫn đăng được.",
                  foreground="#555").grid(row=3, column=0, columnspan=5, sticky="w", pady=(2, 0))

        ttk.Label(source, text="Chọn folder cha: tự quét mọi folder con; mỗi video đăng một bài rồi đổi tên theo ID. Folder bắt buộc Auto publish.",
                  foreground="#555").grid(row=4, column=0, columnspan=5, sticky="w", pady=(2, 0))

        # ---------------- TABS ----------------
        nb = ttk.Notebook(top, height=190)
        nb.pack(fill="x", pady=3)

        ai = ttk.Frame(nb, padding=5)
        st = ttk.Frame(nb, padding=10)
        ads = ttk.Frame(nb, padding=10)
        img = ttk.Frame(nb, padding=10)
        settings_tab = ttk.Frame(nb, padding=10)
        guide_tab = ttk.Frame(nb, padding=10)
        prompt_tab = ttk.Frame(nb, padding=5)

        nb.add(prompt_tab, text="Kịch bản & Prompt")
        nb.add(ai, text="Vilao AI")
        nb.add(st, text="SmartTraffic")
        nb.add(ads, text="Adsconex")
        nb.add(img, text="Image hosting")
        nb.add(settings_tab, text="FFmpeg")
        nb.add(guide_tab, text="Hướng dẫn")

        # The shipped prompt is editable and persists with Save Settings.
        # The script belongs only to the current run.
        prompt_tab.columnconfigure(0, weight=1, uniform="editors")
        prompt_tab.columnconfigure(1, weight=1, uniform="editors")
        prompt_tab.columnconfigure(2, weight=1, uniform="editors")
        prompt_tab.rowconfigure(0, weight=1)
        prompt_panel = ttk.LabelFrame(prompt_tab, text="Prompt viết Blog + Caption", padding=6)
        prompt_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        script_panel = ttk.LabelFrame(prompt_tab, text="Kịch bản đầu vào", padding=6)
        script_panel.grid(row=0, column=1, sticky="nsew", padx=(5, 5))
        phrase_panel = ttk.LabelFrame(prompt_tab, text="Câu chèn trong tên file", padding=6)
        phrase_panel.grid(row=0, column=2, sticky="nsew", padx=(5, 0))

        prompt_controls = ttk.Frame(prompt_panel)
        prompt_controls.pack(fill="x", pady=(0, 4))
        ttk.Button(prompt_controls, text="Nạp prompt .txt", command=self.load_prompt_file).pack(side="left")
        ttk.Button(prompt_controls, text="Khôi phục prompt mẫu", command=self.reset_prompt).pack(side="left", padx=4)
        self.prompt_editor = scrolledtext.ScrolledText(prompt_panel, wrap="word", height=6, width=32)
        self.prompt_editor.pack(fill="both", expand=True)
        self.prompt_editor.insert("1.0", self.cfg.get("story_prompt_text") or DEFAULT_STORY_PROMPT)

        script_controls = ttk.Frame(script_panel)
        script_controls.pack(fill="x", pady=(0, 4))
        ttk.Button(script_controls, text="Nạp kịch bản .txt", command=self.load_script_file).pack(side="left")
        ttk.Button(script_controls, text="Xóa kịch bản", command=self.clear_script).pack(side="left", padx=4)
        self.script_editor = scrolledtext.ScrolledText(script_panel, wrap="word", height=6, width=32)
        self.script_editor.pack(fill="both", expand=True)

        ttk.Label(phrase_panel, text="Mỗi dòng 1 câu; tool chọn ngẫu nhiên 1 câu. Trống = \"full story\".",
                  foreground="#555").pack(anchor="w", pady=(0, 4))
        self.filename_phrase_editor = scrolledtext.ScrolledText(phrase_panel, wrap="word", height=6, width=32)
        self.filename_phrase_editor.pack(fill="both", expand=True)
        self.filename_phrase_editor.insert("1.0", self.cfg.get("filename_phrase_list", DEFAULT_FILENAME_PHRASES))
        # Keep taller settings tabs readable, while the editor tab brings actions up.
        nb.bind("<<NotebookTabChanged>>", lambda event: nb.configure(
            height=190 if nb.select() == str(prompt_tab) else
            190 if nb.select() == str(ai) else
            320 if nb.select() == str(ads) else 290))

        # ============================================================
        # VILAO AI - only settings the user still needs to touch
        # ============================================================
        ai.columnconfigure(0, weight=1, uniform="vilao")
        ai.columnconfigure(1, weight=1, uniform="vilao")
        ai_connection = ttk.LabelFrame(ai, text="Kết nối & Model", padding=6)
        ai_connection.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ai_performance = ttk.LabelFrame(ai, text="Hiệu suất", padding=6)
        ai_performance.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        def compact_row(parent, row, label, key, show=None):
            ttk.Label(parent, text=label, width=17, font=("Segoe UI", 9)).grid(
                row=row, column=0, sticky="w", pady=2)
            ttk.Entry(parent, textvariable=self.vars[key], show=show or "", width=24).grid(
                row=row, column=1, sticky="ew", padx=(4, 0), pady=2)
            parent.columnconfigure(1, weight=1)

        compact_row(ai_connection, 0, "Vilao Base URL", "vilao_base_url")
        compact_row(ai_connection, 1, "Vilao API Key", "vilao_api_key", show="*")
        compact_row(ai_connection, 2, "Writer model", "writer_model")
        compact_row(ai_connection, 3, "Vision model", "vision_model")
        compact_row(ai_performance, 0, "Max vision frames", "max_vision_frames")
        compact_row(ai_performance, 1, "Vision batch size", "vision_batch_size")
        compact_row(ai_performance, 2, "Frame workers", "frame_workers")
        compact_row(ai_performance, 3, "Vision workers", "vision_parallel_workers")
        ttk.Checkbutton(
            ai, text="Viết nhanh: chạy song song 2 nhóm chapter theo dàn ý",
            variable=self.vars["fast_writer_batches"],
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Label(
            ai,
            text=(
                "Prompt sửa trong tab Kịch bản & Prompt • 6–10 chapter • "
                "Whisper small/CPU • Vision chọn riêng • title chapter hấp dẫn."
            ),
            foreground="#555",
            wraplength=1020,
            justify="left"
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 2))

        ai_buttons = ttk.Frame(ai)
        ai_buttons.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(
            ai_buttons, text="Mở Vilao",
            command=lambda: webbrowser.open("https://vilao.ai/")
        ).pack(side="left")
        ttk.Button(
            ai_buttons, text="Mở GPT-6 Sol",
            command=lambda: webbrowser.open("https://vilao.ai/model/vgpt/gpt-6-sol")
        ).pack(side="left", padx=6)
        ttk.Button(
            ai_buttons, text="Test model access",
            command=self.test_vilao_models
        ).pack(side="left", padx=6)

        # ============================================================
        # SMARTTRAFFIC - API key + site/category/pagebreak are editable
        # ============================================================
        st.columnconfigure(1, weight=1)

        # SmartTraffic API Key: keep the key masked by default, but allow the user
        # to explicitly reveal/edit it without changing any other tool behavior.
        ttk.Label(st, text="SmartTraffic API Key", width=22).grid(row=0, column=0, sticky="w", pady=4)
        self.smarttraffic_key_entry = ttk.Entry(
            st, textvariable=self.vars["smarttraffic_api_key"], show="*"
        )
        self.smarttraffic_key_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.smarttraffic_key_editing = False
        self.smarttraffic_edit_btn = ttk.Button(
            st, text="Edit Key", command=self.toggle_smarttraffic_key_edit
        )
        self.smarttraffic_edit_btn.grid(row=0, column=2, padx=(8, 0), pady=4)

        ttk.Checkbutton(
            st,
            text="Auto publish after generation",
            variable=self.vars["auto_publish"]
        ).grid(row=1, column=1, sticky="w", pady=6)

        # These were fixed before. They are now normal editable settings and are
        # saved to the same config file as the other fields.
        self._row(st, 2, "Site host", "site_host")
        self._row(st, 3, "Site ID", "site_id")
        self._row(st, 4, "Category ID", "category_id")
        ttk.Label(st, text="Page break", width=22).grid(row=5, column=0, sticky="w", pady=4)
        ttk.Combobox(st, textvariable=self.vars["pagebreak_mode"],
                     values=("<!--nextpage-->", "{{nextpage}}"), state="readonly", width=24).grid(
                         row=5, column=1, sticky="w", pady=4)

        ttk.Label(
            st,
            text="Blog.txt luôn dùng {{nextpage}}. API dùng Page break đã chọn; sau khi đăng tool kiểm tra ?c=2."
                 " Nếu trang hiện nguyên dấu ngắt, cần kiểm tra Theme Story trên website.",
            foreground="#555",
            wraplength=950
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 4))

        st_buttons = ttk.Frame(st)
        st_buttons.grid(row=7, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(
            st_buttons,
            text="Mở SmartTraffic API Keys",
            command=lambda: webbrowser.open("https://dashboard.smarttraffic.app/admin/api-keys")
        ).pack(side="left")

        # ============================================================
        # ADSCONEX (nền Blogbio) - token + site/author/category riêng
        # ============================================================
        ads.columnconfigure(1, weight=1)

        ttk.Label(ads, text="Adsconex API Token", width=22).grid(row=0, column=0, sticky="w", pady=4)
        self.adsconex_key_entry = ttk.Entry(
            ads, textvariable=self.vars["adsconex_api_key"], show="*"
        )
        self.adsconex_key_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.adsconex_key_editing = False
        self.adsconex_edit_btn = ttk.Button(
            ads, text="Edit Key", command=self.toggle_adsconex_key_edit
        )
        self.adsconex_edit_btn.grid(row=0, column=2, padx=(8, 0), pady=4)

        self._row(ads, 1, "API base URL", "adsconex_base_url")
        self._row(ads, 2, "Site host", "adsconex_site_host")
        self._row(ads, 3, "Author (đuôi slug)", "adsconex_author")
        ads_cat = ttk.Frame(ads)
        ads_cat.grid(row=4, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(ads_cat, text="Category ID (số)", width=22).pack(side="left")
        ttk.Entry(ads_cat, textvariable=self.vars["adsconex_category"], width=12).pack(side="left")
        ttk.Button(ads_cat, text="Lấy danh sách", command=self.fetch_adsconex_categories).pack(
            side="left", padx=(8, 0))
        ttk.Checkbutton(
            ads,
            text="Gắn ảnh bìa cho mọi chapter (apply_image_to_all)",
            variable=self.vars["adsconex_apply_image_to_all"]
        ).grid(row=5, column=1, sticky="w", pady=6)

        ttk.Label(
            ads,
            text=(
                "Đăng ở mode=chapter: server tự tách chương theo marker "
                "'CHAPTER N - Tên chương' và tự tạo series. Phần trước marker đầu là mô tả series."
                " Link bài dạng /blog/<slug>.\n"
                "Category ID: bấm 'Lấy danh sách' (dùng token ở trên) — thường Story = 15.\n"
                "Author: chỉ là chuỗi ghép vào đuôi slug (vd 'aqtn2' → ...-aqtn2). Để trống thì server tự thêm."
            ),
            foreground="#555",
            wraplength=950,
            justify="left"
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 4))

        ads_buttons = ttk.Frame(ads)
        ads_buttons.grid(row=7, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(
            ads_buttons,
            text="Mở trang API docs",
            command=lambda: webbrowser.open(
                str(self.vars["adsconex_site_host"].get() or "usjusticereport.cfx.bz").rstrip("/")
                + "/admin/api-docs")
        ).pack(side="left")
        ttk.Button(
            ads_buttons,
            text="Kiểm tra token",
            command=self.test_adsconex_token
        ).pack(side="left", padx=6)
        ttk.Button(
            ads_buttons,
            text="Lấy link site",
            command=lambda: webbrowser.open(
                "https://" + str(self.vars["adsconex_site_host"].get() or "usjusticereport.cfx.bz").strip().strip("/"))
        ).pack(side="left", padx=6)

        # ============================================================
        # IMAGE HOSTING - only credentials are editable
        # ============================================================
        img.columnconfigure(1, weight=1)
        self._row(img, 0, "Cloudinary cloud name", "cloudinary_cloud_name")
        self._row(img, 1, "Cloudinary API key", "cloudinary_api_key", show="*")
        self._row(img, 2, "Cloudinary API secret", "cloudinary_api_secret", show="*")

        ttk.Label(
            img,
            text=(
                "Có video: ảnh bìa website và thumbnail Facebook vuông "
                "290×290, ưu tiên khung có người trích từ video. Chỉ kịch bản: bài dạng chữ."
            ),
            foreground="#555",
            wraplength=950
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 4))

        img_buttons = ttk.Frame(img)
        img_buttons.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(
            img_buttons,
            text="Mở Cloudinary Console",
            command=lambda: webbrowser.open("https://console.cloudinary.com/")
        ).pack(side="left")
        ttk.Button(
            img_buttons,
            text="Test Cloudinary",
            command=self.test_cloudinary
        ).pack(side="left", padx=6)
        ttk.Button(
            img_buttons,
            text="Hướng dẫn Credentials",
            command=lambda: webbrowser.open(
                "https://cloudinary.com/documentation/developer_onboarding_faq_find_credentials"
            )
        ).pack(side="left", padx=6)

        # ============================================================
        # FFMPEG
        # ============================================================
        ttk.Label(
            settings_tab,
            text="FFmpeg BIN (thư mục chứa ffmpeg.exe và ffprobe.exe)",
            font=("Segoe UI", 10, "bold")
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        ttk.Entry(
            settings_tab,
            textvariable=self.vars["ffmpeg_bin"]
        ).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=4)

        ttk.Button(
            settings_tab, text="Chọn thư mục BIN",
            command=self.select_ffmpeg_bin
        ).grid(row=1, column=1, padx=4)

        ttk.Button(
            settings_tab, text="Kiểm tra FFmpeg",
            command=self.test_ffmpeg
        ).grid(row=1, column=2, padx=4)

        settings_tab.columnconfigure(0, weight=1)

        ttk.Label(
            settings_tab,
            text="Không cần thêm Windows PATH nếu đã chọn đúng thư mục BIN.",
            foreground="#555"
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 2))

        # ============================================================
        # GUIDE
        # ============================================================
        guide_box = scrolledtext.ScrolledText(guide_tab, wrap="word", height=15)
        guide_box.pack(fill="both", expand=True)
        guide_box.insert("1.0", GUIDE_TEXT)
        guide_box.configure(state="disabled")

        # ---------------- ACTIONS ----------------
        actions = ttk.Frame(top)
        actions.pack(fill="x", pady=(0, 8))

        self.run_btn = ttk.Button(
            actions,
            text="ANALYZE + WRITE STORY",
            command=self.start_run
        )
        self.run_btn.pack(side="left")

        ttk.Button(
            actions,
            text="Save Settings",
            command=self.save_settings
        ).pack(side="left", padx=8)

        ttk.Button(
            actions,
            text="Open Output Folder",
            command=self.open_output
        ).pack(side="left", padx=8)

        ttk.Button(
            actions, text="Chẩn đoán máy", command=self.export_machine_diagnostics
        ).pack(side="left", padx=8)

        self.update_btn = ttk.Button(
            actions, text="⬆ Cập nhật", command=self._check_update
        )
        self.update_btn.pack(side="left", padx=8)

        ttk.Button(
            actions,
            text="PUBLISH TO WEBSITE",
            command=self.publish_current
        ).pack(side="left", padx=8)

        ttk.Label(
            top,
            text=(
                "ANALYZE + WRITE STORY chỉ tạo bài. "
                "Muốn bài xuất hiện trên website: bật Auto publish hoặc bấm PUBLISH TO WEBSITE."
            ),
            foreground="#8a3b00"
        ).pack(anchor="w", pady=(2, 5))

        # ---------------- LOG + PREVIEW ----------------
        paned = ttk.Panedwindow(top, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.LabelFrame(paned, text="Log", padding=6)
        right = ttk.LabelFrame(paned, text="Story preview", padding=6)
        paned.add(left, weight=1)
        paned.add(right, weight=2)

        self.log_box = scrolledtext.ScrolledText(left, wrap="word", height=11)
        self.log_box.pack(fill="both", expand=True)

        self.preview = scrolledtext.ScrolledText(right, wrap="word", height=11)
        self.preview.pack(fill="both", expand=True)


    def _row(self, parent, row, label, key, show=None):
        ttk.Label(parent, text=label, width=22).grid(row=row, column=0, sticky="w", pady=4)
        e = ttk.Entry(parent, textvariable=self.vars[key], show=show or "")
        e.grid(row=row, column=1, sticky="ew", pady=4)
        parent.columnconfigure(1, weight=1)

    def toggle_smarttraffic_key_edit(self):
        """Reveal/mask the SmartTraffic key while keeping the field editable."""
        self.smarttraffic_key_editing = not self.smarttraffic_key_editing
        if self.smarttraffic_key_editing:
            self.smarttraffic_key_entry.configure(show="")
            self.smarttraffic_edit_btn.configure(text="Hide Key")
            self.smarttraffic_key_entry.focus_set()
            self.smarttraffic_key_entry.icursor("end")
        else:
            self.smarttraffic_key_entry.configure(show="*")
            self.smarttraffic_edit_btn.configure(text="Edit Key")

    def show_cloudinary_help(self):
        messagebox.showinfo(
            "Cloudinary - lấy thông tin ở đâu?",
            "Cloudinary dùng để upload ảnh lấy từ video và tạo URL public.\\n\\n"
            "1) Tạo/đăng nhập Cloudinary.\\n"
            "2) Cloud name: Dashboard > Product Environment > Cloud name.\\n"
            "3) API Key + API Secret: Settings > API Keys.\\n"
            "4) Copy 3 giá trị vào tab Image hosting.\\n\\n"
            "API Secret là thông tin bí mật, không chia sẻ cho người khác."
        )

    def test_cloudinary(self):
        cfg = self.current_cfg()
        cloud = cfg.get("cloudinary_cloud_name", "").strip()
        key = cfg.get("cloudinary_api_key", "").strip()
        secret = cfg.get("cloudinary_api_secret", "").strip()

        if not (cloud and key and secret):
            messagebox.showerror(
                APP_NAME,
                "Bạn cần điền đủ Cloud name, API key và API secret trước khi test."
            )
            return

        if cloud.lower() in {"root", "default", "admin", "cloudinary"}:
            messagebox.showerror(
                APP_NAME,
                f"Cloud name '{cloud}' không đúng.\n\n"
                "Đừng nhập Root/tên folder/tên tài khoản. Hãy copy đúng Cloud name trong Cloudinary Console."
            )
            return

        def worker():
            try:
                if not CLOUDINARY_AVAILABLE:
                    subprocess.check_call([sys.executable, "-m", "pip", "install", "cloudinary"])
                import cloudinary
                import cloudinary.api
                cloudinary.config(cloud_name=cloud, api_key=key, api_secret=secret, secure=True)
                # ping is a lightweight authenticated API request.
                cloudinary.api.ping()
                self.q.put(("cloudinary_test", f"Cloudinary OK!\nCloud name: {cloud}"))
            except Exception as e:
                self.q.put(("cloudinary_test", f"Cloudinary ERROR\n\n{e}"))

        threading.Thread(target=worker, daemon=True).start()

    def test_vilao_models(self):
        cfg = self.current_cfg()
        key = cfg.get("vilao_api_key", "").strip()
        base = cfg.get("vilao_base_url", "").strip().rstrip("/")
        if not key:
            messagebox.showerror(APP_NAME, "Vilao API Key đang trống.")
            return

        selected = cfg.get("writer_model", "").strip() if cfg.get("use_writer_for_vision") else cfg.get("vision_model", "").strip()
        writer = cfg.get("writer_model", "").strip()

        def worker():
            try:
                client = OpenAI(api_key=key, base_url=base)
                checks = []
                for label, model in [("Vision", selected), ("Writer", writer)]:
                    if not model:
                        checks.append(f"{label}: chưa nhập model")
                        continue
                    try:
                        r = client.chat.completions.create(
                            model=model,
                            messages=[{"role": "user", "content": "Reply only with OK."}],
                            temperature=0,
                            max_tokens=8,
                        )
                        content = extract_chat_content(r, f"{label} test")
                        checks.append(f"{label}: OK -> {model} | reply={content[:40]}")
                    except Exception as e:
                        checks.append(f"{label}: ERROR -> {model}\n{e}")
                self.q.put(("model_test", "\n\n".join(checks)))
            except Exception as e:
                self.q.put(("model_test", f"Không test được Vilao:\n{e}"))

        threading.Thread(target=worker, daemon=True).start()

    def select_ffmpeg_bin(self):
        p = filedialog.askdirectory(title="Chọn thư mục chứa ffmpeg.exe")
        if p:
            self.vars["ffmpeg_bin"].set(p)

    def test_ffmpeg(self):
        try:
            folder = configure_ffmpeg(self.vars["ffmpeg_bin"].get())
            ffmpeg, ffprobe = check_ffmpeg(self.vars["ffmpeg_bin"].get())
            # Ask executables for their version to verify they really run.
            a = subprocess.run([ffmpeg, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            b = subprocess.run([ffprobe, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            if a.returncode != 0 or b.returncode != 0:
                raise RuntimeError("ffmpeg.exe hoặc ffprobe.exe không chạy được.")
            first = (a.stdout.splitlines() or ["FFmpeg OK"])[0]
            messagebox.showinfo(
                APP_NAME,
                f"FFmpeg hoạt động bình thường!\\n\\nBIN: {folder}\\n{first}"
            )
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def select_video(self):
        selected = filedialog.askopenfilename(
            parent=self,
            title="Select video",
            initialdir=self._source_dialog_initialdir(),
            filetypes=[("Video files", " ".join("*" + ext for ext in sorted(VIDEO_EXTS))),
                       ("All files", "*.*")],
        )
        if selected:
            self.input_path.set(selected)
            self._last_source_directory = str(Path(selected).parent)


    def select_folder(self):
        selected = filedialog.askdirectory(
            parent=self,
            title="Select video folder",
            initialdir=self._source_dialog_initialdir(),
            mustexist=True,
        )
        if selected:
            self.input_path.set(selected)
            self._last_source_directory = selected


    def current_cfg(self):
        cfg = {}
        for k, var in self.vars.items():
            cfg[k] = var.get()
        cfg["story_prompt_text"] = self.prompt_editor.get("1.0", "end-1c").strip()
        cfg["filename_phrase_list"] = self.filename_phrase_editor.get("1.0", "end-1c").strip()

        detected_ffmpeg = auto_detect_ffmpeg_bin(str(cfg.get("ffmpeg_bin", "") or ""))
        if detected_ffmpeg:
            cfg["ffmpeg_bin"] = detected_ffmpeg
            try:
                if "ffmpeg_bin" in self.vars:
                    self.vars["ffmpeg_bin"].set(detected_ffmpeg)
            except Exception:
                pass

        return apply_fixed_production_settings(cfg)

    def export_machine_diagnostics(self):
        try:
            selected = Path(self.input_path.get().strip()) if self.input_path.get().strip() else Path.cwd()
            base = selected if selected.is_dir() else selected.parent
            output = base / "Story Outputs"
            output.mkdir(parents=True, exist_ok=True)
            path = output / f"machine_diagnostics_{time.strftime('%Y%m%d-%H%M%S')}.txt"
            path.write_text(machine_diagnostics(self.current_cfg()), encoding="utf-8")
            messagebox.showinfo(APP_NAME, f"Đã lưu thông tin máy (không chứa API key):\n{path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Không xuất được chẩn đoán: {exc}")

    # ============================================================
    # TỰ CẬP NHẬT QUA GITHUB (nút "⬆ Cập nhật")
    # ============================================================
    def _check_update(self):
        """Kiểm tra bản mới trên GitHub. Chạy thread nền để GUI không đứng."""
        try:
            self.update_btn.configure(state="disabled", text="Đang kiểm tra...")
        except Exception:
            pass
        self.status_text.set("Đang kiểm tra bản mới trên GitHub...")
        threading.Thread(target=self._update_work, daemon=True).start()

    def _update_work(self):
        import updater
        try:
            info = updater.check_update()
        except Exception as e:
            self.after(0, lambda: self._update_fail(f"{e}"))
            return
        if not info.get("ok"):
            self.after(0, lambda: self._update_fail(
                str(info.get("error") or "lỗi không rõ")))
            return
        if not info.get("has_update"):
            self.after(0, lambda: self._update_done(
                f"Đang là bản mới nhất (v{info.get('local')})."))
            return
        self.after(0, lambda: self._update_ask(info))

    def _update_ask(self, info):
        try:
            self.update_btn.configure(state="normal", text="⬆ Cập nhật")
        except Exception:
            pass
        notes = info.get("notes") or ""
        commits = "\n".join(f"• {c}" for c in (info.get("commits") or [])[:6])
        msg = (f"Có bản mới v{info['remote']}  (đang dùng v{info['local']}).\n\n"
               + (f"{notes}\n\n" if notes else "")
               + (commits + "\n\n" if commits else "")
               + "Cập nhật ngay?\nCài đặt + API key + bài đã làm được giữ nguyên.")
        if not messagebox.askyesno(APP_NAME, msg):
            self.status_text.set("Đã bỏ qua cập nhật.")
            return
        try:
            self.update_btn.configure(state="disabled", text="Đang cập nhật...")
        except Exception:
            pass
        self.status_text.set(f"Đang tải v{info['remote']} từ GitHub...")
        threading.Thread(target=self._update_apply, args=(info,), daemon=True).start()

    def _update_apply(self, info):
        import updater
        try:
            res = updater.apply_update(info.get("zip") or "")
        except Exception as e:
            self.after(0, lambda: self._update_fail(f"{e}"))
            return
        if not res.get("ok"):
            self.after(0, lambda: self._update_fail(
                str(res.get("error") or "lỗi không rõ")))
            return
        n = len(res.get("written") or [])
        self.after(0, lambda: self._update_restart(res, n))

    def _update_restart(self, res, n):
        self.status_text.set(
            f"Đã cập nhật lên v{res.get('version')} ({n} file). Đang mở lại...")
        messagebox.showinfo(
            APP_NAME,
            f"Đã cập nhật lên v{res.get('version')} ({n} file).\n\n"
            "Tool sẽ tự mở lại để dùng bản mới.")
        self._restart()

    def _update_done(self, msg):
        try:
            self.update_btn.configure(state="normal", text="⬆ Cập nhật")
        except Exception:
            pass
        self.status_text.set(msg)
        messagebox.showinfo(APP_NAME, msg)

    def _update_fail(self, err):
        try:
            self.update_btn.configure(state="normal", text="⬆ Cập nhật")
        except Exception:
            pass
        self.status_text.set("Cập nhật lỗi.")
        messagebox.showerror(APP_NAME, f"Không cập nhật được:\n\n{err}")

    def _restart(self):
        """Mở lại tool bằng code vừa cập nhật (ẩn cửa sổ cmd nếu có Mo_An.vbs)."""
        base = Path(__file__).resolve().parent
        vbs = base / "Mo_An.vbs"
        try:
            if os.name == "nt" and vbs.is_file():
                subprocess.Popen(["wscript.exe", str(vbs)], cwd=str(base))
            else:
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve())], cwd=str(base))
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def save_settings(self):
        cfg = self.current_cfg()
        if not cfg["story_prompt_text"]:
            messagebox.showerror(APP_NAME, "Prompt đang trống.")
            return
        save_config(cfg)
        messagebox.showinfo(APP_NAME, f"Saved to:\n{CONFIG_FILE}")

    def _load_text_into_editor(self, editor, title):
        path = filedialog.askopenfilename(
            title=title,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            value = Path(path).read_text(encoding="utf-8-sig").strip()
            if not value:
                raise ValueError("File không có nội dung.")
        except (OSError, UnicodeError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"Không đọc được file: {exc}")
            return
        editor.delete("1.0", "end")
        editor.insert("1.0", value)
        return path

    def load_prompt_file(self):
        self._load_text_into_editor(self.prompt_editor, "Chọn file prompt")

    def load_script_file(self):
        path = self._load_text_into_editor(self.script_editor, "Chọn file kịch bản")
        if path:
            self.script_path = path

    def clear_script(self):
        self.script_editor.delete("1.0", "end")
        self.script_path = None

    def reset_prompt(self):
        self.prompt_editor.delete("1.0", "end")
        self.prompt_editor.insert("1.0", DEFAULT_STORY_PROMPT)

    def log(self, s):
        self.q.put(("log", s))

    def progress(self, v, s=""):
        self.q.put(("progress", float(v), s))

    def toggle_adsconex_key_edit(self):
        """Reveal/mask the Adsconex token while keeping the field editable."""
        self.adsconex_key_editing = not self.adsconex_key_editing
        if self.adsconex_key_editing:
            self.adsconex_key_entry.configure(show="")
            self.adsconex_edit_btn.configure(text="Hide Key")
            self.adsconex_key_entry.focus_set()
            self.adsconex_key_entry.icursor("end")
        else:
            self.adsconex_key_entry.configure(show="*")
            self.adsconex_edit_btn.configure(text="Edit Key")

    def test_adsconex_token(self):
        """Goi GET /series de kiem tra token + ket noi (chay nen, khong treo GUI)."""
        token = str(self.vars["adsconex_api_key"].get() or "").strip()
        if not token:
            messagebox.showwarning(APP_NAME, "Chưa nhập Adsconex API Token.")
            return
        base = str(self.vars["adsconex_base_url"].get() or "https://usjusticereport.cfx.bz/api").strip().rstrip("/")
        site = str(self.vars["adsconex_site_host"].get() or "usjusticereport.cfx.bz").strip().strip("/")
        host = urlsplit(base).netloc or site
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Bearer {token}",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
            "Referer": f"https://{host}/admin/api-docs",
            "Origin": f"https://{host}",
            "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="24", "Google Chrome";v="141"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        def _worker():
            try:
                r = requests.get(f"{base}/series", headers=headers, timeout=45)
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception:
                        data = {}
                    series = data.get("series") if isinstance(data, dict) else None
                    count = len(series) if isinstance(series, list) else "?"
                    self.log(f"Adsconex token OK — HTTP 200 | series hiện có: {count}\n")
                    self.q.put(("adsconex_test", f"Adsconex OK — token hợp lệ.\n\nSeries hiện có trên site: {count}"))
                else:
                    snippet = (r.text or "")[:300]
                    self.log(f"Adsconex token FAIL — HTTP {r.status_code}: {snippet}\n")
                    self.q.put(("adsconex_test", f"Adsconex HTTP {r.status_code}\n\n{snippet}"))
            except Exception as e:
                self.log(f"Adsconex token FAIL — {e}\n")
                self.q.put(("adsconex_test", f"Không gọi được Adsconex:\n{e}"))

        self.log(f"Đang kiểm tra Adsconex token ({host})...\n")
        threading.Thread(target=_worker, daemon=True).start()

    def fetch_adsconex_categories(self):
        """Lay danh sach category tu Adsconex roi cho chon (chay nen)."""
        token = str(self.vars["adsconex_api_key"].get() or "").strip()
        if not token:
            messagebox.showwarning(APP_NAME, "Chưa nhập Adsconex API Token.")
            return
        base = str(self.vars["adsconex_base_url"].get() or "https://usjusticereport.cfx.bz/api").strip().rstrip("/")
        host = urlsplit(base).netloc or str(self.vars["adsconex_site_host"].get() or "").strip().strip("/")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Bearer {token}",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
            "Referer": f"https://{host}/admin/api-docs",
            "Origin": f"https://{host}",
            "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="24", "Google Chrome";v="141"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        def _worker():
            try:
                r = requests.get(f"{base}/categories", headers=headers, timeout=45)
                if r.status_code != 200:
                    self.log(f"Adsconex categories FAIL — HTTP {r.status_code}\n")
                    self.q.put(("adsconex_categories", ("error", f"HTTP {r.status_code}\n\n{(r.text or '')[:300]}")))
                    return
                data = r.json()
                items = None
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    for key in ("categories", "data", "items", "results"):
                        v = data.get(key)
                        if isinstance(v, list):
                            items = v
                            break
                        if isinstance(v, dict) and isinstance(v.get("data"), list):
                            items = v["data"]
                            break
                if not isinstance(items, list) or not items:
                    self.q.put(("adsconex_categories", ("error", "Không thấy category nào trong response.")))
                    return
                rows = []
                for c in items:
                    if not isinstance(c, dict):
                        continue
                    rows.append((
                        str(c.get("id", "")),
                        str(c.get("title") or c.get("name") or c.get("slug") or ""),
                        str(c.get("slug") or ""),
                    ))
                self.log(f"Adsconex categories OK — {len(rows)} category\n")
                self.q.put(("adsconex_categories", ("ok", rows)))
            except Exception as e:
                self.log(f"Adsconex categories FAIL — {e}\n")
                self.q.put(("adsconex_categories", ("error", str(e))))

        self.log(f"Đang lấy danh sách category ({host})...\n")
        threading.Thread(target=_worker, daemon=True).start()

    def show_adsconex_categories(self, payload):
        """Popup chon category; bam 1 dong la dien ID vao o."""
        kind, data = payload
        if kind == "error":
            messagebox.showerror("Adsconex categories", data)
            return
        win = tk.Toplevel(self)
        win.title("Chọn category — Adsconex")
        win.transient(self)
        win.grab_set()
        ttk.Label(
            win, text="Bấm 1 dòng để điền Category ID (bấm đúp cũng được):"
        ).pack(anchor="w", padx=10, pady=(10, 4))
        box = tk.Listbox(win, width=64, height=min(18, max(4, len(data))))
        box.pack(fill="both", expand=True, padx=10, pady=4)
        for cid, name, slug in data:
            box.insert("end", f"{cid:>5}  |  {name}  ({slug})")

        def _apply(_evt=None):
            sel = box.curselection()
            if not sel:
                return
            cid = data[sel[0]][0]
            self.vars["adsconex_category"].set(cid)
            self.log(f"Đã chọn Adsconex category ID = {cid}\n")
            win.destroy()

        box.bind("<Double-Button-1>", _apply)
        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(btns, text="Chọn", command=_apply).pack(side="right")
        ttk.Button(btns, text="Đóng", command=win.destroy).pack(side="right", padx=(0, 6))

    def _poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self.log_box.insert("end", item[1] + "\n")
                    self.log_box.see("end")
                elif item[0] == "progress":
                    self.progress_var.set(item[1])
                    self.status_text.set(item[2])
                elif item[0] == "batch_stats":
                    self.run_success, self.run_failed = item[1], item[2]
                    self._refresh_timing()
                elif item[0] == "done":
                    self.run_success = 1
                    self._finish_tracking()
                    self.run_btn.config(state="normal")
                    self.last_result = item[1]
                    self.show_result(item[1])
                    if item[1].get("publish_result"):
                        if item[1]["publish_result"].get("chapter_verified") is False:
                            messagebox.showwarning(APP_NAME, "Bài đã đăng nhưng website chưa tách chương. "
                                "Tool không tự đăng lại để tránh trùng bài. Hãy kiểm tra Theme Story và Site ID.")
                        else:
                            self.show_publish_links_dialog(
                                item[1]["publish_result"],
                                item[1].get("story", {})
                            )
                elif item[0] == "batch_done":
                    self.run_btn.config(state="normal")
                    summary = item[1]
                    self.run_success, self.run_failed = summary['success'], summary['failed']
                    self._finish_tracking()
                    self.status_text.set(f"XONG: {summary['success']}/{summary['total']} video | Lỗi: {summary['failed']}")
                    self.progress_var.set(100)
                    messagebox.showinfo(APP_NAME, f"Đã xử lý {summary['total']} video: thành công {summary['success']}, lỗi {summary['failed']}.\n\nTitle bài net: {summary['titles_path']}\nChi tiết: {summary['report_path']}")
                elif item[0] == "error":
                    if self.run_active:
                        if not self.run_is_batch:
                            self.run_failed = 1
                        self.status_text.set("Đã dừng do lỗi — xem thông báo và log")
                        self._finish_tracking()
                    self.run_btn.config(state="normal")
                    messagebox.showerror(APP_NAME, item[1])
                elif item[0] == "adsconex_test":
                    if "OK" in item[1]:
                        messagebox.showinfo("Adsconex token", item[1])
                    else:
                        messagebox.showerror("Adsconex token", item[1])
                elif item[0] == "adsconex_categories":
                    self.show_adsconex_categories(item[1])
                elif item[0] == "model_test":
                    messagebox.showinfo("Vilao model access", item[1])
                elif item[0] == "published":
                    r = item[1]
                    story = item[2] if len(item) > 2 else (self.last_result or {}).get("story", {})
                    url = r.get("url", "")
                    self.status_text.set(
                        f"ĐÃ ĐĂNG NHƯNG CHƯA TÁCH CHƯƠNG: {url}" if r.get("chapter_verified") is False
                        else f"PUBLISHED: {url}"
                    )
                    if r.get("chapter_verified") is False:
                        messagebox.showwarning(APP_NAME, "Bài đã đăng nhưng website chưa tách chương. "
                            "Tool không tự đăng lại để tránh trùng bài. Hãy kiểm tra Theme Story và Site ID.")
                    else:
                        self.show_publish_links_dialog(r, story)
                elif item[0] == "cloudinary_test":
                    if "ERROR" in item[1]:
                        messagebox.showerror("Cloudinary test", item[1])
                    else:
                        messagebox.showinfo("Cloudinary test", item[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


    def start_run(self):
        p1_text = self.input_path.get().strip()
        script = self.script_editor.get("1.0", "end-1c").strip()
        if not p1_text and not script:
            messagebox.showerror(APP_NAME, "Hãy nhập kịch bản hoặc chọn một video.")
            return
        sources = []
        batch_folder = None
        if p1_text:
            p1 = Path(p1_text)
            if p1.is_dir():
                batch_folder = p1
                sources = find_unpublished_videos(p1)
                if not sources:
                    messagebox.showerror(APP_NAME, "Folder không có video chưa xử lý (kể cả folder con).")
                    return
                if script:
                    messagebox.showerror(APP_NAME, "Chạy folder: xóa kịch bản chung để mỗi video được phân tích riêng.")
                    return
            elif not p1.is_file() or p1.suffix.lower() not in VIDEO_EXTS:
                messagebox.showerror(APP_NAME, "Video không tồn tại hoặc định dạng không được hỗ trợ.")
                return
            else:
                sources.append(p1)

        cfg = self.current_cfg()
        if not cfg["story_prompt_text"]:
            messagebox.showerror(APP_NAME, "Prompt đang trống. Hãy nhập prompt trong tab Kịch bản & Prompt.")
            return
        cfg["input_script"] = script
        cfg["input_script_path"] = self.script_path if script else None
        if batch_folder and not cfg.get("auto_publish"):
            messagebox.showerror(APP_NAME, "Chạy folder cần bật Auto publish trong tab SmartTraffic để lấy ID và đổi tên từng video.")
            return
        requested_workers = int(self.batch_workers.get())
        worker_count = min(requested_workers, 2)
        save_config({k: v for k, v in cfg.items() if k not in ("input_script", "input_script_path")})
        self._begin_tracking(len(sources) if batch_folder else 1, bool(batch_folder))
        self.run_btn.config(state="disabled")
        self.preview.delete("1.0", "end")
        self.log_box.delete("1.0", "end")

        def worker():
            try:
                if batch_folder:
                    rows = []
                    output_root = batch_folder / "Story Outputs"
                    output_root.mkdir(parents=True, exist_ok=True)
                    diag_path = output_root / f"machine_diagnostics_{time.strftime('%Y%m%d-%H%M%S')}.txt"
                    diag_path.write_text(machine_diagnostics(cfg), encoding="utf-8")
                    self.log(f"BATCH: {len(sources)} video | {worker_count} luồng | {batch_folder}")
                    if requested_workers > worker_count:
                        self.log("Đã giới hạn 2 video đồng thời vì mỗi video còn có nhiều tác vụ Vision/Writer song song.")
                    self.log(f"Thông tin máy: {diag_path}")
                    existing = find_saved_publications(
                        output_root, sources,
                        cfg.get("net_provider", "SmartTraffic"),
                        cfg.get("site_host", ""),
                        cfg.get("adsconex_site_host", ""))
                    # Do the API model probe once, before starting any per-video
                    # worker. Repeating it concurrently opens several SSL
                    # connections and may crash native libraries on some PCs.
                    if any(not existing.get(str(video.resolve()).casefold()) for video in sources):
                        preflight_cfg = dict(cfg)
                        preflight_cfg["story_output_root"] = str(output_root)
                        StoryPipeline(preflight_cfg, self.log, lambda *args: None).check_model_access()
                        cfg["batch_models_checked"] = True
                    def run_one(video):
                        label = str(video.relative_to(batch_folder))
                        def batch_log(msg):
                            self.log(f"[{label}] {msg}")
                        def batch_progress(value, status=""):
                            if status:
                                self.log(f"[{label}] {status} ({value:.0f}%)")
                        pipeline = None
                        try:
                            video_cfg = dict(cfg)
                            video_cfg["story_output_root"] = str(output_root)
                            video_cfg["vision_parallel_workers"] = "1"
                            video_cfg["chapter_workers"] = "2"
                            video_cfg["cloudinary_workers"] = "2"
                            pipeline = StoryPipeline(video_cfg, batch_log, batch_progress)
                            saved = existing.get(str(video.resolve()).casefold(), [])
                            if saved:
                                work_dir, previous = saved[0]
                                if len(saved) > 1:
                                    batch_log(f"Có {len(saved)} bài đã đăng cho video; dùng bài mới nhất "
                                              f"{active_publish_link(previous, cfg)} "
                                              f"tại {work_dir}. Không đăng lại.")
                                story_path = work_dir / "story.json"
                                if not story_path.is_file():
                                    raise RuntimeError(f"Bài đã đăng tại {work_dir}, nhưng thiếu story.json để khôi phục tên. Không đăng lại.")
                                story = json.loads(story_path.read_text(encoding="utf-8"))
                                script_path = work_dir / "video_script.txt"
                                script_text = script_path.read_text(encoding="utf-8") if script_path.is_file() else ""
                                pipeline.work_dir = work_dir
                                batch_log(f"Bài đã đăng trước đó: {active_publish_link(previous, cfg)}. Chỉ đổi tên video.")
                                renamed = pipeline.rename_published_video(video, story, previous, script_text)
                                result = {"work_dir": str(work_dir), "story": story,
                                          "publish_result": previous, "renamed_video": renamed}
                            else:
                                result = pipeline.run([video], auto_publish=True)
                            published = result.get("publish_result") or {}
                            if not published:
                                raise RuntimeError("Không có kết quả đăng bài; kiểm tra log publish.")
                            if not result.get("renamed_video"):
                                rename_log = Path(result["work_dir"]) / "rename_error.txt"
                                reason = rename_log.read_text(encoding="utf-8") if rename_log.is_file() else "Không có chi tiết lỗi đổi tên."
                                raise RuntimeError(
                                    f"Bài đã đăng (ID {active_publish_link(published, cfg)}), "
                                    f"nhưng không đổi được tên video. Thư mục kết quả: {result['work_dir']}. "
                                    f"Nguyên nhân: {reason.strip()}. Không chạy lại để tránh đăng trùng."
                                )
                            return {"video": label, "status": "success", "renamed_video": result["renamed_video"],
                                    "article_url": active_publish_link(published, cfg),
                                    "net_title": compact_meta_text(result["story"].get("title") or "", 240),
                                    "output": result["work_dir"]}
                        except Exception as exc:
                            batch_log(f"LỖI: {exc}")
                            return {"video": label, "status": "failed", "error": str(exc),
                                    "output": str(pipeline.work_dir) if pipeline and pipeline.work_dir else ""}
                    with ThreadPoolExecutor(max_workers=worker_count) as pool:
                        futures = {pool.submit(run_one, video): video for video in sources}
                        for future in as_completed(futures):
                            rows.append(future.result())
                            self.q.put(("batch_stats", sum(r['status']=='success' for r in rows),
                                        sum(r['status']=='failed' for r in rows)))
                            self.q.put(("progress", 100 * len(rows) / len(sources),
                                        f"Đã xử lý {len(rows)}/{len(sources)} | Lỗi {sum(r['status']=='failed' for r in rows)}"))
                    report_path = output_root / f"batch_result_{time.strftime('%Y%m%d-%H%M%S')}.json"
                    _safe_write_text(report_path, json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
                    titles_path = report_path.with_name(report_path.stem.replace("batch_result_", "net_titles_") + ".txt")
                    title_lines = [str(row["net_title"]).replace("\r", " ").replace("\n", " ").strip()
                                   for row in sorted(rows, key=lambda item: item["video"].casefold())
                                   if row["status"] == "success" and str(row.get("net_title") or "").strip()]
                    _safe_write_text(titles_path, "\n".join(title_lines) + ("\n" if title_lines else ""), encoding="utf-8")
                    self.q.put(("batch_done", {"total": len(rows), "success": sum(r['status']=='success' for r in rows),
                                               "failed": sum(r['status']=='failed' for r in rows), "report_path": str(report_path),
                                               "titles_path": str(titles_path)}))
                    return
                pipe = StoryPipeline(cfg, self.log, self.progress)
                result = pipe.run(sources, auto_publish=bool(cfg.get("auto_publish")))
                result["_pipeline"] = pipe
                self.q.put(("done", result))
            except Exception as e:
                import traceback
                err = traceback.format_exc()
                print("\n" + "=" * 90)
                print("PIPELINE ERROR")
                print("=" * 90)
                print(err)
                print(f"Crash log saved to: {CRASH_LOG_FILE}")
                print("=" * 90)
                _write_crash_log(
                    "PIPELINE ERROR",
                    type(e), e, e.__traceback__,
                    extra="The GUI should remain open after this pipeline failure."
                )
                self.q.put(("error", f"{e}\n\n{err}"))

        threading.Thread(target=worker, daemon=True).start()


    def show_result(self, result):
        story = result["story"]
        chunks = [
            f"TITLE: {story.get('title','')}",
            f"SLUG: {story.get('slug','')}",
            f"META: {story.get('meta_description','')}",
            "",
        ]
        for ch in story.get("chapters", []):
            chunks += [
                f"CHAPTER {ch.get('number')}: {ch.get('title')}",
                ch.get("body", ""),
                "",
            ]
        if story.get("facebook_caption"):
            chunks += ["FACEBOOK CAPTION:", story["facebook_caption"], ""]
        if result.get("publish_result"):
            chunks += ["", "PUBLISH RESULT:", json.dumps(result["publish_result"], ensure_ascii=False, indent=2)]
            url = result["publish_result"].get("url", "")
            self.status_text.set(
                f"ĐÃ ĐĂNG NHƯNG CHƯA TÁCH CHƯƠNG: {url}"
                if result["publish_result"].get("chapter_verified") is False else f"PUBLISHED: {url}"
            )
        else:
            chunks += [
                "",
                "=== CHƯA ĐĂNG LÊN WEBSITE ===",
                "Bài mới chỉ được tạo và lưu local.",
                "Bấm 'Publish Current Result' để đăng ngay."
            ]
            self.status_text.set("GENERATED - NOT PUBLISHED. Bấm 'Publish Current Result'.")
        self.preview.insert("1.0", "\n".join(chunks))

    def open_output(self):
        if not self.last_result:
            messagebox.showinfo(APP_NAME, "No output yet.")
            return
        p = self.last_result["work_dir"]
        if os.name == "nt":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])

    def show_publish_links_dialog(self, publish_result: dict, story: dict):
        url = (publish_result or {}).get("url", "")
        chapter_count = len((story or {}).get("chapters", []))
        if not url:
            return

        links_text = str((publish_result or {}).get("chapter_links_text") or "") \
            or chapter_links_text(url, chapter_count)

        win = tk.Toplevel(self)
        win.title("Published Article Links")
        win.geometry("900x560")
        win.minsize(700, 420)
        win.transient(self)
        win.grab_set()

        top = ttk.Frame(win, padding=12)
        top.pack(fill="both", expand=True)

        ttk.Label(
            top,
            text=f"Publish thành công - {chapter_count} chapter",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor="w")

        ttk.Label(
            top,
            text=url,
            foreground="#1a5fb4",
            wraplength=840
        ).pack(anchor="w", pady=(4, 10))

        box = scrolledtext.ScrolledText(top, wrap="none", height=20)
        box.pack(fill="both", expand=True)
        box.insert("1.0", links_text)
        box.configure(state="normal")

        btns = ttk.Frame(top)
        btns.pack(fill="x", pady=(10, 0))

        def copy_all():
            self.clipboard_clear()
            self.clipboard_append(links_text)
            self.update()
            messagebox.showinfo(APP_NAME, "Đã copy toàn bộ link chapter.")

        def copy_base():
            self.clipboard_clear()
            self.clipboard_append(url)
            self.update()
            messagebox.showinfo(APP_NAME, "Đã copy link bài viết.")

        def open_article():
            try:
                webbrowser.open(url)
            except Exception:
                pass

        ttk.Button(btns, text="Copy ALL chapter links", command=copy_all).pack(side="left")
        ttk.Button(btns, text="Copy article link", command=copy_base).pack(side="left", padx=8)
        ttk.Button(btns, text="Open article", command=open_article).pack(side="left", padx=8)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")

    def _read_previous_publish(self, story: dict) -> Optional[Dict]:
        """V21.3: Return saved publish_result if this work_dir already published
        the same story. The slug is the most reliable identifier because URLs
        might differ on duplicate reposts."""
        try:
            work_dir = self.last_result.get("work_dir") if self.last_result else None
            if not work_dir:
                return None
            p = Path(work_dir) / "publish_result.json"
            if not p.exists():
                return None
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if not (data.get("url") or data.get("id")):
            return None
        data.setdefault("saved_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        return data

    def publish_current(self):
        if not self.last_result:
            messagebox.showinfo(APP_NAME, "Generate a story first.")
            return
        cfg = self.current_cfg()
        story = self.last_result["story"]
        source_pipe = self.last_result.get("_pipeline")
        script_only = bool(getattr(source_pipe, "script_only", False))
        if not story.get("thumbnail_url") and not script_only:
            messagebox.showerror(APP_NAME, "Video chưa có thumbnail URL. Hãy kiểm tra ảnh trích từ video và Cloudinary.")
            return

        # V21.3: Detect a previous successful publish in this work_dir BEFORE we
        # hit the API. SmartTraffic auto-creates a -2/-3 slug on duplicate title
        # instead of refusing, so callers who click "Publish" twice end up with
        # two identical articles on the site. Warn and let the user decide.
        prev = self._read_previous_publish(story)
        if prev:
            existing_url = prev.get("url") or "(không rõ URL)"
            existing_marker = prev.get("chapter_marker") or "{{nextpage}}"
            existing_at = prev.get("saved_at") or "trước đó"
            choice = messagebox.askyesnocancel(
                APP_NAME,
                "BÀI NÀY ĐÃ ĐĂNG TRƯỚC ĐÓ!\n\n"
                f"URL cũ: {existing_url}\n"
                f"Marker đã dùng: {existing_marker}\n"
                f"Lúc: {existing_at}\n\n"
                "Yes  = Đăng lại (SmartTraffic sẽ tạo slug '-2' cho bài mới)\n"
                "No   = Dừng lại, không đăng\n"
                "Cancel = Dừng và xóa publish_result.json cũ để đăng sạch",
                icon="warning",
            )
            if choice is None:
                try:
                    p = Path(self.last_result["work_dir"]) / "publish_result.json"
                    if p.exists():
                        p.unlink()
                    self.last_result.pop("publish_result", None)
                    self.log("[V21.3] Đã xoá publish_result.json cũ. Bấm Publish lại để đăng bài mới.")
                except Exception as exc:
                    self.log(f"[V21.3] Không xoá được publish_result.json cũ: {exc}")
                return
            if not choice:
                self.log("[V21.3] User chọn KHÔNG đăng lại. Bỏ qua.")
                return
            self.log("[V21.3] User chọn ĐĂNG LẠI. Slug sẽ tự động thêm '-2' nếu slug đã tồn tại.")

        def worker():
            try:
                pipe = StoryPipeline(cfg, self.log, self.progress)
                pipe.script_only = script_only
                pipe.work_dir = Path(self.last_result["work_dir"])
                r = pipe.publish_current(story)
                self.last_result["publish_result"] = r
                source_videos = getattr(source_pipe, "source_videos", []) if source_pipe else []
                if source_videos:
                    script_path = Path(self.last_result["work_dir"]) / "video_script.txt"
                    script = script_path.read_text(encoding="utf-8") if script_path.exists() else cfg.get("input_script", "")
                    renamed = pipe.rename_published_video(Path(source_videos[0]), story, r, script)
                    if renamed:
                        self.last_result["renamed_video"] = renamed
                        source_pipe.source_videos = [Path(renamed)]
                try:
                    out_dir = Path(self.last_result["work_dir"])
                    _safe_write_text(
                        out_dir / "publish_result.json",
                        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8",
                    )
                    _safe_write_text(
                        out_dir / "chapter_links.txt",
                        str(r.get("chapter_links_text") or "")
                        or chapter_links_text(
                            r.get("url", ""),
                            len(story.get("chapters", []))
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
                self.q.put(("log", f"PUBLISHED SUCCESSFULLY -> {r.get('url','')}"))
                self.q.put(("published", r, story))
            except Exception as e:
                self.q.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _source_dialog_initialdir(self):
        raw = self.input_path.get().strip()
        if raw:
            selected = Path(raw)
            candidate = selected if selected.is_dir() else selected.parent
            if candidate.is_dir():
                return str(candidate)
        return getattr(self, "_last_source_directory", str(Path.home()))


    def _tick_timing(self):
        self._refresh_timing()
        self.after(1000, self._tick_timing)


    def _refresh_timing(self):
        done = self.run_success + self.run_failed
        self.summary_text.set(
            f"Thành công: {self.run_success}   |   Lỗi: {self.run_failed}   |   Tổng: {self.run_total}")
        if self.run_started is None:
            return
        elapsed = (self.run_finished or time.monotonic()) - self.run_started
        self.timing_text.set(
            f"Bắt đầu: {time.strftime('%H:%M:%S', time.localtime(self.run_started_wall))}"
            f"   |   Đã chạy: {self._duration_text(elapsed)}")
        if not self.run_active:
            label = "Hoàn tất" if done == self.run_total else "Đã dừng"
            self.eta_text.set(f"{label}: {time.strftime('%d/%m %H:%M:%S', time.localtime(self.run_finished_wall))}"
                              f"   |   Tổng thời gian: {self._duration_text(elapsed)}")
        elif self.run_is_batch and done and done < self.run_total:
            remaining = elapsed / done * (self.run_total - done)
            finish = time.strftime('%d/%m %H:%M:%S', time.localtime(time.time() + remaining))
            self.eta_text.set(f"Còn khoảng: {self._duration_text(remaining)}   |   Dự kiến: {finish}")
        elif self.run_is_batch and done == self.run_total:
            self.eta_text.set("Đang lưu báo cáo cuối cùng...")
        else:
            self.eta_text.set("Dự kiến hoàn tất: đang thu thập tốc độ xử lý" if self.run_is_batch
                              else "Dự kiến hoàn tất: chưa đủ dữ liệu cho lần chạy đơn")


    def _finish_tracking(self):
        if self.run_active:
            self.run_finished = time.monotonic()
            self.run_finished_wall = time.time()
            self.run_active = False
        self._refresh_timing()


    def _begin_tracking(self, total, batch=False):
        self.run_total = total
        self.run_success = self.run_failed = 0
        self.run_is_batch = batch
        self.run_started = time.monotonic()
        self.run_started_wall = time.time()
        self.run_finished = None
        self.run_active = True
        self.progress_var.set(0)
        self.status_text.set("Đang chuẩn bị...")
        self._refresh_timing()


    @staticmethod
    def _duration_text(seconds):
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


    def _on_mousewheel(self, event):
        """Route the wheel to the main canvas unless the pointer is over a
        widget that scrolls on its own (Text / Listbox / Combobox dropdown)."""
        try:
            w = self.winfo_containing(event.x_root, event.y_root)
        except Exception:
            w = None
        if w is None:
            # winfo_containing can miss; fall back to a bounds check so the
            # wheel never dies silently over the page background.
            try:
                left = self.content_canvas.winfo_rootx()
                top = self.content_canvas.winfo_rooty()
                right = left + self.content_canvas.winfo_width()
                bottom = top + self.content_canvas.winfo_height()
                if not (left <= event.x_root <= right and top <= event.y_root <= bottom):
                    return None
            except Exception:
                return None
            return self._scroll_content(event)
        # Walk up the widget chain: if any ancestor handles scrolling, leave
        # the event alone so that widget scrolls its own content.
        node = w
        inside_main = False
        while node is not None:
            if isinstance(node, (tk.Text, tk.Listbox)):
                return None
            if isinstance(node, tk.Canvas) and node is self.content_canvas:
                inside_main = True
                break
            try:
                node = node.master
            except Exception:
                break
        # bind_all is application-wide: the pointer may be over a separate
        # Toplevel (e.g. the category picker). Only scroll the main page when
        # the pointer is actually inside this window's scrollable content.
        if not inside_main:
            return None
        return self._scroll_content(event)

    def _scroll_content(self, event):
        """Scroll the main canvas by the wheel delta, if it can move."""
        if not self.content_shell.winfo_ismapped():
            return None
        canvas = self.content_canvas
        try:
            first, last = canvas.yview()
            if first <= 0.0 and last >= 1.0:
                return None  # nothing to scroll
        except Exception:
            return None
        delta = -1 if event.delta > 0 else 1
        step = max(1, int(abs(event.delta) / 120)) * 3
        canvas.yview_scroll(delta * step, "units")
        return "break"

    def _apply_view(self):
        compact = self.compact_override
        if compact is None:
            compact = self.winfo_width() < 650 or self.winfo_height() < 480
        if compact:
            self.content_shell.pack_forget()
            self.view_btn.configure(text="Mở đầy đủ")
        else:
            if not self.content_shell.winfo_manager():
                self.content_shell.pack(fill="both", expand=True)
            self.view_btn.configure(text="Thu gọn")


    def _resize_view(self, event):
        if event.widget is self:
            self._apply_view()


    def _toggle_view(self):
        self.compact_override = bool(self.content_shell.winfo_manager())
        if self.compact_override:
            self.state("normal")
            try:
                self.attributes("-zoomed", False)
            except tk.TclError:
                pass
            self.geometry("380x190")
        else:
            try:
                self.state("zoomed")  # Windows: maximize within the desktop work area.
            except tk.TclError:
                try:
                    self.attributes("-zoomed", True)
                except tk.TclError:
                    self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
        self._apply_view()



if __name__ == "__main__":
    exit_code = 0
    try:
        print("=" * 90)
        print("Video Story Publisher V21.2 - AUTO-INSTALL + NEVER-CLOSE DEBUG")
        print("V21.2: page-break primary = {{nextpage}} (theo tool V27 chạy OK trên viralstory.biz); fallback <!--nextpage-->.")
        print("Unhandled Python errors are printed here and saved to:")
        print(CRASH_LOG_FILE)
        print("=" * 90)

        app = App()
        app.mainloop()

        print("\nGUI mainloop ended.")
        print("If you did NOT intentionally close the tool, inspect the log above.")
    except KeyboardInterrupt:
        print("\nStopped by keyboard interrupt.")
    except BaseException as e:
        exit_code = 1
        import traceback
        print("\n" + "=" * 90)
        print("FATAL APPLICATION ERROR")
        print("=" * 90)
        traceback.print_exc()
        print(f"\nCrash log saved to: {CRASH_LOG_FILE}")
        print("=" * 90)
        _write_crash_log("FATAL APPLICATION ERROR", type(e), e, e.__traceback__)
    finally:
        print("\n" + "=" * 90)
        print(f"PROCESS FINISHED | exit code={exit_code}")
        print("THIS CONSOLE WILL NOT CLOSE AUTOMATICALLY.")
        print("Press ENTER only when you have finished reading/copying the error.")
        print("=" * 90)
        try:
            input()
        except Exception:
            import time
            while True:
                time.sleep(3600)
