"""Xác thực 2 bước (mã OTP Google Authenticator) sau khi qua bước mật khẩu / Google.
Bất biến quan trọng: CHƯA nhập đúng mã thì CHƯA được đăng nhập; mỗi mã chỉ dùng 1 lần; không ai tự thay được mã của tài khoản
đã thiết lập; sai nhiều lần thì bị chặn; mọi lỗi báo tiếng Việt, không 500."""
import ast
import base64
import re
import time
import unittest
from pathlib import Path
from unittest import mock

import pyotp
from flask import redirect

from services import twofa as TF
from tests.helpers import A, AppTestCase
from tests.test_google_login import FakeGoogle, _real_flask_login

EMAIL = "admin@bvtd.vn"
GOOD_PASSWORD = "mat-khau-dung"


# ── Dịch vụ TOTP (không cần Flask) ──
class TotpServiceTests(unittest.TestCase):
    RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()          # khoá trong RFC 6238

    def test_matches_rfc6238_vector(self):
        self.assertEqual(TF.verify(self.RFC_SECRET, "287082", now=59), 1)     # T=59s, 6 số cuối của 94287082

    def test_window_is_one_step_each_side_and_spaces_are_ignored(self):
        self.assertEqual(TF.verify(self.RFC_SECRET, "287 082", now=59 + 30), 1)
        self.assertEqual(TF.verify(self.RFC_SECRET, "287-082", now=59 + 29), 1)
        self.assertIsNone(TF.verify(self.RFC_SECRET, "287082", now=59 + 90))   # lệch 3 bước: từ chối
        self.assertIsNone(TF.verify(self.RFC_SECRET, "000000", now=5))         # sát mốc 0: bước âm bị bỏ qua, không lỗi
        self.assertEqual(TF.verify(self.RFC_SECRET, "287082", now=5), 1)       # bước 1 vẫn nằm trong cửa sổ +1

    def test_a_code_can_only_be_used_once(self):
        self.assertIsNone(TF.verify(self.RFC_SECRET, "287082", last_step=1, now=59))
        self.assertIsNone(TF.verify(self.RFC_SECRET, "287082", last_step=5, now=59))     # mã cũ hơn mã đã dùng
        self.assertEqual(TF.verify(self.RFC_SECRET, "287082", last_step=0, now=59), 1)

    def test_malformed_codes_are_rejected(self):
        for bad in ("", None, "12345", "1234567", "abcdef", "12 34 5a", "٠٠٠٠٠٠"):
            self.assertIsNone(TF.verify(self.RFC_SECRET, bad, now=59), bad)
        self.assertIsNone(TF.verify("", "287082", now=59))

    def test_secret_is_encrypted_and_bound_to_secret_key(self):
        s = TF.new_secret()
        enc = TF.encrypt(s, "khoa-1")
        self.assertNotIn(s, enc)
        self.assertEqual(TF.decrypt(enc, "khoa-1"), s)
        self.assertIsNone(TF.decrypt(enc, "khoa-2"))                          # đổi SECRET_KEY → không giải mã được
        self.assertIsNone(TF.decrypt("", "khoa-1"))
        self.assertIsNone(TF.decrypt("rac-khong-phai-token", "khoa-1"))

    def test_provisioning_uri_and_qr(self):
        s = TF.new_secret()
        uri = TF.provisioning_uri(s, "a@bvtd.vn", "BVĐK Tâm Đức")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn(f"secret={s}", uri)
        self.assertIn("issuer=", uri)
        svg = TF.qr_svg(uri)
        self.assertTrue(svg.startswith("<svg"))
        self.assertNotIn("mm", svg.split(">")[0])                              # không kích thước cố định → co giãn theo CSS
        self.assertEqual(TF.format_secret("ABCDEFGH"), "ABCD EFGH")

    def test_fail_limiter(self):
        lim = TF.FailLimiter(limit=3, window=100)
        for i in range(2):
            lim.fail("a", now=1000 + i)
        self.assertFalse(lim.locked("a", now=1002))
        lim.fail("a", now=1002)
        self.assertTrue(lim.locked("a", now=1003))
        self.assertFalse(lim.locked("b", now=1003))
        self.assertFalse(lim.locked("a", now=1000 + 101 + 3))                  # hết cửa sổ → mở lại
        lim.fail("a"); lim.reset("a")
        self.assertFalse(lim.locked("a"))


