"""Lấy lại mật khẩu bằng Gmail: chỉ cần GMAIL_USER + GMAIL_APP_PASSWORD (+ ADMIN_BASE_URL); lỗi Gmail được giải thích bằng tiếng Việt;
trang Tài khoản cho thấy đã bật chưa và có nút gửi email thử."""
import importlib
import os
import re
import smtplib
import unittest
from unittest import mock

import config
from services import mailer as MAIL
from tests.helpers import A
from tests.test_account_recovery import BASE, EMAIL, RecoveryBase
from tests.test_google_login import _real_flask_login

SMTP_VARS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_SECURITY", "GMAIL_USER", "GMAIL_APP_PASSWORD")


def reload_config(env):
    clean = {k: v for k, v in os.environ.items() if k not in SMTP_VARS}
    with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
        return importlib.reload(config).Config


class GmailConfigTests(unittest.TestCase):
    def tearDown(self):
        reload_config({})                                                           # trả module config về trạng thái ban đầu

    def test_three_variables_are_enough(self):
        c = reload_config({"GMAIL_USER": "lcongnam30@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"})
        self.assertEqual((c.SMTP_HOST, c.SMTP_PORT, c.SMTP_SECURITY), ("smtp.gmail.com", 587, "starttls"))
        self.assertEqual(c.SMTP_USER, "lcongnam30@gmail.com")
        self.assertEqual(c.SMTP_PASSWORD, "abcdefghijklmnop")                       # bỏ dấu cách Google hiển thị
        self.assertIn("<lcongnam30@gmail.com>", c.SMTP_FROM)                        # người gửi = đúng tài khoản Gmail (Gmail chỉ cho phép vậy)
        self.assertTrue(MAIL.configured(vars(c)))

    def test_nothing_configured_means_off(self):
        c = reload_config({})
        self.assertEqual((c.SMTP_HOST, c.SMTP_USER, c.SMTP_FROM, c.GMAIL_USER), ("", "", "", ""))
        self.assertFalse(MAIL.configured(vars(c)))

    def test_explicit_smtp_settings_win_over_the_gmail_shortcut(self):
        c = reload_config({"GMAIL_USER": "a@gmail.com", "GMAIL_APP_PASSWORD": "x", "SMTP_HOST": "smtp.benhvien.vn", "SMTP_PORT": "465",
                           "SMTP_SECURITY": "ssl", "SMTP_USER": "no-reply@benhvien.vn", "SMTP_PASSWORD": "mk", "SMTP_FROM": "BV <no-reply@benhvien.vn>"})
        self.assertEqual((c.SMTP_HOST, c.SMTP_PORT, c.SMTP_SECURITY, c.SMTP_USER, c.SMTP_PASSWORD), ("smtp.benhvien.vn", 465, "ssl", "no-reply@benhvien.vn", "mk"))
        self.assertEqual(c.SMTP_FROM, "BV <no-reply@benhvien.vn>")


class GmailErrorHintTests(unittest.TestCase):
    CFG = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": 587, "SMTP_USER": "a@gmail.com", "SMTP_PASSWORD": "pw", "SMTP_FROM": "a@gmail.com"}

    def test_wrong_password_explains_app_passwords(self):
        with mock.patch("smtplib.SMTP") as smtp:
            smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"BadCredentials")
            with self.assertRaises(MAIL.MailError) as cm:
                MAIL.send(self.CFG, "x@y.vn", "s", "b")
        msg = str(cm.exception)
        self.assertIn("Mật khẩu ứng dụng", msg)
        self.assertIn("apppasswords", msg)
        self.assertNotIn("pw", msg.replace("apppasswords", ""))                      # không lộ mật khẩu

    def test_blocked_port_suggests_ssl_465(self):
        with mock.patch("smtplib.SMTP", side_effect=OSError("timed out")):
            with self.assertRaises(MAIL.MailError) as cm:
                MAIL.send(self.CFG, "x@y.vn", "s", "b")
        self.assertIn("465", str(cm.exception))

    def test_other_providers_get_the_generic_message(self):
        with mock.patch("smtplib.SMTP") as smtp:
            smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"x")
            with self.assertRaises(MAIL.MailError) as cm:
                MAIL.send({**self.CFG, "SMTP_HOST": "smtp.benhvien.vn"}, "x@y.vn", "s", "b")
        self.assertNotIn("Gmail", str(cm.exception))

    def test_real_message_is_built_with_gmail_sender_and_utf8_subject(self):
        with mock.patch("smtplib.SMTP") as smtp:
            MAIL.send({**self.CFG, "SMTP_FROM": "BVĐK Tâm Đức <a@gmail.com>"}, "x@y.vn", "Đặt lại mật khẩu", "Xin chào\n")
        msg = smtp.return_value.send_message.call_args[0][0]
        self.assertIn("a@gmail.com", msg["From"])
        self.assertEqual(str(msg["Subject"]), "Đặt lại mật khẩu")


