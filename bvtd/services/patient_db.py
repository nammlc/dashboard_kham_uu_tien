"""
services/patient_db.py — dữ liệu bệnh nhân lưu trong PostgreSQL (thay Google Sheet).

Nguyên tắc: DB là NGUỒN DUY NHẤT. Mọi sửa đổi đi qua các hàm ở đây:
  • giá trị được chuẩn hoá + kiểm tra trước khi ghi (ngày = DATE, trạng thái/giới tính = danh sách cố định, SĐT = chữ số);
  • mỗi lần sửa tăng `version` (khoá lạc quan: 2 người sửa cùng 1 bệnh nhân thì người sau được báo, không ghi đè);
  • mọi thay đổi ghi vào patient_history (ai, lúc nào, cột nào, giá trị cũ → mới);
  • xoá = xoá MỀM (deleted_at), khôi phục được.

Các hàm load_dataframe / update_status / append_field_maps / write_cells_ex / delete_rows có CÙNG chữ ký và kiểu trả về với
bản Google Sheet trong services/sheets.py; khi STORAGE_BACKEND=postgres, sheets.py trỏ các tên đó về đây nên báo cáo, đối chiếu,
nhập Minh Lộ, đăng ký online… chạy y nguyên. Số "dòng" (sheet_row) trong chế độ này chính là patients.id
(DataFrame có index = id − 2 để công thức cũ `index + 2` ra đúng id).
"""
from __future__ import annotations

import contextvars
import json
import re
import time
from contextlib import contextmanager
from datetime import date, datetime

import pandas as pd
from flask import current_app, has_app_context

from services import sheets as S

_TEST_POOL = None                 # chỉ dùng trong test: pool giả (SQLite)
_LOCK_KEY = 727275

CANON_STATUS = {S.status_key(S.STATUS_ATTENDED): S.STATUS_ATTENDED,
                S.status_key(S.STATUS_NOT_ATTENDED): S.STATUS_NOT_ATTENDED}
PLACEHOLDERS = {"", "N/A", "NA", "NAN", "NONE", "-", "—", "\u200b"}


class Conflict(Exception):
    """Bệnh nhân đã bị người khác sửa trong lúc bạn đang mở form."""


class Invalid(ValueError):
    """Dữ liệu nhập không hợp lệ (thông báo hiển thị thẳng cho người dùng)."""


# ══════════════════════════════════════════════════════════════════════════════
# Cột: (tiêu đề trên Sheet, cột DB, kiểu)
# ══════════════════════════════════════════════════════════════════════════════
ADDRESS_HEADER = "2. ĐỊA CHỈ (THÔN/XÃ)"


def fields():
    return [
        (S.COL_STT, "stt", "int"),
        (S.COL_TIMESTAMP, "registered_at", "ts"),
        (S.COL_SOURCE, "source", "text"),
        (S.COL_STATUS, "status", "status"),
        (S.COL_EXAM_DATE, "exam_date", "date"),
        (S.COL_NAME, "full_name", "name"),
        (S.COL_PHONE, "phone", "phone"),
        (S.COL_BIRTH_YEAR, "birth_year", "year"),
        ("TUỔI", "age_text", "text"),
        (S.COL_KHOA, "khoa", "text"),
        (S.COL_GENDER, "gender", "gender"),
        (ADDRESS_HEADER, "address", "text"),
        (S.COL_SYMPTOM, "symptom", "text"),
        (S.COL_CCCD, "cccd", "text"),
        (S.COL_SPECIALTY, "specialty", "text"),
        (S.COL_DOCTOR, "doctor", "text"),
        (S.COL_EXAM_TIME, "exam_time", "text"),
        (S.COL_CONSENT_1, "consent_truth", "text"),
        (S.COL_CONSENT_2, "consent_terms", "text"),
        (S.COL_ACTUAL_DATE, "actual_date", "date"),
        (S.COL_CREATED, "created_date", "date"),
    ]


def _header_map():
    m = {h: (c, k) for h, c, k in fields()}
    m[S.COL_ADDRESS] = m[ADDRESS_HEADER]          # Sheet chỉ có cột "ĐỊA CHỈ" thì vẫn nhận
    return m


