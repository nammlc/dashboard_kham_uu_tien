"""
services/sheets.py
──────────────────
Toàn bộ logic kết nối Google Sheets và xử lý DataFrame.
Tách riêng khỏi Flask routes để dễ test và maintain.
"""

import os, json, re, time, threading, unicodedata
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
COL_ACTUAL_DATE = "NGÀY THỰC KHÁM"   # cột MỚI (thêm tay trên Sheet): ngày bệnh nhân thực sự đến khám
COL_CREATED     = "NGÀY TẠO LỊCH HẸN KHÁM"   # cột MỚI (thêm tay trên Sheet): ngày Minh Lộ tạo lịch hẹn (= "Ngày lập" của file 04-4)

STATUS_ATTENDED     = "BỆNH NHÂN ĐÃ KHÁM"
# Giữ ĐÚNG chuỗi mà import vẫn ghi từ trước tới nay (không có khoảng trắng quanh dấu "/"). Bản cũ có chỗ ghi
# "CHƯA KHÁM / BỎ KHÁM" (có khoảng trắng) → trên Sheet lẫn 2 kiểu, bộ lọc không bắt được hết. Khi ĐỌC luôn so sánh bằng
# status_key() nên cả 2 kiểu đều được nhận ra.
STATUS_NOT_ATTENDED = "BỆNH NHÂN CHƯA KHÁM/BỎ KHÁM"


def status_key(v) -> str:
    """Chuẩn hoá TRẠNG THÁI để so sánh: IN HOA, gộp khoảng trắng, bỏ khoảng trắng quanh dấu '/'."""
    t = re.sub(r"\s+", " ", str(v or "").replace("\u200b", "").strip().upper())
    return re.sub(r"\s*/\s*", "/", t)


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


_client_lock = threading.Lock()
_CLIENT = {"obj": None, "t": 0.0}
_CLIENT_TTL = 45 * 60            # tạo lại client mỗi 45 phút
_HTTP_TIMEOUT = (8, 20)          # (kết nối, chờ phản hồi) — không để request treo vô hạn


def reset_client() -> None:
    """Bỏ client + worksheet đang cache (gọi khi nghi kết nối hỏng)."""
    with _client_lock:
        _CLIENT["obj"] = None
        _WS_CACHE["ws"] = None


def _get_client() -> gspread.Client:
    """gspread client dùng lại giữa các lần gọi (đỡ đăng nhập OAuth lại mỗi request), có timeout."""
    now = time.time()
    with _client_lock:
        if _CLIENT["obj"] is not None and now - _CLIENT["t"] < _CLIENT_TTL:
            return _CLIENT["obj"]
        creds_data = _get_credentials()
        if not creds_data:
            raise RuntimeError("Không tìm thấy GCP credentials. Kiểm tra biến môi trường GCP_SERVICE_ACCOUNT.")
        creds = Credentials.from_service_account_info(creds_data, scopes=SCOPES)
        client = gspread.authorize(creds)
        if hasattr(client, "set_timeout"):
            try:
                client.set_timeout(_HTTP_TIMEOUT)
            except Exception:
                pass
        _CLIENT["obj"], _CLIENT["t"] = client, now
        return client


def parse_sheet_dates(series: pd.Series) -> pd.Series:
    """Cột ngày trên Sheet → datetime64. Ưu tiên 'dd/mm/yyyy' (đúng như trước đây); những ô còn lại thử thêm:
    'dd/mm/yyyy hh:mm', 'yyyy-mm-dd' và số serial của Excel/Sheets (khi ô bị hiện dạng số). Không hiểu → NaT."""
    txt = series.astype(str).str.strip()
    out = pd.to_datetime(txt, format="%d/%m/%Y", errors="coerce")
    miss = out.isna() & txt.ne("")
    if not miss.any():
        return out
    for idx in out.index[miss]:
        d = to_dmy(txt.at[idx])
        if d:
            out.at[idx] = pd.Timestamp(datetime.strptime(d, "%d/%m/%Y"))
            continue
        try:
            n = float(txt.at[idx])
        except ValueError:
            continue
        if 40000 < n < 60000:                       # serial: ngày 0 của Sheets là 30/12/1899
            out.at[idx] = pd.Timestamp("1899-12-30") + pd.Timedelta(days=int(n))
    return out


def load_dataframe() -> pd.DataFrame:
    """
    Đọc toàn bộ dữ liệu từ Google Sheet → DataFrame đã clean.
    Gọi hàm này trong route, kết quả được cache bởi Flask-Caching.
    """
    ws     = _open_ws()
    values = _retry(ws.get_all_values, deadline=time.time() + READ_BUDGET_S)
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
        df["_date"] = parse_sheet_dates(df[COL_EXAM_DATE])

    # ── Chuẩn hoá NGÀY TẠO LỊCH HẸN KHÁM (cột mới; dòng cũ có thể để trống → NaT) ──
    if COL_CREATED in df.columns:
        df["_created"] = parse_sheet_dates(df[COL_CREATED])

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

    # Bệnh nhân gần đây (50 dòng cuối, giao diện tự chia trang)
    recent = df.sort_values("_ts", ascending=False).head(50) if "_ts" in df.columns else df.tail(50)
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


