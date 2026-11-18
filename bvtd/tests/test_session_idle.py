"""Phiên đăng nhập admin: 15 phút không thao tác thì tự đăng xuất (cần flask_login thật để kiểm tra)."""
import time
import unittest

from tests.helpers import A, AppTestCase, requires_db


def _real_flask_login():
    try:
        import flask_login
        return flask_login if hasattr(flask_login, "__file__") else None
    except ImportError:
        return None


@requires_db
@unittest.skipUnless(_real_flask_login(), "flask_login là bản giả trong môi trường test")
class SessionIdleTests(AppTestCase):
    EMAIL, PW = "admin@bvtd.vn", "mat-khau-test"
    JSON = {"X-Requested-With": "fetch", "Accept": "application/json"}

    def setUp(self):
        super().setUp()
        A.current_user = _real_flask_login().current_user                    # dùng current_user thật
        old = {k: A.app.config.get(k) for k in ("LOGIN_DISABLED", "ALLOWED_EMAILS", "ADMIN_PASSWORD", "SESSION_IDLE_MINUTES")}
        A.app.config.update(LOGIN_DISABLED=False, ALLOWED_EMAILS={self.EMAIL}, ADMIN_PASSWORD=self.PW, SESSION_IDLE_MINUTES=15)
        self.addCleanup(lambda: A.app.config.update(old))

    def login(self):
        return self.client.post("/login", data={"email": self.EMAIL, "password": self.PW})

    def age(self, seconds):
        """Giả lập: lần thao tác cuối cách đây `seconds` giây."""
        with self.client.session_transaction() as s:
            s["last_active"] = time.time() - seconds

    def last_active(self):
        with self.client.session_transaction() as s:
            return s.get("last_active")

    def test_login_ok_and_no_remember_cookie(self):
        r = self.login()
        self.assertEqual(r.status_code, 302)
        self.assertFalse(any("remember_token" in c for c in r.headers.getlist("Set-Cookie")), "không dùng cookie ghi nhớ đăng nhập")
        self.assertEqual(self.client.get("/benh-nhan").status_code, 200)

    def test_wrong_password_is_rejected(self):
        r = self.client.post("/login", data={"email": self.EMAIL, "password": "sai"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/benh-nhan").status_code, 302)

    def test_idle_over_15_minutes_logs_out(self):
        self.login()
        self.age(14 * 60)
        self.assertEqual(self.client.get("/benh-nhan").status_code, 200, "14 phút vẫn còn phiên")
        self.age(16 * 60)
        r = self.client.get("/benh-nhan")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"]); self.assertIn("expired=1", r.headers["Location"])
        again = self.client.get("/benh-nhan")                               # đã bị đăng xuất thật, không vào lại được
        self.assertEqual(again.status_code, 302); self.assertIn("/login", again.headers["Location"])
        page = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("15 phút không thao tác", page)

    def test_activity_extends_the_session(self):
        self.login()
        self.age(10 * 60)
        self.assertEqual(self.client.get("/benh-nhan").status_code, 200)
        self.assertGreater(self.last_active(), time.time() - 30, "mỗi lần thao tác làm mới mốc 15 phút")
        self.age(10 * 60)
        self.assertEqual(self.client.get("/benh-nhan").status_code, 200, "10 + 10 phút nhưng có thao tác ở giữa → còn phiên")

    def test_session_status_does_not_extend(self):
        self.login()
        self.age(10 * 60)
        before = self.last_active()
        d = self.client.get("/api/session-status", headers=self.JSON).get_json()
        self.assertTrue(d["active"]); self.assertTrue(280 <= d["remaining"] <= 305, d)
        self.assertEqual(self.last_active(), before, "chỉ hỏi trạng thái thì không gia hạn")

    def test_keepalive_extends(self):
        self.login()
        self.age(10 * 60)
        r = self.client.post("/api/keepalive", headers=self.JSON)
        self.assertEqual(r.status_code, 200)
        self.assertGreater(self.last_active(), time.time() - 30)

    def test_expired_fetch_gets_json_401(self):
        self.login()
        self.age(16 * 60)
        r = self.client.post("/api/keepalive", headers=self.JSON)
        self.assertEqual(r.status_code, 401)
        d = r.get_json()
        self.assertFalse(d["ok"]); self.assertIn("hết hạn", d["message"]); self.assertIn("/login", d["redirect"])

    def test_session_without_last_active_is_treated_as_expired(self):
        """Ví dụ cookie 'ghi nhớ đăng nhập' cũ: có người dùng nhưng không có mốc thao tác → phải đăng nhập lại."""
        self.login()
        with self.client.session_transaction() as s:
            s.pop("last_active", None)
        r = self.client.get("/benh-nhan")
        self.assertEqual(r.status_code, 302); self.assertIn("/login", r.headers["Location"])

    def test_idle_minutes_is_configurable(self):
        A.app.config["SESSION_IDLE_MINUTES"] = 5
        self.login()
        self.age(6 * 60)
        self.assertEqual(self.client.get("/benh-nhan").status_code, 302)

    def test_public_registration_page_is_not_affected(self):
        self.assertEqual(self.client.get("/dang-ky").status_code, 200)
        self.assertEqual(self.client.post("/api/keepalive", headers=self.JSON).status_code, 401)


if __name__ == "__main__":
    unittest.main()
