"""
services/excel_parser.py
────────────────────────
Parse file Excel xuất từ Minh Lộ HIS (BVST).
Fix lỗi backslash trong zip + merge nhiều dòng vật lý thành 1 bệnh nhân.
"""

import re, io, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
def _t(n): return "{" + _NS + "}" + n


def _col_to_num(col_str: str) -> int:
    n = 0
    for ch in col_str.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _read_xlsx_grid(raw: bytes) -> tuple[dict, int, int]:
    """
    Đọc file xlsx thành grid {(row, col): value}.
    Tương thích với file Minh Lộ dùng backslash trong tên file zip.
    """
    zf = zipfile.ZipFile(io.BytesIO(raw))
    # Fix: chuẩn hoá backslash → forward slash để so khớp đúng
    names_lower = {n.lower().replace("\\", "/"): n for n in zf.namelist()}

    # SharedStrings
    shared: list[str] = []
    ss_key = next((k for k in names_lower if "sharedstrings" in k), None)
    if ss_key:
        tree = ET.parse(zf.open(names_lower[ss_key]))
        for si in tree.findall(".//" + _t("si")):
            texts = [t.text or "" for t in si.findall(".//" + _t("t"))]
            shared.append("".join(texts))

    # Worksheet sheet1
    ws_key = next((k for k in names_lower if "worksheets/sheet1" in k), None)
    if not ws_key:
        raise ValueError("Không tìm thấy worksheet trong file xlsx.")

    ws_tree = ET.parse(zf.open(names_lower[ws_key]))
    grid: dict[tuple[int, int], str] = {}

    for row_el in ws_tree.findall(".//" + _t("row")):
        r = int(row_el.get("r", 0))
        for cell in row_el.findall(_t("c")):
            ref  = cell.get("r", "")
            col_str = "".join(ch for ch in ref if ch.isalpha())
            col_n = _col_to_num(col_str)
            cell_type = cell.get("t", "")
            v_el = cell.find(_t("v"))
            if v_el is None:
                val = ""
            elif cell_type == "s":
                idx = int(v_el.text or 0)
                val = shared[idx] if idx < len(shared) else ""
            elif cell_type == "inlineStr":
                is_el = cell.find(_t("is"))
                val   = (is_el.findtext(_t("t"), "") if is_el is not None else "")
            else:
                val = v_el.text or ""
            grid[(r, col_n)] = val

    max_row = max((r for r, _ in grid), default=0)
    max_col = max((c for _, c in grid), default=0)
    return grid, max_row, max_col


def _to_date(raw: str) -> str:
    """Chuyển serial Excel hoặc chuỗi ngày → dd/mm/yyyy."""
    raw = str(raw).strip()
    try:
        s = float(raw)
        if 40000 < s < 60000:
            from datetime import timedelta
            epoch = datetime(1899, 12, 30)
            d = epoch + timedelta(days=int(s))
            return d.strftime("%d/%m/%Y")
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return raw


def parse_minh_lo_excel(file_bytes: bytes) -> tuple[list[dict], str | None]:
    """
    Parse file Excel Minh Lộ → danh sách dict bệnh nhân.
    Trả về (records, error_message).
    """
    try:
        grid, max_row, max_col = _read_xlsx_grid(file_bytes)
    except ValueError as e:
        return [], str(e)
    except Exception as e:
        return [], f"Lỗi đọc file: {e}"

    def get_row(r: int) -> list[str]:
        return [grid.get((r, c), "") for c in range(1, max_col + 1)]

    # ── Tìm dòng header (chứa "Họ tên" hoặc "Mã y tế") ──────────────────────
    header_row_idx = None
    headers: list[str] = []
    COL_MAP: dict[str, int] = {}

    FIELD_ALIASES = {
        "stt":         ["stt"],
        "ma_yt":       ["mã y tế", "ma y te"],
        "ho_ten":      ["họ tên", "ho ten", "bệnh nhân"],
        "tuoi_nam":    ["nam"],
        "tuoi_nu":     ["nữ", "nu"],
        "dia_chi":     ["địa chỉ", "dia chi"],
        "bhyt":        ["bhyt", "số thẻ"],
        "bac_sy":      ["bác sỹ khám", "bac sy kham"],
        "trieu_chung": ["triệu chứng", "trieu chung"],
        "chan_doan":   ["chẩn đoán", "chan doan"],
        "ngay_hen":    ["ngày hẹn", "ngay hen"],
        "dt":          ["điện thoại", "dien thoai"],
        "khoa_hen":    ["khoa hẹn", "khoa hen"],
        "bac_sy_hen":  ["bác sỹ hẹn"],
        "da_kham":     ["đã khám", "da kham"],
    }

    for r in range(1, min(15, max_row + 1)):
        row = get_row(r)
        row_lower = [str(v).lower().strip() for v in row]
        matched = sum(1 for v in row_lower if any(a in v for aliases in FIELD_ALIASES.values() for a in aliases))
        if matched >= 3:
            header_row_idx = r
            headers = row_lower
            for field, aliases in FIELD_ALIASES.items():
                for i, h in enumerate(headers):
                    if any(a in h for a in aliases):
                        COL_MAP[field] = i
                        break
            break

    if header_row_idx is None:
        return [], "Không nhận dạng được dòng tiêu đề trong file Excel."

    def cv(row: list[str], field: str) -> str:
        idx = COL_MAP.get(field)
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx]).strip()

    def has_letter(s: str) -> bool:
        return bool(re.search(r"[^\W\d_]", s, re.UNICODE))

    # ── Phát hiện dòng đầu của mỗi bệnh nhân ────────────────────────────────
    stt_col = COL_MAP.get("stt", 0)

    start_rows: list[int] = []
    for r in range(header_row_idx + 1, max_row + 1):
        row_vals = get_row(r)
        stt_val  = str(row_vals[stt_col]).strip() if stt_col < len(row_vals) else ""
        ho_ten   = cv(row_vals, "ho_ten")
        if stt_val.isdigit() and has_letter(ho_ten):
            start_rows.append(r)

    # Fallback: mỗi dòng không rỗng là 1 bệnh nhân
    if not start_rows:
        start_rows = [r for r in range(header_row_idx + 1, max_row + 1)
                      if any(str(v).strip() for v in get_row(r))]

    def join_field(r0: int, r1: int, field: str) -> str:
        """Ghép giá trị field từ nhiều dòng vật lý của cùng 1 bệnh nhân."""
        return "".join(cv(get_row(r), field) for r in range(r0, r1 + 1)).strip()

    records = []
    for i, r0 in enumerate(start_rows):
        r1     = (start_rows[i + 1] - 1) if i + 1 < len(start_rows) else max_row
        vals0  = get_row(r0)
        ho_ten = join_field(r0, r1, "ho_ten")
        ma_yt  = cv(vals0, "ma_yt")

        if not ho_ten and not ma_yt:
            continue

        records.append({
            "MÃ Y TẾ":   ma_yt,
            "HỌ TÊN":    ho_ten,
            "ĐỊA CHỈ":   join_field(r0, r1, "dia_chi"),
            "NGÀY HẸN":  _to_date(cv(vals0, "ngay_hen")),
            "SỐ ĐIỆN THOẠI": cv(vals0, "dt") or "N/A",
            "KHOA HẸN":  cv(vals0, "khoa_hen"),
        })

    return records, None
