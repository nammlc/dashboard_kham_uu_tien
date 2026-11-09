"""
app.py — BVĐK Tâm Đức Cầu Quan
Flask app: authentication + dashboard routes.

Chạy local:   flask run  (hoặc python app.py)
Deploy Render: gunicorn app:app
"""

import os, json, time, uuid, threading, hmac, secrets
from datetime import datetime, timedelta

from flask import (Flask, render_template, redirect, url_for,
                   session, request, flash, jsonify, g, abort, send_from_directory)
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_login import (LoginManager, UserMixin,
                         login_user, logout_user, login_required, current_user)
from authlib.integrations.flask_client import OAuth
from flask_caching import Cache
from dotenv import load_dotenv

load_dotenv()

# ── Khởi tạo app ──────────────────────────────────────────────────────────────
app = Flask(__name__)
env = os.environ.get("FLASK_ENV", "development")
app.config.from_object(
    "config.ProductionConfig" if env == "production" else "config.DevelopmentConfig"
)

# ── Chạy sau reverse proxy (Coolify/Traefik) ──────────────────────────────────
# Tin header X-Forwarded-For / X-Forwarded-Proto từ đúng 1 proxy, để Flask biết trang đang chạy bằng
# HTTPS (cần cho link đăng nhập Google và cookie). Không đụng tới Host.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# ── Cache (tránh gọi Google Sheets quá nhiều) ────────────────────────────────
cache = Cache(app)

# ── Flask-Login ──────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view  = "login"
login_manager.login_message = "Vui lòng đăng nhập để tiếp tục."
login_manager.login_message_category = "info"

# ── OAuth ─────────────────────────────────────────────────────────────────────
oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=app.config["GOOGLE_CLIENT_ID"],
    client_secret=app.config["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ── Cơ sở dữ liệu (SQLite trên máy chủ): tài khoản admin + nhật ký truy cập ────────────────
from services import access_log as ACL, admin_users as ADM, db as DB, twofa as TF, mailer as MAIL, tokens as TK


def _admins() -> "ADM.AdminUsers":
    """Bảng admin_users. Lần đầu (bảng trống) tự tạo tài khoản từ ALLOWED_EMAILS + ADMIN_PASSWORD trong .env."""
    a = ADM.get(app.config["DATABASE_URL"])
    if a.count() == 0 and app.config.get("ALLOWED_EMAILS"):
        a.seed_from_env(app.config["ALLOWED_EMAILS"], app.config.get("ADMIN_PASSWORD", ""))
    return a


def _acl() -> "ACL.AccessLog":
    return ACL.get(app.config["DATABASE_URL"], app.config.get("LOG_RETENTION_DAYS", 90))


def _client_ip() -> str:
    return request.remote_addr or ""             # ProxyFix(x_for=1) đã lấy đúng IP khách từ X-Forwarded-For


_log_down_until = 0.0          # DB lỗi → tạm ngừng ghi log 30 giây, để trang không bị chậm vì chờ kết nối


def _log(event, *, email="", detail="", status=None):
    """Ghi 1 dòng nhật ký truy cập; lỗi ghi log không bao giờ được làm hỏng request."""
    global _log_down_until
    if not app.config.get("ACCESS_LOG_ENABLED", True) or not app.config.get("DATABASE_URL"):
        return
    if time.time() < _log_down_until:
        return
    try:
        _acl().add(event, email=email, ip=_client_ip(), ua=request.headers.get("User-Agent", ""),
                   method=request.method, path=request.path, status=status, detail=detail)
    except Exception:                            # noqa: BLE001
        _log_down_until = time.time() + 30
        app.logger.warning("không ghi được nhật ký truy cập", exc_info=True)


class User(UserMixin):
    def __init__(self, email: str, name: str, picture: str = ""):
        self.id      = email
        self.email   = email
        self.name    = name
        self.picture = picture

@login_manager.user_loader
def load_user(user_id: str):
    """Nạp người dùng từ bảng admin_users: tắt tài khoản trong DB là bị đăng xuất ngay ở request kế tiếp."""
    try:
        row = _admins().get_active(user_id)
    except Exception:                            # noqa: BLE001
        app.logger.warning("không đọc được bảng admin_users", exc_info=True)
        return None
    if not row:
        return None
    # Đổi / đặt lại mật khẩu là đăng xuất MỌI phiên cũ: mỗi phiên nhớ "vân tay" của băm mật khẩu lúc đăng nhập.
    # Phiên tạo trước bản nâng cấp (chưa có vân tay) được nhận và ghi nhớ từ lần này.
    fp = TK.fingerprint(row.get("password_hash"))
    have = session.get("pwv")
    if have is None:
        session["pwv"] = fp
    elif not hmac.compare_digest(str(have), fp):
        return None
    return User(row["email"], row["name"] or row["email"].split("@")[0], row["picture"])


# ════════════════════════════════════════════════════════════════════════════════
# TÁCH TÊN MIỀN: bệnh nhân  ↔  nhân viên (admin)
# ════════════════════════════════════════════════════════════════════════════════
# PATIENT_HOST (tuỳ chọn) = tên miền dành cho bệnh nhân, ví dụ dangkyhenkham.tracuuduoc-bvdktamduccauquan.com
#   • Mở trang chủ "/" của tên miền này → tự chuyển sang /dang-ky.
#   • Thêm PATIENT_HOST_PUBLIC_ONLY=1 → tên miền này CHỈ phục vụ trang công khai (/dang-ky, /dang-ky/xong,
#     /static); mọi đường dẫn khác (đăng nhập, bảng điều khiển...) trả 404. Trang admin chỉ vào được bằng
#     tên miền admin.
# Không đặt PATIENT_HOST → không thay đổi gì (chạy local / test như cũ).
def _norm_host(h: str) -> str:
    """Chuẩn hoá: bỏ https://, đường dẫn, cổng, dấu chấm cuối, chữ hoa."""
    h = h.strip().lower()
    h = h.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].rstrip(".")
    return h

# Cho phép nhiều tên miền bệnh nhân, cách nhau bằng dấu phẩy
_PATIENT_HOSTS = {_norm_host(h) for h in os.environ.get("PATIENT_HOST", "").split(",") if h.strip()}
_PATIENT_PUBLIC_ONLY = os.environ.get("PATIENT_HOST_PUBLIC_ONLY", "0") == "1"
_PATIENT_PUBLIC_PATHS = ("/dang-ky", "/dang-ky/xong", "/robots.txt", "/sitemap.xml", "/favicon.ico")


@app.before_request
def _patient_host_gate():
    if not _PATIENT_HOSTS:
        return None
    if _norm_host(request.host) not in _PATIENT_HOSTS:
        return None
    path = request.path
    if path == "/":
        # 301 (vĩnh viễn): Google gom "/" về /dang-ky thay vì giữ cả hai địa chỉ trong chỉ mục
        return redirect(url_for("register_page"), code=301)
    if _PATIENT_PUBLIC_ONLY and not (path in _PATIENT_PUBLIC_PATHS or path.startswith("/static/")):
        abort(404)
    return None


# ════════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ════════════════════════════════════════════════════════════════════════════════

# ── Tự đăng xuất khi không thao tác ──────────────────────────────────────────
# Mỗi request của admin đã đăng nhập làm mới mốc "last_active" (lưu trong session cookie đã ký).
# Quá SESSION_IDLE_MINUTES (mặc định 15 phút) không có request nào → đăng xuất, kể cả khi trình duyệt còn cookie
# "ghi nhớ đăng nhập" cũ (không có last_active → coi là hết phiên). Trang công khai /dang-ky không bị ảnh hưởng.
def _idle_seconds() -> int:
    return max(1, int(app.config.get("SESSION_IDLE_MINUTES", 15))) * 60


def _wants_json() -> bool:
    return (request.headers.get("X-Requested-With") == "fetch" or request.is_json
            or "application/json" in (request.headers.get("Accept") or ""))