def to_dmy(v) -> str:
    """date / datetime / 'YYYY-MM-DD' / 'dd/mm/yyyy [giờ]' → 'dd/mm/YYYY' (chuỗi, 2 chữ số); không hiểu → ''."""
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        try:
            return v.strftime("%d/%m/%Y")
        except Exception:
            return ""
    t = str(v).strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return f"{int(m.group(3)):02d}/{int(m.group(2)):02d}/{m.group(1)}"
    m = re.search(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?!\d)", t)      # d/m/yyyy · d-m-yyyy · d.m.yyyy
    if m:
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"
    m = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{2})(?!\d)", t)                  # d/m/yy (Sheet hiển thị năm 2 số)
    if m:
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/20{m.group(3)}"
    return ""


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


DASH_SCOPES = {
    "today":    "Đăng ký hôm nay",
    "attended": "Đã khám hôm nay",
    "absent":   "Vắng / chưa khám hôm nay",
    "next3":    "Lịch hẹn 3 ngày tới",
}


def filter_by_scope(df: pd.DataFrame, scope: str) -> pd.DataFrame:
    """Lọc theo ô KPI ở trang Tổng quan (cùng logic với get_dashboard_stats)."""
    if scope not in DASH_SCOPES or df.empty or "_date" not in df.columns:
        return df
    today = vn_today()
    dates = df["_date"].dt.date
    if scope == "next3":
        nxt = [today + timedelta(days=i) for i in range(1, 4)]
        return df[dates.isin(nxt)]
    sub = df[dates == today]
    if scope == "attended":
        return sub[sub.get(COL_STATUS, "") == STATUS_ATTENDED]
    if scope == "absent":
        return sub[sub.get(COL_STATUS, "") != STATUS_ATTENDED]
    return sub


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
    """Cập nhật TRẠNG THÁI 1 dòng trong Google Sheet (row_number = chỉ số dòng dữ liệu, dòng đầu tiên = 1)."""
    try:
        ws = _open_ws()
        headers = [h.strip() for h in _retry(lambda: ws.row_values(1))]
        col_idx = headers.index(COL_STATUS) + 1   # gspread 1-indexed
        _retry(lambda: ws.update_cell(row_number + 1, col_idx, new_status))   # +1 vì header chiếm hàng 1 (ghi đè → thử lại an toàn)
        return True
    except Exception as e:
        _WS_CACHE["ws"] = None
        current_app.logger.error(f"update_status error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# IMPORT LỊCH HẸN TÁI KHÁM (file Minh Lộ 04-4) — port y hệt từ Streamlit
# ══════════════════════════════════════════════════════════════════════════════

def norm_name(s) -> str:
    """'Lê Hoàng Phúc' → 'LE HOANG PHUC' (bỏ dấu, gộp khoảng trắng, viết hoa)."""
    s2 = str(s or "").replace("Đ", "D").replace("đ", "d")
    s2 = unicodedata.normalize("NFKD", s2)
    s2 = "".join(c for c in s2 if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s2.strip()).upper()


def norm_phone_key(s) -> str:
    """Chỉ giữ chữ số; 9 số → thêm 0 đầu; không hợp lệ → ''."""
    digits = re.sub(r"\D", "", str(s or ""))
    if len(digits) == 9:
        digits = "0" + digits
    return digits if len(digits) >= 9 and set(digits) != {"0"} else ""


def fmt_vn_time(ts) -> str:
    """Epoch giây → 'HH:MM:SS' giờ Việt Nam ('' nếu không có)."""
    return datetime.fromtimestamp(ts, ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%H:%M:%S") if ts else ""


def vn_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))


_COL_ADDRESS_FORM = "2. ĐỊA CHỈ (THÔN/XÃ)"
_COL_AGE          = "TUỔI"


