"""
services/sheets.py
──────────────────
Toàn bộ logic kết nối Google Sheets và xử lý DataFrame.
Tách riêng khỏi Flask routes để dễ test và maintain.
"""

import os, json, re, unicodedata
from zoneinfo import ZoneInfo
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date, timedelta
from flask import current_app

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Các cột trong Google Sheet ────────────────────────────────────────────────
COL_TIMESTAMP   = "Dấu thời gian"
COL_NAME        = "1. HỌ VÀ TÊN BỆNH NHÂN"
COL_GENDER      = "3. GIỚI TÍNH"
COL_SPECIALTY   = "CHUYÊN KHOA MONG MUỐN KHÁM"
COL_STATUS      = "TRẠNG THÁI"
COL_EXAM_DATE   = "NGÀY KHÁM"
COL_DOCTOR      = "BÁC SĨ MONG MUỐN ( nếu có)"
COL_SOURCE      = "NGUỒN BỆNH NHÂN"
COL_PHONE       = "5. SỐ ĐIÊN THOẠI"
COL_BIRTH_YEAR  = "NĂM SINH"
COL_EXAM_TIME   = "GIỜ KHÁM DỰ KIẾN"
COL_KHOA        = "KHOA KHÁM CHỮA BỆNH"
COL_ADDRESS     = "ĐỊA CHỈ"
COL_STT         = "STT"

STATUS_ATTENDED     = "BỆNH NHÂN ĐÃ KHÁM"
STATUS_NOT_ATTENDED = "BỆNH NHÂN CHƯA KHÁM / BỎ KHÁM"


def vn_today() -> date:
    """Ngày hôm nay theo giờ Việt Nam (server Render chạy giờ UTC)."""
    return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()


def _get_credentials() -> dict | None:
    """Lấy credentials GCP — ưu tiên env var, fallback ra file."""
    raw = os.environ.get("GCP_SERVICE_ACCOUNT")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    for path in ["credentials.json", os.path.join(os.path.dirname(__file__), "..", "credentials.json")]:
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as f:
                return json.load(f)
    return None


def _get_client() -> gspread.Client:
    """Tạo gspread client với service account."""
    creds_data = _get_credentials()
    if not creds_data:
        raise RuntimeError("Không tìm thấy GCP credentials. Kiểm tra biến môi trường GCP_SERVICE_ACCOUNT.")
    creds = Credentials.from_service_account_info(creds_data, scopes=SCOPES)
    return gspread.authorize(creds)


def load_dataframe() -> pd.DataFrame:
    """
    Đọc toàn bộ dữ liệu từ Google Sheet → DataFrame đã clean.
    Gọi hàm này trong route, kết quả được cache bởi Flask-Caching.
    """
    sheet_id   = current_app.config["SHEET_ID"]
    sheet_name = current_app.config["SHEET_NAME"]

    client = _get_client()
    ws     = client.open_by_key(sheet_id).worksheet(sheet_name)
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame()

    # Làm sạch tiêu đề: bỏ cột không tên, đánh số lại cột trùng tên
    raw_headers = [h.strip() for h in values[0]]
    seen, keep_idx, headers = {}, [], []
    for i, h in enumerate(raw_headers):
        if not h:
            continue                       # bỏ cột tiêu đề trống
        if h in seen:
            seen[h] += 1
            h = f"{h} ({seen[h]})"         # cột trùng: "ĐỊA CHỈ (2)"
        else:
            seen[h] = 1
        keep_idx.append(i)
        headers.append(h)

    data = [[(r[i] if i < len(r) else "") for i in keep_idx] for r in values[1:]]
    df   = pd.DataFrame(data, columns=headers)

    if df.empty:
        return df

    # ── Chuẩn hoá ngày khám ──────────────────────────────────────────────────
    if COL_EXAM_DATE in df.columns:
        df["_date"] = pd.to_datetime(df[COL_EXAM_DATE].astype(str).str.strip(), format="%d/%m/%Y", errors="coerce")

    # ── Chuẩn hoá timestamp ───────────────────────────────────────────────────
    if COL_TIMESTAMP in df.columns:
        df["_ts"] = pd.to_datetime(df[COL_TIMESTAMP], dayfirst=True, errors="coerce")

    # ── Tuổi từ năm sinh ─────────────────────────────────────────────────────
    if COL_BIRTH_YEAR in df.columns:
        current_year = datetime.now().year
        df["_age"] = pd.to_numeric(df[COL_BIRTH_YEAR], errors="coerce").apply(
            lambda y: int(current_year - y) if pd.notna(y) and 1900 < y <= current_year else None
        )

    return df