@app.context_processor
def _inject_idle():
    return {"idle_seconds": _idle_seconds(), "idle_minutes": _idle_seconds() // 60}


@app.before_request
def _admin_idle_timeout():
    if app.config.get("LOGIN_DISABLED") or request.endpoint in (None, "static", "favicon"):
        return None
    if not current_user.is_authenticated:
        return None
    now, last = time.time(), session.get("last_active")
    try:
        expired = last is None or now - float(last) > _idle_seconds()
    except (TypeError, ValueError):
        expired = True
    if expired:
        _log("session_expired", email=getattr(current_user, "email", ""),
             detail=f"Không thao tác quá {_idle_seconds() // 60} phút")
        logout_user()
        session.pop("last_active", None)
        login_url = url_for("login", expired=1)
        if _wants_json():
            return jsonify({"ok": False, "level": "warning", "redirect": login_url,
                            "message": f"Phiên đăng nhập đã hết hạn do {_idle_seconds() // 60} phút không thao tác. "
                                       "Vui lòng đăng nhập lại."}), 401
        return redirect(login_url)
    if request.endpoint != "session_status":           # chỉ HỎI trạng thái thì không gia hạn phiên
        session["last_active"] = now
    return None


@app.route("/api/session-status")
def session_status():
    """Trình duyệt hỏi còn bao nhiêu giây trước khi hết phiên (không gia hạn). Hết phiên → 401 (xử lý ở before_request)."""
    if app.config.get("LOGIN_DISABLED"):
        return jsonify({"active": True, "remaining": _idle_seconds()})
    if not current_user.is_authenticated:
        return jsonify({"active": False, "redirect": url_for("login", expired=1)}), 401
    remaining = _idle_seconds() - (time.time() - float(session.get("last_active", 0)))
    return jsonify({"active": True, "remaining": max(0, int(remaining))})


@app.route("/api/keepalive", methods=["POST"])
def keepalive():
    """Người dùng vẫn đang thao tác trên trang (không gọi máy chủ) → gia hạn phiên (before_request đã làm mới mốc)."""
    if not app.config.get("LOGIN_DISABLED") and not current_user.is_authenticated:
        return jsonify({"ok": False, "redirect": url_for("login", expired=1)}), 401
    return jsonify({"ok": True})


# ── Ghi nhật ký truy cập ─────────────────────────────────────────────────────
# Chỉ ghi ĐƯỜNG DẪN (không ghi ?q=... vì có thể chứa tên / SĐT bệnh nhân) và không ghi nội dung form.
_LOG_SKIP = {None, "static", "favicon", "session_status", "keepalive", "robots_txt", "sitemap_xml",   # nhiễu / tự động
             "login", "login_google", "google_callback", "login_otp", "login_otp_setup", "login_otp_cancel", "forgot_password", "reset_password",
             "otp_recover_send", "otp_recover_confirm", "logout"}                          # đã ghi riêng ở trên

PAGE_NAMES = {
    "dashboard": "Tổng quan", "patients": "Bệnh nhân", "upcoming": "Lịch khám 3 ngày", "report": "Báo cáo",
    "report_detail": "Báo cáo chi tiết", "report_csv": "Tải CSV báo cáo", "import_excel": "Import lịch hẹn",
    "import_preview_page": "Import: xem trước", "import_confirm": "Import: xác nhận", "import_backfill": "Import: điền bổ sung",
    "import_csv": "Import: tải CSV", "reconcile_page": "Đối chiếu tái khám", "reconcile_upload": "Đối chiếu: tải file",
    "reconcile_run": "Đối chiếu: chạy", "reconcile_apply": "Đối chiếu: cập nhật trạng thái", "reconcile_one": "Đối chiếu: xử lý từng ca",
    "reconcile_csv": "Đối chiếu: tải CSV", "dup_scan": "Quét trùng", "dup_delete": "Xoá bản ghi trùng",
    "dup_clean_legacy": "Dọn bản ghi cũ", "api_update_status": "Cập nhật trạng thái", "refresh_cache": "Làm mới dữ liệu",
    "access_log": "Nhật ký truy cập", "register_page": "Đăng ký khám", "register_done": "Đăng ký xong",
}


@app.after_request
def _access_log_after(resp):
    try:
        ep = request.endpoint
        if ep in _LOG_SKIP or not app.config.get("ACCESS_LOG_ENABLED", True):
            return resp
        if current_user.is_authenticated:
            if resp.status_code >= 400 and resp.status_code != 401:
                return resp
            kind = "page" if request.method == "GET" else "action"
            _log(kind, email=getattr(current_user, "email", ""), status=resp.status_code,
                 detail=PAGE_NAMES.get(ep, ep or ""))
        elif ep == "register_page":                     # bệnh nhân (chưa đăng nhập) vào / gửi trang đăng ký
            if request.method == "GET" and resp.status_code == 200:
                _log("register_view", status=200)
            elif request.method == "POST" and resp.status_code in (301, 302, 303):
                _log("register_submit", status=resp.status_code, detail="Gửi đăng ký thành công")
    except Exception:                                   # noqa: BLE001
        app.logger.warning("lỗi ghi nhật ký truy cập", exc_info=True)
    return resp


LOG_PAGE_SIZES = (20, 50, 100, 200)
LOG_PAGE_DEFAULT = 20                      # số dòng mỗi trang mặc định của trang nhật ký


def page_window(page: int, total_pages: int, around: int = 2) -> list:
    """Danh sách số trang để hiển thị, None = dấu '…'. Ví dụ trang 6/20 → [1, None, 4, 5, 6, 7, 8, None, 20]."""
    if total_pages <= 1:
        return [1] if total_pages == 1 else []
    keep = {1, total_pages} | set(range(max(1, page - around), min(total_pages, page + around) + 1))
    out, prev = [], 0
    for p in sorted(keep):
        if p - prev == 2:                                 # chỉ thiếu đúng 1 trang → hiện luôn số đó thay vì dấu …
            out.append(prev + 1)
        elif p - prev > 2:
            out.append(None)
        out.append(p)
        prev = p
    return out


@app.route("/nhat-ky")
@login_required
def access_log():
    """Trang xem nhật ký truy cập (mới nhất ở trên), lọc theo từ khoá / loại sự kiện / ngày."""
    q     = request.args.get("q", "").strip()[:80]
    event = request.args.get("event", "")
    d_from, d_to = _parse_date(request.args.get("date_from", "")), _parse_date(request.args.get("date_to", ""))
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", LOG_PAGE_DEFAULT))
    except ValueError:
        per_page = LOG_PAGE_DEFAULT
    if per_page not in LOG_PAGE_SIZES:
        per_page = LOG_PAGE_DEFAULT
    try:
        log = _acl()
        rows, total = log.query(q, event, d_from, d_to, page, per_page)
        total_pages = max(1, -(-total // per_page))
        if page > total_pages:                          # gõ trang vượt quá → về trang cuối thay vì hiện bảng trống
            page = total_pages
            rows, total = log.query(q, event, d_from, d_to, page, per_page)
        stats = log.stats()
    except Exception:                                   # noqa: BLE001
        app.logger.exception("không đọc được nhật ký truy cập")
        rows, total, stats = [], 0, {"today": 0, "fails_24h": 0, "users_today": 0, "retention_days": app.config.get("LOG_RETENTION_DAYS", 90)}
        flash("Không đọc được cơ sở dữ liệu nhật ký truy cập.", "error")
    for r in rows:
        r["when"], r["device"] = ACL.fmt_time(r["ts"]), ACL.short_ua(r["ua"])
        r["label"] = ACL.EVENTS.get(r["event"], r["event"])
    total_pages = max(1, -(-total // per_page))
    return render_template("dashboard/access_log.html", rows=rows, total=total, page=page, per_page=per_page, q=q,
                           total_pages=total_pages, pages=page_window(page, total_pages), page_sizes=LOG_PAGE_SIZES,
                           first_row=(page - 1) * per_page + 1 if total else 0, last_row=min(page * per_page, total),
                           event=event, events=ACL.EVENTS, stats=stats,
                           date_from=d_from.isoformat() if d_from else "", date_to=d_to.isoformat() if d_to else "")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        try:
            row = _admins().verify(email, password)         # mật khẩu so với bản băm trong bảng admin_users
        except DB.NotConfigured:
            flash("Hệ thống chưa được cấu hình cơ sở dữ liệu (thiếu DATABASE_URL). Vui lòng liên hệ quản trị viên.", "error")
            return render_template("auth/login.html", expired=False)
        except DB.DBError:
            app.logger.exception("lỗi cơ sở dữ liệu khi đăng nhập")
            flash("Hệ thống chưa kết nối được cơ sở dữ liệu tài khoản. Vui lòng thử lại sau.", "error")
            return render_template("auth/login.html", expired=False)

        if row:                                             # đúng mật khẩu → còn bước nhập mã OTP (nếu bật) rồi mới đăng nhập
            return _begin_second_factor(row, "Email + mật khẩu", _safe_next(request.args.get("next")))
        else:
            _log("login_fail", email=email[:120], detail="Sai email / mật khẩu hoặc tài khoản bị tắt")
            flash("Email hoặc mật khẩu không đúng.", "error")

    return render_template("auth/login.html", expired=bool(request.args.get("expired")))


def _safe_next(nxt) -> str:
    """Chỉ nhận đường dẫn NỘI BỘ ("/abc"); chặn "//evil.com", "/\\evil.com" và mọi địa chỉ ngoài (open redirect)."""
    nxt = str(nxt or "")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt and "\n" not in nxt else ""


def _google_enabled() -> bool:
    return bool(app.config.get("GOOGLE_CLIENT_ID") and app.config.get("GOOGLE_CLIENT_SECRET"))


@app.context_processor
def _inject_auth_ctx():
    return {"google_login_enabled": _google_enabled(), "mail_enabled": _mail_enabled(), "csrf_token": _csrf_token,
            "forgot_enabled": _forgot_enabled()}


def _google_fail(event_detail: str, message: str, email: str = ""):
    _log("login_fail", email=email[:120], detail=event_detail)
    flash(message, "error")
    return redirect(url_for("login"))


@app.route("/login/google")
def login_google():
    """Bước 1: chuyển sang Google. Chưa cấu hình GOOGLE_CLIENT_ID/SECRET thì báo rõ thay vì lỗi 500."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if not _google_enabled():
        return _google_fail("Google: chưa cấu hình GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET",
                            "Đăng nhập bằng Google chưa được bật trên hệ thống. Vui lòng đăng nhập bằng email và mật khẩu.")
    session["oauth_next"] = _safe_next(request.args.get("next"))
    redirect_uri = app.config.get("GOOGLE_REDIRECT_URI") or url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri, prompt="select_account")      # luôn cho chọn tài khoản (máy dùng chung)


@app.route("/login/google/callback")
def google_callback():
    """Bước 2: Google trả về. Chỉ vào được khi email đã được Google XÁC MINH và đang bật trong bảng admin_users."""
    nxt = _safe_next(session.pop("oauth_next", ""))
    if not _google_enabled():
        return _google_fail("Google: chưa cấu hình GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET",
                            "Đăng nhập bằng Google chưa được bật trên hệ thống. Vui lòng đăng nhập bằng email và mật khẩu.")

    err = request.args.get("error")
    if err:                                                      # bấm Huỷ / từ chối cấp quyền
        msg = "Bạn đã huỷ đăng nhập bằng Google." if err == "access_denied" else "Google từ chối đăng nhập. Vui lòng thử lại."
        return _google_fail(f"Google: {err[:60]}", msg)

    try:
        token     = oauth.google.authorize_access_token()
        user_info = token.get("userinfo") or oauth.google.userinfo()
    except Exception as e:                                       # noqa: BLE001 — state không khớp (mở link cũ/đổi trình duyệt), mạng lỗi…
        app.logger.warning("Google OAuth lỗi: %s: %s", type(e).__name__, e)
        return _google_fail(f"Google: không hoàn tất được ({type(e).__name__})",
                            "Chưa hoàn tất được đăng nhập Google (phiên đăng nhập bị hết hạn hoặc mạng lỗi). Vui lòng bấm “Đăng nhập bằng Google” lại.")

    email = str(user_info.get("email") or "").strip().lower()
    if not email or user_info.get("email_verified") not in (True, "true"):
        return _google_fail("Google: email chưa được Google xác minh", "Email của tài khoản Google này chưa được xác minh nên không thể đăng nhập.", email)

    try:
        row = _admins().get_active(email)                        # chỉ email có trong bảng admin_users (và đang bật) mới vào được
    except DB.NotConfigured:
        return _google_fail("Google: thiếu DATABASE_URL", "Hệ thống chưa được cấu hình cơ sở dữ liệu (thiếu DATABASE_URL). Vui lòng liên hệ quản trị viên.", email)
    except DB.DBError:
        app.logger.exception("lỗi cơ sở dữ liệu khi đăng nhập Google")
        return _google_fail("Google: lỗi cơ sở dữ liệu", "Hệ thống chưa kết nối được cơ sở dữ liệu tài khoản. Vui lòng thử lại sau.", email)

    if not row:
        return _google_fail("Google: tài khoản không có trong danh sách admin",
                            f"Tài khoản {email} không được phép truy cập. Hãy nhờ quản trị viên thêm email này vào danh sách admin.", email)

    return _begin_second_factor(row, "Google", nxt, {"name": user_info.get("name", ""), "picture": user_info.get("picture", "")}, proof=True)


# ── Bước 2 của đăng nhập: mã OTP (Google Authenticator) ───────────────────────────────────────────────────────
# Qua bước 1 (mật khẩu hoặc Google) CHƯA được vào: chỉ lưu "đang chờ OTP" trong phiên (tối đa OTP_PENDING_SECONDS).
# Chỉ khi nhập đúng mã (hoặc đúng mã lúc thiết lập lần đầu) mới login_user().
OTP_PENDING_SECONDS = 300      # từ lúc qua bước 1 đến lúc phải nhập xong mã
OTP_MAX_TRIES       = 5        # sai quá số lần này trong 1 lượt đăng nhập → phải đăng nhập lại từ đầu
_otp_limiter        = TF.FailLimiter(limit=10, window=900)        # 10 lần sai / 15 phút / email → tạm khoá


def _two_factor_on() -> bool:
    return bool(app.config.get("TWO_FACTOR_ENABLED", True))


def _finish_login(row, method: str, nxt: str = "", profile: dict | None = None, proof: bool = False):
    """Đăng nhập thật sự (đã qua MỌI bước xác thực).
    proof=True: bước 1 là Google / liên kết email (đã chứng minh danh tính không cần mật khẩu) → được đặt mật khẩu mới
    mà không phải nhập mật khẩu cũ (dành cho người quên mật khẩu)."""
    login_user(User(row["email"], row["name"] or row["email"].split("@")[0], row["picture"]), remember=False)   # không "ghi nhớ"
    session["last_active"] = time.time()
    session["pwv"] = TK.fingerprint(row.get("password_hash"))
    session["identity_proof"] = bool(proof)
    for k in ("otp_pending", "otp_setup", "otp_reenroll"):
        session.pop(k, None)
    profile = profile or {}
    try:
        _admins().touch_login(row["email"], profile.get("name", ""), profile.get("picture", ""))
    except Exception:                                            # noqa: BLE001 — chỉ là ghi giờ đăng nhập, không được chặn người dùng
        app.logger.warning("không ghi được last_login_at", exc_info=True)
    _log("login_ok", email=row["email"], detail=method)
    return redirect(nxt or url_for("dashboard"))


def _begin_second_factor(row, method: str, nxt: str = "", profile: dict | None = None, proof: bool = False):
    """Đã đúng bước 1 (mật khẩu/Google). Bật 2 bước → sang trang nhập OTP; tắt → đăng nhập luôn."""
    if not _two_factor_on():
        return _finish_login(row, method, nxt, profile, proof)
    session.pop("otp_setup", None)
    session.pop("otp_reenroll", None)
    session["otp_pending"] = {"email": row["email"], "method": method, "next": nxt, "t": time.time(), "tries": 0, "proof": bool(proof),
                              "profile": {"name": (profile or {}).get("name", "")[:120], "picture": (profile or {}).get("picture", "")[:300]}}
    return redirect(url_for("login_otp"))


def _otp_guard():
    """→ (pending, hàng admin, None) nếu đang ở đúng bước OTP; ngược lại (None, None, redirect)."""
    if current_user.is_authenticated:
        return None, None, redirect(url_for("dashboard"))
    p = session.get("otp_pending")
    if not isinstance(p, dict) or time.time() - float(p.get("t", 0)) > OTP_PENDING_SECONDS:
        session.pop("otp_pending", None)
        session.pop("otp_setup", None)
        flash("Phiên xác thực đã hết hạn. Vui lòng đăng nhập lại.", "error")
        return None, None, redirect(url_for("login"))
    try:
        row = _admins().get_active(p.get("email", ""))
    except (DB.NotConfigured, DB.DBError):
        app.logger.exception("lỗi cơ sở dữ liệu ở bước OTP")
        session.pop("otp_pending", None)
        flash("Hệ thống chưa kết nối được cơ sở dữ liệu tài khoản. Vui lòng thử lại sau.", "error")
        return None, None, redirect(url_for("login"))
    if not row:                                                  # bị tắt giữa chừng
        session.pop("otp_pending", None)
        flash("Tài khoản này không còn được phép truy cập.", "error")
        return None, None, redirect(url_for("login"))
    return p, row, None


def _otp_restart(message: str):
    for k in ("otp_pending", "otp_setup", "otp_reenroll"):
        session.pop(k, None)
    flash(message, "error")
    return redirect(url_for("login"))


def _otp_wrong(p, email: str):
    """Ghi nhận 1 lần nhập sai. Trả redirect nếu phải bắt đăng nhập lại, None nếu cho thử tiếp."""
    _otp_limiter.fail(email)
    p["tries"] = int(p.get("tries", 0)) + 1
    session["otp_pending"] = p
    _log("login_fail", email=email[:120], detail=f"Sai mã OTP (lần {p['tries']})")
    if p["tries"] >= OTP_MAX_TRIES:
        return _otp_restart(f"Nhập sai mã quá {OTP_MAX_TRIES} lần. Vui lòng đăng nhập lại.")
    flash("Mã không đúng hoặc đã hết hạn. Hãy nhập mã MỚI NHẤT đang hiện trong ứng dụng.", "error")
    return None


def _otp_locked(email: str):
    if _otp_limiter.locked(email):
        _log("login_fail", email=email[:120], detail="OTP: tạm khoá do nhập sai nhiều lần")
        return _otp_restart("Nhập sai mã quá nhiều lần nên tài khoản bị tạm khoá vài phút. Vui lòng thử lại sau.")
    return None


@app.route("/login/2fa", methods=["GET", "POST"])
def login_otp():
    p, row, resp = _otp_guard()
    if resp:
        return resp
    secret = TF.decrypt(row.get("totp_secret") or "", app.config["SECRET_KEY"])
    if not secret:                                               # chưa thiết lập (hoặc mã cũ không giải mã được) → quét QR
        return redirect(url_for("login_otp_setup"))
    email = row["email"]
    if request.method == "POST":
        resp = _otp_locked(email)
        if resp:
            return resp
        try:
            step = TF.verify(secret, request.form.get("code"), row.get("totp_last_step") or 0)
            ok = step is not None and _admins().use_totp_step(email, step)      # mỗi mã chỉ dùng được 1 lần
        except DB.DBError:
            app.logger.exception("lỗi cơ sở dữ liệu khi kiểm tra OTP")
            flash("Chưa kiểm tra được mã do lỗi cơ sở dữ liệu. Vui lòng thử lại sau ít phút.", "error")
            return render_template("auth/otp.html", email=email, method=p["method"], remaining=OTP_MAX_TRIES - int(p.get("tries", 0)))
        if ok:
            _otp_limiter.reset(email)
            return _finish_login(row, f"{p['method']} + OTP", p.get("next", ""), p.get("profile"), bool(p.get("proof")))
        resp = _otp_wrong(p, email)
        if resp:
            return resp
    return render_template("auth/otp.html", email=email, method=p["method"], remaining=OTP_MAX_TRIES - int(p.get("tries", 0)))


@app.route("/login/2fa/setup", methods=["GET", "POST"])
def login_otp_setup():
    """Lần đầu: hiện mã QR để thêm vào Google Authenticator, nhập thử 1 mã đúng mới bật 2 bước và đăng nhập.
    Tài khoản ĐÃ thiết lập thì KHÔNG vào lại được đây (kẻ biết mật khẩu không thể tự thay bằng điện thoại của mình)
    — mất điện thoại thì quản trị viên chạy: flask --app app admin-2fa-reset email"""
    p, row, resp = _otp_guard()
    if resp:
        return resp
    key, email, old_enc = app.config["SECRET_KEY"], row["email"], row.get("totp_secret") or ""
    # Đổi điện thoại khi mất máy cũ: chỉ khi vừa bấm liên kết trong email (otp_recover_confirm) cho ĐÚNG mã bí mật hiện tại
    reenroll = bool(old_enc) and hmac.compare_digest(str(session.get("otp_reenroll") or "x"), TK.fingerprint(old_enc))
    if TF.decrypt(old_enc, key) and not reenroll:
        return redirect(url_for("login_otp"))
    enc = session.get("otp_setup")
    secret = TF.decrypt(enc, key) if enc else None
    if not secret:
        secret = TF.new_secret()
        session["otp_setup"] = TF.encrypt(secret, key)           # trong phiên cũng để dạng mã hoá
    if request.method == "POST":
        resp = _otp_locked(email)
        if resp:
            return resp
        step = TF.verify(secret, request.form.get("code"))
        if step is not None:
            try:
                saved = _admins().set_totp(email, TF.encrypt(secret, key), step, expect_old=old_enc)
            except DB.DBError:
                app.logger.exception("lỗi cơ sở dữ liệu khi lưu 2 bước")
                flash("Chưa lưu được thiết lập do lỗi cơ sở dữ liệu. Vui lòng thử lại sau ít phút.", "error")
                saved = None
            if saved:
                _otp_limiter.reset(email)
                _log("otp_setup", email=email, detail="Đổi điện thoại (qua liên kết email)" if reenroll else "Thiết lập xác thực 2 bước")
                return _finish_login(row, f"{p['method']} + OTP ({'đổi điện thoại' if reenroll else 'thiết lập lần đầu'})",
                                     p.get("next", ""), p.get("profile"), bool(p.get("proof")))
            if saved is False:                                   # có người thiết lập trước ở lượt khác
                return redirect(url_for("login_otp"))
        else:
            resp = _otp_wrong(p, email)
            if resp:
                return resp
    uri = TF.provisioning_uri(secret, email, app.config.get("TWO_FACTOR_ISSUER", TF.DEFAULT_ISSUER))
    return render_template("auth/otp_setup.html", email=email, method=p["method"], remaining=OTP_MAX_TRIES - int(p.get("tries", 0)),
                           qr_svg=TF.qr_svg(uri), secret_text=TF.format_secret(secret), otp_uri=uri)


@app.route("/login/2fa/cancel")
def login_otp_cancel():
    for k in ("otp_pending", "otp_setup", "otp_reenroll"):
        session.pop(k, None)
    return redirect(url_for("login"))


# ── CSRF cho các biểu mẫu đổi thông tin đăng nhập ──────────────────────────────────────────────────────────────
def _csrf_token() -> str:
    t = session.get("csrf")
    if not t:
        t = session["csrf"] = secrets.token_urlsafe(24)
    return t


def _csrf_ok() -> bool:
    sent, mine = str(request.form.get("csrf", "")), str(session.get("csrf", ""))
    return bool(mine) and hmac.compare_digest(sent, mine)


# ── Gửi email (quên mật khẩu / đổi điện thoại) ───────────────────────────────────────────────────────────────────
RESET_LINK_MINUTES = TK.DEFAULT_MAX_AGE // 60
_reset_email_limiter = TF.FailLimiter(limit=3, window=3600)       # tối đa 3 yêu cầu / giờ / email / loại
_reset_ip_limiter    = TF.FailLimiter(limit=20, window=3600)      # tối đa 20 yêu cầu / giờ / IP


def _mail_enabled() -> bool:
    return MAIL.configured(app.config) and str(app.config.get("ADMIN_BASE_URL", "")).startswith(("http://", "https://"))


def _send_mail_async(to: str, subject: str, body: str) -> None:
    """Gửi ở luồng nền: trả lời người dùng ngay (thời gian phản hồi không lộ email có tồn tại hay không) và SMTP chậm không treo trang."""
    cfg = dict(app.config)

    def run():
        try:
            MAIL.send(cfg, to, subject, body)
        except MAIL.MailError as e:
            app.logger.warning("gửi email thất bại: %s", e)
        except Exception:                                            # noqa: BLE001
            app.logger.exception("gửi email thất bại")
    threading.Thread(target=run, daemon=True).start()


def _admin_link(endpoint: str, **values) -> str:
    return app.config["ADMIN_BASE_URL"] + url_for(endpoint, **values)


def _reset_throttled(kind: str, email: str) -> bool:
    """Ghi nhận 1 yêu cầu gửi email; True nếu đã vượt giới hạn (không gửi nữa)."""
    ip, ekey = _client_ip(), f"{kind}:{email}"
    blocked = _reset_email_limiter.locked(ekey) or _reset_ip_limiter.locked(ip)
    _reset_email_limiter.fail(ekey)
    _reset_ip_limiter.fail(ip)
    return blocked


def _mask_email(e: str) -> str:
    name, _, dom = e.partition("@")
    return f"{name[:2]}{'*' * max(1, len(name) - 2)}@{dom}"


def _db_down(message="Hệ thống chưa kết nối được cơ sở dữ liệu tài khoản. Vui lòng thử lại sau."):
    app.logger.exception("lỗi cơ sở dữ liệu")
    flash(message, "error")


# ── Quên mật khẩu ──────────────────────────────────────────────────────────────────────────────────────────────
def _otp_reset_enabled() -> bool:
    return _two_factor_on() and bool(app.config.get("PASSWORD_RESET_VIA_OTP", True))


def _forgot_enabled() -> bool:
    return _mail_enabled() or _otp_reset_enabled()


def _notify_security(email: str, subject: str, text: str) -> None:
    """Báo qua email cho chủ tài khoản khi mật khẩu/email đăng nhập bị đổi (chỉ khi đã cấu hình gửi email)."""
    if _mail_enabled():
        _send_mail_async(email, subject, f"Xin chào,\n\n{text}\n\nNếu không phải bạn, hãy liên hệ quản trị viên ngay.\n")


def _generic_reset_failure():
    flash("Email hoặc mã xác thực không đúng. Vui lòng kiểm tra lại.", "error")
    return redirect(url_for("forgot_password"))


@app.route("/quen-mat-khau", methods=["GET", "POST"])
def forgot_password():
    """Hai cách lấy lại mật khẩu: (1) liên kết gửi qua email; (2) mã OTP 6 số của Google Authenticator (không cần email)."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    mail_on, otp_on = _mail_enabled(), _otp_reset_enabled()
    if not (mail_on or otp_on):
        flash("Chức năng lấy lại mật khẩu chưa được bật. Vui lòng nhờ quản trị viên đặt lại mật khẩu cho bạn.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        if not _csrf_ok():
            flash("Phiên làm việc đã hết hạn. Vui lòng thử lại.", "error")
            return redirect(url_for("forgot_password"))
        action = request.form.get("action", "email")
        email = request.form.get("email", "").strip().lower()[:120]
        if action == "otp" and otp_on:
            return _forgot_by_otp(email)
        if action == "email" and mail_on:
            return _forgot_by_email(email)
        flash("Cách lấy lại mật khẩu này đang tắt.", "error")
        return redirect(url_for("forgot_password"))
    return render_template("auth/forgot.html", mail_on=mail_on, otp_on=otp_on)


def _forgot_by_email(email: str):
    throttled = _reset_throttled("pw", email)
    try:
        row = _admins().get_active(email) if email and not throttled else None
    except (DB.NotConfigured, DB.DBError):
        _db_down()
        return redirect(url_for("forgot_password"))
    if row and row.get("password_hash"):                         # tài khoản "chỉ Google" không có mật khẩu để đặt lại
        token = TK.make(app.config["SECRET_KEY"], TK.PASSWORD_RESET, email, row["password_hash"])
        _send_mail_async(email, "Đặt lại mật khẩu — BVĐK Tâm Đức Cầu Quan",
                         f"Xin chào {row['name'] or email},\n\nBạn (hoặc ai đó) vừa yêu cầu đặt lại mật khẩu trang quản trị.\n"
                         f"Bấm liên kết dưới đây để đặt mật khẩu mới (có hiệu lực {RESET_LINK_MINUTES} phút, chỉ dùng được 1 lần):\n\n"
                         f"{_admin_link('reset_password', token=token)}\n\n"
                         "Nếu không phải bạn, hãy bỏ qua email này — mật khẩu hiện tại vẫn giữ nguyên.\n")
    _log("reset_request", email=email, detail="Quên mật khẩu")
    # Trả lời GIỐNG HỆT dù email có tồn tại hay không (không cho dò xem email nào là admin)
    flash("Nếu email này thuộc một tài khoản dùng mật khẩu, chúng tôi đã gửi liên kết đặt lại. Hãy kiểm tra hộp thư (cả mục thư rác).", "info")
    return redirect(url_for("forgot_password"))


def _forgot_by_otp(email: str):
    """Email + mã OTP hiện tại + mật khẩu mới trong MỘT lần gửi. Kiểm tra mật khẩu mới TRƯỚC khi tiêu thụ mã OTP
    (nhập mật khẩu quá ngắn không làm mất mã). Mọi lý do thất bại đều trả cùng một câu."""
    ekey, ip = f"reset:{email}", _client_ip()
    if _otp_limiter.locked(ekey) or _reset_ip_limiter.locked(ip):
        flash("Thử quá nhiều lần. Vui lòng đợi khoảng 15 phút rồi thử lại.", "error")
        return redirect(url_for("forgot_password"))
    pw1, pw2 = request.form.get("password", ""), request.form.get("password2", "")
    if pw1 != pw2:
        flash("Hai lần nhập mật khẩu chưa giống nhau.", "error")
        return redirect(url_for("forgot_password"))
    try:
        ADM.check_password_rules(pw1)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("forgot_password"))
    try:
        row = _admins().get_active(email) if email else None
        secret = TF.decrypt((row or {}).get("totp_secret") or "", app.config["SECRET_KEY"]) if (row and row.get("password_hash")) else None
        step = TF.verify(secret, request.form.get("code"), (row or {}).get("totp_last_step") or 0) if secret else None
        ok = step is not None and _admins().use_totp_step(row["email"], step)           # mỗi mã chỉ dùng 1 lần
        if ok:
            _admins().set_password(row["email"], pw1)
    except (DB.NotConfigured, DB.DBError):
        _db_down("Chưa đặt lại được mật khẩu do lỗi cơ sở dữ liệu. Vui lòng thử lại sau ít phút.")
        return redirect(url_for("forgot_password"))
    if not ok:
        _otp_limiter.fail(ekey)
        _reset_ip_limiter.fail(ip)
        _log("login_fail", email=email[:120], detail="Quên mật khẩu: email/mã OTP không đúng")
        return _generic_reset_failure()
    _otp_limiter.reset(ekey)
    _log("password_reset", email=row["email"], detail="Đặt lại mật khẩu bằng mã OTP")
    _notify_security(row["email"], "Mật khẩu tài khoản quản trị vừa được đặt lại — BVĐK Tâm Đức Cầu Quan",
                     "Mật khẩu tài khoản quản trị của bạn vừa được đặt lại bằng mã xác thực 2 bước.")
    flash("Đã đặt mật khẩu mới. Hãy đăng nhập bằng mật khẩu mới.", "info")
    return redirect(url_for("login"))


@app.route("/dat-lai-mat-khau/<token>", methods=["GET", "POST"])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    d = TK.read(app.config["SECRET_KEY"], TK.PASSWORD_RESET, token)
    try:
        row = _admins().get_active(d[0]) if d else None
    except (DB.NotConfigured, DB.DBError):
        _db_down()
        return redirect(url_for("login"))
    # vân tay = băm mật khẩu hiện tại: đặt xong là liên kết (và mọi liên kết cũ) tự hết hiệu lực
    if not (d and row and row.get("password_hash") and TK.matches(d[1], row["password_hash"])):
        flash("Liên kết không hợp lệ hoặc đã hết hạn (mỗi liên kết chỉ dùng được một lần). Vui lòng yêu cầu liên kết mới.", "error")
        return redirect(url_for("forgot_password") if _mail_enabled() else url_for("login"))
    if request.method == "POST":
        if not _csrf_ok():
            flash("Phiên làm việc đã hết hạn. Vui lòng mở lại liên kết trong email.", "error")
            return redirect(url_for("reset_password", token=token))
        pw1, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if pw1 != pw2:
            flash("Hai lần nhập mật khẩu chưa giống nhau.", "error")
        else:
            try:
                _admins().set_password(row["email"], pw1)
            except ValueError as e:
                flash(str(e), "error")
            except (DB.NotConfigured, DB.DBError):
                _db_down("Chưa lưu được mật khẩu mới do lỗi cơ sở dữ liệu. Vui lòng thử lại sau ít phút.")
            else:
                _log("password_reset", email=row["email"], detail="Đặt lại mật khẩu qua email")
                _notify_security(row["email"], "Mật khẩu tài khoản quản trị vừa được đặt lại — BVĐK Tâm Đức Cầu Quan",
                                 "Mật khẩu tài khoản quản trị của bạn vừa được đặt lại bằng liên kết gửi qua email.")
                flash("Đã đặt mật khẩu mới. Hãy đăng nhập bằng mật khẩu mới.", "info")
                return redirect(url_for("login"))
    return render_template("auth/reset.html", email=row["email"], token=token)


# ── Mất điện thoại (chưa đăng nhập được): liên kết email + đã qua bước 1 → quét QR điện thoại mới ──────────────
@app.route("/login/2fa/khoi-phuc", methods=["POST"])
def otp_recover_send():
    p, row, resp = _otp_guard()
    if resp:
        return resp
    if not _mail_enabled():
        flash("Chức năng gửi email chưa được bật. Hãy nhờ quản trị viên chạy lệnh admin-2fa-reset cho tài khoản của bạn.", "error")
        return redirect(url_for("login_otp"))
    if not _csrf_ok():
        flash("Phiên làm việc đã hết hạn. Vui lòng thử lại.", "error")
        return redirect(url_for("login_otp"))
    email, enc = row["email"], row.get("totp_secret") or ""
    if not enc:
        return redirect(url_for("login_otp_setup"))                  # chưa thiết lập thì không cần "khôi phục"
    if _reset_throttled("2fa", email):
        flash("Bạn đã yêu cầu quá nhiều lần. Vui lòng thử lại sau khoảng 1 giờ.", "error")
        return redirect(url_for("login_otp"))
    token = TK.make(app.config["SECRET_KEY"], TK.TWOFA_RESET, email, enc)
    _send_mail_async(email, "Đổi điện thoại xác thực 2 bước — BVĐK Tâm Đức Cầu Quan",
                     f"Xin chào {row['name'] or email},\n\nBạn vừa yêu cầu thiết lập lại xác thực 2 bước (mất hoặc đổi điện thoại).\n"
                     f"Bấm liên kết dưới đây để quét mã QR trên điện thoại mới (có hiệu lực {RESET_LINK_MINUTES} phút, chỉ dùng được 1 lần):\n\n"
                     f"{_admin_link('otp_recover_confirm', token=token)}\n\n"
                     "Nếu không phải bạn, hãy bỏ qua email này và báo quản trị viên — điện thoại cũ vẫn dùng được bình thường.\n")
    _log("reset_request", email=email, detail="Mất / đổi điện thoại 2 bước")
    flash(f"Đã gửi liên kết đến {_mask_email(email)}. Mở email (hiệu lực {RESET_LINK_MINUTES} phút) để thiết lập điện thoại mới.", "info")
    return redirect(url_for("login_otp"))


@app.route("/login/2fa/khoi-phuc/<token>", methods=["GET", "POST"])
def otp_recover_confirm(token):
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    d = TK.read(app.config["SECRET_KEY"], TK.TWOFA_RESET, token)
    try:
        row = _admins().get_active(d[0]) if d else None
    except (DB.NotConfigured, DB.DBError):
        _db_down()
        return redirect(url_for("login"))
    enc = (row or {}).get("totp_secret") or ""
    if not (d and row and enc and TK.matches(d[1], enc)):            # đổi điện thoại xong là liên kết tự hết hiệu lực
        flash("Liên kết không hợp lệ hoặc đã hết hạn (mỗi liên kết chỉ dùng được một lần). Hãy đăng nhập lại và yêu cầu liên kết mới.", "error")
        return redirect(url_for("login"))
    email, has_pw = row["email"], bool(row.get("password_hash"))
    p = session.get("otp_pending")
    pending_ok = (isinstance(p, dict) and p.get("email") == email and time.time() - float(p.get("t", 0)) <= OTP_PENDING_SECONDS)
    if request.method == "POST":
        if not _csrf_ok():
            flash("Phiên làm việc đã hết hạn. Vui lòng mở lại liên kết trong email.", "error")
            return redirect(url_for("otp_recover_confirm", token=token))
        if _otp_limiter.locked(email):
            return _otp_restart("Nhập sai quá nhiều lần nên tài khoản bị tạm khoá vài phút. Vui lòng thử lại sau.")
        if pending_ok:                                               # vừa qua bước 1 ngay trên trình duyệt này + có email → đủ
            method, nxt, profile = f"{p['method']} + liên kết email", p.get("next", ""), p.get("profile")
        elif has_pw:                                                 # mở email ở máy khác → nhập mật khẩu thay cho bước 1
            try:
                ok = bool(_admins().verify(email, request.form.get("password", "")))
            except (DB.NotConfigured, DB.DBError):
                _db_down()
                return redirect(url_for("otp_recover_confirm", token=token))
            if not ok:
                _otp_limiter.fail(email)
                _log("login_fail", email=email[:120], detail="Đổi điện thoại 2 bước: sai mật khẩu")
                flash("Mật khẩu không đúng.", "error")
                return redirect(url_for("otp_recover_confirm", token=token))
            method, nxt, profile = "Email + mật khẩu + liên kết email", "", None
        else:                                                        # chỉ-Google và chưa đăng nhập Google ở trình duyệt này
            flash("Hãy đăng nhập bằng Google trên trình duyệt này trước, rồi mở lại liên kết trong email.", "error")
            return redirect(url_for("login"))
        session["otp_pending"] = {"email": email, "method": method, "next": nxt, "t": time.time(), "tries": 0, "profile": profile or {},
                                  "proof": True}                      # đã có email + (mật khẩu | Google) → đủ để đặt mật khẩu mới
        session["otp_reenroll"] = TK.fingerprint(enc)
        session.pop("otp_setup", None)
        return redirect(url_for("login_otp_setup"))
    return render_template("auth/otp_recover.html", email=email, token=token, pending_ok=pending_ok, has_pw=has_pw)


# ── Tài khoản & bảo mật (đã đăng nhập): đổi điện thoại khi vẫn còn điện thoại cũ ──────────────────────────────
PHONE_CHANGE_SECONDS = 600


@app.route("/tai-khoan")
@login_required
def account_page():
    try:
        row = _admins().get_active(current_user.email)
    except (DB.NotConfigured, DB.DBError):
        _db_down()
        row = None
    enabled_at = (row or {}).get("totp_enabled_at")
    base_set = str(app.config.get("ADMIN_BASE_URL", "")).startswith(("http://", "https://"))
    mail = {"smtp_ok": MAIL.configured(app.config), "base_ok": base_set, "sender": MAIL.sender(app.config),
            "gmail": MAIL._is_gmail(app.config.get("SMTP_HOST", "")),
            "suggest_base": "" if base_set else request.host_url.rstrip("/")}     # chỉ GỢI Ý cho admin đang đăng nhập, không tự dùng
    return render_template("account/account.html", row=row, two_factor_on=_two_factor_on(),
                           has_totp=bool((row or {}).get("totp_secret")), mail=mail,
                           enabled_at=ACL.fmt_time(enabled_at) if enabled_at else "")


@app.route("/tai-khoan/thu-email", methods=["POST"])
@login_required
def account_mail_test():
    """Gửi thử 1 email tới chính email của người đang đăng nhập để kiểm tra cấu hình Gmail/SMTP (gửi ngay, báo rõ lỗi)."""
    if not _csrf_ok():
        flash("Phiên làm việc đã hết hạn. Vui lòng thử lại.", "error")
    elif not MAIL.configured(app.config):
        flash("Chưa cấu hình gửi email. Cần GMAIL_USER + GMAIL_APP_PASSWORD (hoặc SMTP_*).", "error")
    elif _reset_throttled("mailtest", current_user.email):
        flash("Bạn đã gửi thử quá nhiều lần. Vui lòng thử lại sau khoảng 1 giờ.", "error")
    else:
        try:
            MAIL.send(app.config, current_user.email, "Email thử — BVĐK Tâm Đức Cầu Quan",
                      "Nếu bạn đọc được email này thì cấu hình gửi email đã đúng.\n"
                      "Chức năng Quên mật khẩu / Đổi email / Đổi điện thoại sẽ gửi thư tới hộp thư này.\n")
            flash(f"Đã gửi email thử tới {_mask_email(current_user.email)}. Hãy kiểm tra hộp thư (cả mục thư rác).", "info")
        except MAIL.MailError as e:
            flash(str(e), "error")
    return redirect(url_for("account_page"))


@app.route("/tai-khoan/doi-dien-thoai", methods=["GET", "POST"])
@login_required
def account_change_phone():
    """Bước 1: nhập mã OTP HIỆN TẠI (chứng minh vẫn đang cầm điện thoại cũ) → Bước 2: quét QR điện thoại mới và nhập thử mã."""
    if not _two_factor_on():
        flash("Xác thực 2 bước đang tắt trên hệ thống.", "error")
        return redirect(url_for("account_page"))
    try:
        row = _admins().get_active(current_user.email)
    except (DB.NotConfigured, DB.DBError):
        _db_down()
        return redirect(url_for("account_page"))
    if not row:
        return redirect(url_for("login"))
    key, email, old_enc = app.config["SECRET_KEY"], row["email"], row.get("totp_secret") or ""
    old_secret = TF.decrypt(old_enc, key)
    if not old_secret:
        flash("Tài khoản chưa thiết lập 2 bước. Hãy đăng xuất và đăng nhập lại để thiết lập.", "error")
        return redirect(url_for("account_page"))

    st = session.get("phone_change")
    new_secret = None
    if isinstance(st, dict) and time.time() - float(st.get("t", 0)) <= PHONE_CHANGE_SECONDS:
        new_secret = TF.decrypt(st.get("enc", ""), key)
    if not new_secret:
        session.pop("phone_change", None)

    if request.method == "POST":
        if not _csrf_ok():
            flash("Phiên làm việc đã hết hạn. Vui lòng thử lại.", "error")
            return redirect(url_for("account_change_phone"))
        act = request.form.get("action", "")
        if act == "cancel":
            session.pop("phone_change", None)
            return redirect(url_for("account_page"))
        if _otp_limiter.locked(email):
            flash("Nhập sai mã quá nhiều lần nên tạm khoá vài phút. Vui lòng thử lại sau.", "error")
            return redirect(url_for("account_page"))
        try:
            if act == "verify_old":
                step = TF.verify(old_secret, request.form.get("code"), row.get("totp_last_step") or 0)
                if step is not None and _admins().use_totp_step(email, step):
                    _otp_limiter.reset(email)
                    session["phone_change"] = {"enc": TF.encrypt(TF.new_secret(), key), "t": time.time()}
                    return redirect(url_for("account_change_phone"))
                _otp_limiter.fail(email)
                _log("login_fail", email=email[:120], detail="Đổi điện thoại: sai mã OTP hiện tại")
                flash("Mã không đúng hoặc đã hết hạn. Hãy nhập mã MỚI NHẤT trong ứng dụng trên điện thoại hiện tại.", "error")
            elif act == "confirm_new" and new_secret:
                step = TF.verify(new_secret, request.form.get("code"))
                if step is None:
                    _otp_limiter.fail(email)
                    flash("Mã không đúng. Hãy quét mã QR bằng điện thoại mới rồi nhập mã đang hiện trong ứng dụng.", "error")
                elif _admins().set_totp(email, TF.encrypt(new_secret, key), step, expect_old=old_enc):
                    session.pop("phone_change", None)
                    _otp_limiter.reset(email)
                    _log("otp_setup", email=email, detail="Đổi điện thoại (đang đăng nhập)")
                    flash("Đã đổi sang điện thoại mới. Từ lần đăng nhập sau hãy dùng mã trên điện thoại mới.", "info")
                    return redirect(url_for("account_page"))
                else:                                                # tài khoản vừa bị đổi ở nơi khác
                    session.pop("phone_change", None)
                    flash("Không lưu được vì tài khoản vừa được thay đổi ở nơi khác. Vui lòng thử lại.", "error")
                    return redirect(url_for("account_change_phone"))
        except (DB.NotConfigured, DB.DBError):
            _db_down("Chưa hoàn tất được do lỗi cơ sở dữ liệu. Vui lòng thử lại sau ít phút.")
        return redirect(url_for("account_change_phone"))             # sai mã / lỗi: quay lại đúng bước hiện tại (mã QR giữ nguyên trong phiên)
    return _render_phone_change(email, new_secret)


def _render_phone_change(email, new_secret):
    ctx = {"email": email, "step": 2 if new_secret else 1}
    if new_secret:
        uri = TF.provisioning_uri(new_secret, email, app.config.get("TWO_FACTOR_ISSUER", TF.DEFAULT_ISSUER))
        ctx.update(qr_svg=TF.qr_svg(uri), secret_text=TF.format_secret(new_secret), otp_uri=uri)
    return render_template("account/change_phone.html", **ctx)


# ── Đổi mật khẩu (đã đăng nhập) ───────────────────────────────────────────────────────────────────────────────
_pw_limiter = TF.FailLimiter(limit=5, window=900)            # 5 lần nhập sai mật khẩu hiện tại / 15 phút / email


def _account_row():
    """Hàng admin của người đang đăng nhập; lỗi DB → None (đã báo lỗi cho người dùng)."""
    try:
        return _admins().get_active(current_user.email)
    except (DB.NotConfigured, DB.DBError):
        _db_down()
        return None


@app.route("/tai-khoan/doi-mat-khau", methods=["GET", "POST"])
@login_required
def account_change_password():
    row = _account_row()
    if not row:
        return redirect(url_for("account_page"))
    email = row["email"]
    # Phải nhập mật khẩu hiện tại — TRỪ khi phiên này vào bằng Google / liên kết email (người quên mật khẩu) hoặc tài khoản chưa có mật khẩu
    need_old = bool(row.get("password_hash")) and not session.get("identity_proof")
    if request.method == "POST":
        if not _csrf_ok():
            flash("Phiên làm việc đã hết hạn. Vui lòng thử lại.", "error")
            return redirect(url_for("account_change_password"))
        if _pw_limiter.locked(email):
            flash("Nhập sai mật khẩu hiện tại quá nhiều lần nên tạm khoá vài phút. Vui lòng thử lại sau.", "error")
            return redirect(url_for("account_page"))
        old, pw1, pw2 = request.form.get("old_password", ""), request.form.get("password", ""), request.form.get("password2", "")
        try:
            if need_old and not _admins().verify(email, old):
                _pw_limiter.fail(email)
                _log("login_fail", email=email[:120], detail="Đổi mật khẩu: sai mật khẩu hiện tại")
                flash("Mật khẩu hiện tại không đúng.", "error")
            elif pw1 != pw2:
                flash("Hai lần nhập mật khẩu mới chưa giống nhau.", "error")
            elif need_old and pw1 == old:
                flash("Mật khẩu mới phải khác mật khẩu hiện tại.", "error")
            else:
                _admins().set_password(email, pw1)                      # ValueError nếu quá ngắn
                fresh = _admins().get_active(email)
                session["pwv"] = TK.fingerprint((fresh or {}).get("password_hash"))   # phiên này ở lại; mọi phiên khác bị đăng xuất
                _pw_limiter.reset(email)
                _log("password_change", email=email, detail="Đổi mật khẩu (đã đăng nhập)")
                _notify_security(email, "Mật khẩu tài khoản quản trị vừa được đổi — BVĐK Tâm Đức Cầu Quan",
                                 "Mật khẩu tài khoản quản trị của bạn vừa được đổi. Các thiết bị khác đã bị đăng xuất.")
                flash("Đã đổi mật khẩu. Các thiết bị khác đã được đăng xuất.", "info")
                return redirect(url_for("account_page"))
        except ValueError as e:
            flash(str(e), "error")
        except (DB.NotConfigured, DB.DBError):
            _db_down("Chưa đổi được mật khẩu do lỗi cơ sở dữ liệu. Vui lòng thử lại sau ít phút.")
        return redirect(url_for("account_change_password"))
    return render_template("account/change_password.html", need_old=need_old, has_pw=bool(row.get("password_hash")))


# ── Đổi email đăng nhập (đã đăng nhập): xác nhận bằng liên kết gửi tới EMAIL MỚI ─────────────────────────────────
@app.route("/tai-khoan/doi-email", methods=["GET", "POST"])
@login_required
def account_change_email():
    row = _account_row()
    if not row:
        return redirect(url_for("account_page"))
    email, key = row["email"], app.config["SECRET_KEY"]
    has_pw = bool(row.get("password_hash"))
    secret = TF.decrypt(row.get("totp_secret") or "", key) if _two_factor_on() else None
    if not _mail_enabled():
        return render_template("account/change_email.html", email=email, mail_off=True, has_pw=has_pw, need_otp=bool(secret))
    if request.method == "POST":
        if not _csrf_ok():
            flash("Phiên làm việc đã hết hạn. Vui lòng thử lại.", "error")
            return redirect(url_for("account_change_email"))
        if not (has_pw or secret):                                  # không có gì để chứng minh đúng chủ tài khoản
            flash("Tài khoản cần có mật khẩu hoặc xác thực 2 bước để đổi email.", "error")
            return redirect(url_for("account_page"))
        if _pw_limiter.locked(email) or _otp_limiter.locked(email):
            flash("Nhập sai quá nhiều lần nên tạm khoá vài phút. Vui lòng thử lại sau.", "error")
            return redirect(url_for("account_page"))
        try:
            new = ADM.check_email(request.form.get("new_email", ""))
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("account_change_email"))
        try:
            # Chứng minh đúng chủ: mật khẩu hiện tại (nếu có) VÀ mã OTP hiện tại (nếu bật 2 bước)
            if has_pw and not _admins().verify(email, request.form.get("password", "")):
                _pw_limiter.fail(email)
                _log("login_fail", email=email[:120], detail="Đổi email: sai mật khẩu")
                flash("Mật khẩu hiện tại không đúng.", "error")
                return redirect(url_for("account_change_email"))
            if secret:
                step = TF.verify(secret, request.form.get("code"), row.get("totp_last_step") or 0)
                if step is None or not _admins().use_totp_step(email, step):
                    _otp_limiter.fail(email)
                    _log("login_fail", email=email[:120], detail="Đổi email: sai mã OTP")
                    flash("Mã OTP không đúng hoặc đã dùng rồi. Hãy nhập mã MỚI NHẤT trong ứng dụng.", "error")
                    return redirect(url_for("account_change_email"))
            if new == email:
                flash("Email mới trùng với email hiện tại.", "error")
            elif _admins().get(new):
                flash("Email này không dùng được (đã gắn với một tài khoản khác).", "error")
            elif _reset_throttled("email", email):
                flash("Bạn đã yêu cầu quá nhiều lần. Vui lòng thử lại sau khoảng 1 giờ.", "error")
            else:
                token = TK.make(key, TK.EMAIL_CHANGE, email, f"{row.get('password_hash')}|{row.get('totp_secret')}", extra=new)
                _send_mail_async(new, "Xác nhận email đăng nhập mới — BVĐK Tâm Đức Cầu Quan",
                                 f"Tài khoản quản trị {email} vừa yêu cầu đổi email đăng nhập sang địa chỉ này.\n"
                                 f"Mở liên kết dưới đây trên trình duyệt đang đăng nhập tài khoản đó để xác nhận "
                                 f"(có hiệu lực {RESET_LINK_MINUTES} phút, chỉ dùng được 1 lần):\n\n"
                                 f"{_admin_link('account_confirm_email', token=token)}\n\n"
                                 "Nếu không phải bạn, hãy bỏ qua email này.\n")
                _log("reset_request", email=email, detail="Đổi email đăng nhập")
                flash(f"Đã gửi liên kết xác nhận tới {_mask_email(new)}. Mở email (hiệu lực {RESET_LINK_MINUTES} phút) rồi bấm liên kết.", "info")
                return redirect(url_for("account_page"))
        except (DB.NotConfigured, DB.DBError):
            _db_down("Chưa xử lý được do lỗi cơ sở dữ liệu. Vui lòng thử lại sau ít phút.")
        return redirect(url_for("account_change_email"))
    return render_template("account/change_email.html", email=email, mail_off=False, has_pw=has_pw, need_otp=bool(secret))


@app.route("/tai-khoan/xac-nhan-email/<token>", methods=["GET", "POST"])
@login_required
def account_confirm_email(token):
    """GET chỉ HIỂN THỊ nút xác nhận (trình quét thư/xem trước liên kết không vô tình đổi email); POST mới thực hiện."""
    d = TK.read_ex(app.config["SECRET_KEY"], TK.EMAIL_CHANGE, token)
    row = _account_row()
    if not row:
        return redirect(url_for("account_page"))
    new = d[2] if d else ""
    # liên kết phải do CHÍNH tài khoản đang đăng nhập yêu cầu, và chưa bị vô hiệu (đổi mật khẩu / điện thoại / email sau đó)
    if not (d and d[0] == row["email"] and new and TK.matches(d[1], f"{row.get('password_hash')}|{row.get('totp_secret')}")):
        flash("Liên kết không hợp lệ, đã hết hạn hoặc không thuộc tài khoản đang đăng nhập. Hãy yêu cầu đổi email lại.", "error")
        return redirect(url_for("account_page"))
    if request.method == "POST":
        if not _csrf_ok():
            flash("Phiên làm việc đã hết hạn. Vui lòng mở lại liên kết trong email.", "error")
            return redirect(url_for("account_confirm_email", token=token))
        old = row["email"]
        try:
            changed = _admins().change_email(old, new)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("account_page"))
        except (DB.NotConfigured, DB.DBError):
            _db_down("Chưa đổi được email do lỗi cơ sở dữ liệu. Vui lòng thử lại sau ít phút.")
            return redirect(url_for("account_confirm_email", token=token))
        if not changed:
            flash("Không đổi được: email này đã gắn với một tài khoản khác.", "error")
            return redirect(url_for("account_page"))
        _log("email_change", email=old, detail=f"{old} → {new}")
        _send_mail_async(old, "Email đăng nhập quản trị đã được đổi — BVĐK Tâm Đức Cầu Quan",
                         f"Email đăng nhập tài khoản quản trị {old} vừa được đổi sang {new}.\n"
                         "Nếu không phải bạn, hãy liên hệ quản trị viên ngay.\n")
        fresh = _admins().get_active(new) or row
        login_user(User(fresh["email"], fresh["name"] or new.split("@")[0], fresh.get("picture", "")), remember=False)   # id phiên theo email mới
        session["last_active"] = time.time()
        flash(f"Đã đổi email đăng nhập sang {new}. Từ nay đăng nhập bằng email mới.", "info")
        return redirect(url_for("account_page"))
    return render_template("account/confirm_email.html", old=row["email"], new=new, token=token)


@app.route("/logout")
@login_required
def logout():
    _log("logout", email=getattr(current_user, "email", ""))
    logout_user()
    session.pop("last_active", None)
    return redirect(url_for("login"))


# ════════════════════════════════════════════════════════════════════════════════
# DASHBOARD ROUTES
# ════════════════════════════════════════════════════════════════════════════════

def _get_df(fresh=False, max_age=None):
    """DataFrame của Sheet, có cache. `fresh=True`: luôn đọc lại Sheet. `max_age`: chỉ dùng bản cache nếu
    chưa quá N giây tuổi (dùng cho kiểm tra trùng khi import — không để dữ liệu cũ hàng phút)."""
    from services.sheets import load_dataframe
    df = cache.get("main_df")
    age = time.time() - (cache.get("main_df_ts") or 0)
    if fresh or df is None or (max_age is not None and age > max_age):
        df = load_dataframe()
        cache.set("main_df", df)
        cache.set("main_df_ts", time.time())
    return df


@app.route("/")
@login_required
def dashboard():
    from services.sheets import get_dashboard_stats
    df    = _get_df()
    stats = get_dashboard_stats(df)
    return render_template("dashboard/index.html", stats=stats)


def _report_args():
    """Đọc kỳ + khoảng ngày từ URL (dùng chung cho 3 route báo cáo)."""
    from services.sheets import PERIODS
    period = request.args.get("period", "month")
    if period not in PERIODS:
        period = "month"
    d_from = _parse_date(request.args.get("date_from", ""))
    d_to   = _parse_date(request.args.get("date_to", ""))
    if d_from and d_to and d_from > d_to:
        d_from, d_to = d_to, d_from
    return period, d_from, d_to


@app.route("/bao-cao")
@login_required
def report():
    from services.sheets import build_report, PERIODS
    period, d_from, d_to = _report_args()
    data = build_report(_get_df(), period, d_from, d_to)
    return render_template("dashboard/report.html", data=data, periods=PERIODS,
                           period=period,
                           date_from=d_from.isoformat() if d_from else "",
                           date_to=d_to.isoformat() if d_to else "")


def _fold_text(s) -> str:
    """Chữ thường, bỏ dấu tiếng Việt (đ → d): 'Nguyễn' khớp 'nguyen' — dùng cho ô tìm kiếm."""
    import unicodedata
    t = unicodedata.normalize("NFD", str(s or "").lower().replace("đ", "d"))
    return "".join(ch for ch in t if not unicodedata.combining(ch))


@app.route("/bao-cao/chi-tiet")
@login_required
def report_detail():
    """Danh sách bệnh nhân của 1 ô trong bảng báo cáo (bấm vào con số)."""
    from services.sheets import (report_drilldown, REPORT_KINDS, PERIODS, COL_STT, COL_NAME,
                                 COL_PHONE, COL_SOURCE, COL_STATUS, COL_KHOA, COL_EXAM_DATE)
    period, d_from, d_to = _report_args()
    key  = request.args.get("key", "all")
    kind = request.args.get("kind", "all")
    if kind not in REPORT_KINDS:
        kind = "all"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 20

    d = report_drilldown(_get_df(), period, key, kind, d_from, d_to)
    from services.sheets import COL_BIRTH_YEAR, COL_ACTUAL_DATE, to_dmy
    has_actual_col = COL_ACTUAL_DATE in d.columns
    ky_df = d                                          # giữ nguyên danh sách của ô báo cáo để lấy nhãn kỳ (trước khi tìm kiếm)

    # Tìm kiếm trong danh sách: tên, SĐT, STT, năm sinh, ngày hẹn / thực khám, khoa, nguồn (không phân biệt hoa-thường, có dấu hay không)
    q = request.args.get("q", "").strip()[:80]
    if q and len(d):
        want = _fold_text(q)

        def _hit(r):
            actual = to_dmy(r.get(COL_ACTUAL_DATE, "")) if has_actual_col else ""
            hay = " ".join(str(x or "") for x in (
                r.get(COL_STT, ""), r.get(COL_NAME, ""), r.get(COL_BIRTH_YEAR, ""), r.get(COL_PHONE, ""),
                r["_date"].strftime("%d/%m/%Y"), actual, r.get(COL_KHOA, ""), r.get(COL_SOURCE, "")))
            return want in _fold_text(hay)
        d = d[d.apply(_hit, axis=1)]

    total = len(d)
    page_df = d.iloc[(page - 1) * per_page: page * per_page] if total else d

    def _birth(v):
        t = str(v or "").strip()
        return t if t and t.upper() not in ("N/A", "NAN", "NONE") else "—"

    def _actual(r):
        """→ (chuỗi dd/mm/YYYY | '—', nhãn lệch so với ngày hẹn | '')"""
        a = to_dmy(r.get(COL_ACTUAL_DATE, "")) if has_actual_col else ""
        if not a:
            return "—", ""
        try:
            diff = (datetime.strptime(a, "%d/%m/%Y") - r["_date"].to_pydatetime().replace(tzinfo=None)).days
        except Exception:
            return a, ""
        return a, ("đúng hẹn" if diff == 0 else (f"sớm {-diff} ngày" if diff < 0 else f"muộn {diff} ngày"))

    records = []
    for _, r in page_df.iterrows():
        actual, delta = _actual(r)
        records.append({
            "stt":    str(r.get(COL_STT, "") or "—"),
            "name":   str(r.get(COL_NAME, "") or "—"),
            "birth":  _birth(r.get(COL_BIRTH_YEAR, "")),
            "phone":  str(r.get(COL_PHONE, "") or "—"),
            "date":   r["_date"].strftime("%d/%m/%Y"),          # ngày hẹn (cột NGÀY KHÁM trên Sheet)
            "actual": actual, "delta": delta,
            "khoa":   str(r.get(COL_KHOA, "") or "—"),
            "source": str(r.get(COL_SOURCE, "") or "—"),
            "att":    bool(r["_att"]),
        })

    ky = "Toàn bộ"
    if key != "all" and len(ky_df):
        from services.sheets import _period_label
        ky = _period_label(ky_df["_start"].iloc[0], period)

    return render_template("dashboard/report_detail.html",
                           records=records, total=total, page=page, per_page=per_page, q=q,
                           kind=kind, kind_label=REPORT_KINDS[kind], key=key, ky=ky,
                           has_actual_col=has_actual_col,
                           period=period, period_name=PERIODS[period],
                           date_from=d_from.isoformat() if d_from else "",
                           date_to=d_to.isoformat() if d_to else "")


@app.route("/bao-cao/csv")
@login_required
def report_csv():
    from flask import Response
    import csv, io
    from services.sheets import build_report
    period, d_from, d_to = _report_args()
    data = build_report(_get_df(), period, d_from, d_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([data["period_name"], "Tổng đăng ký", "Tái khám", "Tái khám - Đã khám", "Tái khám - Không đến",
                "Khám ưu tiên", "Ưu tiên - Đã khám", "Ưu tiên - Không đến",
                "Tổng đã khám", "Tổng không đến", "% đến", "% không đến"])
    for r in data["rows"]:
        w.writerow([r["ky"], r["total"], r["tk_total"], r["tk_att"], r["tk_abs"],
                    r["ut_total"], r["ut_att"], r["ut_abs"], r["att"], r["abs"],
                    r["pct_att"], r["pct_abs"]])
    fname = f"baocao_{period}_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


def _parse_date(value):
    """'2026-10-05' (từ <input type=date>) → date, sai định dạng → None."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


@app.route("/benh-nhan")
@login_required
def patients():
    from services.sheets import COL_STATUS, DASH_SCOPES, filter_by_date_range, filter_by_scope, status_key
    df       = _get_df()
    scope    = request.args.get("scope", "")
    if scope not in DASH_SCOPES:
        scope = ""
    q        = request.args.get("q", "").strip()
    status   = request.args.get("status", "")
    d_from   = _parse_date(request.args.get("date_from", ""))
    d_to     = _parse_date(request.args.get("date_to", ""))
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 20

    if d_from and d_to and d_from > d_to:          # người dùng chọn ngược → tự đổi chỗ
        d_from, d_to = d_to, d_from

    filtered = filter_by_date_range(filter_by_scope(df.copy(), scope), d_from, d_to)
    if q:
        mask = filtered.apply(lambda row: q.lower() in " ".join(str(v) for v in row).lower(), axis=1)
        filtered = filtered[mask]
    if status and COL_STATUS in filtered.columns:        # so sánh theo status_key: "CHƯA KHÁM/BỎ KHÁM" và "CHƯA KHÁM / BỎ KHÁM" là MỘT
        want = status_key(status)
        filtered = filtered[filtered[COL_STATUS].map(status_key) == want]

    total   = len(filtered)
    start   = (page - 1) * per_page
    records = filtered.iloc[start:start + per_page].to_dict("records")
    from services.sheets import created_fields
    for r in records:                                  # ngày/giờ tạo hẹn: trống thì lấy từ Dấu thời gian
        r["_created_date"], r["_created_time"] = created_fields(r)

    return render_template("dashboard/patients.html",
                           records=records, total=total,
                           page=page, per_page=per_page,
                           q=q, status=status,
                           scope=scope, scope_label=DASH_SCOPES.get(scope, ""),
                           date_from=d_from.isoformat() if d_from else "",
                           date_to=d_to.isoformat() if d_to else "")


@app.route("/lich-kham")
@login_required
def upcoming():
    """Bệnh nhân sẽ đến khám trong 3 ngày tới, chia tab theo khoa."""
    from services.sheets import get_upcoming_patients
    data = get_upcoming_patients(_get_df(), days=3)
    return render_template("dashboard/upcoming.html", data=data)


# ════════════════════════════════════════════════════════════════════════════════
# IMPORT LỊCH HẸN TÁI KHÁM (file Minh Lộ 04-4)
#   /import          → upload file
#   /import/preview  → chọn Ngày lập · cảnh báo trùng · xem trước · import
#   /import/confirm  → ghi vào Google Sheet
#   /import/csv      → tải CSV các BN mới
# Dữ liệu đã đọc được giữ trên server (cache) theo token trong session — KHÔNG nhét
# vào cookie (cookie tối đa ~4KB, không chứa nổi danh sách BN).
# ════════════════════════════════════════════════════════════════════════════════
_import_lock = threading.Lock()    # chặn 2 cú click cùng ghi 1 lúc (trong 1 worker)


def _import_data():
    from services.importer import load_import
    return load_import(session.get("import_token", ""))


_IMPORT_SNAPSHOT_MAX_AGE = 20     # giây: bản Sheet dùng để kiểm tra trùng ở trang xem trước không được cũ hơn mức này


def _import_context(selected_arg: list[str] | None, has_sel: bool, fresh: bool = False):
    """Tính toàn bộ số liệu cho trang preview / confirm / csv. Trả None nếu phiên hết hạn.
    `fresh=True` (lúc ghi): đọc lại Sheet ngay, không dùng cache.

    Kiểm tra trùng chạy trên TOÀN BỘ file (mỗi bản ghi xét độc lập) rồi mới cắt theo ngày lập đã chọn, nhờ vậy mỗi ngày lập
    đều có số liệu "đã nhập X/Y" để hiện ngay trên nút chọn — kể cả khi chưa tích chọn."""
    from services.importer import group_by_lap, filter_by_lap, lap_key, lap_progress, UNKNOWN_LAP
    from services.sheets import (check_sheet_duplicates, drop_file_duplicates, sheet_snapshot_stats,
                                 sheet_import_diagnostics, vn_today, fmt_vn_time)

    data = _import_data()
    if not data:
        return None
    records = data["records"]
    order, counts = group_by_lap(records)

    today_str = vn_today().strftime("%d/%m/%Y")
    if has_sel:
        selected = {d for d in (selected_arg or []) if d in counts}
    else:                                           # lần đầu vào: mặc định chọn ngày lập HÔM NAY
        selected = {today_str} if today_str in counts else set()

    filtered = filter_by_lap(records, selected)

    df = _get_df(fresh=True) if fresh else _get_df(max_age=_IMPORT_SNAPSHOT_MAX_AGE)
    all_new, all_dup = check_sheet_duplicates(df, records)
    progress = lap_progress(records, all_dup)
    new_sel = [r for r in all_new if lap_key(r) in selected]
    dup_records = [r for r in all_dup if lap_key(r) in selected]
    new_records, file_dups = drop_file_duplicates(new_sel)
    stats = sheet_snapshot_stats(df)
    diag = sheet_import_diagnostics(df)

    # Dòng cũ (nhập từ trước khi có cột NGÀY TẠO LỊCH HẸN KHÁM) khớp DUY NHẤT 1 BN trong file → có thể điền bổ sung ngày tạo
    backfill = [{"sheet_row": r["_existing_row"], "stt": r.get("_existing_stt", ""), "name": r.get("HỌ TÊN", ""),
                 "lap": r.get("NGÀY LẬP", "")}
                for r in dup_records if r.get("_backfill") and r.get("_existing_row")] if diag["has_created_col"] else []

    return {
        "filename": data["filename"], "records": records, "total": len(records),
        "order": order, "counts": counts, "selected": selected, "progress": progress,
        "today_str": today_str, "UNKNOWN_LAP": UNKNOWN_LAP,
        "filtered": filtered, "new": new_records, "dup": dup_records, "file_dup": file_dups,
        "backfill": backfill, "diag": diag,
        "sheet_rows": stats["rows"], "sheet_valid": stats["valid"],
        "sheet_read_at": fmt_vn_time(cache.get("main_df_ts")),
        # Sheet có dữ liệu nhưng không dòng nào đọc được tên+ngày → không thể kiểm tra trùng → KHÔNG cho ghi
        "blind": stats["rows"] > 0 and stats["valid"] == 0,
    }


@app.errorhandler(413)
def too_large(_e):
    flash("File quá lớn (tối đa 10MB). Hãy xuất lại file 04-4 với khoảng ngày ngắn hơn.", "error")
    return redirect(url_for("import_excel"))


@app.route("/import", methods=["GET", "POST"])
@login_required
def import_excel():
    if request.method == "POST":
        f = request.files.get("excel_file")
        if not f or not f.filename.lower().endswith(".xlsx"):
            flash("Vui lòng chọn file .xlsx hợp lệ.", "error")
            return redirect(url_for("import_excel"))

        from services.excel_parser import parse_minh_lo_excel
        from services.sheets import vn_today
        records, err = parse_minh_lo_excel(f.read(), current_year=vn_today().year)
        if err:
            flash(f"❌ {err}", "error")
            return redirect(url_for("import_excel"))
        if not records:
            flash("⚠️ Không tìm thấy dữ liệu bệnh nhân trong file. Kiểm tra lại định dạng file.", "warning")
            return redirect(url_for("import_excel"))

        from services.importer import save_import, delete_import
        delete_import(session.get("import_token", ""))
        token = uuid.uuid4().hex
        save_import(token, records, f.filename)
        session["import_token"] = token
        cache.delete("main_df")        # bắt đầu phiên import bằng dữ liệu Sheet mới nhất
        return redirect(url_for("import_preview_page"))

    return render_template("dashboard/import.html", has_pending=_import_data() is not None)


@app.route("/import/preview")
@login_required
def import_preview_page():
    ctx = _import_context(request.args.getlist("lap"), "sel" in request.args)
    if ctx is None:
        flash("Phiên import đã hết hạn hoặc chưa upload file — vui lòng chọn lại file.", "info")
        return redirect(url_for("import_excel"))

    show_all = request.args.get("all") == "1"
    lim = 500 if show_all else 10
    sel = sorted(ctx["selected"])

    def url_with(laps, **extra):
        return url_for("import_preview_page", sel=1, lap=laps, **extra)

    return render_template(
        "dashboard/import_preview.html", c=ctx, show_all=show_all,
        preview_rows=ctx["new"][:lim],
        n_dates=len({r["NGÀY HẸN"] for r in ctx["new"] if r.get("NGÀY HẸN")}),
        url_all=url_with(ctx["order"]),
        url_today=url_with([ctx["today_str"]] if ctx["today_str"] in ctx["counts"] else []),
        url_none=url_with([]),
        url_full=url_with(sel, all=1), url_short=url_with(sel),
        csv_url=url_for("import_csv", lap=sel),
    )


@app.route("/import/csv")
@login_required
def import_csv():
    import csv, io
    from flask import Response
    from services.sheets import vn_now
    ctx = _import_context(request.args.getlist("lap"), True)
    if ctx is None:
        flash("Phiên import đã hết hạn — vui lòng upload lại file.", "info")
        return redirect(url_for("import_excel"))
    buf = io.StringIO()
    if ctx["new"]:
        cols = [k for k in ctx["new"][0].keys() if not k.startswith("_")]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(ctx["new"])
    fname = f"henkham_minhlo_{vn_now().strftime('%Y%m%d_%H%M')}.csv"
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.route("/import/confirm", methods=["POST"])
@login_required
def import_confirm():
    """Ghi các BN mới vào Google Sheet. Trả JSON (gửi nền, có pop-up) hoặc redirect (form thường)."""
    from services.sheets import append_patients
    from services.importer import delete_import
    laps = request.form.getlist("lap")
    back = url_for("import_preview_page", sel=1, lap=laps)
    home = url_for("import_excel")
    try:
        with _locked(_import_lock) as got:
            if not got:
                return _json_or_flash(False, "Đang có một lần import khác chạy — hãy đợi vài giây rồi bấm lại.",
                                      "warning", back)
            # Đọc lại Sheet MỚI NHẤT rồi kiểm tra trùng lần nữa ngay trước khi ghi: chặn bấm 2 lần / refresh /
            # người khác vừa nhập cùng lịch / bấm lại sau khi lần trước bị timeout nhưng thực ra đã ghi xong.
            ctx = _import_context(laps, True, fresh=True)
            if ctx is None:
                return _json_or_flash(False, "Phiên import đã hết hạn — hãy upload lại file.", "info", home,
                                      redirect=home)
            new, dup = ctx["new"], ctx["dup"]
            if not ctx["selected"]:
                return _json_or_flash(False, "Chưa chọn ngày lập nào — hãy tích chọn ít nhất 1 ngày.", "warning", back)
            if ctx["blind"]:
                return _json_or_flash(False, f"Không kiểm tra được trùng: đọc được {ctx['sheet_rows']} dòng trên Sheet "
                                      "nhưng không dòng nào có Họ tên + Ngày khám hợp lệ. Để tránh nhập trùng, hệ thống "
                                      "không ghi. Hãy kiểm tra tên cột NGÀY KHÁM / HỌ VÀ TÊN trên Sheet.", "error", back)
            if not new:
                return _json_or_flash(True, f"{len(dup)} bệnh nhân trong các ngày lập đã chọn đã có sẵn trên Google Sheet "
                                      "— không ghi lại. (Nếu lần bấm trước bị gián đoạn thì dữ liệu đã được ghi.)",
                                      "success", back, count=0, already=len(dup))
            n, err = append_patients(new)
        cache.delete("main_df")
        if err:
            return _json_or_flash(False, err, "error", back)
        delete_import(session.pop("import_token", ""))      # xong → bỏ file, tránh import lại
        msg = f"Đã thêm {n} dòng mới vào Google Sheet (theo {len(ctx['selected'])} ngày lập đã chọn)."
        if dup:
            msg += f" Đã tự động bỏ qua {len(dup)} bệnh nhân đã có sẵn."
        if ctx["file_dup"]:
            msg += f" Bỏ qua {len(ctx['file_dup'])} dòng trùng ngay trong file."
        if not ctx["diag"]["has_created_col"]:
            msg += (" ⚠️ Sheet chưa có cột \"NGÀY TẠO LỊCH HẸN KHÁM\" nên chưa ghi được ngày tạo lịch hẹn — "
                    "hãy thêm cột này (đúng tên) vào dòng tiêu đề.")
        names = [r["HỌ TÊN"] for r in new]
        return _json_or_flash(True, msg, "success", home, count=n, already=len(dup),
                              names=names[:12], more=max(0, len(names) - 12), redirect=home)
    except Exception as e:
        app.logger.exception("import_confirm")
        return _json_or_flash(False, f"Lỗi máy chủ: {type(e).__name__}: {e}", "error", back)


@app.route("/import/backfill", methods=["POST"])
@login_required
def import_backfill():
    """Điền NGÀY TẠO LỊCH HẸN KHÁM cho các dòng đã import từ trước khi có cột này (chỉ ô trống, không đè dữ liệu)."""
    from services.sheets import backfill_created
    laps = request.form.getlist("lap")
    back = url_for("import_preview_page", sel=1, lap=laps)
    try:
        with _locked(_import_lock) as got:
            if not got:
                return _json_or_flash(False, "Đang có một lần import khác chạy — hãy đợi vài giây rồi bấm lại.",
                                      "warning", back)
            ctx = _import_context(laps, True, fresh=True)          # đọc lại Sheet mới nhất rồi mới quyết định điền dòng nào
            if ctx is None:
                return _json_or_flash(False, "Phiên import đã hết hạn — hãy upload lại file.", "info",
                                      url_for("import_excel"), redirect=url_for("import_excel"))
            if not ctx["diag"]["has_created_col"]:
                return _json_or_flash(False, "Sheet chưa có cột \"NGÀY TẠO LỊCH HẸN KHÁM\" — hãy thêm cột này (đúng tên) "
                                      "vào dòng tiêu đề rồi thử lại.", "warning", back)
            items = ctx["backfill"]
            if not items:
                return _json_or_flash(True, "Không còn dòng cũ nào cần điền ngày tạo lịch hẹn.", "success", back,
                                      count=0, already=0)
            n, err = backfill_created(items)
        cache.delete("main_df")
        if err and not n:
            return _json_or_flash(False, err, "error", back)
        msg = f"Đã điền NGÀY TẠO LỊCH HẸN KHÁM cho {n} dòng đã có sẵn trên Sheet."
        if err:
            msg += f" ⚠️ {err}"
        return _json_or_flash(True, msg, "warning" if err else "success", back, count=n,
                              names=[i["name"] for i in items[:12]], more=max(0, len(items) - 12))
    except Exception as e:
        app.logger.exception("import_backfill")
        return _json_or_flash(False, f"Lỗi máy chủ: {type(e).__name__}: {e}", "error", back)


# ════════════════════════════════════════════════════════════════════════════════
# ĐĂNG KÝ KHÁM ONLINE — trang CÔNG KHAI cho bệnh nhân tự điền (không cần đăng nhập)
# ════════════════════════════════════════════════════════════════════════════════

_reg_lock = threading.Lock()          # ghi đăng ký tuần tự (chống bấm đúp)


def _reg_no_store(resp, indexable=False):
    resp.headers["Cache-Control"] = "no-store"          # có thông tin cá nhân → không để trình duyệt/proxy lưu
    if not indexable:
        resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def _reg_render(values=None, errors=None, banner="", status=200):
    from services import registration as RG
    now = RG.vn_now()
    today = now.date()
    ctx = {
        "v": values or {}, "errors": errors or {}, "banner": banner,
        "csrf": session.get("reg_csrf", ""),
        "specialties": RG.SPECIALTIES, "OTHER": RG.OTHER,
        "slots_morning": RG.SLOTS_MORNING, "slots_afternoon": RG.SLOTS_AFTERNOON,
        "today": today.isoformat(), "max_date": (today + timedelta(days=RG.MAX_DAYS_AHEAD)).isoformat(),
        "now_hm": now.strftime("%H:%M"), "max_days": RG.MAX_DAYS_AHEAD,
    }
    # Chỉ cho Google lập chỉ mục khi là trang form trống (GET, 200). Form có lỗi / sau POST thì noindex.
    indexable = request.method == "GET" and status == 200 and not errors
    ctx["indexable"] = indexable
    ctx["canonical"] = url_for("register_page", _external=True)
    return _reg_no_store(app.make_response((render_template("register/form.html", **ctx), status)), indexable)


def _reg_ip() -> str:
    return (request.access_route[0] if request.access_route else request.remote_addr) or "?"


@app.route("/dang-ky", methods=["GET", "POST"])
def register_page():
    from services import registration as RG
    if not app.config.get("REG_ENABLED", True):
        return _reg_no_store(app.make_response((render_template("register/closed.html"), 503)))

    if request.method == "GET":
        session.update(RG.new_form_state())
        session.pop("reg_done", None)
        return _reg_render()

    # ── POST ──
    values = {k: v for k, v in request.form.items() if k not in ("csrf", "website")}
    bad = RG.check_state(session, request.form)
    if bad and session.get("reg_done"):                               # bấm đúp: lượt đầu đã ghi xong → cho xem trang kết quả
        return redirect(url_for("register_done"))
    if bad:
        if "hết hạn" in bad or "quá lâu" in bad:
            session.update(RG.new_form_state())                       # cấp phiên mới, GIỮ nguyên những gì đã điền
        return _reg_render(values, {}, bad, 400)
    clean, errors = RG.validate(request.form)
    if errors:                                                        # lỗi nhập liệu KHÔNG tính vào giới hạn (nhiều bệnh nhân dùng chung wifi/máy ở bệnh viện)
        return _reg_render(values, errors, f"Có {len(errors)} mục cần kiểm tra lại (hiện chữ đỏ bên dưới).", 422)
    if RG.rate_limited(_reg_ip()):                                    # chỉ đơn HỢP LỆ mới đụng tới Sheet → giới hạn ở đây
        return _reg_render(values, {}, "Hệ thống đang nhận quá nhiều đăng ký từ thiết bị này. Vui lòng đợi vài phút rồi thử lại.", 429)

    summary = {
        "name": clean["full_name"], "birth_year": clean["birth_year"], "phone": clean["phone"],
        "exam_date": clean["exam_date"].strftime("%d/%m/%Y"), "exam_time": clean["exam_time"],
        "specialty": next((lbl for v, lbl in RG.SPECIALTIES if v == clean["specialty"]), clean["specialty"]),
        "already": False,
    }

    with _locked(_reg_lock) as got:                                   # tuần tự hoá: 2 lần bấm gần nhau không ghi đôi
        if not got:
            return _reg_render(values, {}, "Hệ thống đang bận. Vui lòng đợi vài giây rồi bấm Gửi lại.", 503)
        already = RG.recently_submitted(clean)
        if not already:
            try:
                already = RG.already_on_sheet(_get_df(fresh=True), clean)    # đã có trên Sheet (kể cả Google Form cũ)
            except Exception as e:                                    # không đọc được Sheet → vẫn cho đăng ký, tránh chặn oan
                app.logger.warning(f"register: không kiểm tra trùng được: {e}")
        if already:
            summary["already"] = True
        else:
            ok, err = RG.submit(clean)
            if not ok:
                app.logger.error(f"register: ghi Sheet lỗi: {err}")
                return _reg_render(values, {}, "Hệ thống đang bận, chưa ghi được đăng ký. Vui lòng bấm Gửi lại sau ít phút. "
                                   "Nếu vẫn lỗi, xin gọi điện cho bệnh viện.", 503)
            RG.remember_submitted(clean)
            cache.delete("main_df")

    session["reg_done"] = summary
    session.update(RG.new_form_state())
    return redirect(url_for("register_done"))                          # PRG: tải lại trang không gửi lại đơn


@app.route("/favicon.ico")
def favicon():
    """Google/trình duyệt/bot hay thử /favicon.ico ở gốc tên miền. Phục vụ file thật (công khai, không cần đăng nhập)."""
    resp = send_from_directory(app.static_folder, "favicon.ico", mimetype="image/x-icon")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/robots.txt")
def robots_txt():
    is_patient = (not _PATIENT_HOSTS) or _norm_host(request.host) in _PATIENT_HOSTS
    if is_patient:
        body = ("User-agent: *\nAllow: /dang-ky\nDisallow: /dang-ky/xong\n"
                f"Sitemap: {url_for('sitemap_xml', _external=True)}\n")
    else:
        body = "User-agent: *\nDisallow: /\n"             # tên miền admin: cấm bot
    return app.response_class(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    loc = url_for("register_page", _external=True)
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'<url><loc>{loc}</loc></url></urlset>')
    return app.response_class(body, mimetype="application/xml")


@app.after_request
def _admin_host_noindex(resp):
    """Tên miền không phải của bệnh nhân (admin) → luôn noindex."""
    if _PATIENT_HOSTS and _norm_host(request.host) not in _PATIENT_HOSTS:
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.route("/dang-ky/xong")
def register_done():
    summary = session.get("reg_done")
    if not summary:
        return redirect(url_for("register_page"))
    return _reg_no_store(app.make_response(render_template("register/done.html", s=summary)))


# ── API: cập nhật trạng thái bệnh nhân (từ bảng danh sách) ──────────────────
@app.route("/api/status", methods=["POST"])
@login_required
def api_update_status():
    from services.sheets import update_status
    data = request.get_json(silent=True) or {}
    try:
        row_number = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Thiếu hoặc sai số dòng."}), 400
    new_status = str(data.get("status") or "").strip()
    if not new_status:
        return jsonify({"ok": False, "message": "Thiếu trạng thái."}), 400
    ok = update_status(row_number, new_status)
    cache.delete("main_df")
    return jsonify({"ok": ok})


# ════════════════════════════════════════════════════════════════════════════════
# ĐỐI CHIẾU TÁI KHÁM + LỌC TRÙNG  (port từ tab "Đối Chiếu Tái Khám" của Streamlit)
#   /doi-chieu               → trang chính (upload file 01-1 · kết quả · quét trùng)
#   /doi-chieu/upload        → đọc file nhật ký khám
#   /doi-chieu/chay          → chạy đối chiếu
#   /doi-chieu/cap-nhat      → Bước 2: ghi "Đã khám" cho các ca khớp chắc chắn đã chọn
#   /doi-chieu/xu-ly         → Bước 3/4: sửa SĐT/năm sinh hoặc xác nhận từng ca
#   /doi-chieu/csv           → tải báo cáo
#   /doi-chieu/trung/...     → quét & xoá dòng Form trùng với dòng tái khám
# Kết quả giữ trên server (đĩa) theo token trong session, không nhét vào cookie.
# ════════════════════════════════════════════════════════════════════════════════
_rec_lock = threading.Lock()


class _locked:
    """Khoá có thời hạn: nếu thao tác ghi khác đang chạy quá lâu thì báo lại ngay thay vì treo request."""
    def __init__(self, lock, timeout=25):
        self.lock, self.timeout, self.ok = lock, timeout, False

    def __enter__(self):
        self.ok = self.lock.acquire(timeout=self.timeout)
        return self.ok

    def __exit__(self, *exc):
        if self.ok:
            self.lock.release()


def _wants_json():
    return request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json"


def _json_or_flash(ok, message, level, fallback_url, **extra):
    """Trả JSON cho thao tác gửi nền (fetch) hoặc flash + redirect cho form thường / trình duyệt tắt JS."""
    level = level or ("success" if ok else "error")
    if _wants_json():
        resp = jsonify({"ok": bool(ok), "level": level, "message": message, **extra})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    probs = extra.get("problems") or []
    flash(message + ("  " + " · ".join(probs[:5]) if probs else ""), level)
    return redirect(fallback_url)


def _rec_reply(ok, message, level=None, tab="tk", anchor="", **extra):
    return _json_or_flash(ok, message, level, url_for("reconcile_page", tab=tab, _anchor=anchor or None), **extra)


def _rec_token(create=False):
    tok = session.get("rec_token", "")
    if not tok and create:
        tok = uuid.uuid4().hex
        session["rec_token"] = tok
    return tok


@app.template_filter("dmy")
def _dmy_filter(v):
    """'2026-10-05' → '05/10/2026' (kết quả đối chiếu lưu ngày dạng ISO)."""
    s = str(v or "")
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"
    return s or "—"


def _rec_back(tab="tk", anchor=""):
    return redirect(url_for("reconcile_page", tab=tab, _anchor=anchor or None))


def _actual_date_of(entry) -> str:
    """Ngày THỰC KHÁM (dd/mm/YYYY) của lượt khám đã khớp trong file nhật ký HIS; không có → ''."""
    from services.sheets import to_dmy
    nm = entry.get("near_miss") if entry.get("status") == "not_attended" else None
    nm = nm or {}
    for cand in (entry.get("visit_date"), nm.get("visit_date"),
                 (entry.get("visit") or {}).get("NGÀY ĐK"), (nm.get("visit") or {}).get("NGÀY ĐK")):
        d = to_dmy(cand)
        if d:
            return d
    return ""


def _mark_attended(entries):
    """Ghi 'Đã khám' + NGÀY THỰC KHÁM (nếu Sheet có cột này) và gán nguồn vãng lai nếu nguồn đang trống.
    Từng người được xử lý ĐỘC LẬP: 1 người lỗi (dòng đã đổi chỗ, đọc lại chưa đúng…) KHÔNG làm hỏng cả lô.
    Trả (số người đã ghi, danh sách lỗi từng người ['Tên: lý do'], lỗi chung | None)."""
    from services.sheets import write_cells_ex, STATUS_ATTENDED, COL_STATUS, COL_SOURCE, COL_ACTUAL_DATE
    from services.reconcile import blank_source, SOURCE_VANG_LAI
    ups = []
    for r in entries:
        vals = {COL_STATUS: STATUS_ATTENDED}
        actual = _actual_date_of(r)
        if actual:
            vals[COL_ACTUAL_DATE] = actual          # cột chưa có trên Sheet thì write_cells tự bỏ qua
        if blank_source(r.get("source")):
            vals[COL_SOURCE] = SOURCE_VANG_LAI
        ups.append({"sheet_row": r["sheet_row"], "stt": r.get("stt"), "name": r.get("name"), "values": vals})
    # RAW: ngày ghi dạng chữ dd/mm/yyyy, không bị Sheets hiểu nhầm tháng/ngày
    done, errors, fatal = write_cells_ex(ups, raw=True)
    if fatal:
        return 0, [], fatal
    for i in done:
        r = entries[i]
        r["done"] = True
        r["status_now"] = STATUS_ATTENDED
        if blank_source(r.get("source")):
            r["source"] = SOURCE_VANG_LAI
    if done:
        cache.delete("main_df")
    problems = [f"{entries[i].get('name', '?')}: {errors[i]}" for i in sorted(errors)]
    return len(done), problems, None


def _mark_result(n, problems, fatal, names_done, tab, anchor, already=0):
    """Gom kết quả _mark_attended thành 1 phản hồi: lỗi chung · một phần · thành công."""
    if fatal:
        return _rec_reply(False, fatal, "error", tab, anchor)
    if problems:                                        # chi tiết từng ca chỉ ghi log, không hiện trên giao diện
        app.logger.warning("cập nhật trạng thái: %d ca lỗi: %s", len(problems), "; ".join(problems[:12]))
    if not n and problems:
        return _rec_reply(False, "Cập nhật thất bại. Vui lòng thử lại.",
                          "error", tab, anchor, problems=problems[:12], more=max(0, len(problems) - 12))
    msg = "Cập nhật trạng thái thành công."
    if problems:
        msg = "Cập nhật chưa hoàn tất: một số bệnh nhân chưa được cập nhật."
    return _rec_reply(True, msg, "warning" if problems else "success", tab, anchor, count=n,
                      names=names_done[:12], more=max(0, len(names_done) - 12),
                      problems=problems[:12], more_problems=max(0, len(problems) - 12))


@app.route("/doi-chieu")
@login_required
def reconcile_page():
    from services.reconcile import (store_load, build_reconcile_scope, build_dup_lists,
                                    patient_kind, RECONCILE_LOOKBACK_DAYS, RECONCILE_WINDOW_BEFORE,
                                    RECONCILE_WINDOW_AFTER, DUPLICATE_WINDOW_DAYS)
    from services.sheets import vn_today
    tok   = _rec_token()
    visit = store_load(tok, "visit")
    rec   = store_load(tok, "rec")
    dup   = store_load(tok, "dup")
    today = vn_today()
    df    = _get_df()

    tab = request.args.get("tab", "tk")
    tab = tab if tab in ("tk", "vl") else "tk"
    flt = request.args.get("f", "all")
    flt = flt if flt in ("all", "attended_sure", "attended_unsure", "not_attended") else "all"

    scope_n = scope_cnt = None
    if visit:
        pts, scope_cnt = build_reconcile_scope(df, today)
        scope_n = len(pts)

    groups = {"tk": [], "vl": []}
    g = {}
    if rec:
        for r in rec["results"]:
            groups["tk" if patient_kind(r.get("source")) == "tai_kham" else "vl"].append(r)
        rows = groups[tab]
        sure   = [r for r in rows if r["status"] == "attended_sure"]
        unsure = [r for r in rows if r["status"] == "attended_unsure"]
        notatt = [r for r in rows if r["status"] == "not_attended"]
        g = {
            "rows": rows, "shown": rows if flt == "all" else [r for r in rows if r["status"] == flt],
            "n_sure": len(sure), "n_unsure": len(unsure), "n_not": len(notatt),
            "to_update": [r for r in sure if not r.get("done")],
            "need_review": [r for r in unsure if not r.get("done")],
            # Bước 4 (nghi bị sót) CHỈ dành cho nhóm vãng lai
            "sot": [r for r in notatt if r.get("near_miss") and not r.get("done")] if tab == "vl" else [],
        }

    legacy = build_dup_lists(df, today)[2] if df is not None and not df.empty else []
    return render_template(
        "dashboard/reconcile.html", visit=visit, rec=rec, g=g, tab=tab, flt=flt,
        n_tk=len(groups["tk"]), n_vl=len(groups["vl"]),
        scope_n=scope_n, scope_cnt=scope_cnt, dup=dup, legacy=legacy, today=today,
        LOOKBACK=RECONCILE_LOOKBACK_DAYS, WB=RECONCILE_WINDOW_BEFORE, WA=RECONCILE_WINDOW_AFTER,
        DUPWIN=DUPLICATE_WINDOW_DAYS,
        range_start=today - timedelta(days=RECONCILE_LOOKBACK_DAYS),
    )


@app.route("/doi-chieu/upload", methods=["POST"])
@login_required
def reconcile_upload():
    from services.excel_parser import parse_minh_lo_visit_log
    from services.reconcile import store_save, store_delete, parse_dmy
    f = request.files.get("visit_file")
    if not f or not f.filename.lower().endswith(".xlsx"):
        flash("Vui lòng chọn file .xlsx (Báo cáo ĐK KCB — file 01-1).", "error")
        return _rec_back()
    records, err, warn = parse_minh_lo_visit_log(f.read())
    if err:
        flash(f"❌ {err}", "error")
        return _rec_back()
    if not records:
        flash("⚠️ Không tìm thấy dữ liệu trong file. Kiểm tra đúng loại báo cáo \"ĐK KCB\".", "warning")
        return _rec_back()
    ds = [d for d in (parse_dmy(v["NGÀY ĐK"]) for v in records) if d]
    tok = _rec_token(create=True)
    store_delete(tok, "rec")                       # file mới → kết quả cũ không còn đúng
    store_save(tok, "visit", {
        "filename": f.filename, "records": records, "warn": warn,
        "vmin": min(ds).strftime("%d/%m/%Y") if ds else "?",
        "vmax": max(ds).strftime("%d/%m/%Y") if ds else "?",
    })
    cache.delete("main_df")
    if warn:
        flash(warn, "warning")
    return _rec_back(anchor="buoc1")


@app.route("/doi-chieu/chay", methods=["POST"])
@login_required
def reconcile_run():
    from services.reconcile import (store_load, store_save, build_reconcile_scope, reconcile_attendance,
                                    blank_source, SOURCE_VANG_LAI)
    from services.sheets import vn_today, vn_now, write_cells_ex, STATUS_NOT_ATTENDED, COL_STATUS, COL_SOURCE
    tok = _rec_token()
    visit = store_load(tok, "visit")
    if not visit:
        flash("Phiên đối chiếu đã hết hạn — vui lòng upload lại file.", "info")
        return _rec_back()
    with _rec_lock:
        cache.delete("main_df")
        today = vn_today()
        pts, _ = build_reconcile_scope(_get_df(), today)
        results = reconcile_attendance(pts, visit["records"], today=today)

        # Như Streamlit: BN đăng ký online (nguồn trống) mà CHƯA đến khám → tự gán nguồn
        # "BỆNH NHÂN VÃNG LAI" + trạng thái chưa khám ngay (lượt đối chiếu chính là bước xác nhận).
        auto = [r for r in results if r["status"] == "not_attended" and blank_source(r.get("source"))]
        n_auto, err_auto = 0, None
        if auto:
            done, errs, fatal = write_cells_ex([
                {"sheet_row": r["sheet_row"], "stt": r.get("stt"), "name": r.get("name"),
                 "values": {COL_STATUS: STATUS_NOT_ATTENDED, COL_SOURCE: SOURCE_VANG_LAI}} for r in auto])
            n_auto = len(done)
            for i in done:                                  # từng người độc lập: ai ghi được thì cập nhật, ai lỗi thì báo
                auto[i]["source"] = SOURCE_VANG_LAI
            if done:
                cache.delete("main_df")
            if fatal:
                err_auto = fatal
            elif errs:
                err_auto = f"{len(errs)}/{len(auto)} ca lỗi — {errs[min(errs)]}"
        store_save(tok, "rec", {"results": results, "ran_at": vn_now().strftime("%H:%M %d/%m/%Y"),
                                "n_auto": n_auto})
    if err_auto:
        flash(f"⚠️ Không tự gán được nguồn/trạng thái cho BN vãng lai chưa khám: {err_auto}", "warning")
    flash(f"📊 Đã đối chiếu {len(results)} bệnh nhân.", "success")
    return _rec_back(anchor="ketqua")


@app.route("/doi-chieu/cap-nhat", methods=["POST"])
@login_required
def reconcile_apply():
    """Bước 2: ghi "Đã khám" cho các ca khớp chắc chắn đã chọn. Trả JSON (fetch) hoặc redirect (form thường)."""
    from services.reconcile import store_load, store_save
    tok, tab = _rec_token(), request.form.get("tab", "tk")
    try:
        rec = store_load(tok, "rec")
        if not rec:
            return _rec_reply(False, "Kết quả đối chiếu đã hết hạn — hãy đối chiếu lại.", "info", tab, reload_hint=True)
        if not request.form.get("confirm"):
            return _rec_reply(False, "Hãy tích ô xác nhận đã xem kỹ danh sách trước khi ghi vào Google Sheet.",
                              "warning", tab, "buoc2")
        picked = set()
        for x in request.form.getlist("row"):
            try:
                picked.add(int(x))
            except ValueError:
                pass
        with _locked(_rec_lock) as got:
            if not got:
                return _rec_reply(False, "Đang có một thao tác ghi khác chạy — hãy đợi vài giây rồi bấm lại.",
                                  "warning", tab, "buoc2")
            rec = store_load(tok, "rec") or rec            # đọc lại sau khi giữ khoá (tránh ghi chồng)
            mine = [r for r in rec["results"] if r["sheet_row"] in picked and r["status"] == "attended_sure"]
            entries = [r for r in mine if not r.get("done")]
            already = len(mine) - len(entries)
            if not entries:
                if already:
                    return _rec_reply(True, "Cập nhật trạng thái thành công.", "success", tab, "buoc2",
                                      count=0, already=already)
                return _rec_reply(False, "Không có bệnh nhân nào được chọn để cập nhật.", "warning", tab, "buoc2")
            n, problems, fatal = _mark_attended(entries)
            if n:
                try:
                    store_save(tok, "rec", rec)
                except Exception as e:                     # Sheet đã ghi xong; chỉ mất cờ 'done' trên màn hình
                    app.logger.warning(f"reconcile_apply: store_save lỗi: {e}")
            names = [r["name"] for r in entries if r.get("done")]
        return _mark_result(n, problems, fatal, names, tab, "buoc2", already)
    except Exception as e:
        app.logger.exception("reconcile_apply")
        return _rec_reply(False, f"Lỗi máy chủ: {type(e).__name__}: {e}", "error", tab, "buoc2")


@app.route("/doi-chieu/xu-ly", methods=["POST"])
@login_required
def reconcile_one():
    """Bước 3 (chỉ khớp tên) & Bước 4 (nghi bị sót — chỉ nhóm vãng lai): lưu SĐT/năm sinh hoặc xác nhận Đã khám."""
    from services.reconcile import store_load, store_save, patient_kind
    from services.sheets import write_cells, COL_PHONE, COL_BIRTH_YEAR
    tok, tab = _rec_token(), request.form.get("tab", "tk")
    anchor = request.form.get("anchor", "buoc3")
    try:
        rec = store_load(tok, "rec")
        try:
            row = int(request.form.get("sheet_row", ""))
        except ValueError:
            row = -1
        entry = next((r for r in (rec or {}).get("results", []) if r["sheet_row"] == row), None)
        if not entry:
            return _rec_reply(False, "Không tìm thấy bệnh nhân này trong kết quả — hãy đối chiếu lại.", "info", tab,
                              reload_hint=True)
        action = request.form.get("action")
        if entry.get("done") and action == "confirm":
            return _rec_reply(True, f"{entry['name']} đã được đánh dấu \"Đã khám\" trước đó.", "success", tab, anchor,
                              count=0, already=1)
        if entry.get("done"):
            return _rec_reply(False, "Bệnh nhân này đã được xử lý — hãy tải lại trang.", "info", tab, anchor)
        with _locked(_rec_lock) as got:
            if not got:
                return _rec_reply(False, "Đang có một thao tác ghi khác chạy — hãy đợi vài giây rồi bấm lại.",
                                  "warning", tab, anchor)
            if action == "save":
                phone = request.form.get("phone", "").strip()
                birth = request.form.get("birth", "").strip()
                n, err = write_cells([{"sheet_row": row, "stt": entry.get("stt"), "name": entry["name"],
                                       "values": {COL_PHONE: phone, COL_BIRTH_YEAR: birth}}], raw=True)
                if err:
                    return _rec_reply(False, err, "error", tab, anchor)
                entry["phone"], entry["birth_year"] = phone, birth
                try:
                    store_save(tok, "rec", rec)
                except Exception as e:
                    app.logger.warning(f"reconcile_one: store_save lỗi: {e}")
                cache.delete("main_df")
                return _rec_reply(True, f"Đã lưu SĐT/Năm sinh cho {entry['name']} và kiểm tra lại trên Google Sheet — "
                                  "lần đối chiếu sau sẽ tự khớp đúng hơn.", "success", tab, anchor, count=1,
                                  names=[entry["name"]])
            # Bước 3 (khớp yếu) luôn được xác nhận; Bước 4 (nghi bị sót) chỉ cho nhóm vãng lai
            can_confirm = entry["status"] == "attended_unsure" or (
                bool(entry.get("near_miss")) and patient_kind(entry.get("source")) != "tai_kham")
            if action == "confirm" and can_confirm:
                n, problems, fatal = _mark_attended([entry])
                if fatal or not n:
                    return _rec_reply(False, fatal or (problems[0] if problems else "Không ghi được."), "error", tab, anchor)
                try:
                    store_save(tok, "rec", rec)
                except Exception as e:
                    app.logger.warning(f"reconcile_one: store_save lỗi: {e}")
                return _rec_reply(True, f"Đã đánh dấu \"Đã khám\" cho {entry['name']} và kiểm tra lại trên Google Sheet.",
                                  "success", tab, anchor, count=1, names=[entry["name"]])
        return _rec_reply(False, "Ca này không thuộc nhóm có thể xác nhận ở bước này.", "warning", tab, anchor)
    except Exception as e:
        app.logger.exception("reconcile_one")
        return _rec_reply(False, f"Lỗi máy chủ: {type(e).__name__}: {e}", "error", tab, anchor)


@app.route("/doi-chieu/csv")
@login_required
def reconcile_csv():
    import csv, io
    from flask import Response
    from services.reconcile import store_load, patient_kind
    from services.sheets import vn_now
    tab = request.args.get("tab", "tk")
    rec = store_load(_rec_token(), "rec")
    if not rec:
        flash("Chưa có kết quả đối chiếu.", "info")
        return _rec_back(tab)
    lab = {"attended_sure": "Đã đến khám", "attended_unsure": "Có thể đã đến (cần xác nhận)",
           "not_attended": "Chưa đến khám"}
    tier = {1: "Tên + SĐT", 2: "Tên + Năm sinh", 3: "Chỉ Tên"}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["STT", "Họ tên", "SĐT", "Năm sinh", "Tuổi", "Nguồn", "Ngày hẹn", "Kết quả", "Khớp qua",
                "Ngày thực đến", "SĐT lúc khám", "Năm sinh lúc khám", "Khoa thực khám", "Độ tin cậy"])
    for r in rec["results"]:
        if (patient_kind(r.get("source")) == "tai_kham") != (tab == "tk"):
            continue
        v = r.get("visit") or {}
        w.writerow([r.get("stt", ""), r["name"], r.get("phone", ""), r.get("birth_year", ""), r.get("age", ""),
                    r.get("source", ""), _dmy_filter(r.get("exam_date")), lab[r["status"]],
                    tier.get(r.get("match_tier"), ""), v.get("NGÀY ĐK", ""), v.get("SỐ ĐIỆN THOẠI", ""),
                    v.get("NĂM SINH", ""), v.get("KHOA ĐK", ""), f"{r['score']:.0f}%"])
    fname = f"doi_chieu_taikham_{tab}_{vn_now().strftime('%Y%m%d_%H%M')}.csv"
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ── Lọc trùng: dòng tái khám (từ khoa) ↔ dòng đăng ký Form ───────────────────
@app.route("/doi-chieu/trung/quet", methods=["POST"])
@login_required
def dup_scan():
    from services.reconcile import (store_save, build_dup_lists, find_duplicate_tk_form, classify_dup_pairs)
    from services.sheets import vn_today, vn_now
    tok = _rec_token(create=True)
    cache.delete("main_df")
    khoa, form, _ = build_dup_lists(_get_df(), vn_today())
    pairs = classify_dup_pairs(find_duplicate_tk_form(khoa, form))
    store_save(tok, "dup", {"pairs": pairs, "n_khoa": len(khoa), "n_form": len(form),
                            "scanned_at": vn_now().strftime("%H:%M %d/%m/%Y")})
    if not pairs:
        flash("✅ Không phát hiện trường hợp nghi trùng nào trong phạm vi quét.", "success")
    return redirect(url_for("reconcile_page", _anchor="trung"))


@app.route("/doi-chieu/trung/xoa", methods=["POST"])
@login_required
def dup_delete():
    """Xoá HẲN các dòng Form đã chọn (giữ dòng tái khám). Dòng Form đang 'Đã khám' mà dòng tái khám
    chưa → chuyển trạng thái sang dòng tái khám TRƯỚC khi xoá để không mất dấu lượt khám."""
    from services.reconcile import store_load, store_delete, pair_locked
    from services.sheets import write_cells, delete_rows, STATUS_ATTENDED, COL_STATUS
    tok = _rec_token()
    dup = store_load(tok, "dup")
    back = redirect(url_for("reconcile_page", _anchor="trung"))
    if not dup:
        flash("Kết quả quét đã hết hạn — hãy quét lại.", "info")
        return back
    if not request.form.get("confirm"):
        flash("⚠️ Hãy tích ô xác nhận (xoá không thể hoàn tác) trước khi xoá.", "warning")
        return back
    picked = set()
    for x in request.form.getlist("row"):
        try:
            picked.add(int(x))
        except ValueError:
            pass
    chosen = [d for d in dup["pairs"] if d["form"]["sheet_row"] in picked]
    # Cặp khớp yếu mà dòng Form đã khám: không xoá, không chuyển trạng thái (có thể là 2 người trùng tên)
    locked = [d for d in chosen if pair_locked(d)]
    sel = [d for d in chosen if not pair_locked(d)]
    if locked:
        flash(f"🔒 Bỏ qua {len(locked)} cặp khớp yếu (chỉ khớp tên, dòng Form đã khám) — có thể là 2 người khác nhau, "
              "hãy kiểm tra tay trên Sheet.", "warning")
    if not sel:
        if not locked:
            flash("Chưa chọn dòng Form nào để xoá.", "warning")
        return back
    with _rec_lock:
        # chỉ chuyển "Đã khám" khi khớp CHẮC (tier 1/2)
        transfer = [d for d in sel if d.get("needs_status_transfer") and d.get("match_tier", 3) <= 2]
        if transfer:
            from services.sheets import COL_ACTUAL_DATE, to_dmy
            def _vals(d):
                v = {COL_STATUS: STATUS_ATTENDED}
                act = to_dmy(d["form"].get("actual_date")) or to_dmy(d["form"].get("exam_date"))
                if act:
                    v[COL_ACTUAL_DATE] = act
                return v
            _, err = write_cells([{"sheet_row": d["khoa"]["sheet_row"], "stt": d["khoa"].get("stt"),
                                   "name": d["khoa"]["name"], "values": _vals(d)}
                                  for d in transfer], raw=True)
            if err:
                flash(f"❌ Lỗi khi chuyển trạng thái sang dòng tái khám (chưa xoá gì): {err}", "error")
                return back
        n, err = delete_rows([{"sheet_row": d["form"]["sheet_row"], "stt": d["form"].get("stt"),
                               "name": d["form"]["name"]} for d in sel])
    cache.delete("main_df")
    if err:
        flash(f"❌ {err}", "error")
        return back
    store_delete(tok, "dup")
    store_delete(tok, "rec")        # số dòng trên Sheet đã đổi → kết quả đối chiếu cũ không còn đúng
    flash(f"✅ Đã xoá {n} dòng Form khỏi Google Sheet"
          + (f" · đã cập nhật {len(transfer)} dòng tái khám thành \"Đã khám\"" if transfer else "") + ".", "success")
    return back


@app.route("/doi-chieu/trung/don-nhan-cu", methods=["POST"])
@login_required
def dup_clean_legacy():
    """Dọn các dòng đã bị phiên bản cũ gắn nhãn '⚠️ TRÙNG' nhưng chưa xoá."""
    from services.reconcile import build_dup_lists, store_delete
    from services.sheets import vn_today, delete_rows
    back = redirect(url_for("reconcile_page", _anchor="trung"))
    if not request.form.get("confirm"):
        flash("⚠️ Hãy tích ô xác nhận (xoá không thể hoàn tác) trước khi xoá.", "warning")
        return back
    with _rec_lock:
        cache.delete("main_df")
        legacy = build_dup_lists(_get_df(), vn_today())[2]
        n, err = delete_rows([{"sheet_row": r["sheet_row"], "stt": r.get("stt"), "name": r["name"]} for r in legacy])
    cache.delete("main_df")
    if err:
        flash(f"❌ {err}", "error")
    else:
        store_delete(_rec_token(), "rec")
        flash(f"✅ Đã xoá {n} dòng đã gắn nhãn trùng khỏi Google Sheet.", "success")
    return back


# ── Làm mới cache thủ công ────────────────────────────────────────────────────
@app.route("/refresh")
@login_required
def refresh_cache():
    cache.delete("main_df")
    flash("Đã làm mới dữ liệu từ Google Sheet.", "success")
    return redirect(request.referrer or url_for("dashboard"))


# ── Context processor: inject user + ngày hiện tại vào mọi template ──────────
@app.context_processor
def inject_globals():
    from services.importer import IMPORT_VERSION
    return {
        "import_version": IMPORT_VERSION,
        "current_user": current_user,
        "today_str":    datetime.now().strftime("%A, %d/%m/%Y"),
    }


# ── Chạy local ────────────────────────────────────────────────────────────────

# ════════════════════════════════════════════════════════════════════════════════
# LỆNH QUẢN LÝ TÀI KHOẢN ADMIN (chạy trong terminal của máy chủ / container)
#   flask --app app init-db                              (kiểm tra kết nối + tạo bảng)
#   flask --app app admin-list
#   flask --app app admin-add email@benhvien.vn        (hỏi mật khẩu; bỏ trống = chỉ đăng nhập bằng Google)
#   flask --app app admin-passwd email@benhvien.vn      (đổi mật khẩu)
#   flask --app app admin-disable email@benhvien.vn     /  admin-enable email@benhvien.vn
#   flask --app app admin-2fa-reset email@benhvien.vn   (mất điện thoại: xoá mã OTP, lần đăng nhập sau quét QR lại)
#   flask --app app mail-test email@benhvien.vn         (gửi thử email để kiểm tra SMTP)
#   flask --app app admin-email cu@benhvien.vn moi@benhvien.vn   (đổi email đăng nhập, không cần SMTP)
# ════════════════════════════════════════════════════════════════════════════════
import click


@app.cli.command("init-db")
def cli_init_db():
    """Kiểm tra kết nối PostgreSQL và tạo bảng admin_users + access_log nếu chưa có (tự chạy lại được, không mất dữ liệu)."""
    url = app.config.get("DATABASE_URL", "")
    if not url:
        raise click.ClickException("Chưa đặt biến môi trường DATABASE_URL.")
    try:
        with DB.pool(url).connection() as c:
            ver = c.execute("SELECT version() AS v").fetchone()["v"]
            tables = [r["tablename"] for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename IN ('admin_users','access_log') "
                "ORDER BY tablename").fetchall()]
    except DB.DBError as e:
        raise click.ClickException(f"Không kết nối được PostgreSQL: {e}")
    click.echo(f"Kết nối OK: {ver.split(',')[0]}")
    click.echo(f"Các bảng đã có: {', '.join(tables)}")
    n = _admins().count()
    click.echo(f"Số tài khoản admin: {n}" + ("  → thêm bằng: flask --app app admin-add email@..." if n == 0 else ""))


@app.cli.command("admin-list")
def cli_admin_list():
    """Liệt kê tài khoản admin."""
    rows = _admins().list()
    if not rows:
        click.echo("Chưa có tài khoản admin nào.")
    for r in rows:
        last = ACL.fmt_time(r["last_login_at"]) if r["last_login_at"] else "chưa đăng nhập"
        click.echo(f"{'BẬT ' if r['is_active'] else 'TẮT '} {r['email']:<36} "
                   f"{'mật khẩu' if r['has_password'] else 'chỉ Google'}  · 2 bước: {'đã thiết lập' if r['has_totp'] else 'chưa'}  · lần cuối: {last}")


@app.cli.command("admin-add")
@click.argument("email")
@click.option("--name", default="", help="Tên hiển thị")
def cli_admin_add(email, name):
    """Thêm tài khoản admin."""
    pw = click.prompt("Mật khẩu (>= 8 ký tự, bỏ trống = chỉ đăng nhập bằng Google)", hide_input=True,
                      confirmation_prompt=True, default="", show_default=False)
    try:
        ok = _admins().add(email, pw, name)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo("Đã thêm tài khoản." if ok else "Email này đã có tài khoản.")


@app.cli.command("admin-passwd")
@click.argument("email")
def cli_admin_passwd(email):
    """Đổi mật khẩu một tài khoản admin."""
    pw = click.prompt("Mật khẩu mới (>= 8 ký tự)", hide_input=True, confirmation_prompt=True)
    try:
        ok = _admins().set_password(email, pw)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo("Đã đổi mật khẩu." if ok else "Không tìm thấy tài khoản.")


@app.cli.command("admin-disable")
@click.argument("email")
def cli_admin_disable(email):
    """Tắt tài khoản (không xoá): người đó bị đăng xuất ở lần thao tác kế tiếp."""
    click.echo("Đã tắt tài khoản." if _admins().set_active(email, False) else "Không tìm thấy tài khoản.")


@app.cli.command("admin-email")
@click.argument("old_email")
@click.argument("new_email")
def cli_admin_email(old_email, new_email):
    """Đổi email đăng nhập của 1 admin (không cần gửi email xác nhận — dùng khi chưa cấu hình SMTP)."""
    try:
        ok = _admins().change_email(old_email, new_email)
    except ValueError as e:
        click.echo(f"LỖI: {e}")
        return
    click.echo("Đã đổi email đăng nhập." if ok else "Không đổi được: không thấy tài khoản cũ hoặc email mới đã có người dùng.")


@app.cli.command("mail-test")
@click.argument("to")
def cli_mail_test(to):
    """Gửi thử 1 email để kiểm tra cấu hình SMTP (dùng cho Quên mật khẩu / Đổi điện thoại)."""
    if not MAIL.configured(app.config):
        click.echo("Chưa cấu hình SMTP: cần SMTP_HOST và SMTP_FROM (hoặc SMTP_USER dạng email).")
        return
    try:
        MAIL.send(app.config, to, "Thử gửi email — BVĐK Tâm Đức Cầu Quan", "Nếu bạn đọc được email này thì cấu hình SMTP đã đúng.\n")
        click.echo(f"Đã gửi email thử tới {to}.")
    except MAIL.MailError as e:
        click.echo(f"LỖI: {e}")
    if not str(app.config.get("ADMIN_BASE_URL", "")).startswith(("http://", "https://")):
        click.echo("LƯU Ý: chưa đặt ADMIN_BASE_URL (vd https://quanly.ten-mien.vn) nên liên kết Quên mật khẩu / Đổi điện thoại vẫn đang TẮT.")


@app.cli.command("admin-2fa-reset")
@click.argument("email")
def cli_admin_2fa_reset(email):
    """Xoá thiết lập xác thực 2 bước (mất/đổi điện thoại): lần đăng nhập sau người đó quét lại mã QR."""
    click.echo("Đã đặt lại 2 bước — lần đăng nhập tới sẽ hiện mã QR mới." if _admins().reset_totp(email) else "Không tìm thấy tài khoản.")


@app.cli.command("admin-enable")
@click.argument("email")
def cli_admin_enable(email):
    """Bật lại tài khoản đã tắt."""
    click.echo("Đã bật tài khoản." if _admins().set_active(email, True) else "Không tìm thấy tài khoản.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