def build_sheet_field_map(record: dict, import_time_str: str) -> dict:
    """Bản ghi Minh Lộ → {tên cột trên Sheet: giá trị}. Ghép theo TÊN cột nên
    không phụ thuộc thứ tự cột trên Sheet thực tế."""
    khoa_hen = str(record.get("KHOA HẸN", "N/A") or "N/A").upper()   # KHOA/NGUỒN/TRẠNG THÁI luôn IN HOA
    lap = to_dmy(record.get("NGÀY LẬP", ""))
    return {
        "Dấu thời gian":              import_time_str,
        COL_SOURCE:                   "BỆNH NHÂN ĐIỀU TRỊ NỘI KHOA TÁI KHÁM",
        COL_STATUS:                   STATUS_NOT_ATTENDED,
        COL_EXAM_DATE:                record.get("NGÀY HẸN", "N/A") or "N/A",
        # Dấu ' đứng đầu = ép Sheets lưu thành CHỮ "dd/mm/yyyy" (không bị hiểu nhầm tháng/ngày theo ngôn ngữ của Sheet,
        # không bị hiện thành số serial). Khi đọc lại, Sheets không trả về dấu ' này.
        COL_CREATED:                  ("'" + lap) if lap else "",
        COL_NAME:                     record.get("HỌ TÊN", "N/A"),
        COL_BIRTH_YEAR:               record.get("NĂM SINH (ước tính)") or "N/A",
        _COL_AGE:                     record.get("TUỔI") or "N/A",
        COL_PHONE:                    record.get("SỐ ĐIỆN THOẠI", "N/A"),
        _COL_ADDRESS_FORM:            record.get("ĐỊA CHỈ", "N/A"),
        COL_KHOA:                     khoa_hen,
        COL_GENDER:                   record.get("GIỚI TÍNH") or "N/A",
        "1. TRIỆU CHỨNG CHÍNH":       "N/A",
        "4. SỐ CĂN CƯỚC CÔNG DÂN - CHỨNG MINH THƯ": "N/A",
        COL_SPECIALTY:                "Other: Bệnh nhân điều trị nội khoa tái khám",
        COL_DOCTOR:                   "N/A",
        COL_EXAM_TIME:                "N/A",
        "1. CAM KẾT CÁC THÔNG TIN LÀ THÔNG TIN ĐÚNG, CHỊU TRÁCH NHIỆM TRƯỚC PHÁP LUẬT TRƯỚC NHỮNG THÔNG TIN ĐÃ CUNG CẤP TRÊN": "CÓ",
        "ĐỒNG Ý CÁC ĐIỀU KHOẢN ĐẶT LỊCH KHÁM ONLINE TẠI BVĐK TÂM ĐỨC CẦU QUAN": "CÓ",
    }


