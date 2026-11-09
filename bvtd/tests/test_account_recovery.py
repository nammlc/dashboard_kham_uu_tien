"""Quên mật khẩu (liên kết email), mất điện thoại (liên kết email + bước 1), đổi điện thoại khi đang đăng nhập.
Bất biến: không lộ email nào là admin; liên kết dùng 1 lần, có hạn, không lấy từ header Host; không ai thay được mã 2 bước
mà không có điện thoại cũ HOẶC (email + mật khẩu/Google); mọi lỗi báo tiếng Việt, không 500."""
import re
import secrets
import smtplib
import time
import unittest
from unittest import mock

import pyotp

from services import mailer as MAIL, tokens as TK, twofa as TF
from tests.helpers import A, AppTestCase
from tests.test_google_login import FakeGoogle, _real_flask_login
from tests.test_two_factor import EMAIL, GOOD_PASSWORD, FakeAdmins

GG_EMAIL = "gg@bvtd.vn"
BASE = "https://quanly.bvtd.vn"
CFG = {"SMTP_HOST": "smtp.test", "SMTP_PORT": 587, "SMTP_USER": "u", "SMTP_PASSWORD": "pw-bi-mat", "SMTP_FROM": "BVĐK <no-reply@bvtd.vn>",
       "SMTP_SECURITY": "starttls"}


# ── mailer ──
class MailerTests(unittest.TestCase):
    def test_configured_needs_host_and_valid_sender(self):
        self.assertTrue(MAIL.configured(CFG))
        self.assertFalse(MAIL.configured({**CFG, "SMTP_HOST": ""}))
        self.assertFalse(MAIL.configured({**CFG, "SMTP_FROM": "", "SMTP_USER": "khong-co-a-cong"}))
        self.assertTrue(MAIL.configured({**CFG, "SMTP_FROM": "", "SMTP_USER": "a@b.vn"}))      # lấy người gửi từ SMTP_USER

    def test_starttls_login_and_utf8_message(self):
        with mock.patch("smtplib.SMTP") as smtp:
            MAIL.send(CFG, "to@bvtd.vn", "Đặt lại mật khẩu", "Xin chào bạn\n")
        srv = smtp.return_value                                                  # smtplib.SMTP.__enter__ trả về chính nó
        smtp.assert_called_once_with("smtp.test", 587, timeout=MAIL.TIMEOUT_S)
        srv.starttls.assert_called_once()
        srv.login.assert_called_once_with("u", "pw-bi-mat")
        msg = srv.send_message.call_args[0][0]
        self.assertEqual(msg["To"], "to@bvtd.vn")
        self.assertIn("no-reply@bvtd.vn", msg["From"])
        self.assertIn("Xin chào bạn", msg.get_content())

    def test_ssl_mode_and_none_mode(self):
        with mock.patch("smtplib.SMTP_SSL") as ssl_cls:
            MAIL.send({**CFG, "SMTP_SECURITY": "ssl", "SMTP_PORT": 465}, "a@b.vn", "s", "b")
        self.assertEqual(ssl_cls.call_args[0][:2], ("smtp.test", 465))
        with mock.patch("smtplib.SMTP") as smtp:
            MAIL.send({**CFG, "SMTP_SECURITY": "none", "SMTP_USER": ""}, "a@b.vn", "s", "b")
        srv = smtp.return_value
        srv.starttls.assert_not_called()
        srv.login.assert_not_called()

    def test_failures_become_mailerror_without_leaking_the_password(self):
        with mock.patch("smtplib.SMTP") as smtp:
            smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad creds pw-bi-mat")
            with self.assertRaises(MAIL.MailError) as cm:
                MAIL.send(CFG, "a@b.vn", "s", "b")
        self.assertNotIn("pw-bi-mat", str(cm.exception))
        with mock.patch("smtplib.SMTP", side_effect=OSError("timed out")):
            with self.assertRaises(MAIL.MailError):
                MAIL.send(CFG, "a@b.vn", "s", "b")
        with self.assertRaises(MAIL.MailError):
            MAIL.send({}, "a@b.vn", "s", "b")


