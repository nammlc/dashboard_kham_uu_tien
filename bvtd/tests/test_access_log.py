"""Cơ sở dữ liệu máy chủ: nhật ký truy cập (access_log) + tài khoản admin (admin_users) + trang /nhat-ky + lệnh quản lý."""
import os
import time
import unittest
from datetime import date, timedelta

from services import access_log as ACL, admin_users as ADM, db as DB
from tests.helpers import A, AppTestCase, TEST_DB, requires_db, reset_test_db


def _real_flask_login():
    try:
        import flask_login
        return flask_login if hasattr(flask_login, "__file__") else None
    except ImportError:
        return None


class PageWindow(unittest.TestCase):
    """Danh sách số trang hiển thị ở thanh phân trang (None = dấu …)."""

    def test_windows(self):
        w = A.page_window
        self.assertEqual(w(1, 0), []); self.assertEqual(w(1, 1), [1])
        self.assertEqual(w(1, 5), [1, 2, 3, 4, 5])
        self.assertEqual(w(1, 20), [1, 2, 3, None, 20])
        self.assertEqual(w(6, 20), [1, None, 4, 5, 6, 7, 8, None, 20])
        self.assertEqual(w(20, 20), [1, None, 18, 19, 20])
        self.assertEqual(w(4, 20), [1, 2, 3, 4, 5, 6, None, 20])        # chỉ thiếu 1 trang thì hiện số, không hiện …


class SchemaFile(unittest.TestCase):
    def test_schema_sql_matches_the_one_used_by_the_app(self):
        """schema.sql (để tạo tay) phải giống đúng SQL mà ứng dụng tự chạy."""
        norm = lambda t: " ".join(l.split("--")[0].strip() for l in t.splitlines() if l.split("--")[0].strip())
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
        self.assertEqual(norm(open(path, encoding="utf-8").read()), norm(DB.SCHEMA_SQL))


@requires_db
class AccessLogUnit(unittest.TestCase):
    def setUp(self):
        reset_test_db()
        self.log = ACL.AccessLog(TEST_DB, retention_days=30)

    def test_add_and_query_newest_first(self):
        self.log.add("login_ok", email="a@x.vn", ip="1.2.3.4", ua="Mozilla Chrome/120 Windows")
        self.log.add("page", email="a@x.vn", method="GET", path="/benh-nhan", status=200)
        rows, total = self.log.query()
        self.assertEqual(total, 2); self.assertEqual(rows[0]["event"], "page"); self.assertEqual(rows[1]["event"], "login_ok")

    def test_filters(self):
        self.log.add("login_fail", email="x@x.vn", ip="9.9.9.9")
        self.log.add("page", email="a@x.vn", path="/bao-cao")
        self.assertEqual(self.log.query(event="login_fail")[1], 1)
        self.assertEqual(self.log.query(q="BAO-CAO")[1], 1)                     # không phân biệt hoa thường
        self.assertEqual(self.log.query(q="9.9.9")[1], 1)
        self.assertEqual(self.log.query(q="100%")[1], 0)                        # ký tự % không bị hiểu là ký tự đại diện
        today = date.today()
        self.assertEqual(self.log.query(d_from=today - timedelta(days=1), d_to=today + timedelta(days=1))[1], 2)
        self.assertEqual(self.log.query(d_from=today + timedelta(days=2))[1], 0)

    def test_pagination(self):
        for i in range(7):
            self.log.add("page", path=f"/p{i}")
        rows, total = self.log.query(page=2, per_page=5)
        self.assertEqual((len(rows), total), (2, 7))

    def test_purge_old_rows(self):
        self.log.add("page", path="/moi")
        with DB.pool(TEST_DB).connection() as c:
            c.execute("INSERT INTO access_log (ts, event, path) VALUES (now() - interval '31 days', 'page', '/cu')")
        self.log.purge()
        rows, total = self.log.query()
        self.assertEqual([r["path"] for r in rows], ["/moi"])

    def test_stats(self):
        self.log.add("login_ok", email="a@x.vn"); self.log.add("login_fail", email="b@x.vn")
        s = self.log.stats()
        self.assertEqual((s["today"], s["fails_24h"], s["users_today"]), (2, 1, 1))

    def test_short_ua(self):
        self.assertEqual(ACL.short_ua("Mozilla/5.0 (Windows NT 10.0) Chrome/120.0 Safari/537.36"), "Chrome · Windows")
        self.assertEqual(ACL.short_ua("Mozilla/5.0 (iPhone; CPU iPhone OS 17) Safari/604.1"), "Safari · iOS")
        self.assertEqual(ACL.short_ua(""), "—")

    def test_time_is_shown_in_vietnam_timezone(self):
        from datetime import datetime, timezone
        self.assertEqual(ACL.fmt_time(datetime(2026, 11, 3, 17, 30, 5, tzinfo=timezone.utc)), "04/11/2026 00:30:05")


