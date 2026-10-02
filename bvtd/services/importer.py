"""
services/importer.py
────────────────────
Logic "Ngày lập" cho màn Import (giống Streamlit): 1 file 04-4 thường trải dài
nhiều ngày lập, người dùng chọn ngày nào thì chỉ ghi các BN có Ngày lập đó.
"""
import json, os, re, tempfile, time
from collections import Counter
from datetime import datetime

IMPORT_VERSION = "import-04-4 · v2 · 02/10/2026"
_DIR = os.path.join(tempfile.gettempdir(), "bvtd_import")
_TTL = 3600                                    # giữ file đã upload 1 giờ
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

UNKNOWN_LAP = "__unknown__"


def _dmy(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%d/%m/%Y")
    except Exception:
        return datetime.min


def lap_key(rec: dict) -> str:
    return rec.get("NGÀY LẬP") or UNKNOWN_LAP


def group_by_lap(records: list[dict]):
    """→ (order, counts): ngày lập mới nhất trước, 'không rõ ngày' xếp cuối."""
    counts = Counter(lap_key(r) for r in records)
    known = sorted((d for d in counts if d != UNKNOWN_LAP), key=_dmy, reverse=True)
    order = known + ([UNKNOWN_LAP] if UNKNOWN_LAP in counts else [])
    return order, counts


def filter_by_lap(records: list[dict], selected: set[str]) -> list[dict]:
    return [r for r in records if lap_key(r) in selected] if selected else []


# ── Lưu tạm file đã đọc (theo token trong session) ───────────────────────────
# Ghi ra đĩa thay vì RAM để mọi worker gunicorn đều thấy cùng dữ liệu, và không
# nhét danh sách bệnh nhân vào cookie (giới hạn ~4KB).
def _path(token: str):
    return os.path.join(_DIR, token + ".json") if token and _TOKEN_RE.match(token) else None


def _cleanup():
    now = time.time()
    try:
        for fn in os.listdir(_DIR):
            fp = os.path.join(_DIR, fn)
            if now - os.path.getmtime(fp) > _TTL:
                os.remove(fp)
    except OSError:
        pass


def save_import(token: str, records: list[dict], filename: str) -> None:
    os.makedirs(_DIR, exist_ok=True)
    _cleanup()
    fp = _path(token)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"records": records, "filename": filename}, f, ensure_ascii=False)
    os.replace(tmp, fp)


def load_import(token: str):
    fp = _path(token)
    try:
        if not fp or time.time() - os.path.getmtime(fp) > _TTL:
            return None
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def delete_import(token: str) -> None:
    fp = _path(token)
    try:
        if fp:
            os.remove(fp)
    except OSError:
        pass