def _date_key(v) -> str:
    """Ô ngày (Timestamp / NaT / chuỗi) → 'dd/mm/YYYY' hoặc '' nếu không có ngày hợp lệ."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return to_dmy(v)


def _index_sheet(df: pd.DataFrame) -> dict:
    """Dựng chỉ mục các dòng ĐÃ CÓ trên Sheet để tra trùng nhanh.
      lap  : (tên, ngày tạo lịch hẹn)          — khoá CHÍNH, dùng cột NGÀY TẠO LỊCH HẸN KHÁM
      full : (tên, SĐT, ngày hẹn)              — khoá cũ cho các dòng chưa có ngày tạo
      nd   : (tên, ngày hẹn)                   — khoá cũ khi thiếu SĐT
    Mỗi khoá → danh sách tham chiếu {stt, row, phone, has_lap, hen, lap}."""
    idx = {"lap": {}, "full": {}, "nd": {}}
    if df is None or len(df) == 0 or COL_NAME not in df.columns:
        return idx
    n = len(df)
    blank = [None] * n
    names = df[COL_NAME].tolist()
    hens = df["_date"].tolist() if "_date" in df.columns else blank
    laps = df["_created"].tolist() if "_created" in df.columns else blank
    phones = df[COL_PHONE].tolist() if COL_PHONE in df.columns else blank
    stts = df[COL_STT].tolist() if COL_STT in df.columns else blank
    for i, (name, h, l, phone, stt) in enumerate(zip(names, hens, laps, phones, stts)):
        nk = norm_name(name)
        if not nk:
            continue
        hk, lk, pk = _date_key(h), _date_key(l), norm_phone_key(phone)
        ref = {"stt": "" if stt is None else str(stt).strip(), "row": i + 2, "phone": pk,
               "has_lap": bool(lk), "hen": hk, "lap": lk}
        if lk:
            idx["lap"].setdefault((nk, lk), []).append(ref)
        if hk:
            idx["nd"].setdefault((nk, hk), []).append(ref)
            if pk:
                idx["full"].setdefault((nk, pk, hk), []).append(ref)
    return idx


def _phones_conflict(a: str, b: str) -> bool:
    """2 SĐT đều hợp lệ mà khác nhau → rất có thể là 2 NGƯỜI khác nhau trùng tên."""
    return bool(a and b and a != b)


def check_sheet_duplicates(df: pd.DataFrame, candidates: list[dict]):
    """
    So khớp BN sắp import với dữ liệu ĐÃ CÓ trên Sheet (df đầy đủ, không lọc trạng thái).
    Mỗi bản ghi được xét ĐỘC LẬP (kết quả không phụ thuộc thứ tự trong file). Thứ tự ưu tiên của khoá:
      1) Tên + NGÀY TẠO LỊCH HẸN KHÁM (cột mới) — cùng Họ tên, cùng ngày lập, SĐT không mâu thuẫn
      2) Tên + SĐT + NGÀY KHÁM                  — cho các dòng nhập từ trước khi có cột mới
      3) Tên + NGÀY KHÁM (khi thiếu SĐT)        — SĐT không được mâu thuẫn
    Trả về (new_records, dup_records). Mỗi dup có thêm: _existing_stt, _existing_row, _match ('lap' | 'hen'),
    _existing_hen (ngày hẹn đang có trên Sheet) và _backfill (True = dòng cũ còn trống NGÀY TẠO LỊCH HẸN KHÁM
    và khớp DUY NHẤT 1 dòng → có thể điền bổ sung).
    """
    idx = _index_sheet(df)
    new_records, dup_records = [], []
    for r in candidates:
        nk = norm_name(r.get("HỌ TÊN", ""))
        hk = to_dmy(r.get("NGÀY HẸN", ""))          # '5/10/2026' và '05/10/2026' phải là MỘT khoá
        lk = to_dmy(r.get("NGÀY LẬP", ""))
        pk = norm_phone_key(r.get("SỐ ĐIỆN THOẠI", ""))

        found, how = [], None
        if nk and lk:
            found = [x for x in idx["lap"].get((nk, lk), ()) if not _phones_conflict(pk, x["phone"])]
            how = "lap" if found else None
        if not found and nk and hk and pk:
            found = [x for x in idx["full"].get((nk, pk, hk), ())]
            how = "hen" if found else None
        if not found and nk and hk:
            found = [x for x in idx["nd"].get((nk, hk), ()) if not _phones_conflict(pk, x["phone"])]
            how = "hen" if found else None

        if found:
            ref = found[0]
            r2 = dict(r)
            r2.update({"_existing_stt": ref["stt"], "_existing_row": ref["row"], "_match": how,
                       "_existing_hen": ref["hen"],
                       "_backfill": bool(lk) and len(found) == 1 and not ref["has_lap"]})
            dup_records.append(r2)
        else:
            new_records.append(r)
    return new_records, dup_records


def drop_file_duplicates(records: list[dict]):
    """Loại các dòng TRÙNG NGAY TRONG FILE đã chọn (cùng Tên + Ngày hẹn + Ngày lập, SĐT không mâu thuẫn) — chỉ giữ dòng
    đầu tiên. Trả về (kept, dropped). Gọi SAU khi đã lọc theo ngày lập đã chọn."""
    kept, dropped, seen = [], [], {}
    for r in records:
        key = (norm_name(r.get("HỌ TÊN", "")), to_dmy(r.get("NGÀY HẸN", "")), to_dmy(r.get("NGÀY LẬP", "")))
        pk = norm_phone_key(r.get("SỐ ĐIỆN THOẠI", ""))
        if key[0] and any(not _phones_conflict(pk, other) for other in seen.get(key, ())):
            r2 = dict(r)
            r2["_existing_stt"] = "(trùng ngay trong file)"
            dropped.append(r2)
            continue
        if key[0]:
            seen.setdefault(key, []).append(pk)
        kept.append(r)
    return kept, dropped


def sheet_snapshot_stats(df) -> dict:
    """Để người dùng thấy việc kiểm tra trùng có thật sự đọc được Sheet không: tổng số dòng / số dòng đủ tên + ngày hợp lệ."""
    if df is None or len(df) == 0:
        return {"rows": 0, "valid": 0}
    if "_date" not in df.columns or COL_NAME not in df.columns:
        return {"rows": len(df), "valid": 0}
    ok = df["_date"].notna() & df[COL_NAME].astype(str).str.strip().ne("")
    return {"rows": len(df), "valid": int(ok.sum())}


def sheet_import_diagnostics(df) -> dict:
    """Thông tin giúp người dùng hiểu vì sao kiểm tra trùng có thể chưa đầy đủ:
      has_created_col : Sheet đã có cột NGÀY TẠO LỊCH HẸN KHÁM chưa (chưa có → không ghi được ngày tạo)
      created_filled  : số dòng đã có ngày tạo hợp lệ
      bad_dates       : số dòng có NGÀY KHÁM nhưng KHÔNG đọc được (không dùng để tra trùng được)
      bad_samples     : vài giá trị mẫu của các ô đó"""
    out = {"has_created_col": False, "created_filled": 0, "bad_dates": 0, "bad_samples": []}
    if df is None:
        return out
    cols = list(getattr(df, "columns", []))
    out["has_created_col"] = COL_CREATED in cols
    if len(df) == 0:
        return out
    if "_created" in cols:
        out["created_filled"] = int(df["_created"].notna().sum())
    if COL_EXAM_DATE in cols and "_date" in cols and COL_NAME in cols:
        raw = df[COL_EXAM_DATE].astype(str).str.strip()
        named = df[COL_NAME].astype(str).str.strip().ne("")
        bad = df["_date"].isna() & raw.ne("") & ~raw.str.upper().isin({"N/A", "NAN"}) & named
        out["bad_dates"] = int(bad.sum())
        out["bad_samples"] = list(dict.fromkeys(raw[bad].tolist()))[:3]
    return out


def append_patients(records: list[dict]) -> tuple[int, str | None]:
    """
    Ghi BN (từ file 04-4) vào Sheet chính, ghép đúng cột theo TÊN tiêu đề dòng 1.
    NGÀY KHÁM ghi dạng số serial + USER_ENTERED để Sheet tự hiểu là Date;
    NGÀY TẠO LỊCH HẸN KHÁM ghi dạng chữ dd/mm/yyyy (xem build_sheet_field_map);
    TRẠNG THÁI khớp list dropdown. Trả về (số dòng đã ghi, lỗi hoặc None).

    Thêm dòng KHÔNG idempotent nên KHÔNG tự thử lại: nếu mất kết nối giữa chừng, người dùng bấm lại thì
    bước kiểm tra trùng (đọc Sheet mới) sẽ loại những dòng đã được ghi — không bao giờ nhập 2 lần.
    """
    if not records:
        return 0, "Không có dữ liệu để ghi."
    try:
        ws = _open_ws()
        headers = [h.strip() for h in _retry(lambda: ws.row_values(1))]
        if not headers:
            return 0, "Sheet đang trống, không có dòng tiêu đề."

        import_time_str = vn_now().strftime("%d/%m/%Y %H:%M:%S")
        date_idx = headers.index(COL_EXAM_DATE) if COL_EXAM_DATE in headers else None

        def to_serial(s):
            try:
                return (datetime.strptime(str(s).strip(), "%d/%m/%Y") - datetime(1899, 12, 30)).days
            except Exception:
                return s

        rows = []
        for r in records:
            fm = build_sheet_field_map(r, import_time_str)
            # Sheet nào chỉ có cột "ĐỊA CHỈ" (không có "2. ĐỊA CHỈ (THÔN/XÃ)") thì ghi vào đó
            if _COL_ADDRESS_FORM not in headers and COL_ADDRESS in headers:
                fm[COL_ADDRESS] = fm[_COL_ADDRESS_FORM]
            row = [fm.get(h, "") for h in headers]
            if date_idx is not None:
                row[date_idx] = to_serial(row[date_idx])
            rows.append(row)

        resp = ws.append_rows(rows, value_input_option="USER_ENTERED",
                              insert_data_option="INSERT_ROWS", table_range="A1")
        # Google trả về số dòng đã ghi — đối chiếu để không báo thành công giả
        updated = ((resp or {}).get("updates") or {}).get("updatedRows") if isinstance(resp, dict) else None
        if updated is not None and int(updated) != len(rows):
            return 0, (f"Google chỉ xác nhận ghi {updated}/{len(rows)} dòng. Hãy tải lại trang để xem phần đã ghi, "
                       "rồi import lại — hệ thống sẽ tự bỏ qua những bệnh nhân đã có.")
        return len(rows), None
    except Exception as e:
        _WS_CACHE["ws"] = None
        return 0, friendly_error(e, "ghi Sheet")


# ══════════════════════════════════════════════════════════════════════════════
# GHI SHEET CHO ĐỐI CHIẾU TÁI KHÁM / LỌC TRÙNG
# Mọi hàm: tra lại dòng THỰC TẾ ngay lúc ghi (theo khoá chính STT nếu có) rồi
# ĐỐI CHIẾU LẠI TÊN ở dòng đó — nếu Sheet vừa bị thêm/xoá/sắp xếp lại và dòng không
# còn đúng bệnh nhân thì DỪNG, không ghi nhầm người.
# ══════════════════════════════════════════════════════════════════════════════
_WS_CACHE = {"ws": None, "key": None, "t": 0.0}
_WS_TTL = 10 * 60
WRITE_BUDGET_S = 70      # tổng thời gian tối đa cho 1 lần ghi (< timeout trình duyệt 80s < gunicorn 120s)
READ_BUDGET_S = 40       # đọc toàn bộ Sheet: tối đa ~2 lần thử (import = đọc + ghi vẫn < 120s)
_TRANSIENT = {"ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout", "ChunkedEncodingError",
              "SSLError", "ProxyError", "RemoteDisconnected", "TransportError", "ReadTimeoutError",
              "ConnectionResetError", "BrokenPipeError", "TimeoutError"}


def _http_status(e):
    return getattr(getattr(e, "response", None), "status_code", None)


def _is_transient(e) -> bool:
    return _http_status(e) in (429, 500, 502, 503, 504) or any(c.__name__ in _TRANSIENT for c in type(e).__mro__)


def _retry(fn, attempts=3, deadline=None):
    """Gọi fn(); lỗi tạm thời (429/5xx/mất kết nối/timeout) thì thử lại với backoff.
    Lỗi 429 (vượt hạn mức đọc/phút của Google) chờ LÂU hơn các lỗi khác — thử lại sau 1,5 giây gần như chắc chắn lại
    bị 429 tiếp. `deadline` (time.time() tuyệt đối): quá hạn là thôi, không thử lại nữa — để tổng thời gian của 1 thao tác
    luôn nhỏ hơn timeout của gunicorn/trình duyệt.
    CHỈ dùng cho thao tác ĐỌC hoặc GHI-ĐÈ (idempotent) — tuyệt đối không dùng cho xoá dòng hay thêm dòng."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            late = deadline is not None and time.time() > deadline
            if i == attempts - 1 or late or not _is_transient(e):
                raise
            if _http_status(e) is None:      # lỗi kết nối → bỏ client cũ, lần sau tạo lại
                reset_client()
            delay = 5.0 * (i + 1) if _http_status(e) == 429 else 1.5 * (i + 1)
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - time.time()))
            time.sleep(delay)