# ── token ──
class TokenTests(unittest.TestCase):
    def test_roundtrip_and_fingerprint(self):
        t = TK.make("k", TK.PASSWORD_RESET, "A@B.vn", "hash-1")
        email, fp = TK.read("k", TK.PASSWORD_RESET, t)
        self.assertEqual(email, "a@b.vn")
        self.assertTrue(TK.matches(fp, "hash-1"))
        self.assertFalse(TK.matches(fp, "hash-2"))                              # đổi mật khẩu → token cũ vô hiệu

    def test_rejects_wrong_purpose_key_tamper_and_expiry(self):
        t = TK.make("k", TK.PASSWORD_RESET, "a@b.vn", "h")
        self.assertIsNone(TK.read("k", TK.TWOFA_RESET, t))                      # liên kết đặt mật khẩu ≠ liên kết đổi điện thoại
        self.assertIsNone(TK.read("khac", TK.PASSWORD_RESET, t))
        self.assertIsNone(TK.read("k", TK.PASSWORD_RESET, t[:-2] + ("AA" if not t.endswith("AA") else "BB")))
        self.assertIsNone(TK.read("k", TK.PASSWORD_RESET, ""))
        self.assertIsNone(TK.read("k", TK.PASSWORD_RESET, None))
        self.assertIsNone(TK.read("k", TK.PASSWORD_RESET, t, max_age=-1))       # hết hạn


# ── luồng ──
class Admins(FakeAdmins):
    def __init__(self):
        super().__init__()
        self.password = GOOD_PASSWORD
        self.rows[EMAIL].update(password_hash="h1", totp_enabled_at=None)
        self.rows[GG_EMAIL] = {"email": GG_EMAIL, "name": "Chỉ Google", "picture": "", "is_active": True, "password_hash": "",
                               "totp_secret": "", "totp_last_step": 0, "totp_enabled_at": None}
        self.rows["off@bvtd.vn"] = dict(self.rows[EMAIL], email="off@bvtd.vn", is_active=False)
        self.db_down = False

    def get_active(self, email):
        if self.db_down:
            raise A.DB.DBError("down")
        return super().get_active(email)

    def verify(self, email, password):
        r = self.get_active(email)
        return r if r and r.get("password_hash") and password == self.password else None

    def get(self, email):
        r = self.rows.get(email)
        return dict(r) if r else None

    def change_email(self, old, new):
        new = A.ADM.check_email(new)
        if new in self.rows or old not in self.rows:
            return False
        r = self.rows.pop(old)
        r["email"] = new
        self.rows[new] = r
        return True

    def set_password(self, email, pw):
        if len(pw) < 8:
            raise ValueError("Mật khẩu phải có ít nhất 8 ký tự.")
        self.password = pw
        self.rows[email]["password_hash"] = f"h-{pw}"
        return True


