"""Trang đăng ký khám online CÔNG KHAI (/dang-ky): hiển thị, kiểm tra dữ liệu, ghi Sheet đúng cột, chống spam / bấm đúp / công thức."""
import datetime as dt
import re
import unittest
from unittest import mock

from tests.helpers import A, AppTestCase, FakeSheet, HEADERS, ApiError, sh, sheet_row
import services.registration as RG
from services import sheets as S

NOW = dt.datetime(2026, 10, 21, 9, 0, 0)
REG_HEADERS = HEADERS + [S.COL_CCCD, S.COL_SYMPTOM, S.COL_CONSENT_1, S.COL_CONSENT_2]
text = lambda h: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h))


class RegisterBase(AppTestCase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(RG, "vn_now", lambda: NOW), mock.patch.object(RG, "MIN_FILL_SECONDS", 0)):
            p.start()
            self.addCleanup(p.stop)
        RG.reset_state_for_tests()
        A.app.config["REG_ENABLED"] = True
        self.install_sheet(FakeSheet([], headers=REG_HEADERS))

    def csrf(self):
        html = self.client.get("/dang-ky").get_data(as_text=True)
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def form(self, **over):
        d = {"full_name": "nguyễn văn an", "birth_year": "1958",
             "phone": "0912 345 678", "symptoms": "Đau đầu, chóng mặt",
             "specialty": "Khám Nội tổng hợp", "exam_date": "2026-10-22", "exam_time": "08:30",
             "agree_truth": "1", "agree_terms": "1", "website": ""}
        d.update(over)
        return {k: v for k, v in d.items() if v is not None}

    def submit(self, csrf=None, **over):
        d = self.form(**over)
        d["csrf"] = csrf or self.csrf()
        return self.client.post("/dang-ky", data=d)

    def rows(self):
        return self.sheet.grid[1:]


class PageTests(RegisterBase):
    def test_page_is_public_and_self_contained(self):
        self.assertFalse(hasattr(A.app.view_functions["register_page"], "__wrapped__"), "trang đăng ký không được yêu cầu đăng nhập")
        self.assertFalse(hasattr(A.app.view_functions["register_done"], "__wrapped__"))
        r = self.client.get("/dang-ky")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r.headers["Cache-Control"])
        html = r.get_data(as_text=True)
        self.assertNotRegex(re.sub(r'<link rel="canonical"[^>]*>|<meta property="og:[^>]*>', "", html), r"https?://(?!www\.w3)", "không phụ thuộc CDN/trang ngoài (mạng yếu ở vùng quê)")
        for name in ("full_name", "birth_year", "phone", "symptoms", "specialty",
                     "exam_date", "exam_time", "agree_truth", "agree_terms", "csrf"):
            self.assertIn(f'name="{name}"', html, name)
        for gone in ("gender", "address", "cccd", "doctor"):               # đã bỏ khỏi biểu mẫu
            self.assertNotIn(f'name="{gone}"', html, gone)
        for gone in ("Giới tính", "Địa chỉ", "căn cước công dân <", "Bác sĩ muốn khám"):
            self.assertNotIn(gone, html, gone)
        self.assertIn("07:00", html); self.assertIn("16:30", html); self.assertNotIn(">12:00<", html)
        self.assertIn("Khám Nội tiết", html)
        self.assertNotIn("sz-plus", html)                               # không có nút chỉnh cỡ chữ
        self.assertIn("ĐĂNG KÝ HẸN KHÁM ONLINE", html)
        self.assertNotIn("LỊCH HẸN KHÁM ONLINE", html)                  # đã bỏ chữ "lịch" ở tiêu đề
        self.assertIn("Đăng ký hẹn khám online", html)                   # <title>/mô tả cũng đổi theo
        self.assertIn('src="/static/logo.png"', html)
        self.assertEqual(self.client.get("/static/logo.png").status_code, 200)     # logo công khai, không cần đăng nhập
        self.assertNotIn("Điền lần lượt từng bước", html)

    def test_inputs_are_big_enough_for_the_elderly(self):
        html = self.client.get("/dang-ky").get_data(as_text=True)
        self.assertRegex(html, r"html\{font-size:19px")
        self.assertRegex(html, r"min-height:56px")             # ô nhập
        self.assertRegex(html, r"min-height:62px")             # nút Gửi
        self.assertIn('inputmode="tel"', html)

    def test_single_page_layout_works_on_phone_and_pc(self):
        """Một trang duy nhất (không chia bước), co giãn theo màn hình: lưới 2 cột trên PC, 1 cột trên điện thoại."""
        html = self.client.get("/dang-ky").get_data(as_text=True)
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', html)
        self.assertEqual(html.count("<form "), 1)
        self.assertNotIn("data-step", html)                               # không còn wizard nhiều bước
        self.assertNotIn("Bước 1 /", html)
        self.assertRegex(html, r"@media\(min-width:640px\)\{\.row2")      # 2 cột (năm sinh | SĐT) từ màn hình vừa
        self.assertRegex(html, r"@media\(min-width:900px\)")                # bố cục PC
        self.assertRegex(html, r"auto-fill,minmax")                       # giờ khám / chuyên khoa tự xuống dòng
        self.assertRegex(html, r"\.bar\{position:sticky;bottom:0")         # nút Gửi luôn thấy được trên điện thoại
        self.assertEqual(html.count('type="submit"'), 1)

    def test_header_is_large_on_desktop(self):
        """Trên PC tiêu đề + logo phải to rõ (không bị 'lọt thỏm' trong thanh xanh), ở cả trang form lẫn trang kết quả."""
        form = self.client.get("/dang-ky").get_data(as_text=True)
        self.assertRegex(form, r"@media\(min-width:900px\)\{\s*header\.top\{padding:24px\}")
        self.assertIn("header.top h1{font-size:2.3rem}", form)
        self.assertIn("header.top .logo{width:92px;height:92px}", form)
        self.submit()
        done = self.client.get("/dang-ky/xong").get_data(as_text=True)
        self.assertIn("header b{font-size:2.3rem}", done)
        self.assertIn("header img{width:92px;height:92px}", done)

    def test_closed_switch(self):
        A.app.config["REG_ENABLED"] = False
        r = self.client.get("/dang-ky")
        self.assertEqual(r.status_code, 503)
        self.assertIn("Tạm ngừng", r.get_data(as_text=True))


