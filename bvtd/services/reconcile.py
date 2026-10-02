"""
services/reconcile.py
─────────────────────
ĐỐI CHIẾU TÁI KHÁM + LỌC TRÙNG — port y hệt thuật toán từ bản Streamlit.

  1. reconcile_attendance      : bệnh nhân đã hẹn (Sheet) ↔ nhật ký khám thực tế (file 01-1)
                                 3 tầng: Tên+SĐT → Tên+Năm sinh(±1) → chỉ Tên (cần xem tay)
  2. find_duplicate_tk_form    : dòng "tái khám (từ khoa)" ↔ dòng "đăng ký Form" trùng nhau

Nguyên tắc so khớp (v3): MỘT NGƯỜI = TÊN giống + ÍT NHẤT 1 bằng chứng độc lập (SĐT / năm sinh), và
KHÔNG có mâu thuẫn (năm sinh lệch > 1, khác giới tính). Tên chỉ "gần giống" thì không bao giờ được
xếp vào nhóm khớp chắc chắn. Xem compare_person().

Chỉ chứa logic thuần (không gọi Google Sheet) để dễ test. Việc ghi Sheet nằm ở sheets.py.
"""
import difflib, json, os, re, tempfile, time
from functools import lru_cache
from datetime import date, datetime, timedelta

import pandas as pd

