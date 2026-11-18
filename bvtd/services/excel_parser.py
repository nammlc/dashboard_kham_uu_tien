"""
services/excel_parser.py
────────────────────────
Parse file Excel 04-4 "Danh sách bệnh nhân hẹn khám lại" xuất từ Minh Lộ HIS.
Port y hệt từ bản Streamlit (_load_xlsx_grid + parse_minh_lo_excel):
  - đọc thẳng XML trong zip (file Minh Lộ dùng '\\' trong tên đường dẫn zip,
    có thể không có sharedStrings.xml)
  - tự fill giá trị cho ô gộp (merge cells)
  - 1 bệnh nhân trải trên NHIỀU dòng vật lý → gộp lại thành 1 bản ghi
"""

import re, io, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _tag(name: str) -> str:
    return "{" + _NS + "}" + name


def _col_num(s: str) -> int:
    n = 0
    for ch in "".join(c for c in s if c.isalpha()).upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _row_num(s: str) -> int:
    return int("".join(c for c in s if c.isdigit()))


def _load_xlsx_grid(raw: bytes):
    """Trả về (grid, max_row, max_col, get_row, error_or_None)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        names_lower = {n.lower().replace("\\", "/"): n for n in zf.namelist()}

        shared: list[str] = []
        ss_key = next((k for k in names_lower if "sharedstrings" in k), None)
        if ss_key:
            with zf.open(names_lower[ss_key]) as f:
                tree = ET.parse(f)
            for si in tree.findall(".//" + _tag("si")):
                shared.append("".join(t.text or "" for t in si.findall(".//" + _tag("t"))))

        ws_key = next((k for k in names_lower
                       if "worksheets/sheet1" in k or "worksheet/sheet1" in k), None)
        if ws_key is None:
            ws_key = next((k for k in names_lower if "worksheets/sheet" in k), None)
        if ws_key is None:
            return {}, 0, 0, None, "Không tìm thấy worksheet trong file xlsx."

        with zf.open(names_lower[ws_key]) as f:
            ws_tree = ET.parse(f)

        # Ô gộp → map {(r,c): (r_gốc,c_gốc)}
        merge_fill = {}
        mc_el = ws_tree.find(".//" + _tag("mergeCells"))
        if mc_el is not None:
            for mc in mc_el.findall(_tag("mergeCell")):
                ref = mc.get("ref", "")
                if ":" not in ref:
                    continue
                p1, p2 = ref.split(":")
                r1, c1, r2, c2 = _row_num(p1), _col_num(p1), _row_num(p2), _col_num(p2)
                for r in range(r1, r2 + 1):
                    for c in range(c1, c2 + 1):
                        if r != r1 or c != c1:
                            merge_fill[(r, c)] = (r1, c1)

        grid: dict[tuple[int, int], str] = {}
        for row_el in ws_tree.findall(".//" + _tag("row")):
            r = int(row_el.get("r", 0))
            for cell_el in row_el.findall(_tag("c")):
                col_n = _col_num(cell_el.get("r", ""))
                t = cell_el.get("t", "")
                v_el = cell_el.find(_tag("v"))
                if v_el is None:
                    # inlineStr nằm trong <is>, không nằm trong <v>
                    if t == "inlineStr":
                        is_el = cell_el.find(_tag("is"))
                        val = "".join(x.text or "" for x in is_el.iter(_tag("t"))) if is_el is not None else ""
                    else:
                        val = ""
                elif t == "s":
                    i = int(v_el.text or 0)
                    val = shared[i] if i < len(shared) else ""
                else:
                    val = v_el.text or ""
                grid[(r, col_n)] = val

        for (r, c), (sr, sc) in merge_fill.items():
            if (sr, sc) in grid and (r, c) not in grid:
                grid[(r, c)] = grid[(sr, sc)]

        max_row = max((r for r, _ in grid), default=0)
        max_col = max((c for _, c in grid), default=0)

        def get_row(r):
            return [grid.get((r, c), "") for c in range(1, max_col + 1)]

        return grid, max_row, max_col, get_row, None

    except Exception as e:
        return {}, 0, 0, None, f"Lỗi đọc file: {type(e).__name__}: {e}"


def fix_phone(raw) -> str:
    """Chuẩn hoá SĐT: bỏ khoảng trắng/chấm/gạch; 9 số (Excel mất số 0 đầu) → thêm '0'."""
    phone = re.sub(r"[\s.\-]", "", str(raw).strip())
    if not phone or phone.lower() in ("n/a", "none", ""):
        return "N/A"
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 9:
        digits = "0" + digits
    return digits if digits else "N/A"


def _to_date(val) -> str:
    val = str(val).strip()
    if not val or val == "None":
        return ""
    m = re.search(r"\d{1,2}/\d{1,2}/\d{4}", val)
    if m:
        return m.group()
    if re.match(r"\d{4}-\d{2}-\d{2}", val):
        return datetime.strptime(val[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    try:                                   # số serial Excel
        s = float(val)
        if 40000 < s < 60000:
            return (datetime(1899, 12, 30) + timedelta(days=int(s))).strftime("%d/%m/%Y")
    except Exception:
        pass
    return val


def parse_minh_lo_excel(file_bytes: bytes, current_year: int | None = None):
    """
    Parse file 04-4 → (list[dict], error_or_None).
    Mỗi dict có: MÃ Y TẾ, HỌ TÊN, TUỔI, NĂM SINH (ước tính), GIỚI TÍNH, ĐỊA CHỈ, SỐ BHYT,
    BÁC SĨ KHÁM, TRIỆU CHỨNG, CHẨN ĐOÁN, NGÀY LẬP, NGÀY HẸN, GIỜ HẸN, SỐ ĐIỆN THOẠI,
    KHOA HẸN, BÁC SĨ HẸN, ĐÃ KHÁM.
    """
    current_year = current_year or datetime.now().year
    grid, max_row, max_col, get_row, err = _load_xlsx_grid(file_bytes)
    if err:
        return [], err

    try:
        # ── Dòng tiêu đề: có "STT" và "Mã y tế" ─────────────────────────────
        header_row_idx = None
        for r in range(1, min(max_row + 1, 15)):
            row_vals = [str(v).strip() for v in get_row(r)]
            has_stt = "STT" in row_vals
            has_ma = any("m" in v.lower() and "y t" in v.lower() for v in row_vals)
            if has_stt and has_ma:
                header_row_idx = r
                break
        if header_row_idx is None:
            return [], "Không tìm thấy hàng tiêu đề trong file. Kiểm tra đúng loại báo cáo Minh Lộ (file 04-4)."

        headers = [str(v).strip().replace("\n", " ").lower() for v in get_row(header_row_idx)]

        def find_col(keywords):
            for i, h in enumerate(headers):
                if any(k.lower() in h for k in keywords):
                    return i
            return None

        # Cột "Tuổi" gồm 2 cột con Nam / Nữ nằm ở DÒNG PHỤ ngay dưới tiêu đề
        sub_idx = header_row_idx + 1
        sub_vals = [str(v).strip().lower() for v in get_row(sub_idx)] if sub_idx <= max_row else []
        tuoi_nam = next((i for i, v in enumerate(sub_vals) if v == "nam"), None)
        tuoi_nu = next((i for i, v in enumerate(sub_vals) if v in ("nữ", "nu")), None)
        if tuoi_nam is None and tuoi_nu is None:
            tuoi_nam = find_col(["tuổi nam", "nam"])
            tuoi_nu = find_col(["tuổi nữ", "nữ", "nu"])

        idx = {
            "ma_yt":       find_col(["mã y tế", "ma y te"]),
            "ho_ten":      find_col(["họ tên", "ho ten", "bệnh nhân"]),
            "tuoi_nam":    tuoi_nam,
            "tuoi_nu":     tuoi_nu,
            "dia_chi":     find_col(["địa chỉ", "dia chi"]),
            "bhyt":        find_col(["bhyt"]),
            "bac_sy":      find_col(["bác sỹ khám", "bac sy kham", "bác sĩ khám"]),
            "trieu_chung": find_col(["triệu chứng", "trieu chung"]),
            "chan_doan":   find_col(["chẩn đoán", "chan doan"]),
            "ngay_hen":    find_col(["ngày hẹn", "ngay hen"]),
            "ngay_lap":    find_col(["ngày lập", "ngay lap"]),
            "dt":          find_col(["điện thoại", "dien thoai"]),
            "khoa_hen":    find_col(["khoa hẹn", "khoa hen"]),
            "bac_sy_hen":  find_col(["bác sỹ hẹn", "bac sy hen", "bác sĩ hẹn"]),
            "da_kham":     find_col(["đã khám", "da kham"]),
        }

        def cv(row_vals, key):
            i = idx.get(key)
            if i is None or i >= len(row_vals):
                return ""
            return str(row_vals[i]).strip()

        stt_col = headers.index("stt") if "stt" in headers else 0

        def has_letter(s):
            return bool(re.search(r"[^\W\d_]", str(s), re.UNICODE))

        # ── Dòng bắt đầu của mỗi bệnh nhân: STT là số + Họ tên có chữ ────────
        start_rows = []
        for r in range(header_row_idx + 1, max_row + 1):
            row_vals = get_row(r)
            stt_val = str(row_vals[stt_col]).strip() if stt_col < len(row_vals) else ""
            if stt_val.isdigit() and has_letter(cv(row_vals, "ho_ten")):
                start_rows.append(r)

        if not start_rows:                       # layout lạ: mỗi dòng không rỗng = 1 BN
            start_rows = [r for r in range(header_row_idx + 1, max_row + 1)
                          if any(str(v).strip() for v in get_row(r))]

        def join_field(r0, r1, key):
            parts = [cv(get_row(r), key).strip() for r in range(r0, r1 + 1)]
            return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()

        rows = []
        for i, r0 in enumerate(start_rows):
            r1 = (start_rows[i + 1] - 1) if i + 1 < len(start_rows) else max_row
            vals0 = get_row(r0)

            ma_yt = cv(vals0, "ma_yt")
            ho_ten = join_field(r0, r1, "ho_ten")
            dia_chi = join_field(r0, r1, "dia_chi")
            if not ma_yt and not ho_ten:
                continue

            ngay_raw = cv(vals0, "ngay_hen")
            gio_hen = ""
            try:
                s = float(ngay_raw)
                if 40000 < s < 60000:
                    frac = s - int(s)
                    if frac > 0.0:
                        sec = int(frac * 86400)
                        gio_hen = f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}"
            except Exception:
                pass

            tuoi_val = cv(vals0, "tuoi_nam") or cv(vals0, "tuoi_nu")
            gioi_tinh = "Nam" if cv(vals0, "tuoi_nam") else ("Nữ" if cv(vals0, "tuoi_nu") else "")

            nam_sinh = ""
            if tuoi_val:
                nums = re.findall(r"\d+", tuoi_val)
                if nums:
                    n = int(nums[0])
                    if "tháng" in tuoi_val.lower():          # trẻ nhỏ tính theo tháng
                        if 0 <= n < 1200 and 0 <= n // 12 < 120:
                            nam_sinh = str(current_year - n // 12)
                    elif 0 < n < 120:
                        nam_sinh = str(current_year - n)

            rows.append({
                "MÃ Y TẾ":             ma_yt,
                "HỌ TÊN":              ho_ten,
                "TUỔI":                tuoi_val,
                "NĂM SINH (ước tính)": nam_sinh,
                "GIỚI TÍNH":           gioi_tinh,
                "ĐỊA CHỈ":             dia_chi,
                "SỐ BHYT":             cv(vals0, "bhyt"),
                "BÁC SĨ KHÁM":         cv(vals0, "bac_sy"),
                "TRIỆU CHỨNG":         cv(vals0, "trieu_chung"),
                "CHẨN ĐOÁN":           cv(vals0, "chan_doan"),
                "NGÀY LẬP":            _to_date(cv(vals0, "ngay_lap")),
                "NGÀY HẸN":            _to_date(ngay_raw),
                "GIỜ HẸN":             gio_hen,
                "SỐ ĐIỆN THOẠI":       fix_phone(cv(vals0, "dt")),
                "KHOA HẸN":            cv(vals0, "khoa_hen"),
                "BÁC SĨ HẸN":          cv(vals0, "bac_sy_hen"),
                "ĐÃ KHÁM":             cv(vals0, "da_kham"),
            })

        return rows, None

    except Exception as e:
        return [], f"Lỗi đọc file: {type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# NHẬT KÝ KHÁM THỰC TẾ (file 01-1 "Báo cáo ĐK Khám Chữa Bệnh" của Minh Lộ)
# Dùng cho chức năng Đối Chiếu Tái Khám — port từ Streamlit.
# ══════════════════════════════════════════════════════════════════════════════

def _serial_to_date_str(val):
    try:
        s = float(val)
        if 20000 < s < 60000:
            return (datetime(1899, 12, 30) + timedelta(days=int(s))).strftime("%d/%m/%Y")
    except Exception:
        pass
    return None


def parse_minhlo_date(val):
    """Ô ngày Minh Lộ → ('dd/mm/yyyy' | '', đọc_được: bool).
    Chấp nhận số serial Excel lẫn chữ (dd/mm/yyyy, d/m/yyyy, dd-mm-yyyy, yyyy-mm-dd,
    có hoặc không kèm giờ). Ô trống → ('', True) (không phải lỗi)."""
    raw = str(val).strip()
    if not raw:
        return "", True
    serial = _serial_to_date_str(raw)
    if serial:
        return serial, True
    date_part = re.split(r"\s+", raw, maxsplit=1)[0]
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(date_part, fmt).strftime("%d/%m/%Y"), True
        except Exception:
            continue
    return "", False


def parse_minh_lo_visit_log(file_bytes: bytes):
    """Đọc nhật ký bệnh nhân THỰC TẾ đến khám.
    Trả về (records, error | None, warning | None)."""
    grid, max_row, max_col, get_row, err = _load_xlsx_grid(file_bytes)
    if err:
        return [], err, None
    try:
        header_row_idx = None
        for r in range(1, min(max_row + 1, 15)):
            row_vals = [str(v).strip().lower() for v in get_row(r)]
            has_ma_bn = any("mã bn" in v or "ma bn" in v for v in row_vals)
            has_ngay_dk = any(v in ("ngày đk", "ngay dk") for v in row_vals)
            if has_ma_bn and has_ngay_dk:
                header_row_idx = r
                break
        if header_row_idx is None:
            return [], ("Không tìm thấy hàng tiêu đề \"Mã BN\" / \"Ngày ĐK\". "
                        "Kiểm tra đúng loại báo cáo \"ĐK Khám Chữa Bệnh\" (file 01-1) của Minh Lộ."), None

        headers = [str(v).strip().replace("\n", " ").lower() for v in get_row(header_row_idx)]

        def find_col(keywords):
            for i, h in enumerate(headers):
                if any(k.lower() in h for k in keywords):
                    return i
            return None

        idx = {
            "ma_bn":     find_col(["mã bn", "ma bn"]),
            "ho_ten":    find_col(["họ tên", "ho ten"]),
            "ngay_sinh": find_col(["ngày tháng năm sinh", "ngay thang nam sinh"]),
            # "năm sinh" là chuỗi con của "ngày tháng năm sinh" → phải khớp CHÍNH XÁC cả ô
            "nam_sinh":  next((i for i, h in enumerate(headers) if h.strip() == "năm sinh"), None),
            "tuoi":      find_col(["tuổi", "tuoi"]),
            "gioi_tinh": find_col(["giới tính", "gioi tinh"]),
            "xa":        find_col(["xã,phường", "xã", "phường", "xa,phuong"]),
            "huyen":     find_col(["huyện,tỉnh", "huyện", "tỉnh", "huyen,tinh"]),
            "cmnd":      find_col(["số cmnd", "so cmnd", "cmnd"]),
            "ngay_dk":   find_col(["ngày đk", "ngay dk"]),
            "gio_dk":    find_col(["giờ đk", "gio dk"]),
            "khoa_dk":   find_col(["khoa đk", "khoa dk"]),
            "dt":        find_col(["điện thoại", "dien thoai"]),
            "dia_chi":   find_col(["địa chỉ", "dia chi"]),
            "chan_doan": find_col(["chẩn đoán", "chan doan"]),
            "bhyt":      find_col(["mã thẻ bhyt", "ma the bhyt", "bhyt"]),
        }

        def cv(row_vals, key):
            i = idx.get(key)
            if i is None or i >= len(row_vals):
                return ""
            return str(row_vals[i]).strip()

        data_rows, n_bad_date = [], 0
        for r in range(header_row_idx + 1, max_row + 1):
            row_vals = get_row(r)
            ho_ten = cv(row_vals, "ho_ten")
            if not ho_ten or not re.search(r"[^\W\d_]", ho_ten, re.UNICODE):
                continue                     # bỏ dòng trống / không phải bệnh nhân
            ngay_sinh, _ = parse_minhlo_date(cv(row_vals, "ngay_sinh"))
            ngay_dk, dk_ok = parse_minhlo_date(cv(row_vals, "ngay_dk"))
            if not dk_ok:
                n_bad_date += 1
            dia_chi = cv(row_vals, "dia_chi") or " ".join(
                p for p in [cv(row_vals, "xa"), cv(row_vals, "huyen")] if p)
            data_rows.append({
                "MÃ BN": cv(row_vals, "ma_bn"), "HỌ TÊN": ho_ten,
                "NGÀY SINH": ngay_sinh, "NĂM SINH": cv(row_vals, "nam_sinh"),
                "TUỔI": cv(row_vals, "tuoi"), "GIỚI TÍNH": cv(row_vals, "gioi_tinh"),
                "ĐỊA CHỈ": dia_chi, "SỐ CMND": cv(row_vals, "cmnd"),
                "NGÀY ĐK": ngay_dk, "GIỜ ĐK": cv(row_vals, "gio_dk"),
                "KHOA ĐK": cv(row_vals, "khoa_dk"),
                "SỐ ĐIỆN THOẠI": fix_phone(cv(row_vals, "dt")),
                "CHẨN ĐOÁN": cv(row_vals, "chan_doan"), "SỐ BHYT": cv(row_vals, "bhyt"),
            })
        warn = (f"⚠️ {n_bad_date} dòng trong file không đọc được NGÀY ĐK (định dạng lạ) — "
                f"những dòng này sẽ KHÔNG đối chiếu được, cần kiểm tra thủ công."
                if n_bad_date else None)
        return data_rows, None, warn
    except Exception as e:
        return [], f"Lỗi đọc file: {type(e).__name__}: {e}", None