@requires_db
class AdminUsersUnit(unittest.TestCase):
    def setUp(self):
        reset_test_db()
        self.users = ADM.AdminUsers(TEST_DB)

    def test_add_verify_and_hash(self):
        self.assertTrue(self.users.add("Admin@BV.vn", "matkhau-123"))
        self.assertFalse(self.users.add("admin@bv.vn", "khac-123456"))           # trùng email (không phân biệt hoa thường)
        self.assertIsNotNone(self.users.verify("ADMIN@bv.vn", "matkhau-123"))
        self.assertIsNone(self.users.verify("admin@bv.vn", "sai-mat-khau"))
        self.assertNotIn("matkhau-123", self.users.get("admin@bv.vn")["password_hash"])   # chỉ lưu bản băm

    def test_password_rules_and_google_only(self):
        with self.assertRaises(ValueError):
            self.users.add("a@bv.vn", "ngan")
        self.users.add("g@bv.vn", "")                                            # chỉ Google
        self.assertIsNone(self.users.verify("g@bv.vn", ""))
        self.assertIsNotNone(self.users.get_active("g@bv.vn"))

    def test_disable_and_change_password(self):
        self.users.add("a@bv.vn", "matkhau-123")
        self.users.set_password("a@bv.vn", "mat-khau-moi-9")
        self.assertIsNone(self.users.verify("a@bv.vn", "matkhau-123"))
        self.assertIsNotNone(self.users.verify("a@bv.vn", "mat-khau-moi-9"))
        self.users.set_active("a@bv.vn", False)
        self.assertIsNone(self.users.verify("a@bv.vn", "mat-khau-moi-9")); self.assertIsNone(self.users.get_active("a@bv.vn"))

    def test_seed_from_env_only_when_empty(self):
        self.assertEqual(self.users.seed_from_env({"A@x.vn", "b@x.vn"}, "abc"), 2)      # mật khẩu cũ ngắn vẫn tạo được
        self.assertIsNotNone(self.users.verify("a@x.vn", "abc"))
        self.assertEqual(self.users.seed_from_env({"c@x.vn"}, "abc"), 0)                # bảng đã có dữ liệu → không tạo thêm


