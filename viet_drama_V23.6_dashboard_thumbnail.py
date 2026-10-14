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

# KHONG dat OMP_NUM_THREADS / MKL_NUM_THREADS / OPENBLAS_NUM_THREADS o day.
#
# Do that tren may nay (Xeon E5-2673 v3, 48 logical CPU, video 30s, model small):
#   khong dat gi                     -> 28s   <-- nhanh nhat
#   OPENBLAS_NUM_THREADS=2           -> 106s  (cham 3.8x)
#   OMP_NUM_THREADS=2                -> 145s  (cham 5x)
#   OMP=12 -> 141s | OMP=16 -> 110s
# => CHI CAN dat mot trong cac bien nay la ctranslate2 chuyen sang duong song song
# kem hon va Whisper cham gap 4-5 lan. Ban cu dat ca 3 bien = 2 nen tool "dung" rat
# lau o buoc Transcribing (de bi tuong la treo).
# Cach dung: de nguyen cac bien moi truong, dieu khien so thread bang tham so
# cpu_threads cua WhisperModel (xem _WHISPER_CHILD).

# Ep OFFLINE truoc MOI import: ensure_packages() o duoi co import faster_whisper,
# va huggingface_hub doc bien nay NGAY LUC import (khong doc lai sau). Neu de muon
# thi faster-whisper kiem tra huggingface.co moi lan nap model -> do that: nap
# model "small" mat 172s khi con cho mang, chi 3.4s khi HF_HUB_OFFLINE=1.
# Model da nam san trong cache C:\Users\<user>\.cache\huggingface.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

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
# Robust numeric parsing (so model tra ve co the dinh ky tu la)
# ---------------------------
_NUM_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")

def _clean_number_text(value):
    """Chuan hoa chuoi so: fullwidth, NBSP, zero-width, dau gach Unicode."""
    if isinstance(value, str):
        import unicodedata
        value = unicodedata.normalize("NFKC", value)
        value = value.replace(" ", " ").replace(" ", " ")
        for _zw in ("​", "‌", "‍", "﻿"):
            value = value.replace(_zw, "")
        value = value.replace("−", "-")
    return value

def safe_float(value, default=0.0):
    """float() KHONG BAO GIO crash. Dung cho moi so den tu model."""
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    cleaned = _clean_number_text(value)
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        pass
    if isinstance(cleaned, str):
        probe = cleaned
        if "," in probe and "." not in probe:
            probe = probe.replace(",", ".")
        m = _NUM_RE.search(probe)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                pass
    try:
        return float(default)
    except (TypeError, ValueError):
        return default

def safe_int(value, default=0):
    """int() KHONG BAO GIO crash (ke ca '3.0' hay so fullwidth)."""
    f = safe_float(value, None)
    if f is None:
        try:
            return int(default)
        except (TypeError, ValueError):
            return 0
    return int(f)

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

    def pip_install_pinned(spec):
        """Cai DUNG phien ban (khong --upgrade) - dung cho cac goi phai khoa."""
        print(f"[SETUP] Pinning: {spec}")
        cmd = [
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check",
            "--no-input",
            spec
        ]
        subprocess.check_call(cmd)

    def module_version(module_name):
        try:
            mod = importlib.import_module(module_name)
            return str(getattr(mod, "__version__", "") or "")
        except Exception:
            return ""

    def ensure_ctranslate2():
        """Khoa ctranslate2 ve ban CHAY DUOC.

        ctranslate2 4.6.1 -> 4.8.2 CRASH native `access violation` ngay khi nap model
        Whisper (do that tren may nay: ca 7 ban deu crash; 4.6.0 on dinh 8/8 lan).
        faster-whisper chi doi `ctranslate2<5,>=4.0` nen neu no duoc cai lai (hoac ai do
        chay `pip install -U faster-whisper`) pip se keo ve ban MOI nhat = ban crash.
        Vi vay: neu thay ban KHAC 4.6.0 thi cai lai dung 4.6.0, va keo setuptools<81
        (ct2 4.6.0 can pkg_resources).
        """
        target = "4.6.0"
        current = module_version("ctranslate2")
        if current == target:
            return
        try:
            if current:
                print(f"[SETUP] ctranslate2 {current} khong chay duoc tren may nay "
                      f"(crash khi nap Whisper) -> ha ve {target}.")
            pip_install_pinned(f"ctranslate2=={target}")
            pip_install_pinned("setuptools<81")
        except Exception as e:
            print(f"[SETUP] Khong khoa duoc ctranslate2=={target}: {e}")

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

    # Khoa ctranslate2 ve ban chay duoc (4.6.0). Phai chay SAU vong cai o tren vi
    # cai "faster-whisper" bang --upgrade co the keo ctranslate2 ve ban crash.
    ensure_ctranslate2()

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
import json, os, sys, wave
import numpy as np

# Ep OFFLINE truoc khi import faster_whisper: huggingface_hub chi doc bien nay
# LUC IMPORT, nen dat sau se vo hieu (faster-whisper se cho mang 172s moi lan nap).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
# KHONG dat OMP/MKL/OPENBLAS_NUM_THREADS: dat bat ky gia tri nao cung lam
# ctranslate2 cham gap 4-5 lan (do that: khong dat 28s, OPENBLAS=2 -> 106s,
# OMP=2 -> 145s). So thread dieu khien bang cpu_threads cua WhisperModel.
from faster_whisper import WhisperModel

source, model_name, device, compute, beam, output = sys.argv[1:]

# So thread CPU: uoc luong hop ly theo so CPU cua may (mac dinh 12).
try:
    cpu_total = os.cpu_count() or 8
except Exception:
    cpu_total = 8
