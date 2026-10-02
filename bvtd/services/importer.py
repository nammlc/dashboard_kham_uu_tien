"""
services/importer.py
────────────────────
Logic "Ngày lập" cho màn Import (giống Streamlit): 1 file 04-4 thường trải dài
nhiều ngày lập, người dùng chọn ngày nào thì chỉ ghi các BN có Ngày lập đó.
"""
from collections import Counter
from datetime import datetime

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
