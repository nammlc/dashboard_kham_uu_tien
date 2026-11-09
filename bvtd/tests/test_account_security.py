"""Quên mật khẩu bằng mã OTP (không cần email), đổi mật khẩu, đổi email đăng nhập, và đăng xuất phiên cũ khi mật khẩu đổi.
Bất biến: mã OTP chỉ dùng 1 lần và không bị tiêu thụ khi mật khẩu nhập chưa hợp lệ; lý do thất bại luôn giống nhau (không lộ email nào là admin);
đổi mật khẩu đăng xuất mọi thiết bị KHÁC; đổi email phải có mật khẩu/OTP + bấm liên kết gửi tới email MỚI."""
import re
import time
import unittest

import pyotp

from services import admin_users as ADM, tokens as TK, twofa as TF
from tests.helpers import A
from tests.test_account_recovery import BASE, EMAIL, GG_EMAIL, GOOD_PASSWORD, RecoveryBase

NEW_PW = "mat-khau-moi-1"
NEW_EMAIL = "moi@bvtd.vn"


def next_code(secret, ahead=1):
    return pyotp.TOTP(secret).at(time.time() + 30 * ahead)         # mã bước kế tiếp (mã bước hiện tại thường vừa dùng để đăng nhập)


class CheckEmailTests(unittest.TestCase):
    def test_check_email(self):
        self.assertEqual(ADM.check_email("  A@Bvtd.VN "), "a@bvtd.vn")
        for bad in ("", "a", "a@b", "@b.vn", "a b@c.vn", "a@@b.vn", "x" * 130 + "@b.vn", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ADM.check_email(bad)

    def test_token_extra_is_signed(self):
        t = TK.make("k", TK.EMAIL_CHANGE, "a@b.vn", "m", extra="moi@b.vn")
        self.assertEqual(TK.read_ex("k", TK.EMAIL_CHANGE, t), ("a@b.vn", TK.fingerprint("m"), "moi@b.vn"))
        self.assertIsNone(TK.read_ex("k", TK.PASSWORD_RESET, t))                       # đúng mục đích mới đọc được
        self.assertEqual(TK.read("k", TK.EMAIL_CHANGE, t)[0], "a@b.vn")                # hàm cũ vẫn dùng được


class ForgotByOtpTests(RecoveryBase):
    def reset(self, code, email=EMAIL, pw=NEW_PW, pw2=None, **kw):
        return self.post("/quen-mat-khau", action="otp", email=email, code=code,
                         password=pw, password2=pw if pw2 is None else pw2, **kw)

    def failure_text(self, r):
        self.assertEqual(r.status_code, 302)
        return re.sub(r"\s+", " ", self.client.get("/quen-mat-khau").get_data(as_text=True))

    def test_works_without_any_email_setup(self):
        A.app.config.update(SMTP_HOST="")
        secret = self.enroll()
        page = self.client.get("/quen-mat-khau").get_data(as_text=True)
        self.assertIn('value="otp"', page)
        self.assertNotIn('value="email"', page)                                         # chưa có SMTP: chỉ còn cách dùng mã OTP
        r = self.reset(pyotp.TOTP(secret).now())
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertEqual(self.admins.password, NEW_PW)
        self.assertEqual(self.mails, [])
        self.assertEqual(len(self.ev("password_reset")), 1)
        self.assertIn("Đã đặt mật khẩu mới", self.page())

    def test_both_methods_are_offered_when_mail_is_on_and_owner_is_notified(self):
        secret = self.enroll()
        page = self.client.get("/quen-mat-khau").get_data(as_text=True)
        self.assertIn('value="email"', page)
        self.assertIn('value="otp"', page)
        self.reset(pyotp.TOTP(secret).now())
        self.assertEqual([m[0] for m in self.mails], [EMAIL])
        self.assertIn("vừa được đặt lại", self.mails[0][1])

    def test_new_password_still_needs_the_otp_at_login(self):
        secret = self.enroll()
        self.reset(pyotp.TOTP(secret).now())
        r = self.client.post("/login", data={"email": EMAIL, "password": NEW_PW})
        self.assertTrue(r.headers["Location"].endswith("/login/2fa"))
        self.assertFalse(self.logged_in())

    def test_every_failure_looks_the_same(self):
        secret = self.enroll()
        good = pyotp.TOTP(secret).now()
        bad_code = "000000" if good != "000000" else "111111"
        seen = set()
        for who, code in ((EMAIL, bad_code), ("khong-co@bvtd.vn", good), (GG_EMAIL, good), ("off@bvtd.vn", good), (EMAIL, "abc")):
            r = self.reset(code, email=who)
            seen.add(re.search(r"Email hoặc mã xác thực không đúng[^<]*", self.failure_text(r)).group(0))
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.admins.password, GOOD_PASSWORD)
        self.assertEqual(len(self.ev("login_fail")), 5)

    def test_a_code_is_single_use(self):
        secret = self.enroll()
        code = pyotp.TOTP(secret).now()
        self.assertTrue(self.reset(code).headers["Location"].endswith("/login"))
        self.assertEqual(self.admins.password, NEW_PW)
        self.reset(code, pw="ke-gian-5678")                                              # kẻ nhìn lén dùng lại đúng mã đó
        self.assertEqual(self.admins.password, NEW_PW)

    def test_bad_new_password_does_not_burn_the_code(self):
        secret = self.enroll()
        code = pyotp.TOTP(secret).now()
        self.reset(code, pw="ngan")
        self.reset(code, pw=NEW_PW, pw2="khac-nhau-123")
        self.assertEqual(self.admins.password, GOOD_PASSWORD)
        self.assertEqual(self.admins.rows[EMAIL]["totp_last_step"], 0)                  # mã chưa bị tiêu thụ
        self.assertTrue(self.reset(code).headers["Location"].endswith("/login"))        # cùng mã vẫn dùng được khi nhập đúng
        self.assertEqual(self.admins.password, NEW_PW)

    def test_lockouts(self):
        secret = self.enroll()
        for _ in range(10):
            A._otp_limiter.fail(f"reset:{EMAIL}")
        self.reset(pyotp.TOTP(secret).now())
        self.assertEqual(self.admins.password, GOOD_PASSWORD)
        A._otp_limiter.reset()
        for _ in range(20):
            A._reset_ip_limiter.fail("127.0.0.1")
        self.reset(pyotp.TOTP(secret).now())
        self.assertEqual(self.admins.password, GOOD_PASSWORD)

    def test_repeated_wrong_codes_lock_the_account_out_of_guessing(self):
        secret = self.enroll()
        bad = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
        for _ in range(10):
            self.reset(bad)
        self.reset(pyotp.TOTP(secret).now())                                              # mã đúng cũng bị chặn: đã khoá
        self.assertEqual(self.admins.password, GOOD_PASSWORD)

    def test_not_enrolled_account_cannot_use_it(self):
        self.reset("123456")
        self.assertEqual(self.admins.password, GOOD_PASSWORD)

    def test_csrf_is_required(self):
        secret = self.enroll()
        self.client.get("/quen-mat-khau")
        self.client.post("/quen-mat-khau", data={"action": "otp", "email": EMAIL, "code": pyotp.TOTP(secret).now(),
                                                 "password": NEW_PW, "password2": NEW_PW})
        self.assertEqual(self.admins.password, GOOD_PASSWORD)

    def test_can_be_switched_off(self):
        secret = self.enroll()
        for cfg in ({"PASSWORD_RESET_VIA_OTP": False}, {"TWO_FACTOR_ENABLED": False}):
            A.app.config.update(cfg)
            self.reset(pyotp.TOTP(secret).now())
            self.assertEqual(self.admins.password, GOOD_PASSWORD, cfg)
            self.assertNotIn('value="otp"', self.client.get("/quen-mat-khau").get_data(as_text=True))
            A.app.config.update(PASSWORD_RESET_VIA_OTP=True, TWO_FACTOR_ENABLED=True)

    def test_database_down_is_reported_not_500(self):
        secret = self.enroll()
        self.admins.db_down = True
        r = self.reset(pyotp.TOTP(secret).now())
        self.assertEqual(r.status_code, 302)
        self.assertIn("cơ sở dữ liệu", self.client.get("/quen-mat-khau").get_data(as_text=True))