@unittest.skipUnless(_real_flask_login(), "flask_login là bản giả trong môi trường test")
class RecoveryBase(AppTestCase):
    def setUp(self):
        super().setUp()
        A.current_user = _real_flask_login().current_user
        keys = ("LOGIN_DISABLED", "TWO_FACTOR_ENABLED", "PASSWORD_RESET_VIA_OTP", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ADMIN_BASE_URL", *CFG)
        old = {k: A.app.config.get(k) for k in keys}
        A.app.config.update(LOGIN_DISABLED=False, TWO_FACTOR_ENABLED=True, PASSWORD_RESET_VIA_OTP=True, GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="s",
                            ADMIN_BASE_URL=BASE, **CFG)
        self.addCleanup(lambda: A.app.config.update(old))
        self.key = A.app.config["SECRET_KEY"]
        self.admins, self.events, self.mails = Admins(), [], []
        for lim in (A._otp_limiter, A._reset_email_limiter, A._reset_ip_limiter, A._pw_limiter):
            lim.reset()
            self.addCleanup(lim.reset)
        self.google = FakeGoogle(info={"email": GG_EMAIL, "email_verified": True, "name": "G", "picture": ""})
        for p in (mock.patch.object(A, "_admins", lambda: self.admins),
                  mock.patch.object(A, "_log", lambda ev, **kw: self.events.append((ev, kw))),
                  mock.patch.object(A, "oauth", mock.Mock(google=self.google)),
                  mock.patch.object(A, "_send_mail_async", lambda to, subj, body: self.mails.append((to, subj, body)))):
            p.start()
            self.addCleanup(p.stop)

    # tiện ích
    def csrf(self, client=None):
        """Mã CSRF của phiên (tạo sẵn trong phiên nếu chưa có). Các trang có biểu mẫu được kiểm tra riêng là có ô name="csrf"."""
        c = client or self.client
        with c.session_transaction() as s:
            s.setdefault("csrf", "tok-" + secrets.token_hex(6))
            return s["csrf"]

    def post(self, path, client=None, **data):
        c = client or self.client
        data.setdefault("csrf", self.csrf(c))
        return c.post(path, data=data)

    def logged_in(self, client=None):
        return (client or self.client).get("/api/session-status").status_code == 200

    def enroll(self, email=EMAIL):
        secret = TF.new_secret()
        self.admins.rows[email].update(totp_secret=TF.encrypt(secret, self.key), totp_last_step=0)
        return secret

    def secret_of(self, email=EMAIL):
        return TF.decrypt(self.admins.rows[email]["totp_secret"], self.key)

    def link(self, mail_index=-1):
        m = re.search(re.escape(BASE) + r"(/\S+)", self.mails[mail_index][2])
        return m.group(1)

    def ev(self, name):
        return [kw for e, kw in self.events if e == name]

    def page(self, client=None):
        return re.sub(r"\s+", " ", (client or self.client).get("/login").get_data(as_text=True))

    def full_login(self, secret):
        self.client.post("/login", data={"email": EMAIL, "password": self.admins.password})
        self.client.post("/login/2fa", data={"code": pyotp.TOTP(secret).now()})
        self.assertTrue(self.logged_in())


class ForgotPasswordTests(RecoveryBase):
    def test_link_is_hidden_only_when_no_recovery_method_is_available(self):
        self.assertIn("Quên mật khẩu?", self.client.get("/login").get_data(as_text=True))
        A.app.config.update(SMTP_HOST="")                                           # hết email nhưng còn đặt lại bằng mã OTP
        self.assertIn("Quên mật khẩu?", self.client.get("/login").get_data(as_text=True))
        A.app.config.update(PASSWORD_RESET_VIA_OTP=False)                           # hết cả hai
        self.assertNotIn("Quên mật khẩu?", self.client.get("/login").get_data(as_text=True))
        r = self.client.get("/quen-mat-khau")
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertIn("chưa được bật", self.page())
        A.app.config.update(SMTP_HOST="smtp.test", ADMIN_BASE_URL="")           # thiếu địa chỉ gốc cũng coi như chưa bật
        self.assertFalse(A._mail_enabled())

    def test_sends_one_email_with_link_on_the_configured_base_url(self):
        r = self.post("/quen-mat-khau", email=EMAIL.upper() + " ")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.mails), 1)
        to, subject, body = self.mails[0]
        self.assertEqual(to, EMAIL)
        self.assertIn("Đặt lại mật khẩu", subject)
        self.assertRegex(body, re.escape(BASE) + r"/dat-lai-mat-khau/\S+")
        self.assertIn("30 phút", body)
        self.assertEqual(len(self.ev("reset_request")), 1)

    def test_host_header_cannot_poison_the_link(self):
        with A.app.test_request_context("/", base_url="https://evil.example"):
            link = A._admin_link("reset_password", token="TOKEN")
        self.assertEqual(link, BASE + "/dat-lai-mat-khau/TOKEN")

    def test_forms_carry_a_csrf_field(self):
        self.assertIn('name="csrf"', self.client.get("/quen-mat-khau").get_data(as_text=True))
        self.assertIn('name="csrf"', self.client.get(f"/dat-lai-mat-khau/{TK.make(self.key, TK.PASSWORD_RESET, EMAIL, 'h1')}").get_data(as_text=True))

    def test_same_answer_whether_or_not_the_email_exists(self):
        texts = []
        for who in (EMAIL, "khong-co@bvtd.vn", GG_EMAIL, "off@bvtd.vn", ""):
            r = self.post("/quen-mat-khau", email=who)
            self.assertEqual(r.status_code, 302, who)
            self.assertTrue(r.headers["Location"].endswith("/quen-mat-khau"), who)
            texts.append(re.search(r"Nếu email này[^<]*", self.client.get("/quen-mat-khau").get_data(as_text=True)).group(0))
        self.assertEqual(len(set(texts)), 1)
        self.assertEqual([m[0] for m in self.mails], [EMAIL])                    # chỉ đúng 1 admin có mật khẩu & đang bật nhận email

    def test_throttled_after_three_requests_per_hour(self):
        for _ in range(5):
            self.post("/quen-mat-khau", email=EMAIL)
        self.assertEqual(len(self.mails), 3)

    def test_csrf_is_required(self):
        self.client.get("/quen-mat-khau")
        self.client.post("/quen-mat-khau", data={"email": EMAIL})
        self.client.post("/quen-mat-khau", data={"email": EMAIL, "csrf": "sai"})
        self.assertEqual(self.mails, [])

    def test_database_down_is_reported_not_500(self):
        self.admins.db_down = True
        r = self.post("/quen-mat-khau", email=EMAIL)
        self.assertEqual(r.status_code, 302)
        self.assertIn("cơ sở dữ liệu", self.client.get("/quen-mat-khau").get_data(as_text=True))
        self.assertEqual(self.mails, [])