DB_COLS = None


def db_cols():
    return [c for _, c, _ in fields()]


# ══════════════════════════════════════════════════════════════════════════════
# Chuẩn hoá từng kiểu giá trị  →  (giá trị đã chuẩn hoá, cảnh báo | None)
# ══════════════════════════════════════════════════════════════════════════════
def _s(v) -> str:
    t = str(v if v is not None else "").replace("\u200b", "").strip()
    if t.startswith("'"):                         # dấu ' ép Sheet lưu dạng chữ — DB không cần
        t = t[1:].strip()
    return t


def _is_placeholder(t: str) -> bool:
    return t.upper() in PLACEHOLDERS


def conv_text(v):
    t = _s(v)
    return ("" if _is_placeholder(t) else re.sub(r"[ \t]+", " ", t)), None


def conv_name(v):
    t = re.sub(r"\s+", " ", _s(v))
    return ("" if _is_placeholder(t) else t), None


def conv_int(v):
    t = _s(v)
    if _is_placeholder(t):
        return None, None
    return (int(t), None) if re.fullmatch(r"\d{1,9}", t) else (None, f"STT không phải số: «{t}»")


def conv_year(v):
    t = _s(v)
    if _is_placeholder(t):
        return None, None
    if re.fullmatch(r"\d{4}", t) and 1900 <= int(t) <= date.today().year + 1:
        return int(t), None
    return None, f"năm sinh không hợp lệ: «{t}»"


def conv_gender(v):
    t = S.norm_name(_s(v))
    if t in ("NAM", "M", "MALE"):
        return "Nam", None
    if t in ("NU", "F", "FEMALE"):
        return "Nữ", None
    if _is_placeholder(_s(v)):
        return None, None
    return None, f"giới tính lạ: «{_s(v)}»"


def conv_status(v):
    t = _s(v)
    if _is_placeholder(t):
        return None, None
    k = S.status_key(t)
    if k in CANON_STATUS:
        return CANON_STATUS[k], None
    return None, f"trạng thái lạ: «{t}»"


def conv_phone(v):
    t = _s(v)
    if _is_placeholder(t):
        return "", None
    digits = re.sub(r"\D", "", t)
    if digits.startswith("84") and len(digits) in (11, 12):
        digits = "0" + digits[2:]
    key = S.norm_phone_key(digits)
    if not key:
        return "", (None if digits and set(digits) <= {"0"} else f"SĐT không hợp lệ: «{t}»")
    warn = None
    if not key.startswith("0") or len(key) not in (10, 11):
        warn = f"SĐT bất thường (không phải số di động 10 số bắt đầu bằng 0): «{t}»"
    return key, warn


def conv_date(v):
    """→ (date | None, cảnh báo). Ô trống/N/A → None không cảnh báo; có chữ mà không hiểu → cảnh báo."""
    if isinstance(v, datetime):
        return v.date(), None
    if isinstance(v, date):
        return v, None
    t = _s(v)
    if _is_placeholder(t):
        return None, None
    d = S.to_dmy(t)
    if d:
        try:
            return datetime.strptime(d, "%d/%m/%Y").date(), None
        except ValueError:
            pass
    try:                                          # số serial của Sheets (ô bị hiện dạng số)
        n = float(t)
        if 40000 < n < 60000:
            return (pd.Timestamp("1899-12-30") + pd.Timedelta(days=int(n))).date(), None
    except ValueError:
        pass
    return None, f"ngày không hiểu: «{t}»"


def conv_ts(v):
    if isinstance(v, datetime):
        return v.replace(microsecond=0), None
    t = _s(v)
    if _is_placeholder(t):
        return None, None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(t, fmt), None
        except ValueError:
            continue
    d = conv_date(t)[0]
    if d:
        return datetime(d.year, d.month, d.day), None
    return None, f"dấu thời gian không hiểu: «{t}»"


CONVERTERS = {"text": conv_text, "name": conv_name, "int": conv_int, "year": conv_year, "gender": conv_gender,
              "status": conv_status, "phone": conv_phone, "date": conv_date, "ts": conv_ts}


