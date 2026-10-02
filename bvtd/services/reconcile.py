"""
services/reconcile.py
─────────────────────
ĐỐI CHIẾU TÁI KHÁM + LỌC TRÙNG — port y hệt thuật toán từ bản Streamlit.

  1. reconcile_attendance      : bệnh nhân đã hẹn (Sheet) ↔ nhật ký khám thực tế (file 01-1)
                                 3 tầng: Tên+SĐT → Tên+Năm sinh(±1) → chỉ Tên (cần xem tay)
  2. find_duplicate_tk_form    : dòng "tái khám (từ khoa)" ↔ dòng "đăng ký Form" trùng nhau

Chỉ chứa logic thuần (không gọi Google Sheet) để dễ test. Việc ghi Sheet nằm ở sheets.py.
"""
import difflib, json, os, re, tempfile, time
from datetime import date, datetime, timedelta

import pandas as pd

from services.sheets import (
    COL_NAME, COL_PHONE, COL_BIRTH_YEAR, COL_SOURCE, COL_STATUS, COL_STT,
    STATUS_ATTENDED, norm_name, norm_phone_key,
)

RECONCILE_LOOKBACK_DAYS = 7    # danh sách gốc: BN có NGÀY KHÁM trong X ngày gần đây
RECONCILE_WINDOW_BEFORE = 2    # chấp nhận đến SỚM hơn hẹn tối đa 2 ngày
RECONCILE_WINDOW_AFTER  = 3    # và MUỘN hơn hẹn tối đa 3 ngày — nhưng KHÔNG BAO GIỜ vượt quá HÔM NAY
DUPLICATE_WINDOW_DAYS   = 5    # lệch ngày hẹn tối đa giữa dòng tái khám và dòng Form
DUP_TAG_PREFIX          = "⚠️ TRÙNG"   # nhãn trùng do phiên bản cũ để lại
NAME_RATIO_MIN          = 0.92
SOURCE_VANG_LAI         = "BỆNH NHÂN VÃNG LAI"

_COL_AGE = "TUỔI"


# ── Phân loại nguồn ───────────────────────────────────────────────────────────
def blank_source(v) -> bool:
    """NGUỒN trống = bệnh nhân đăng ký online qua Form, chưa gắn nhãn."""
    return str(v or "").strip() in ("", "nan", "N/A", "—", "None")


def patient_kind(source) -> str:
    """'tai_kham' (từ khoa/tái khám/nội trú/xuất viện) hoặc 'vang_lai' (còn lại, kể cả nguồn trống)."""
    s = str(source or "").strip()
    if blank_source(s):
        return "vang_lai"
    sl = s.lower()
    if any(k in sl for k in ["khoa", "tái", "nội trú", "xuất viện", "tai"]):
        return "tai_kham"
    return "vang_lai"


def is_attended(status) -> bool:
    return STATUS_ATTENDED.upper() in str(status or "").upper()


# ── So khớp tên ───────────────────────────────────────────────────────────────
def name_similarity(a, b) -> float:
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def name_match_ok(a, b, min_ratio=NAME_RATIO_MIN):
    """CÙNG 1 NGƯỜI khi: từ đầu (họ) VÀ từ cuối (tên gọi) khớp chính xác sau chuẩn hoá,
    VÀ độ giống toàn chuỗi ≥ min_ratio. Tránh nhầm 'NGUYỄN THỊ ANH' / 'NGUYỄN THỊ HOAN'...
    Trả về (đạt, tỉ lệ)."""
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False, 0.0
    if na == nb:
        return True, 1.0
    ta, tb = na.split(), nb.split()
    if not ta or not tb:
        return False, 0.0
    if ta[0] != tb[0] or ta[-1] != tb[-1]:
        return False, name_similarity(a, b)
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return ratio >= min_ratio, ratio


def parse_dmy(s):
    try:
        return datetime.strptime(str(s).strip(), "%d/%m/%Y").date()
    except Exception:
        return None