def _check_deadline(deadline):
    if deadline is not None and time.time() > deadline:
        raise TimeoutError("quá thời gian chờ Google Sheets")


def friendly_error(e, action="ghi Sheet") -> str:
    code = _http_status(e)
    if code == 429:
        return "Google Sheets đang giới hạn số lần gọi (quá nhanh). Hãy đợi khoảng 1 phút rồi thử lại."
    if code in (401, 403):
        return "Không có quyền ghi vào Google Sheet — vào Share và đổi Service Account thành Editor."
    if code == 404:
        return "Không tìm thấy Google Sheet / tab (kiểm tra SHEET_ID và SHEET_NAME)."
    if code in (500, 502, 503, 504) or any(c.__name__ in _TRANSIENT for c in type(e).__mro__):
        return (f"Google Sheets phản hồi quá chậm hoặc mất kết nối khi {action}. Dữ liệu CÓ THỂ chưa được ghi — "
                "hãy tải lại trang để xem trạng thái thực tế rồi thử lại.")
    return f"Lỗi {action}: {type(e).__name__}: {e}"


def _open_ws(fresh=False):
    """Worksheet dùng lại 10 phút (đỡ 2 lần gọi API mỗi thao tác)."""
    key = (current_app.config["SHEET_ID"], current_app.config["SHEET_NAME"])
    now = time.time()
    if (not fresh and _WS_CACHE["ws"] is not None and _WS_CACHE["key"] == key and now - _WS_CACHE["t"] < _WS_TTL):
        return _WS_CACHE["ws"]
    ws = _retry(lambda: _get_client().open_by_key(key[0]).worksheet(key[1]))
    _WS_CACHE.update(ws=ws, key=key, t=now)
    return ws