class ResetPasswordTests(RecoveryBase):
    def token(self, email=EMAIL, purpose=TK.PASSWORD_RESET, material=None):
        return TK.make(self.key, purpose, email, material if material is not None else self.admins.rows[email]["password_hash"])

    def reset(self, token, pw="mat-khau-moi-1", pw2=None, **kw):
        return self.post(f"/dat-lai-mat-khau/{token}", password=pw, password2=pw if pw2 is None else pw2, **kw)

    def test_full_reset_then_login_with_new_password(self):
        self.post("/quen-mat-khau", email=EMAIL)
        path = self.link()
        page = self.client.get(path).get_data(as_text=True)
        self.assertIn(EMAIL, page)
        self.assertIn('name="password2"', page)
        r = self.post(path, password="mat-khau-moi-1", password2="mat-khau-moi-1")
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertIn("Đã đặt mật khẩu mới", self.page())
        self.assertEqual(self.admins.password, "mat-khau-moi-1")
        self.assertEqual(len(self.ev("password_reset")), 1)
        # đăng nhập bằng mật khẩu cũ thất bại, mật khẩu mới qua bước 1
        self.assertIn("không đúng", self.client.post("/login", data={"email": EMAIL, "password": GOOD_PASSWORD}).get_data(as_text=True))
        r = self.client.post("/login", data={"email": EMAIL, "password": "mat-khau-moi-1"})
        self.assertTrue(r.headers["Location"].endswith("/login/2fa"))

    def test_a_link_works_only_once(self):
        t = self.token()
        self.assertTrue(self.reset(t).headers["Location"].endswith("/login"))
        self.assertEqual(self.admins.password, "mat-khau-moi-1")
        r = self.reset(t, pw="ke-gian-1234")
        self.assertTrue(r.headers["Location"].endswith("/quen-mat-khau"))
        self.assertEqual(self.admins.password, "mat-khau-moi-1")
        self.assertIn("không hợp lệ", self.client.get("/quen-mat-khau").get_data(as_text=True))

    def test_bad_inputs_do_not_change_the_password(self):
        t = self.token()
        self.assertIn("chưa giống nhau", self.reset(t, pw2="khac-nhau-123").get_data(as_text=True))
        self.assertIn("ít nhất 8", self.reset(t, pw="ngan").get_data(as_text=True))
        self.assertEqual(self.admins.password, GOOD_PASSWORD)

    def test_invalid_expired_and_wrong_purpose_tokens(self):
        for bad in ("rac", "a.b.c", self.token(purpose=TK.TWOFA_RESET), self.token(email="khong-co@bvtd.vn", material="x"),
                    self.token(material="mat-khau-cu-khac")):
            self.assertTrue(self.client.get(f"/dat-lai-mat-khau/{bad}").headers["Location"].endswith("/quen-mat-khau"), bad)
            r = self.reset(bad)
            self.assertTrue(r.headers["Location"].endswith("/quen-mat-khau"), bad)
        self.assertEqual(self.admins.password, GOOD_PASSWORD)
        good = self.token()
        self.assertEqual(self.client.get(f"/dat-lai-mat-khau/{good}").status_code, 200)      # còn hạn thì dùng được
        with mock.patch.object(TK, "DEFAULT_MAX_AGE", -1):                                    # hết hạn
            self.assertTrue(self.client.get(f"/dat-lai-mat-khau/{good}").headers["Location"].endswith("/quen-mat-khau"))
            self.assertTrue(self.reset(good).headers["Location"].endswith("/quen-mat-khau"))
        self.assertEqual(self.admins.password, GOOD_PASSWORD)

    def test_google_only_and_disabled_accounts_cannot_use_a_link(self):
        for email in (GG_EMAIL, "off@bvtd.vn"):
            t = TK.make(self.key, TK.PASSWORD_RESET, email, self.admins.rows[email]["password_hash"])
            self.assertEqual(self.client.get(f"/dat-lai-mat-khau/{t}").status_code, 302, email)

    def test_csrf_is_required(self):
        t = self.token()
        self.client.get(f"/dat-lai-mat-khau/{t}")
        self.client.post(f"/dat-lai-mat-khau/{t}", data={"password": "mat-khau-moi-1", "password2": "mat-khau-moi-1"})
        self.assertEqual(self.admins.password, GOOD_PASSWORD)

    def test_reset_does_not_bypass_two_factor(self):
        secret = self.enroll()
        self.reset(self.token())
        r = self.client.post("/login", data={"email": EMAIL, "password": "mat-khau-moi-1"})
        self.assertTrue(r.headers["Location"].endswith("/login/2fa"))
        self.assertFalse(self.logged_in())
        self.client.post("/login/2fa", data={"code": pyotp.TOTP(secret).now()})
        self.assertTrue(self.logged_in())