def _to_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 1) ĐỐI CHIẾU TRÙNG — TÁI KHÁM (TỪ KHOA) ↔ ĐĂNG KÝ FORM
# ══════════════════════════════════════════════════════════════════════════════
def find_duplicate_tk_form(khoa_patients, form_patients, window_days=DUPLICATE_WINDOW_DAYS):
    """Mỗi dòng Form chỉ ghép tối đa 1 dòng khoa. 3 tầng: Tên+SĐT, Tên+Năm sinh(±1),
    chỉ Tên (cần kiểm tra tay). Chỉ ghép cặp có ngày hẹn lệch ≤ window_days."""
    used_form_idx, results = set(), []

    for pk in khoa_patients:
        k_name = pk.get("name", "")
        k_phone = norm_phone_key(pk.get("phone"))
        k_birth = _to_int(pk.get("birth_year"))
        k_date = pk.get("exam_date")

        candidates = []
        for j, pf in enumerate(form_patients):
            if j in used_form_idx:
                continue
            f_date = pf.get("exam_date")
            if k_date and f_date and abs((f_date - k_date).days) > window_days:
                continue
            ok, ratio = name_match_ok(k_name, pf.get("name", ""))
            if ok:
                candidates.append((ratio, j, pf))
        if not candidates:
            continue

        best = None
        if k_phone:                                               # Tầng 1
            for ratio, j, pf in candidates:
                if norm_phone_key(pf.get("phone")) == k_phone and (best is None or ratio > best[0]):
                    best = (ratio, j, pf, 1)
        if best is None and k_birth is not None:                  # Tầng 2
            for ratio, j, pf in candidates:
                f_birth = _to_int(pf.get("birth_year"))
                if f_birth is not None and abs(f_birth - k_birth) <= 1 and (best is None or ratio > best[0]):
                    best = (ratio, j, pf, 2)
        if best is None:                                          # Tầng 3
            ratio, j, pf = max(candidates, key=lambda c: c[0])
            best = (ratio, j, pf, 3)

        ratio, j, pf, tier = best
        used_form_idx.add(j)
        day_diff = (pf["exam_date"] - k_date).days if (k_date and pf.get("exam_date")) else None
        results.append({"khoa": pk, "form": pf, "match_tier": tier,
                        "score": round(100 * ratio, 1), "day_diff": day_diff})
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 2) ĐỐI CHIẾU TÁI KHÁM — ĐÃ HẸN ↔ THỰC TẾ ĐẾN KHÁM
# ══════════════════════════════════════════════════════════════════════════════
def reconcile_attendance(sheet_patients, visit_records,
                         window_before=RECONCILE_WINDOW_BEFORE,
                         window_after=RECONCILE_WINDOW_AFTER, today=None):
    """Với BN có ngày hẹn D, cửa sổ = [D − before, min(D + after, HÔM NAY)] (chặn ở hôm nay
    để không vồ nhầm một đợt khám CŨ của cùng bệnh nhân).
    status: attended_sure (tầng 1/2) · attended_unsure (tầng 3, cần xem tay) · not_attended
    (kèm near_miss nếu tìm thấy tên giống NGOÀI cửa sổ ngày)."""
    today_d = today or date.today()

    # Parse ngày 1 lần cho cả file (tránh parse lại cho từng bệnh nhân)
    visits = [(parse_dmy(v.get("NGÀY ĐK")), v) for v in visit_records]

    results = []
    for p in sheet_patients:
        p_name = p.get("name", "")
        p_phone_key = norm_phone_key(p.get("phone"))
        p_birth = _to_int(p.get("birth_year"))
        exam_date = p.get("exam_date")

        if exam_date:
            w_start = exam_date - timedelta(days=window_before)
            w_end = min(exam_date + timedelta(days=window_after), today_d)
            candidates = [v for vd, v in visits if vd is not None and w_start <= vd <= w_end]
        else:
            candidates = []     # không rõ ngày hẹn → không đủ an toàn để khớp

        entry = {
            "sheet_row": p.get("sheet_row"), "stt": p.get("stt", ""), "name": p_name,
            "phone": p.get("phone", ""), "age": p.get("age", ""),
            "birth_year": p.get("birth_year", ""), "exam_date": exam_date,
            "source": p.get("source", ""), "status_now": p.get("status_now", ""),
            "visit": None, "score": 0.0, "match_tier": None, "near_miss": None,
        }

        name_sims = []
        for v in candidates:
            ok, ratio = name_match_ok(p_name, v.get("HỌ TÊN"))
            if ok:
                name_sims.append((ratio, v))

        best1 = None                                              # Tầng 1: Tên + SĐT
        if p_phone_key:
            for ns, v in name_sims:
                if norm_phone_key(v.get("SỐ ĐIỆN THOẠI")) == p_phone_key and (best1 is None or ns > best1[0]):
                    best1 = (ns, v)
        if best1:
            entry.update(visit=best1[1], score=round(100 * best1[0], 1), match_tier=1, status="attended_sure")
            results.append(entry)
            continue

        best2 = None                                              # Tầng 2: Tên + Năm sinh ±1
        for ns, v in name_sims:
            v_birth = _to_int(v.get("NĂM SINH"))
            if p_birth is None or v_birth is None:
                continue
            if abs(v_birth - p_birth) <= 1 and (best2 is None or ns > best2[0]):
                best2 = (ns, v)
        if best2:
            entry.update(visit=best2[1], score=round(100 * best2[0], 1), match_tier=2, status="attended_sure")
            results.append(entry)
            continue

        best3 = max(name_sims, key=lambda t: t[0], default=None)  # Tầng 3: chỉ Tên → luôn xem tay
        if best3:
            entry.update(visit=best3[1], score=round(100 * best3[0], 1), match_tier=3, status="attended_unsure")
        else:
            entry.update(score=0.0, status="not_attended")
            near = None                                           # "nghi bị sót": tên giống nhưng ngoài cửa sổ
            for vd, v in visits:
                ok, ratio = name_match_ok(p_name, v.get("HỌ TÊN"))
                if ok and (near is None or ratio > near[0]):
                    near = (ratio, v, vd)
            if near:
                entry["near_miss"] = {"visit": near[1], "score": round(100 * near[0], 1), "visit_date": near[2]}
        results.append(entry)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Dựng danh sách bệnh nhân từ DataFrame (cùng bộ lọc phạm vi như Streamlit)
