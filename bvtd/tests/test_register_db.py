"""Đăng ký khám công khai (/dang-ky) khi dữ liệu nằm trong PostgreSQL: bệnh nhân bấm Gửi → 1 dòng trong bảng patients.

Chạy bằng pool SQLite giả (không cần PostgreSQL). STORAGE_BACKEND được chọn lúc import nên ở đây ta trỏ các hàm của
services.sheets về patient_db — đúng như khi đặt STORAGE_BACKEND=postgres."""
import datetime as dt
import re
import unittest
from unittest import mock

from tests.helpers import A, AppTestCase
import services.registration as RG
from services import patient_db as PD, sheets as S
from tests.sqlite_pool import Pool

NOW = dt.datetime(2026, 10, 21, 9, 0, 0)


class RegisterToDb(AppTestCase):
    def setUp(self):
        super().setUp()
        PD._TEST_POOL = Pool()
        self.addCleanup(setattr, PD, "_TEST_POOL", None)
        for name in ("load_dataframe", "update_status", "append_field_maps", "write_cells_ex", "delete_rows"):
            p = mock.patch.object(S, name, getattr(PD, name))
            p.start(); self.addCleanup(p.stop)
        for p in (mock.patch.object(RG, "vn_now", lambda: NOW), mock.patch.object(RG, "MIN_FILL_SECONDS", 0),
                  mock.patch.object(S, "vn_now", lambda: NOW.replace(tzinfo=None) if False else NOW)):
            p.start(); self.addCleanup(p.stop)
        RG.reset_state_for_tests()
        A.app.config["REG_ENABLED"] = True
        A.cache.clear()

    def csrf(self):
        html = self.client.get("/dang-ky").get_data(as_text=True)
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def form(self, **over):
        d = {"full_name": "nguyễn văn an", "birth_year": "1958", "phone": "0912 345 678", "symptoms": "Đau đầu, chóng mặt",
             "specialty": "Khám Nội tổng hợp", "exam_date": "2026-10-22", "exam_time": "08:30",
             "agree_truth": "1", "agree_terms": "1", "website": ""}
        d.update(over)
        return d

    def submit(self, **over):
        d = self.form(**over); d["csrf"] = self.csrf()
        return self.client.post("/dang-ky", data=d)

    def rows(self):
        return [dict(r) for r in PD._TEST_POOL.raw.execute("SELECT * FROM patients ORDER BY id").fetchall()]

    def test_valid_registration_creates_one_row_with_right_fields(self):
        r = self.submit()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/dang-ky/xong"))
        (row,) = self.rows()
        self.assertEqual(row["full_name"], "NGUYỄN VĂN AN")                    # viết HOA như quy ước
        self.assertEqual((row["phone"], row["birth_year"]), ("0912345678", 1958))
        self.assertEqual(str(row["exam_date"])[:10], "2026-10-22")
        self.assertEqual(row["exam_time"], "08:30")
        self.assertEqual(row["specialty"], "Khám Nội tổng hợp")
        self.assertEqual(row["symptom"], "Đau đầu, chóng mặt")
        self.assertEqual((row["consent_truth"], row["consent_terms"]), ("CÓ", "CÓ"))
        self.assertEqual((row["source"], row["status"], row["khoa"]), ("", None, ""))   # như dòng Google Form: chờ nhân viên phân loại
        self.assertEqual(str(row["registered_at"])[:19], "2026-10-21 09:00:00")
        self.assertEqual((row["stt"], row["version"]), (1, 1))
        self.assertEqual(PD.get_history(row["id"])[0]["by"], "dang-ky-online")        # lịch sử ghi rõ nguồn là đăng ký online

    def test_registration_shows_up_for_staff_immediately(self):
        self.submit()
        df = PD.load_dataframe()
        self.assertEqual(list(df["1. HỌ VÀ TÊN BỆNH NHÂN"]), ["NGUYỄN VĂN AN"])
        self.assertEqual(S.build_report(df, "day")["empty"], True)                   # chưa có trạng thái → chưa vào báo cáo
        self.assertEqual(len(A._get_df()), 1)

    def test_numbering_continues_and_double_tap_writes_once(self):
        self.submit()
        self.submit()                                                              # bấm đúp: cùng tên+SĐT+ngày trong 10 phút
        self.assertEqual(len(self.rows()), 1)
        self.submit(full_name="TRẦN THỊ BÌNH", phone="0987654321")
        self.assertEqual([r["stt"] for r in self.rows()], [1, 2])

    def test_same_person_already_in_db_is_not_written_twice(self):
        PD.create_patient({"full_name": "NGUYỄN VĂN AN", "phone": "0912345678", "exam_date": "2026-10-22"})
        RG.reset_state_for_tests()
        self.submit()
        self.assertEqual(len(self.rows()), 1)                                      # đã có (kể cả nhập tay) → không ghi đôi

    def test_database_failure_keeps_typed_values_and_hides_details(self):
        with mock.patch.object(PD, "_conn", side_effect=RuntimeError("password=secret host=10.0.0.5")):
            r = self.submit()
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 503)
        self.assertIn("chưa ghi được đăng ký", html)
        self.assertIn("nguyễn văn an", html.lower())                               # giữ nguyên những gì đã điền
        self.assertNotIn("secret", html); self.assertNotIn("10.0.0.5", html)       # không lộ thông tin hệ thống cho bệnh nhân
        self.assertEqual(self.rows(), [])
        r = self.submit()                                                          # DB hồi phục → gửi lại được
        self.assertEqual(r.status_code, 302); self.assertEqual(len(self.rows()), 1)

    def test_formula_text_is_stored_as_plain_text(self):
        self.submit(symptoms="=HYPERLINK(\"http://x\",\"a\")")
        self.assertEqual(self.rows()[0]["symptom"], "=HYPERLINK(\"http://x\",\"a\")")   # DB lưu chữ thuần; web luôn escape khi hiển thị

    def test_specialty_other_keeps_google_form_style(self):
        self.submit(specialty=RG.OTHER, specialty_other="Tim mạch can thiệp")
        self.assertTrue(self.rows()[0]["specialty"].startswith("Other: "))

    def test_bad_input_is_rejected_and_nothing_written(self):
        self.assertEqual(self.submit(phone="123").status_code, 422)
        self.assertEqual(self.submit(birth_year="19").status_code, 422)
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