def get_dashboard_stats(df: pd.DataFrame) -> dict:
    """Tính các chỉ số tổng quan cho trang Dashboard."""
    today = vn_today()
    stats = {
        "today_total": 0, "today_attended": 0,
        "today_absent": 0,  "today_rate": 0.0,
        "next3_total": 0,
        "monthly_chart": [],   # [{month, registered, attended}]
        "recent_patients": [],
    }
    if df.empty or "_date" not in df.columns:
        return stats

    today_df = df[df["_date"].dt.date == today]
    stats["today_total"]    = len(today_df)
    stats["today_attended"] = int((today_df.get(COL_STATUS, pd.Series()) == STATUS_ATTENDED).sum())
    stats["today_absent"]   = stats["today_total"] - stats["today_attended"]
    if stats["today_total"] > 0:
        stats["today_rate"] = round(stats["today_attended"] / stats["today_total"] * 100, 1)

    # 3 ngày tới
    from datetime import timedelta
    next3 = [today + timedelta(days=i) for i in range(1, 4)]
    stats["next3_total"] = int(df[df["_date"].dt.date.isin(next3)].shape[0])

    # Biểu đồ theo tháng (12 tháng gần nhất)
    df["_ym"] = df["_date"].dt.to_period("M")
    monthly = df.groupby("_ym").apply(lambda g: {
        "month":      str(g.name),
        "registered": len(g),
        "attended":   int((g.get(COL_STATUS, pd.Series()) == STATUS_ATTENDED).sum()),
    }).tolist()
    stats["monthly_chart"] = sorted(monthly, key=lambda x: x["month"])[-12:]

    # Bệnh nhân gần đây (20 dòng cuối)
    recent = df.sort_values("_ts", ascending=False).head(20) if "_ts" in df.columns else df.tail(20)
    keep = [COL_STT, COL_NAME, COL_EXAM_DATE, COL_SPECIALTY, COL_DOCTOR, COL_STATUS, COL_SOURCE]
    stats["recent_patients"] = recent[[c for c in keep if c in recent.columns]].to_dict("records")

    return stats


def get_report_stats(df: pd.DataFrame, period: str = "month") -> list[dict]:
    """Tính bảng báo cáo chi tiết. period: 'day'|'week'|'month'|'quarter'|'year'"""
    if df.empty or "_date" not in df.columns:
        return []

    period_map = {"day": "D", "week": "W-MON", "month": "M", "quarter": "Q", "year": "Y"}
    freq = period_map.get(period, "M")

    df2 = df.copy()
    df2["_period"] = df2["_date"].dt.to_period(freq)

    is_attended = df2.get(COL_STATUS, pd.Series(dtype=str)) == STATUS_ATTENDED
    source_col  = df2.get(COL_SOURCE, pd.Series(dtype=str)).fillna("")

    is_tk = source_col.str.contains("tái khám|nội khoa", case=False, na=False)
    is_vl = ~is_tk

    rows = []
    for period_val, g in df2.groupby("_period"):
        att = g[is_attended.reindex(g.index, fill_value=False)]
        vng = g[~is_attended.reindex(g.index, fill_value=False)]
        rows.append({
            "ky":        str(period_val),
            "tong":      len(g),
            "den_tk":    int((att.index.isin(g[is_tk.reindex(g.index,fill_value=False)].index)).sum()),
            "den_vl":    int((att.index.isin(g[is_vl.reindex(g.index,fill_value=False)].index)).sum()),
            "vang_tk":   int((vng.index.isin(g[is_tk.reindex(g.index,fill_value=False)].index)).sum()),
            "vang_vl":   int((vng.index.isin(g[is_vl.reindex(g.index,fill_value=False)].index)).sum()),
            "tong_den":  len(att),
            "tong_vang": len(vng),
            "pct_den":   round(len(att)/len(g)*100, 1) if len(g) else 0.0,
            "pct_vang":  round(len(vng)/len(g)*100, 1) if len(g) else 0.0,
        })
    return rows


# ── Lọc theo khoảng ngày & lịch khám sắp tới ─────────────────────────────────
_WEEKDAY_VN = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]