threads = max(4, min(12, cpu_total // 4))

# Doc WAV bang thu vien chuan (khong dung PyAV: faster-whisper 1.2.1 goi
# av.open(metadata_errors=...) ma PyAV 19 khong nhan -> TypeError moi lan
# transcribe duong dan). Dua MANG numpy cho transcribe() thi khong cham tới PyAV.
with wave.open(source, "rb") as handle:
    raw = handle.readframes(handle.getnframes())
audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

model = WhisperModel(model_name, device=device, compute_type=compute,
                     cpu_threads=threads, num_workers=1)
# vad_filter=False: VAD nap onnxruntime, ma onnxruntime tren may nay no
# "access violation" ngay khi import (Silero VAD) -> crash native.
segments, info = model.transcribe(audio, vad_filter=False, beam_size=int(beam),
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
APP_VERSION = "23.6.35"

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
    # "language"/"language_enabled" KHONG nam o day: nguoi dung chon ngon ngu o
    # tab SmartTraffic. Neu ghim o day, apply_fixed_production_settings() se GHI DE
    # lua chon cua nguoi dung -> ngon ngu KHONG BAO GIO doi du da tich.
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
    # Logo YouTube: KHONG ghim o day. Nguoi dung bat/tat va chinh kieu/kich thuoc
    # o tab Image hosting. Neu ghim o day, apply_fixed_production_settings() se
    # GHI DE lua chon cua nguoi dung -> logo KHONG BAO GIO hien du da tich.
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

# Cau chen trong ten file theo TUNG NGON NGU (dung khi nguoi dung chua tu nhap
# danh sach rieng trong o 'Cau chien trong ten file'). Ten file la thu hien ra
# cho nguoi xem, nen phai cung ngon ngu voi truyen.
DEFAULT_FILENAME_PHRASES_BY_LANGUAGE = {
    "English": DEFAULT_FILENAME_PHRASES,
    "Vietnamese": "\n".join([
        "PHẦN 2 & KẾT THÚC TRỌN BỘ", "XEM PHẦN TIẾP THEO TẠI ĐÂY",
        "CHUYỆN GÌ XẢY RA TIẾP? XEM TẠI ĐÂY", "XEM TRỌN BỘ PHIM TẠI ĐÂY",
        "XEM KẾT THÚC NHƯ THẾ NÀO", "XEM TIẾP TẠI ĐÂY",
    ]),
    "Spanish": "\n".join([
        "PARTE 2 Y FINAL COMPLETO", "MIRA LA SIGUIENTE PARTE AQUÍ",
        "¿QUÉ PASA DESPUÉS? MÍRALO AQUÍ", "MIRA LA HISTORIA COMPLETA AQUÍ",
        "DESCUBRE CÓMO TERMINA", "SIGUE VIENDO AQUÍ",
    ]),
    "Portuguese": "\n".join([
        "PARTE 2 E FINAL COMPLETO", "ASSISTA À PRÓXIMA PARTE AQUI",
        "O QUE ACONTECE DEPOIS? VEJA AQUI", "ASSISTA À HISTÓRIA COMPLETA AQUI",
        "VEJA COMO TERMINA", "CONTINUE ASSISTINDO AQUI",
    ]),
    "French": "\n".join([
        "PARTIE 2 ET FIN COMPLÈTE", "REGARDEZ LA SUITE ICI",
        "QUE SE PASSE-T-IL ENSUITE ? VOIR ICI", "REGARDEZ L'HISTOIRE COMPLÈTE ICI",
        "VOYEZ COMMENT ÇA FINIT", "CONTINUEZ À REGARDER ICI",
    ]),
    "German": "\n".join([
        "TEIL 2 & KOMPLETTES ENDE", "HIER DEN NÄCHSTEN TEIL ANSEHEN",
        "WAS PASSIERT ALS NÄCHSTES? HIER ANSEHEN", "HIER DIE GANZE GESCHICHTE ANSEHEN",
        "SIEH, WIE ES ENDET", "HIER WEITERSCHAUEN",
    ]),
    "Italian": "\n".join([
        "PARTE 2 E FINALE COMPLETO", "GUARDA LA PROSSIMA PARTE QUI",
        "COSA SUCCEDE DOPO? GUARDA QUI", "GUARDA LA STORIA COMPLETA QUI",
        "SCOPRI COME FINISCE", "CONTINUA A GUARDARE QUI",
    ]),
    "Indonesian": "\n".join([
        "BAGIAN 2 & AKHIR CERITA", "TONTON BAGIAN SELANJUTNYA DI SINI",
        "APA YANG TERJADI SELANJUTNYA? TONTON DI SINI", "TONTON CERITA LENGKAPNYA DI SINI",
        "LIHAT BAGAIMANA AKHIRNYA", "LANJUT TONTON DI SINI",
    ]),
    "Thai": "\n".join([
        "ภาค 2 และตอนจบ", "ดูตอนต่อไปได้ที่นี่",
        "เกิดอะไรขึ้นต่อ? ดูที่นี่", "ดูเรื่องเต็มได้ที่นี่",
        "ดูว่าจบอย่างไร", "ดูต่อที่นี่",
    ]),
    "Filipino": "\n".join([
        "PART 2 AT BUONG PAGWAWAKAS", "PANONOORIN ANG SUSUNOD NA BAHAGI DITO",
        "ANO ANG SUSUNOD? PANOORIN DITO", "PANONOORIN ANG BUONG KUWENTO DITO",
        "TINGNAN KUNG PAANO NAGWAKAS", "MAGPATULOY SA PANONOOD DITO",
    ]),
    "Hindi": "\n".join([
        "भाग 2 और पूरा अंत", "अगला भाग यहाँ देखें",
        "आगे क्या हुआ? यहाँ देखें", "पूरी कहानी यहाँ देखें",
        "देखें कैसे खत्म होता है", "यहाँ देखते रहें",
    ]),
    "Arabic": "\n".join([
        "الجزء 2 والنهاية الكاملة", "شاهد الجزء التالي هنا",
        "ماذا حدث بعد ذلك؟ شاهد هنا", "شاهد القصة كاملة هنا",
        "شاهد كيف تنتهي", "تابع المشاهدة هنا",
    ]),
    "Turkish": "\n".join([
        "2. BÖLÜM VE TAM FİNAL", "SONRAKİ BÖLÜMÜ BURADA İZLE",
        "SONRA NE OLUYOR? BURADA İZLE", "TÜM HİKÂYEYİ BURADA İZLE",
        "NASIL BİTTİĞİNİ GÖR", "BURADA İZLEMEYE DEVAM ET",
    ]),
    "Japanese": "\n".join([
        "パート2と完全な結末", "続きはこちらで視聴",
        "この後どうなる？こちらで視聴", "全話はこちらで視聴",
        "結末を見る", "続きを視聴する",
    ]),
    "Korean": "\n".join([
        "2부와 완전한 결말", "다음 편은 여기서 보기",
        "그다음엔 어떻게? 여기서 보기", "전체 이야기는 여기서 보기",
        "결말 보기", "여기서 계속 보기",
    ]),
    "Chinese": "\n".join([
        "第2部分与完整结局", "在这里观看下一部分",
        "接下来会发生什么？在这里观看", "在这里观看完整故事",
        "看看结局如何", "在这里继续观看",
    ]),
}


def fallback_hashtags_for_language(language_key: str) -> list:
    """Hashtag du phong khi AI khong tra du 3 the.

    Phai la ASCII (khong dau) vi ten file di qua o mang/WebDAV.
    """
    lang = str(language_key or "English").strip() or "English"
    table = {
        "English": ["#story", "#drama", "#fullstory"],
        "Vietnamese": ["#truyen", "#tamly", "#fullstory"],
        "Spanish": ["#historia", "#drama", "#final"],
        "Portuguese": ["#historia", "#drama", "#final"],
        "French": ["#histoire", "#drame", "#final"],
        "German": ["#geschichte", "#drama", "#finale"],
        "Italian": ["#storia", "#dramma", "#finale"],
        "Indonesian": ["#cerita", "#drama", "#akhir"],
        "Thai": ["#story", "#drama", "#ending"],
        "Filipino": ["#kuwento", "#drama", "#wakas"],
        "Hindi": ["#kahani", "#drama", "#ending"],
        "Arabic": ["#story", "#drama", "#ending"],
        "Turkish": ["#hikaye", "#dram", "#final"],
        "Japanese": ["#story", "#drama", "#ending"],
        "Korean": ["#story", "#drama", "#ending"],
        "Chinese": ["#story", "#drama", "#ending"],
    }
    return list(table.get(lang, table["English"]))


# Tap hop MOI danh sach mac dinh (moi ngon ngu). Dung de phan biet "tool tu dien"
# voi "nguoi dung tu nhap": neu o dang chua dung mot trong cac ban mac dinh nay thi
# coi nhu tool quan ly -> phai doi theo ngon ngu dang chon.
_ALL_DEFAULT_PHRASE_SETS = {
    "\n".join(re.sub(r"\s+", " ", ln).strip(" .;") for ln in v.split("\n") if ln.strip())
    for v in DEFAULT_FILENAME_PHRASES_BY_LANGUAGE.values()
}
_ALL_DEFAULT_PHRASE_SETS.add(
    "\n".join(re.sub(r"\s+", " ", ln).strip(" .;") for ln in DEFAULT_FILENAME_PHRASES.split("\n") if ln.strip()))


def _is_default_phrase_list(raw: str) -> bool:
    """True khi o 'Cau chien trong ten file' dang chua ban mac dinh (cua BAT KY
    ngon ngu nao) hoac de trong -> tool duoc phep tu doi theo ngon ngu."""
    norm = "\n".join(re.sub(r"\s+", " ", ln).strip(" .;")
                     for ln in str(raw or "").replace("\r", "\n").split("\n") if ln.strip())
    return (not norm) or (norm in _ALL_DEFAULT_PHRASE_SETS)


def filename_phrases_for_language(cfg: dict) -> str:
    """Danh sach cau chen ten file theo ngon ngu dang chon.

    Neu nguoi dung da TU NHAP danh sach rieng thi giu nguyen (ton trong y nguoi
    dung). Neu o dang chua ban mac dinh cua bat ky ngon ngu nao (ke ca ban da bi
    luu tu lan doi ngon ngu truoc) thi tra ve ban dung ngon ngu dang chon.
    """
    raw = str((cfg or {}).get("filename_phrase_list") or "").strip()
    if raw and not _is_default_phrase_list(raw):
        return raw
    lang = effective_language(cfg)
    return DEFAULT_FILENAME_PHRASES_BY_LANGUAGE.get(lang, DEFAULT_FILENAME_PHRASES)


def choose_filename_phrase(cfg: dict) -> str:
    """Chon ngau nhien 1 cau trong o 'Cau chien trong ten file' (theo ngon ngu)."""
    import random
    raw = filename_phrases_for_language(cfg).replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", ln).strip(" .;") for ln in raw.split("\n")]
    lines = [ln for ln in lines if ln]
    return random.choice(lines) if lines else "full story"

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
    "adsconex_base_url": "https://dramanest.gigglelo.com/api",
    "adsconex_site_host": "dramanest.gigglelo.com",
    "adsconex_category": "15",
    "adsconex_author": "",
    "adsconex_apply_image_to_all": True,
    "adsconex_retry_count": "3",
    "adsconex_retry_delay": "60",
    "adsconex_rate_limit_per_min": "6",
    "adsconex_rate_shared_path": "",
    "adsconex_rate_fallback_delay": "15",
    "slink_api_key": "",
    "slink_base_url": "https://fableon.treeiq.biz/api",
    "slink_site_host": "fableon.treeiq.biz",
    "slink_site_id": "724",
    "slink_category": "266",
    "slink_status": "published",
    "slink_retry_count": "3",
    "slink_retry_delay": "30",
    # Origin cua site co TIMEOUT XU LY ~12s: POST nao server can >12s de tao xong
    # chuong -> Cloudflare tra 502 origin_bad_gateway. Da do that: <=4 chuong/request
    # luon 201; 5-10 chuong -> 502 hang loat (token cua nguoi dung khac);
    # >=20 chuong -> 502 voi MOI token. Vi vay phai chia nho theo SO CHUONG,
    # khong chi theo so ky tu.
    "adsconex_max_chapters_per_request": "3",
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
    # Logo YouTube ve them o giua anh bia website (mac dinh TAT).
    "youtube_badge_on_cover": False,
    "youtube_badge_style": "play",
    "youtube_badge_scale": "30",
    "require_thumbnail_before_publish": True,
    "fast_whisper_beam_size": "1",
    "skip_audio_extract": True,
    "frame_workers": "4",
    "vision_parallel_workers": "2",
    "cloudinary_workers": "4",
    "ffmpeg_bin": r"C:\\ffmpeg-9.0.1-essentials_build\\bin",
    "keep_frames": False,
    # Nut tich "Ap ngon ngu da chon": False = mac dinh tieng Anh nhu truoc gio.
    "language_enabled": False,
    # SmartTraffic thinh thoang tra 5xx TAM THOI -> thu lai chinh payload do.
    # Gian cach tang dan: 20s, 40s, 60s, 80s (tong ~3,5 phut).
    "smarttraffic_retry_count": "4",
    "smarttraffic_retry_delay": "20",
}

# ---------------------------------------------------------------------------
# NGON NGU BAI VIET (chon o tab SmartTraffic, ap cho MOI truyen + moi chapter)
# ---------------------------------------------------------------------------
# Nhan o day chi de hien thi; gia tri luu trong config la KEY (xem ben duoi).
LANGUAGE_OPTIONS = [
    ("English", "English"),
    ("Vietnamese (Tiếng Việt)", "Vietnamese"),
    ("Spanish (Español)", "Spanish"),
    ("Portuguese (Português)", "Portuguese"),
    ("French (Français)", "French"),
    ("German (Deutsch)", "German"),
    ("Italian (Italiano)", "Italian"),
    ("Indonesian (Bahasa Indonesia)", "Indonesian"),
    ("Thai (ภาษาไทย)", "Thai"),
    ("Filipino (Tagalog)", "Filipino"),
    ("Hindi (हिन्दी)", "Hindi"),
    ("Arabic (العربية)", "Arabic"),
    ("Turkish (Türkçe)", "Turkish"),
    ("Japanese (日本語)", "Japanese"),
    ("Korean (한국어)", "Korean"),
    ("Chinese (中文)", "Chinese"),
]

LANGUAGE_KEYS = [key for _label, key in LANGUAGE_OPTIONS]
LANGUAGE_LABELS = {key: label for label, key in LANGUAGE_OPTIONS}
LANGUAGE_LABEL_TO_KEY = {label: key for label, key in LANGUAGE_OPTIONS}

# Cau ket caption (CTA). Phai dich theo ngon ngu, neu khong caption se bi tron
# ngon ngu: than bai tieng Nhat ma cau cuoi lai tieng Anh.
CAPTION_CTA = {
    "English": "Read the full story — link in the comments.",
    "Vietnamese": "Đọc trọn câu chuyện — link ở phần bình luận.",
    "Spanish": "Lee la historia completa — enlace en los comentarios.",
    "Portuguese": "Leia a história completa — link nos comentários.",
    "French": "Lisez l'histoire complète — lien dans les commentaires.",
    "German": "Lies die ganze Geschichte — Link in den Kommentaren.",
    "Italian": "Leggi la storia completa — link nei commenti.",
    "Indonesian": "Baca cerita lengkapnya — link ada di komentar.",
    "Thai": "อ่านเรื่องเต็มได้ที่ลิงก์ในคอมเมนต์",
    "Filipino": "Basahin ang buong kuwento — nasa komento ang link.",
    "Hindi": "पूरी कहानी पढ़ें — लिंक कमेंट में है।",
    "Arabic": "اقرأ القصة كاملة — الرابط في التعليقات.",
    "Turkish": "Hikâyenin tamamını okuyun — bağlantı yorumlarda.",
    "Japanese": "全文はコメント欄のリンクから。",
    "Korean": "전체 이야기는 댓글의 링크에서 확인하세요.",
    "Chinese": "完整故事请看评论区的链接。",
}
ALL_CTAS = sorted(set(CAPTION_CTA.values()), key=len, reverse=True)

# Dau hieu ngoi thu nhat theo tung ngon ngu. Bo kiem tra caption truoc day CHI
# nhan dai tu tieng Anh (I/my/me/we/our) nen caption tieng Viet/Nhat/... luon bi
# loai oan roi bao loi "Caption chua dat giong ke ngoi thu nhat".
FIRST_PERSON_MARKERS = {
    "English": ["I", "my", "me", "we", "our"],
    "Vietnamese": ["tôi", "mình", "tớ", "chúng tôi", "của tôi", "em", "con"],
    "Spanish": ["yo", "mi", "mis", "me", "nosotros"],
    "Portuguese": ["eu", "meu", "minha", "meus", "minhas", "nós"],
    "French": ["je", "moi", "mon", "ma", "mes", "nous"],
    "German": ["ich", "mein", "meine", "meiner", "mir", "wir", "uns"],
    "Italian": ["io", "mio", "mia", "miei", "noi", "mi"],
    "Indonesian": ["saya", "aku", "kami", "kita"],
    "Thai": ["ฉัน", "ผม", "เรา", "หนู", "ดิฉัน"],
    "Filipino": ["ako", "ko", "akin", "kami", "namin"],
    "Hindi": ["मैं", "मेरा", "मेरी", "मुझे", "हम"],
    "Arabic": ["أنا", "لي", "نحن", "أنني"],
    "Turkish": ["ben", "benim", "bana", "biz", "bizi"],
    "Japanese": ["私", "僕", "俺", "わたし", "ぼく", "私たち"],
    "Korean": ["나", "저", "내", "제", "우리"],
    "Chinese": ["我", "我们", "我的", "咱們"],
}

# Ngon ngu khong dan cach bang dau cach -> do do dai bang KY TU, khong phai "tu".
NO_SPACE_LANGUAGES = {"Japanese", "Korean", "Chinese", "Thai"}

# Ngon ngu "pro-drop": nguoi ke thuong BO dai tu ("Todavia recuerdo..." thay vi
# "Yo todavia recuerdo..."). Chi kiem dai tu la se loai oan caption dung.
# Them dong tu ngoi thu nhat de nhan dien.
FIRST_PERSON_VERBS = {
    "Spanish": [
        "recuerdo", "conduje", "supe", "vi", "dije", "tenia", "estaba", "fui", "era",
        "se", "creo", "quiero", "puedo", "debo", "pienso", "siento", "mire", "llegue",
        "quede", "conte", "descubri", "decidi", "llame", "oi", "escuche", "entendi",
        "pense", "hable", "tome", "deje", "volvi", "sali", "entre", "perdi", "encontre",
        "empece", "termine", "espere", "necesite", "pude", "tuve", "hice", "vine",
        "puse", "traje", "sabia", "queria", "podia", "hacia", "decia", "veia", "venia",
        "tengo", "hago", "veo", "vengo", "digo", "pongo", "conozco", "entiendo",
    ],
    "Italian": [
        "ricordo", "sapevo", "vidi", "dissi", "avevo", "ero", "so", "credo", "voglio",
        "posso", "devo", "penso", "sento", "guardai", "arrivai", "restai", "raccontai",
        "scoprii", "decisi", "chiamai", "udii", "ascoltai", "capii", "pensai", "parlai",
        "presi", "lasciai", "tornai", "uscii", "entrai", "persi", "trovai", "iniziai",
        "finii", "aspettai", "ebbi", "feci", "venni", "misi", "portai", "conosco",
    ],
    "Portuguese": [
        "lembro", "conduzi", "soube", "vi", "disse", "tinha", "estava", "fui", "era",
        "sei", "creio", "quero", "posso", "devo", "penso", "sinto", "olhei", "cheguei",
        "fiquei", "contei", "descobri", "decidi", "chamei", "ouvi", "escutei", "entendi",
        "pensei", "falei", "tomei", "deixei", "voltei", "sai", "entrei", "perdi",
        "encontrei", "comecei", "terminei", "esperei", "precisei", "pude", "tive",
        "fiz", "vim", "pus", "trouxe", "sabia", "queria", "podia", "fazia", "dizia",
        "via", "vinha", "tenho", "faco", "vejo", "venho", "digo", "conheco", "entendo",
    ],
}


# Cau nhac viet lai caption, dich theo ngon ngu.
CAPTION_REWRITE_NOTE = {
    "English": "REWRITE: First-person personal confession, 180-300 words, one sentence or dialogue line per paragraph, each separated by a blank line; no scene or camera descriptions. Keep the same established facts and open loop.",
    "Vietnamese": "VIẾT LẠI: Lời tự thuật ngôi thứ nhất, 180-300 từ, mỗi câu hoặc lời thoại một đoạn, cách nhau bằng dòng trống; không mô tả cảnh quay hay máy quay. Giữ nguyên các tình tiết đã có và giữ nguyên open loop.",
    "Spanish": "REESCRIBE: Confesión personal en primera persona, 180-300 palabras, una frase o línea de diálogo por párrafo, separadas por una línea en blanco; sin descripciones de escena ni de cámara. Mantén los mismos hechos y el mismo open loop.",
    "Portuguese": "REESCREVA: Confissão pessoal em primeira pessoa, 180-300 palavras, uma frase ou fala por parágrafo, separadas por uma linha em branco; sem descrições de cena ou câmera. Mantenha os mesmos fatos e o mesmo open loop.",
    "French": "RÉÉCRIVEZ : Confession personnelle à la première personne, 180-300 mots, une phrase ou une réplique par paragraphe, séparées par une ligne vide ; aucune description de scène ni de caméra. Gardez les mêmes faits et le même open loop.",
    "German": "NEU SCHREIBEN: Persönliches Geständnis in der Ich-Form, 180-300 Wörter, ein Satz oder eine Dialogzeile pro Absatz, durch eine Leerzeile getrennt; keine Szenen- oder Kamerabeschreibungen. Behalte dieselben Fakten und denselben Open Loop.",
    "Italian": "RISCRIVI: Confessione personale in prima persona, 180-300 parole, una frase o battuta per paragrafo, separate da una riga vuota; nessuna descrizione di scena o di camera. Mantieni gli stessi fatti e lo stesso open loop.",
    "Indonesian": "TULIS ULANG: Pengakuan pribadi orang pertama, 180-300 kata, satu kalimat atau dialog per paragraf, dipisah baris kosong; tanpa deskripsi adegan atau kamera. Pertahankan fakta dan open loop yang sama.",
    "Thai": "เขียนใหม่: คำสารภาพส่วนตัวในมุมมองบุคคลที่หนึ่ง 180-300 คำ ประโยคหรือบทสนทนาละหนึ่งย่อหน้า คั่นด้วยบรรทัดว่าง ไม่บรรยายฉากหรือกล้อง รักษาข้อเท็จจริงและ open loop เดิมไว้",
    "Filipino": "ISULAT MULI: Personal na pagtatapat sa unang panauhan, 180-300 salita, isang pangungusap o dayalogo bawat talata, pinaghihiwalay ng blangkong linya; walang paglalarawan ng eksena o kamera. Panatilihin ang parehong mga detalye at open loop.",
    "Hindi": "फिर से लिखें: प्रथम पुरुष में व्यक्तिगत स्वीकारोक्ति, 180-300 शब्द, प्रत्येक पैराग्राफ में एक वाक्य या संवाद, बीच में खाली पंक्ति; दृश्य या कैमरा विवरण नहीं। वही तथ्य और open loop बनाए रखें।",
    "Arabic": "أعد الكتابة: اعتراف شخصي بصيغة المتكلم، 180-300 كلمة، جملة أو سطر حوار لكل فقرة، تفصل بينها سطر فارغ؛ دون وصف للمشهد أو الكاميرا. حافظ على الوقائع نفسها وopen loop نفسه.",
    "Turkish": "YENİDEN YAZ: Birinci tekil şahıstan kişisel itiraf, 180-300 kelime, her paragrafta bir cümle veya diyalog satırı, aralarında boş satır; sahne veya kamera betimlemesi yok. Aynı olayları ve aynı open loop'u koru.",
    "Japanese": "書き直し: 一人称の個人的な告白、180〜300語、1文またはセリフごとに1段落とし、段落の間は空行で区切る。場面やカメラの描写は入れない。同じ事実と open loop を維持すること。",
    "Korean": "다시 쓰기: 1인칭 개인적 고백, 180-300단어, 문장 또는 대사 하나당 한 문단, 문단 사이는 빈 줄로 구분; 장면이나 카메라 묘사 금지. 동일한 사실과 open loop를 유지할 것.",
    "Chinese": "重写：第一人称的个人自白，180-300 字，每段一句或一句对白，段间空一行；不要场景或镜头描写。保持相同的事实和 open loop。",
}


def language_label(language_key: str) -> str:
    """Nhan hien thi cua mot ngon ngu; nhan la thi tra ve nguyen van."""
    key = str(language_key or "").strip()
    return LANGUAGE_LABELS.get(key, key or "English")


def language_prompt_name(language_key: str) -> str:
    """Ten ngon ngu gon (bo phan trong ngoac) de dua vao prompt.

    Nhan hien thi la "Japanese (日本語)" nhung trong prompt chi nen ghi
    "Japanese" cho sach, tranh tron ky tu vao cau lenh.
    """
    label = language_label(language_key)
    return label.split(" (")[0].strip() or "English"


def effective_language(cfg) -> str:
    """Ngon ngu thuc su dung: KHONG tich 'language_enabled' -> luon tieng Anh."""
    try:
        enabled = bool((cfg or {}).get("language_enabled", False))
    except Exception:
        enabled = False
    if not enabled:
        return "English"
    key = str((cfg or {}).get("language") or "English").strip() or "English"
    return key if key in LANGUAGE_LABELS else "English"


def strip_language_rules(prompt: str, language_key: str) -> str:
    """Bo MOI cau ghim ngon ngu trong prompt goc khi nguoi dung da chon ngon ngu.

    Prompt goc ghim tieng Anh o vai cho ("viet bang English", "natural American
    English", "doc gia My/Au"...). Neu de nguyen thi model nhan 2 lenh trai nhau
    va ket qua lai ra tieng Anh. Ham nay thay cac cau do bang ngon ngu da chon,
    nen truyen chi theo DUNG MOT ngon ngu, mac ke video/kich ban goc la gi.
    """
    label = language_prompt_name(language_key)
    if not prompt:
        return prompt or ""
    if label == "English":
        return prompt  # Mac dinh: giu nguyen prompt goc, khong dung tới.

    out = prompt
    replacements = [
        # Prompt goc (tieng Viet, viet bang \n that trong chuoi)
        ("Full Story và Caption viết bằng **English**.",
         f"Full Story và Caption viết bằng **{label}**."),
        ("Ưu tiên **natural American English**.",
         f"Ưu tiên văn phong tự nhiên của người bản xứ {label}."),
        ("Tên tiếng Anh tự nhiên, dễ hiểu với người Mỹ, tạo tò mò nhưng không spoil kết thúc.",
         f"Tên truyện viết bằng {label}, tự nhiên, dễ hiểu, tạo tò mò nhưng không spoil kết thúc."),
        ("dành cho độc giả Mỹ/Âu, đặc biệt nhóm trung niên và lớn tuổi",
         f"dành cho độc giả nói tiếng {label}, đặc biệt nhóm trung niên và lớn tuổi"),
        # Addendum tieng Anh
        ("Write the FULL BLOG STORY in natural American English",
         f"Write the FULL BLOG STORY in {label}"),
        ("Use natural American English, about 180-300 words.",
         f"Use {label}, about 180-300 words."),
    ]
    for old, new in replacements:
        out = out.replace(old, new)
    return out


def language_instruction(language_key: str) -> str:
    """Chi thi ngon ngu, noi vao MOI loi goi Writer.

    Khong co phan nay thi model tu do tra loi bang tieng Anh (theo prompt goc),
    nen phai nhac ro va nhac lai o tung request: story architecture, tung batch
    chapter, metadata, Facebook caption va tieu de video.
    """
    label = language_prompt_name(language_key)
    if label == "English":
        # Mac dinh: giu nguyen hanh vi cu, khong them chi thi nao.
        return ""
    return (
        "\n\n# NGON NGU BAT BUOC — AP CHO TOAN BO TRUYEN\n"
        f"Write EVERYTHING in {label}: the story title, the meta title, the meta description, "
        f"the keywords, every chapter title, every chapter body, and the Facebook caption.\n"
        f"VIET TOAN BO BANG {label}: ten truyen, meta title, meta description, keywords, "
        f"tieu de TUNG chapter, noi dung TUNG chapter va caption Facebook. "
        f"KHONG duoc tron ngon ngu khac, KHONG giu lai cau tieng Anh nao, KE CA khi nguon "
        f"(kich ban / hook video) dang la tieng Anh — phai dich va viet lai sang {label}.\n"
        "Keep proper nouns (character names, place names, brand names) in their original form; "
        "do not translate them.\n"
        f"RULES ABOVE THAT MENTION ENGLISH OR AMERICAN ENGLISH ARE OVERRIDDEN: the language to "
        f"use is {label}.\n"
        f"Chapter headings keep the tool's exact format (the word \"Chapter\" + number), but the "
        f"chapter TITLE and BODY are written in {label}."
    )

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
                            adsconex_site_host: str = "dramanest.gigglelo.com",
                            slink_site_host: str = "fableon.treeiq.biz") -> dict:
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
            _p = str(net_provider or "").strip().lower()
            if _p.startswith("ads"):
                existing_link = published_adsconex_link(result, adsconex_site_host)
            elif _p.startswith("slink"):
                existing_link = published_slink_link(result, slink_site_host)
            else:
                existing_link = published_article_link(result, site_host)
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

def _adsconex_retry_after_seconds(response, default=60.0):
    """Doc Retry-After (giay hoac HTTP-date) tu header, fallback tu body JSON Cloudflare."""
    raw = ""
    try:
        raw = str(response.headers.get("Retry-After") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        try:
            body = response.json()
            if isinstance(body, dict):
                for key in ("retry_after", "retryAfter"):
                    if body.get(key) not in (None, ""):
                        raw = str(body.get(key)).strip()
                        break
        except Exception:
            raw = ""
    # Chi parse so khi chuoi THUC SU la so. Neu khong, "Wed, 21 Oct 2099 07:28:00 GMT"
    # se bi safe_float doc thanh 21 (ngay trong thang) -> backoff sai.
    seconds = None
    if raw and re.fullmatch(r"\d+(?:\.\d+)?", raw.strip()):
        seconds = safe_float(raw, None)
    if seconds is None:
        try:
            from email.utils import parsedate_to_datetime
            import datetime as _dt
            when = parsedate_to_datetime(raw)
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_dt.timezone.utc)
                seconds = (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        except Exception:
            seconds = None
    if seconds is None:
        seconds = safe_float(default, 60.0)
    return max(0.0, min(600.0, float(seconds)))


def _adsconex_is_transient(status_code, payload=None):
    """True khi loi TAM THOI (Cloudflare origin 502/503/504/520-524, rate limit 429).

    Loai tru 500: request co the da duoc origin xu ly mot phan, retry se tao bai trung.
    """
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return False
    if code in (429, 502, 503, 504, 520, 521, 522, 523, 524):
        return True
    if isinstance(payload, dict) and payload.get("retryable") is True:
        return True
    return False


# ---------------------------------------------------------------------------
# GIOI HAN TOC DO DANG ADSCONEX (nhieu may chay song song, dung chung 1 tran)
# ---------------------------------------------------------------------------
# Net gioi han ~6-10 bai/phut (1 bai = 1 POST mode=chapter = ca truyen ~10 chuong).
# N may chay song song ma moi may tu gian cach thi tong van vuot tran -> 502 hang loat.
# Cach lam: truoc khi POST, may "danh dau" 1 bai vao thu muc CHUNG tren o Z, sau khi
# dem so dau trong 60s qua. Du tran thi cho. Cua so TRUOT 60s duoc bao dam: khong bao
# gio co qua N dau trong BAT KY cua so 60s nao (khong phai "N bai moi phut" kieu cua
# so co dinh - kieu do van lot 2N bai o ranh gioi).
# CHI dung 2 thu da kiem chung chay dung tren RaiDrive/WebDAV:
#   - O_CREAT|O_EXCL lam mutex (stress 40 vong: moi vong dung 1 nguoi thang)
#   - liet ke thu muc luon thay file do may khac vua tao
# KHONG doc noi dung file: o Z cache noi dung doc (doc lai file vua ghi mat 0.000s)
# nen doc file trang thai co the ra ban CU -> hai may cung tuong con luot -> lot tran.
# O Z loi / khong truy cap duoc -> tu gian cach tai may nay, tool khong bi treo.
ADSCONEX_RATE_DEFAULT_PATH = r"Z:\HQData-2\TOOLS TỔNG HỢP\_adsconex_rate"
ADSCONEX_RATE_LOCK_NAME = "_lock"
ADSCONEX_RATE_WINDOW = 60.0             # cua so dem luot dang (giay)
ADSCONEX_RATE_KEEP = 180.0              # xoa dau cu hon muc nay (giay)
ADSCONEX_RATE_LOCK_STALE = 30.0         # khoa cu hon -> may do da crash, pha duoc
ADSCONEX_RATE_LOCK_WAIT_MAX = 120.0     # cho khoa qua lau -> coi nhu o Z loi
ADSCONEX_RATE_SLOT_WAIT_MAX = 25.0      # cho luot dang qua lau -> gian cach tai may
ADSCONEX_RATE_CLOCK_SKEW_MAX = 90.0     # mtime vuot muc nay = dong ho sai han -> rac
ADSCONEX_RATE_MIN_GAP_SAFETY = 0.5     # bien an toan cho gian cach toi thieu (giay)
_adsconex_local_lock = threading.Lock()
_adsconex_local_last_post = [0.0]


def _adsconex_rate_dir(cfg) -> str:
    """Thu muc chung giua cac may. De trong -> duong dan mac dinh tren o Z."""
    raw = str(cfg.get("adsconex_rate_shared_path") or "").strip()
    return raw or ADSCONEX_RATE_DEFAULT_PATH


def _adsconex_rate_limits(cfg):
    """(so bai toi da moi phut cho TAT CA cac may, gian cach tai may khi Z loi)."""
    per_min = max(1, min(60, safe_int(cfg.get("adsconex_rate_limit_per_min", 6), 6)))
    gap = max(0.0, min(600.0, safe_float(cfg.get("adsconex_rate_fallback_delay", 15), 15.0)))
    return per_min, gap


def _adsconex_local_wait(gap_seconds) -> float:
    """Fallback khi o Z khong dung duoc: gian cach tai may nay."""
    if gap_seconds <= 0:
        return 0.0
    with _adsconex_local_lock:
        now = time.time()
        wait = _adsconex_local_last_post[0] + gap_seconds - now
        if wait > 0:
            time.sleep(wait)
        _adsconex_local_last_post[0] = time.time()
        return max(0.0, wait)


def _adsconex_claim_slot(cfg):
    """Thu danh dau 1 luot dang.

    Tra ("shared", 0.0)     -> da danh dau duoc, duoc phep POST
        ("wait", giay)      -> het luot, cho roi thu lai
        ("fallback", gap)   -> o Z khong dung duoc, gian cach tai may
    """
    per_min, local_gap = _adsconex_rate_limits(cfg)
    dir_path = _adsconex_rate_dir(cfg)
    lock_path = os.path.join(dir_path, ADSCONEX_RATE_LOCK_NAME)
    try:
        os.makedirs(dir_path, exist_ok=True)
    except Exception:
        return ("fallback", local_gap)

    waited = 0.0
    while True:
        fd = None
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            # RaiDrive (WebDAV) tra PermissionError [Errno 13] thay vi
            # FileExistsError khi file khoa DA ton tai -> van la "may khac dang giu".
            # Phan biet voi "o Z hong" bang cach kiem tra thu muc cha.
            if not os.path.isdir(dir_path):
                return ("fallback", local_gap)
            try:
                if time.time() - os.path.getmtime(lock_path) > ADSCONEX_RATE_LOCK_STALE:
                    os.remove(lock_path)
            except OSError:
                pass
            if waited >= ADSCONEX_RATE_LOCK_WAIT_MAX:
                return ("fallback", local_gap)
            time.sleep(0.05)
            waited += 0.05
            continue
        except OSError:
            # Khong mo duoc file tren o mang -> gian cach tai may.
            return ("fallback", local_gap)

        try:
            now = time.time()
            try:
                names = os.listdir(dir_path)
            except OSError:
                return ("fallback", local_gap)
            inside = []
            for name in names:
                if not (name.startswith("s_") and name.endswith(".mrk")):
                    continue
                mark_path = os.path.join(dir_path, name)
                try:
                    mtime = os.path.getmtime(mark_path)
                except OSError:
                    continue
                # Lech dong ho giua cac may: mtime co the nam o TUONG LAI. Dung nguyen
                # gia tri do thi `since_last` am -> wait_s luon lon -> vong xin luot lap
                # vo han, tool TREO khong bao gio dang duoc (da sap that: 2 may lech ~45s).
                # Kep mtime ve hien tai; dau lech qua xa = dong ho sai han -> coi la rac.
                if mtime > now:
                    # mtime o TUONG LAI = dong ho may ghi mark di truoc may nay. Khong the
                    # tinh gian cach tu no (since_last se am -> wait vo han), cung khong nen
                    # keo ve `now` vi nhu vay mark cu se chan mai. Bo qua khi tinh cua so.
                    continue
                if now - mtime > ADSCONEX_RATE_KEEP:
                    try:
                        os.remove(mark_path)
                    except OSError:
                        pass
                    continue
                if (now - mtime) < ADSCONEX_RATE_WINDOW:
                    inside.append(mtime)
            # Gian cach toi thieu giua 2 bai lien tiep. Neu chi dem theo cua so
            # 60s thi 6 bai dau se dong loat trong ~0.5s (da do that) -> origin bi
            # soc -> 502, du khong vuot tran.
            # Dung LAN DANG CUOI DO CHINH MAY NAY ghi (dong ho cua chinh minh) chu
            # KHONG dung mtime cua mark: cac may co the lech dong ho nhau, mtime cua
            # may khac khong so sanh duoc voi dong ho may nay (da sap that: lech ~45s
            # lam since_last am -> wait vo han -> tool treo).
            min_gap = (ADSCONEX_RATE_WINDOW / per_min) + ADSCONEX_RATE_MIN_GAP_SAFETY
            with _adsconex_local_lock:
                last_local = _adsconex_local_last_post[0]
            since_last = (now - last_local) if last_local > 0 else None
            if since_last is not None and since_last < min_gap:
                wait_s = max(0.2, min_gap - since_last + 0.05)
            elif len(inside) < per_min:
                mark_path = os.path.join(dir_path, "s_%d_%s_%d.mrk" % (
                    int(now * 1000), platform.node(), os.getpid()))
                with open(mark_path, "w", encoding="utf-8") as fh:
                    fh.write("")
                with _adsconex_local_lock:
                    _adsconex_local_last_post[0] = time.time()
                return ("shared", 0.0)
            else:
                # Het luot: cho cho dau cu nhat ra khoi cua so roi thu lai.
                wait_s = max(0.2, (min(inside) + ADSCONEX_RATE_WINDOW) - now + 0.05)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            try:
                os.remove(lock_path)
            except OSError:
                pass

        return ("wait", wait_s)


def _adsconex_take_shared_slot(cfg):
    """Xin luot dang, cho den khi duoc phep. Tra (mode, so_giay).

    Co GIOI HAN tong thoi gian cho: khong xin duoc luot trong ADSCONEX_RATE_SLOT_WAIT_MAX
    thi coi nhu tran dung chung dang bi ket (vd lech dong ho giua cac may lam mark nam o
    tuong lai) -> tra 'fallback' de gian cach tai may thay vi treo vo han.
    """
    deadline = time.time() + ADSCONEX_RATE_SLOT_WAIT_MAX
    while True:
        mode, extra = _adsconex_claim_slot(cfg)
        if mode == "wait":
            remain = deadline - time.time()
            if remain <= 0:
                _per_min, local_gap = _adsconex_rate_limits(cfg)
                return ("fallback", local_gap)
            time.sleep(min(max(0.2, extra), remain))
            continue
        return (mode, extra)


def _adsconex_wait_for_slot(cfg, log=None) -> str:
    """Cho toi khi duoc phep POST 1 bai. Tra mo ta ngan de ghi log."""
    per_min, _gap = _adsconex_rate_limits(cfg)
    mode, extra = _adsconex_take_shared_slot(cfg)
    if mode == "fallback":
        waited = _adsconex_local_wait(extra)
        note = ("khong xin duoc luot chung -> gian cach tai may %.0fs" % extra)
        if waited > 0.5:
            note += " (cho %.0fs)" % waited
        return note
    return "tran chung %d bai/phut" % per_min


# ---------------------------------------------------------------------------
# BAI LOI 502: luu payload lai de dang lai sau (khong ton Vision/Writer)
# ---------------------------------------------------------------------------
# 502 Cloudflare origin_bad_gateway = origin qua tai TUC THOI. Da kiem chung tren
# site that: 12/12 bai 502 deu tra 404 -> chua he tao bai, nen dang lai KHONG sinh
# bai trung. Origin sap hang gio nen retry trong vai phut la vo ich; thay vao do
# luu payload xuong work_dir roi dang lai khi origin hoi: cuoi batch, hoac bam nut
# "Dang lai bai loi" trong tab Adsconex.
ADSCONEX_PENDING_FILE = "publish_pending_adsconex.json"

# SmartTraffic: luu payload bai loi 5xx xuong work_dir de dang lai sau.
# Truoc day bai SmartTraffic loi 5xx bi mat (khong co file pending), chi con
# publish_payload_attempt_*.json nen nut "Dang lai bai loi" khong nhat duoc.
SMARTTRAFFIC_PENDING_FILE = "publish_pending_smarttraffic.json"

# Circuit breaker: origin Cloudflare sap HANG GIO thi retry 3 lan x 60/120s cho moi bai
# chi lam batch cham them hang gio ma khong cuu duoc bai nao. Bien nay la TRANG THAI CHUNG
# ca tien trinh (batch tao StoryPipeline moi cho moi video, thuoc tinh instance se bi reset).
# >=2 bai loi LIEN TIEP -> cac bai sau chi thu 1 lan roi luu payload de dang lai sau.
_adsconex_fail_streak = [0]
_adsconex_fail_streak_lock = threading.Lock()
ADSCONEX_FAIL_STREAK_LIMIT = 2


def adsconex_fail_streak() -> int:
    with _adsconex_fail_streak_lock:
        return _adsconex_fail_streak[0]


def adsconex_note_result(ok: bool) -> None:
    """Cap nhat chuoi loi lien tiep: thanh cong -> 0, loi tam thoi -> +1."""
    with _adsconex_fail_streak_lock:
        _adsconex_fail_streak[0] = 0 if ok else _adsconex_fail_streak[0] + 1


def adsconex_origin_down() -> bool:
    return adsconex_fail_streak() >= ADSCONEX_FAIL_STREAK_LIMIT


def adsconex_pending_path(work_dir):
    return Path(work_dir) / ADSCONEX_PENDING_FILE


def adsconex_write_pending(work_dir, payload: dict, status, detail: str = "",
                           extra: dict = None) -> None:
    """Ghi payload cua bai dang loi (502/503/504/429/network) de lan sau dang lai.

    'extra' luu tien do dang do (series_id + SO CHUONG da len) de lan sau dang TIEP
    phan con thieu thay vi dang lai tu dau (tranh trung chuong).

    Cac truong tien do cua ban ghi CU duoc GIU LAI khi lan ghi moi khong truyen
    'extra': _adsconex_send_all luu tien do chi tiet (series_id/posted_chunks) roi
    publish_adsconex goi lai ham nay voi thong tin ngan hon -> neu de ghi de thi
    mat tien do va lan sau se dang lai tu dau (sinh chuong trung).
    """
    if not work_dir or not isinstance(payload, dict) or not payload.get("content"):
        return
    try:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        record = {
            "http_status": status,
            "detail": str(detail)[:800],
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "machine": platform.node(),
            "payload": payload,
        }
        try:
            previous = json.loads(
                adsconex_pending_path(work_dir).read_text(encoding="utf-8") or "{}")
        except Exception:
            previous = {}
        if isinstance(previous, dict):
            for k in ("series_id", "posted_chapters", "total_chapters",
                      "posted_chunks", "total_chunks"):
                if k not in record and previous.get(k) not in (None, ""):
                    record[k] = previous[k]
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k not in record:
                    record[k] = v
        _safe_write_text(
            adsconex_pending_path(work_dir),
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def adsconex_clear_pending(work_dir) -> None:
    """Xoa dau 'con bai loi' khi da dang thanh cong."""
    if not work_dir:
        return
    try:
        adsconex_pending_path(work_dir).unlink()
    except OSError:
        pass
    except Exception:
        pass


def _adsconex_has_published(work_dir) -> bool:
    """True khi work_dir da co bai dang thanh cong (co URL hoac ID that)."""
    try:
        path = Path(work_dir) / "publish_result.json"
        if not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return bool(isinstance(data, dict) and (data.get("url") or data.get("id")))
    except Exception:
        return False



# ---------------------------
# Slink (fableon): luu payload bai loi de dang lai, khong ton Vision/Writer
# ---------------------------
SLINK_PENDING_FILE = "publish_pending_slink.json"


def slink_pending_path(work_dir):
    return Path(work_dir) / SLINK_PENDING_FILE


def slink_write_pending(work_dir, payload: dict, status, detail: str = "") -> None:
    """Ghi payload bai Slink dang loi (network/502/503/504/429) de lan sau dang lai.

    fableon tao bai NGAY khi nhan POST /api/posts/chapter, nen chi luu khi CHAC CHAN
    chua tao duoc bai (loi mang hoac 5xx/429 tu Cloudflare/origin). Loi 401/403/422
    tra ve ngay tu Laravel -> khong tao bai -> van luu de dang lai an toan.
    """
    if not work_dir or not isinstance(payload, dict) or not payload.get("chapters"):
        return
    try:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        _safe_write_text(
            slink_pending_path(work_dir),
            json.dumps({
                "http_status": status,
                "detail": str(detail)[:800],
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "machine": platform.node(),
                "payload": payload,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def slink_clear_pending(work_dir) -> None:
    """Xoa dau 'con bai loi' khi da dang thanh cong."""
    if not work_dir:
        return
    try:
        slink_pending_path(work_dir).unlink()
    except OSError:
        pass
    except Exception:
        pass


def slink_existing_slugs(cfg) -> set:
    """Slug cua MOI bai da co tren site fableon (GET /posts).

    Dung de TRANH dang trung: neu bai da ton tai (502 la loi gia) thi bo qua.
    Loi goi API -> tra set rong (khong chan dang lai).
    """
    try:
        base = str((cfg or {}).get("slink_base_url") or "https://fableon.treeiq.biz/api").strip().rstrip("/")
        token = str((cfg or {}).get("slink_api_key") or "").strip()
        if not token:
            return set()
        host = urlsplit(base).netloc or "fableon.treeiq.biz"
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Bearer {token}",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
            "Referer": f"https://{host}/admin/dashboard",
            "Origin": f"https://{host}",
        }
        r = requests.get(f"{base}/posts", headers=headers, timeout=60)
        if r.status_code != 200:
            return set()
        data = r.json()
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return set()
        return {str((p or {}).get("slug") or "").strip().lower()
                for p in rows if isinstance(p, dict)}
    except Exception:
        return set()


def find_pending_slink(output_root, cfg=None) -> list:
    """Liet ke [(work_dir, payload)] cua cac bai Slink tung loi va CHUA dang duoc."""
    items = []
    try:
        root = Path(output_root)
    except Exception:
        return items
    if not root.is_dir():
        return items
    existing = slink_existing_slugs(cfg) if cfg else set()
    for work_dir in sorted(root.glob("_story_output_*"), key=lambda p: p.name):
        if _adsconex_has_published(work_dir):
            continue
        path = slink_pending_path(work_dir)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            continue
        payload = data.get("payload") if isinstance(data, dict) else None
        if not (isinstance(payload, dict) and payload.get("chapters")):
            continue
        slug = str(payload.get("slug") or "").strip().lower()
        if slug and slug in existing:
            # Bai DA co tren site (502 la loi gia) -> KHONG dang lai de tranh trung.
            try:
                _safe_write_text(work_dir / "republish_skipped_already_live.txt",
                                 f"Bai slug '{slug}' da co tren site fableon. Bo qua dang lai.\n",
                                 encoding="utf-8")
            except Exception:
                pass
            continue
        items.append((work_dir, payload))
    return items
def adsconex_split_chunks(content: str, max_chars: int = 36000,
                          max_chapters: int = 3) -> list:
    """Chia content chapter-mode thanh nhieu phan.

    HAI gioi han phai ton trong:
      1. max_chars  - origin 502 khi request qua lon (da do: >~50KB -> 502 sau ~11s).
      2. max_chapters - origin co TIMEOUT XU LY ~12s; POST nao server can >12s de
         tao xong chuong thi Cloudflare tra 502 origin_bad_gateway. Da do that tren
         site that (token that): 1-4 chuong -> 201 (4-12s); 5 chuong -> 502;
         10 chuong -> 502 (11/12 lan); >=20 chuong -> 502 voi MOI token.
         Vi vay chia theo SO CHUONG moi la gioi han quyet dinh, khong phai so ky tu.

    Gui nhieu lan CUNG 'permalink' + 'series_id' -> server gop vao MOT series,
    chuong giu dung thu tu (da kiem chung: 3+3+3+1 -> series co du 10 chuong).
    Tra [content] (1 phan) khi content da du nho.
    """
    content = content or ""
    max_chapters = max(1, min(10, safe_int(max_chapters, 3)))
    intro, blocks = adsconex_split_blocks(content)
    if not blocks:
        return [content]
    return adsconex_pack_blocks(intro, blocks, max_chars, max_chapters) or [content]


def adsconex_split_blocks(content: str):
    """Tach content thanh (intro, [block chuong]). Moi block bat dau bang marker
    '<p>CHAPTER N - ...</p>' va keo dai toi truoc marker ke tiep.

    'intro' la phan truoc marker dau tien (server dung lam mo ta series). Cac phan
    sau phai gui lai intro nay de server khong mat mo ta series.
    """
    content = content or ""
    mk = [m.start() for m in re.finditer(r"<p>CHAPTER\s+\d+\s*-\s*[^<]*</p>", content)]
    if not mk:
        return (content, [])
    intro = content[:mk[0]]
    blocks = [content[mk[i]:(mk[i + 1] if i + 1 < len(mk) else len(content))]
              for i in range(len(mk))]
    return (intro, blocks)


def adsconex_pack_blocks(intro: str, blocks: list, max_chars: int = 36000,
                         max_chapters: int = 3) -> list:
    """Gop cac block chuong thanh tung request, ton trong CA HAI gioi han."""
    max_chapters = max(1, min(10, safe_int(max_chapters, 3)))
    chunks = []
    cur = intro
    cur_count = 0
    for b in blocks:
        too_big = bool(cur) and (len(cur) + len(b) > max_chars or cur_count >= max_chapters)
        if too_big and cur != intro:
            chunks.append(cur)
            cur = intro + b
            cur_count = 1
        elif too_big and cur == intro:
            # intro + 1 chuong van qua lon -> de nguyen (hien tuong hiem)
            chunks.append(cur + b)
            cur = intro
            cur_count = 0
        else:
            cur += b
            cur_count += 1
    if cur and cur != intro:
        chunks.append(cur)
    return chunks


def _adsconex_payload_slug(payload) -> str:
    """Slug goc cua payload (permalink) de doi chieu voi series da co tren site."""
    try:
        return str((payload or {}).get("permalink") or "").strip().lower()
    except Exception:
        return ""


def _adsconex_headers(cfg) -> dict:
    base = str((cfg or {}).get("adsconex_base_url") or "https://dramanest.gigglelo.com/api").strip().rstrip("/")
    host = urlsplit(base).netloc or "dramanest.gigglelo.com"
    token = str((cfg or {}).get("adsconex_api_key") or "").strip()
    return {
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


def adsconex_series_id_for_slug(cfg, slug, tries: int = 3) -> int:
    """ID cua series vua tao (khop theo slug/permalink).

    Can thiet khi chia content thanh nhieu phan: moi POST /posts KHONG truyen
    series_id se TAO SERIES MOI (da kiem chung: 2 phan -> 2 series, chuong bi
    tach). Phan thu 2 tro di phai kem series_id nay de server gop dung 1 series.

    UU TIEN endpoint tra CU THE theo slug: GET /api/series/<slug>/posts
    -> {"series": {...}, "posts": [...], "total": N}. Endpoint nay nhanh (~1.5s)
    va KHONG bi gioi han danh sach. Ham cu doc GET /series (tra TOAN BO series)
    roi tu loc theo slug: site da co >5000 series nen danh sach bi CAT, series
    moi nam ngoai 5000 dong dau -> luon tra 0 -> phan 2+ tao series rieng
    (da bat that: 2 series cho cung 1 truyen).
    """
    slug = str(slug or "").strip().lower()
    if not slug:
        return 0
    base = str((cfg or {}).get("adsconex_base_url") or "https://dramanest.gigglelo.com/api").strip().rstrip("/")
    headers = _adsconex_headers(cfg)
    for attempt in range(max(1, tries)):
        try:
            r = requests.get(f"{base}/series/{slug}/posts", headers=headers, timeout=60)
            if r.status_code == 200:
                body = r.json()
                if isinstance(body, dict):
                    found = safe_int((body.get("series") or {}).get("id"), 0)
                    if found:
                        return found
        except Exception:
            pass
        time.sleep(2)
    # Fallback: quet danh sach series (co the bi cat voi site lon).
    for _ in range(max(1, tries)):
        try:
            r = requests.get(f"{base}/series", headers=headers, timeout=60)
            if r.status_code == 200:
                for s in (r.json() or []):
                    if isinstance(s, dict) and str(s.get("slug") or "").strip().lower() == slug:
                        return safe_int(s.get("id"), 0)
        except Exception:
            pass
        time.sleep(2)
    return 0


def adsconex_existing_slugs(cfg) -> set:
    """Slug cua MOI series da co tren site (GET /series).

    Dung de TRANH dang trung: Cloudflare 502 nghia la response bi cat, KHONG co
    nghia origin chua tao bai. Da bat qua that: vai bai luu lai voi http_status
    502 nhung series DA ton tai tren site. Neu khong doi chieu ma dang lai mu ->
    sinh bai trung. Loi goi API -> tra set rong (khong chan dang lai).

    LUU Y: GET /series tra TOAN BO series va bi CAT voi site lon (da do: dung
    5000 dong du site co >10000 series) -> series MOI luon nam ngoai danh sach.
    Vi vay ham nay CHI la lop du phong; kiem tra chinh xac phai dung
    adsconex_series_exists() ben duoi (GET /series/<slug>/posts).
    """
    try:
        base = str((cfg or {}).get("adsconex_base_url") or "https://dramanest.gigglelo.com/api").strip().rstrip("/")
        token = str((cfg or {}).get("adsconex_api_key") or "").strip()
        if not token:
            return set()
        headers = _adsconex_headers(cfg)
        r = requests.get(f"{base}/series", headers=headers, timeout=60)
        if r.status_code != 200:
            return set()
        data = r.json()
        if not isinstance(data, list):
            return set()
        return {str((s or {}).get("slug") or "").strip().lower()
                for s in data if isinstance(s, dict)}
    except Exception:
        return set()


def adsconex_series_info(cfg, slug) -> tuple:
    """(series_id, posts) cua series <slug> tren site. (0, []) neu chua co.

    Dung GET /api/series/<slug>/posts -> {"series": {...}, "posts": [...], "total": N}.
    'posts' moi phan tu co {"id", "chapter", "title", "slug", ...} -> dung de dung lai
    danh sach chuong da len (link dang /blog/<slug>) khi dang TIEP phan con thieu.
    """
    slug = str(slug or "").strip().lower()
    if not slug:
        return (0, [])
    try:
        base = str((cfg or {}).get("adsconex_base_url") or "https://dramanest.gigglelo.com/api").strip().rstrip("/")
        if not str((cfg or {}).get("adsconex_api_key") or "").strip():
            return (0, [])
        r = requests.get(f"{base}/series/{slug}/posts", headers=_adsconex_headers(cfg), timeout=60)
        if r.status_code != 200:
            return (0, [])
        body = r.json()
        if not isinstance(body, dict):
            return (0, [])
        sid = safe_int((body.get("series") or {}).get("id"), 0)
        posts = body.get("posts")
        return (sid, posts if isinstance(posts, list) else [])
    except Exception:
        return (0, [])


def adsconex_series_progress(cfg, slug) -> tuple:
    """(series_id, so_chuong_da_len) cua series <slug> tren site. (0, 0) neu chua co.

    Can thiet de biet bai dang DO DANG (mot vai chuong da len roi moi 502) va dang
    TIEP phan con thieu thay vi dang lai tu dau (sinh chuong trung).
    """
    sid, posts = adsconex_series_info(cfg, slug)
    if not sid:
        return (0, 0)
    return (sid, len(posts))


def adsconex_series_exists(cfg, slug) -> bool:
    """True khi series <slug> DA co tren site.

    Dung endpoint CHINH XAC GET /api/series/<slug>/posts (tra {"series": ...,
    "posts": [...], "total": N}) thay vi quet GET /series: danh sach series bi
    CAT o 5000 dong nen series moi khong bao gio xuat hien -> quet se bao "chua
    co" roi dang lai -> SINH BAI TRUNG.

    Loi goi API -> False (khong chan dang lai).
    """
    slug = str(slug or "").strip().lower()
    if not slug:
        return False
    try:
        base = str((cfg or {}).get("adsconex_base_url") or "https://dramanest.gigglelo.com/api").strip().rstrip("/")
        if not str((cfg or {}).get("adsconex_api_key") or "").strip():
            return False
        r = requests.get(f"{base}/series/{slug}/posts", headers=_adsconex_headers(cfg), timeout=60)
        if r.status_code != 200:
            return False
        body = r.json()
        return bool(isinstance(body, dict) and isinstance(body.get("series"), dict)
                    and safe_int((body.get("series") or {}).get("id"), 0))
    except Exception:
        return False


def smarttraffic_pending_path(work_dir):
    return Path(work_dir) / SMARTTRAFFIC_PENDING_FILE


def smarttraffic_write_pending(work_dir, payload: dict, status, detail: str = "") -> None:
    """Ghi payload bai SmartTraffic loi 5xx de lan sau dang lai (khong ton Vision/Writer)."""
    if not work_dir or not isinstance(payload, dict) or not payload.get("content"):
        return
    try:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        record = {
            "http_status": status,
            "detail": str(detail)[:800],
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "machine": platform.node(),
            "payload": payload,
        }
        smarttraffic_pending_path(work_dir).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def smarttraffic_clear_pending(work_dir) -> None:
    try:
        smarttraffic_pending_path(work_dir).unlink(missing_ok=True)
    except Exception:
        pass


def _smarttraffic_has_published(work_dir) -> bool:
    """Bai nay da dang duoc chua (co publish_result.json voi url)?"""
    try:
        data = json.loads((Path(work_dir) / "publish_result.json").read_text(encoding="utf-8") or "{}")
        return bool(str(data.get("url") or "").strip())
    except Exception:
        return False


def find_pending_smarttraffic(output_root) -> list:
    """Liet ke [(work_dir, payload)] cua cac bai SmartTraffic tung loi va CHUA dang duoc.

    Duyet tu CU den MOI de bai cu duoc dang truoc (giu thu tu tren website).
    """
    items = []
    try:
        root = Path(output_root)
    except Exception:
        return items
    if not root.is_dir():
        return items
    for work_dir in sorted(root.glob("_story_output_*"), key=lambda x: x.name):
        if _smarttraffic_has_published(work_dir):
            continue
        path = smarttraffic_pending_path(work_dir)
        payload = None
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
            except Exception:
                data = {}
            payload = data.get("payload")
        if not (isinstance(payload, dict) and payload.get("content")):
            # Lop du phong: bai loi tu BAN CU chua co file pending (chi co
            # publish_payload_attempt_*.json). Doc lai de cuu bai thay vi bat
            # nguoi dung chay lai video (ton Vision/Writer).
            try:
                for attempt in sorted(work_dir.glob("publish_payload_attempt_*.json")):
                    try:
                        cand = json.loads(attempt.read_text(encoding="utf-8") or "{}")
                    except Exception:
                        continue
                    if isinstance(cand, dict) and cand.get("content"):
                        payload = cand
                        break
            except Exception:
                payload = None
        if isinstance(payload, dict) and payload.get("content"):
            items.append((work_dir, payload))
    return items


def find_pending_adsconex(output_root, cfg=None) -> list:
    """Liet ke [(work_dir, payload, resume)] cua cac bai tung loi va CHUA dang xong.

    'resume' = {"series_id": int, "posted_chapters": int} khi series DA co san tren site
    nhung CHUA du chuong (bai dang do dang: vai chuong dau da len roi moi 502).
    Luc do KHONG duoc bo qua (bai con thieu) cung KHONG duoc dang lai tu dau (sinh
    chuong trung) -> phai dang TIEP vao dung series cu.
    """
    items = []
    try:
        root = Path(output_root)
    except Exception:
        return items
    if not root.is_dir():
        return items
    existing = adsconex_existing_slugs(cfg) if cfg else set()
    skip_codes = {"401", "403", "429", "500", "502", "503", "504",
                  "520", "521", "522", "523", "524", "network"}
    for work_dir in sorted(root.glob("_story_output_*"), key=lambda p: p.name):
        if _adsconex_has_published(work_dir):
            continue
        path = adsconex_pending_path(work_dir)
        payload = None
        progress = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
            except Exception:
                data = None
            if isinstance(data, dict):
                payload = data.get("payload")
                progress = {"series_id": data.get("series_id"),
                            "posted_chapters": data.get("posted_chapters",
                                                       data.get("posted_chunks"))}
        else:
            # Bai loi chi luu publish_payload_adsconex.json + publish_response_adsconex.json
            # (KHONG co file pending) -> truoc day nut "Dang lai" chi nhat 401/403 nen bo
            # sot het bai 502/503/504/429/network. Doc lai payload do de cuu.
            try:
                rp = work_dir / "publish_response_adsconex.json"
                pp = work_dir / "publish_payload_adsconex.json"
                if rp.is_file() and pp.is_file():
                    resp = json.loads(rp.read_text(encoding="utf-8") or "{}")
                    code = str((resp or {}).get("http_status", ""))
                    if code in skip_codes:
                        payload = json.loads(pp.read_text(encoding="utf-8") or "{}")
            except Exception:
                payload = None
        if not (isinstance(payload, dict) and payload.get("content")):
            continue
        slug = _adsconex_payload_slug(payload)
        resume = {}
        if slug:
            if slug in existing:
                # Co trong danh sach /series -> CHAC CHAN da len. Phai kiem tra con thieu
                # chuong khong (danh sach bi cat nen day chi la lop du phong).
                sid, posted = adsconex_series_progress(cfg, slug) if cfg else (0, 0)
                if not sid:
                    # Nam trong danh sach nhung khong tra duoc chi tiet -> coi nhu da xong.
                    try:
                        _safe_write_text(work_dir / "republish_skipped_already_live.txt",
                                         f"Series slug '{slug}' da co tren site. Bo qua dang lai.\n",
                                         encoding="utf-8")
                    except Exception:
                        pass
                    continue
                resume = {"series_id": sid, "posted_chapters": posted}
            else:
                # Danh sach /series bi cat o 5000 dong -> phai hoi THANG theo slug,
                # neu khong se dang lai bai da co (sinh bai trung). Da bat that:
                # bai 502 nhung series DA ton tai tren site.
                sid, posted = adsconex_series_progress(cfg, slug) if cfg else (0, 0)
                if sid:
                    resume = {"series_id": sid, "posted_chapters": posted}
                elif adsconex_series_exists(cfg, slug) if cfg else False:
                    try:
                        _safe_write_text(work_dir / "republish_skipped_already_live.txt",
                                         f"Series slug '{slug}' da co tren site. Bo qua dang lai.\n",
                                         encoding="utf-8")
                    except Exception:
                        pass
                    continue
        # Bo sung tien do ghi trong file pending (neu co) khi API khong tra duoc.
        if not resume and isinstance(progress, dict) and progress.get("series_id"):
            resume = {"series_id": safe_int(progress.get("series_id"), 0),
                      "posted_chapters": safe_int(progress.get("posted_chapters"), 0)}
        items.append((work_dir, payload, resume))
    return items


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
        n = safe_int(ch.get("number"), idx + 1)
        raw_title = str(ch.get("title") or "").strip()
        raw_title = re.sub(rf"^chapter\s*{n}\s*[-:–—]*\s*", "", raw_title, flags=re.I).strip()
        if not raw_title:
            raw_title = "The Story Takes a Dangerous Turn"
        parts.append(f"<p>CHAPTER {n} - {html_escape(raw_title)}</p>")
        parts.append(paragraphize(chapter_body_only(ch.get("body", ""), n)))
    return "\n".join(parts)

def published_slink_link(result: dict, site_host: str) -> str:
    """Link bài Slink/fableon dạng /<slug>. Lấy từ response, không đoán."""
    if not isinstance(result, dict):
        return ""
    host = urlsplit("https://" + re.sub(r"^https?://", "", str(site_host).strip().strip("/"))).netloc
    if not host:
        return ""
    candidates = [result]
    for obj in list(candidates):
        for key in ("data", "post", "result", "posts"):
            nested = obj.get(key) if isinstance(obj, dict) else None
            if isinstance(nested, dict) and nested not in candidates:
                candidates.append(nested)
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict) and item not in candidates:
                        candidates.append(item)
    for obj in candidates:
        url = str(obj.get("url") or obj.get("link") or obj.get("permalink") or "").strip()
        if url.startswith("http"):
            return url
        if url.startswith("/"):
            return f"https://{host}{url}"
    for obj in candidates:
        slug = str(obj.get("slug") or "").strip()
        if slug:
            return f"https://{host}/{slug}"
    return ""


def slink_localized(value, lang: str):
    """fableon nhận title/content dạng chuỗi hoặc dict đa ngôn ngữ {en,vi,zh,ko}."""
    if isinstance(value, dict):
        text = str(value.get(lang) or "").strip()
        if not text:
            for key in ("en", "vi", "zh", "ko"):
                text = str(value.get(key) or "").strip()
                if text:
                    break
        return text
    return str(value or "")


# fableon CHI co 4 slot ngon ngu cho title/content/seo_*. Ngon ngu khac (Nhat,
# Han, Tay Ban Nha...) khong co slot rieng -> ghi vao 'en' nhu truoc gio.
SLINK_LOCALES = ("en", "vi", "zh", "ko")


def slink_localized_value(text, locale: str) -> dict:
    """Boc text thanh dict da ngon ngu {en,vi,zh,ko} de gui len fableon.

    fableon luu CHUOI THUONG vao key 'en' bat ke noi dung viet bang ngon ngu nao,
    nen bai tieng Viet truoc day bi gan nhan 'en' (da kiem chung tren bai that:
    title = {"en": "Chiếc Xe Đen Trên Con Đường Đất"}). Gui dict -> server ghi
    dung key locale.

    LUON kem 'en' cung noi dung lam fallback: theme hien tai render <title> va
    og:title tu key 'en', neu chi gui {vi: ...} thi cac the do se RONG.
    """
    text = str(text or "")
    out = {"en": text}
    key = str(locale or "").strip().lower()
    if key and key != "en":
        out[key] = text
    return out


def build_slink_chapters(story: dict, pagebreak_marker: str = "{{nextpage}}") -> list:
    """Dựng mảng chapters cho POST /api/posts/chapter của fableon.

    Mỗi phần tử: {"title": ..., "content": "<p>...</p>"} — content là HTML,
    phân trang trong 1 chương bằng marker {{nextpage}} (Tiptap/fableon hiểu).
    """
    marker = pagebreak_marker if pagebreak_marker in ("{{nextpage}}", "<!--nextpage-->") else "{{nextpage}}"
    chapters = []
    for idx, ch in enumerate(story.get("chapters", []) or [], 1):
        n = safe_int(ch.get("number"), idx)
        raw_title = str(ch.get("title") or "").strip()
        raw_title = re.sub(rf"^chapter\s*{n}\s*[-:–—]*\s*", "", raw_title, flags=re.I).strip()
        if not raw_title:
            raw_title = "The Story Takes a Dangerous Turn"
        raw_body = ch.get("body")
        if raw_body is None:
            raw_body = ch.get("content") or ""
        # Cat truoc khi paragraphize: paragraphize() escape HTML, nen marker
        # <!--nextpage--> se bi doi thanh &lt;!--nextpage--&gt; va fableon khong
        # con hieu la ngat trang. Tach -> paragraphize tung phan -> noi lai.
        parts = re.split(r"<!--\s*nextpage\s*-->", chapter_body_only(raw_body, n), flags=re.I)
        blocks = [paragraphize(p).strip() for p in parts]
        body = f"\n\n{marker}\n\n".join(b for b in blocks if b)
        chapters.append({"title": html_escape(raw_title), "content": body})
    return chapters

def active_publish_link(result: dict, cfg: dict) -> str:
    """Link bai theo net dang chon: Adsconex (/blog/<slug>), Slink (/<slug>) hoac SmartTraffic (/article/<id>)."""
    cfg = cfg or {}
    provider = str(cfg.get("net_provider") or "SmartTraffic").strip().lower()
    if provider.startswith("ads"):
        return published_adsconex_link(result, cfg.get("adsconex_site_host", ""))
    if provider.startswith("slink"):
        return published_slink_link(result, cfg.get("slink_site_host", ""))
    return published_article_link(result, cfg.get("site_host", ""))

def safe_video_filename(post_text: str, article_url: str, suffix: str, max_length: int = 219,
                        phrase: str = "full story") -> str:
    """Limit the entire filename, including extension, to 219 characters.
    'phrase' là câu ngẫu nhiên chèn giữa caption và link bài viết."""
    import unicodedata
    safe_url = article_url.replace(":", "").replace("/", "")
    clean_phrase = re.sub(r'[<>:"/\\|?*\x00-\x1f;]', " ", str(phrase or ""))
    clean_phrase = re.sub(r"\s+", " ", clean_phrase).strip(" .;")
    # Some WebDAV/SMB servers limit UTF-8 bytes, even when Windows reports
    # a character count below 219. Keep the filename ASCII and URL intact.
    # Cau chen ten file cung phai bo dau: khi chon ngon ngu khac (Tay Ban Nha,
    # Viet...) cau chien co dau -> de nguyen se pha vo dam bao ASCII cua ten file.
    clean_phrase = (clean_phrase.replace("\u2019", "'").replace("\u2018", "'")
                    .replace("\u2014", "-").replace("\u2013", "-"))
    clean_phrase = unicodedata.normalize("NFKD", clean_phrase).encode("ascii", "ignore").decode("ascii")
    clean_phrase = re.sub(r"\s+", " ", clean_phrase).strip(" .;")
    ending = f";{clean_phrase} {safe_url}" if clean_phrase else f";{safe_url}"
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
            # CHI them 1 LAN: configure_ffmpeg() duoc goi lai cho MOI video
            # (moi StoryPipeline), nen prepend vo dieu kien lam PATH phinh dan.
            # Windows gioi han bien moi truong 32767 ky tu -> ValueError
            # (da gap that: no o lan goi thu ~454 khi chay batch nhieu video).
            _ff = str(folder)
            _cur = os.environ.get("PATH", "")
            if _ff and _ff.casefold() not in _cur.casefold():
                try:
                    os.environ["PATH"] = _ff + os.pathsep + _cur
                except ValueError:
                    # PATH qua dai: van chay duoc vi exe_path() dung duong dan tuyet doi.
                    pass
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



# ---------------------------------------------------------------------------
#  NHAN DIEN KHUON MAT + CAM XUC
#  (Haar cascade cua ban cu da bi OpenCV 5.x xoa -> FACE_CASCADE = None
#   -> tool khong phat hien duoc khuon mat. Thay bang YuNet.)
#  Model tu tai ve ~/.video_story_publisher_models, chi tai 1 lan.
# ---------------------------------------------------------------------------
FACE_MODEL_FILE = "face_detection_yunet_2023mar.onnx"
EMOTION_MODEL_FILE = "facial_expression_recognition_mobilefacenet_2022july_int8bq.onnx"

FACE_MODEL_URLS = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
    "face_detection_yunet/" + FACE_MODEL_FILE,
    "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/"
    "face_detection_yunet/" + FACE_MODEL_FILE,
)
EMOTION_MODEL_URLS = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
    "facial_expression_recognition/" + EMOTION_MODEL_FILE,
    "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/"
    "facial_expression_recognition/" + EMOTION_MODEL_FILE,
)
FACE_MIN_BYTES = 80000
EMOTION_MIN_BYTES = 500000

# Thu tu nhan do model tra ve (opencv_zoo / facial_expression_recognition)
EMOTION_LABELS = ["angry", "disgust", "fearful", "happy", "neutral", "sad", "surprised"]
# Cam xuc cang thang / drama cang tot cho anh bia bai viet
EMOTION_DRAMA = {
    "angry": 1.00, "fearful": 1.00, "sad": 0.92, "surprised": 0.82,
    "disgust": 0.68, "happy": 0.55, "neutral": 0.12,
}
FACE_ALIGN_STD = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)

_FACE_TLS = threading.local()
_FACE_LOCK = threading.Lock()
_FACE_FAILED = [False, False]        # [detector, emotion]
_FACE_NOTICE = [False, False]


def _face_log(msg):
    try:
        print(f"[FACE] {msg}")
    except Exception:
        pass


def _model_cache_dir():
    d = Path.home() / ".video_story_publisher_models"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _download_model(urls, dest, min_bytes):
    """Tai model ve cache. True neu file da co san hoac tai xong."""
    try:
        if dest.exists() and dest.stat().st_size >= min_bytes:
            return True
    except Exception:
        pass
    import urllib.request
    for url in urls:
        tmp = dest.with_name(dest.name + ".part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as resp, open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
            if tmp.stat().st_size >= min_bytes:
                tmp.replace(dest)
                return True
        except Exception:
            pass
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return False


def _get_face_detector():
    """YuNet. Moi thread giu 1 instance (buoc quet khung chay da luong)."""
    det = getattr(_FACE_TLS, "det", None)
    if det is not None:
        return det
    if _FACE_FAILED[0]:
        return None
    with _FACE_LOCK:
        det = getattr(_FACE_TLS, "det", None)
        if det is not None:
            return det
        if _FACE_FAILED[0]:
            return None
        path = _model_cache_dir() / FACE_MODEL_FILE
        if not _download_model(FACE_MODEL_URLS, path, FACE_MIN_BYTES):
            _FACE_FAILED[0] = True
            if not _FACE_NOTICE[0]:
                _FACE_NOTICE[0] = True
                _face_log("Khong tai duoc model khuon mat -> tam dung do net + do sang.")
            return None
        try:
            det = cv2.FaceDetectorYN.create(str(path), "", (480, 480), 0.55, 0.3, 5000)
        except Exception as exc:
            _FACE_FAILED[0] = True
            if not _FACE_NOTICE[0]:
                _FACE_NOTICE[0] = True
                _face_log(f"Khong khoi tao duoc YuNet ({exc}) -> tam dung do net.")
            return None
        _FACE_TLS.det = det
        return det


def _get_emotion_net():
    net = getattr(_FACE_TLS, "emo", None)
    if net is not None:
        return net
    if _FACE_FAILED[1]:
        return None
    with _FACE_LOCK:
        net = getattr(_FACE_TLS, "emo", None)
        if net is not None:
            return net
        if _FACE_FAILED[1]:
            return None
        path = _model_cache_dir() / EMOTION_MODEL_FILE
        if not _download_model(EMOTION_MODEL_URLS, path, EMOTION_MIN_BYTES):
            _FACE_FAILED[1] = True
            if not _FACE_NOTICE[1]:
                _FACE_NOTICE[1] = True
                _face_log("Khong tai duoc model cam xuc -> bo qua phan cam xuc/drama.")
            return None
        try:
            net = cv2.dnn.readNet(str(path))
        except Exception:
            _FACE_FAILED[1] = True
            return None
        _FACE_TLS.emo = net
        return net


def _align_face(frame, landmarks):
    """Can chinh mat theo 5 diem moc ve khung chuan 112x112."""
    try:
        src = np.asarray(landmarks, dtype=np.float64).reshape(5, 2)
        matrix, _ = cv2.estimateAffinePartial2D(src, FACE_ALIGN_STD, method=cv2.LMEDS)
        if matrix is not None:
            return cv2.warpAffine(frame, matrix, (112, 112))
    except Exception:
        pass
    return None


def _emotion_of(frame, face_row):
    """-> (nhan cam xuc, do tin cay, diem drama 0..1)."""
    net = _get_emotion_net()
    if net is None:
        return "", 0.0, 0.0
    try:
        face = _align_face(frame, face_row[4:14])
        if face is None:
            x, y, fw, fh = [int(v) for v in face_row[:4]]
            x, y = max(0, x), max(0, y)
            fw = max(1, min(fw, frame.shape[1] - x))
            fh = max(1, min(fh, frame.shape[0] - y))
            face = cv2.resize(frame[y:y + fh, x:x + fw], (112, 112))
        face = cv2.cvtColor(face, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        face = (face - 0.5) / 0.5
        net.setInput(cv2.dnn.blobFromImage(face), "data")
        logits = np.asarray(net.forward()).ravel()
        if logits.size != len(EMOTION_LABELS):
            return "", 0.0, 0.0
        exp = np.exp(logits - logits.max())
        prob = exp / exp.sum()
        idx = int(np.argmax(prob))
        label = EMOTION_LABELS[idx]
        return label, float(prob[idx]), float(EMOTION_DRAMA.get(label, 0.0))
    except Exception:
        return "", 0.0, 0.0


def analyze_frame_faces(frame, max_width: int = 480, with_emotion: bool = True) -> dict:
    """Phat hien khuon mat (+cam xuc) tren 1 khung BGR. Toa do theo khung GOC."""
    result = {"faces": 0, "face_area_ratio": 0.0, "center_bonus": 0.0,
              "emotion": "", "emotion_conf": 0.0, "drama": 0.0, "boxes": []}
    if frame is None or getattr(frame, "size", 0) == 0:
        return result
    det = _get_face_detector()
    if det is None:
        return result
    h, w = frame.shape[:2]
    if h <= 0 or w <= 0:
        return result
    scale = min(1.0, float(max_width) / float(w))
    small = frame
    if scale < 1.0:
        small = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
    try:
        det.setInputSize((small.shape[1], small.shape[0]))
        _, rows = det.detect(small)
    except Exception:
        return result
    if rows is None or len(rows) == 0:
        return result

    inv = 1.0 / scale
    boxes = []
    for row in rows:
        boxes.append({
            "x": float(row[0]) * inv, "y": float(row[1]) * inv,
            "w": float(row[2]) * inv, "h": float(row[3]) * inv,
            "conf": float(row[-1]),
            "landmarks": [(float(row[4 + i * 2]) * inv, float(row[5 + i * 2]) * inv)
                          for i in range(5)],
        })
    boxes.sort(key=lambda b: b["w"] * b["h"], reverse=True)
    main = boxes[0]
    result["faces"] = len(boxes)
    result["boxes"] = boxes
    result["face_area_ratio"] = min(1.0, (main["w"] * main["h"]) / float(w * h))
    cx = main["x"] + main["w"] / 2.0
    cy = main["y"] + main["h"] / 2.0
    dx = abs(cx - w / 2.0) / (w / 2.0)
    dy = abs(cy - h / 2.0) / (h / 2.0)
    result["center_bonus"] = max(0.0, 1.0 - (dx * 0.65 + dy * 0.35))

    if with_emotion:
        # Cham cam xuc tren mat TO NHAT (nhan vat chinh cua khung hinh)
        row = max(rows, key=lambda r: float(r[2]) * float(r[3]))
        label, conf, drama = _emotion_of(frame, row)
        result["emotion"], result["emotion_conf"], result["drama"] = label, conf, drama
        if len(boxes) > 1:
            # Nhieu nguoi trong khung = them kich tinh (doi dau, tranh cai...)
            result["drama"] = min(1.0, result["drama"] + 0.10)
    return result


# --- Trong so cham diem anh bia (thu tu uu tien: NGUOI > NET > CAM XUC) ------
# Diem khung CO MAT luon >= FACE_GATE; khung KHONG MAT toi da ~= NO_FACE_MAX.
# NO_FACE_MAX = SHARP_WEIGHT + EXPOSURE_WEIGHT = 3300 + 130 = 3430 < FACE_GATE.
FACE_GATE = 3600.0
SHARP_WEIGHT = 3300.0
EXPOSURE_WEIGHT = 130.0
DRAMA_WEIGHT = 320.0


def _sharpness_term(sharpness: float) -> float:
    """Do net -> diem. Thang log cho gian ro dai do net cua video thuc (5..500)."""
    import math
    s = max(0.0, float(sharpness))
    term = (math.log1p(min(s, 1500.0)) / math.log1p(1500.0)) * SHARP_WEIGHT
    if s < 25.0:
        term *= 0.40          # gan nhu nhoe / chuyen canh
    elif s < 60.0:
        term *= 0.80
    return term

def image_quality_metrics_from_frame(frame) -> dict:
    """Diem 1 khung hinh theo thu tu uu tien: CO NGUOI > DO NET > CAM XUC/DRAMA."""
    blank = {"score": 0.0, "sharpness": 0.0, "brightness": 0.0, "faces": 0,
             "face_area_ratio": 0.0, "emotion": "", "emotion_conf": 0.0,
             "drama": 0.0, "boxes": []}
    if frame is None:
        return dict(blank)

    h, w = frame.shape[:2]
    if h <= 0 or w <= 0:
        return dict(blank)

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    exposure_score = max(0.0, 1.0 - abs(brightness - 128.0) / 128.0)
    if brightness < 35 or brightness > 225:
        exposure_score *= 0.25

    info = analyze_frame_faces(frame)
    faces = int(info["faces"])
    face_area_ratio = float(info["face_area_ratio"])
    center_bonus = float(info["center_bonus"])
    drama = float(info["drama"])

    score = _sharpness_term(sharpness) + exposure_score * EXPOSURE_WEIGHT

    if faces:
        # Uu tien so 1: anh PHAI co nguoi / nhan vat.
        # Cong theo kieu "cong don" (khong bi phep phat do net nhan vao),
        # nho vay khung co mat luon thang khung vang nguoi.
        score += FACE_GATE
        # Mat cang to cang de nhan ra nhan vat.
        score += min(500.0, face_area_ratio * 2600.0)
        # Nhan vat o giua khung.
        score += center_bonus * 160.0
        # Cam xuc cang / drama cang tot.
        score += drama * DRAMA_WEIGHT
        score += min(1.0, float(info["emotion_conf"])) * 40.0

    return {
        "score": float(score),
        "sharpness": sharpness,
        "brightness": brightness,
        "faces": faces,
        "face_area_ratio": face_area_ratio,
        "emotion": info["emotion"],
        "emotion_conf": float(info["emotion_conf"]),
        "drama": drama,
        "boxes": info["boxes"],
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
            "emotion": (metrics.get("emotion", "") if metrics else ""),
            "drama": float(metrics.get("drama", 0.0) if metrics else 0.0),
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


def _youtube_badge_font(size: int):
    """Chon font dam de ve chu YouTube; may nao thieu font thi dung mac dinh."""
    from PIL import ImageFont
    size = max(8, int(size))
    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in ("arialbd.ttf", "ariblk.ttf", "segoeuib.ttf", "arial.ttf", "segoeui.ttf"):
        for cand in (os.path.join(fonts_dir, name), name):
            try:
                return ImageFont.truetype(cand, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Nut play TRANG (kieu "white") - asset PNG trong suot THAT, nhung san duoi
# dang base64 de tool van la 1 file .py duy nhat (khong can thu muc assets).
# PNG 221x221: dia xam dac #B0B0B0 + tam giac trang NHON, vien cung,
# khong shadow.  D = 100% khung; tam giac th = 0.450D, tw = 0.400D,
# lech phai 0.048D.
# ---------------------------------------------------------------------------
_WHITE_PLAY_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAN0AAADdCAQAAACVzb+RAAAKMWlDQ1BJQ0MgUHJvZmlsZQAAeJydlndUU9kWh8+9N71QkhCK"
    "lNBraFICSA29SJEuKjEJEErAkAAiNkRUcERRkaYIMijggKNDkbEiioUBUbHrBBlE1HFwFBuWSWStGd+8ee/Nm98f935rn73P"
    "3Wfvfda6AJD8gwXCTFgJgAyhWBTh58WIjYtnYAcBDPAAA2wA4HCzs0IW+EYCmQJ82IxsmRP4F726DiD5+yrTP4zBAP+flLlZ"
    "IjEAUJiM5/L42VwZF8k4PVecJbdPyZi2NE3OMErOIlmCMlaTc/IsW3z2mWUPOfMyhDwZy3PO4mXw5Nwn4405Er6MkWAZF+cI"
    "+LkyviZjg3RJhkDGb+SxGXxONgAoktwu5nNTZGwtY5IoMoIt43kA4EjJX/DSL1jMzxPLD8XOzFouEiSniBkmXFOGjZMTi+HP"
    "z03ni8XMMA43jSPiMdiZGVkc4XIAZs/8WRR5bRmyIjvYODk4MG0tbb4o1H9d/JuS93aWXoR/7hlEH/jD9ld+mQ0AsKZltdn6"
    "h21pFQBd6wFQu/2HzWAvAIqyvnUOfXEeunxeUsTiLGcrq9zcXEsBn2spL+jv+p8Of0NffM9Svt3v5WF485M4knQxQ143bmZ6"
    "pkTEyM7icPkM5p+H+B8H/nUeFhH8JL6IL5RFRMumTCBMlrVbyBOIBZlChkD4n5r4D8P+pNm5lona+BHQllgCpSEaQH4eACgq"
    "ESAJe2Qr0O99C8ZHA/nNi9GZmJ37z4L+fVe4TP7IFiR/jmNHRDK4ElHO7Jr8WgI0IABFQAPqQBvoAxPABLbAEbgAD+ADAkEo"
    "iARxYDHgghSQAUQgFxSAtaAYlIKtYCeoBnWgETSDNnAYdIFj4DQ4By6By2AE3AFSMA6egCnwCsxAEISFyBAVUod0IEPIHLKF"
    "WJAb5AMFQxFQHJQIJUNCSAIVQOugUqgcqobqoWboW+godBq6AA1Dt6BRaBL6FXoHIzAJpsFasBFsBbNgTzgIjoQXwcnwMjgf"
    "LoK3wJVwA3wQ7oRPw5fgEVgKP4GnEYAQETqiizARFsJGQpF4JAkRIauQEqQCaUDakB6kH7mKSJGnyFsUBkVFMVBMlAvKHxWF"
    "4qKWoVahNqOqUQdQnag+1FXUKGoK9RFNRmuizdHO6AB0LDoZnYsuRlegm9Ad6LPoEfQ4+hUGg6FjjDGOGH9MHCYVswKzGbMb"
    "0445hRnGjGGmsVisOtYc64oNxXKwYmwxtgp7EHsSewU7jn2DI+J0cLY4X1w8TogrxFXgWnAncFdwE7gZvBLeEO+MD8Xz8Mvx"
    "ZfhGfA9+CD+OnyEoE4wJroRIQiphLaGS0EY4S7hLeEEkEvWITsRwooC4hlhJPEQ8TxwlviVRSGYkNimBJCFtIe0nnSLdIr0g"
    "k8lGZA9yPFlM3kJuJp8h3ye/UaAqWCoEKPAUVivUKHQqXFF4pohXNFT0VFysmK9YoXhEcUjxqRJeyUiJrcRRWqVUo3RU6YbS"
    "tDJV2UY5VDlDebNyi/IF5UcULMWI4kPhUYoo+yhnKGNUhKpPZVO51HXURupZ6jgNQzOmBdBSaaW0b2iDtCkVioqdSrRKnkqN"
    "ynEVKR2hG9ED6On0Mvph+nX6O1UtVU9Vvuom1TbVK6qv1eaoeajx1UrU2tVG1N6pM9R91NPUt6l3qd/TQGmYaYRr5Grs0Tir"
    "8XQObY7LHO6ckjmH59zWhDXNNCM0V2ju0xzQnNbS1vLTytKq0jqj9VSbru2hnaq9Q/uE9qQOVcdNR6CzQ+ekzmOGCsOTkc6o"
    "ZPQxpnQ1df11Jbr1uoO6M3rGelF6hXrtevf0Cfos/ST9Hfq9+lMGOgYhBgUGrQa3DfGGLMMUw12G/YavjYyNYow2GHUZPTJW"
    "Mw4wzjduNb5rQjZxN1lm0mByzRRjyjJNM91tetkMNrM3SzGrMRsyh80dzAXmu82HLdAWThZCiwaLG0wS05OZw2xljlrSLYMt"
    "Cy27LJ9ZGVjFW22z6rf6aG1vnW7daH3HhmITaFNo02Pzq62ZLde2xvbaXPJc37mr53bPfW5nbse322N3055qH2K/wb7X/oOD"
    "o4PIoc1h0tHAMdGx1vEGi8YKY21mnXdCO3k5rXY65vTW2cFZ7HzY+RcXpkuaS4vLo3nG8/jzGueNueq5clzrXaVuDLdEt71u"
    "Unddd457g/sDD30PnkeTx4SnqWeq50HPZ17WXiKvDq/XbGf2SvYpb8Tbz7vEe9CH4hPlU+1z31fPN9m31XfKz95vhd8pf7R/"
    "kP82/xsBWgHcgOaAqUDHwJWBfUGkoAVB1UEPgs2CRcE9IXBIYMj2kLvzDecL53eFgtCA0O2h98KMw5aFfR+OCQ8Lrwl/GGET"
    "URDRv4C6YMmClgWvIr0iyyLvRJlESaJ6oxWjE6Kbo1/HeMeUx0hjrWJXxl6K04gTxHXHY+Oj45vipxf6LNy5cDzBPqE44foi"
    "40V5iy4s1licvvj4EsUlnCVHEtGJMYktie85oZwGzvTSgKW1S6e4bO4u7hOeB28Hb5Lvyi/nTyS5JpUnPUp2Td6ePJninlKR"
    "8lTAFlQLnqf6p9alvk4LTduf9ik9Jr09A5eRmHFUSBGmCfsytTPzMoezzLOKs6TLnJftXDYlChI1ZUPZi7K7xTTZz9SAxESy"
    "XjKa45ZTk/MmNzr3SJ5ynjBvYLnZ8k3LJ/J9879egVrBXdFboFuwtmB0pefK+lXQqqWrelfrry5aPb7Gb82BtYS1aWt/KLQu"
    "LC98uS5mXU+RVtGaorH1futbixWKRcU3NrhsqNuI2ijYOLhp7qaqTR9LeCUXS61LK0rfb+ZuvviVzVeVX33akrRlsMyhbM9W"
    "zFbh1uvb3LcdKFcuzy8f2x6yvXMHY0fJjpc7l+y8UGFXUbeLsEuyS1oZXNldZVC1tep9dUr1SI1XTXutZu2m2te7ebuv7PHY"
    "01anVVda926vYO/Ner/6zgajhop9mH05+x42Rjf2f836urlJo6m06cN+4X7pgYgDfc2Ozc0tmi1lrXCrpHXyYMLBy994f9Pd"
    "xmyrb6e3lx4ChySHHn+b+O31w0GHe4+wjrR9Z/hdbQe1o6QT6lzeOdWV0iXtjusePhp4tLfHpafje8vv9x/TPVZzXOV42QnC"
    "iaITn07mn5w+lXXq6enk02O9S3rvnIk9c60vvG/wbNDZ8+d8z53p9+w/ed71/LELzheOXmRd7LrkcKlzwH6g4wf7HzoGHQY7"
    "hxyHui87Xe4Znjd84or7ldNXva+euxZw7dLI/JHh61HXb95IuCG9ybv56Fb6ree3c27P3FlzF3235J7SvYr7mvcbfjT9sV3q"
    "ID0+6j068GDBgztj3LEnP2X/9H686CH5YcWEzkTzI9tHxyZ9Jy8/Xvh4/EnWk5mnxT8r/1z7zOTZd794/DIwFTs1/lz0/NOv"
    "m1+ov9j/0u5l73TY9P1XGa9mXpe8UX9z4C3rbf+7mHcTM7nvse8rP5h+6PkY9PHup4xPn34D94Tz+6TMXDkAACHiSURBVHja"
    "7Z35d1NHmvc/VXfR5hXFOIaEBLIAcXvc9NDLzDkz5z3n/cPf98x0Z9IZdzxuN6GzNBAwGGOMsGXJ0l2q5odburrypl26sl0+"
    "JAcsW7r1reepZ/0+QjP569F0LRvYSoaWktGXlo3nEub/VmAHtm+pTM0OskefB5P/1GIyoXsq92cr00fZetb1AjuUegNAoxNg"
    "RUsBAo0ABAIN6IcgQ8fLeJmjfDV7dK92Bd2Q1z/s/dnyrOf6dmCr9ejfQgQCCWhCQiwDUQQlCTAj2CzzuzQaiQDC31l+rpav"
    "5ioz+5MkjRMC3XqxNF/LhrZaj7a8AYlAG1lrAKZa4MK8tgEiKKSBrPH9EIGNxoffuXXXmyrPHCyXr6Drc/2Q3b1+lPfcwFFr"
    "0mw8aFQMnRWDJxAI8x0d33MiBk7EUIKKf0YDEhsI0cjoa8VSmdp0eX7vjrqCruv1uFCeOpw+ygcbyXsrTEiTQCLwYnWpUYAd"
    "A0cshU0YBU0sop/X5ie1+Z0BFpb5ffZqoZqrzJfu1q6g62j96JaK+7OeqzYiSRAGiqbUNW4uhTay1oQjMCBxzMJslUViNaux"
    "DIAC0EhzBAQSC4nAWna9XGWulDZjJmXQfVd8V6xl1UZ0iwUE2OPYFAOjBmwCFDbuSq46V/p16Qq6U2TtzYf7c7Wc842KpUKh"
    "xw6dRCOwAB/xMHs0fbj4Kh12aCqgW1vYW/DcYF2auyhpeIxlUxJK1iEw96NCYiMQq4Xq0tb41efYoft2sVSsFvhWGivQM8Z7"
    "w8DXY4bOByxsBCHKWKMC/cD1PtxeLV1S6J7KN4vvivWMWJMowhgkaQwS3bKN44JOJT5R5FK4CAIk2WXXK1R+v33poPv65rui"
    "v6EIsY2ajE52c9MYk7psXQ4hoYm9WEgUKrZABdbq9OFcaTzSNxbo/rz0ZjFwxLcgYk9NmHNttTjYST9sPCtAxp5eiEKAgdBH"
    "YCOxV/LVa3sr+xceur8s7C4cFeS3kgCw8VFILDSBCUhp461FSlOOXFnqE6qz4elhLE4FWAgUioAMDqzmqh9uf1W5sND9bfpd"
    "sTzjbzRCU/qY8T8us6S/FbkwGdy72dr83ujgGxl0T+XLj97PeRtWDM9pKRoxcdBpYw8rwCF/d670z7sXCrrvim8Xqo8jdcOJ"
    "0FR/W3e6dTg6mYvMLB+wcWF16vDDV6OIeo4Aup/tJ59XC/aaiu1HEvm0SYfuuKq3EFgr86V/eTnx0P3XzffzwWaAxkIRmFTo"
    "IBXWuKHTxlWILOQ6WRz0Azu4/fR+ZYKh+/93yjN6XaIIjGsbnnB6J32JxL0dOQ02GQL0wxtbv9uZSOh+yL64VZ51vq2Z+yAE"
    "HOOnicR51RcKugxH+LjYBPBwZv/TJ8NK1w4Nuv9eLM17jz1CXCwCFBJNiDwmc7JP2RNtFOjwgVMtCjMgi6AGZBAoMisfvRiO"
    "uz4k6L6+WSpWNjQZHAKCuIRHD/wuGo3q1Wfeoid90ahYInpWhxBr9YPdYUQ6hwDdT+6LW4dTej00DrcyIdzhONxNd0OPSV3q"
    "Y3+P4pzSZOA9XOyVQvXWs9sq5dB9V9xZqmzKuL5Dx3UhvW2wGLPU6R6OUojGMYWG2jgM2fsfvxisxTlg6L5Z2l301jUCaaJ9"
    "Oi776S3QJWHM0RLd9VELwEAHFnUsMoC9/NGLX5VTCt0fb71Z1N9Kk+VqyF2zPjJSJoOFTozgltNdfuIwUQQV/QmRuNgrH+z+"
    "did10D2Rzz/dnz9ac01MXcd1VknjWUyg1HWnNrVJW0VPb5nCqACNjcBend/715epgu4n94d7/oZDFacFrCZ0vVqXkwWdiDOO"
    "wqRnFRoL2+T3XMRqtvZ/f0wNdH+d3b5Zf+QhyVAx1mRrqrR3+MYNXTcqUxglqeN6TmlUerM43sZ+MH3wf56kArq/Z3+85627"
    "xiTWXUfez6/7kmPPxg12hbjI1anD/sHrG7rN2Vc3q4+kqZiSPZzp86ETqVCYg3MeAixsnJXC4b8/Gyt0jws/faHXbUK8uGaj"
    "t4LVSV2q66f1sXBQD2dL/Ume7K/f7cXHat1CG3O4/yKgSQax0w230PiEawczX98ck9Q9Lrz4+Oixb9xvYeoU5ZXUdZBVDwmR"
    "OA+Ku727CrL3WpNfPqk8TvaNNlqkRhO1mFypi1rNLCBcP5j9062RQ/fk8/ojG3BMbUbY1uC4yLLX+YpqBRQWNprqo7cL3xVH"
    "Ct0fb+0uSOOxKBM7sQHv6q5rszwsXBNvUhxhrZWKf50d2V33n59ufVz4D02dTA+6XrQExOSEQ6H6zF1oIL/8yS/dZxVkL/XL"
    "pbnCf4SEuPg9KcbkFxegvEHE/xU9qD3J0aPnn/zDHjp0G/Nbt8L1RpzOuoIuAZvoKboCgtqj558OGbqf3O0luaY5wsHC7wG6"
    "i7dEIpUserStBZV8t15el9A9u7P/KIxPmU4UxQ4390yqS9c1zS6KbldknwcEG6XixvzQoPvTrVrWwsPCoYJHpoePqhO1JBfL"
    "k9M9hqMxXQv+xs7i37NDge6vs7sLYj1rymb0hbAPh1Gb1i10CsuUgNQev/h0KND9cps1Hx8bD48pMng9fNzmZX7xnPBensc1"
    "AY0AgaBS+OOtgUP3n582CmA1Lg4+IVaHXp0wcU55jJ/rIkqeNDxK0iScO0l6RVVjGoW9VrrWaXRFdtpCvFcU6zYBV2sYFWeN"
    "Qv4Qb/310gCh+9neWQzW9AWzDNPm1msUCoda7v99PjjovgzWHfwx0T5dLggtxLflqW8XBwLdXxYOZp0EeeHVGk7hUmQReCj8"
    "9VLxiewbuqfy7YL9tTY1FeHVPg/ZLvVRaPxH//iyb+ie3ak9slEEhuTlag1e5pqUqw0eMkWl8LfpvqD76+z7+cAoysh5vFrD"
    "iMEIk0BS2NhINM7aq5t9Qbd90/rWiilDdUeGro79G9mFf9N6BhvBIUHOVBQ7cW5Mm1SJbvEq05SJEKf6szJRFX5WT6w0dmaI"
    "T2D/182eofuf+VpOdcG50Hv0vLX9Q8T9CpoyIZKQuvEpbdMGFdV3WBMX0G49XuLU0JiMetI3D6fPy+KdC93rJdaSjF2jKFBo"
    "0CmqBL29hcAig4vAS7A565YQwUWxgJWBLkQ9fnGrJ+j+vHQ4fTzeOIqt0S0HQcagqJiR0ooJtxUXLdsg424FjcXh1PeFrqF7"
    "Kt8u6G/UsY3sjQam21qPJol21GjYOIcRC7OVuPGcic3u6TO0hCKyLVw0Hmxs3zw703fG2roVbjhdEsqIgTyaZSwtTYiFjSTP"
    "HHkUFcrUqBsoiSGepACdaHM7N7qDJFAlQy37aPr0gRlnQncwo83kjU47SAdl3dl4KCQBITnmuc48WUMocMQ+u7zFM99vPMDk"
    "UOiIY5b0ye+HcWdCHRux8e423UD3x1uBE7acENEGPDEwGK3YynRY5BNmEh/SZZoFXrPLASpu8NITxOzXjufFxsNC45HDB+pU"
    "8uvFB3sd33XVfH0txE6YAWdp6NaKqKR3pXuCMLIhI/iu8xnzx06XJMcn3OMjisySpWra7DU1gtgyFam0N0Viu8UZJR6h6cF3"
    "CIguLPXoYLZjM2VtoVqwDNfj6cV4w+sVsIwBEpLlYwqngi+Z4Qvus0iGa7jUOARmyVFL3IHiApBYgSagPPVDtkPoXi/pNSse"
    "ZNR5RdSgiJsEFopp5s4JvFnM8Tm/4jo5suSRVKjhxsTDF6V8wkITbrz6qKO77tF0LWebRsdw5GV5Udu8RDNHpg3IGT6gQJFt"
    "ysY9lwl+FnFBOoM0FtV8R1K3syi+0fGEqtE3eET5YouZDoLdgjxL3OM20+TIxMNcGpS+8kIozBCh/rzUFrrvC5UpxyjLsI0z"
    "MIwSdBEz+Lkd/k6LaT7mV3xIxNki2lKyMVHZPI2Czd2FttDtz/nrAoGNwD/l0fUp0XEx4OyV6lqibea4zz/xEdPG0CEeLzP5"
    "KlMQEjjrxTbQHeU86mgcBOrUQJM+Ve4GdcLDOKmjutp4QYYF7vElSxQSKZZJXyESCx/WSvPnQrdefD/fGKmpkKc0YR2XMT1g"
    "g8VKsK90HzXNsMRdbnONAhlTpy3im49Txk+0DiIUqbQwI11UzbemgI5Btz+rvrVMIbUa86OIHtVLgU9Z4QY2kMPFo4ZFFmF4"
    "ulqPoU55JbYyzd6acGOveA50R/lmSmW8D9TPe1tM8xkrfIINuGRR1AhMZ5I4wf4lUqxck62X5dkzoVtb8G2ZGMQ+ec0XzZ92"
    "WeAL7vEhU2ZSkGXSKToBXuuEk3Smahtca5K6m+wEaoHucFqvNyZGDYbCpr+z1u/vyHKDL/mEAhlyWOYmTUYQdYu8idTyskQl"
    "HcHmu+Kp0ZR/2LVss3CnQUAqJlLqmidziiyz7LDDoQntcgIunWr2CEmTl76WO1Xq3s97jy6KI5s8Ag5FPuMrPmHWDI0XLZPJ"
    "ky0bOpXqUht6KkFg/2yfInWVQmiyBdLcCXpCzZTTVOd1ZnnHFhVq5ukakZtm8CyN0EUVsBE5lSZ4fHiN0gnoSsU8R4kzeLHk"
    "T5JjiTles0UFm5C6sTctQ7E9WtNMd3FxNMZ6hAQc5U9A9/es51opOnViKPAV+JQP2eIFAQUER/hxhi8if0prB2FDkA6nT9x1"
    "B7Pi6yBVUiaGFJ0ocIdVPjJMS9G849AQZfkpfTod11LX3ceFY9AdTltjtCcZ8dTjD7jHZ8zh4sRM1aP36zrjOBKtsZ+Nw6kW"
    "hflE1rMyVS1YYsi/PcsN5njDG/bxE0HvcEzdCfpcguVk6KDhIBjoqoVgU5lRJhcfuIbqnCLLHG94xUHMDitHPA1ItJABtC8l"
    "UXhui8I8ykcTMPSlkbuGz3eNO3zFLQqGqTJtMSN9ovwjlFEGQTakLoryiUvYU5phkS+5zRQO+VQG/VpjrOpxZGUahVnLRkGi"
    "9DQ/ipH6fNPkmGeHXTw8QlNThmncEHHrosY6NrRCDERp6jZD3kRiTKM2KtMG+Mmtu415a5d12cwzRZEt9gw0ymQgtGFrbpRZ"
    "jTfiqVDGULEbGQOL0PA3c2nb8V0WmOYtv1AyEx+DRJ3LaREQMYYBo5qgedcdZVUKg15iLO+Z4yar3GcOgcZpGeOmU0EOqfDt"
    "p9JInZeJhnOmqdVQjDHaOU2eD3jOG0IzL6VRq2OZvzHGIt0Q/7GfwZMASopTym4u87KY5R7LXCNnspaWMeNaaWr0WOoyNV7G"
    "SF1oNy2oK2aU5s23yAzv2GHPVKQm90eMccSFwHMbCtNNTgm/WslMQ4YZZtihbPJ74ZjNlEhh17Jgw+NC4GSoEWC3DYSJBP2v"
    "uBQyajNDnmu8ZpcjA1xrE7RuKZQQfeTudMvvFmd2QkmUBTZ4brhmd+XVD6rwZ5JKJKaZZ5v3vDcUIJBB4LWQb/Urg53GMcOG"
    "wqxluyllEJdoiMvxuupZdnlBGUXWzF61EwxKo+npi8AN7EjqMsTBlvMgPI2LSKRmlPsobr6oumyLbeqJeVjEFD397UdnF5Dx"
    "MeUTaUcIdjoHeHDN/pM5CyEKl13nKSUcNMFYuokk6lGYsSGUTUau7uLb+lJmGlwWmWWXXyjFo6zpu3dddJyzi7hVQmlDaPce"
    "UtWX1OfLcYMCr3hHBQ81MK7Q9uA1CLYCx06mL3QX50ZfQnXZqjqLzPCOl+xxmCCjG4VLDorAtkFJy2SHg3M4t3Sbe04nmp7E"
    "JZFHh+tM844XvMYCAnLUB3JgxbmtkgpBaNtPpJaDK5bVl0515slQIMdLBILqiDKeAiXtwNYbOu7g0gNgMb4sKpMEg4tFicO+"
    "A4m6i1cqKUOn6UxeRTB7lYEpPiaD7EPmdNfmjFSyWevXn9TpSx2oXmQRq8+brpt91lI21eQgap8vL3w5pkcyTEAk++tES/Jw"
    "8OScl2PAfNSBnxkJdLpBvTcIf+RqZEx/R7cXsZFChdhgalNkz+1DooXi6TICGQ6k975d24qIR4QeU5hiAsuA0kTDNgrag8br"
    "pAwbeVl1pfT6WHUO8EdWUacRypaKAfGWi0sWxUyugNe8G+nwJoG0lFi5KuKjz1KftzwhwBnRtWEc8s8CofSFnFc8quXxgp+o"
    "9Bl0F12+UiqbFPCBTa66VFTZ4ik+Nn7fdCWdplpj6KTSCYL5q9X54arzhi3eEZJFcIA7kryBqRFSJl/XrK2/UpudLZ8SW2yb"
    "ITWHQAFB7xwYOvFHtJH0KHJjgxWIq5uuS+e7wjY7HBj6AD3C5LJARcRTgQ12IMZeST9J1mSdN/zC+3i+lzZRKD2i/veohdX2"
    "bXCCRqtrmvJ1abx1NR7veMEuPrahJ8YUFUVBDTEiz07edT0b3Ho6B4np1N1u+2zxlkNDZx4mxkGNshoncsnvKBsyNRHPwbmy"
    "MM+63cps84YadSQ2EBCYKXq0nTI2cJnDCsCGTF0kqHSvwDt5ux2xwyvKRj02KM3tRCn/6KgBIkmXCmxwPLkabuieU60XWV1q"
    "fN7xkhIBniEFUoaEw0YTGAuhMYRm+LddxFdqRdDdUY9x8DrgzdGXrEkr5D1bvKNi0jlWi3L0j9FF9W5hiq7ERiCRoelqtYPg"
    "qhrsFDX5ircmldOk+ZapIM/P1mLoPEOme7UaTsAuL00PuU5VOlkAYjmGLl+ppGzGoh7jO4fs85w3eFi41Fr6CkUqJmtZwf2K"
    "gW76YBeQpIuFVo/ldjvkFS+po/AQ5EzdTqfxxdEsJ6BR8FyoWKtp4/rXY7jdKrzke35mnxALF0GtZZxpWiK9Tp0GR9ht9bNX"
    "h0t+u73jObtIwCJA4GAZ6JIGgk5BpXXGS5AqZmvlS2ykBLxnm7fUTOJLYhNyiMA1XasqZicSaYCuloDOqeepX0obM7rddqni"
    "EWIbDy4EHENhONyCRR17dadfWRZ+oxoFgcLGWXb8BAvt9KFa0ZdQTVZ5xvc8p2JGVKix0didNfFWGTrx5iCbXPVuUuqWyy+U"
    "TBEv36hutx0OqFInwDJBZTEmg/+8mKVMlPEpske0ThfJeL6hy70MNmbAPq8M85eDg8CPJ9yR6lnJUSSlBbrp/ctiqIRU2WKH"
    "QzQOHgEWAiemCR71AW1HeqITdDohudVC5dh0kemyeKAvvMxpajxnnV84IiREcg2bGh4CO5VmWpPuMlqFw8+CY1J3t/ai6l/w"
    "fJ3HHk8omxk+DgGHVJA4SEIUEmfkE1Z0m6Oq44Gf0RS7hsy1DEGbKlcuMHAB+2yxh089oiXkCEnexEqUGcc0DjNFn0ukmCSN"
    "E2RX8tVTpkZOH2aW1ZlTx8/L4g2n9FYPMMR1wFP+bqCTZuCgZQqDdKKrTYwUMhXTNp9dfy7NgRI4+GRry+VTpG65/Ep1b2GJ"
    "CfDddnjNARUgF8/DnBwnRhne6TpZHI/TxuxCplaLdf1ZSQ59jBNTp9x32+MNu9SwyKHicy4mqLOhcdt5zH8xXT4Dutn98iob"
    "7dP1+hihqU4lWX5AmVe8iBjK0WQNc/NklWVEKXAfgYVgtXQGdKul10v1YzTB+lz4dIo7cF7zhjIhvgGs3ujAbqnyT7/Cl2hC"
    "HPII1Uow17Ly1VoiINpOGprupBjKXNLeq0resst7aoawzsIFfENKPlkOUCPsbJG5P7N/DnQf7JZX/E3RoW0phjhNuVdTwuc9"
    "L9ilZopNfSyghkJiJxrR9IT0WIh4lpdQv9k7B7r7lde180lxk98b3DCwQYa4XlNGG8NfxOWujLArYLDQBQ2frnbSbWhZ83vh"
    "7yRV3Pixz/e2Bh31k2YEotelm6Ko8Avf8TMHCKx49qpIeJ/jO2AnEzrtZ7MKE7+MdoSVG1twjtTBr0uvK1F77XhuBY8MkiPc"
    "LlxkTZ23PKWMwDN5bX1B2l8kEcFNoXpHtZE6KO6phw7BmKyvME4r1jt6WE2dHf7G97ynTjWerJi2/njdIcmNaNFljSiPvVLc"
    "PQ3UY+u3OzJ0xqZcHALqWNiU8DqomdzjR/7KNh4CCzueMi5IU6+u7kry9DFDTZGv/qoMbRQmQOEw8oHGIXc2AQobzTuqZNtU"
    "lezwhn3TCR8gcE0AOZ3mv6Y9vdDxSyokR/hgtnS6Kj2xbmxZ9+WYTqxGYBHiUWPvTGpQRYXn/I3vOcTHwwcsBH5MmdXICKQR"
    "QN12JEiTTyPApnD469Jph/yUda+2Vz0yPoUYQ7jVxSNE8AaHj06M3NbU2WOHN3i4VJGmdisKGqkWj02n7rbTbdiyj0udJFy5"
    "vne6fjp1ffBmf8XfHMejS+q4WCg0+9TwWSIfF9ZE/W5bvCVAo/AMsI1G/MAwl4iJmH7SiVovYAenydyZ0N2tbXk1soTHIiW6"
    "jd+iBxA0dk3MUVOjzo/sUWQWF0WNCm8p4ZPB5ghlXIjmJ5MdTycadZHCaSW44tQb3MEnxEXhk0Mun7Qtz4UOPn7mrQSbyZGy"
    "54VrB9eH3qzub9Qdv2EXBxtBiI/AJzARBvvEFMf0x0bavyI0BXw2ISH56m/2uoTuS+/97mvDYSQ6oLgdlHIS8ehRYVoRQwK8"
    "hGRbOASmlzuYiMCW7oooIOoWj6pnnNUPds9z1s9YC7vOinUssSqGXuxgHctNeGhsMmRwkEBoymsskwzhgrFCa6NNAgJs5t5/"
    "VekButtqfk+stkImOqKe7j8IpeN2+yiSGhKY+I5tqHpDAvTZSiOFDkG3Uqpxlq/tnR8iO3P9YTtflR1EJXQfVPtneT26hYQe"
    "E0yOqjR03INrkb7bTJzQQrrFm2sffg8jhoiHM/v3Kz1CB4s7zmqnwLS+RvcxvFkkSiwCAtMyEVX/hoCFjY1lOnLSL2/dXCMN"
    "N0eQr/zLy3aB6XPWcnnmwDr31IgBl+CJOFUTvaMde27aOOwYriDG0pnTu7rsDMBG/Zd8MFdqn1M4X+5eyVVlWI2tBCuWPsYa"
    "cpylX5xq1uguVGaz6rcV2Abti07lACndEmrWLQ1Yp1084piKlQhCQqYPfrvTJ3RfejdeWg+DeBqwiju+znfB08V9ND6Gk/ZS"
    "luxUl0jqWMzcP89A6RA6+M1erhKaqYiyZYZI95bVFd9mR+H3BzP7/7Q/AOjg5kt3lbjYWxjZ021NFn0JZa9bQ60pDI2e9SzZ"
    "2h+2O8uft13L5YVd/cDCIYxr9DtxGC6j4kymnHrzb537N152WvrQwfr9dq5mYXEUm+pWTJrbqYd2meDTHYfBj991zup0ebk8"
    "QOjgoxf2so7nb+tEh0w3WarLdteJrmsEpg//5SUMFLr7leKuvVIwXP6hmRslBjSy+eJBJjrOT4pEdjF7d2kLBgwdPNydOgwf"
    "goOPwkZ0lHIRLdm+i6c89bGvdk+q4/pWK96/qBrHxln5YPdLbwjQwb89m92HABDmjS249NORu6tAa4YSIsCiILoNyNXi3q9L"
    "MBTo4MNtawVyyDh/3k9lxuWEukGIGsZBr+hfs7Xfb8PQoLtfWdyRq1ls04Chuq4rvGhK83SF2a6LqZFMVlhoQgT5uzefwxCh"
    "g3/enTnwV3qjqL48d935OfAmY7tPxP/rLC9t36sNGTr492d2ABlEFz/e6un0nxqa7EBXsyg9ROOQXZkvdebL9QkdfP5j5n4v"
    "BDH6Kigdd/FgEqou9vK1ve5uuT6gu6NuvsjdlT3ly/SVmdJiYWYoVHsBrueE11eVxR17xTbUnzoO54i2hrRsW4l4kaIjnFo4"
    "Fd14FpIMmS+6NU/6hA5W9q/vyNWcafuN6kY6H1g5WbLXTUyStiXCGMvcxiX/2c3nd3pM9Yt+PsxfFnYWg00PgQvUAadDFSom"
    "hkJHM9gLwMWnDmSxyX1xc+tuDUYsdZGjMFdyVmRM/Ce6dM31KV/pdgEYwOcNgBwOmbs3+gCuT6kD+Gbp/bz/yANDCCo6rAQe"
    "ylkaWk/c4FYNlywO9hcfbn9Voa/Gmj7XH7aLe/qBhY1P/WoyEO27drNI5N1+gRuA1AH8eeldMdg8QpPruzJSprQXdXDQWTh3"
    "P3p+r0Yadur32/N7+kGBPN7A81lcODpV5+4nz/oHbkBSB/Dfi3tF/9GRacQPCU0Llehy3r0cGnf5aa8SHVuQuodZdMI0FIt4"
    "dkKe3GcfP29QAKcEOviu+HqJzZqhRnIICQ2DeZIIQw/Y6e113KbukhRS99SWFTngPhoHC1anDj95dntAJdtikNbT94Wtj2uP"
    "hUkfBvi48QhoyXDmMPcam+l0Nq3ouslKHHMFZKOtbHX24N+eDfDJB7uVjwvbS7WfojYqjSagYOZ2DL+7u5sJc50TT/XWZt0c"
    "9uLh4OAil2f2Oy0YGgt08FQ+u1PJiw0PiaQaU/aKoc9/6wW6wX8ecewdnGiYyxfXd35VHvA7DSN+8adb+zPVTUHGSNywGWu7"
    "H/w7PLo3cazjMIfAubu0vVwe+DsNZyvXi9tLR5sCl3rcnUMceB13Ad0oMvbaHCVn9dreh9u3h/DYYlgP8JP7+sZeUaz5WFim"
    "vXiYo3xFD2bKMLRAciivTfb+XKlds1XqoAP4461q/uixipWIMsZysutcDpwyZ7B+WmfvqFBmVJlHxkwmyKxka4s7g1eUI4EO"
    "NuZfL9Ue+QRkKBByiDPmrPmgXZRm31OUP8kQosmQuT9dHqxFOWLo4O/Z7Y/qbvjIx0fiEjDe/Joc8HHxDJUjBsKAPO59SxV3"
    "uymHTSV0AH9Z2CvWH9fROCcUlpj4WUENQoLIe3Vx70+V//XlCPTHaBTWD9nXN6r5cNM/Rmgz+UsmmkQt3JVcda40bHkbKXQA"
    "/zP/ZvGooNaYuNEstKE8BhsHiXN3Zv93O6N6XzHarfv65vt5b1ONUVXqoYzTkDgrMwfXX3fepzNx0MHjwl6xPONvqjFJ3KBj"
    "KCEu7nK2Nio1OUbooljLXtFz/c3AcPhrM/tD4bU0O6VjhHuDyleaelMbiTI9hi4Ozt2Z/d/ujOFzjeuu+Wbp7YKS1npgJkUp"
    "Y2SrBGdz1AvDmAc7NdwJy1BdRdx5NqCxVuZKwwlzpRg6gLWF3cVQqg0dFwNKgpaYZzpGJ+kW5rKI7kc8yNbm94p7t8d2tsS4"
    "t+a74t5CpaDXLLNFoQko6QT/3rjHITX4mRp+nL3qBLOlP2yP+ZOl4VRvzu4uVPPBBoloZ2Mmj0qFxFnYRnnrh/lKcfc3e+Pf"
    "NZEWv+qJfLNYKlYL6huRoPhIT2eORjx061OHC68HUc11oaDDhKvfz1fz/iax+rR6mJXDQKedgMBZztZmR27+TxR0AE9laf79"
    "/FE+3KCD+SDDgy6SfnslV50qz+5/4aVtn0RaA1E/20eFw6nDac9VG5i7r5GuFXGDytnleQ21Gybyg+IEIT9IY+43/MnQ3LES"
    "ayVXnS4XymlRkBMDXVOF1rLVQi3rb2ozT6TBty5olcpGJ46M2QND45W1ZtGbVaHCjIQwqVFDnCpXHC9fmTosVD4L0rwzYjLC"
    "v0/lwWwlX8v5rhV4P/iGMl8ckzWdmCjQiMmoY7lxcaxVTJjhF+CuZLxsbbaUO0o3ZBMGXXM9mj7KHhV8W8nA9h81ZkSKhAJU"
    "hvqxqQAF8hiUUSeujXUfIFOb2c9XnOC2mqSdEJObdPnZruc813O9jOeGUkkltWRDJ8oOmlNKoiVXpBIIJZWlHC9Tz1UnRcIu"
    "GHQn1z/swA6cwA4jGAGhBBiw7MCpfx5cnKf9X7ZQyUXgmq+bAAAAAElFTkSuQmCC"
)

_WHITE_PLAY_CACHE = None


def _white_play_asset():
    """Giai ma + cache anh nut play trang (RGBA). Chi doc 1 lan duy nhat."""
    global _WHITE_PLAY_CACHE
    if _WHITE_PLAY_CACHE is None:
        import io
        _WHITE_PLAY_CACHE = Image.open(
            io.BytesIO(base64.b64decode(_WHITE_PLAY_B64))).convert("RGBA")
    return _WHITE_PLAY_CACHE


def _draw_white_play_badge(base, scale: float, shadow: bool = True):
    """Nut play TRANG: dia xam dac + tam giac trang nhon (asset PNG that).

    Dung cho kieu "white".  Anh nam san trong _WHITE_PLAY_B64 nen khong
    can file roi di kem - tool van chay khi chi co 1 file .py.
    Ve o 4x roi LANCZOS thu nho nen khong bi rang cua.
    """
    w, h = base.size
    size = max(24, int(round(min(w, h) * float(scale))))
    art = _white_play_asset()
    if art.width != size:
        art = art.resize((size, size), Image.LANCZOS)
    out = base.copy()
    out.alpha_composite(art, ((w - size) // 2, (h - size) // 2))
    return out


def draw_youtube_badge(img, style: str = "play", scale: float = 0.30, shadow: bool = True):
    """Ve huy hieu YouTube vao CHINH GIUA anh.

    style="play"  -> o do bo goc + tam giac trang (giong 1 video chua mo)
    style="logo"  -> o do bo goc + tam giac + chu "YouTube" mau trang
    style="white" -> nut play TRANG (asset PNG that nhung san trong code):
                     dia xam dac #B0B0B0 + tam giac trang nhon, vien cung
    scale         -> chieu rong huy hieu / chieu rong anh (0.30 = 30%)
    """
    from PIL import ImageDraw
    base = img.convert("RGBA")
    w, h = base.size
    style_key = str(style or "").strip().lower()
    is_logo = style_key.startswith("logo")
    is_white = style_key.startswith("white") or style_key.startswith("trang")
    try:
        scale = float(scale)
    except Exception:
        scale = 0.30
    scale = max(0.05, min(scale, 0.95))
    if is_white:
        return _draw_white_play_badge(base, scale, shadow).convert("RGB")
    bw = int(round(w * scale))
    bh = int(round(bw / (2.05 if is_logo else 1.417)))
    bw = max(16, min(bw, max(16, w - 2)))
    bh = max(12, min(bh, max(12, h - 2)))
    x0 = (w - bw) // 2
    y0 = (h - bh) // 2
    radius = max(2, int(round(bh * 0.24)))

    if shadow:
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        off = max(2, int(round(bh * 0.10)))
        od.rounded_rectangle(
            [x0 + off, y0 + off, x0 + bw + off, y0 + bh + off],
            radius=radius, fill=(0, 0, 0, 120))
        base = Image.alpha_composite(base, overlay)

    d = ImageDraw.Draw(base)
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=radius, fill=(255, 0, 0, 255))

    th = bh * 0.46
    tw = th * 0.82
    tx = x0 + bw * 0.085 if is_logo else x0 + (bw - tw) / 2.0 + bw * 0.045
    ty = y0 + (bh - th) / 2.0
    d.polygon([(tx, ty), (tx, ty + th), (tx + tw, ty + th / 2.0)], fill=(255, 255, 255, 255))

    if is_logo:
        text = "YouTube"
        avail = bw * 0.78
        size = max(8, int(bh * 0.62))
        font = _youtube_badge_font(size)
        for _ in range(40):
            box = d.textbbox((0, 0), text, font=font)
            if (box[2] - box[0]) <= avail or size <= 8:
                break
            size -= 1
            font = _youtube_badge_font(size)
        box = d.textbbox((0, 0), text, font=font)
        d.text((x0 + bw * 0.255 + (avail - (box[2] - box[0])) / 2.0 - box[0],
                y0 + (bh - (box[3] - box[1])) / 2.0 - box[1]),
               text, font=font, fill=(255, 255, 255, 255))
    return base.convert("RGB")


def make_social_thumbnail(src_path: Path, out_path: Path, width: int = 290, height: int = 290,
                          badge_style: str = "", badge_scale: float = 0.30) -> Path:
    """Resize then crop a square below 300px, centered on the detected face.

    badge_style="play"/"logo" -> ve them huy hieu YouTube o chinh giua anh bia.
    """
    size = max(64, min(int(width), 299))
    with Image.open(src_path) as source:
        img = source.convert("RGB")
    cx, cy = img.width / 2, img.height / 2
    head_ratio = 0.5
    try:
        rgb = np.array(img)
        info = analyze_frame_faces(rgb[:, :, ::-1], max_width=640, with_emotion=False)
        if info.get("boxes"):
            box = info["boxes"][0]
            cx = box["x"] + box["w"] / 2.0
            cy = box["y"] + box["h"] / 2.0
            # Mat nam ~45% tu tren xuong -> chua duoc dinh dau, khong cat tran.
            head_ratio = 0.45
    except Exception:
        pass
    lanczos = getattr(Image, "Resampling", Image).LANCZOS
    ratio = size / min(img.size)
    resized = img.resize((max(size, round(img.width * ratio)),
                          max(size, round(img.height * ratio))), lanczos)
    left = max(0, min(resized.width - size, round(cx * ratio - size / 2)))
    top = max(0, min(resized.height - size, round(cy * ratio - size * head_ratio)))
    result = resized.crop((left, top, left + size, top + size))
    if badge_style:
        try:
            result = draw_youtube_badge(result, style=badge_style, scale=badge_scale)
        except Exception:
            pass  # Loi ve logo khong duoc lam hong anh bia.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(out_path, "JPEG", quality=92, optimize=True)
    return out_path


def find_person_thumbnail_frame(videos: List[Path], timeline: List[Dict], out_path: Path) -> Optional[Dict]:
    """Quet them cac moc deu nhau de tim khung CO NGUOI + net + nhieu cam xuc nhat."""
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
                metrics = image_quality_metrics_from_frame(frame)
                if metrics["faces"] and (best_metrics is None or metrics["score"] > best_metrics["score"]):
                    best_frame, best_metrics, best_video, best_second = frame.copy(), metrics, video, second
        finally:
            cap.release()
    if best_frame is None or not cv2_imwrite_unicode(out_path, best_frame, quality=95):
        return None
    return {"path": str(out_path), "video": best_video.name, "second": best_second,
            "quality": best_metrics["score"], "faces": best_metrics["faces"],
            "emotion": best_metrics.get("emotion", ""), "drama": best_metrics.get("drama", 0.0)}


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

def clean_meta_text(value, max_len: int) -> str:
    """Nhu compact_meta_text nhung lam sach ky tu Adsconex tu choi (HTTP 422).

    Da kiem chung tren 168 bai that: seo_description chua '--' (hai dau gach noi
    lien nhau) bi tra 422 "The seo_description field contains invalid input.",
    nen doi '--' thanh ' - ' va bo ky tu dieu khien truoc khi gui.
    """
    value = "" if value is None else str(value)
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = value.replace("--", " - ")
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
    lo = max(0.0, safe_float(start_second, 0.0) - padding)
    hi = safe_float(end_second, 0.0) + padding
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
        # LUON xuat WAV 16k mono tren dia local: faster-whisper 1.2.1 goi
        # av.open(..., metadata_errors=...) ma PyAV 19 khong nhan tham so do ->
        # TypeError. Dua MANG numpy (doc tu WAV) cho transcribe() thi khong cham
        # tới PyAV. WAV local cung tranh loi decoder native tren o WebDAV (Z:).
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
                            # Du phong: thu model base nhe hon.
                            # KHONG dat OMP/MKL_NUM_THREADS o day - dat bat ky gia tri nao
                            # cung lam ctranslate2 cham gap 4-5 lan (do that: khong dat 28s,
                            # OMP=2 -> 145s). Truong hop nay chi con la luoi an toan cuoi.
                            self._log("Whisper lỗi; thử lại model base nhẹ hơn.")
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

    def cleanup_frames(self):
        """Xoá thư mục frames sau khi đã dùng xong (Vision + ảnh bìa).

        Mặc định tool KHÔNG giữ lại ảnh trung gian: mỗi video sinh ra hàng chục
        khung hình nên chạy nhiều video sẽ rất nặng ổ đĩa. Bật "keep_frames"
        trong tab Image hosting nếu cần giữ lại để kiểm tra.
        """
        if bool(self.cfg.get("keep_frames", False)):
            return
        work_dir = getattr(self, "work_dir", None)
        self.frames = []
        if not work_dir:
            return
        frame_dir = Path(work_dir) / "frames"
        try:
            if frame_dir.is_dir():
                shutil.rmtree(frame_dir, ignore_errors=True)
                self._log("Dọn dẹp: đã xoá thư mục frames (không lưu ảnh trung gian).")
        except Exception as exc:
            self._log(f"WARNING: không xoá được thư mục frames: {exc}")

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
                    # Model co the tra so dang chuoi (vd dau cham fullwidth U+FF0E)
                    # -> chuan hoa ngay tai day, khong de so ban di tiep vao prompt.
                    _ev = dict(item)
                    for _k in ("start_second", "end_second", "start", "end"):
                        if _k in _ev:
                            _ev[_k] = safe_float(_ev.get(_k), 0.0)
                    events.append(_ev)
                elif item:
                    events.append({
                        "start_second": safe_float(batch.get("time_start"), 0.0),
                        "end_second": safe_float(batch.get("time_end"), 0.0),
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
                return safe_float(event.get("start_second"), 0.0)
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
                lang_note = language_instruction(effective_language(self.cfg))
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
                                + strip_language_rules(self.cfg["story_prompt_text"],
                                                       effective_language(self.cfg))
                                + lang_note
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
            start_s = safe_float(ch.get("start_second"), 0.0)
            end_s = safe_float(ch.get("end_second") or (start_s + 1), start_s + 1)
            excerpt = transcript_slice_for_time(transcript, start_s, end_s, padding=45.0)
            if len(excerpt) > 10000:
                lines = excerpt.splitlines()
                step = max(1, len(lines) // 320)
                excerpt = "\n".join(lines[::step][:320])
            chapter_sources.append({
                "number": safe_int(ch.get("number"), idx),
                "title": str(ch.get("title") or ""),
                "start_second": start_s,
                "end_second": end_s,
                "summary": str(ch.get("summary") or ""),
                "source_video": str(ch.get("source_video") or ""),
                "transcript_excerpt": excerpt,
            })
        return chapter_sources

    def _write_chapter_batch(self, analysis: dict, batch: List[Dict], prior_chapters=None) -> List[Dict]:
        nums = [safe_int(x.get("number"), 0) for x in batch]
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
""".strip() + language_instruction(effective_language(self.cfg))
        obj = self._writer_json_call(
            f"Writer chapters {nums[0]}-{nums[-1]}",
            prompt,
            max_tokens=5200,
            temperature=0.42,
        )
        chapters = obj.get("chapters") or []
        return [x for x in chapters if isinstance(x, dict)]

    def _write_one_chapter(self, analysis: dict, source: Dict, continuity: str = "") -> Dict:
        n = safe_int(source.get("number"), 0)
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
{{"chapter": {{"number": {n}, "title": "...", "start_second": {safe_float(source.get('start_second'), 0.0)}, "end_second": {safe_float(source.get('end_second'), 0.0)}, "body": "..."}}}}
""".strip() + language_instruction(effective_language(self.cfg))
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
        # Giu nguyen ngu nghia setdefault, nhung chuan hoa so (model co the tra chuoi).
        ch["start_second"] = safe_float(ch.get("start_second", source.get("start_second", 0)), 0.0)
        ch["end_second"] = safe_float(ch.get("end_second", source.get("end_second", 0)), 0.0)
        return ch

    def _story_metadata(self, analysis: dict, chapter_titles: List[str]) -> dict:
        lang_name = language_prompt_name(effective_language(self.cfg))
        prompt = f"""
Create metadata for a serialized drama article written in {lang_name}.

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
                    max_end = max(max_end, safe_float(e.get("end_second"), 0.0))
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
                    n = safe_int(ch.get("number"), 0)
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
            ch["start_second"] = safe_float(src.get("start_second"), 0.0)
            ch["end_second"] = safe_float(src.get("end_second"), 0.0)
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

        s = safe_float(chapter.get("start_second"), 0.0)
        e = safe_float(chapter.get("end_second") or (s + 0.5), s + 0.5)
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
            # Diem da uu tien co nguoi > do net > cam xuc; KHONG nhan
            # them he so phat do net vi se de khung mo co mat thua khung net.
            score = float(metrics.get("score", 0) or 0)
            score += float(metrics.get("drama", 0) or 0) * 150.0
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

    def _youtube_badge_settings(self):
        """Doc tuy chon logo YouTube tu config. Tra ve (style, scale)."""
        if not bool(self.cfg.get("youtube_badge_on_cover", False)):
            return "", 0.0
        style = str(self.cfg.get("youtube_badge_style", "play") or "play").strip().lower()
        if style not in ("play", "logo", "white"):
            style = "play"
        try:
            pct = float(str(self.cfg.get("youtube_badge_scale", "30")).strip() or "30")
        except Exception:
            pct = 30.0
        pct = max(10.0, min(pct, 60.0))
        return style, pct / 100.0

    def attach_local_images(self, story: dict):
        # Image data from the writer is never used in a chapter, including in
        # video-only runs where frame extraction returned no usable frames.
        for ch in story.get("chapters", []):
            ch.pop("image_local", None)
            ch.pop("image_url", None)
        if not self.frames:
            self._log("WARNING: Không có khung hình dùng được để tạo ảnh bìa.")
            return story

        def thumb_score(f):
            # Điểm "có người" đã nằm trong quality; cộng thêm cảm xúc/drama
            # để giữa các khung cùng có mặt thì khung nhiều drama được ưu tiên.
            return float(f.get("quality", 0) or 0) + float(f.get("drama", 0) or 0) * 250.0

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
        thumb = max(pool, key=thumb_score)
        original_thumb = Path(thumb["path"])
        story["thumbnail_original_local"] = str(original_thumb)
        badge_style, badge_scale = self._youtube_badge_settings()
        cover_path = make_social_thumbnail(original_thumb, self.work_dir / "thumbnail_website_290x290.jpg",
                                           badge_style=badge_style, badge_scale=badge_scale)
        story["thumbnail_local"] = str(cover_path)
        story["thumbnail_size"] = "290x290"
        story["youtube_badge"] = bool(badge_style)
        emotion = (thumb.get("emotion") or "").strip()
        sharp = float(thumb.get("sharpness", 0) or 0)
        self._log("Ảnh bìa website: 290x290px | ưu tiên CÓ NGƯỜI + nét + cảm xúc"
                  + (f" | cảm xúc: {emotion}" if emotion else "")
                  + f" | nét: {sharp:.0f}"
                  + (f" | logo YouTube: {badge_style} {badge_scale*100:.0f}%" if badge_style else ""))
        if people and sharp and sharp < 25:
            self._log("LƯU Ý: khung có người nét thấp (video rung/mờ); nên kiểm tra lại ảnh bìa.")

        if bool(self.cfg.get("facebook_thumbnail_mode", True)):
            social_thumb = self.work_dir / "thumbnail_facebook_290x290.jpg"
            make_social_thumbnail(original_thumb, social_thumb)
            story["facebook_thumbnail_local"] = str(social_thumb)
            self._log("Ảnh bìa Facebook: 290x290px | cắt vuông theo khuôn mặt")
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
            + ("Use natural American English, about 180-300 words. Put EACH sentence or dialogue "
               if language_prompt_name(effective_language(self.cfg)) == "English" else
               f"Use {language_prompt_name(effective_language(self.cfg))}, about 180-300 words. "
               "Put EACH sentence or dialogue ")
            + "line in its own paragraph with one blank line before the next. Start inside the dangerous event, with the narrator's "
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
            "End EXACTLY with: " + CAPTION_CTA.get(effective_language(self.cfg),
                                                    CAPTION_CTA["English"]) + "\n"
            "Return STRICT JSON only: {\"caption\": \"first-person caption with \\n\\n between paragraphs\"}.\n\n"
            "STORY CONTEXT:\n" + json.dumps(source, ensure_ascii=False)
        )
        cta = CAPTION_CTA.get(effective_language(self.cfg), CAPTION_CTA["English"])
        for attempt in range(2):
            caption = str(self._writer_json_call("Facebook caption", prompt, max_tokens=1400).get("caption") or "").strip()
            lang_key = effective_language(self.cfg)
            markers = FIRST_PERSON_MARKERS.get(lang_key, FIRST_PERSON_MARKERS["English"])
            marker_re = re.compile("|".join(re.escape(m) for m in markers), flags=re.I)
            hits = marker_re.findall(caption)
            # Ngon ngu pro-drop: nhan them dong tu ngoi thu nhat (bo dau de so sanh).
            verbs = FIRST_PERSON_VERBS.get(lang_key) or []
            # Tieng Nhat/Han/Thai khong dan cach bang dau cach -> dem ky tu thay vi "tu".
            if lang_key in NO_SPACE_LANGUAGES:
                length_ok = 200 <= len(caption) <= 4000
            else:
                length_ok = 150 <= len(caption.split()) <= 330
            # Cac cau cam la tieng Anh -> chi kiem khi ngon ngu la tieng Anh.
            if lang_key == "English":
                no_camera = not re.search(
                    r"screen went black|hard cut|the video ends|the viewer", caption, flags=re.I)
            else:
                no_camera = True
            # "Mo dau bang ngoi thu nhat": xet 2 doan dau (khong chi doan 1) de
            # khong loai oan ngon ngu pro-drop / cau mo dau ngan.
            head = "\n\n".join(caption.split("\n\n")[:2])
            opens_first_person = bool(marker_re.search(head)) or (
                bool(verbs) and any(
                    re.search(r"\b" + re.escape(v) + r"\b",
                              "".join(ch for ch in head.lower() if ch.isalnum() or ch.isspace()))
                    for v in verbs))
            if (caption and length_ok and caption.count("\n\n") >= 4
                    and len(hits) >= 4
                    and opens_first_person
                    and no_camera):
                # Chuan hoa cau ket ve dung CTA cua ngon ngu da chon; bo moi CTA
                # cu (ke ca tieng Anh) neu model lo giu lai.
                body = caption
                for old_cta in ALL_CTAS:
                    if body.endswith(old_cta):
                        body = body[:-len(old_cta)].rstrip()
                        break
                caption = body + "\n\n" + cta
                return caption
            self._log(f"Caption chưa đúng giọng kể/độ dài (lần {attempt + 1}/2); viết lại.")
            prompt += "\n\n" + CAPTION_REWRITE_NOTE.get(lang_key, CAPTION_REWRITE_NOTE["English"])
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
            n = safe_int(ch.get("number"), idx + 1)
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
        base = str(self.cfg.get("adsconex_base_url") or "https://dramanest.gigglelo.com/api").strip().rstrip("/")
        host = urlsplit(base).netloc or "dramanest.gigglelo.com"
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

    def _adsconex_send(self, payload: dict):
        """POST /posts kem retry loi TAM THOI + xin luot tran toc do dung chung.

        Tra (response, data, payload). KHONG raise: caller tu quyet dinh theo
        response.status_code (r = None nghia la loi mang sau khi het luot thu).
        """
        base = str(self.cfg.get("adsconex_base_url") or "https://dramanest.gigglelo.com/api").strip().rstrip("/")
        endpoint = f"{base}/posts"
        token = str(self.cfg.get("adsconex_api_key", "") or "").strip()
        headers = self._adsconex_headers(token)

        # Retry loi TAM THOI (Cloudflare 502 origin_bad_gateway / 503 / 504 / 429)
        # kem backoff theo Retry-After. 502 nghia la origin chua tao bai (da kiem chung
        # tren site that), nen thu lai KHONG sinh bai trung.
        max_attempts = max(1, min(6, safe_int(self.cfg.get("adsconex_retry_count", 3), 3)))
        base_delay = max(5.0, safe_float(self.cfg.get("adsconex_retry_delay", 60), 60.0))
        # Circuit breaker: origin da sap lien tiep >=2 bai thi chi thu 1 lan roi luu payload.
        # Retry 3 lan x 60/120s cho MOI bai lam batch cham hang gio ma vo ich (origin sap
        # hang gio thi vai phut khong cuu duoc). Bai loi van duoc luu lai de dang lai sau.
        if adsconex_origin_down():
            max_attempts = 1
        r = None
        data = {}
        seo_stripped = False
        for attempt in range(1, max_attempts + 1):
            # Tran toc do dung chung: cho toi khi co luot moi POST.
            self._log("Adsconex xin luot dang: " + _adsconex_wait_for_slot(self.cfg))
            try:
                r = requests.post(endpoint, headers=headers, json=payload, timeout=180)
            except Exception as e:
                r = None
                data = {"network_error": str(e)}
                if attempt >= max_attempts:
                    break
                wait_s = min(base_delay * (2 ** (attempt - 1)), 300.0)
                self._log(f"Adsconex network error ({e}) | thu lai {attempt + 1}/{max_attempts} sau {wait_s:.0f}s")
                time.sleep(wait_s)
                continue

            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:2000]}

            if r.status_code in (200, 201):
                adsconex_note_result(True)
                break

            if r.status_code == 422 and "seo_description" in json.dumps(data, ensure_ascii=False).lower() \
                    and "seo_description" in payload and not seo_stripped:
                # Server tu choi meta description (da gap that: chuoi chua '--').
                # Go han truong nay roi gui lai -> bai van dang duoc, chi thieu meta
                # description, thay vi mat ca bai.
                payload.pop("seo_description", None)
                seo_stripped = True
                self._log(
                    "Adsconex 422: seo_description bi tu choi -> go truong nay va dang lai "
                    "(bai van len, chi thieu meta description)."
                )
                continue

            if attempt >= max_attempts or not _adsconex_is_transient(r.status_code, data):
                break

            wait_s = max(5.0, _adsconex_retry_after_seconds(r, base_delay * (2 ** (attempt - 1))))
            detail = json.dumps(data, ensure_ascii=False)[:200] if isinstance(data, (dict, list)) else str(data)[:200]
            self._log(
                f"Adsconex HTTP {r.status_code} (loi tam thoi) | thu lai {attempt + 1}/{max_attempts} "
                f"sau {wait_s:.0f}s | {detail}"
            )
            time.sleep(wait_s)

        # Dem so bai loi LIEN TIEP de bat circuit breaker (origin sap hang gio).
        # Chi tinh la "loi" khi that su tam thoi (502/503/504/429/520-524 hoac loi mang);
        # 422/401/500 khong lam bat breaker.
        if r is not None and r.status_code in (200, 201):
            adsconex_note_result(True)
        elif r is None or _adsconex_is_transient(r.status_code, data):
            adsconex_note_result(False)
        else:
            adsconex_note_result(True)
        return r, data, payload

    def _adsconex_send_all(self, payload: dict, resume: dict = None):
        """Chia content thanh nhieu phan nho roi gui lien tiep (xem adsconex_split_chunks).

        Origin cua site 502 khi 1 request qua lon HOAC tao qua nhieu chuong trong
        1 lan (timeout xu ly ~12s). Gui nhieu lan CUNG 'permalink' + 'series_id'
        -> server tu gop vao MOT series, chuong dung thu tu.

        'resume' = {"series_id": int, "posted_chapters": int} khi dang LAI bai loi da
        len mot phan: chi gui cac phan con thieu vao dung series cu (khong dang lai
        tu dau -> khong sinh chuong trung).

        DON VI LA CHUONG, khong phai "phan": so chuong da len tren site (API tra ve)
        phai doi chieu voi so chuong trong content de biet con thieu bao nhieu. Neu
        doi chieu sai don vi thi bai DA DU chuong van bi dang lai -> SINH SERIES TRUNG.

        Tra (r, data, payload) nhu _adsconex_send de caller khong phai doi:
        - Moi phan OK: data = {"posts": <gop tat ca>, "count": N, "chunks": k}
        - Bat ky phan loi: tra loi cua phan do + payload GOC (de luu nguyen de dang lai).
        """
        content = str((payload or {}).get("content") or "")
        max_chars = max(8000, safe_int(self.cfg.get("adsconex_max_chars_per_request", 36000), 36000))
        max_chapters = max(1, min(10, safe_int(
            self.cfg.get("adsconex_max_chapters_per_request", 3), 3)))
        intro, blocks = adsconex_split_blocks(content)
        total_chapters = len(blocks)
        if not blocks:
            return self._adsconex_send(payload)

        resume = resume if isinstance(resume, dict) else {}
        series_id = safe_int(resume.get("series_id"), 0)
        # Ho tro ca ten cu ('posted_chunks') de khong vo ban ghi pending da luu.
        posted = safe_int(resume.get("posted_chapters", resume.get("posted_chunks")), 0)
        posted = max(0, min(posted, total_chapters))

        # Bai DA DU chuong tren site -> khong gui gi nua (dang lai se tao series trung).
        if posted >= total_chapters:
            self._log(f"Adsconex: series da co du {posted}/{total_chapters} chuong tren site "
                      f"-> khong dang lai (tranh tao bai trung).")
            return (None, {"already_complete": True, "count": posted,
                           "series_id": series_id}, payload)

        # Chi gui cac chuong CON THIEU. Chia lai tu so chuong con thieu chu KHONG bo
        # qua nguyen cac "phan" da tinh: neu bai do dang GIUA mot phan (vd da len 1/3
        # chuong cua phan do) thi phan do van con 2 chuong chua len -> phai gui tiep.
        # Bo qua ca phan se lam MAT chuong do.
        remaining = blocks[posted:]
        chunks = adsconex_pack_blocks(intro, remaining, max_chars, max_chapters) or [content]
        chunk_counts = [len(re.findall(r"<p>CHAPTER\s+\d+\s*-\s*[^<]*</p>", c)) for c in chunks]
        resuming = posted > 0

        if len(chunks) <= 1 and not resuming:
            return self._adsconex_send(payload)

        # Khi DANG TIEP: chunk DAU TIEN cung phai mang series_id. Neu thieu, server
        # tao SERIES MOI cho no (mode=chapter khong co series_id = tao series moi) ->
        # chuong doi sang series khac va cac chunk sau lai gop vao series cu, lam
        # chuong bi lech so/lech ten (da bat that: dang tiep 6 chuong -> site chi con
        # 7 chuong, chuong 5-7 mang ten chuong 8-10).
        if resuming and not series_id:
            series_id = adsconex_series_id_for_slug(self.cfg, slug)
            if not series_id:
                self._log("Adsconex DUNG: dang tiep nhung khong lay duoc series_id "
                          "-> khong gui (tranh tao series moi lam lech chuong). "
                          "Da luu payload de dang lai.")
                adsconex_write_pending(
                    self.work_dir, payload, "series_id",
                    "dang tiep nhung khong lay duoc series_id cua series cu",
                    extra={"posted_chapters": posted, "total_chapters": total_chapters,
                           "series_id": 0})
                return None, {"error": "series_id_unresolved",
                              "posted_chapters": posted, "posts": all_posts}, payload

        if posted:
            self._log(f"Adsconex DANG LAI TIEP: da co {posted}/{total_chapters} chuong tren site "
                      f"(series_id={series_id}) -> chi gui {total_chapters - posted} chuong con "
                      f"thieu qua {len(chunks)} request.")
        else:
            self._log(f"Adsconex: content {len(content)} ky tu -> chia {len(chunks)} phan "
                      f"(toi da {max_chapters} chuong/request: origin 502 khi tao qua nhieu "
                      f"chuong trong 1 lan), gui lien tiep cung permalink.")
        all_posts = []
        last_r = None
        slug = str(payload.get("permalink") or "").strip()
        for idx, chunk in enumerate(chunks, 1):
            part = dict(payload)
            part["content"] = chunk
            # Chi phan dau mang SEO/thumbnail/series metadata; cac phan sau chi can
            # title + content + permalink de server gop dung series.
            if idx > 1:
                for k in ("seo_title", "seo_description", "seokeyword",
                          "feature_image", "apply_image_to_all"):
                    part.pop(k, None)
            # BAT BUOC: khong co series_id thi moi POST tao 1 series moi -> chuong bi
            # tach ra nhieu series (da bi that). Ap dung cho MOI phan tu phan 2 tro di,
            # VA cho ca phan 1 khi dang DANG TIEP (series da ton tai tu lan truoc).
            if idx > 1 or resuming:
                if not series_id:
                    series_id = adsconex_series_id_for_slug(self.cfg, slug)
                if series_id:
                    part["series_id"] = series_id
                else:
                    # Khong xac dinh duoc series -> DUNG ngay. Neu van POST, server se
                    # tao series MOI cho tung phan (chuong bi tach) va phan con lai se
                    # bi luu payload roi dang lai -> SINH BAI TRUNG. Dung lai an toan:
                    # phan da dang van con tren site, nguoi dung dang lai sau.
                    self._log("Adsconex DUNG: khong lay duoc series_id -> "
                              "khong gui tiep (tranh tao series rieng lam tach chuong "
                              "va sinh bai trung). Da luu payload de dang lai.")
                    adsconex_write_pending(
                        self.work_dir, payload,
                        last_r.status_code if last_r is not None else "series_id",
                        "khong lay duoc series_id truoc khi gui phan %d" % idx,
                        extra={"posted_chapters": posted + sum(chunk_counts[:idx - 1]),
                               "total_chapters": total_chapters,
                               "series_id": series_id})
                    return last_r, {"error": "series_id_unresolved",
                                    "posted_chapters": posted + sum(chunk_counts[:idx - 1]),
                                    "posts": all_posts}, payload
            self._log(f"Adsconex phan {idx}/{len(chunks)} | {len(chunk)} ky tu")
            r, data, _ = self._adsconex_send(part)
            last_r = r
            if r is None or r.status_code not in (200, 201):
                self._log(f"Adsconex phan {idx}/{len(chunks)} LOI "
                          f"(HTTP {r.status_code if r is not None else 'network'}) - "
                          f"luu lai toan bo payload de dang lai.")
                # Luu TIEN DO: cac phan truoc da len that. Lan dang lai chi gui phan
                # con thieu (dung series_id) -> khong dang lai tu dau (tranh trung chuong).
                adsconex_write_pending(
                    self.work_dir, payload,
                    (r.status_code if r is not None else "network"),
                    "phan %d/%d loi" % (idx, len(chunks)),
                    extra={"posted_chapters": posted + sum(chunk_counts[:idx - 1]),
                           "total_chapters": total_chapters,
                           "series_id": series_id})
                return r, data, payload
            posts = data.get("posts") if isinstance(data, dict) else None
            if not isinstance(posts, list) or not posts:
                return r, data, payload
            all_posts.extend(posts)
            if idx == 1 and len(chunks) > 1:
                series_id = adsconex_series_id_for_slug(self.cfg, payload.get("permalink"))
                if series_id:
                    self._log(f"Adsconex: series_id={series_id} -> gop cac phan sau vao cung series.")
            if idx < len(chunks):
                time.sleep(max(0.0, safe_float(self.cfg.get("adsconex_chunk_gap_seconds", 3), 3.0)))
        merged = {"posts": all_posts, "count": len(all_posts), "chunks": len(chunks)}
        self._log(f"Adsconex: da dang {len(all_posts)} chuong qua {len(chunks)} phan.")
        return last_r, merged, payload

    def publish_adsconex(self, story: dict) -> dict:
        """Dang bai len Adsconex (nen Blogbio) o che do chapter.

        Server tu tach chuong theo marker 'CHAPTER N - Title' va tu tao series.
        """
        self._step_start("Adsconex publish")
        token = str(self.cfg.get("adsconex_api_key", "") or "").strip()
        if not token:
            raise RuntimeError("Adsconex API token đang trống. Nhập trong tab Adsconex.")

        base = str(self.cfg.get("adsconex_base_url") or "https://dramanest.gigglelo.com/api").strip().rstrip("/")
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
        seo_title = clean_meta_text(story.get("meta_title") or story.get("title") or "", 240)
        seo_desc = clean_meta_text(story.get("meta_description") or "", 500)
        keywords = clean_meta_text(story.get("keywords") or "", 500)
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
        r, data, payload = self._adsconex_send_all(payload)

        if self.work_dir:
            try:
                _safe_write_text(self.work_dir / "publish_response_adsconex.json",
                                 json.dumps({"http_status": (r.status_code if r is not None else "network"),
                                             "response": data},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

        if r is None and isinstance(data, dict) and data.get("already_complete"):
            # Series da co du chuong tren site -> khong dang lai, KHONG coi la loi.
            site_host = self.cfg.get("adsconex_site_host", "")
            _sid, _posts = adsconex_series_info(self.cfg, payload.get("permalink"))
            link = published_adsconex_link({"posts": _posts}, site_host)
            chapter_links = []
            for idx, post in enumerate(_posts, 1):
                if not isinstance(post, dict):
                    continue
                one = published_adsconex_link({"posts": [post]}, site_host)
                if one:
                    chapter_links.append(f"Chapter {idx}: {one}")
            chapter_links_text_value = "\n".join(chapter_links) + ("\n" if chapter_links else "")
            self._log(f"Adsconex: bai da co du {data.get('count')} chuong tren site | url={link}")
            self._step_end("Adsconex publish")
            adsconex_clear_pending(self.work_dir)
            return {
                "url": link,
                "id": (_posts[0] or {}).get("id") if _posts else None,
                "chapters": _posts,
                "chapter_links": chapter_links,
                "chapter_links_text": chapter_links_text_value,
                "count": len(_posts),
                "provider": "Adsconex",
                "response": data,
                "already_complete": True,
            }

        if r is None:
            detail = json.dumps(data, ensure_ascii=False)[:400] if isinstance(data, (dict, list)) else str(data)[:400]
            # Loi mang: chac chan chua tao bai -> luu payload de dang lai.
            adsconex_write_pending(self.work_dir, payload, "network", detail)
            raise RuntimeError(f"Adsconex network error: {detail}")

        if r.status_code not in (200, 201):
            detail = json.dumps(data, ensure_ascii=False)[:800] if isinstance(data, (dict, list)) else str(data)[:800]
            if adsconex_origin_down():
                self._log(
                    "Adsconex: origin dang sap lien tuc (>=2 bai loi) -> tu day chi thu 1 lan/bai "
                    "roi luu payload, KHONG cho retry dai. Cuoi batch se dang lai."
                )
            if r.status_code in (401, 403):
                # 401/403 = token bi tang verify tu choi (blogbio_verify_failed /
                # missing_bearer_token). Da kiem chung tren site that: bai 403 KHONG he
                # duoc tao (tra 404) -> dang lai KHONG sinh bai trung. Luu payload de
                # khong mat bai (truoc day 42 bai 403 bi mat trang) va noi ro nguyen nhan.
                adsconex_write_pending(self.work_dir, payload, r.status_code, detail)
                raise RuntimeError(
                    f"Adsconex HTTP {r.status_code} - TOKEN BI TU CHOI (blogbio_verify_failed). "
                    f"Kiem tra: (1) API base URL co dung host dang chay khong, (2) token con hieu luc "
                    f"khong - bam nut 'Kiem tra token' trong tab Adsconex. "
                    f"Da luu payload de dang lai (nut 'Dang lai bai loi'). Chi tiet: {detail}"
                )
            if _adsconex_is_transient(r.status_code, data):
                # 502/503/504/429/520-524: origin chua tao bai (da kiem chung 12/12 tra 404)
                # -> luu payload de dang lai sau, khong ton Vision/Writer.
                adsconex_write_pending(self.work_dir, payload, r.status_code, detail)
                raise RuntimeError(
                    f"Adsconex HTTP {r.status_code} (loi tam thoi - da luu payload de dang lai): {detail}"
                )
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
        adsconex_clear_pending(self.work_dir)
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

    def _slink_headers(self, token: str) -> dict:
        """Header cho API fableon (Laravel): Bearer <id|secret>."""
        base = str(self.cfg.get("slink_base_url") or "https://fableon.treeiq.biz/api").strip().rstrip("/")
        host = urlsplit(base).netloc or "fableon.treeiq.biz"
        return {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
            "Referer": f"https://{host}/admin/dashboard",
            "Origin": f"https://{host}",
        }


    def _slink_locale(self) -> str:
        """Slot ngon ngu fableon dung cho title/content/seo_* (en/vi/zh/ko).

        fableon chi co 4 slot; ngon ngu khac (Nhat, Han, Tay Ban Nha...) khong co
        slot rieng nen ghi vao 'en' — dung nhu hanh vi cu.
        """
        key = effective_language(self.cfg)
        return {"Vietnamese": "vi", "Chinese": "zh", "Korean": "ko"}.get(key, "en")


    def _slink_send(self, payload: dict, endpoint: str = ""):
        """POST len fableon kem retry loi TAM THOI. Tra (response, data, payload)."""
        base = str(self.cfg.get("slink_base_url") or "https://fableon.treeiq.biz/api").strip().rstrip("/")
        endpoint = endpoint or f"{base}/posts/chapter"
        token = str(self.cfg.get("slink_api_key", "") or "").strip()
        headers = self._slink_headers(token)
        max_attempts = max(1, min(6, safe_int(self.cfg.get("slink_retry_count", 3), 3)))
        base_delay = max(5.0, safe_float(self.cfg.get("slink_retry_delay", 30), 30.0))
        r = None
        data = {}
        for attempt in range(1, max_attempts + 1):
            try:
                r = requests.post(endpoint, headers=headers, json=payload, timeout=180)
            except Exception as e:
                r = None
                data = {"network_error": str(e)}
                if attempt >= max_attempts:
                    break
                wait_s = min(base_delay * (2 ** (attempt - 1)), 300.0)
                self._log(f"Slink network error ({e}) | thu lai {attempt + 1}/{max_attempts} sau {wait_s:.0f}s")
                time.sleep(wait_s)
                continue
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:2000]}
            if r.status_code in (200, 201):
                break
            if attempt >= max_attempts or not _adsconex_is_transient(r.status_code, data):
                break
            wait_s = max(5.0, _adsconex_retry_after_seconds(r, base_delay * (2 ** (attempt - 1))))
            detail = json.dumps(data, ensure_ascii=False)[:200] if isinstance(data, (dict, list)) else str(data)[:200]
            self._log(f"Slink HTTP {r.status_code} (loi tam thoi) | thu lai {attempt + 1}/{max_attempts} "
                      f"sau {wait_s:.0f}s | {detail}")
            time.sleep(wait_s)
        return r, data, payload


    def publish_slink(self, story: dict) -> dict:
        """Đăng bài lên Slink (fableon.treeiq.biz) ở chế độ chapter qua REST API.

        POST /api/posts/chapter cần title + content + chapters[] (mảng >= 1).
        Server lưu sẵn từng chương; phân trang trong 1 chương dùng {{nextpage}}.
        """
        self._step_start("Slink publish")
        token = str(self.cfg.get("slink_api_key", "") or "").strip()
        if not token:
            raise RuntimeError("Slink API token đang trống. Nhập trong tab Slink.")

        base = str(self.cfg.get("slink_base_url") or "https://fableon.treeiq.biz/api").strip().rstrip("/")
        endpoint = f"{base}/posts/chapter"

        chapters = story.get("chapters", []) or []
        if not chapters:
            raise RuntimeError("Truyện chưa có chapter nào để đăng dạng series.")

        # fableon CHI hieu {{nextpage}}. Marker <!--nextpage--> (dung cho
        # SmartTraffic) bi server coi la HTML comment va xoa sach -> mat phan trang.
        # Vi vay KHONG lay theo cfg["pagebreak_mode"], luon dung {{nextpage}}.
        slink_chapters = build_slink_chapters(story, "{{nextpage}}")
        if not slink_chapters:
            raise RuntimeError("Không dựng được nội dung chapter cho Slink.")

        locale = self._slink_locale()
        title_text = compact_meta_text(story.get("title") or "Untitled Story", 240)
        intro = compact_meta_text(story.get("meta_description") or story.get("summary") or "", 600)
        thumb = (story.get("thumbnail_url") or "").strip()

        payload = {
            "title": slink_localized_value(title_text, locale),
            "content": slink_localized_value(
                f"<p>{html_escape(intro)}</p>" if intro else "", locale),
            "slug": safe_slug(story.get("slug") or title_text or "story"),
            "status": str(self.cfg.get("slink_status") or "published").strip() or "published",
            "type": "post",
            "chapter_skip_intro": True,
            "chapters": slink_chapters,
        }
        raw_category = str(self.cfg.get("slink_category", "") or "").strip()
        if raw_category.isdigit():
            payload["category_id"] = int(raw_category)
        raw_site = str(self.cfg.get("slink_site_id", "") or "").strip()
        if raw_site.isdigit():
            payload["site_ids"] = [int(raw_site)]
        if thumb.startswith("https://"):
            payload["featured_image"] = thumb
        # fableon yeu cau seo_* la ARRAY da ngon ngu; gui string -> 422.
        # Gui kem ca 'en' lam fallback: theme hien tai render the <title>/og tu
        # key 'en', chi gui {vi:...} thi cac the do rong.
        seo_title = clean_meta_text(story.get("meta_title") or story.get("title") or "", 240)
        seo_desc = clean_meta_text(story.get("meta_description") or "", 500)
        if seo_title:
            payload["seo_title"] = slink_localized_value(seo_title, locale)
        if seo_desc:
            payload["seo_description"] = slink_localized_value(seo_desc, locale)

        if self.work_dir:
            try:
                _safe_write_text(self.work_dir / "publish_payload_slink.json",
                                 json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

        self._log(f"Slink POST {endpoint} | chapters={len(slink_chapters)} | locale={locale}")
        r, data, payload = self._slink_send(payload, endpoint)

        if self.work_dir:
            try:
                _safe_write_text(self.work_dir / "publish_response_slink.json",
                                 json.dumps({"http_status": (r.status_code if r is not None else "network"),
                                             "response": data}, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            except Exception:
                pass

        if r is None or r.status_code not in (200, 201):
            detail = (json.dumps(data, ensure_ascii=False)[:600]
                      if isinstance(data, (dict, list)) else str(data)[:600])
            # 401/403/422 Laravel tra ngay -> CHUA tao bai -> luu payload dang lai an toan.
            # 502/503/504/429 = Cloudflare/origin loi tam thoi -> luu payload.
            if r is None or r.status_code in (401, 403, 422, 429, 500, 502, 503, 504,
                                              520, 521, 522, 523, 524):
                slink_write_pending(self.work_dir, payload,
                                    (r.status_code if r is not None else "network"), detail)
            raise RuntimeError(
                f"Slink dang bai that bai (HTTP {r.status_code if r is not None else 'network'}): {detail}")

        site_host = self.cfg.get("slink_site_host", "")
        link = published_slink_link(data if isinstance(data, dict) else {}, site_host)
        if not link:
            slug = str(payload.get("slug") or "").strip()
            host = urlsplit("https://" + re.sub(r"^https?://", "", str(site_host).strip().strip("/"))).netloc
            link = f"https://{host}/{slug}" if host and slug else ""

        post_id = ""
        if isinstance(data, dict):
            for key in ("id", "post_id"):
                if str(data.get(key) or "").strip().isdigit():
                    post_id = str(data[key]).strip()
                    break
            if not post_id and isinstance(data.get("data"), dict):
                for key in ("id", "post_id"):
                    if str(data["data"].get(key) or "").strip().isdigit():
                        post_id = str(data["data"][key]).strip()
                        break

        self._log(f"Slink accepted | HTTP {r.status_code} | chapters={len(slink_chapters)} | url={link}")
        self._step_end("Slink publish")
        slink_clear_pending(self.work_dir)
        return {
            "url": link,
            "id": post_id or None,
            "chapters": slink_chapters,
            "count": len(slink_chapters),
            "provider": "Slink",
            "chapter_verified": True,
            "response": data,
        }


    def publish_current(self, story: dict) -> dict:
        """Chọn net theo cài đặt trong GUI: SmartTraffic, Adsconex hoặc Slink."""
        provider = str(self.cfg.get("net_provider") or "SmartTraffic").strip().lower()
        if provider.startswith("ads"):
            return self.publish_adsconex(story)
        if provider.startswith("slink"):
            return self.publish_slink(story)
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

        # SmartTraffic thinh thoang tra 5xx TAM THOI (da do: CUNG payload do,
        # lan dau 500 ca 2 luot, thu lai sau do vai phut -> 201 trong 0,4s).
        # Vi vay gap 5xx thi thu lai CHINH payload do (khong doi noi dung, khong
        # doi slug) voi gian cach tang dan. So lan thu lai va gian cach lay tu
        # config de sau nay chinh duoc ma khong phai sua code.
        try:
            retry_count = max(0, min(int(str(self.cfg.get("smarttraffic_retry_count", "4")).strip() or "4"), 10))
        except Exception:
            retry_count = 4
        try:
            retry_delay = max(1, min(int(str(self.cfg.get("smarttraffic_retry_delay", "20")).strip() or "20"), 300))
        except Exception:
            retry_delay = 20

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

            # 5xx = loi TAM THOI phia SmartTraffic -> gui lai CHINH payload nay.
            # Giu nguyen slug nen neu lan dau thuc ra da tao bai (server 500 sau
            # khi luu) thi lan sau se la bai trung, khong sinh -2/-3.
            retry_round = 0
            net_error = False
            while r.status_code >= 500 and retry_round < retry_count:
                retry_round += 1
                wait = retry_delay * retry_round
                self._log(
                    f"SmartTraffic HTTP {r.status_code} (lỗi tạm thời phía server) | "
                    f"gửi lại lần {retry_round}/{retry_count} sau {wait}s (giữ nguyên nội dung + slug)"
                )
                time.sleep(wait)
                try:
                    r = requests.post(endpoint, headers=headers, json=payload, timeout=120)
                except Exception as e:
                    last_error = f"Network/request error: {e}"
                    net_error = True
                    break
            if net_error:
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
                smarttraffic_clear_pending(self.work_dir)
                self._step_end("SmartTraffic publish")
                return data

            last_error = f"SmartTraffic HTTP {r.status_code}: {data}"
            self._log(last_error)
            if r.status_code in (400, 401, 403, 404, 429):
                break
            if r.status_code < 500:
                break
            # Da thu lai het luot ma van 5xx: DUNG lai, KHONG thu payload rut gon
            # nua. Payload rut gon dung CUNG slug nen de sinh bai trung/-2 tren
            # website. Nguoi dung bam "Dang lai bai loi" khi server da hoi.
            # LUU payload de lan sau dang lai (khong ton Vision/Writer). Truoc day
            # bai SmartTraffic loi 5xx bi mat, khong co file pending nao.
            smarttraffic_write_pending(self.work_dir, payload, r.status_code, str(data))
            self._log("Đã lưu payload để đăng lại sau. Đã thử lại đủ số lần mà server vẫn lỗi 5xx; "
                      "dừng để tránh tạo bài trùng. Chờ vài phút rồi bấm nút đăng lại bài lỗi.")
            break

        raise RuntimeError(f"{last_error}\n\nRequest/response debug đã được lưu trong output folder.")

    def republish_pending_smarttraffic(self, work_dir, payload: dict) -> dict:
        """Dang lai payload SmartTraffic da luu khi 5xx. KHONG ton Vision/Writer.

        Dung lai story.json trong work_dir de doi ten video sau khi dang thanh cong
        (giong ban Adsconex). Tra dict ket qua, hoac {} khi van loi.
        """
        work_dir = Path(work_dir)
        previous_work_dir = getattr(self, "work_dir", None)
        self.work_dir = work_dir
        try:
            api_key = str(self.cfg.get("smarttraffic_api_key") or "").strip()
            if not api_key:
                raise RuntimeError("SmartTraffic API key is empty.")
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            self.check_publish_site(headers)
            endpoint = "https://apiv3.smarttraffic.app/api/publish/articles"

            try:
                retry_count = max(0, min(int(str(self.cfg.get("smarttraffic_retry_count", "4")).strip() or "4"), 10))
            except Exception:
                retry_count = 4
            try:
                retry_delay = max(1, min(int(str(self.cfg.get("smarttraffic_retry_delay", "20")).strip() or "20"), 300))
            except Exception:
                retry_delay = 20

            r = requests.post(endpoint, headers=headers, json=payload, timeout=120)
            round_no = 0
            while r.status_code >= 500 and round_no < retry_count:
                round_no += 1
                wait = retry_delay * round_no
                self._log(f"[{work_dir.name}] SmartTraffic HTTP {r.status_code} | "
                          f"gửi lại {round_no}/{retry_count} sau {wait}s")
                time.sleep(wait)
                r = requests.post(endpoint, headers=headers, json=payload, timeout=120)

            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}

            if r.status_code not in (200, 201):
                smarttraffic_write_pending(work_dir, payload, r.status_code, str(data))
                self._log(f"[{work_dir.name}] vẫn lỗi HTTP {r.status_code}; giữ payload để thử tiếp.")
                return {}

            smarttraffic_clear_pending(work_dir)
            url = ""
            if isinstance(data, dict):
                url = str(data.get("url") or "").strip()
                nested = data.get("data")
                if not url and isinstance(nested, dict):
                    url = str(nested.get("url") or nested.get("article_url") or "").strip()
            publish_result = {
                "url": url,
                "http_status": r.status_code,
                "provider": "SmartTraffic",
                "response": data,
                "republished_from_pending": True,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            try:
                _safe_write_text(work_dir / "publish_result.json",
                                 json.dumps(publish_result, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            except Exception:
                pass
            self._log(f"[{work_dir.name}] DA DANG LAI | HTTP {r.status_code} | url={url}")

            # Doi ten video (chi khi dang thanh cong va video goc con nguyen ten).
            story = {}
            try:
                story_path = work_dir / "story.json"
                if story_path.is_file():
                    story = json.loads(story_path.read_text(encoding="utf-8") or "{}")
            except Exception:
                story = {}
            video_path = None
            try:
                src = work_dir / "source_video.json"
                if src.is_file():
                    info = json.loads(src.read_text(encoding="utf-8") or "{}")
                    video_path = Path(str(info.get("path") or ""))
            except Exception:
                video_path = None
            renamed = ""
            if isinstance(story, dict) and story and video_path is not None and video_path.is_file():
                script_path = work_dir / "video_script.txt"
                script_text = script_path.read_text(encoding="utf-8") if script_path.is_file() else ""
                try:
                    renamed = self.rename_published_video(video_path, story, publish_result, script_text)
                except Exception as exc:
                    self._log(f"[{work_dir.name}] Dang OK nhung doi ten video loi: {exc}")

            return {"work_dir": str(work_dir), "story": story,
                    "publish_result": publish_result, "renamed_video": renamed}
        finally:
            self.work_dir = previous_work_dir

    def republish_pending(self, work_dir, payload: dict, resume: dict = None) -> dict:
        """Dang lai payload da luu cua bai loi 502. KHONG ton Vision/Writer.

        'resume' = {"series_id": int, "posted_chapters": int} khi series da co tren site
        nhung con thieu chuong -> dang TIEP phan con thieu, khong dang lai tu dau.

        Dung lai story.json + source_video.json trong work_dir de doi ten video sau
        khi dang thanh cong. Tra dict ket qua, hoac {} khi van loi.
        """
        work_dir = Path(work_dir)
        previous_work_dir = getattr(self, "work_dir", None)
        self.work_dir = work_dir
        try:
            resume = resume if isinstance(resume, dict) else {}
            resume_sid = safe_int(resume.get("series_id"), 0)
            already_posts = []
            if resume_sid:
                _sid, already_posts = adsconex_series_info(self.cfg, payload.get("permalink"))
            r, data, payload = self._adsconex_send_all(payload, resume=resume)
            try:
                _safe_write_text(work_dir / "publish_response_adsconex.json",
                                 json.dumps({"http_status": (r.status_code if r is not None else "network"),
                                             "response": data},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

            # Bai da du chuong tren site -> khong phai loi, KHONG dang lai (tranh trung).
            already_complete = bool(isinstance(data, dict) and data.get("already_complete"))
            if already_complete and r is None:
                r = type("_R", (), {"status_code": 200})()

            if r is None or r.status_code not in (200, 201):
                detail = (json.dumps(data, ensure_ascii=False)[:600]
                          if isinstance(data, (dict, list)) else str(data)[:600])
                adsconex_write_pending(work_dir, payload,
                                       (r.status_code if r is not None else "network"), detail)
                self._log(f"[{work_dir.name}] Van loi: {detail[:200]}")
                return {}

            posts = data.get("posts") if isinstance(data, dict) else None
            if already_complete:
                # Cac chuong da co san: dung lai danh sach tu API de ghi ket qua.
                posts = list(already_posts)
            if not isinstance(posts, list) or not posts:
                detail = (json.dumps(data, ensure_ascii=False)[:400]
                          if isinstance(data, (dict, list)) else str(data)[:400])
                adsconex_write_pending(work_dir, payload, r.status_code, "thieu 'posts': " + detail)
                self._log(f"[{work_dir.name}] Response thieu 'posts': {detail[:200]}")
                return {}

            # Dang TIEP: gop cac chuong DA len truoc do (lay tu API) voi cac chuong vua
            # gui -> danh sach day du, dung thu tu. Neu khong gop thi chapter_links.txt
            # chi co phan vua dang (thieu chuong dau).
            all_posts = list(already_posts) + [p for p in posts if isinstance(p, dict)]
            site_host = self.cfg.get("adsconex_site_host", "")
            link = published_adsconex_link({"posts": all_posts}, site_host)
            if not link:
                first = all_posts[0] if all_posts else {}
                link = str(first.get("link") or first.get("url") or "").strip()

            chapter_links = []
            for idx, post in enumerate(all_posts, 1):
                if not isinstance(post, dict):
                    continue
                one = published_adsconex_link({"posts": [post]}, site_host)
                if one:
                    chapter_links.append(f"Chapter {idx}: {one}")
            chapter_links_text_value = "\n".join(chapter_links) + ("\n" if chapter_links else "")

            publish_result = {
                "url": link,
                "id": (all_posts[0] or {}).get("postId") if all_posts else None,
                "chapters": all_posts,
                "chapter_links": chapter_links,
                "chapter_links_text": chapter_links_text_value,
                "count": len(all_posts),
                "provider": "Adsconex",
                "response": data,
                "republished_from_pending": True,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            try:
                _safe_write_text(work_dir / "publish_result.json",
                                 json.dumps(publish_result, ensure_ascii=False, indent=2), encoding="utf-8")
                _safe_write_text(work_dir / "chapter_links.txt", chapter_links_text_value, encoding="utf-8")
            except Exception:
                pass
            adsconex_clear_pending(work_dir)
            self._log(f"[{work_dir.name}] DA DANG LAI | HTTP {r.status_code} | chapters={len(posts)} | url={link}")

            # Doi ten video (chi khi dang thanh cong va video goc con nguyen ten).
            story = {}
            try:
                story_path = work_dir / "story.json"
                if story_path.is_file():
                    story = json.loads(story_path.read_text(encoding="utf-8") or "{}")
            except Exception:
                story = {}
            video_path = None
            try:
                src = work_dir / "source_video.json"
                if src.is_file():
                    info = json.loads(src.read_text(encoding="utf-8") or "{}")
                    video_path = Path(str(info.get("path") or ""))
            except Exception:
                video_path = None
            renamed = ""
            if isinstance(story, dict) and story and video_path is not None and video_path.is_file():
                script_path = work_dir / "video_script.txt"
                script_text = script_path.read_text(encoding="utf-8") if script_path.is_file() else ""
                try:
                    renamed = self.rename_published_video(video_path, story, publish_result, script_text)
                except Exception as exc:
                    self._log(f"[{work_dir.name}] Dang OK nhung doi ten video loi: {exc}")

            return {"work_dir": str(work_dir), "story": story,
                    "publish_result": publish_result, "renamed_video": renamed}
        finally:
            self.work_dir = previous_work_dir

    def republish_pending_slink(self, work_dir, payload: dict) -> dict:
        """Dang lai payload da luu cua bai Slink loi. KHONG ton Vision/Writer."""
        work_dir = Path(work_dir)
        previous_work_dir = self.work_dir
        self.work_dir = work_dir
        try:
            base = str(self.cfg.get("slink_base_url") or "https://fableon.treeiq.biz/api").strip().rstrip("/")
            r, data, payload = self._slink_send(payload, f"{base}/posts/chapter")
            try:
                _safe_write_text(work_dir / "publish_response_slink.json",
                                 json.dumps({"http_status": (r.status_code if r is not None else "network"),
                                             "response": data},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

            if r is None or r.status_code not in (200, 201):
                detail = (json.dumps(data, ensure_ascii=False)[:600]
                          if isinstance(data, (dict, list)) else str(data)[:600])
                slink_write_pending(work_dir, payload,
                                    (r.status_code if r is not None else "network"), detail)
                self._log(f"[{work_dir.name}] Vẫn lỗi: {detail[:200]}")
                return {}

            site_host = self.cfg.get("slink_site_host", "")
            link = published_slink_link(data if isinstance(data, dict) else {}, site_host)
            if not link:
                slug = str(payload.get("slug") or "").strip()
                host = urlsplit("https://" + re.sub(r"^https?://", "", str(site_host).strip().strip("/"))).netloc
                link = f"https://{host}/{slug}" if host and slug else ""

            post_id = ""
            if isinstance(data, dict):
                for key in ("id", "post_id"):
                    if str(data.get(key) or "").strip().isdigit():
                        post_id = str(data[key]).strip()
                        break
                if not post_id and isinstance(data.get("data"), dict):
                    for key in ("id", "post_id"):
                        if str(data["data"].get(key) or "").strip().isdigit():
                            post_id = str(data["data"][key]).strip()
                            break

            publish_result = {
                "url": link,
                "id": post_id or None,
                "chapters": payload.get("chapters") or [],
                "count": len(payload.get("chapters") or []),
                "provider": "Slink",
                "chapter_verified": True,
                "response": data,
                "republished_from_pending": True,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            try:
                _safe_write_text(work_dir / "publish_result.json",
                                 json.dumps(publish_result, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            slink_clear_pending(work_dir)
            self._log(f"[{work_dir.name}] ĐÃ ĐĂNG LẠI | HTTP {r.status_code} | url={link}")

            story = {}
            try:
                story_path = work_dir / "story.json"
                if story_path.is_file():
                    story = json.loads(story_path.read_text(encoding="utf-8") or "{}")
            except Exception:
                story = {}
            video_path = None
            try:
                src = work_dir / "source_video.json"
                if src.is_file():
                    info = json.loads(src.read_text(encoding="utf-8") or "{}")
                    video_path = Path(str(info.get("path") or ""))
            except Exception:
                video_path = None
            renamed = ""
            if isinstance(story, dict) and story and video_path is not None and video_path.is_file():
                script_path = work_dir / "video_script.txt"
                script_text = script_path.read_text(encoding="utf-8") if script_path.is_file() else ""
                try:
                    renamed = self.rename_published_video(video_path, story, publish_result, script_text)
                except Exception as exc:
                    self._log(f"[{work_dir.name}] Đăng OK nhưng đổi tên video lỗi: {exc}")
            return {"work_dir": str(work_dir), "story": story,
                    "publish_result": publish_result, "renamed_video": renamed}
        finally:
            self.work_dir = previous_work_dir


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
        elif provider.startswith("slink"):
            link = published_slink_link(result, self.cfg.get("slink_site_host", ""))
        else:
            link = published_article_link(result, self.cfg.get("site_host", ""))
        if not link:
            self._log("Chưa nhận được ID bài viết từ API; giữ tên video, không đoán link từ slug.")
            return ""
        # Ten file video hien ra cho nguoi xem -> phai cung ngon ngu voi truyen.
        name_lang = language_prompt_name(effective_language(self.cfg))
        try:
            prompt = (
                f"Write one short {name_lang} Facebook video title based on the actual VIDEO HOOK. "
                "Make a specific curiosity gap without revealing the outcome or inventing facts. "
                f"Write a fuller, natural title of about 110-140 characters in {name_lang} when the hook has enough detail. "
                f"Add 3 to 5 relevant, short hashtags written in {name_lang}. "
                "Hashtags must use plain ASCII letters/digits/underscore only (no accents, no spaces) "
                "so the filename stays safe on network drives. "
                "The complete VIDEO FILENAME, including hashtags, full story article ID link and .mp4 extension, "
                "must stay at or below 219 characters; the application will trim the hook first if needed. "
                f"No URLs or semicolons. The title and hashtags MUST be in {name_lang}, NOT in English. "
                "Return JSON: {\"title\":\"...\",\"hashtags\":[\"#...\",\"#...\",\"#...\"]}.\n"
                + json.dumps({"video_script": script[:3500], "story_title": story.get("title", ""),
                              "caption_start": str(story.get("facebook_caption", ""))[:650],
                              "required_language": name_lang}, ensure_ascii=False)
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
                tags = fallback_hashtags_for_language(effective_language(self.cfg))
            if not title:
                raise ValueError("AI returned an empty title")
        except Exception as exc:
            self._log(f"Không tạo được tên bằng AI ({exc}); dùng tiêu đề bài viết.")
            title = re.sub(r"[;#\r\n]+", " ", str(story.get("title") or video.stem)).strip()[:160]
            tags = fallback_hashtags_for_language(effective_language(self.cfg))
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
        # Ghi ro ngon ngu dang ap de con doi chieu ket qua (va luu chung vao
        # prompt_used.txt ben duoi, de xem lai sau nay tool da chay voi ngon ngu nao).
        run_language = effective_language(self.cfg)
        combined_prompt += language_instruction(run_language)
        self._log(f"Ngôn ngữ áp cho truyện + mọi chapter: {language_label(run_language)}")
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
        self.cleanup_frames()

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
                     values=("SmartTraffic", "Adsconex", "Slink"), state="readonly", width=16).grid(
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

        # ------------------------------------------------------------
        # NGON NGU TRUYEN (mac dinh TAT -> giu nguyen hanh vi cu: tieng Anh)
        # Dat ngay tai muc 1 "Dau vao": ap cho MOI net dang bai duoc chon.
        # ------------------------------------------------------------
        lang_box = ttk.LabelFrame(source, text="Ngôn ngữ truyện (áp cho mọi web/net đã chọn)")
        lang_box.grid(row=5, column=0, columnspan=5, sticky="ew", pady=(8, 2))
        lang_box.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            lang_box,
            text=("Viết truyện + caption + tên file bằng ngôn ngữ chọn bên dưới "
                  "(mặc định TẮT = tiếng Anh như cũ)"),
            variable=self.vars["language_enabled"],
            command=self._refresh_language_state
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(2, 4))

        ttk.Label(lang_box, text="Ngôn ngữ", width=26).grid(row=1, column=0, sticky="w", pady=3)
        self.language_combo = ttk.Combobox(
            lang_box, textvariable=self.vars["language"],
            values=LANGUAGE_KEYS, state="readonly", width=30
        )
        self.language_combo.grid(row=1, column=1, sticky="w", padx=(6, 8), pady=3)
        self.language_hint = ttk.Label(lang_box, text="", foreground="#0a7", width=24)
        self.language_hint.grid(row=1, column=2, sticky="w", pady=3)

        self.language_note = ttk.Label(
            lang_box,
            text="",
            foreground="#555", justify="left", wraplength=950
        )
        self.language_note.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 2))
        # O 'Cau chien trong ten file' nam o tab khac; theo doi ca 2 bien de cap nhat
        # ngay khi nguoi dung doi lua chon (khong phai bam Save moi thay).
        self.vars["language"].trace_add("write", lambda *a: self._refresh_language_state())
        self.vars["language_enabled"].trace_add("write", lambda *a: self._refresh_language_state())
        self._refresh_language_state()

        # ---------------- TABS ----------------
        nb = ttk.Notebook(top, height=190)
        nb.pack(fill="x", pady=3)

        ai = ttk.Frame(nb, padding=5)
        st = ttk.Frame(nb, padding=10)
        ads = ttk.Frame(nb, padding=10)
        slink = ttk.Frame(nb, padding=10)
        img = ttk.Frame(nb, padding=10)
        settings_tab = ttk.Frame(nb, padding=10)
        guide_tab = ttk.Frame(nb, padding=10)
        prompt_tab = ttk.Frame(nb, padding=5)

        nb.add(prompt_tab, text="Kịch bản & Prompt")
        nb.add(ai, text="Vilao AI")
        nb.add(st, text="SmartTraffic")
        nb.add(ads, text="Adsconex")
        nb.add(slink, text="Slink")
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
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(8, 4))

        st_buttons = ttk.Frame(st)
        st_buttons.grid(row=10, column=0, columnspan=3, sticky="w", pady=4)
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
        ads_chunk = ttk.Frame(ads)
        ads_chunk.grid(row=5, column=2, sticky="w", pady=6)
        ttk.Label(ads_chunk, text="Số chương/lần gửi").pack(side="left")
        ttk.Entry(ads_chunk, textvariable=self.vars["adsconex_max_chapters_per_request"],
                  width=5).pack(side="left", padx=(6, 0))

        ttk.Label(
            ads,
            text=(
                "Đăng ở mode=chapter: server tự tách chương theo marker "
                "'CHAPTER N - Tên chương' và tự tạo series. Phần trước marker đầu là mô tả series."
                " Link bài dạng /blog/<slug>.\n"
                "Category ID: bấm 'Lấy danh sách' (dùng token ở trên) — thường Story = 15.\n"
                "Author: chỉ là chuỗi ghép vào đuôi slug (vd 'aqtn2' → ...-aqtn2). Để trống thì server tự thêm.\n"
                "Số chương mỗi lần gửi: server có timeout xử lý ~12s; gửi quá nhiều chương "
                "trong 1 request bị Cloudflare trả 502. Tool tự chia truyện thành nhiều "
                "request (giữ nguyên 1 series) theo ô dưới — để 3 là an toàn nhất."
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
                str(self.vars["adsconex_site_host"].get() or "dramanest.gigglelo.com").rstrip("/")
                + "/admin/api-docs")
        ).pack(side="left")
        ttk.Button(
            ads_buttons,
            text="Đăng lại bài lỗi",
            command=self.republish_failed_posts
        ).pack(side="left", padx=6)
        ttk.Button(
            ads_buttons,
            text="Kiểm tra token",
            command=self.test_adsconex_token
        ).pack(side="left", padx=6)
        ttk.Button(
            ads_buttons,
            text="Lấy link site",
            command=lambda: webbrowser.open(
                "https://" + re.sub(
                    r"^https?://", "",
                    str(self.vars["adsconex_site_host"].get() or "dramanest.gigglelo.com").strip().strip("/"),
                    flags=re.I))
        ).pack(side="left", padx=6)

        ads_rate = ttk.LabelFrame(ads, text="Giới hạn tốc độ đăng (dùng chung cho MỌI máy)")
        ads_rate.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        ads_rate.columnconfigure(1, weight=1)
        self._row(ads_rate, 0, "Số bài mỗi phút (tổng)", "adsconex_rate_limit_per_min")
        self._row(ads_rate, 1, "File trạng thái chung", "adsconex_rate_shared_path")
        self._row(ads_rate, 2, "Giãn cách khi Z lỗi (giây)", "adsconex_rate_fallback_delay")
        ttk.Label(
            ads_rate,
            text=(
                "Mọi máy cùng đọc/ghi file trạng thái trên ổ Z nên tổng số bài gửi lên net "
                "không vượt mức đặt ở trên \u2014 chạy bao nhiêu máy song song cũng được.\n"
                "Mặc định " + ADSCONEX_RATE_DEFAULT_PATH +
                " (net giới hạn ~6-10 bài/phút; 1 bài = cả truyện ~10 chương).\n"
                "Ổ Z lỗi thì tool tự giãn cách tại máy theo ô cuối, không treo."
            ),
            foreground="#555", justify="left", wraplength=950
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 2))

        # ============================================================
        # SLINK (fableon) - dang bai che do chapter qua REST API
        # ============================================================
        slink.columnconfigure(1, weight=1)

        ttk.Label(slink, text="Slink API Token", width=22).grid(row=0, column=0, sticky="w", pady=4)
        self.slink_key_entry = ttk.Entry(
            slink, textvariable=self.vars["slink_api_key"], show="*"
        )
        self.slink_key_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.slink_key_editing = False
        self.slink_edit_btn = ttk.Button(
            slink, text="Edit Key", command=self.toggle_slink_key_edit
        )
        self.slink_edit_btn.grid(row=0, column=2, padx=(8, 0), pady=4)

        self._row(slink, 1, "API base URL", "slink_base_url")
        self._row(slink, 2, "Site host", "slink_site_host")
        self._row(slink, 3, "Site ID (số)", "slink_site_id")
        slink_cat = ttk.Frame(slink)
        slink_cat.grid(row=4, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(slink_cat, text="Category ID (số)", width=22).pack(side="left")
        ttk.Entry(slink_cat, textvariable=self.vars["slink_category"], width=12).pack(side="left")
        ttk.Button(slink_cat, text="Lấy danh sách", command=self.fetch_slink_categories).pack(
            side="left", padx=(8, 0))

        slink_status_row = ttk.Frame(slink)
        slink_status_row.grid(row=5, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(slink_status_row, text="Trạng thái bài", width=22).pack(side="left")
        ttk.Combobox(
            slink_status_row, textvariable=self.vars["slink_status"],
            values=("published", "draft"), state="readonly", width=12
        ).pack(side="left")

        ttk.Label(
            slink,
            text=(
                "Đăng ở mode=chapter: mỗi chapter gửi kèm trong 1 request POST "
                "/api/posts/chapter (cần title + content + chapters[]). Server tự "
                "lưu từng chương; phân trang TRONG một chương dùng marker {{nextpage}}.\\n"
                "Link bài dạng /<slug> (không có tiền tố /blog/).\\n"
                "API dùng Bearer token dạng id|secret — tạo ở trang /admin/settings/api-tokens "
                "của site. Token chỉ đọc từ ô nhập này, KHÔNG đọc từ .env."
            ),
            foreground="#555",
            wraplength=950,
            justify="left"
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 4))

        slink_buttons = ttk.Frame(slink)
        slink_buttons.grid(row=7, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(
            slink_buttons,
            text="Kiểm tra token",
            command=self.test_slink_token
        ).pack(side="left")
        ttk.Button(
            slink_buttons,
            text="Mở trang API token",
            command=lambda: webbrowser.open(
                "https://" + re.sub(
                    r"^https?://", "",
                    str(self.vars["slink_site_host"].get() or "fableon.treeiq.biz").strip().strip("/"),
                    flags=re.I) + "/admin/settings/api-tokens")
        ).pack(side="left", padx=6)
        ttk.Button(
            slink_buttons,
            text="Lấy link site",
            command=lambda: webbrowser.open(
                "https://" + re.sub(
                    r"^https?://", "",
                    str(self.vars["slink_site_host"].get() or "fableon.treeiq.biz").strip().strip("/"),
                    flags=re.I))
        ).pack(side="left", padx=6)
        ttk.Button(
            slink_buttons,
            text="Đăng lại bài lỗi",
            command=self.republish_failed_posts
        ).pack(side="left", padx=6)

        slink_retry = ttk.LabelFrame(slink, text="Thử lại khi net lỗi tạm thời (502/503/429)")
        slink_retry.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        slink_retry.columnconfigure(1, weight=1)
        self._row(slink_retry, 0, "Số lần thử lại", "slink_retry_count")
        self._row(slink_retry, 1, "Chờ giữa các lần (giây)", "slink_retry_delay")
        ttk.Label(
            slink_retry,
            text=(
                "Chỉ thử lại khi lỗi TẠM THỜI (502/503/504/429/timeout). Lỗi 401/403/422 "
                "dừng ngay để không đăng trùng. Mỗi lần chờ gấp đôi lần trước."
            ),
            foreground="#555", justify="left", wraplength=950
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 2))

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

        ttk.Checkbutton(
            img,
            text=("Giữ lại thư mục frames sau khi chạy (mặc định TẮT: tool tự xoá "
                  "ảnh trung gian cho nhẹ ổ đĩa)"),
            variable=self.vars["keep_frames"],
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 2))

        # ------------------------------------------------------------
        # LOGO YOUTUBE tren anh bia website (mac dinh TAT)
        # ------------------------------------------------------------
        badge_box = ttk.LabelFrame(img, text="Logo YouTube trên ảnh bìa website")
        badge_box.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 2))
        badge_box.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            badge_box,
            text=("Vẽ logo YouTube vào CHÍNH GIỮA ảnh bìa website 290×290 "
                  "(giống 1 video chưa mở)"),
            variable=self.vars["youtube_badge_on_cover"],
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(2, 4))

        ttk.Label(badge_box, text="Kiểu logo", width=22).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(
            badge_box, textvariable=self.vars["youtube_badge_style"],
            values=("play", "logo", "white"), state="readonly", width=12
        ).grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(badge_box, text="Kích thước (% chiều rộng ảnh)", width=28).grid(
            row=2, column=0, sticky="w", pady=3)
        ttk.Entry(badge_box, textvariable=self.vars["youtube_badge_scale"], width=8).grid(
            row=2, column=1, sticky="w", pady=3)

        badge_btns = ttk.Frame(badge_box)
        badge_btns.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 2))
        ttk.Button(
            badge_btns, text="Xem trước logo", command=self.preview_youtube_badge
        ).pack(side="left")
        ttk.Label(
            badge_btns,
            text="  (dùng ảnh bìa của lần chạy gần nhất)",
            foreground="#555"
        ).pack(side="left")

        ttk.Label(
            badge_box,
            text=("Kiểu 'play' = nút play đỏ (khuyên dùng, giống video chưa mở).\n"
                  "Kiểu 'white' = nút play TRẮNG: vòng tròn xám + tam giác "
                  "trắng (giống nút play overlay trên Facebook).\n"
                  "Kiểu 'logo' = thêm chữ YouTube.\n"
                  "Mặc định TẮT: ảnh bìa giữ nguyên như trước. Ảnh bìa Facebook "
                  "KHÔNG bị ảnh hưởng."),
            foreground="#555", justify="left", wraplength=950
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 2))

        img_buttons = ttk.Frame(img)
        img_buttons.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
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

    def preview_youtube_badge(self):
        """Ve thu logo YouTube len anh bia that va mo file cho nguoi dung xem."""
        import tempfile
        cfg = self.current_cfg()
        style = str(cfg.get("youtube_badge_style", "play") or "play").strip().lower()
        if style not in ("play", "logo", "white"):
            style = "play"
        try:
            pct = float(str(cfg.get("youtube_badge_scale", "30")).strip() or "30")
        except Exception:
            pct = 30.0
        pct = max(10.0, min(pct, 60.0))

        source = None
        try:
            if self.last_result and self.last_result.get("story"):
                st = self.last_result["story"]
                for key in ("thumbnail_original_local", "thumbnail_local"):
                    p = str(st.get(key) or "").strip()
                    if p and Path(p).exists():
                        source = Path(p)
                        break
        except Exception:
            source = None
        if source is None:
            messagebox.showinfo(
                APP_NAME,
                "Chưa có ảnh bìa để xem trước.\n\nHãy chạy 1 video trước, "
                "rồi bấm lại nút này.")
            return
        try:
            out_dir = Path(tempfile.gettempdir()) / "youtube_badge_preview"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%H%M%S")
            out = out_dir / f"xem_truoc_{style}_{int(pct)}_{stamp}.jpg"
            make_social_thumbnail(source, out, badge_style=style, badge_scale=pct / 100.0)
            self.log(f"Ảnh xem trước logo YouTube: {out}")
            try:
                os.startfile(str(out))
            except Exception:
                webbrowser.open(out.as_uri())
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Không tạo được ảnh xem trước: {exc}")

    def _refresh_language_state(self):
        """Bật/tắt ô chọn ngôn ngữ theo nút tích; cập nhật ô 'Câu chèn trong tên file'.

        Ô câu chèn tên file CHỈ tự đổi khi nó vẫn đang chứa bản mặc định — nếu
        người dùng đã tự nhập danh sách riêng thì giữ nguyên (tôn trọng ý người dùng).
        """
        try:
            enabled = bool(self.vars["language_enabled"].get())
        except Exception:
            enabled = False
        try:
            self.language_combo.configure(state="readonly" if enabled else "disabled")
        except Exception:
            pass
        key = str(self.vars["language"].get() or "English").strip() or "English"
        try:
            self.language_hint.configure(
                text=("Đang áp: " + language_label(key)) if enabled else "(đang tắt)")
        except Exception:
            pass
        try:
            if enabled:
                self.language_note.configure(
                    text=(f"Đang BẬT: Writer viết toàn bộ truyện, tiêu đề chapter, metadata, caption "
                          f"Facebook và tên file video bằng {language_label(key)} — kể cả khi kịch bản/hook "
                          "nguồn là tiếng Anh thì vẫn dịch và viết lại. Tên riêng (tên nhân vật, địa danh) "
                          "giữ nguyên.\n"
                          "Áp cho MỌI net đang chọn ở 'Net đăng bài' (SmartTraffic / Adsconex / Slink) — "
                          "đổi net không phải bật lại.\n"
                          "Đổi ngôn ngữ KHÔNG tự đổi danh sách 'Câu chèn trong tên file' anh đã tự nhập — "
                          "chỉ thay khi ô đó còn là bản mặc định."))
            else:
                self.language_note.configure(
                    text=("Đang TẮT: mọi thứ chạy y như trước (tiếng Anh), KHÔNG đổi gì — "
                          "kể cả khi đổi net đăng bài."))
        except Exception:
            pass
        try:
            editor = getattr(self, "filename_phrase_editor", None)
            if editor is not None:
                current = editor.get("1.0", "end-1c").strip()
                if _is_default_phrase_list(current):
                    wanted = filename_phrases_for_language(
                        {"language": key, "language_enabled": enabled}).strip()
                    if current != wanted:
                        editor.delete("1.0", "end")
                        editor.insert("1.0", wanted)
        except Exception:
            pass

    def toggle_slink_key_edit(self):
        """Reveal/mask the Slink (fableon) API token while keeping the field editable."""
        self.slink_key_editing = not self.slink_key_editing
        if self.slink_key_editing:
            self.slink_key_entry.configure(show="")
            self.slink_edit_btn.configure(text="Hide Key")
            self.slink_key_entry.focus_set()
            self.slink_key_entry.icursor("end")
        else:
            self.slink_key_entry.configure(show="*")
            self.slink_edit_btn.configure(text="Edit Key")


    def test_slink_token(self):
        """GET /posts để kiểm tra token + kết nối Slink (chạy nền, không treo GUI)."""
        token = str(self.vars["slink_api_key"].get() or "").strip()
        if not token:
            messagebox.showwarning(APP_NAME, "Chưa nhập Slink API Token.")
            return
        base = str(self.vars["slink_base_url"].get() or "https://fableon.treeiq.biz/api").strip().rstrip("/")
        host = urlsplit(base).netloc or str(self.vars["slink_site_host"].get() or "fableon.treeiq.biz")
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Bearer {token}",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
            "Referer": f"https://{host}/admin/dashboard",
            "Origin": f"https://{host}",
        }

        def _worker():
            try:
                r = requests.get(f"{base}/posts", headers=headers, timeout=45)
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception:
                        data = {}
                    rows = data.get("data") if isinstance(data, dict) else None
                    if rows is None and isinstance(data, dict):
                        rows = data.get("posts")
                    count = len(rows) if isinstance(rows, list) else "?"
                    self.log(f"Slink token OK — HTTP 200 | bài hiện có: {count}\n")
                    self.q.put(("slink_test",
                                f"Slink OK — token hợp lệ.\n\nBài hiện có trên site: {count}"))
                elif r.status_code in (401, 403):
                    snippet = (r.text or "")[:300]
                    why = ("Token bị từ chối. Kiểm tra: (1) token còn hiệu lực không, "
                           "(2) API base URL có đúng host không (" + host + "), "
                           "(3) token có đúng dạng id|secret không.")
                    self.log(f"Slink token FAIL — HTTP {r.status_code} | {snippet}\n")
                    self.q.put(("slink_test", f"Slink HTTP {r.status_code}\n\n{why}\n\nChi tiết: {snippet}"))
                else:
                    snippet = (r.text or "")[:300]
                    self.log(f"Slink token FAIL — HTTP {r.status_code}: {snippet}\n")
                    self.q.put(("slink_test", f"Slink HTTP {r.status_code}\n\n{snippet}"))
            except Exception as e:
                self.log(f"Slink token FAIL — {e}\n")
                self.q.put(("slink_test", f"Không gọi được Slink:\n{e}"))

        self.log(f"Đang kiểm tra Slink token ({host})...\n")
        threading.Thread(target=_worker, daemon=True).start()


    def fetch_slink_categories(self):
        """Lấy danh sách category của Slink rồi cho chọn (chạy nền)."""
        token = str(self.vars["slink_api_key"].get() or "").strip()
        if not token:
            messagebox.showwarning(APP_NAME, "Chưa nhập Slink API Token.")
            return
        base = str(self.vars["slink_base_url"].get() or "https://fableon.treeiq.biz/api").strip().rstrip("/")
        host = urlsplit(base).netloc or str(self.vars["slink_site_host"].get() or "fableon.treeiq.biz")
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Bearer {token}",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
            "Referer": f"https://{host}/admin/dashboard",
            "Origin": f"https://{host}",
        }

        def _worker():
            try:
                r = requests.get(f"{base}/categories", headers=headers, timeout=45)
                if r.status_code != 200:
                    self.q.put(("slink_categories", ("error", f"HTTP {r.status_code}\n\n{(r.text or '')[:300]}")))
                    return
                try:
                    data = r.json()
                except Exception:
                    data = {}
                rows = data.get("data") if isinstance(data, dict) else None
                if rows is None and isinstance(data, dict):
                    rows = data.get("categories")
                if not isinstance(rows, list) or not rows:
                    self.q.put(("slink_categories", ("error", "Không thấy category nào trong response.")))
                    return
                out = []
                for c in rows:
                    if not isinstance(c, dict):
                        continue
                    out.append((str(c.get("id", "")),
                                str(c.get("title") or c.get("name") or c.get("slug") or ""),
                                str(c.get("slug") or "")))
                self.q.put(("slink_categories", ("ok", out)))
            except Exception as e:
                self.q.put(("slink_categories", ("error", str(e))))

        self.log(f"Đang lấy category Slink ({host})...\n")
        threading.Thread(target=_worker, daemon=True).start()


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
            _err = str(e)
            self.after(0, lambda: self._update_fail(_err))
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
            _err = str(e)
            self.after(0, lambda: self._update_fail(_err))
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
        base = str(self.vars["adsconex_base_url"].get() or "https://dramanest.gigglelo.com/api").strip().rstrip("/")
        site = str(self.vars["adsconex_site_host"].get() or "dramanest.gigglelo.com").strip().strip("/")
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
                elif r.status_code in (401, 403):
                    # 401/403 o day hau het la TOKEN BI TU CHOI (blogbio_verify_failed)
                    # hoac base URL tro sai host. Noi ro de khong doan mo.
                    snippet = (r.text or "")[:300]
                    why = ("Token bi tu choi (blogbio_verify_failed). "
                           "Kiem tra: (1) API base URL co dung host dang chay khong ("
                           + host + "), (2) token con hieu luc khong.")
                    self.log(f"Adsconex token FAIL — HTTP {r.status_code} | {snippet}\n")
                    self.q.put(("adsconex_test",
                                f"Adsconex HTTP {r.status_code}\n\n{why}\n\nChi tiet: {snippet}"))
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
        base = str(self.vars["adsconex_base_url"].get() or "https://dramanest.gigglelo.com/api").strip().rstrip("/")
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

    def show_slink_categories(self, payload):
        """Popup chọn category Slink; bấm 1 dòng là điền ID vào ô."""
        kind, data = payload
        if kind == "error":
            messagebox.showerror("Slink categories", data)
            return
        win = tk.Toplevel(self)
        win.title("Chọn category — Slink")
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
            self.vars["slink_category"].set(cid)
            self.log(f"Đã chọn Slink category ID = {cid}\n")
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
                elif item[0] == "slink_categories":
                    self.show_slink_categories(item[1])
                elif item[0] == "slink_test":
                    if "OK" in item[1]:
                        messagebox.showinfo("Slink token", item[1])
                    else:
                        messagebox.showerror("Slink token", item[1])
                elif item[0] == "adsconex_categories":
                    self.show_adsconex_categories(item[1])
                elif item[0] == "adsconex_republish_done":
                    ok, fail = item[1]
                    self.run_btn.config(state="normal")
                    if fail:
                        messagebox.showwarning(
                            "Đăng lại bài lỗi",
                            f"Đã đăng lại thành công {ok} bài.\nCòn {fail} bài vẫn lỗi "
                            "(origin chưa hồi) — bấm lại nút sau vài phút."
                        )
                    else:
                        messagebox.showinfo("Đăng lại bài lỗi", f"Đã đăng lại thành công {ok} bài.")
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


    def republish_failed_posts(self):
        """Quet folder dang chon: dang lai moi bai da luu payload (502) - khong ton Vision/Writer.

        Chay theo net dang chon: SmartTraffic, Adsconex hoac Slink.
        """
        p1 = self.input_path.get().strip()
        folder = Path(p1) if p1 and Path(p1).is_dir() else None
        if folder is None:
            messagebox.showinfo(APP_NAME, "Chọn folder batch vào ô đường dẫn trước, rồi bấm lại.")
            return
        cfg = self.current_cfg()
        provider = str(cfg.get("net_provider") or "").strip().lower()
        is_ads = provider.startswith("ads")
        is_slink = provider.startswith("slink")
        output_root = folder / "Story Outputs"

        # Nhat CA BA net: Adsconex/Slink va SmartTraffic deu luu payload bai loi 5xx.
        pending_ads = []
        pending_slink = []
        pending_st = []
        if is_ads:
            if not str(cfg.get("adsconex_api_key") or "").strip():
                messagebox.showwarning(APP_NAME, "Chưa nhập Adsconex API Token trong tab Adsconex.")
                return
            pending_ads = find_pending_adsconex(output_root, cfg)
        elif is_slink:
            if not str(cfg.get("slink_api_key") or "").strip():
                messagebox.showwarning(APP_NAME, "Chưa nhập Slink API Token trong tab Slink.")
                return
            pending_slink = find_pending_slink(output_root, cfg)
        else:
            if not str(cfg.get("smarttraffic_api_key") or "").strip():
                messagebox.showwarning(APP_NAME, "Chưa nhập SmartTraffic API Key trong tab SmartTraffic.")
                return
            pending_st = find_pending_smarttraffic(output_root)

        total = len(pending_ads) + len(pending_slink) + len(pending_st)
        if not total:
            messagebox.showinfo(APP_NAME, f"Không có bài lỗi nào cần đăng lại trong:\n{output_root}")
            return
        net_name = ("Slink" if is_slink else ("Adsconex" if is_ads else "SmartTraffic"))
        if not messagebox.askyesno(
            APP_NAME,
            f"Tìm thấy {total} bài lỗi đã lưu payload trong:\n{output_root}\n\n"
            f"(net đang chọn: {net_name})\n\n"
            "Đăng lại ngay? (không tốn Vision/Writer, video tự đổi tên nếu đăng được)"
        ):
            return
        self.log(f"DANG LAI BAI LOI ({net_name}): {total} bai trong {output_root}")
        self.run_btn.config(state="disabled")

        def worker():
            ok = 0
            fail = 0
            retry_cfg = dict(cfg)
            retry_cfg["story_output_root"] = str(output_root)
            pipe = StoryPipeline(retry_cfg, self.log, self.progress)
            if is_ads:
                jobs = [(wd, pl, rs) for wd, pl, rs in pending_ads]
            elif is_slink:
                jobs = [(wd, pl, None) for wd, pl in pending_slink]
            else:
                jobs = [(wd, pl, None) for wd, pl in pending_st]
            for work_dir, payload, resume in jobs:
                try:
                    if is_ads:
                        res = pipe.republish_pending(work_dir, payload, resume)
                    elif is_slink:
                        res = pipe.republish_pending_slink(work_dir, payload)
                    else:
                        res = pipe.republish_pending_smarttraffic(work_dir, payload)
                except Exception as exc:
                    res = {}
                    self.log(f"[{work_dir.name}] LOI: {exc}")
                if res:
                    ok += 1
                    link = (res.get("publish_result") or {}).get("url", "")
                    extra = " | doi ten video OK" if res.get("renamed_video") else ""
                    self.log(f"[{work_dir.name}] DA DANG LAI: {link}{extra}")
                else:
                    fail += 1
            self.log(f"DANG LAI XONG: thanh cong {ok} | con loi {fail} "
                     f"(bam lai nut nay sau vai phut de thu tiep)")
            self.q.put(("adsconex_republish_done", (ok, fail)))

        threading.Thread(target=worker, daemon=True).start()

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
                        cfg.get("adsconex_site_host", ""),
                        cfg.get("slink_site_host", ""))
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
                            if pipeline:
                                pipeline.cleanup_frames()
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
                    # ---- Dang lai cac bai 502 da luu payload (cuoi batch, origin da hoi) ----
                    # Khong ton Vision/Writer: chi gui lai dung payload da luu.
                    # Nhat CA BA net: Adsconex, Slink + SmartTraffic (moi net co file pending rieng).
                    _prov = str(cfg.get("net_provider") or "").strip().lower()
                    _use_ads = _prov.startswith("ads")
                    _use_slink = _prov.startswith("slink")
                    try:
                        if _use_ads:
                            pending = [(wd, pl, rs) for wd, pl, rs in find_pending_adsconex(output_root, cfg)]
                        elif _use_slink:
                            pending = [(wd, pl, None) for wd, pl in find_pending_slink(output_root, cfg)]
                        else:
                            pending = [(wd, pl, None) for wd, pl in find_pending_smarttraffic(output_root)]
                    except Exception:
                        pending = []
                    if pending:
                        self.log(f"BATCH: dang lai {len(pending)} bai loi da luu payload "
                                 f"({'Slink' if _use_slink else ('Adsconex' if _use_ads else 'SmartTraffic')}, khong ton Vision/Writer)...")
                        retry_cfg = dict(cfg)
                        retry_cfg["story_output_root"] = str(output_root)
                        retry_pipe = StoryPipeline(retry_cfg, self.log, lambda *args: None)
                        fixed = 0
                        stuck = 0
                        for work_dir, payload, resume in pending:
                            try:
                                if _use_ads:
                                    res = retry_pipe.republish_pending(work_dir, payload, resume)
                                elif _use_slink:
                                    res = retry_pipe.republish_pending_slink(work_dir, payload)
                                else:
                                    res = retry_pipe.republish_pending_smarttraffic(work_dir, payload)
                            except Exception as exc:
                                self.log(f"[{work_dir.name}] Dang lai loi: {exc}")
                                res = {}
                            if not res:
                                stuck += 1
                                # Origin van sap: 3 bai lien tiep khong dang duoc thi dung
                                # som, khong ngoi cho het danh sach (moi bai mat ~10s luot).
                                if stuck >= 3:
                                    self.log(
                                        f"BATCH: origin van chua hoi ({stuck} bai lien tiep loi) "
                                        f"-> dung dang lai, con {len(pending) - fixed - stuck} bai giu nguyen payload."
                                    )
                                    break
                                continue
                            stuck = 0
                            fixed += 1
                            link = (res.get("publish_result") or {}).get("url", "")
                            title = compact_meta_text((res.get("story") or {}).get("title") or "", 240)
                            row_done = False
                            for row in rows:
                                if row.get("status") == "failed" and \
                                        os.path.normcase(str(row.get("output") or "")) == \
                                        os.path.normcase(str(work_dir)):
                                    row["status"] = "success"
                                    row["error"] = ""
                                    row["renamed_video"] = res.get("renamed_video", "")
                                    row["article_url"] = link
                                    row["net_title"] = title
                                    row_done = True
                                    break
                            if not row_done:
                                rows.append({"video": work_dir.name, "status": "success",
                                             "renamed_video": res.get("renamed_video", ""),
                                             "article_url": link, "net_title": title,
                                             "output": str(work_dir)})
                        self.log(f"BATCH: dang lai xong - thanh cong {fixed}/{len(pending)} bai loi.")
                        self.q.put(("batch_stats", sum(r['status']=='success' for r in rows),
                                    sum(r['status']=='failed' for r in rows)))
                        self.q.put(("progress", 100.0,
                                    f"Da dang lai {fixed}/{len(pending)} bai loi"))
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
                try:
                    result = pipe.run(sources, auto_publish=bool(cfg.get("auto_publish")))
                    result["_pipeline"] = pipe
                    self.q.put(("done", result))
                finally:
                    pipe.cleanup_frames()
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
