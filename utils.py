# -*- coding: utf-8 -*-
"""Các hàm tiện ích dùng chung."""
import unicodedata
import re

def _strip_accents(s: str) -> str:
    """Bỏ dấu tiếng Việt."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def canon_id(s: str) -> str:
    """
    Chuẩn hoá ID/QR về dạng so khớp (dùng cho logic):
    - Bỏ dấu, Uppercase
    - Bỏ mọi ký tự không phải A-Z/0-9
    - Bỏ tiền tố LOAI / LO ở ĐẦU chuỗi
    """
    if s is None: return ""
    s = str(s).strip()
    try: s = s.encode("utf-8").decode("unicode_escape")
    except Exception: pass
    s = _strip_accents(s).upper()
    s = re.sub(r"[^A-Z0-9]", "", s)  # Chỉ giữ lại A-Z, 0-9
    s = re.sub(r"^(LOAI|LO)+", "", s) # Bỏ prefix LOAI/LO
    return s