class SubmitTests(RegisterBase):
    def test_valid_registration_is_written_to_the_right_columns(self):
        r = self.submit()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/dang-ky/xong"))
        self.assertEqual(len(self.rows()), 1)
        c = lambda h: self.sheet.cell(2, h)
        self.assertEqual(c("1. HỌ VÀ TÊN BỆNH NHÂN"), "NGUYỄN VĂN AN")
        self.assertEqual(c("NĂM SINH"), "1958")
        self.assertEqual(c("5. SỐ ĐIÊN THOẠI"), "0912345678")                       # chuẩn hoá, giữ số 0 đầu
        self.assertEqual(c(S.COL_SYMPTOM), "Đau đầu, chóng mặt")
        self.assertEqual(c("CHUYÊN KHOA MONG MUỐN KHÁM"), "Khám Nội tổng hợp")
        self.assertEqual(S.to_dmy(c("NGÀY KHÁM")), "22/10/2026")
        self.assertEqual(c("GIỜ KHÁM DỰ KIẾN"), "08:30")
        self.assertEqual(c(S.COL_CONSENT_1), "CÓ"); self.assertEqual(c(S.COL_CONSENT_2), "CÓ")
        self.assertEqual(c("Dấu thời gian"), "21/10/2026 09:00:00")
        # giống dòng do Google Form tạo ra → luồng đối chiếu coi đây là "đăng ký qua form"
        # các trường đã bỏ khỏi biểu mẫu (giới tính, địa chỉ, CCCD, bác sĩ) cũng để trống
        for blank in ("NGUỒN BỆNH NHÂN", "TRẠNG THÁI", "KHOA KHÁM CHỮA BỆNH", "NGÀY THỰC KHÁM", S.COL_CREATED,
                      "3. GIỚI TÍNH", "2. ĐỊA CHỈ (THÔN/XÃ)", S.COL_CCCD, "BÁC SĨ MONG MUỐN ( nếu có)"):
            self.assertEqual(c(blank), "", blank)

    def test_success_page_after_redirect_and_refresh_does_not_resubmit(self):
        r = self.submit()
        page = self.client.get(r.headers["Location"])
        t = text(page.get_data(as_text=True))
        self.assertIn("ĐĂNG KÝ THÀNH CÔNG", t); self.assertIn("NGUYỄN VĂN AN", t); self.assertIn("08:30 · ngày 22/10/2026", t)
        self.client.get(r.headers["Location"])                                       # tải lại trang kết quả
        self.assertEqual(len(self.rows()), 1)

    def test_double_tap_writes_once_and_lands_on_success_page(self):
        tok = self.csrf()
        first = self.submit(csrf=tok)
        second = self.submit(csrf=tok)                                               # cùng phiên, bấm lần 2
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertTrue(second.headers["Location"].endswith("/dang-ky/xong"))
        self.assertEqual(len(self.rows()), 1)

    def test_same_person_already_on_sheet_is_not_written_twice(self):
        pre = sheet_row(1, "NGUYỄN VĂN AN", phone="0912345678", hen="22/10/2026", source="") + ["", "", "", ""]
        self.install_sheet(FakeSheet([pre], headers=REG_HEADERS))
        r = self.submit()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.rows()), 1)
        self.assertIn("đã được ghi nhận", text(self.client.get("/dang-ky/xong").get_data(as_text=True)))

    def test_register_another_person_after_success(self):
        self.submit()
        self.submit(full_name="Trần Thị Bình", phone="0987654321")
        self.assertEqual([r[REG_HEADERS.index("1. HỌ VÀ TÊN BỆNH NHÂN")] for r in self.rows()], ["NGUYỄN VĂN AN", "TRẦN THỊ BÌNH"])

    def test_specialty_other(self):
        self.submit(specialty=RG.OTHER, specialty_other="Răng hàm mặt")
        self.assertEqual(self.sheet.cell(2, "CHUYÊN KHOA MONG MUỐN KHÁM"), "Other: Răng hàm mặt")

    def test_removed_fields_are_ignored_even_if_a_client_still_sends_them(self):
        """Trình duyệt mở sẵn bản cũ / bot vẫn gửi giới tính, địa chỉ, CCCD, bác sĩ: không lỗi, và KHÔNG được ghi vào Sheet."""
        r = self.submit(gender="NAM", address="Thôn 3", cccd="038058001234", doctor="BS Bất Kỳ")
        self.assertEqual(r.status_code, 302)
        for h in ("3. GIỚI TÍNH", "2. ĐỊA CHỈ (THÔN/XÃ)", S.COL_CCCD, "BÁC SĨ MONG MUỐN ( nếu có)"):
            self.assertEqual(self.sheet.cell(2, h), "", h)

    def test_google_failure_keeps_what_the_patient_typed(self):
        self.sheet.faults["append_rows"] = [ApiError(500)]
        r = self.submit()
        self.assertEqual(r.status_code, 503)
        html = r.get_data(as_text=True)
        self.assertIn("chưa ghi được đăng ký", html)
        self.assertIn("nguyễn văn an", html)                                         # giữ nguyên dữ liệu đã điền
        self.assertEqual(len(self.rows()), 0)
        self.assertEqual(self.submit().status_code, 302)                             # bấm gửi lại sau đó thì thành công
        self.assertEqual(len(self.rows()), 1)