class LostPhoneTests(RecoveryBase):
    def start(self, secret=None):
        secret = secret or self.enroll()
        self.client.post("/login", data={"email": EMAIL, "password": GOOD_PASSWORD})
        return secret

    def test_recover_form_only_when_mail_is_on(self):
        self.start()
        self.assertIn("Mất / đổi điện thoại", self.client.get("/login/2fa").get_data(as_text=True))
        A.app.config.update(SMTP_HOST="")
        page = self.client.get("/login/2fa").get_data(as_text=True)
        self.assertNotIn("Gửi liên kết vào email", page)
        self.assertIn("Liên hệ quản trị viên", page)
        r = self.post("/login/2fa/khoi-phuc")
        self.assertEqual(self.mails, [])

    def test_send_requires_first_step_csrf_and_is_throttled(self):
        self.enroll()
        r = self.client.post("/login/2fa/khoi-phuc", data={"csrf": "x"})            # chưa qua bước 1
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertEqual(self.mails, [])
        self.start()
        self.client.post("/login/2fa/khoi-phuc", data={"csrf": "sai"})
        self.assertEqual(self.mails, [])
        for _ in range(5):
            self.post("/login/2fa/khoi-phuc")
        self.assertEqual(len(self.mails), 3)
        to, subject, body = self.mails[0]
        self.assertEqual(to, EMAIL)
        self.assertIn("xác thực 2 bước", subject)
        self.assertIn(BASE + "/login/2fa/khoi-phuc/", body)
        self.assertIn("ad***@bvtd.vn".replace("ad***", EMAIL[:2] + "*" * (len(EMAIL.split("@")[0]) - 2)), self.client.get("/login/2fa").get_data(as_text=True))

    def test_same_browser_flow_replaces_the_phone(self):
        old = self.start()
        self.post("/login/2fa/khoi-phuc")
        path = self.link()
        page = self.client.get(path).get_data(as_text=True)
        self.assertIn("điện thoại cũ sẽ ngừng dùng được", page.lower().replace("điện thoại cũ", "điện thoại cũ"))
        self.assertNotIn('name="password"', page)                                    # đã qua bước 1 ở trình duyệt này → không hỏi lại
        r = self.post(path)
        self.assertTrue(r.headers["Location"].endswith("/login/2fa/setup"))
        self.assertFalse(self.logged_in())
        self.client.get("/login/2fa/setup")
        with self.client.session_transaction() as s:
            new = TF.decrypt(s["otp_setup"], self.key)
        self.assertNotEqual(new, old)
        self.client.post("/login/2fa/setup", data={"code": pyotp.TOTP(new).now()})
        self.assertTrue(self.logged_in())
        self.assertEqual(self.secret_of(), new)
        self.assertEqual([kw["detail"] for kw in self.ev("otp_setup")], ["Đổi điện thoại (qua liên kết email)"])
        # điện thoại cũ hết tác dụng, điện thoại mới dùng được
        self.client.get("/logout")
        self.client.post("/login", data={"email": EMAIL, "password": GOOD_PASSWORD})
        self.client.post("/login/2fa", data={"code": pyotp.TOTP(old).at(time.time() + 30)})
        self.assertFalse(self.logged_in())
        self.client.post("/login/2fa", data={"code": pyotp.TOTP(new).at(time.time() + 30)})
        self.assertTrue(self.logged_in())
        # liên kết đã dùng: vô hiệu
        self.client.get("/logout")
        self.assertTrue(self.client.get(path).headers["Location"].endswith("/login"))

    def test_other_browser_needs_the_password(self):
        old = self.start()
        self.post("/login/2fa/khoi-phuc")
        path = self.link()
        other = A.app.test_client()
        page = other.get(path).get_data(as_text=True)
        self.assertIn('name="password"', page)
        r = self.post(path, client=other, password="sai-mat-khau")
        self.assertTrue(r.headers["Location"].endswith(path))
        self.assertFalse(other.get("/login/2fa/setup").headers["Location"].endswith("/setup"))        # chưa có quyền thiết lập
        self.assertEqual(self.secret_of(), old)
        r = self.post(path, client=other, password=GOOD_PASSWORD)
        self.assertTrue(r.headers["Location"].endswith("/login/2fa/setup"))
        other.get("/login/2fa/setup")
        with other.session_transaction() as s:
            new = TF.decrypt(s["otp_setup"], self.key)
        other.post("/login/2fa/setup", data={"code": pyotp.TOTP(new).now()})
        self.assertTrue(self.logged_in(other))
        self.assertEqual(self.secret_of(), new)

    def test_google_only_account_cannot_skip_the_google_step(self):
        self.enroll(GG_EMAIL)
        self.client.get("/login/google/callback", query_string={"code": "c", "state": "s"})        # đang ở bước OTP (đã qua Google)
        self.post("/login/2fa/khoi-phuc")
        path = self.link()
        other = A.app.test_client()                                                  # mở email ở máy khác: chưa qua Google
        self.assertIn("đăng nhập bằng Google", other.get(path).get_data(as_text=True))
        r = self.post(path, client=other)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertFalse(other.get("/login/2fa/setup").headers["Location"].endswith("/setup"))
        r = self.post(path)                                                          # cùng trình duyệt đã qua Google → được
        self.assertTrue(r.headers["Location"].endswith("/login/2fa/setup"))

    def test_link_dies_when_the_secret_changes_and_never_works_without_a_secret(self):
        self.start()
        self.post("/login/2fa/khoi-phuc")
        path = self.link()
        self.enroll()                                                                # mã bí mật đổi (đã thiết lập lại ở nơi khác)
        self.assertTrue(self.client.get(path).headers["Location"].endswith("/login"))
        t = TK.make(self.key, TK.TWOFA_RESET, GG_EMAIL, "")                          # tài khoản chưa thiết lập: không có gì để khôi phục
        self.assertTrue(self.client.get(f"/login/2fa/khoi-phuc/{t}").headers["Location"].endswith("/login"))
        self.assertTrue(self.client.get(f"/login/2fa/khoi-phuc/{TK.make(self.key, TK.PASSWORD_RESET, EMAIL, 'x')}").headers["Location"].endswith("/login"))

    def test_setup_flag_must_match_the_current_secret(self):
        old = self.start()
        with self.client.session_transaction() as s:
            s["otp_reenroll"] = TK.fingerprint("mot-ma-bi-mat-khac")
        r = self.client.get("/login/2fa/setup")
        self.assertTrue(r.headers["Location"].endswith("/login/2fa"))                # cờ giả/cũ không mở được thiết lập lại
        self.assertEqual(self.secret_of(), old)

    def test_csrf_is_required_to_confirm(self):
        self.start()
        self.post("/login/2fa/khoi-phuc")
        path = self.link()
        self.client.get(path)
        r = self.client.post(path, data={})
        self.assertTrue(r.headers["Location"].endswith(path))
        self.assertFalse(self.client.get("/login/2fa/setup").headers["Location"].endswith("/setup"))