def _cell(grid, row, ci):
    """Giá trị ô (row 1-based, ci 0-based) từ ảnh chụp Sheet; ô ngoài vùng dữ liệu → ''."""
    if ci is None or not (0 < row <= len(grid)):
        return ""
    r = grid[row - 1]
    return r[ci] if ci < len(r) else ""


def _resolve_items(grid, headers, items):
    """items: list dict {sheet_row, stt, name} → ({chỉ_số_item: số dòng thực tế}, {chỉ_số_item: lỗi}).
    Chỉ ghi vào dòng ĐÚNG bệnh nhân (đối chiếu lại TÊN ngay trên ảnh chụp Sheet vừa đọc). Thứ tự thử:
      1) STT (nếu có) — xét MỌI dòng mang STT đó (STT trên Sheet có thể bị trùng), lấy dòng đúng tên;
      2) số dòng đã ghi nhớ — chỉ khi tên khớp và STT ở dòng đó không mâu thuẫn.
    Lỗi của 1 người KHÔNG làm hỏng cả lô: các người còn lại vẫn được xử lý."""
    name_ci = headers.index(COL_NAME) if COL_NAME in headers else None
    stt_ci = headers.index(COL_STT) if COL_STT in headers else None
    stt_rows = {}
    if stt_ci is not None:
        for r in range(2, len(grid) + 1):
            v = str(_cell(grid, r, stt_ci)).strip()
            if v:
                stt_rows.setdefault(v, []).append(r)

    rows, errs = {}, {}
    for i, it in enumerate(items):
        stt = str(it.get("stt") or "").strip()
        expect = norm_name(it.get("name", ""))
        try:
            hint = int(it["sheet_row"])
        except (KeyError, TypeError, ValueError):
            hint = -1

        def name_ok(r):
            return not expect or name_ci is None or norm_name(_cell(grid, r, name_ci)) == expect

        picked = None
        if stt and stt in stt_rows:
            cands = [r for r in stt_rows[stt] if name_ok(r)]
            if len(cands) == 1:
                picked = cands[0]
            elif len(cands) > 1:
                picked = hint if hint in cands else None
                if picked is None:
                    errs[i] = (f"Có {len(cands)} dòng trùng STT {stt} và trùng tên «{it.get('name', '')}» "
                               "— hãy xử lý tay trên Sheet.")
                    continue
        if picked is None and hint > 1 and name_ok(hint):
            cell_stt = str(_cell(grid, hint, stt_ci)).strip() if stt_ci is not None else ""
            if not stt or not cell_stt or cell_stt == stt:
                picked = hint
        if picked is None:
            if stt and stt not in stt_rows and not (hint > 1 and name_ok(hint)):
                errs[i] = (f"Không tìm thấy STT {stt} («{it.get('name', '')}») trên Google Sheet — dòng có thể đã bị xoá "
                           "hoặc đánh số lại. Bấm Làm mới rồi đối chiếu lại.")
            else:
                errs[i] = (f"Dòng {hint} trên Sheet không còn là bệnh nhân «{it.get('name', '')}» "
                           f"(Sheet vừa bị sửa/sắp xếp lại?). Đã dừng để không ghi nhầm — "
                           f"bấm Làm mới rồi đối chiếu lại.")
            continue
        rows[i] = picked
    return rows, errs


