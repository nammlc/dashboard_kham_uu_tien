"""Đăng nhập admin bằng Google: chỉ email ĐÃ XÁC MINH và đang bật trong danh sách admin mới vào được;
mọi tình huống lỗi (huỷ, link cũ, mạng, DB, chưa cấu hình) phải báo tiếng Việt rõ ràng thay vì lỗi 500."""
import unittest
from unittest import mock

from flask import redirect

from tests.helpers import A, AppTestCase


def _real_flask_login():
    try:
        import flask_login
        return flask_login if hasattr(flask_login, "__file__") else None
    except ImportError:
        return None


class FakeAdmins:
    """Thay bảng admin_users: {email: dòng}. Chỉ cần các hàm callback Google dùng."""
    def __init__(self, rows):
        self.rows = {r["email"]: dict(r, picture=r.get("picture", "")) for r in rows}
        self.touched = []
        self.boom = None

    def get_active(self, email):
        if self.boom:
            raise self.boom
        r = self.rows.get(email)
        return r if r and r.get("active", True) else None

    def touch_login(self, email, name="", picture=""):
        self.touched.append((email, name, picture))
        if name and email in self.rows:
            self.rows[email]["name"] = name


class FakeGoogle:
    def __init__(self, info=None, boom=None):
        self.info, self.boom = info, boom
        self.redirect_uri = self.kw = None

    def authorize_redirect(self, uri, **kw):
        self.redirect_uri, self.kw = uri, kw
        return redirect("https://accounts.google.com/o/oauth2/v2/auth?client_id=x")

    def authorize_access_token(self):
        if self.boom:
            raise self.boom
        return {"userinfo": self.info}

    def userinfo(self):
        return self.info