@requires_db
class AccessLogPages(AppTestCase):
    """Hook ghi log + trang /nhat-ky (đăng nhập giả lập sẵn)."""

    def setUp(self):
        super().setUp()
        A.app.config.update(ACCESS_LOG_ENABLED=True)

    def rows(self, **kw):
        return A._acl().query(**kw)[0]

    def dump(self):
        with DB.pool(TEST_DB).connection() as c:
            return repr(c.execute("SELECT * FROM access_log").fetchall())

    def test_admin_page_view_is_logged_without_query_string(self):
        self.client.get("/benh-nhan?q=NGUYEN-VAN-A-0912345678")
        r = self.rows()[0]
        self.assertEqual((r["event"], r["path"], r["email"]), ("page", "/benh-nhan", "t@x.vn"))
        self.assertNotIn("NGUYEN", self.dump()); self.assertNotIn("0912345678", self.dump())   # không lộ từ khoá tìm bệnh nhân

    def test_noise_is_not_logged(self):
        self.client.get("/static/x.css"); self.client.get("/api/session-status"); self.client.post("/api/keepalive")
        self.assertEqual(self.rows(), [])

    def test_patient_register_visit_is_logged_without_account(self):
        A.current_user = type("Anon", (), {"is_authenticated": False})()
        self.assertEqual(self.client.get("/dang-ky").status_code, 200)
        r = self.rows()[0]
        self.assertEqual((r["event"], r["email"]), ("register_view", ""))

    def test_log_page_renders_filters_and_paginates(self):
        for i in range(60):
            A._acl().add("page", email="a@x.vn", method="GET", path=f"/p{i}", status=200, detail="Tổng quan")
        A._acl().add("login_fail", email="ke-xau@x.vn", ip="8.8.8.8", detail="Sai email / mật khẩu")
        h = self.client.get("/nhat-ky").get_data(as_text=True)
        self.assertIn("Nhật ký truy cập", h); self.assertIn("ke-xau@x.vn", h); self.assertIn("Đăng nhập sai", h)
        self.assertIn("61 bản ghi", h)
        h = self.client.get("/nhat-ky?event=login_fail").get_data(as_text=True)
        self.assertIn("ke-xau@x.vn", h); self.assertNotIn("/p5", h)
        h = self.client.get("/nhat-ky?q=8.8.8.8").get_data(as_text=True)
        self.assertIn("1 bản ghi", h)
        self.assertEqual(self.client.get("/nhat-ky?page=2").status_code, 200)

    def fill(self, n):
        for i in range(n):
            A._acl().add("action", email="a@x.vn", method="POST", path=f"/p{i}", status=200)    # event riêng, lọc bằng event=action
                                                                                                # (chính việc xem trang /nhat-ky cũng được ghi thành dòng "page")

    def test_page_size_selector_and_range_text(self):
        self.fill(61)
        h = self.client.get("/nhat-ky?event=action&per_page=20").get_data(as_text=True)
        self.assertIn("<b>1–20</b> / 61", h); self.assertIn('<option value="20" selected>', h)
        h = self.client.get("/nhat-ky?event=action&per_page=100").get_data(as_text=True)
        self.assertIn("<b>1–61</b> / 61", h); self.assertNotIn("Tới trang", h)        # 1 trang → không cần nút chuyển trang
        h = self.client.get("/nhat-ky?event=action&per_page=7").get_data(as_text=True)              # giá trị lạ → về mặc định 20
        self.assertIn("<b>1–20</b> / 61", h)
        h = self.client.get("/nhat-ky?event=action").get_data(as_text=True)                         # không chọn gì → 20 dòng / trang
        self.assertIn("<b>1–20</b> / 61", h); self.assertIn('<option value="20" selected>', h)

    def test_page_beyond_the_end_shows_last_page(self):
        self.fill(61)
        h = self.client.get("/nhat-ky?event=action&per_page=20&page=999").get_data(as_text=True)
        self.assertIn("<b>61–61</b> / 61", h); self.assertIn('aria-current="page"', h)
        self.assertEqual(self.client.get("/nhat-ky?page=abc&per_page=xyz").status_code, 200)

    def test_pager_controls_and_ellipsis(self):
        self.fill(300)
        h = self.client.get("/nhat-ky?event=action&per_page=20&page=8").get_data(as_text=True)      # 15 trang, đang ở trang 8
        self.assertIn("…", h); self.assertIn('title="Trang đầu"', h); self.assertIn('title="Trang cuối"', h)
        self.assertIn("Tới trang", h); self.assertIn('max="15"', h)
        first = self.client.get("/nhat-ky?event=action&per_page=20&page=1").get_data(as_text=True)
        self.assertNotIn('title="Trang đầu"', first)                                   # trang 1: « ‹ bị vô hiệu

    def test_pager_keeps_filters_and_page_size(self):
        self.fill(60)
        A._acl().add("login_fail", email="x@x.vn")
        h = self.client.get("/nhat-ky?event=action&per_page=20&q=a%40x.vn").get_data(as_text=True)
        links = [l for l in h.split('href="') if "page=2" in l.split('"')[0]]
        self.assertTrue(links)
        for l in links:
            u = l.split('"')[0]
            self.assertIn("event=action", u); self.assertIn("per_page=20", u); self.assertIn("q=a@x.vn", u)

    def test_log_page_in_sidebar(self):
        self.assertIn("Nhật Ký Truy Cập", self.client.get("/benh-nhan").get_data(as_text=True))

    def test_logging_failure_never_breaks_the_page(self):
        DB.POOL_TIMEOUT = 1
        self.addCleanup(setattr, DB, "POOL_TIMEOUT", 5)
        A.app.config["DATABASE_URL"] = "postgresql://nobody:x@127.0.0.1:1/none"     # cổng đóng → không kết nối được
        A._log_down_until = 0.0
        self.addCleanup(setattr, A, "_log_down_until", 0.0)
        self.assertEqual(self.client.get("/benh-nhan").status_code, 200)            # log lỗi nhưng trang vẫn hiển thị
        t = time.time(); self.client.get("/benh-nhan")
        self.assertLess(time.time() - t, 0.9, "sau 1 lần lỗi tạm ngừng ghi log, không chờ kết nối mỗi request")