class ValidationTests(RegisterBase):
    def check(self, field, msg_part, status=422, **over):
        r = self.submit(**over)
        self.assertEqual(r.status_code, status, over)
        self.assertIn(msg_part, text(r.get_data(as_text=True)), over)
        self.assertEqual(len(self.rows()), 0)
        return r

    def test_optional_fields_can_be_empty(self):
        """Triệu chứng và chuyên khoa không bắt buộc."""
        r = self.submit(symptoms="", specialty="")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.rows()), 1)

    def test_each_rule(self):
        self.check("full_name", "đầy đủ họ và tên", full_name="An")
        self.check("full_name", "Vui lòng nhập họ và tên", full_name="  ")
        self.check("full_name", "chỉ được có chữ cái", full_name="An 123 Văn")
        self.check("birth_year", "4 chữ số", birth_year="58")
        self.check("birth_year", "4 chữ số", birth_year="2099")
        self.check("phone", "10 chữ số", phone="12345")
        self.check("specialty", "không hợp lệ", specialty="Khoa lạ")
        self.check("specialty_other", "ghi rõ", specialty=RG.OTHER, specialty_other="")
        self.check("exam_date", "đã qua", exam_date="2026-10-20")
        self.check("exam_date", "30 ngày", exam_date="2026-12-31")
        self.check("exam_date", "chọn ngày", exam_date="")
        self.check("exam_time", "chọn giờ", exam_time="12:00")                       # không nằm trong khung giờ
        self.check("exam_time", "đã qua", exam_date="2026-10-21", exam_time="08:30")  # hôm nay, 9h00 → 8h30 đã qua
        self.check("agree_truth", "cam kết", agree_truth=None)
        self.check("agree_terms", "điều khoản", agree_terms=None)

    def test_phone_formats_are_accepted(self):
        for raw in ("0912345678", "091 234 5678", "+84912345678", "84912345678", "0912.345.678"):
            RG.reset_state_for_tests()
            self.install_sheet(FakeSheet([], headers=REG_HEADERS))
            self.assertEqual(self.submit(phone=raw).status_code, 302, raw)
            self.assertEqual(self.sheet.cell(2, "5. SỐ ĐIÊN THOẠI"), "0912345678", raw)

    def test_today_in_a_future_slot_is_allowed(self):
        self.assertEqual(self.submit(exam_date="2026-10-21", exam_time="09:30").status_code, 302)

    def test_errors_are_shown_on_the_same_page_and_values_are_kept(self):
        html = self.submit(exam_time="12:00", symptoms="Đau lưng").get_data(as_text=True)
        self.assertIn("Vui lòng chọn giờ khám dự kiến", text(html))
        self.assertIn("Đau lưng", html)                                              # giữ nguyên chữ đã điền
        self.assertIn('value="nguyễn văn an"', html)
        html = self.submit(phone="1").get_data(as_text=True)
        self.assertIn("Có 1 mục cần kiểm tra lại", text(html))
        self.assertIn('id="err-phone"', html)

    def test_html_in_input_is_escaped(self):
        html = self.submit(symptoms='<script>alert(1)</script>', phone="1").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class SecurityTests(RegisterBase):
    def test_sheet_formulas_are_neutralised(self):
        c, errors = RG.validate(self.form(symptoms="+cmd|' /C calc'!A0",
                                          specialty=RG.OTHER, specialty_other="@SUM(1)"), NOW)
        self.assertEqual(errors, {})
        fm = RG.build_field_map(c, NOW)
        self.assertTrue(fm[S.COL_SYMPTOM].startswith("'+"))
        self.assertTrue(fm[S.COL_SPECIALTY].startswith("Other:"))                     # đã có tiền tố nên không còn đứng đầu bằng @
        self.assertEqual(fm[S.COL_PHONE], "'0912345678")                              # ép chữ: giữ số 0 đầu
        self.assertEqual(RG.sheet_safe("-1+1"), "'-1+1")
        self.assertEqual(RG.sheet_safe("Bình thường"), "Bình thường")

    def test_control_and_invisible_characters_are_stripped(self):
        self.assertEqual(RG.clean_text("a\u200bb\x00c \n  d"), "abc d")

    def test_missing_or_wrong_csrf_is_rejected(self):
        self.client.get("/dang-ky")
        for bad in ("", "sai-token"):
            r = self.client.post("/dang-ky", data={**self.form(), "csrf": bad})
            self.assertEqual(r.status_code, 400)
            self.assertIn("hết hạn", r.get_data(as_text=True))
        self.assertEqual(len(self.rows()), 0)

    def test_post_without_any_session_is_rejected(self):
        fresh = A.app.test_client()
        r = fresh.post("/dang-ky", data={**self.form(), "csrf": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(self.rows()), 0)

    def test_honeypot_blocks_bots(self):
        r = self.submit(website="http://spam.example")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(self.rows()), 0)

    def test_submitting_too_fast_is_rejected(self):
        with mock.patch.object(RG, "MIN_FILL_SECONDS", 60):
            r = self.submit()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(self.rows()), 0)

    def test_stale_page_is_rejected_but_values_kept(self):
        tok = self.csrf()
        with mock.patch.object(RG, "MAX_AGE_SECONDS", -1):
            r = self.client.post("/dang-ky", data={**self.form(), "csrf": tok})
        self.assertEqual(r.status_code, 400)
        self.assertIn("nguyễn văn an", r.get_data(as_text=True))

    def test_ip_rate_limit_counts_only_valid_submissions(self):
        with mock.patch.object(RG, "IP_LIMIT", 3):
            for _ in range(6):                                                        # lỗi nhập liệu không bị tính
                self.assertEqual(self.submit(phone="1").status_code, 422)
            codes = [self.submit(full_name=f"Nguyễn Văn {chr(65 + i)}{chr(66 + i)}", phone=f"09123456{i:02d}").status_code for i in range(5)]
        self.assertEqual(codes, [302, 302, 302, 429, 429])
        self.assertEqual(len(self.rows()), 3)

    def test_success_page_without_registration_redirects_to_form(self):
        r = A.app.test_client().get("/dang-ky/xong")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/dang-ky"))

    def test_only_registration_pages_are_public(self):
        """Mọi trang nội bộ vẫn phải có @login_required (chỉ kiểm được khi cài flask_login thật)."""
        try:
            import flask_login
            if not hasattr(flask_login, "__file__"):
                raise ImportError
        except ImportError:
            self.skipTest("flask_login là bản giả trong môi trường test")
        public = {"register_page", "register_done", "static", "login", "logout", "auth_google", "auth_callback", "health",
                  "login_google", "google_callback",                       # đăng nhập Google (chưa đăng nhập thì phải vào được)
                  "session_status", "keepalive", "robots_txt", "sitemap_xml",
                  "favicon"}   # icon trang (công khai, Google cần lấy được); tự kiểm tra đăng nhập, trả 401 JSON thay vì chuyển hướng
        open_ones = [ep for ep, fn in A.app.view_functions.items() if ep not in public and not hasattr(fn, "__wrapped__")]
        self.assertEqual(open_ones, [])


if __name__ == "__main__":
    unittest.main()