def record_to_row(rec: dict, partial: bool = False):
    """{tiêu đề cột: giá trị thô} → (cols, extra, warns).
    cols  : {cột DB: giá trị đã chuẩn hoá}      (partial=True: chỉ các cột có trong rec)
    extra : {tên: giá trị gốc} — cột lạ và giá trị không đọc được (không để mất dữ liệu)
    warns : [mô tả] — các giá trị bị bỏ/đổi, để báo cáo."""
    hm = _header_map()
    cols, extra, warns = {}, {}, []
    for h, raw in rec.items():
        h2 = S.canon_header(h)
        if h2 not in hm:
            if _s(raw) and h2:
                extra[h2] = _s(raw)
            continue
        col, kind = hm[h2]
        if h2 == S.COL_ADDRESS and ADDRESS_HEADER in rec:
            continue
        val, warn = CONVERTERS[kind](raw)
        cols[col] = val
        if warn:
            warns.append(f"{h2}: {warn}")
            extra[f"{col}_goc"] = _s(raw)
    if not partial:
        for _, col, kind in fields():
            if col not in cols:
                cols[col] = CONVERTERS[kind]("")[0]
    return cols, extra, warns


# ══════════════════════════════════════════════════════════════════════════════
# DB → bản ghi kiểu Sheet
# ══════════════════════════════════════════════════════════════════════════════
def _as_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _as_dt(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    t = str(v).replace("T", " ")[:19]
    return datetime.strptime(t, "%Y-%m-%d %H:%M:%S") if len(t) > 10 else datetime.strptime(t, "%Y-%m-%d")


def row_to_record(r) -> dict:
    out = {}
    for h, col, kind in fields():
        v = r[col]
        if v is None:
            s = ""
        elif kind == "date":
            s = _as_date(v).strftime("%d/%m/%Y")
        elif kind == "ts":
            s = _as_dt(v).strftime("%d/%m/%Y %H:%M:%S")
        else:
            s = str(v)
        out[h] = s
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Kết nối
# ══════════════════════════════════════════════════════════════════════════════
def _pool():
    if _TEST_POOL is not None:
        return _TEST_POOL
    from services import db as DB
    return DB.pool(current_app.config["DATABASE_URL"])


@contextmanager
def _conn():
    with _pool().connection() as c:
        yield c


def _lock(c):
    if not getattr(c, "is_sqlite", False):
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))   # chỉ 1 giao dịch ghi/lúc → STT không trùng


_ACTOR = contextvars.ContextVar("patient_db_actor", default="")


@contextmanager
def actor(name: str):
    """Ghi rõ NGUỒN thay đổi trong lịch sử (vd. bệnh nhân tự đăng ký online), bất kể ai đang đăng nhập trình duyệt."""
    tok = _ACTOR.set(name)
    try:
        yield
    finally:
        _ACTOR.reset(tok)


def _actor(default="system") -> str:
    if _ACTOR.get():
        return _ACTOR.get()
    try:
        if has_app_context():
            from flask_login import current_user
            if current_user and getattr(current_user, "is_authenticated", False):
                return str(getattr(current_user, "email", "") or getattr(current_user, "id", "") or default)
    except Exception:
        pass
    return default


def _now():
    return S.vn_now().replace(tzinfo=None, microsecond=0)


def _history(c, pid, action, changes, by):
    c.execute("INSERT INTO patient_history (patient_id, at, by_email, action, changes) VALUES (%s, %s, %s, %s, %s)",
              (pid, _now(), by, action, json.dumps(changes, ensure_ascii=False, default=str)))


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _same(a, b) -> bool:
    """So giá trị cũ (đọc từ DB) với giá trị mới (đã chuẩn hoá). Rỗng = rỗng; ngày so theo ngày; số so theo số."""
    if a in (None, "") or b in (None, ""):
        return a in (None, "") and b in (None, "")
    try:
        if isinstance(b, datetime):
            return _as_dt(a) == b.replace(microsecond=0)
        if isinstance(b, date):
            return _as_date(a) == b
        if isinstance(a, (date, datetime)):
            return _as_date(a) == _as_date(b)
        if isinstance(a, int) or isinstance(b, int):
            return int(a) == int(b)
    except (TypeError, ValueError):
        return False
    return str(a) == str(b)