@requires_db
@unittest.skipUnless(_real_flask_login(), "flask_login là bản giả trong môi trường test")
class AccountsAndLoginLog(AppTestCase):
    EMAIL, PW = "bs@bvtd.vn", "mat-khau-9999"

    def setUp(self):
        super().setUp()
        A.current_user = _real_flask_login().current_user
        old = {k: A.app.config.get(k) for k in ("LOGIN_DISABLED", "ALLOWED_EMAILS", "ADMIN_PASSWORD", "ACCESS_LOG_ENABLED")}
        A.app.config.update(LOGIN_DISABLED=False, ALLOWED_EMAILS=set(), ADMIN_PASSWORD="", ACCESS_LOG_ENABLED=True)
        self.addCleanup(lambda: A.app.config.update(old))
        A._admins().add(self.EMAIL, self.PW)

    def events(self):
        return [r["event"] for r in reversed(A._acl().query()[0])]

    def login(self, pw=None, email=None):
        return self.client.post("/login", data={"email": email or self.EMAIL, "password": pw or self.PW})

    def test_login_from_db_logs_in_and_out(self):
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(self.client.get("/benh-nhan").status_code, 200)
        self.client.get("/logout")
        self.assertEqual(self.events(), ["login_ok", "page", "logout"])
        self.assertIsNotNone(A._admins().get(self.EMAIL)["last_login_at"])

    def test_wrong_password_is_logged_without_the_password(self):
        r = self.login(pw="mat-khau-SAI-123")
        self.assertEqual(r.status_code, 200)
        row = A._acl().query()[0][0]
        self.assertEqual((row["event"], row["email"]), ("login_fail", self.EMAIL))
        with DB.pool(TEST_DB).connection() as c:
            dump = repr(c.execute("SELECT * FROM access_log").fetchall())
        self.assertNotIn("mat-khau-SAI-123", dump)

    def test_unknown_email_and_disabled_account_fail(self):
        self.assertEqual(self.login(email="la@bvtd.vn").status_code, 200)
        A._admins().set_active(self.EMAIL, False)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.events().count("login_fail"), 2)

    def test_disabling_account_kicks_out_active_session(self):
        self.login()
        self.assertEqual(self.client.get("/benh-nhan").status_code, 200)
        A._admins().set_active(self.EMAIL, False)
        r = self.client.get("/benh-nhan")
        self.assertEqual(r.status_code, 302); self.assertIn("/login", r.headers["Location"])

    def test_session_expiry_is_logged(self):
        self.login()
        with self.client.session_transaction() as s:
            s["last_active"] = time.time() - 16 * 60
        self.client.get("/benh-nhan")
        self.assertIn("session_expired", self.events())

    def test_open_redirect_is_blocked(self):
        r = self.client.post("/login?next=https://evil.example/x", data={"email": self.EMAIL, "password": self.PW})
        self.assertEqual(r.status_code, 302); self.assertNotIn("evil.example", r.headers["Location"])

    def test_first_run_seeds_accounts_from_env(self):
        reset_test_db()                                                          # bảng admin_users trống
        A.app.config.update(ALLOWED_EMAILS={"seed@bvtd.vn"}, ADMIN_PASSWORD="mk-cu-123")
        self.assertEqual(self.login(pw="mk-cu-123", email="seed@bvtd.vn").status_code, 302)


@requires_db
class AdminCli(AppTestCase):
    def run_cli(self, *args, input=None):
        return A.app.test_cli_runner().invoke(args=list(args), input=input)

    def test_add_list_passwd_disable_enable(self):
        r = self.run_cli("admin-add", "Moi@BV.vn", input="matkhau-123\nmatkhau-123\n")
        self.assertIn("Đã thêm", r.output)
        self.assertIsNotNone(A._admins().verify("moi@bv.vn", "matkhau-123"))
        self.assertIn("moi@bv.vn", self.run_cli("admin-list").output)
        self.assertIn("Đã đổi", self.run_cli("admin-passwd", "moi@bv.vn", input="mat-khau-moi-1\nmat-khau-moi-1\n").output)
        self.assertIsNotNone(A._admins().verify("moi@bv.vn", "mat-khau-moi-1"))
        self.assertIn("Đã tắt", self.run_cli("admin-disable", "moi@bv.vn").output)
        self.assertIsNone(A._admins().verify("moi@bv.vn", "mat-khau-moi-1"))
        self.assertIn("Đã bật", self.run_cli("admin-enable", "moi@bv.vn").output)

    def test_init_db_reports_connection_and_tables(self):
        r = self.run_cli("init-db")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Kết nối OK", r.output); self.assertIn("access_log", r.output); self.assertIn("admin_users", r.output)

    def test_init_db_without_url_fails_clearly(self):
        A.app.config["DATABASE_URL"] = ""
        r = self.run_cli("init-db")
        self.assertNotEqual(r.exit_code, 0); self.assertIn("DATABASE_URL", r.output)

    def test_short_password_rejected(self):
        r = self.run_cli("admin-add", "a@bv.vn", input="ngan\nngan\n")
        self.assertNotEqual(r.exit_code, 0); self.assertIn("8 ký tự", r.output)


if __name__ == "__main__":
    unittest.main()