def _norm_cmp(v) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "").strip()).casefold()


def _same_cell(got, want) -> bool:
    """So ô đọc lại với giá trị đã ghi: bỏ qua khác biệt hoa/thường, khoảng trắng, dấu ' đứng đầu, và ngày viết khác kiểu."""
    g, w = _norm_cmp(got), _norm_cmp(str(want).lstrip("'"))
    if g == w:
        return True
    gd, wd = to_dmy(g), to_dmy(w)
    return bool(gd and gd == wd and re.fullmatch(r"[\d/.\-: ]+", w) is not None)


def _col_letter(ci: int) -> str:
    """Số cột 1-based → chữ cái (1→A, 27→AA)."""
    out = ""
    while ci:
        ci, rem = divmod(ci - 1, 26)
        out = chr(65 + rem) + out
    return out


def _read_columns(ws, cols, deadline=None):
    """Đọc nhiều cột (số cột 1-based) trong CÙNG 1 lần gọi API → {cột: danh sách giá trị từ dòng 1}."""
    cols = sorted(set(cols))
    _check_deadline(deadline)
    res = _retry(lambda: ws.batch_get([f"{_col_letter(c)}:{_col_letter(c)}" for c in cols]), deadline=deadline)
    out = {}
    for c, vr in zip(cols, res):
        out[c] = [(row[0] if row else "") for row in vr]
    return out


def _verify_written(ws, expected, deadline=None):
    """Đọc lại các cột vừa ghi và so với giá trị mong muốn (1 lần gọi API cho tất cả các cột).
    expected: {(row, col): giá_trị}. Trả về tập {(row, col)} KHÔNG khớp (rỗng = đã ghi đúng hết)."""
    if not expected:
        return set()
    vals = _read_columns(ws, [c for _, c in expected], deadline)
    bad = set()
    for (row, c), want in expected.items():
        col_vals = vals.get(c, [])
        got = col_vals[row - 1] if 0 < row <= len(col_vals) else ""
        if not _same_cell(got, want):
            bad.add((row, c))
    return bad