from services.sheets import (
    COL_NAME, COL_PHONE, COL_BIRTH_YEAR, COL_SOURCE, COL_STATUS, COL_STT, COL_GENDER,
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


# ══════════════════════════════════════════════════════════════════════════════
# LÕI SO KHỚP "CÙNG MỘT NGƯỜI" (dùng chung cho đối chiếu tái khám + lọc trùng)
# ══════════════════════════════════════════════════════════════════════════════
# Vì sao viết lại: bản cũ coi 2 tên là 1 người khi "từ đầu + từ cuối trùng và độ giống chuỗi ≥ 92%".
# Với tên 3 từ, đổi 1 chữ ở tên đệm vẫn đạt ~93% — vd. "NGUYỄN THẾ HUẤN" (nam, 1956) bị coi là
# "NGUYỄN THỊ HUẤN" (nữ, 1964). Bản mới: so TỪNG TỪ, và bắt buộc có bằng chứng độc lập + không mâu thuẫn.
_norm = lru_cache(maxsize=200_000)(norm_name)


@lru_cache(maxsize=None)
def _parse_dmy_cached(s):
    try:
        return datetime.strptime(str(s).strip(), "%d/%m/%Y").date()
    except Exception:
        return None


def _name_key(na):
    """Khoá ngăn: (từ đầu, từ cuối) của tên ĐÃ chuẩn hoá; tên rỗng → None. Mọi kiểu 'cùng tên' đều
    đòi từ đầu + từ cuối trùng, nên chỉ cần so trong cùng ngăn (nhanh, không đổi kết quả)."""
    t = na.split()
    return (t[0], t[-1]) if t else None


def valid_phone(s) -> str:
    """SĐT dùng làm BẰNG CHỨNG: chuẩn hoá, đổi +84 → 0, chỉ nhận số bắt đầu bằng 0 dài 10–11 chữ số.
    Loại '0', 'N/A', số rác như 3997393303 (Excel làm mất số 0 + sai số) để không 'trùng SĐT' oan."""
    k = norm_phone_key(s)
    if k.startswith("84") and len(k) in (11, 12):
        k = "0" + k[2:]
    return k if re.fullmatch(r"0\d{9,10}", k) else ""


def birth_int(v):
    """Năm sinh hợp lệ (1900…năm nay+1) từ '1956' hoặc '01/01/1956'; không có → None."""
    m = re.search(r"(?:19|20)\d{2}", str(v or ""))
    if not m:
        return None
    y = int(m.group())
    return y if 1900 <= y <= date.today().year + 1 else None


def gender_key(v) -> str:
    """'Nam' → 'M', 'Nữ' → 'F', còn lại ('N/A', trống) → ''."""
    s = _norm(v)
    return "M" if s.startswith("NAM") else ("F" if s.startswith("NU") else "")


def _lev1(x, y) -> bool:
    """2 từ khác nhau đúng 1 ký tự (thay / thêm / bớt)."""
    if x == y:
        return True
    lx, ly = len(x), len(y)
    if abs(lx - ly) > 1:
        return False
    if lx == ly:
        return sum(a != b for a, b in zip(x, y)) == 1
    if lx > ly:
        x, y = y, x
    i = 0
    while i < len(x) and x[i] == y[i]:
        i += 1
    return x[i:] == y[i + 1:]


@lru_cache(maxsize=500_000)
def name_relation(na, nb):
    """Quan hệ giữa 2 tên ĐÃ chuẩn hoá (bỏ dấu, IN HOA). Trả về (kiểu, tỉ lệ giống chuỗi):
      'exact'   — trùng hoàn toàn
      'typo'    — cùng số từ, từ đầu & từ cuối trùng, mỗi từ đệm trùng HOẶC chỉ sai 1 ký tự và dài ≥ 4
                  (từ ngắn ≤ 3 ký tự như THỊ/THẾ/VĂN/VINH… sai 1 ký tự là NGƯỜI KHÁC)
      'partial' — thiếu/thừa tên đệm (từ đầu & cuối trùng, các từ của tên ngắn nằm trong tên dài đúng thứ tự)
      None      — khác người
    """
    if not na or not nb:
        return None, 0.0
    if na == nb:
        return "exact", 1.0
    ta, tb = na.split(), nb.split()
    if ta[0] != tb[0] or ta[-1] != tb[-1]:
        return None, 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    if len(ta) == len(tb):
        ok = all(x == y or (len(x) >= 4 and len(y) >= 4 and _lev1(x, y))
                 for x, y in zip(ta[1:-1], tb[1:-1]))
        return ("typo", ratio) if ok else (None, ratio)
    if abs(len(ta) - len(tb)) <= 2:
        short, long_ = (ta, tb) if len(ta) < len(tb) else (tb, ta)
        it = iter(long_)
        if all(tok in it for tok in short):               # tên ngắn là dãy con của tên dài
            return "partial", ratio
    return None, ratio


def person_view(rec: dict, kind: str = "sheet") -> dict:
    """Chuẩn hoá 1 bản ghi (bệnh nhân trên Sheet hoặc lượt khám trong log Minh Lộ) về cùng 1 dạng."""
    if kind == "visit":
        name, phone = rec.get("HỌ TÊN"), rec.get("SỐ ĐIỆN THOẠI")
        birth = birth_int(rec.get("NĂM SINH")) or birth_int(rec.get("NGÀY SINH"))
        gender = rec.get("GIỚI TÍNH")
    else:
        name, phone = rec.get("name"), rec.get("phone")
        birth, gender = birth_int(rec.get("birth_year")), rec.get("gender")
    nn = _norm(name)
    return {"name": str(name or "").strip(), "nn": nn, "key": _name_key(nn),
            "phone": valid_phone(phone), "birth": birth, "gender": gender_key(gender)}


def compare_person(a: dict, b: dict):
    """So 2 `person_view`. Trả về None nếu KHÔNG phải cùng người, ngược lại dict:
         tier  1 = Tên (trùng hẳn) + SĐT       → khớp chắc chắn
               2 = Tên (trùng hẳn) + năm sinh lệch ≤ 1 → khớp chắc chắn
               3 = khớp yếu → LUÔN cần người xem
         score (0–100, độ giống tên) · rel · notes (lý do, hiển thị cho người xem)
    Quy tắc:
      • Tên khác người (name_relation=None) → loại.
      • MÂU THUẪN (năm sinh lệch > 1 · khác giới tính) → loại, trừ khi tên trùng hẳn VÀ SĐT trùng
        (khi đó hạ xuống tier 3 kèm cảnh báo — có thể nhập sai năm sinh, hoặc cha/con trùng tên chung SĐT).
      • Tên chỉ 'gần giống' (typo/partial) → cần SĐT trùng hoặc năm sinh ±1; và chỉ được tier 3.
      • Tên trùng hẳn nhưng không có bằng chứng nào khác → tier 3 ("chỉ khớp tên").
    """
    rel, ratio = name_relation(a["nn"], b["nn"])
    if rel is None:
        return None
    phone_eq = bool(a["phone"] and b["phone"] and a["phone"] == b["phone"])
    phone_ne = bool(a["phone"] and b["phone"] and a["phone"] != b["phone"])
    diff = abs(a["birth"] - b["birth"]) if (a["birth"] and b["birth"]) else None
    g_conflict = bool(a["gender"] and b["gender"] and a["gender"] != b["gender"])

    conflicts = []
    if diff is not None and diff > 1:
        conflicts.append(f"năm sinh lệch {diff} năm ({a['birth']} ≠ {b['birth']})")
    if g_conflict:
        conflicts.append("khác giới tính")

    notes = []
    if rel == "typo":
        notes.append(f"Tên lệch nhẹ: «{a['name']}» ↔ «{b['name']}»")
    elif rel == "partial":
        notes.append(f"Tên thiếu/thừa tên đệm: «{a['name']}» ↔ «{b['name']}»")
    score = round(100 * ratio, 1)

    if conflicts:
        if rel == "exact" and phone_eq:
            notes.append("⚠️ SĐT trùng nhưng " + " và ".join(conflicts) + " — có thể là 2 người khác nhau")
            return {"tier": 3, "score": score, "rel": rel, "notes": notes}
        return None

    corroborated = phone_eq or (diff is not None and diff <= 1)
    if rel == "exact":
        tier = 1 if phone_eq else (2 if diff is not None and diff <= 1 else 3)
        if tier == 3:
            notes.append("Chỉ khớp tên — không có SĐT/năm sinh để xác nhận")
    else:
        if not corroborated:
            return None                       # tên gần giống mà không có bằng chứng nào → coi là người khác
        tier = 3
        notes.append("Đã khớp " + ("SĐT" if phone_eq else f"năm sinh (lệch {diff})") + " nhưng tên chưa trùng hẳn")
    if phone_ne:
        notes.append(f"SĐT khác nhau ({a['phone']} ≠ {b['phone']})")
    return {"tier": tier, "score": score, "rel": rel, "notes": notes}


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
    """Dòng 'tái khám (từ khoa)' ↔ dòng 'đăng ký Form' trùng nhau. Mỗi dòng chỉ thuộc tối đa 1 cặp.
    Chỉ ghép cặp có ngày hẹn lệch ≤ window_days VÀ compare_person() chấp nhận (xem lõi so khớp).
    Ghép TOÀN CỤC: cặp chắc chắn nhất (tier thấp, ngày gần, tên giống) được chọn trước — kết quả
    không còn phụ thuộc thứ tự dòng trên Sheet như bản cũ."""
    forms = [person_view(pf) for pf in form_patients]
    buckets = {}
    for j, fv in enumerate(forms):
        if fv["key"]:
            buckets.setdefault(fv["key"], []).append(j)

    cand = []                                              # (tier, |lệch ngày|, -score, i, j, cmp)
    for i, pk in enumerate(khoa_patients):
        kv = person_view(pk)
        if not kv["key"]:
            continue
        k_date = pk.get("exam_date")
        for j in buckets.get(kv["key"], ()):
            f_date = form_patients[j].get("exam_date")
            gap = abs((f_date - k_date).days) if (k_date and f_date) else None
            if gap is not None and gap > window_days:
                continue
            c = compare_person(kv, forms[j])
            if c:
                cand.append((c["tier"], gap if gap is not None else 99, -c["score"], i, j, c))
    cand.sort(key=lambda t: t[:5])

    used_k, used_f, results = set(), set(), []
    for tier, _gap, _ns, i, j, c in cand:
        if i in used_k or j in used_f:
            continue
        used_k.add(i); used_f.add(j)
        pk, pf = khoa_patients[i], form_patients[j]
        day_diff = (pf["exam_date"] - pk["exam_date"]).days if (pk.get("exam_date") and pf.get("exam_date")) else None
        results.append((i, {"khoa": pk, "form": pf, "match_tier": tier, "score": c["score"],
                            "day_diff": day_diff, "notes": c["notes"], "rel": c["rel"]}))
    results.sort(key=lambda t: t[0])
    return [r for _, r in results]


# ══════════════════════════════════════════════════════════════════════════════
# 2) ĐỐI CHIẾU TÁI KHÁM — ĐÃ HẸN ↔ THỰC TẾ ĐẾN KHÁM
# ══════════════════════════════════════════════════════════════════════════════
def reconcile_attendance(sheet_patients, visit_records,
                         window_before=RECONCILE_WINDOW_BEFORE,
                         window_after=RECONCILE_WINDOW_AFTER, today=None):
    """Với BN có ngày hẹn D, cửa sổ = [D − before, min(D + after, HÔM NAY)] (chặn ở hôm nay
    để không vồ nhầm một đợt khám CŨ của cùng bệnh nhân).
    status: attended_sure (tier 1/2) · attended_unsure (tier 3, cần xem tay) · not_attended
    (kèm near_miss nếu có lượt khám CÙNG NGƯỜI nhưng NGOÀI cửa sổ ngày).
    Cùng 1 người = compare_person(): tên + bằng chứng + không mâu thuẫn (xem lõi so khớp).

    Hiệu năng: gom nhật ký khám vào ngăn theo (từ đầu, từ cuối) của tên — mỗi bệnh nhân chỉ so với
    vài chục dòng thay vì cả chục nghìn."""
    today_d = today or date.today()

    buckets = {}                                       # (từ đầu, từ cuối) → [(ngày, bản_ghi, person_view)]
    for v in visit_records:
        pv = person_view(v, "visit")
        if pv["key"]:
            buckets.setdefault(pv["key"], []).append((_parse_dmy_cached(v.get("NGÀY ĐK")), v, pv))

    results = []
    for p in sheet_patients:
        pp = person_view(p)
        exam_date = p.get("exam_date")
        bucket = buckets.get(pp["key"], ()) if pp["key"] else ()

        entry = {
            "sheet_row": p.get("sheet_row"), "stt": p.get("stt", ""), "name": p.get("name", ""),
            "phone": p.get("phone", ""), "age": p.get("age", ""),
            "birth_year": p.get("birth_year", ""), "exam_date": exam_date,
            "source": p.get("source", ""), "status_now": p.get("status_now", ""),
            "visit": None, "score": 0.0, "match_tier": None, "near_miss": None, "match_notes": [],
        }

        best = None                                    # (tier, -score, v, cmp)
        if exam_date:                                  # không rõ ngày hẹn → không đủ an toàn để khớp
            w_start = exam_date - timedelta(days=window_before)
            w_end = min(exam_date + timedelta(days=window_after), today_d)
            for vd, v, pv in bucket:
                if vd is None or not (w_start <= vd <= w_end):
                    continue
                c = compare_person(pp, pv)
                if c and (best is None or (c["tier"], -c["score"]) < best[:2]):
                    best = (c["tier"], -c["score"], v, c)

        if best:
            c = best[3]
            entry.update(visit=best[2], score=c["score"], match_tier=c["tier"], match_notes=c["notes"],
                         status="attended_sure" if c["tier"] <= 2 else "attended_unsure")
        else:
            entry.update(score=0.0, status="not_attended")
            near = None                                # "nghi bị sót": cùng người nhưng ngoài cửa sổ ngày
            for vd, v, pv in bucket:
                c = compare_person(pp, pv)
                if c and (near is None or (c["tier"], -c["score"]) < near[:2]):
                    near = (c["tier"], -c["score"], v, vd, c)
            if near:
                entry["near_miss"] = {"visit": near[2], "score": near[4]["score"],
                                      "visit_date": near[3], "notes": near[4]["notes"]}
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
        "gender": row.get(COL_GENDER, "") if COL_GENDER in row.index else "",
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
        # Cả 2 đã khám: chỉ kết luận "đếm trùng" khi khớp CHẮC (tier 1/2). Khớp yếu → có thể là 2 người / 2 lượt thật.
        if k_att and f_att:
            d["severity"] = "critical" if d["match_tier"] <= 2 else "review"
        else:
            d["severity"] = "leftover" if (k_att or f_att) else "normal"
        # Dòng Form (sắp xoá) đã khám mà dòng tái khám (giữ lại) chưa → chuyển trạng thái trước khi xoá
        d["needs_status_transfer"] = f_att and not k_att
    rank = {"critical": 0, "review": 1, "leftover": 2, "normal": 3}
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