# ══════════════════════════════════════════════════════════════════════════════
# ĐỌC
# ══════════════════════════════════════════════════════════════════════════════
def load_dataframe() -> pd.DataFrame:
    """Cùng hình dạng với bản Sheet: mọi cột là chuỗi + _date, _created, _ts, _age. Index = id − 2."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM patients WHERE deleted_at IS NULL ORDER BY id").fetchall()
    headers = [h for h, _, _ in fields()]
    if not rows:
        return pd.DataFrame(columns=headers)
    recs = [row_to_record(r) for r in rows]
    df = pd.DataFrame(recs, columns=headers)
    df.index = [int(r["id"]) - 2 for r in rows]
    df["_date"] = pd.to_datetime([_as_date(r["exam_date"]) for r in rows])
    df["_created"] = pd.to_datetime([_as_date(r["created_date"]) for r in rows])
    df["_ts"] = pd.to_datetime([_as_dt(r["registered_at"]) for r in rows])
    cy = datetime.now().year
    df["_age"] = pd.to_numeric(df[S.COL_BIRTH_YEAR], errors="coerce").apply(
        lambda y: int(cy - y) if pd.notna(y) and 1900 < y <= cy else None)
    return df


def get_patient(pid: int):
    with _conn() as c:
        r = c.execute("SELECT * FROM patients WHERE id = %s AND deleted_at IS NULL", (int(pid),)).fetchone()
    return dict(r) if r else None


def get_history(pid: int, limit: int = 100):
    with _conn() as c:
        rows = c.execute("SELECT * FROM patient_history WHERE patient_id = %s ORDER BY id DESC LIMIT %s",
                         (int(pid), limit)).fetchall()
    out = []
    for r in rows:
        try:
            ch = json.loads(r["changes"] or "{}")
        except ValueError:
            ch = {}
        out.append({"at": _as_dt(r["at"]), "by": r["by_email"], "action": r["action"], "changes": ch})
    return out


# ══════════════════════════════════════════════════════════════════════════════
# GHI — cùng chữ ký/kiểu trả về như sheets.py
# ══════════════════════════════════════════════════════════════════════════════
def _insert(c, cols: dict, extra: dict, by: str, legacy_row=None, action="insert", keep_ts=True):
    now = _now()
    if cols.get("registered_at") is None and keep_ts:
        cols = dict(cols, registered_at=now)
    names = list(cols) + ["extra", "legacy_row", "version", "created_at", "updated_at", "updated_by"]
    vals = list(cols.values()) + [json.dumps(extra, ensure_ascii=False), legacy_row, 1, now, now, by]
    q = f"INSERT INTO patients ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) RETURNING id"
    pid = c.execute(q, vals).fetchone()["id"]
    _history(c, pid, action, {"_new": True, "full_name": cols.get("full_name")}, by)
    return pid


def append_field_maps(maps):
    """Thêm bệnh nhân từ danh sách {tiêu đề cột: giá trị}. Tất cả hoặc không gì cả (1 giao dịch).
    Trả về (số dòng đã ghi, lỗi | None)."""
    if not maps:
        return 0, "Không có dữ liệu để ghi."
    by = _actor("dang-ky-online")
    try:
        prepared = []
        for fm in maps:
            cols, extra, warns = record_to_row(fm)
            if not cols.get("full_name"):
                return 0, "Thiếu họ tên bệnh nhân — không ghi."
            prepared.append((cols, extra))
        with _conn() as c:
            _lock(c)
            nxt = int(c.execute("SELECT COALESCE(MAX(stt), 0) AS m FROM patients").fetchone()["m"] or 0)
            for cols, extra in prepared:
                if cols.get("stt") is None:
                    nxt += 1
                    cols["stt"] = nxt
                _insert(c, cols, extra, by)
        return len(prepared), None
    except Exception as e:
        return 0, _friendly(e, "ghi vào cơ sở dữ liệu")


def _friendly(e, action):
    name = type(e).__name__
    return f"Lỗi {action}: {name}: {e}"


def _resolve(c, items):
    """items: [{sheet_row, stt, name}] → ({chỉ_số: id}, {chỉ_số: lỗi}). Chỉ chọn dòng ĐÚNG bệnh nhân:
    thử id (sheet_row) rồi tới STT, và luôn đối chiếu lại TÊN (có dấu thanh) để không ghi nhầm người."""
    rows, errs = {}, {}
    for i, it in enumerate(items):
        stt = str(it.get("stt") or "").strip()
        expect = S.acc_name(it.get("name", ""))
        try:
            hint = int(it["sheet_row"])
        except (KeyError, TypeError, ValueError):
            hint = -1

        def ok(row):
            return (not expect) or S.acc_name(row["full_name"]) == expect

        picked = None
        if hint > 0:
            r = c.execute("SELECT id, full_name, stt FROM patients WHERE id = %s AND deleted_at IS NULL", (hint,)).fetchone()
            if r and ok(r) and (not stt or r["stt"] is None or str(r["stt"]) == stt):
                picked = r["id"]
        if picked is None and stt.isdigit():
            cands = [r for r in c.execute("SELECT id, full_name FROM patients WHERE stt = %s AND deleted_at IS NULL",
                                          (int(stt),)).fetchall() if ok(r)]
            if len(cands) == 1:
                picked = cands[0]["id"]
            elif len(cands) > 1:
                errs[i] = f"Có {len(cands)} bệnh nhân trùng STT {stt} và trùng tên «{it.get('name', '')}» — hãy xử lý tay."
                continue
        if picked is None:
            errs[i] = (f"Không tìm thấy bệnh nhân «{it.get('name', '')}» (STT {stt or '—'}) — có thể đã bị xoá hoặc sửa tên. "
                       "Bấm Làm mới rồi đối chiếu lại.")
            continue
        rows[i] = picked
    return rows, errs


def _apply_update(c, pid, cols: dict, by: str, expect_version=None, fill_only=False, action="update", extra_add=None):
    """Ghi các cột đã chuẩn hoá. Trả về dict thay đổi {cột: [cũ, mới]} ({} nếu không có gì đổi)."""
    cur = c.execute("SELECT * FROM patients WHERE id = %s AND deleted_at IS NULL", (pid,)).fetchone()
    if cur is None:
        raise Invalid("Bệnh nhân không còn tồn tại (đã bị xoá).")
    if expect_version is not None and int(cur["version"]) != int(expect_version):
        raise Conflict("Bệnh nhân này vừa được người khác sửa. Hãy mở lại để xem dữ liệu mới nhất.")
    changes = {}
    for col, new in cols.items():
        old = cur[col]
        if fill_only and old not in (None, ""):
            continue
        if _same(old, new):
            continue
        changes[col] = [_jsonable(old), _jsonable(new)]
    extra = json.loads(cur["extra"] or "{}")
    if extra_add:
        extra.update(extra_add)
    if not changes and not extra_add:
        return {}
    sets = [f"{col} = %s" for col in cols if col in changes]
    vals = [cols[col] for col in cols if col in changes]
    sets += ["extra = %s", "version = version + 1", "updated_at = %s", "updated_by = %s"]
    vals += [json.dumps(extra, ensure_ascii=False), _now(), by]
    where = "id = %s AND deleted_at IS NULL"
    vals.append(pid)
    if expect_version is not None:
        where += " AND version = %s"
        vals.append(int(expect_version))
    n = c.execute(f"UPDATE patients SET {', '.join(sets)} WHERE {where}", vals).rowcount
    if n != 1:
        raise Conflict("Bệnh nhân này vừa được người khác sửa. Hãy mở lại để xem dữ liệu mới nhất.")
    _history(c, pid, action, changes, by)
    return changes


def write_cells_ex(updates, raw=False, verify=True, fill_only=False):
    """updates: [{sheet_row, stt, name, values: {tiêu đề cột: giá trị}}] → (done, errors, fatal) như bản Sheet.
    fill_only=True: chỉ ghi vào ô đang trống. Lỗi 1 người không làm hỏng người khác."""
    if not updates:
        return set(), {}, None
    by = _actor()
    done, errors = set(), {}
    try:
        with _conn() as c:
            _lock(c)
            rows, errors = _resolve(c, updates)
            for i, pid in rows.items():
                known = [h for h in updates[i]["values"] if S.canon_header(h) in _header_map()]
                if not known:
                    errors[i] = "Không tìm thấy cột nào khớp trong cơ sở dữ liệu."
                    continue
                cols, extra, warns = record_to_row({h: updates[i]["values"][h] for h in known}, partial=True)
                if warns:
                    errors[i] = "Giá trị không hợp lệ, chưa ghi: " + "; ".join(w.split(": ", 1)[-1] for w in warns)
                    continue
                try:
                    _apply_update(c, pid, cols, by, fill_only=fill_only)
                    done.add(i)
                except (Conflict, Invalid) as e:
                    errors[i] = str(e)
        return done, errors, None
    except Exception as e:
        return set(), {}, _friendly(e, "ghi vào cơ sở dữ liệu")


def update_status(row_number: int, new_status: str) -> bool:
    """row_number = chỉ số dòng dữ liệu bắt đầu từ 1 (giống bản Sheet) → patients.id = row_number + 1."""
    val, warn = conv_status(new_status)
    if val is None:
        return False
    try:
        with _conn() as c:
            _lock(c)
            _apply_update(c, int(row_number) + 1, {"status": val}, _actor())
        return True
    except Exception as e:
        if has_app_context():
            current_app.logger.error(f"update_status (db) lỗi: {e}")
        return False


def delete_rows(items):
    """XOÁ MỀM. Một người không xác định được là DỪNG CẢ LÔ (giống bản Sheet). Trả về (số dòng, lỗi | None)."""
    if not items:
        return 0, None
    by = _actor()
    try:
        with _conn() as c:
            _lock(c)
            rows, errs = _resolve(c, items)
            if errs:
                return 0, errs[min(errs)]
            n = 0
            for pid in sorted(set(rows.values())):
                cur = c.execute("SELECT * FROM patients WHERE id = %s AND deleted_at IS NULL", (pid,)).fetchone()
                if cur is None:
                    continue
                c.execute("UPDATE patients SET deleted_at = %s, deleted_by = %s, version = version + 1, "
                          "updated_at = %s, updated_by = %s WHERE id = %s", (_now(), by, _now(), by, pid))
                _history(c, pid, "delete", {"snapshot": {k: _jsonable(cur[k]) for k in db_cols()}}, by)
                n += 1
        return n, None
    except Exception as e:
        return 0, _friendly(e, "xoá")


def restore_patient(pid: int) -> bool:
    by = _actor()
    with _conn() as c:
        _lock(c)
        n = c.execute("UPDATE patients SET deleted_at = NULL, deleted_by = '', version = version + 1, updated_at = %s, "
                      "updated_by = %s WHERE id = %s AND deleted_at IS NOT NULL", (_now(), by, int(pid))).rowcount
        if n:
            _history(c, int(pid), "restore", {}, by)
    return bool(n)


# ── Form web: thêm / sửa 1 bệnh nhân ─────────────────────────────────────────────────────────────────────
FORM_FIELDS = {          # tên input trong form → tiêu đề cột
    "full_name": S.COL_NAME, "phone": S.COL_PHONE, "birth_year": S.COL_BIRTH_YEAR, "gender": S.COL_GENDER,
    "address": ADDRESS_HEADER, "source": S.COL_SOURCE, "khoa": S.COL_KHOA, "exam_date": S.COL_EXAM_DATE,
    "exam_time": S.COL_EXAM_TIME, "symptom": S.COL_SYMPTOM, "specialty": S.COL_SPECIALTY, "doctor": S.COL_DOCTOR,
    "status": S.COL_STATUS, "cccd": S.COL_CCCD,
}


def validate_form(form: dict, partial: bool = False):
    """form: {tên input: chuỗi}. Trả về (cols, extra, lỗi_list). Ngày nhận dd/mm/yyyy hoặc yyyy-mm-dd (input type=date)."""
    rec = {FORM_FIELDS[k]: v for k, v in form.items() if k in FORM_FIELDS}
    cols, extra, warns = record_to_row(rec, partial=partial)
    errs = []
    if (not partial or "full_name" in form) and not cols.get("full_name"):
        errs.append("Họ tên không được để trống.")
    if (not partial or "exam_date" in form) and not cols.get("exam_date") and not any("ngày" in w for w in warns):
        errs.append("Ngày khám không được để trống.")
    for w in warns:
        msg = w.split(": ", 1)[-1]
        errs.append(msg[:1].upper() + msg[1:])
    return cols, extra, errs


def create_patient(form: dict, by: str | None = None) -> int:
    cols, extra, errs = validate_form(form)
    if errs:
        raise Invalid("; ".join(errs))
    by = by or _actor()
    with _conn() as c:
        _lock(c)
        nxt = int(c.execute("SELECT COALESCE(MAX(stt), 0) AS m FROM patients").fetchone()["m"] or 0)
        cols["stt"] = nxt + 1
        if cols.get("registered_at") is None:
            cols["registered_at"] = _now()
        if cols.get("created_date") is None:
            cols["created_date"] = _now().date()
        return _insert(c, cols, extra, by)


def update_patient(pid: int, form: dict, version: int, by: str | None = None) -> dict:
    cols, extra, errs = validate_form(form, partial=True)
    if errs:
        raise Invalid("; ".join(errs))
    by = by or _actor()
    with _conn() as c:
        _lock(c)
        return _apply_update(c, int(pid), cols, by, expect_version=version)


# ══════════════════════════════════════════════════════════════════════════════
# NHẬP TỪ GOOGLE SHEET  (chạy bằng:  flask --app app import-sheet)
# ══════════════════════════════════════════════════════════════════════════════
def read_sheet_records():
    """Đọc cả Sheet → [(số dòng trên Sheet, {tiêu đề chuẩn: giá trị chuỗi})], bỏ dòng hoàn toàn trống."""
    ws = S._open_ws(fresh=True)
    grid = S._retry(ws.get_all_values, deadline=time.time() + 120)
    if not grid:
        return [], []
    raw_headers = [S.canon_header(h) for h in grid[0]]
    seen, keep, headers = {}, [], []
    for i, h in enumerate(raw_headers):
        if not h:
            continue
        if h in seen:
            seen[h] += 1
            h = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
        keep.append(i)
        headers.append(h)
    out = []
    for n, r in enumerate(grid[1:], start=2):
        rec = {h: (r[i] if i < len(r) else "") for h, i in zip(headers, keep)}
        if any(str(v).strip() for v in rec.values()):
            out.append((n, rec))
    return out, headers


def plan_import(records):
    """[(dòng, rec)] → (kế hoạch, báo cáo). Không chạm DB."""
    plan, warn_by_kind, examples = [], {}, {}
    unknown_cols, stt_seen, dup_stt = {}, {}, set()
    n_no_name = n_no_date = 0
    status_dist, source_dist, by_month = {}, {}, {}
    for n, rec in records:
        cols, extra, warns = record_to_row(rec)
        if not cols["full_name"]:
            n_no_name += 1
            warns.append("Họ tên: trống")
        if cols["exam_date"] is None:
            n_no_date += 1
        stt = cols["stt"]
        if stt is not None:
            if stt in stt_seen:
                dup_stt.add(stt)
            stt_seen[stt] = n
        for w in warns:
            kind = w.split(":")[0] + ": " + re.sub(r"«.*?»", "«…»", w.split(": ", 1)[-1])
            warn_by_kind[kind] = warn_by_kind.get(kind, 0) + 1
            examples.setdefault(kind, []).append(f"dòng {n}: {w}")
        for k in extra:
            if not k.endswith("_goc"):
                unknown_cols[k] = unknown_cols.get(k, 0) + 1
        status_dist[cols["status"] or "(trống)"] = status_dist.get(cols["status"] or "(trống)", 0) + 1
        src = cols["source"] or "(trống)"
        source_dist[src] = source_dist.get(src, 0) + 1
        if cols["exam_date"]:
            m = cols["exam_date"].strftime("%Y-%m")
            by_month[m] = by_month.get(m, 0) + 1
        plan.append({"legacy_row": n, "cols": cols, "extra": extra, "warns": warns})
    report = {
        "rows": len(plan), "no_name": n_no_name, "no_exam_date": n_no_date,
        "dup_stt": sorted(dup_stt)[:20], "n_dup_stt": len(dup_stt),
        "unknown_columns": unknown_cols, "status": status_dist, "source": source_dist,
        "by_month": dict(sorted(by_month.items())),
        "warnings": {k: {"count": v, "examples": examples[k][:3]} for k, v in sorted(warn_by_kind.items(), key=lambda t: -t[1])},
    }
    return plan, report


def _row_signature(cols: dict) -> tuple:
    return tuple((k, (_jsonable(v) if v not in (None, "") else None)) for k, v in sorted(cols.items()))


def db_signature(r) -> tuple:
    cols = {c: r[c] for c in db_cols()}
    for c in ("exam_date", "actual_date", "created_date"):
        cols[c] = _as_date(cols[c])
    cols["registered_at"] = _as_dt(cols["registered_at"])
    return _row_signature(cols)


def run_import(plan, commit=False, resync=False, by="import"):
    """Ghi kế hoạch vào DB. Mặc định chỉ chạy khi bảng còn TRỐNG. resync=True: thêm dòng mới + cập nhật dòng CHƯA từng
    sửa trong DB (version = 1); dòng đã sửa trong DB không bị đụng tới. Trả về thống kê."""
    stats = {"insert": 0, "update": 0, "same": 0, "skipped_edited": 0, "only_in_db": 0}
    with _conn() as c:
        _lock(c)
        existing = {r["legacy_row"]: r for r in c.execute(
            "SELECT * FROM patients WHERE legacy_row IS NOT NULL").fetchall()}
        total_db = c.execute("SELECT COUNT(*) AS n FROM patients").fetchone()["n"]
        if commit and total_db and not resync:
            raise Invalid(f"Bảng patients đã có {total_db} dòng. Dùng --resync để cập nhật những dòng CHƯA sửa trong DB, "
                          "hoặc xoá hết bảng nếu muốn nhập lại từ đầu.")
        planned_rows = set()
        for p in plan:
            planned_rows.add(p["legacy_row"])
            cur = existing.get(p["legacy_row"])
            if cur is None:
                stats["insert"] += 1
                if commit:
                    _insert(c, dict(p["cols"]), p["extra"], by, legacy_row=p["legacy_row"], action="import", keep_ts=False)
            elif _row_signature(p["cols"]) == db_signature(cur):
                stats["same"] += 1
            elif int(cur["version"]) > 1 or cur["deleted_at"] is not None:
                stats["skipped_edited"] += 1
            else:
                stats["update"] += 1
                if commit:
                    _apply_update(c, cur["id"], {k: v for k, v in p["cols"].items()}, by, action="import")
        stats["only_in_db"] = sum(1 for k in existing if k not in planned_rows)
    return stats


def export_to_sheet_mirror(tab=None):
    """Ghi 1 CHIỀU DB → tab bản sao trên Google Sheet (để xem/báo cáo quen thuộc). KHÔNG BAO GIỜ ghi vào tab gốc."""
    import gspread
    tab = (tab or current_app.config.get("SHEET_MIRROR_NAME") or "Bản sao từ CSDL (chỉ đọc)").strip()
    main = S._open_ws(fresh=True)
    if tab == main.title:
        raise Invalid("Tên tab bản sao trùng với tab dữ liệu gốc — từ chối để không ghi đè dữ liệu.")
    df = load_dataframe()
    headers = [h for h, _, _ in fields()]
    values = [headers] + (df[headers].astype(str).values.tolist() if not df.empty else [])
    sh = main.spreadsheet
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=max(len(values) + 50, 100), cols=len(headers) + 2)
    ws.clear()
    if ws.row_count < len(values) + 1:
        ws.resize(rows=len(values) + 50)
    ws.update(values, "A1", value_input_option="RAW")
    return len(values) - 1, tab
