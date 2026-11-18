"""services/regions.py — tách XÃ/PHƯỜNG từ địa chỉ chữ tự do để lọc bệnh nhân theo vùng.

Địa chỉ trên Sheet/Form thường dạng:  «Xã Trung Chính, , Tỉnh Thanh Hóa»,  «Phú Quý Xã Trung Chính, , Tỉnh Thanh Hóa»,
«Thôn 3, Xã Minh Nghĩa, Huyện Nông Cống». Lấy cụm «Xã|Phường|Thị trấn + tên» đầu tiên làm xã; phần đứng trước là thôn/xóm.
Không tách được → '' (hiện là «Chưa rõ xã» để nhân viên sửa tay ở trang Sửa bệnh nhân)."""
from __future__ import annotations

import re
import unicodedata

UNKNOWN = "Chưa rõ xã"
_RE = re.compile(r"(?<![\wÀ-ỹ])(Xã|Phường|Thị\s+trấn|TT\.?)\s+([^,;\n]+)", re.IGNORECASE)
_CUT = re.compile(r"\s+(Huyện|Tỉnh|Thị\s+xã|Thành\s+phố|TP\.?|Quận)\b.*$", re.IGNORECASE)
_PREFIX = {"xã": "Xã", "phường": "Phường", "thị trấn": "Thị trấn", "tt": "Thị trấn", "tt.": "Thị trấn"}


def extract_commune(address) -> tuple[str, str]:
    """→ (xã/phường chuẩn hoá, thôn/xóm). Không tách được → ('', '')."""
    text = re.sub(r"\s+", " ", str(address or "")).strip()
    m = _RE.search(text)
    if not m:
        return "", ""
    kind = _PREFIX.get(re.sub(r"\s+", " ", m.group(1).lower()), "Xã")
    name = _CUT.sub("", m.group(2)).strip(" .-")
    if not name or name.upper() in ("N/A", "NA"):
        return "", ""
    name = " ".join(w.capitalize() for w in name.split())
    village = text[:m.start()].strip(" ,;-")
    return f"{kind} {name}", village


def commune_key(s) -> str:
    """Khoá so sánh không phân biệt dấu/hoa-thường: 'Xã Trung Chính' == 'xa trung chinh'."""
    t = unicodedata.normalize("NFD", str(s or "").replace("đ", "d").replace("Đ", "D"))
    return re.sub(r"\s+", " ", "".join(c for c in t if unicodedata.category(c) != "Mn")).strip().lower()


def commune_options(communes) -> list[tuple[str, int]]:
    """[(xã, số bệnh nhân)] nhiều nhất trước; '' gộp thành «Chưa rõ xã»."""
    count: dict[str, int] = {}
    for c in communes:
        c = c or UNKNOWN
        count[c] = count.get(c, 0) + 1
    return sorted(count.items(), key=lambda t: (t[0] == UNKNOWN, -t[1], t[0]))