class PasswordChangeTests(RecoveryBase):
    def setUp(self):
        super().setUp()
        self.secret = self.enroll()
        self.full_login(self.secret)

    def change(self, old=GOOD_PASSWORD, pw=NEW_PW, pw2=None, client=None):
        return self.post("/tai-khoan/doi-mat-khau", client=client, old_password=old, password=pw, password2=pw if pw2 is None else pw2)

    def test_needs_login(self):
        c = A.app.test_client()
        self.assertEqual(c.get("/tai-khoan/doi-mat-khau").status_code, 302)
        self.assertEqual(c.post("/tai-khoan/doi-mat-khau", data={"password": "x" * 9}).status_code, 302)

    def test_change_password_and_other_devices_are_logged_out(self):
        other = A.app.test_client()                                                      # thiết bị thứ hai đang đăng nhập
        other.post("/login", data={"email": EMAIL, "password": GOOD_PASSWORD})
        other.post("/login/2fa", data={"code": next_code(self.secret)})
        self.assertTrue(self.logged_in(other))
        r = self.change()
        self.assertTrue(r.headers["Location"].endswith("/tai-khoan"))
        self.assertEqual(self.admins.password, NEW_PW)
        self.assertTrue(self.logged_in())                                                # thiết bị đang đổi vẫn ở lại
        self.assertFalse(self.logged_in(other))                                          # thiết bị khác bị đăng xuất
        self.assertEqual(len(self.ev("password_change")), 1)
        self.assertEqual([m[0] for m in self.mails], [EMAIL])
        self.assertIn("vừa được đổi", self.mails[0][1])

    def test_wrong_current_password(self):
        self.change(old="sai-mat-khau")
        self.assertEqual(self.admins.password, GOOD_PASSWORD)
        self.assertIn("Mật khẩu hiện tại không đúng", self.client.get("/tai-khoan/doi-mat-khau").get_data(as_text=True))

    def test_bad_new_passwords(self):
        for kw, text in (({"pw": "ngan"}, "ít nhất 8"), ({"pw2": "khac-nhau-123"}, "chưa giống nhau"), ({"pw": GOOD_PASSWORD}, "phải khác")):
            self.change(**kw)
            self.assertIn(text, self.client.get("/tai-khoan/doi-mat-khau").get_data(as_text=True), kw)
        self.assertEqual(self.admins.password, GOOD_PASSWORD)

    def test_guessing_the_current_password_is_limited(self):
        for _ in range(5):
            self.change(old="sai-mat-khau")
        r = self.change()                                                                # đúng mật khẩu nhưng đã bị khoá
        self.assertTrue(r.headers["Location"].endswith("/tai-khoan"))
        self.assertEqual(self.admins.password, GOOD_PASSWORD)
        self.assertEqual(len(self.ev("login_fail")), 5)

    def test_csrf_is_required(self):
        self.client.get("/tai-khoan/doi-mat-khau")
        self.client.post("/tai-khoan/doi-mat-khau", data={"old_password": GOOD_PASSWORD, "password": NEW_PW, "password2": NEW_PW})
        self.assertEqual(self.admins.password, GOOD_PASSWORD)

    def test_password_login_session_must_type_the_old_password(self):
        self.assertIn('name="old_password"', self.client.get("/tai-khoan/doi-mat-khau").get_data(as_text=True))

    def test_google_session_may_set_a_new_password_without_the_old_one(self):
        """Người QUÊN mật khẩu vào bằng Google rồi đặt mật khẩu mới."""
        c = A.app.test_client()
        self.google.info = {"email": EMAIL, "email_verified": True, "name": "G", "picture": ""}
        c.get("/login/google/callback", query_string={"code": "c", "state": "s"})
        c.post("/login/2fa", data={"code": next_code(self.secret)})
        self.assertTrue(self.logged_in(c))
        self.assertNotIn('name="old_password"', c.get("/tai-khoan/doi-mat-khau").get_data(as_text=True))
        r = self.post("/tai-khoan/doi-mat-khau", client=c, password=NEW_PW, password2=NEW_PW)
        self.assertTrue(r.headers["Location"].endswith("/tai-khoan"))
        self.assertEqual(self.admins.password, NEW_PW)

    def test_google_only_account_can_create_a_password(self):
        secret = self.enroll(GG_EMAIL)
        c = A.app.test_client()
        c.get("/login/google/callback", query_string={"code": "c", "state": "s"})
        c.post("/login/2fa", data={"code": pyotp.TOTP(secret).now()})
        self.assertTrue(self.logged_in(c))
        page = c.get("/tai-khoan/doi-mat-khau").get_data(as_text=True)
        self.assertNotIn('name="old_password"', page)
        self.assertIn("Đặt mật khẩu", page)
        self.post("/tai-khoan/doi-mat-khau", client=c, password=NEW_PW, password2=NEW_PW)
        self.assertTrue(self.admins.rows[GG_EMAIL]["password_hash"])
        self.assertTrue(self.logged_in(c))

    def test_sessions_from_before_the_upgrade_are_adopted(self):
        with self.client.session_transaction() as s:
            s.pop("pwv", None)
        self.assertTrue(self.logged_in())
        with self.client.session_transaction() as s:
            self.assertIn("pwv", s)

    def test_email_reset_link_also_logs_out_existing_sessions(self):
        other = A.app.test_client()
        t = TK.make(self.key, TK.PASSWORD_RESET, EMAIL, self.admins.rows[EMAIL]["password_hash"])
        self.post(f"/dat-lai-mat-khau/{t}", client=other, password=NEW_PW, password2=NEW_PW)
        self.assertEqual(self.admins.password, NEW_PW)
        self.assertFalse(self.logged_in())                                               # phiên đang mở trước đó mất hiệu lực

    def test_otp_reset_also_logs_out_existing_sessions(self):
        other = A.app.test_client()
        self.post("/quen-mat-khau", client=other, action="otp", email=EMAIL, code=next_code(self.secret),
                  password=NEW_PW, password2=NEW_PW)
        self.assertEqual(self.admins.password, NEW_PW)
        self.assertFalse(self.logged_in())


