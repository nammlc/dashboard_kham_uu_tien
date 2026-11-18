"""Trang Đối chiếu: Bước 2/3/4 (gửi nền, JSON), lọc trùng, ghi NGÀY THỰC KHÁM — chạy qua mã ghi Sheet thật."""
import datetime as dt
import html.parser
import re
import threading
import unittest
from unittest import mock

from werkzeug.datastructures import MultiDict

from tests.helpers import (A, AppTestCase, FakeSheet, ApiError, ReadTimeout, sh, sheet_row, TK, VL, DA_KHAM, CHUA_KHAM)

import services.reconcile as R

TOK = "c" * 32


def result(row, stt, name, source, status, visit=None, visit_date=None, near=None):
    return {"sheet_row": row, "stt": str(stt), "name": name, "phone": "", "age": "", "birth_year": "1959", "gender": "Nữ",
            "exam_date": "2026-10-21", "source": source, "status_now": "", "visit": visit, "visit_date": visit_date,
            "score": 100.0, "match_tier": 1 if status == "attended_sure" else 3,
            "match_notes": ["Chỉ khớp tên"] if status == "attended_unsure" else [], "status": status,
            "near_miss": near, "done": False}


class _Forms(html.parser.HTMLParser):
    """Gom các form.js-rec: action + các ô sẽ được gửi (như trình duyệt: input ẩn, ô tích đã chọn, nút đầu tiên)."""
    def __init__(self):
        super().__init__()
        self.forms, self.cur = [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and "js-rec" in (a.get("class") or ""):
            self.cur = {"action": a.get("action"), "method": (a.get("method") or "").upper(), "data": [], "buttons": []}
            self.forms.append(self.cur)
        elif self.cur is not None and tag == "input" and a.get("name"):
            if a.get("type") in ("checkbox", "radio") and "checked" not in a:
                return
            self.cur["data"].append((a["name"], a.get("value", "1" if a.get("type") == "checkbox" else "")))
        elif self.cur is not None and tag == "button" and a.get("name"):
            self.cur["buttons"].append((a["name"], a.get("value", "")))

    def handle_endtag(self, tag):
        if tag == "form":
            self.cur = None


class ReconcileRoutes(AppTestCase):
    def setUp(self):
        super().setUp()
        self.install_sheet(FakeSheet([
            sheet_row(1, "NGUYỄN THỊ TỈNH", source=TK, hen="21/10/2026"),     # row 2
            sheet_row(2, "LÊ THỊ HOA", source=TK, hen="21/10/2026"),          # row 3
            sheet_row(3, "PHẠM VĂN DŨNG", source=TK, hen="21/10/2026"),       # row 4
            sheet_row(4, "TRẦN THỊ MAI", source=VL, hen="21/10/2026"),        # row 5
            sheet_row(5, "HOÀNG VĂN NAM", source="", hen="21/10/2026"),       # row 6  ← đăng ký qua FORM, nguồn trống
        ]))

    # ── dữ liệu mẫu ──
    def seed(self, *results):
        R.store_save(TOK, "rec", {"results": list(results), "ran_at": "x", "n_auto": 0})
        with self.client.session_transaction() as s:
            s["rec_token"] = TOK

    def default_seed(self):
        near = {"visit": {"NGÀY ĐK": "10/10/2026 10:00"}, "score": 96.0, "visit_date": "2026-10-10", "notes": []}
        self.seed(
            result(2, 1, "NGUYỄN THỊ TỈNH", TK, "attended_sure", {"NGÀY ĐK": "20/10/2026 08:19"}, "2026-10-20"),
            result(3, 2, "LÊ THỊ HOA", TK, "attended_unsure", {"NGÀY ĐK": "19/10/2026 07:00"}, "2026-10-19"),
            result(4, 3, "PHẠM VĂN DŨNG", TK, "not_attended", near=near),
            result(5, 4, "TRẦN THỊ MAI", VL, "not_attended", near=near),
            result(6, 5, "HOÀNG VĂN NAM", "", "attended_sure", {"NGÀY ĐK": "21/10/2026 09:05"}, None))

    def page(self, tab="tk"):
        return self.client.get("/doi-chieu", query_string={"tab": tab}).get_data(as_text=True)

    # ── lỗi 404 do form.action bị ghi đè ──
    def test_every_form_action_is_a_real_url_and_accepts_its_own_payload(self):
        """Mỗi form.js-rec: action là đường dẫn thật; gửi đúng dữ liệu form sẽ không bao giờ 404."""
        self.default_seed()
        seen = 0
        for tab in ("tk", "vl"):
            p = _Forms(); p.feed(self.page(tab))
            for f in p.forms:
                seen += 1
                self.assertTrue(f["action"] and f["action"].startswith("/doi-chieu/"), f)
                self.assertNotIn("object", f["action"])
                self.assertEqual(f["method"], "POST")
                data = list(f["data"]) + ([("action", f["buttons"][-1][1])] if f["buttons"] else [])
                r = self.client.post(f["action"], data=MultiDict(data), headers=self.JSON)
                self.assertEqual(r.status_code, 200, (f["action"], r.status_code))
                self.assertTrue(r.is_json, f["action"])
                self.default_seed()                    # mỗi form dùng bản dữ liệu mới
        self.assertGreaterEqual(seen, 4)

    # ── hiển thị ──
    def test_step4_only_on_vang_lai_tab(self):
        self.default_seed()
        tk, vl = self.page("tk"), self.page("vl")
        self.assertNotIn('id="buoc4"', tk)
        self.assertNotIn("Nghi bị sót", tk)
        self.assertIn('id="buoc3"', tk)
        self.assertIn('id="buoc4"', vl)
        self.assertIn("Nghi bị sót", vl)
        self.assertIn('data-anchor="buoc4"', vl)

    # ── Bước 2 ──
    def test_step2_updates_status_and_actual_date_and_reads_back(self):
        self.default_seed()
        r = self.post_json("/doi-chieu/cap-nhat", {"row": ["2"], "confirm": "1", "tab": "tk"}).get_json()
        self.assertTrue(r["ok"] and r["count"] == 1 and r["names"] == ["NGUYỄN THỊ TỈNH"], r)
        self.assertEqual(self.sheet.cell(2, "TRẠNG THÁI"), DA_KHAM)
        self.assertEqual(self.sheet.cell(2, "NGÀY THỰC KHÁM"), "20/10/2026")

    def test_step2_for_form_registrant_assigns_source_and_works(self):
        """Người đăng ký qua Form (nguồn trống): Bước 2 ghi Đã khám + gán nguồn vãng lai + ngày thực khám."""
        self.default_seed()
        r = self.post_json("/doi-chieu/cap-nhat", {"row": ["6"], "confirm": "1", "tab": "vl"}).get_json()
        self.assertTrue(r["ok"] and r["count"] == 1, r)
        self.assertEqual(self.sheet.cell(6, "TRẠNG THÁI"), DA_KHAM)
        self.assertEqual(self.sheet.cell(6, "NGUỒN BỆNH NHÂN"), VL)
        self.assertEqual(self.sheet.cell(6, "NGÀY THỰC KHÁM"), "21/10/2026")

    def test_step2_is_idempotent_and_ignores_unsure_rows(self):
        self.default_seed()
        self.post_json("/doi-chieu/cap-nhat", {"row": ["2"], "confirm": "1", "tab": "tk"})
        writes = self.sheet.calls["batch_update"]
        again = self.post_json("/doi-chieu/cap-nhat", {"row": ["2"], "confirm": "1", "tab": "tk"}).get_json()
        self.assertTrue(again["ok"] and again["count"] == 0 and again["already"] == 1)
        self.assertEqual(self.sheet.calls["batch_update"], writes)             # không ghi lần 2
        bad = self.post_json("/doi-chieu/cap-nhat", {"row": ["3", "4"], "confirm": "1", "tab": "tk"}).get_json()
        self.assertFalse(bad["ok"])                                           # ca "cần xác nhận"/"chưa đến" không đi qua Bước 2
        self.assertEqual(self.sheet.cell(3, "TRẠNG THÁI"), CHUA_KHAM)

    def test_step2_requires_confirm_tick(self):
        self.default_seed()
        r = self.post_json("/doi-chieu/cap-nhat", {"row": ["2"], "tab": "tk"}).get_json()
        self.assertFalse(r["ok"])
        self.assertEqual(r["level"], "warning")
        self.assertEqual(self.sheet.cell(2, "TRẠNG THÁI"), CHUA_KHAM)

    def test_google_errors_are_reported_and_can_be_retried(self):
        self.default_seed()
        self.sheet.faults["batch_update"] = [ApiError(429)] * 3
        r = self.post_json("/doi-chieu/cap-nhat", {"row": ["2"], "confirm": "1", "tab": "tk"}).get_json()
        self.assertFalse(r["ok"])
        self.assertIn("giới hạn", r["message"])
        self.assertEqual(self.sheet.cell(2, "TRẠNG THÁI"), CHUA_KHAM)
        ok = self.post_json("/doi-chieu/cap-nhat", {"row": ["2"], "confirm": "1", "tab": "tk"}).get_json()   # "Thử lại"
        self.assertTrue(ok["ok"] and ok["count"] == 1)

    def test_transient_error_is_retried_transparently(self):
        self.default_seed()
        self.sheet.faults["batch_update"] = [ReadTimeout("t"), ApiError(503)]
        r = self.post_json("/doi-chieu/cap-nhat", {"row": ["2"], "confirm": "1", "tab": "tk"}).get_json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.sheet.calls["batch_update"], 3)

    def test_wrong_person_on_that_row_is_never_written(self):
        """Sheet vừa bị sắp xếp lại: dòng không còn đúng bệnh nhân → dừng, không ghi nhầm người."""
        self.default_seed()
        self.sheet.grid[1][self.sheet.grid[0].index("1. HỌ VÀ TÊN BỆNH NHÂN")] = "NGƯỜI KHÁC HẲN"
        self.sheet.grid[1][self.sheet.grid[0].index("STT")] = "999"
        r = self.post_json("/doi-chieu/cap-nhat", {"row": ["2"], "confirm": "1", "tab": "tk"}).get_json()
        self.assertFalse(r["ok"])
        self.assertEqual(self.sheet.cell(2, "TRẠNG THÁI"), CHUA_KHAM)

    def test_busy_lock_answers_immediately(self):
        self.default_seed()
        held, release = threading.Event(), threading.Event()

        def hold():
            A._rec_lock.acquire(); held.set(); release.wait(5); A._rec_lock.release()
        t = threading.Thread(target=hold); t.start(); held.wait()
        real = A._locked
        with mock.patch.object(A, "_locked", lambda lock, timeout=25: real(lock, 0.2)):
            r = self.post_json("/doi-chieu/cap-nhat", {"row": ["2"], "confirm": "1", "tab": "tk"}).get_json()
        release.set(); t.join()
        self.assertFalse(r["ok"])
        self.assertIn("đợi vài giây", r["message"])

    def test_unexpected_server_error_still_returns_json(self):
        self.default_seed()
        with mock.patch.object(A, "_mark_attended", side_effect=RuntimeError("boom")):
            r = self.post_json("/doi-chieu/cap-nhat", {"row": ["2"], "confirm": "1", "tab": "tk"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Lỗi máy chủ", r.get_json()["message"])

    def test_form_without_js_still_works(self):
        self.default_seed()
        r = self.client.post("/doi-chieu/cap-nhat", data={"row": ["2"], "confirm": "1", "tab": "tk"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.sheet.cell(2, "TRẠNG THÁI"), DA_KHAM)

    # ── Bước 3 / 4 ──
    def test_step3_confirm_and_save(self):
        self.default_seed()
        ok = self.post_json("/doi-chieu/xu-ly", {"sheet_row": "3", "tab": "tk", "anchor": "buoc3", "action": "confirm"}).get_json()
        self.assertTrue(ok["ok"] and ok["names"] == ["LÊ THỊ HOA"], ok)
        self.assertEqual(self.sheet.cell(3, "NGÀY THỰC KHÁM"), "19/10/2026")
        again = self.post_json("/doi-chieu/xu-ly", {"sheet_row": "3", "tab": "tk", "action": "confirm"}).get_json()
        self.assertTrue(again["ok"] and again["already"] == 1)
        self.default_seed()
        saved = self.post_json("/doi-chieu/xu-ly", {"sheet_row": "3", "tab": "tk", "action": "save",
                                                    "phone": "0987654321", "birth": "1960"}).get_json()
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(self.sheet.cell(3, "5. SỐ ĐIÊN THOẠI"), "0987654321")
        self.assertEqual(self.sheet.cell(3, "NĂM SINH"), "1960")

    def test_step4_allowed_for_vang_lai_but_refused_for_tai_kham(self):
        self.default_seed()
        no = self.post_json("/doi-chieu/xu-ly", {"sheet_row": "4", "tab": "tk", "anchor": "buoc4", "action": "confirm"}).get_json()
        self.assertFalse(no["ok"])
        self.assertEqual(self.sheet.cell(4, "TRẠNG THÁI"), CHUA_KHAM)
        yes = self.post_json("/doi-chieu/xu-ly", {"sheet_row": "5", "tab": "vl", "anchor": "buoc4", "action": "confirm"}).get_json()
        self.assertTrue(yes["ok"])
        self.assertEqual(self.sheet.cell(5, "NGÀY THỰC KHÁM"), "10/10/2026")

    def test_expired_result_suggests_reload(self):
        self.default_seed()
        r = self.post_json("/doi-chieu/xu-ly", {"sheet_row": "99", "tab": "tk", "action": "confirm"}).get_json()
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("reload_hint"))

    # ── Lọc trùng: cặp khớp yếu bị khoá, cặp khớp chắc chuyển trạng thái + ngày thực khám ──
    def test_dup_weak_pair_is_locked_and_strong_pair_transfers(self):
        self.install_sheet(FakeSheet([
            sheet_row(1, "NGUYỄN THỊ TỈNH", birth="1959", phone="0", source=TK, hen="21/10/2026"),                    # row 2
            sheet_row(2, "Nguyễn Thị Tỉnh", birth="", phone="", source="", hen="20/10/2026", status=DA_KHAM),        # row 3 Form (người khác)
            sheet_row(3, "TRẦN VĂN BÌNH", birth="1950", phone="0912345678", source=TK, hen="21/10/2026"),             # row 4
            sheet_row(4, "Trần Văn Bình", birth="1950", phone="0912 345 678", source="", hen="21/10/2026",
                      status=DA_KHAM, actual="21/10/2026"),                                                          # row 5 Form (cùng người)
        ]))
        D = dt.date
        P = lambda row, stt, name, birth, phone, src, st, exam, actual="": {
            "sheet_row": row, "stt": str(stt), "name": name, "phone": phone, "birth_year": birth, "gender": "Nữ",
            "exam_date": exam, "source": src, "status_now": st, "actual_date": actual}
        khoa = [P(2, 1, "NGUYỄN THỊ TỈNH", "1959", "0", TK, CHUA_KHAM, D(2026, 10, 21)),
                P(4, 3, "TRẦN VĂN BÌNH", "1950", "0912345678", TK, CHUA_KHAM, D(2026, 10, 21))]
        form = [P(3, 2, "Nguyễn Thị Tỉnh", "", "", "", DA_KHAM, D(2026, 10, 20)),
                P(5, 4, "Trần Văn Bình", "1950", "0912 345 678", "", DA_KHAM, D(2026, 10, 21), "21/10/2026")]
        pairs = R.classify_dup_pairs(R.find_duplicate_tk_form(khoa, form))
        by = {p["khoa"]["name"]: p for p in pairs}
        self.assertTrue(by["NGUYỄN THỊ TỈNH"]["locked"] and not by["NGUYỄN THỊ TỈNH"]["needs_status_transfer"])
        self.assertTrue(by["TRẦN VĂN BÌNH"]["needs_status_transfer"] and not by["TRẦN VĂN BÌNH"]["locked"])
        R.store_save(TOK, "dup", {"pairs": pairs, "n_khoa": 2, "n_form": 2, "scanned_at": "x"})
        with self.client.session_transaction() as s:
            s["rec_token"] = TOK
        self.client.post("/doi-chieu/trung/xoa", data={"row": ["3", "5"], "confirm": "1"}, headers=self.JSON)
        names = self.sheet.names()
        self.assertEqual(names.count("Nguyễn Thị Tỉnh"), 1, "dòng Form của người trùng tên phải còn nguyên")
        self.assertNotIn("Trần Văn Bình", names)                                        # dòng Form cùng người đã bị xoá
        tinh = names.index("NGUYỄN THỊ TỈNH") + 2
        binh = names.index("TRẦN VĂN BÌNH") + 2
        self.assertEqual(self.sheet.cell(tinh, "TRẠNG THÁI"), CHUA_KHAM)                # 4886 KHÔNG bị gán "Đã khám" oan
        self.assertEqual(self.sheet.cell(binh, "TRẠNG THÁI"), DA_KHAM)
        self.assertEqual(self.sheet.cell(binh, "NGÀY THỰC KHÁM"), "21/10/2026")


if __name__ == "__main__":
    unittest.main()