class ChangePhoneTests(RecoveryBase):
    def setUp(self):
        super().setUp()
        self.old = self.enroll()
        self.full_login(self.old)

    def next_code(self, secret, ahead=1):
        return pyotp.TOTP(secret).at(time.time() + 30 * ahead)                      # mã ở bước kế tiếp (mã bước hiện tại vừa dùng để đăng nhập)

    def step1(self, code=None):
        return self.post("/tai-khoan/doi-dien-thoai", action="verify_old", code=self.next_code(self.old) if code is None else code)

    def new_secret(self):
        with self.client.session_transaction() as s:
            return TF.decrypt(s["phone_change"]["enc"], self.key)

    def test_pages_need_login(self):
        c = A.app.test_client()
        for path in ("/tai-khoan", "/tai-khoan/doi-dien-thoai"):
            self.assertEqual(c.get(path).status_code, 302, path)
        self.assertEqual(c.post("/tai-khoan/doi-dien-thoai", data={"action": "verify_old", "code": "123456"}).status_code, 302)

    def test_account_page_shows_status_and_menu_link(self):
        page = self.client.get("/tai-khoan").get_data(as_text=True)
        self.assertIn("Đã thiết lập", page)
        self.assertIn("Đổi điện thoại", page)
        self.assertIn(self.admins.rows[EMAIL]["email"], page)
        self.assertIn('href="/tai-khoan"', page)                                     # menu người dùng có lối vào

    def test_full_change_replaces_the_secret(self):
        self.assertIn("Bước 1/2", self.client.get("/tai-khoan/doi-dien-thoai").get_data(as_text=True))
        r = self.step1()
        self.assertTrue(r.headers["Location"].endswith("/tai-khoan/doi-dien-thoai"))
        page = self.client.get("/tai-khoan/doi-dien-thoai").get_data(as_text=True)
        new = self.new_secret()
        self.assertIn("Bước 2/2", page)
        self.assertIn("<svg", page)
        self.assertIn(TF.format_secret(new), page)
        self.assertEqual(self.secret_of(), self.old)                                 # chưa xác nhận mã mới → chưa đổi gì
        r = self.post("/tai-khoan/doi-dien-thoai", action="confirm_new", code=pyotp.TOTP(new).now())
        self.assertTrue(r.headers["Location"].endswith("/tai-khoan"))
        self.assertEqual(self.secret_of(), new)
        self.assertEqual([kw["detail"] for kw in self.ev("otp_setup")], ["Đổi điện thoại (đang đăng nhập)"])
        self.assertIn("Đã đổi sang điện thoại mới", self.client.get("/tai-khoan").get_data(as_text=True))
        self.client.get("/logout")
        self.client.post("/login", data={"email": EMAIL, "password": GOOD_PASSWORD})
        self.client.post("/login/2fa", data={"code": self.next_code(self.old)})
        self.assertFalse(self.logged_in())                                           # điện thoại cũ hết tác dụng
        self.client.post("/login/2fa", data={"code": self.next_code(new)})
        self.assertTrue(self.logged_in())

    def test_wrong_current_code_does_not_start_the_change(self):
        for bad in ("000000", "abc", ""):
            self.step1(bad)
        with self.client.session_transaction() as s:
            self.assertNotIn("phone_change", s)
        self.assertEqual(self.secret_of(), self.old)

    def test_current_code_cannot_be_replayed(self):
        used = pyotp.TOTP(self.old).now()                                            # mã vừa dùng để đăng nhập
        self.step1(used)
        with self.client.session_transaction() as s:
            self.assertNotIn("phone_change", s)

    def test_wrong_new_code_changes_nothing(self):
        self.step1()
        new = self.new_secret()
        for bad in ("000000" if pyotp.TOTP(new).now() != "000000" else "111111", "zzz"):
            self.post("/tai-khoan/doi-dien-thoai", action="confirm_new", code=bad)
        self.assertEqual(self.secret_of(), self.old)

    def test_cannot_skip_step_one(self):
        self.post("/tai-khoan/doi-dien-thoai", action="confirm_new", code="123456")
        self.assertEqual(self.secret_of(), self.old)
        with self.client.session_transaction() as s:
            self.assertNotIn("phone_change", s)

    def test_pending_change_expires(self):
        self.step1()
        with self.client.session_transaction() as s:
            st = dict(s["phone_change"]); st["t"] -= A.PHONE_CHANGE_SECONDS + 5; s["phone_change"] = st
            new = TF.decrypt(st["enc"], self.key)
        self.post("/tai-khoan/doi-dien-thoai", action="confirm_new", code=pyotp.TOTP(new).now())
        self.assertEqual(self.secret_of(), self.old)
        self.assertIn("Bước 1/2", self.client.get("/tai-khoan/doi-dien-thoai").get_data(as_text=True))

    def test_cancel_clears_the_state(self):
        self.step1()
        self.post("/tai-khoan/doi-dien-thoai", action="cancel")
        with self.client.session_transaction() as s:
            self.assertNotIn("phone_change", s)

    def test_csrf_is_required(self):
        self.client.get("/tai-khoan/doi-dien-thoai")
        self.client.post("/tai-khoan/doi-dien-thoai", data={"action": "verify_old", "code": self.next_code(self.old)})
        with self.client.session_transaction() as s:
            self.assertNotIn("phone_change", s)

    def test_concurrent_change_elsewhere_is_not_overwritten(self):
        self.step1()
        new = self.new_secret()
        other = TF.encrypt(TF.new_secret(), self.key)
        real_set = self.admins.set_totp

        def racing_set(email, enc, step, expect_old=""):                              # người khác đổi xong NGAY TRƯỚC lệnh ghi của mình
            self.admins.rows[EMAIL]["totp_secret"] = other
            return real_set(email, enc, step, expect_old)
        self.admins.set_totp = racing_set
        self.post("/tai-khoan/doi-dien-thoai", action="confirm_new", code=pyotp.TOTP(new).now())
        self.assertEqual(self.admins.rows[EMAIL]["totp_secret"], other)

    def test_lockout_after_too_many_wrong_codes(self):
        for _ in range(10):
            A._otp_limiter.fail(EMAIL)
        r = self.step1()
        self.assertTrue(r.headers["Location"].endswith("/tai-khoan"))
        with self.client.session_transaction() as s:
            self.assertNotIn("phone_change", s)

    def test_two_factor_off_redirects(self):
        A.app.config.update(TWO_FACTOR_ENABLED=False)
        self.assertTrue(self.client.get("/tai-khoan/doi-dien-thoai").headers["Location"].endswith("/tai-khoan"))
        self.assertIn("đang <b>tắt</b>", self.client.get("/tai-khoan").get_data(as_text=True).replace("Đang", "đang"))


if __name__ == "__main__":
    unittest.main()
