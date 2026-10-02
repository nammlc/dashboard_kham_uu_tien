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


PERIODS = {"day": "Ngày", "week": "Tuần", "month": "Tháng", "quarter": "Quý", "year": "Năm"}
_STATUS_BLANK = {"", "nan", "N/A", "\u200b"}
_TK_RE = "khoa|tái|nội trú|xuất viện|tai"      # giống Streamlit: nguồn tái khám / từ khoa

REPORT_KINDS = {                               # bấm số trong bảng → lọc danh sách BN
    "all":    "Tất cả đăng ký",
    "att":    "Tổng đã khám",
    "abs":    "Tổng không đến / chưa khám",
    "tk":     "Tái khám (tất cả)",
    "ut":     "Khám ưu tiên (tất cả)",
    "tk_att": "Tái khám · Đã khám",
    "tk_abs": "Tái khám · Không đến",
    "ut_att": "Khám ưu tiên · Đã khám",
    "ut_abs": "Khám ưu tiên · Không đến",
}


def _pct(a, b) -> float:
    return round(a / b * 100, 1) if b else 0.0


def _period_start(dates: pd.Series, period: str) -> pd.Series:
    """Ngày đầu kỳ của từng dòng — dùng làm khoá nhóm & sắp xếp theo thời gian."""
    d = dates.dt.normalize()
    if period == "week":
        return d - pd.to_timedelta(d.dt.weekday, unit="D")
    if period == "month":
        return d.dt.to_period("M").dt.start_time
    if period == "quarter":
        return d.dt.to_period("Q").dt.start_time
    if period == "year":
        return d.dt.to_period("Y").dt.start_time
    return d


def _period_label(start: pd.Timestamp, period: str) -> str:
    if period == "week":
        end = start + pd.Timedelta(days=6)
        iso = start.isocalendar()
        return f"T{iso[1]}/{iso[0]} ({start:%d/%m}–{end:%d/%m})"
    if period == "month":
        return f"Tháng {start:%m/%Y}"
    if period == "quarter":
        return f"Q{(start.month - 1) // 3 + 1}/{start.year}"
    if period == "year":
        return f"Năm {start.year}"
    return f"{start:%d/%m/%Y}"


def report_frame(df: pd.DataFrame, period: str = "month", date_from=None, date_to=None):
    """
    DataFrame đã gắn cờ cho báo cáo (giống _period_tag_df của Streamlit):
      _att = đã khám, _tk = tái khám/từ khoa, _ut = khám ưu tiên (đăng ký online/vãng lai/khác),
      _start = ngày đầu kỳ.
    Chỉ tính bệnh nhân ĐÃ CÓ TRẠNG THÁI và có ngày khám hợp lệ.
    """
    if df.empty or "_date" not in df.columns or COL_STATUS not in df.columns:
        return None
    d = df[df["_date"].notna()].copy()
    d = d[~d[COL_STATUS].astype(str).str.strip().isin(_STATUS_BLANK)]
    d = filter_by_date_range(d, date_from, date_to)
    if d.empty:
        return None
    d["_att"] = d[COL_STATUS].astype(str).str.strip().str.upper() == STATUS_ATTENDED.upper()
    if COL_SOURCE in d.columns:
        d["_tk"] = d[COL_SOURCE].astype(str).str.contains(_TK_RE, case=False, na=False)
    else:
        d["_tk"] = False
    d["_ut"] = ~d["_tk"]
    d["_start"] = _period_start(d["_date"], period)
    return d


def build_report(df: pd.DataFrame, period: str = "month", date_from=None, date_to=None) -> dict:
    """Số liệu cho trang Báo cáo: KPI tổng, bảng theo kỳ, dữ liệu biểu đồ."""
    period = period if period in PERIODS else "month"
    out = {"period": period, "period_name": PERIODS[period], "empty": True,
           "kpi": None, "rows": [], "chart": None}
    d = report_frame(df, period, date_from, date_to)
    if d is None:
        return out

    def block(g) -> dict:
        tot = len(g)
        att = int(g["_att"].sum())
        tk, ut = g[g["_tk"]], g[g["_ut"]]
        tk_att, ut_att = int(tk["_att"].sum()), int(ut["_att"].sum())
        return {
            "total": tot, "att": att, "abs": tot - att,
            "pct_att": _pct(att, tot), "pct_abs": _pct(tot - att, tot),
            "tk_total": len(tk), "tk_att": tk_att, "tk_abs": len(tk) - tk_att,
            "tk_pct": _pct(tk_att, len(tk)),
            "ut_total": len(ut), "ut_att": ut_att, "ut_abs": len(ut) - ut_att,
            "ut_pct": _pct(ut_att, len(ut)),
        }

    rows = []
    for start, g in d.groupby("_start", sort=True):
        r = block(g)
        r.update({"ky": _period_label(start, period), "key": start.strftime("%Y-%m-%d")})
        rows.append(r)

    kpi = block(d)
    peak = max(rows, key=lambda r: r["total"])
    kpi["peak_total"], kpi["peak_label"] = peak["total"], peak["ky"]
    kpi["n_periods"] = len(rows)
    kpi["tk_share"] = _pct(kpi["tk_total"], kpi["total"])
    kpi["ut_share"] = _pct(kpi["ut_total"], kpi["total"])

    shown = rows[-24:]                               # biểu đồ: tối đa 24 kỳ gần nhất cho dễ đọc
    out.update({
        "empty": False, "kpi": kpi, "rows": rows,
        "chart": {
            "truncated": len(rows) > len(shown),
            "labels":  [r["ky"] for r in shown],
            "att":     [r["att"] for r in shown],
            "abs":     [r["abs"] for r in shown],
            "pct_att": [r["pct_att"] for r in shown],
            "tk":      [r["tk_total"] for r in shown],
            "ut":      [r["ut_total"] for r in shown],
        },
    })
    return out


def report_drilldown(df: pd.DataFrame, period: str, key: str, kind: str,
                     date_from=None, date_to=None) -> pd.DataFrame:
    """Danh sách bệnh nhân khớp 1 ô trong bảng báo cáo (kỳ + loại)."""
    d = report_frame(df, period, date_from, date_to)
    if d is None:
        return pd.DataFrame()
    if key != "all":
        d = d[d["_start"].dt.strftime("%Y-%m-%d") == key]
    mask = {
        "all":    pd.Series(True, index=d.index),
        "att":    d["_att"],             "abs":    ~d["_att"],
        "tk":     d["_tk"],              "ut":     d["_ut"],
        "tk_att": d["_tk"] & d["_att"],  "tk_abs": d["_tk"] & ~d["_att"],
        "ut_att": d["_ut"] & d["_att"],  "ut_abs": d["_ut"] & ~d["_att"],
    }.get(kind)
    return d if mask is None else d[mask]



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