def write_cells_ex(updates, raw=False, verify=True, fill_only=False):
    """updates: list dict {sheet_row, stt, name, values: {tên_cột: giá_trị}}.
    Trả về (done, errors, fatal):
      done   — tập chỉ số (trong `updates`) đã ghi VÀ đọc lại xác nhận đúng
      errors — {chỉ số: lý do} cho từng người không ghi được (các người khác vẫn được ghi)
      fatal  — thông báo lỗi chung (mất kết nối, hết quyền, quá thời gian…) hoặc None
    Số lần gọi Google API cố định, KHÔNG phụ thuộc số người: 1 (đọc Sheet) + 1 (ghi) + 1 (đọc lại) — bản cũ tốn
    6–7 lần/thao tác nên dễ vượt hạn mức 60 lần đọc/phút và báo lỗi 429 (phải bấm đi bấm lại).
    fill_only=True: chỉ ghi vào ô đang TRỐNG (không đè dữ liệu đã có)."""
    if not updates:
        return set(), {}, None
    deadline = time.time() + WRITE_BUDGET_S
    try:
        ws = _open_ws()
        grid = _retry(ws.get_all_values, deadline=deadline)
        headers = [h.strip() for h in (grid[0] if grid else [])]
        if not headers:
            return set(), {}, "Sheet đang trống, không có dòng tiêu đề."
        _check_deadline(deadline)
        rows, errors = _resolve_items(grid, headers, updates)

        cells, owners = {}, {}                          # (row, col) → giá trị · (row, col) → [chỉ số item]
        done = set()
        for i, row in rows.items():
            touched = 0
            for col, val in updates[i]["values"].items():
                if col not in headers:
                    continue
                ci = headers.index(col) + 1
                touched += 1
                if fill_only and str(_cell(grid, row, ci - 1)).strip():
                    continue                            # ô đã có dữ liệu → giữ nguyên
                cells[(row, ci)] = val
                owners.setdefault((row, ci), []).append(i)
            if not touched:
                errors[i] = "Không tìm thấy cột nào khớp trên Google Sheet."
            elif not any(i in o for o in owners.values()):
                done.add(i)                             # fill_only: mọi ô đã có sẵn → coi như xong
        if not cells:
            return done, errors, None

        # Ghi đè giá trị (idempotent) nên an toàn khi thử lại nếu lần trước bị timeout giữa chừng
        body = [{"range": gspread.utils.rowcol_to_a1(r, c), "values": [[v]]} for (r, c), v in cells.items()]
        _check_deadline(deadline)
        _retry(lambda: ws.batch_update(body, value_input_option="RAW" if raw else "USER_ENTERED"), deadline=deadline)

        bad = set()
        if verify:
            try:
                bad = _verify_written(ws, cells, deadline)
            except Exception as e:       # ghi xong nhưng chưa đọc lại được → không báo lỗi oan
                current_app.logger.warning(f"write_cells: không đọc lại để kiểm tra được: {e}")
        bad_items = {i for key in bad for i in owners[key]}
        for i in rows:
            if i in errors or i in done:
                continue
            if i in bad_items:
                n_bad = sum(1 for key in bad if i in owners[key])
                errors[i] = (f"Đã gửi lệnh ghi nhưng đọc lại thấy {n_bad} ô chưa đúng trên Sheet "
                             "(có thể Sheet đang bị khoá hoặc đang có người sửa). Hãy tải lại trang để kiểm tra rồi thử lại.")
            else:
                done.add(i)
        return done, errors, None
    except Exception as e:
        _WS_CACHE["ws"] = None
        return set(), {}, friendly_error(e)


def write_cells(updates, raw=False, verify=True):
    """Bản gọn của write_cells_ex cho nơi chỉ cần (số dòng đã ghi, lỗi | None). Có người lỗi → `lỗi` khác None
    (số dòng đã ghi vẫn là số thật đã ghi được)."""
    done, errors, fatal = write_cells_ex(updates, raw=raw, verify=verify)
    if fatal:
        return 0, fatal
    if errors:
        first = errors[min(errors)]
        return len(done), first + (f" (và {len(errors) - 1} ca khác cũng lỗi)" if len(errors) > 1 else "")
    return len(done), None


def delete_rows(items):
    """XOÁ HẲN các dòng khỏi Sheet (không hoàn tác được). items như _resolve_items.
    Xoá từ dòng LỚN → NHỎ trong 1 lần gọi API để không lệch số dòng. Trả về (số dòng, lỗi).
    Chỉ cần 1 người không xác định được đúng dòng là DỪNG CẢ LÔ (xoá nhầm không có đường lùi)."""
    if not items:
        return 0, None
    try:
        ws = _open_ws()
        grid = _retry(ws.get_all_values)
        headers = [h.strip() for h in (grid[0] if grid else [])]
        rows, errors = _resolve_items(grid, headers, items)
        if errors:
            return 0, errors[min(errors)]
        uniq = sorted(set(rows.values()), reverse=True)
        ws.spreadsheet.batch_update({"requests": [
            {"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS",
                                           "startIndex": r - 1, "endIndex": r}}}
            for r in uniq]})
        return len(uniq), None
    except Exception as e:
        return 0, f"Lỗi xoá dòng: {type(e).__name__}: {e}"


def backfill_created(items):
    """Điền NGÀY TẠO LỊCH HẸN KHÁM cho các dòng ĐÃ import từ trước khi có cột này (chỉ ô đang trống, không đè).
    items: list {sheet_row, stt, name, lap('dd/mm/yyyy')}. Trả về (số dòng đã điền, lỗi | None)."""
    ups = [{"sheet_row": it["sheet_row"], "stt": it.get("stt"), "name": it["name"],
            "values": {COL_CREATED: to_dmy(it["lap"])}} for it in items if to_dmy(it.get("lap"))]
    done, errors, fatal = write_cells_ex(ups, raw=True, fill_only=True)
    if fatal:
        return 0, fatal
    if errors:
        first = errors[min(errors)]
        return len(done), first + (f" (và {len(errors) - 1} ca khác cũng lỗi)" if len(errors) > 1 else "")
    return len(done), None