@unittest.skipUnless(_real_flask_login(), "flask_login là bản giả trong môi trường test")
class AccountMailCardTests(RecoveryBase):
    def setUp(self):
        super().setUp()
        self.secret = self.enroll()
        self.full_login(self.secret)
        self.sent = []
        p = mock.patch.object(A.MAIL, "send", lambda cfg, to, subject, body: self.sent.append((to, subject, body)))
        p.start()
        self.addCleanup(p.stop)

    def card(self):
        return re.sub(r"\s+", " ", self.client.get("/tai-khoan").get_data(as_text=True))

    def test_status_when_on(self):
        page = self.card()
        self.assertIn("Đã bật", page)
        self.assertIn("no-reply@bvtd.vn", page)                                      # hiện địa chỉ gửi đi
        self.assertIn("Gửi email thử tới " + EMAIL, page)

    def test_status_when_off_lists_exactly_what_is_missing_and_suggests_the_base_url(self):
        A.app.config.update(SMTP_HOST="", ADMIN_BASE_URL="")
        page = self.card()
        self.assertIn("Chưa bật", page)
        self.assertIn("GMAIL_USER", page)
        self.assertIn("ADMIN_BASE_URL", page)
        self.assertIn("http://localhost", page)                                      # gợi ý đúng địa chỉ trang đang mở để dán vào
        A.app.config.update(SMTP_HOST="smtp.test", ADMIN_BASE_URL=BASE)
        self.assertNotIn("GMAIL_USER", self.card())

    def test_only_base_url_missing(self):
        A.app.config.update(ADMIN_BASE_URL="")
        page = self.card()
        self.assertIn("Chưa bật", page)
        self.assertNotIn("<code>GMAIL_USER</code>", page)
        self.assertIn("ADMIN_BASE_URL", page)

    def test_test_button_sends_to_my_own_address(self):
        r = self.post("/tai-khoan/thu-email")
        self.assertTrue(r.headers["Location"].endswith("/tai-khoan"))
        self.assertEqual([m[0] for m in self.sent], [EMAIL])
        self.assertIn("Email thử", self.sent[0][1])
        self.assertIn("Đã gửi email thử", self.card())

    def test_test_button_reports_the_real_error(self):
        def boom(cfg, to, subject, body):
            raise MAIL.MailError("Gmail từ chối đăng nhập. Hãy dùng “Mật khẩu ứng dụng”")
        with mock.patch.object(A.MAIL, "send", boom):
            self.post("/tai-khoan/thu-email")
        self.assertIn("Mật khẩu ứng dụng", self.card())

    def test_test_button_guards(self):
        self.client.post("/tai-khoan/thu-email", data={"csrf": "sai"})                # thiếu/sai CSRF
        A.app.config.update(SMTP_HOST="")
        self.post("/tai-khoan/thu-email")                                              # chưa cấu hình
        A.app.config.update(SMTP_HOST="smtp.test")
        self.assertEqual(self.sent, [])
        c = A.app.test_client()                                                        # chưa đăng nhập
        self.assertEqual(c.post("/tai-khoan/thu-email", data={}).status_code, 302)
        for _ in range(3):
            A._reset_email_limiter.fail(f"mailtest:{EMAIL}")                           # đã gửi thử 3 lần trong giờ
        self.post("/tai-khoan/thu-email")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