# ── Luồng đăng nhập ──
class FakeAdmins:
    def __init__(self):
        self.rows = {EMAIL: {"email": EMAIL, "name": "Quản trị", "picture": "", "is_active": True, "totp_secret": "", "totp_last_step": 0}}
        self.touched, self.boom_step = [], None

    def get_active(self, email):
        r = self.rows.get(email)
        return dict(r) if r and r["is_active"] else None

    def verify(self, email, password):
        return self.get_active(email) if password == GOOD_PASSWORD else None

    def touch_login(self, email, name="", picture=""):
        self.touched.append((email, name, picture))

    def set_totp(self, email, enc, step, expect_old=""):
        r = self.rows[email]
        if r["totp_secret"] != expect_old:
            return False
        r.update(totp_secret=enc, totp_last_step=step)
        return True

    def use_totp_step(self, email, step):
        if self.boom_step:
            raise self.boom_step
        r = self.rows[email]
        if r["totp_last_step"] < step:
            r["totp_last_step"] = step
            return True
        return False


@unittest.skipUnless(_real_flask_login(), "flask_login là bản giả trong môi trường test")
class TwoFactorFlowTests(AppTestCase):
    def setUp(self):
        super().setUp()
        A.current_user = _real_flask_login().current_user
        keys = ("LOGIN_DISABLED", "TWO_FACTOR_ENABLED", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
        old = {k: A.app.config.get(k) for k in keys}
        A.app.config.update(LOGIN_DISABLED=False, TWO_FACTOR_ENABLED=True, GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="sec")
        self.addCleanup(lambda: A.app.config.update(old))
        self.key = A.app.config["SECRET_KEY"]
        self.admins, self.events = FakeAdmins(), []
        A._otp_limiter.reset()
        self.addCleanup(A._otp_limiter.reset)
        self.google = FakeGoogle(info={"email": EMAIL, "email_verified": True, "name": "Nguyễn Admin", "picture": "http://p"})
        for p in (mock.patch.object(A, "_admins", lambda: self.admins),
                  mock.patch.object(A, "_log", lambda ev, **kw: self.events.append((ev, kw))),
                  mock.patch.object(A, "oauth", mock.Mock(google=self.google))):
            p.start()
            self.addCleanup(p.stop)

    # tiện ích
    def password_login(self, **q):
        return self.client.post("/login", data={"email": EMAIL, "password": GOOD_PASSWORD}, query_string=q)

    def logged_in(self):
        return self.client.get("/api/session-status").status_code == 200

    def enroll(self):
        """Đưa tài khoản vào trạng thái ĐÃ thiết lập 2 bước; trả về mã bí mật."""
        secret = TF.new_secret()
        self.admins.rows[EMAIL].update(totp_secret=TF.encrypt(secret, self.key), totp_last_step=0)
        return secret

    def session_secret(self):
        with self.client.session_transaction() as s:
            return TF.decrypt(s.get("otp_setup", ""), self.key)

    def post_code(self, code, path="/login/2fa"):
        return self.client.post(path, data={"code": code})

    def flash_text(self, r):
        return re.sub(r"\s+", " ", self.client.get("/login").get_data(as_text=True))

    def ev(self, name):
        return [kw for e, kw in self.events if e == name]

    # ── bất biến: chưa có mã đúng thì chưa vào ──
    def test_password_alone_is_not_enough(self):
        self.enroll()
        r = self.password_login()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login/2fa"), r.headers["Location"])
        self.assertFalse(self.logged_in())
        self.assertEqual(self.ev("login_ok"), [])
        self.assertEqual(self.admins.touched, [])
        self.assertEqual(self.client.get("/").status_code, 302)                   # trang admin vẫn bị chặn

    def test_login_user_is_called_only_where_expected(self):
        """Không có đường tắt: đăng nhập chỉ xảy ra ở _finish_login (sau OTP). Ngoại lệ DUY NHẤT: đổi email khi ĐÃ đăng nhập
        (account_confirm_email, có @login_required) phải cấp lại phiên theo email mới."""
        tree = ast.parse((Path(A.__file__).parent / "app.py").read_text(encoding="utf-8"))
        owners = []
        for fn in (f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)):
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "login_user":
                    owners.append(fn.name)
        self.assertEqual(sorted(set(owners)), ["_finish_login", "account_confirm_email"])
        self.assertEqual(owners.count("_finish_login"), 1)
        confirm = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "account_confirm_email")
        self.assertIn("login_required", [getattr(d, "id", "") for d in confirm.decorator_list])

    def test_enrolled_user_logs_in_with_correct_code(self):
        secret = self.enroll()
        self.password_login(next="/bao-cao")
        page = self.client.get("/login/2fa").get_data(as_text=True)
        self.assertIn('name="code"', page)
        self.assertIn("Google Authenticator", page)
        r = self.post_code(pyotp.TOTP(secret).now())
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/bao-cao"), r.headers["Location"])    # quay lại trang đang mở
        self.assertTrue(self.logged_in())
        self.assertEqual([kw["detail"] for kw in self.ev("login_ok")], ["Email + mật khẩu + OTP"])
        self.assertEqual(len(self.admins.touched), 1)
        with self.client.session_transaction() as s:
            self.assertNotIn("otp_pending", s)
            self.assertIn("last_active", s)

    def test_wrong_code_is_rejected(self):
        secret = self.enroll()
        self.password_login()
        for bad in ("000000", "abcdef", "", "12345"):
            if pyotp.TOTP(secret).now() == bad:
                continue
            r = self.post_code(bad)
            self.assertEqual(r.status_code, 200, bad)
            self.assertIn("Mã không đúng", r.get_data(as_text=True))
        self.assertFalse(self.logged_in())
        self.assertEqual(len(self.ev("login_fail")), 4)

    def test_previous_step_is_tolerated_for_slow_phone_clocks(self):
        secret = self.enroll()
        self.password_login()
        self.post_code(pyotp.TOTP(secret).at(time.time() - 30))
        self.assertTrue(self.logged_in())

    def test_a_code_cannot_be_replayed(self):
        secret = self.enroll()
        code = pyotp.TOTP(secret).now()
        self.password_login()
        self.post_code(code)
        self.assertTrue(self.logged_in())
        self.client.get("/logout")
        self.assertFalse(self.logged_in())
        self.password_login()                                                     # kẻ xem lén được mã vừa dùng
        r = self.post_code(code)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.logged_in())

    def test_five_wrong_codes_force_a_fresh_login(self):
        secret = self.enroll()
        self.password_login()
        bad = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
        for _ in range(A.OTP_MAX_TRIES - 1):
            self.assertEqual(self.post_code(bad).status_code, 200)
        r = self.post_code(bad)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertIn("quá 5 lần", self.flash_text(r))
        r = self.post_code(pyotp.TOTP(secret).now())                               # mã ĐÚNG cũng không còn dùng được lượt này
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertFalse(self.logged_in())

    def test_per_email_lockout_across_sessions(self):
        secret = self.enroll()
        for _ in range(10):
            A._otp_limiter.fail(EMAIL)
        self.password_login()
        r = self.post_code(pyotp.TOTP(secret).now())
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertIn("tạm khoá", self.flash_text(r))
        self.assertFalse(self.logged_in())

    def test_pending_login_expires(self):
        secret = self.enroll()
        self.password_login()
        with self.client.session_transaction() as s:
            p = dict(s["otp_pending"]); p["t"] -= A.OTP_PENDING_SECONDS + 5; s["otp_pending"] = p
        r = self.post_code(pyotp.TOTP(secret).now())
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertIn("hết hạn", self.flash_text(r))
        self.assertFalse(self.logged_in())

    def test_otp_pages_need_a_pending_login(self):
        for path in ("/login/2fa", "/login/2fa/setup"):
            for method in ("get", "post"):
                r = getattr(self.client, method)(path)
                self.assertEqual(r.status_code, 302, (method, path))
                self.assertTrue(r.headers["Location"].endswith("/login"), (method, path))

    def test_account_disabled_midway(self):
        secret = self.enroll()
        self.password_login()
        self.admins.rows[EMAIL]["is_active"] = False
        r = self.post_code(pyotp.TOTP(secret).now())
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertFalse(self.logged_in())

    def test_cancel_clears_the_pending_login(self):
        self.enroll()
        self.password_login()
        self.client.get("/login/2fa/cancel")
        self.assertTrue(self.client.get("/login/2fa").headers["Location"].endswith("/login"))

    def test_database_error_is_reported_not_500(self):
        secret = self.enroll()
        self.password_login()
        self.admins.boom_step = A.DB.DBError("down")
        r = self.post_code(pyotp.TOTP(secret).now())
        self.assertEqual(r.status_code, 200)
        self.assertIn("lỗi cơ sở dữ liệu", r.get_data(as_text=True))
        self.assertFalse(self.logged_in())

    # ── thiết lập lần đầu ──
    def test_first_login_goes_to_setup_and_shows_qr(self):
        r = self.password_login()
        self.assertTrue(r.headers["Location"].endswith("/login/2fa"))
        r = self.client.get("/login/2fa")
        self.assertTrue(r.headers["Location"].endswith("/login/2fa/setup"))
        page = self.client.get("/login/2fa/setup").get_data(as_text=True)
        secret = self.session_secret()
        self.assertTrue(secret)
        self.assertIn("<svg", page)
        self.assertIn(TF.format_secret(secret), page)
        self.assertIn("otpauth://totp/", page)
        self.assertNotIn(secret, self.admins.rows[EMAIL]["totp_secret"])         # chưa lưu gì vào DB khi chưa xác nhận mã
        self.assertEqual(self.admins.rows[EMAIL]["totp_secret"], "")
        self.assertFalse(self.logged_in())

    def test_setup_requires_a_correct_code_then_enrolls_and_logs_in(self):
        self.password_login()
        self.client.get("/login/2fa/setup")
        secret = self.session_secret()
        r = self.post_code("000000" if pyotp.TOTP(secret).now() != "000000" else "111111", "/login/2fa/setup")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.admins.rows[EMAIL]["totp_secret"], "")              # sai mã → không bật
        self.assertFalse(self.logged_in())
        r = self.post_code(pyotp.TOTP(secret).now(), "/login/2fa/setup")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.logged_in())
        self.assertEqual(TF.decrypt(self.admins.rows[EMAIL]["totp_secret"], self.key), secret)   # lưu dạng mã hoá, giải ra đúng
        self.assertEqual(len(self.ev("otp_setup")), 1)
        self.assertIn("thiết lập lần đầu", self.ev("login_ok")[0]["detail"])

    def test_setup_secret_is_stable_across_page_reloads(self):
        self.password_login()
        self.client.get("/login/2fa/setup")
        first = self.session_secret()
        self.client.get("/login/2fa/setup")
        self.assertEqual(self.session_secret(), first)                            # quét xong lỡ F5 không làm đổi mã

    def test_enrolled_account_cannot_be_re_enrolled_by_someone_with_the_password(self):
        secret = self.enroll()
        self.password_login()
        r = self.client.get("/login/2fa/setup")
        self.assertTrue(r.headers["Location"].endswith("/login/2fa"))
        r = self.client.post("/login/2fa/setup", data={"code": "123456"})
        self.assertTrue(r.headers["Location"].endswith("/login/2fa"))
        self.assertEqual(TF.decrypt(self.admins.rows[EMAIL]["totp_secret"], self.key), secret)   # mã bí mật cũ còn nguyên

    def test_parallel_enrollment_cannot_overwrite(self):
        self.password_login()
        self.client.get("/login/2fa/setup")
        mine = self.session_secret()
        other = TF.encrypt(TF.new_secret(), self.key)
        self.admins.rows[EMAIL]["totp_secret"] = other                            # lượt khác thiết lập xong trước
        r = self.post_code(pyotp.TOTP(mine).now(), "/login/2fa/setup")
        self.assertFalse(self.logged_in())
        self.assertEqual(self.admins.rows[EMAIL]["totp_secret"], other)

    def test_changed_secret_key_means_re_enrolling_not_locking_out(self):
        old = TF.encrypt(TF.new_secret(), "khoa-cu-da-bi-doi")
        self.admins.rows[EMAIL]["totp_secret"] = old
        self.password_login()
        self.assertTrue(self.client.get("/login/2fa").headers["Location"].endswith("/login/2fa/setup"))
        self.client.get("/login/2fa/setup")
        secret = self.session_secret()
        self.post_code(pyotp.TOTP(secret).now(), "/login/2fa/setup")
        self.assertTrue(self.logged_in())
        self.assertEqual(TF.decrypt(self.admins.rows[EMAIL]["totp_secret"], self.key), secret)

    # ── Google cũng phải qua OTP ──
    def test_google_login_also_needs_the_code(self):
        secret = self.enroll()
        r = self.client.get("/login/google/callback", query_string={"code": "c", "state": "s"})
        self.assertTrue(r.headers["Location"].endswith("/login/2fa"), r.headers["Location"])
        self.assertFalse(self.logged_in())
        self.assertEqual(self.admins.touched, [])
        self.post_code(pyotp.TOTP(secret).now())
        self.assertTrue(self.logged_in())
        self.assertEqual(self.admins.touched, [(EMAIL, "Nguyễn Admin", "http://p")])      # tên/ảnh Google lưu khi vào thật
        self.assertEqual([kw["detail"] for kw in self.ev("login_ok")], ["Google + OTP"])

    # ── tắt bằng cấu hình ──
    def test_can_be_switched_off_by_config(self):
        A.app.config.update(TWO_FACTOR_ENABLED=False)
        r = self.password_login()
        self.assertFalse(r.headers["Location"].endswith("/login/2fa"))
        self.assertTrue(self.logged_in())
        self.assertEqual([kw["detail"] for kw in self.ev("login_ok")], ["Email + mật khẩu"])


if __name__ == "__main__":
    unittest.main()