def _norm(text) -> str:
    """Bỏ dấu + chữ thường để so khớp chuỗi tiếng Việt ổn định."""
    t = unicodedata.normalize("NFD", str(text or "").replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower().strip()


_KHOA_BLANK = {"", "nan", "n/a", "na", "none", "-", "—", "chưa xác định"}


def is_khoa_kham_benh(khoa) -> bool:
    """Giống classify_khoa_group() của Streamlit:
    Khoa Khám bệnh + chưa phân khoa (trống/placeholder) → True, khoa khác → False."""
    s = str(khoa or "").strip().lower()
    return s in _KHOA_BLANK or "khám bệnh" in s


def source_kind(src) -> str:
    """Giống source_pill_html() của Streamlit: noi | vl | other | none."""
    s = str(src or "").strip()
    if s in ("", "nan", "N/A", "—", "None"):
        return "none"
    sl = s.lower()
    if any(k in sl for k in ["khoa", "tái", "nội trú", "xuất viện", "tai"]):
        return "noi"
    if any(k in sl for k in ["vãng lai", "vang lai", "ngoài", "ngoai"]):
        return "vl"
    return "other"


def filter_by_date_range(df: pd.DataFrame, date_from=None, date_to=None) -> pd.DataFrame:
    """Lọc DataFrame theo NGÀY KHÁM từ date_from đến date_to (gồm cả 2 đầu)."""
    if df.empty or "_date" not in df.columns:
        return df
    if date_from is not None:
        df = df[df["_date"] >= pd.Timestamp(date_from)]
    if date_to is not None:
        df = df[df["_date"] < pd.Timestamp(date_to) + pd.Timedelta(days=1)]
    return df


def get_upcoming_patients(df: pd.DataFrame, days: int = 3) -> dict:
    """
    Bệnh nhân hẹn khám trong `days` ngày tới (từ ngày mai), nhóm theo ngày.
    Mỗi ngày chia 2 nhóm: kb (Khoa Khám bệnh + chưa phân khoa) và khac (nội trú khác).
    Dùng df ĐẦY ĐỦ (không lọc theo TRẠNG THÁI) như bản Streamlit.
    """
    today = vn_today()
    dates = [today + timedelta(days=i) for i in range(1, days + 1)]
    out = {"days": [], "total": 0, "start": dates[0], "end": dates[-1]}
    wd = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]

    has_date = (not df.empty) and "_date" in df.columns
    for d in dates:
        day = {"label": f"{wd[d.weekday()]} — {d.strftime('%d/%m/%Y')}",
               "kb": [], "khac": [], "count": 0}
        if has_date:
            sub = df[df["_date"].dt.date == d]
            if COL_EXAM_TIME in sub.columns:
                sub = sub.sort_values(COL_EXAM_TIME, kind="stable")
            for _, r in sub.iterrows():
                khoa = str(r.get(COL_KHOA, "") or "").strip()
                phone = str(r.get(COL_PHONE, "") or "").strip()
                etime = str(r.get(COL_EXAM_TIME, "") or "").strip()
                src = str(r.get(COL_SOURCE, "") or "").strip()
                rec = {
                    "name":   str(r.get(COL_NAME, "") or "—"),
                    "phone":  phone or "N/A",
                    "tel":    "".join(c for c in phone if c.isdigit() or c == "+"),
                    "time":   etime[:5] if ":" in etime else (etime or "—"),
                    "khoa":   khoa if khoa.lower() not in _KHOA_BLANK else "",
                    "source": src,
                    "src_kind": source_kind(src),
                }
                day["kb" if is_khoa_kham_benh(khoa) else "khac"].append(rec)
        day["count"] = len(day["kb"]) + len(day["khac"])
        out["total"] += day["count"]
        out["days"].append(day)
    return out


def update_status(row_number: int, new_status: str) -> bool:
    """Cập nhật TRẠNG THÁI 1 dòng trong Google Sheet."""
    try:
        sheet_id   = current_app.config["SHEET_ID"]
        sheet_name = current_app.config["SHEET_NAME"]
        client = _get_client()
        ws     = client.open_by_key(sheet_id).worksheet(sheet_name)
        headers = ws.row_values(1)
        col_idx = headers.index(COL_STATUS) + 1   # gspread 1-indexed
        ws.update_cell(row_number + 1, col_idx, new_status)   # +1 vì header chiếm hàng 1
        return True
    except Exception as e:
        current_app.logger.error(f"update_status error: {e}")
        return False


def append_patients(records: list[dict]) -> tuple[int, str | None]:
    """
    Ghi danh sách bệnh nhân (từ Excel Minh Lộ) vào Google Sheet.
    Trả về (số dòng đã ghi, thông báo lỗi hoặc None).
    """
    if not records:
        return 0, "Không có dữ liệu để import."
    try:
        sheet_id   = current_app.config["SHEET_ID"]
        sheet_name = current_app.config["SHEET_NAME"]
        client = _get_client()
        ws     = client.open_by_key(sheet_id).worksheet(sheet_name)
        headers = ws.row_values(1)

        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        rows_to_append = []
        for rec in records:
            row = [""] * len(headers)
            def _set(col_name, val):
                if col_name in headers:
                    row[headers.index(col_name)] = val or ""

            _set(COL_TIMESTAMP,  now_str)
            _set(COL_NAME,       rec.get("HỌ TÊN", ""))
            _set(COL_ADDRESS,    rec.get("ĐỊA CHỈ", ""))
            _set(COL_EXAM_DATE,  rec.get("NGÀY HẸN", ""))
            _set(COL_PHONE,      rec.get("SỐ ĐIỆN THOẠI", "N/A"))
            _set(COL_KHOA,       rec.get("KHOA HẸN", ""))
            _set(COL_SOURCE,     "Bệnh nhân điều trị nội khoa tái khám")
            _set(COL_SPECIALTY,  "Other: Bệnh nhân điều trị nội khoa tái khám")
            rows_to_append.append(row)

        ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        return len(rows_to_append), None
    except Exception as e:
        return 0, str(e)