# ══════════════════════════════════════════════════════════════════════════════
_KHOA_RE = "khoa|tái|nội trú|xuất viện|tai"
_VL_RE   = "vãng lai|vang lai|ngoài|ngoai"


def _row_to_patient(idx, row):
    d = row["_date"].date() if pd.notna(row.get("_date")) else None
    age = row.get(_COL_AGE, "") if _COL_AGE in row.index else ""
    if not str(age).strip() and "_age" in row.index and pd.notna(row.get("_age")):
        age = int(row["_age"])
    return {
        "sheet_row": int(idx) + 2,       # df index 0 = dòng 2 trên Sheet (dòng 1 là tiêu đề)
        "stt": row.get(COL_STT, "") if COL_STT in row.index else "",
        "name": row.get(COL_NAME, ""), "phone": row.get(COL_PHONE, ""),
        "birth_year": row.get(COL_BIRTH_YEAR, ""), "age": age,
        "exam_date": d, "source": row.get(COL_SOURCE, ""),
        "status_now": row.get(COL_STATUS, ""),
    }


def _masks(df):
    src = df[COL_SOURCE].astype(str).str.strip() if COL_SOURCE in df.columns else pd.Series("", index=df.index)
    return (src, src.str.contains(_KHOA_RE, case=False, na=False),
            src.apply(blank_source), src.str.contains(_VL_RE, case=False, na=False))


