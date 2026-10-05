# -*- coding: utf-8 -*-
"""Sync Desktop -> Z (chi file code, KHONG copy config/cache) + doi chieu MD5."""
import os, shutil, hashlib
from pathlib import Path

SRC = Path(r"C:\Users\Admin\Desktop\Phong_DHue")
DST = Path(r"Z:\HQData-2\TOOLS TỔNG HỢP\TOOLS UPDATE CUỐI\Phong_DHue")

SKIP_DIRS = {"output", "__pycache__", "_backup_update", ".git", "models"}


def _skip(fn: str) -> bool:
    """Bo config + ban sao luu + file tam. Config nam o thu muc user nen
    binh thuong khong lot len day, nhung chan luon cho chac."""
    if fn == "config.json" or fn.startswith("config.json."):
        return True
    return fn.endswith((".log", ".bak", ".part", ".tmp", ".pyc"))


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


copied = []
for root, dirs, files in os.walk(SRC):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    rel = Path(root).relative_to(SRC)
    for fn in files:
        if _skip(fn):
            continue
        s = Path(root) / fn
        d = DST / rel / fn
        d.parent.mkdir(parents=True, exist_ok=True)
        if not d.exists() or md5(s) != md5(d):
            shutil.copy2(s, d)
            copied.append(str(rel / fn))

print("Da copy %d file:" % len(copied))
for c in copied:
    print("  ", c)

print("\n=== DOI CHIEU MD5 ===")
ok = bad = 0
for root, dirs, files in os.walk(SRC):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    rel = Path(root).relative_to(SRC)
    for fn in files:
        if _skip(fn):
            continue
        s = Path(root) / fn
        d = DST / rel / fn
        if not d.exists():
            print("  THIEU:", rel / fn); bad += 1; continue
        if md5(s) == md5(d):
            ok += 1
        else:
            print("  LECH :", rel / fn); bad += 1
print("\n==== %d/%d MD5 KHOP ====" % (ok, ok + bad))