class EmailChangeTests(RecoveryBase):
    def setUp(self):
        super().setUp()
        self.secret = self.enroll()
        self.full_login(self.secret)

    def request(self, new=NEW_EMAIL, password=GOOD_PASSWORD, code=None, client=None):
        return self.post("/tai-khoan/doi-email", client=client, new_email=new, password=password,
                         code=next_code(self.secret) if code is None else code)

    def confirm_path(self):
        return re.search(re.escape(BASE) + r"(/tai-khoan/xac-nhan-email/\S+)", self.mails[-1][2]).group(1)

    def test_needs_login(self):
        c = A.app.test_client()
        for path in ("/tai-khoan/doi-email", "/tai-khoan/xac-nhan-email/abc"):
            self.assertEqual(c.get(path).status_code, 302, path)

    def test_form_asks_for_password_and_otp(self):
        page = self.client.get("/tai-khoan/doi-email").get_data(as_text=True)
        self.assertIn('name="new_email"', page)
        self.assertIn('name="password"', page)
        self.assertIn('name="code"', page)

    def test_without_mail_the_page_explains_and_nothing_is_sent(self):
        A.app.config.update(SMTP_HOST="")
        page = self.client.get("/tai-khoan/doi-email").get_data(as_text=True)
        self.assertIn("admin-email", page)
        self.assertNotIn('name="new_email"', page)
        self.request()
        self.assertEqual(self.mails, [])
        self.assertIn("admin-email", self.client.get("/tai-khoan").get_data(as_text=True))

    def test_full_change(self):
        r = self.request(new="  MOI@Bvtd.vn ")
        self.assertTrue(r.headers["Location"].endswith("/tai-khoan"))
        self.assertEqual(len(self.mails), 1)
        to, subject, body = self.mails[0]
        self.assertEqual(to, NEW_EMAIL)                                                  # gửi tới email MỚI (chứng minh sở hữu)
        self.assertIn("Xác nhận", subject)
        self.assertIn(EMAIL, body)
        self.assertIn(EMAIL, self.admins.rows)                                           # chưa đổi gì cho tới khi bấm liên kết
        path = self.confirm_path()
        page = self.client.get(path).get_data(as_text=True)
        self.assertIn(EMAIL, page); self.assertIn(NEW_EMAIL, page)
        self.assertIn(EMAIL, self.admins.rows)                                           # GET (trình quét thư) không làm đổi
        r = self.post(path)
        self.assertTrue(r.headers["Location"].endswith("/tai-khoan"))
        self.assertIn(NEW_EMAIL, self.admins.rows)
        self.assertNotIn(EMAIL, self.admins.rows)
        self.assertEqual(TF.decrypt(self.admins.rows[NEW_EMAIL]["totp_secret"], self.key), self.secret)   # 2 bước & mật khẩu giữ nguyên
        self.assertEqual(self.admins.rows[NEW_EMAIL]["password_hash"], "h1")
        self.assertTrue(self.logged_in())                                                # phiên này tiếp tục với email mới
        self.assertIn(NEW_EMAIL, self.client.get("/tai-khoan").get_data(as_text=True))
        self.assertEqual([kw["detail"] for kw in self.ev("email_change")], [f"{EMAIL} → {NEW_EMAIL}"])
        self.assertEqual(self.mails[-1][0], EMAIL)                                       # email CŨ nhận thông báo
        self.assertIn("đã được đổi", self.mails[-1][1])
        # email cũ không đăng nhập được nữa, email mới đăng nhập được (vẫn cần OTP)
        c = A.app.test_client()
        r = c.post("/login", data={"email": EMAIL, "password": GOOD_PASSWORD})
        self.assertEqual(r.status_code, 200)                                              # email cũ: đăng nhập thất bại (không chuyển sang bước OTP)
        self.assertNotIn("Location", r.headers)
        self.assertTrue(c.post("/login", data={"email": NEW_EMAIL, "password": GOOD_PASSWORD}).headers["Location"].endswith("/login/2fa"))

    def test_link_cannot_be_reused_or_used_by_someone_else(self):
        self.request()
        path = self.confirm_path()
        self.post(path)
        self.assertEqual(self.client.get(path).status_code, 302)                         # đã dùng: tài khoản giờ là email mới
        self.assertNotIn("Xác nhận đổi email", self.client.get(path, follow_redirects=True).get_data(as_text=True))
        other_user = TK.make(self.key, TK.EMAIL_CHANGE, GG_EMAIL, "x", extra="ke-gian@bvtd.vn")
        self.assertTrue(self.client.get(f"/tai-khoan/xac-nhan-email/{other_user}").headers["Location"].endswith("/tai-khoan"))
        self.assertNotIn("ke-gian@bvtd.vn", self.admins.rows)

    def test_logged_out_visitor_is_sent_to_login_first(self):
        self.request()
        path = self.confirm_path()
        c = A.app.test_client()
        r = c.get(path)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertIn(EMAIL, self.admins.rows)

    def test_link_dies_when_password_or_phone_changes(self):
        self.request()
        path = self.confirm_path()
        self.admins.set_password(EMAIL, "mat-khau-khac-9")                                # đổi mật khẩu sau khi yêu cầu
        with self.client.session_transaction() as s:
            s["pwv"] = TK.fingerprint(self.admins.rows[EMAIL]["password_hash"])
        self.post(path)
        self.assertIn(EMAIL, self.admins.rows)
        self.assertNotIn(NEW_EMAIL, self.admins.rows)

    def test_proof_of_ownership_is_required_and_limited(self):
        self.request(password="sai-mat-khau")
        self.request(code="000000")
        self.request(code="abc")
        self.assertEqual(self.mails, [])
        for _ in range(5):
            self.request(password="sai-mat-khau")
        self.request()                                                                    # đúng hết nhưng đã bị khoá
        self.assertEqual(self.mails, [])

    def test_otp_cannot_be_replayed_for_this(self):
        self.request(code=pyotp.TOTP(self.secret).now())                                  # mã vừa dùng để đăng nhập
        self.assertEqual(self.mails, [])

    def test_rejects_bad_targets(self):
        for new in ("khong-hop-le", EMAIL, GG_EMAIL, "a@b"):
            self.request(new=new)
        self.assertEqual(self.mails, [])
        self.assertEqual(self.client.get("/tai-khoan/doi-email").status_code, 200)

    def test_taken_by_someone_else_in_the_meantime(self):
        self.request()
        path = self.confirm_path()
        self.admins.rows[NEW_EMAIL] = dict(self.admins.rows[GG_EMAIL], email=NEW_EMAIL)   # người khác chiếm email này trước
        self.post(path)
        self.assertIn(EMAIL, self.admins.rows)
        self.assertEqual(self.ev("email_change"), [])

    def test_requests_are_throttled(self):
        for _ in range(3):
            A._reset_email_limiter.fail(f"email:{EMAIL}")                                  # đã yêu cầu 3 lần trong giờ
        self.request()
        self.assertEqual(self.mails, [])
        self.assertIn("quá nhiều lần", self.client.get("/tai-khoan").get_data(as_text=True))

    def test_csrf_is_required(self):
        self.client.get("/tai-khoan/doi-email")
        self.client.post("/tai-khoan/doi-email", data={"new_email": NEW_EMAIL, "password": GOOD_PASSWORD, "code": next_code(self.secret)})
        self.assertEqual(self.mails, [])
        self.request()
        path = self.confirm_path()
        self.client.get(path)
        self.client.post(path, data={})
        self.assertIn(EMAIL, self.admins.rows)

    def test_google_only_account_needs_only_the_otp(self):
        secret = self.enroll(GG_EMAIL)
        c = A.app.test_client()
        c.get("/login/google/callback", query_string={"code": "c", "state": "s"})
        c.post("/login/2fa", data={"code": pyotp.TOTP(secret).now()})
        page = c.get("/tai-khoan/doi-email").get_data(as_text=True)
        self.assertNotIn('name="password"', page)
        self.assertIn('name="code"', page)
        self.post("/tai-khoan/doi-email", client=c, new_email=NEW_EMAIL, code=next_code(secret))
        self.assertEqual([m[0] for m in self.mails], [NEW_EMAIL])

    def test_account_without_password_or_two_factor_cannot_change_email(self):
        A.app.config.update(TWO_FACTOR_ENABLED=False)
        c = A.app.test_client()
        c.get("/login/google/callback", query_string={"code": "c", "state": "s"})
        self.assertTrue(self.logged_in(c))
        self.post("/tai-khoan/doi-email", client=c, new_email=NEW_EMAIL)
        self.assertEqual(self.mails, [])


if __name__ == "__main__":
    unittest.main()