def build_reconcile_scope(df, today):
    """BN chưa khám trong LOOKBACK ngày gần nhất, nguồn = khoa / trống / vãng lai."""
    empty = ([], {"khoa": 0, "blank": 0, "vl": 0})
    if df.empty or "_date" not in df.columns or COL_SOURCE not in df.columns:
        return empty
    src, m_khoa, m_blank, m_vl = _masks(df)
    start = today - timedelta(days=RECONCILE_LOOKBACK_DAYS)
    status = df[COL_STATUS].astype(str).str.upper() if COL_STATUS in df.columns else pd.Series("", index=df.index)
    scope = df[df["_date"].notna()
               & (df["_date"].dt.date >= start) & (df["_date"].dt.date <= today)
               & ~status.str.contains(STATUS_ATTENDED.upper(), na=False)
               & (m_khoa | m_blank | m_vl)]
    pts = [_row_to_patient(i, r) for i, r in scope.iterrows()]
    cnt = {"khoa": int(m_khoa[scope.index].sum()), "blank": int(m_blank[scope.index].sum()),
           "vl": int(m_vl[scope.index].sum())}
    return pts, cnt


def build_dup_lists(df, today):
    """(khoa_list, form_list, legacy_tagged_rows) — quét bất kể trạng thái đã/chưa khám."""
    if df.empty or "_date" not in df.columns or COL_SOURCE not in df.columns:
        return [], [], []
    src, m_khoa, m_blank, m_vl = _masks(df)
    tagged = src.str.startswith(DUP_TAG_PREFIX)
    m_form = (m_blank | m_vl) & ~tagged
    in_range = df["_date"].notna() & (df["_date"].dt.date >= today - timedelta(days=RECONCILE_LOOKBACK_DAYS))

    def lst(mask):
        return [_row_to_patient(i, r) for i, r in df[mask & in_range].iterrows()]

    legacy = [{"sheet_row": int(i) + 2,
               "stt": r.get(COL_STT, "") if COL_STT in r.index else "",
               "name": r.get(COL_NAME, ""), "source": r.get(COL_SOURCE, "")}
              for i, r in df[tagged].iterrows()]
    return lst(m_khoa), lst(m_form), legacy


def classify_dup_pairs(pairs):
    """Gắn severity + needs_status_transfer cho từng cặp trùng."""
    for d in pairs:
        k_att, f_att = is_attended(d["khoa"].get("status_now")), is_attended(d["form"].get("status_now"))
        d["khoa_attended"], d["form_attended"] = k_att, f_att
        d["severity"] = "critical" if (k_att and f_att) else ("leftover" if (k_att or f_att) else "normal")
        # Dòng Form (sắp xoá) đã khám mà dòng tái khám (giữ lại) chưa → chuyển trạng thái trước khi xoá
        d["needs_status_transfer"] = f_att and not k_att
    rank = {"critical": 0, "leftover": 1, "normal": 2}
    return sorted(pairs, key=lambda x: (rank[x["severity"]], x["match_tier"]))


# ══════════════════════════════════════════════════════════════════════════════
# Lưu tạm trên đĩa (theo token trong session) — không nhét vào cookie
# ══════════════════════════════════════════════════════════════════════════════
_DIR = os.path.join(tempfile.gettempdir(), "bvtd_reconcile")
_TTL = 6 * 3600
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _ser(o):
    """date → 'YYYY-MM-DD' khi dump JSON."""
    if isinstance(o, (date, datetime)):
        return o.isoformat()[:10]
    raise TypeError(type(o).__name__)


def _path(token, kind):
    return os.path.join(_DIR, f"{token}_{kind}.json") if token and _TOKEN_RE.match(token) else None


def _cleanup():
    now = time.time()
    try:
        for fn in os.listdir(_DIR):
            fp = os.path.join(_DIR, fn)
            if now - os.path.getmtime(fp) > _TTL:
                os.remove(fp)
    except OSError:
        pass


def store_save(token, kind, payload):
    os.makedirs(_DIR, exist_ok=True)
    _cleanup()
    fp = _path(token, kind)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=_ser)
    os.replace(tmp, fp)


def store_load(token, kind):
    fp = _path(token, kind)
    try:
        if not fp or time.time() - os.path.getmtime(fp) > _TTL:
            return None
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def store_delete(token, kind):
    fp = _path(token, kind)
    try:
        if fp:
            os.remove(fp)
    except OSError:
        pass


def iso_to_date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
