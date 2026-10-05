# -*- coding: utf-8 -*-
"""Phát hành bản mới lên GitHub bằng 1 lệnh.

    python release.py 22.100.0 "Mô tả ngắn thay đổi"
    python release.py 22.100.0            # tự lấy mô tả từ commit gần nhất

Việc script làm:
    1. Kiểm tra version mới > version đang có (tránh lùi bản)
    2. Ghi version.json (version + notes + date)
    3. Ghi APP_VERSION trong viet_drama_V23.6_dashboard_thumbnail.py
    4. commit + push lên GitHub

Sau đó máy khác chỉ cần bấm nút "⬆ Cập nhật" trong tool.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
MAIN = BASE / "viet_drama_V23.6_dashboard_thumbnail.py"
VER_F = BASE / "version.json"
REPO = "NaupUuh/Phong_DHue"
GH = r"C:\Users\Admin\AppData\Local\ghcli\bin\gh.exe"
USER = "NaupUuh"
EMAIL = "NaupUuh@users.noreply.github.com"


def run(cmd, **kw):
    r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def cur_version() -> str:
    m = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']',
                  MAIN.read_text(encoding="utf-8"), re.MULTILINE)
    return m.group(1) if m else "0.0.0"


def vt(v):
    return tuple(int(x) for x in (re.findall(r"\d+", str(v)) or ["0"])[:4])


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    new = sys.argv[1].strip()
    if not re.match(r"^\d+(\.\d+)*$", new):
        print(f"Version không hợp lệ: {new!r} (phải dạng 22.100.0)")
        return 2

    old = cur_version()
    if vt(new) <= vt(old):
        print(f"Version mới ({new}) phải LỚN HƠN bản hiện tại ({old}).")
        return 2

    notes = " ".join(sys.argv[2:]).strip()
    if not notes:
        _, last = run(["git", "log", "-1", "--pretty=%s"])
        notes = last.strip() or f"Cập nhật v{new}"
        notes = re.sub(r"^v?[\d.]+\s*[:\-–]?\s*", "", notes) or f"Cập nhật v{new}"

    print(f"Phát hành v{old} -> v{new}")
    print(f"  mô tả: {notes}")

    VER_F.write_text(json.dumps(
        {"version": new, "notes": notes, "date": time.strftime("%Y-%m-%d")},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    txt = MAIN.read_text(encoding="utf-8")
    txt2 = re.sub(r'^(APP_VERSION\s*=\s*)["\'][^"\']+["\']',
                  lambda m: f'{m.group(1)}"{new}"', txt, count=1, flags=re.MULTILINE)
    if txt2 == txt:
        print("LỖI: không sửa được APP_VERSION trong file chính")
        return 1
    MAIN.write_text(txt2, encoding="utf-8")
    print("  đã ghi version.json + APP_VERSION")

    for cmd in (["git", "add", "-A"],
                ["git", "-c", f"user.name={USER}", "-c", f"user.email={EMAIL}",
                 "commit", "-m", f"v{new}: {notes}"],
                ["git", "push"]):
        rc, out = run(cmd)
        if rc != 0 and "nothing to commit" not in out:
            print(f"LỖI ở {' '.join(cmd[:2])}: {out.strip()[:400]}")
            return 1

    print(f"\nĐÃ PHÁT HÀNH v{new}.")
    print("Máy khác: mở tool -> bấm '⬆ Cập nhật' là xong.")
    print(f"Repo: https://github.com/{REPO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
