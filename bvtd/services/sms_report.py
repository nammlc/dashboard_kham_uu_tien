"""
services/sms_report.py
──────────────────────
BÁO CÁO TỈ LỆ ĐẾN của đợt khám lưu động.

  Danh sách người ĐÃ ĐƯỢC NHẮC của đợt  = những người có tin trạng thái «đã gửi / đã nhận» trong các chiến dịch của đợt
                                          (cả người trong danh bạ lẫn bệnh nhân cũ theo vùng; mỗi SĐT tính 1 lần).
  Đến hay chưa đến                      = đối chiếu với nhật ký khám (file 01-1) bằng ĐÚNG thuật toán của trang «Đối chiếu tái khám»
                                          (tên + SĐT → tên + năm sinh ±1 → chỉ tên), trong cửa sổ `days` ngày (2 hoặc 3) KỂ TỪ NGÀY KHÁM
                                          [ngày khám, ngày khám + days − 1] và không vượt quá hôm nay — vì đợt khám chỉ kéo dài 2–3 ngày.

Chỉ chứa logic thuần + truy vấn; không đụng Google Sheet.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

from services import patient_db as PD, reconcile as R, regions as RG, sheets as S

MOBILE_WINDOW_BEFORE = 0                 # không nhận lượt khám TRƯỚC ngày khám
WINDOW_DAYS_CHOICES = (2, 3)             # đợt khám sức khoẻ / khám lưu động chỉ kéo dài 2–3 ngày
WINDOW_DAYS_DEFAULT = 3


def clamp_days(v) -> int:
    """Số ngày của cửa sổ tính «đến» (kể cả ngày khám): chỉ 2 hoặc 3; giá trị lạ → mặc định 3."""
    return v if v in WINDOW_DAYS_CHOICES else WINDOW_DAYS_DEFAULT


def window(event_date: date, days: int) -> tuple[date, date]:
    """Cửa sổ ngày tính «đến»: từ ngày khám đến ngày khám + days − 1 (ngày khám là ngày thứ nhất)."""
    return event_date, event_date + timedelta(days=clamp_days(days) - 1)

STATUS_LABEL = {
    "attended_sure": "Đã đến",
    "attended_unsure": "Có thể đã đến (cần xác nhận)",
    "not_attended": "Chưa đến",
    "no_name": "Thiếu họ tên (không đối chiếu được)",
}
TIER_LABEL = {1: "Tên + SĐT", 2: "Tên + Năm sinh", 3: "Chỉ Tên"}
GROUP_CONTACT, GROUP_PATIENT = "Danh bạ", "Bệnh nhân"


# ══════════════════════════════════════════════════════════════════════════════
# 1. Danh sách người đã được nhắc của đợt
# ══════════════════════════════════════════════════════════════════════════════
def reminded_counts(c) -> dict[int, int]:
    """event_id → số người (SĐT) đã được nhắc. Chỉ tính tin đã gửi / đã nhận."""
    out = {}
    for r in c.execute("SELECT k.event_id AS eid, COUNT(DISTINCT m.phone) AS n FROM sms_messages m JOIN sms_campaigns k ON k.id = m.campaign_id "
                       "WHERE k.kind = 'mobile' AND k.event_id IS NOT NULL AND m.status IN ('sent','delivered') GROUP BY k.event_id").fetchall():
        out[int(r["eid"])] = int(r["n"])
    return out


def event_recipients(c, df, event_id: int, real_only: bool = False) -> list[dict]:
    """Những người đã được nhắc của đợt, mỗi SĐT một dòng (lần gửi đầu tiên). `real_only` = bỏ các tin gửi thử (sandbox)."""
    sql = ("SELECT m.id, m.phone, m.recipient_name, m.patient_id, m.contact_id, m.sent_at, k.mode FROM sms_messages m "
           "JOIN sms_campaigns k ON k.id = m.campaign_id WHERE k.kind = 'mobile' AND k.event_id = %s AND m.status IN ('sent','delivered')"
           + (" AND k.mode = 'live'" if real_only else "") + " ORDER BY m.id")
    msgs = c.execute(sql, (int(event_id),)).fetchall()
    contacts = {r["id"]: r for r in c.execute("SELECT id, name, commune, org FROM sms_contacts WHERE event_id = %s", (int(event_id),)).fetchall()}
    pdf = None
    if df is not None and len(df):
        from services.sms import _with_commune
        pdf = _with_commune(df)
    seen, out = set(), []
    for m in msgs:
        if m["phone"] in seen:
            continue
        seen.add(m["phone"])
        row = {"phone": m["phone"], "name": (m["recipient_name"] or "").strip(), "sent_at": m["sent_at"], "mode": m["mode"],
               "group": GROUP_CONTACT if m["contact_id"] else GROUP_PATIENT, "commune": "", "org": "", "birth_year": "", "age": "",
               "source": "", "khoa": "", "patient_id": m["patient_id"], "contact_id": m["contact_id"]}
        ct = contacts.get(m["contact_id"]) if m["contact_id"] else None
        if ct:
            row.update(commune=ct["commune"] or "", org=ct["org"] or "")
        elif pdf is not None and m["patient_id"] is not None and (int(m["patient_id"]) - 2) in pdf.index:
            r = pdf.loc[int(m["patient_id"]) - 2]
            row.update(commune=str(r.get(PD.COMMUNE_HEADER, "") or ""), birth_year=str(r.get(S.COL_BIRTH_YEAR, "") or ""),
                       age=str(r.get("TUỔI", "") or ""), source=str(r.get(S.COL_SOURCE, "") or ""), khoa=str(r.get(S.COL_KHOA, "") or ""))
        out.append(row)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 2. Đối chiếu với nhật ký khám + thống kê
# ══════════════════════════════════════════════════════════════════════════════
def compute(event_date: date, recipients: list[dict], visit_records: list[dict], today: date | None = None,
            days: int = WINDOW_DAYS_DEFAULT) -> list[dict]:
    """Mỗi người đã nhắc → 1 kết quả (cùng thứ tự). status: attended_sure | attended_unsure | not_attended | no_name.
    Chỉ tính lượt khám trong `days` ngày (2 hoặc 3) kể từ ngày khám."""
    pts = [{"sheet_row": i, "stt": i + 1, "name": r["name"], "phone": r["phone"], "age": r["age"], "birth_year": r["birth_year"],
            "exam_date": event_date, "source": r["source"]} for i, r in enumerate(recipients)]
    results = R.reconcile_attendance(pts, visit_records, window_before=MOBILE_WINDOW_BEFORE, window_after=clamp_days(days) - 1,
                                     today=today or date.today())
    for res, r in zip(results, recipients):
        res.update(group=r["group"], commune=r["commune"] or RG.UNKNOWN, org=r["org"], khoa=r["khoa"], mode=r["mode"])
        if not R.person_view({"name": r["name"], "phone": r["phone"], "birth_year": r["birth_year"]})["key"]:
            res.update(status="no_name", visit=None, match_tier=None, score=0.0, near_miss=None)      # không có họ tên thì không so khớp được
    return results


def _pct(a: int, b: int):
    return round(100.0 * a / b, 1) if b else None


def summarize(results: list[dict]) -> dict:
    """Tổng hợp: tỉ lệ đến chắc chắn / tối đa, theo nhóm (danh bạ / bệnh nhân) và theo xã. Mẫu số = người đối chiếu được (có họ tên)."""
    def bucket(rows):
        n = len(rows)
        sure = sum(1 for r in rows if r["status"] == "attended_sure")
        unsure = sum(1 for r in rows if r["status"] == "attended_unsure")
        return {"n": n, "sure": sure, "unsure": unsure, "not": n - sure - unsure, "rate": _pct(sure, n), "rate_max": _pct(sure + unsure, n)}

    matchable = [r for r in results if r["status"] != "no_name"]
    tot = bucket(matchable)
    by_commune, by_group = {}, {}
    for r in matchable:
        by_commune.setdefault(r["commune"], []).append(r)
        by_group.setdefault(r["group"], []).append(r)
    return {"total": len(results), "no_name": len(results) - len(matchable), **tot,
            "by_commune": sorted(({"name": k, **bucket(v)} for k, v in by_commune.items()), key=lambda x: (-x["n"], x["name"])),
            "by_group": sorted(({"name": k, **bucket(v)} for k, v in by_group.items()), key=lambda x: -x["n"])}


# ══════════════════════════════════════════════════════════════════════════════
# 3. Excel — cùng bố cục cột với file «Đối chiếu tái khám»
# ══════════════════════════════════════════════════════════════════════════════
LIST_HEADERS = ["STT", "Họ tên", "SĐT", "Năm sinh", "Tuổi", "Nhóm", "Xã/Phường", "Thôn/Đơn vị", "Ngày khám lưu động", "Kết quả", "Khớp qua",
                "Ngày thực đến", "SĐT lúc khám", "Năm sinh lúc khám", "Khoa thực khám", "Độ tin cậy"]


def list_rows(event_date: date, results: list[dict]) -> list[list]:
    rows = []
    for i, r in enumerate(results, 1):
        v = r.get("visit") or {}
        rows.append([i, r["name"], r["phone"], r.get("birth_year", ""), r.get("age", ""), r["group"],
                     "" if r["commune"] == RG.UNKNOWN else r["commune"], r.get("org", ""), event_date.strftime("%d/%m/%Y"),
                     STATUS_LABEL[r["status"]], TIER_LABEL.get(r.get("match_tier"), ""), v.get("NGÀY ĐK", ""), v.get("SỐ ĐIỆN THOẠI", ""),
                     v.get("NĂM SINH", ""), v.get("KHOA ĐK", ""), f"{r['score']:.0f}%" if r.get("match_tier") else ""])
    return rows


def build_xlsx(event: dict, results: list[dict], summary: dict, ran_at: str, visit_name: str, real_only: bool = False,
               days: int = WINDOW_DAYS_DEFAULT) -> bytes:
    """File Excel 2 trang: «Tổng hợp» (tỉ lệ đến, theo nhóm, theo xã) và «Danh sách» (từng người, cùng cột với file đối chiếu tái khám)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    head_fill = PatternFill("solid", fgColor="0F2642")
    head_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="CBD5E1")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    fills = {"attended_sure": "D1FAE5", "attended_unsure": "FEF3C7", "not_attended": "FEE2E2", "no_name": "E2E8F0"}

    wb = Workbook()
    ws = wb.active
    ws.title = "Tổng hợp"
    ws["A1"] = "BÁO CÁO TỈ LỆ ĐẾN — ĐỢT KHÁM LƯU ĐỘNG"
    ws["A1"].font = Font(bold=True, size=14)
    info = [("Đợt khám", event["name"]), ("Ngày khám", event["event_date"].strftime("%d/%m/%Y") + (f" lúc {event['event_time']}" if event.get("event_time") else "")),
            ("Địa điểm", event.get("location", "")), ("Đối tượng", event.get("audience_label", "")),
            ("Tin tính", "Chỉ tin gửi thật" if real_only else "Tất cả tin đã gửi (kể cả gửi thử)"),
            ("File nhật ký khám", visit_name),
            ("Cửa sổ tính đến", f"{clamp_days(days)} ngày: từ {window(event['event_date'], days)[0]:%d/%m/%Y} đến {window(event['event_date'], days)[1]:%d/%m/%Y}"),
            ("Tính lúc", ran_at)]
    r = 3
    for k, v in info:
        ws.cell(r, 1, k).font = Font(bold=True)
        ws.cell(r, 2, v)
        r += 1
    r += 1
    ws.cell(r, 1, "KẾT QUẢ CHUNG").font = Font(bold=True, size=12)
    r += 1
    kpis = [("Số người được nhắc", summary["total"]), ("Không đối chiếu được (thiếu họ tên)", summary["no_name"]),
            ("Số người đối chiếu (mẫu số)", summary["n"]), ("Đã đến", summary["sure"]), ("Có thể đã đến (cần xác nhận)", summary["unsure"]),
            ("Chưa đến", summary["not"])]
    for k, v in kpis:
        ws.cell(r, 1, k)
        ws.cell(r, 2, v)
        r += 1
    for k, v in (("TỈ LỆ ĐẾN (chắc chắn)", summary["rate"]), ("Tỉ lệ đến tối đa (gồm cả ca cần xác nhận)", summary["rate_max"])):
        ws.cell(r, 1, k).font = Font(bold=True)
        c = ws.cell(r, 2, (v / 100.0) if v is not None else "—")
        c.font = Font(bold=True)
        if v is not None:
            c.number_format = "0.0%"
        r += 1

    def table(start, title, rows):
        ws.cell(start, 1, title).font = Font(bold=True, size=12)
        hdr = ["", "Được nhắc", "Đã đến", "Có thể đến", "Chưa đến", "Tỉ lệ đến"]
        hdr[0] = "Nhóm" if "nhóm" in title.lower() else "Xã/Phường"
        for j, h in enumerate(hdr, 1):
            c = ws.cell(start + 1, j, h)
            c.fill, c.font, c.border = head_fill, head_font, box
            c.alignment = Alignment(horizontal="center")
        rr = start + 2
        for x in rows:
            vals = [x["name"], x["n"], x["sure"], x["unsure"], x["not"], (x["rate"] / 100.0) if x["rate"] is not None else None]
            for j, v in enumerate(vals, 1):
                c = ws.cell(rr, j, v)
                c.border = box
                if j == 6 and v is not None:
                    c.number_format = "0.0%"
            rr += 1
        return rr + 1

    r = table(r + 1, "THEO NHÓM", summary["by_group"])
    table(r, "THEO XÃ / PHƯỜNG", summary["by_commune"])
    ws.column_dimensions["A"].width = 42
    for col in "BCDEF":
        ws.column_dimensions[col].width = 16

    wl = wb.create_sheet("Danh sách")
    for j, h in enumerate(LIST_HEADERS, 1):
        c = wl.cell(1, j, h)
        c.fill, c.font, c.border = head_fill, head_font, box
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, (row, res) in enumerate(zip(list_rows(event["event_date"], results), results), start=2):
        for j, v in enumerate(row, 1):
            c = wl.cell(i, j, v)
            c.border = box
        wl.cell(i, 10).fill = PatternFill("solid", fgColor=fills[res["status"]])
    widths = [6, 26, 14, 10, 7, 11, 18, 18, 16, 30, 16, 14, 14, 12, 22, 11]
    for j, w in enumerate(widths, 1):
        wl.column_dimensions[get_column_letter(j)].width = w
    wl.freeze_panes = "C2"
    wl.auto_filter.ref = f"A1:{get_column_letter(len(LIST_HEADERS))}{max(len(results) + 1, 2)}"
    wl.row_dimensions[1].height = 32

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