@unittest.skipUnless(_real_flask_login(), "flask_login là bản giả trong môi trường test")
class GoogleLoginTests(AppTestCase):
    ADMIN = "admin@bvtd.vn"

    def setUp(self):
        super().setUp()
        A.current_user = _real_flask_login().current_user                    # dùng current_user thật
        old = {k: A.app.config.get(k) for k in ("LOGIN_DISABLED", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI")}
        A.app.config.update(LOGIN_DISABLED=False, GOOGLE_CLIENT_ID="id.apps.googleusercontent.com", GOOGLE_CLIENT_SECRET="sec",
                            GOOGLE_REDIRECT_URI="")
        self.addCleanup(lambda: A.app.config.update(old))
        self.admins = FakeAdmins([{"email": self.ADMIN, "name": "Quản trị"},
                                  {"email": "tat@bvtd.vn", "name": "Đã tắt", "active": False}])
        self.google = FakeGoogle(info={"email": self.ADMIN, "email_verified": True, "name": "Nguyễn Admin", "picture": "http://p"})
        for p in (mock.patch.object(A, "_admins", lambda: self.admins), mock.patch.object(A, "oauth", mock.Mock(google=self.google))):
            p.start()
            self.addCleanup(p.stop)

    def callback(self, **q):
        return self.client.get("/login/google/callback", query_string=q)

    def logged_in(self):
        return self.client.get("/api/session-status").status_code == 200

    def flashed(self, r):
        """Theo chuyển hướng về /login rồi lấy chữ thông báo."""
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login"), r.headers["Location"])
        return self.client.get("/login").get_data(as_text=True)

    # ── trang đăng nhập / bước 1 ──
    def test_button_only_shown_when_google_is_configured(self):
        self.assertIn("Đăng nhập bằng Google", self.client.get("/login").get_data(as_text=True))
        A.app.config.update(GOOGLE_CLIENT_SECRET="")
        self.assertNotIn("Đăng nhập bằng Google", self.client.get("/login").get_data(as_text=True))

    def test_not_configured_gives_a_clear_message_not_a_500(self):
        A.app.config.update(GOOGLE_CLIENT_ID="")
        r = self.client.get("/login/google")
        self.assertIn("chưa được bật", self.flashed(r))
        self.assertIn("chưa được bật", self.flashed(self.callback(code="x", state="y")))     # callback cũng không nổ

    def test_start_redirects_to_google_with_account_chooser_and_callback_url(self):
        r = self.client.get("/login/google")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("https://accounts.google.com/"))
        self.assertTrue(self.google.redirect_uri.endswith("/login/google/callback"), self.google.redirect_uri)
        self.assertEqual(self.google.kw.get("prompt"), "select_account")

    def test_redirect_uri_can_be_pinned_by_config(self):
        A.app.config.update(GOOGLE_REDIRECT_URI="https://quanly.bv.vn/login/google/callback")
        self.client.get("/login/google")
        self.assertEqual(self.google.redirect_uri, "https://quanly.bv.vn/login/google/callback")

    # ── bước 2: đăng nhập thành công / bị từ chối ──
    def test_listed_admin_gets_in_and_profile_is_saved(self):
        r = self.callback(code="c", state="s")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(r.headers["Location"].endswith("/login"))
        self.assertTrue(self.logged_in())
        self.assertEqual(self.admins.touched, [(self.ADMIN, "Nguyễn Admin", "http://p")])
        with self.client.session_transaction() as s:
            self.assertIn("last_active", s)                                   # có mốc để tính 15 phút không thao tác

    def test_email_is_case_insensitive(self):
        self.google.info["email"] = "ADMIN@BVTD.VN"
        self.callback(code="c", state="s")
        self.assertTrue(self.logged_in())

    def test_unlisted_google_account_is_rejected(self):
        self.google.info["email"] = "la@gmail.com"
        self.assertIn("la@gmail.com không được phép", self.flashed(self.callback(code="c", state="s")))
        self.assertFalse(self.logged_in())

    def test_disabled_admin_is_rejected(self):
        self.google.info["email"] = "tat@bvtd.vn"
        self.flashed(self.callback(code="c", state="s"))
        self.assertFalse(self.logged_in())

    def test_unverified_email_is_rejected_even_if_listed(self):
        for verified in (False, None, "false"):
            self.google.info["email_verified"] = verified
            self.assertIn("chưa được xác minh", self.flashed(self.callback(code="c", state="s")))
            self.assertFalse(self.logged_in(), verified)
        self.google.info["email_verified"] = "true"                           # userinfo đôi khi trả chuỗi
        self.callback(code="c", state="s")
        self.assertTrue(self.logged_in())

    def test_missing_email_is_rejected(self):
        self.google.info = {"email_verified": True}
        self.flashed(self.callback(code="c", state="s"))
        self.assertFalse(self.logged_in())

    # ── lỗi ──
    def test_user_pressing_cancel(self):
        self.assertIn("đã huỷ", self.flashed(self.callback(error="access_denied")))
        self.assertFalse(self.logged_in())

    def test_other_google_error(self):
        self.assertIn("Google từ chối", self.flashed(self.callback(error="invalid_request")))

    def test_stale_link_or_network_error_is_reported_not_500(self):
        self.google.boom = RuntimeError("mismatching_state")
        self.assertIn("Chưa hoàn tất", self.flashed(self.callback(code="c", state="cu")))
        self.assertFalse(self.logged_in())

    def test_database_problems_are_reported_not_500(self):
        self.admins.boom = A.DB.DBError("down")
        self.assertIn("cơ sở dữ liệu", self.flashed(self.callback(code="c", state="s")))
        self.admins.boom = A.DB.NotConfigured("no url")
        self.assertIn("DATABASE_URL", self.flashed(self.callback(code="c", state="s")))
        self.assertFalse(self.logged_in())

    # ── quay lại trang đang mở dở ──
    def test_next_page_is_honoured_only_when_internal(self):
        for nxt, expect in (("/bao-cao", "/bao-cao"), ("//evil.com", "/"), ("https://evil.com", "/"), ("/\\evil.com", "/"), ("", "/")):
            self.client.get("/logout")
            self.client.get("/login/google", query_string={"next": nxt})
            r = self.callback(code="c", state="s")
            self.assertEqual(r.headers["Location"].split("://")[-1].split("/", 1)[-1].rstrip("/") if "://" in r.headers["Location"] else r.headers["Location"].lstrip("/").rstrip("/"),
                             expect.lstrip("/").rstrip("/"), (nxt, r.headers["Location"]))

    def test_safe_next_helper(self):
        for bad in ("//x.com", "/\\x.com", "http://x.com", "javascript:1", "x", None, "/a\nb"):
            self.assertEqual(A._safe_next(bad), "", bad)
        self.assertEqual(A._safe_next("/nhat-ky?page=2"), "/nhat-ky?page=2")

    def test_login_page_passes_next_to_google_button(self):
        self.assertIn("/login/google?next=/bao-cao", self.client.get("/login?next=/bao-cao").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
