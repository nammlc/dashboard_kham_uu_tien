"""
app.py — BVĐK Tâm Đức Cầu Quan
Flask app: authentication + dashboard routes.

Chạy local:   flask run  (hoặc python app.py)
Deploy Render: gunicorn app:app
"""

import os, json
from datetime import datetime

from flask import (Flask, render_template, redirect, url_for,
                   session, request, flash, jsonify, g)
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

# ── User Model (in-memory, đơn giản — có thể mở rộng sang DB sau) ────────────
_users: dict[str, "User"] = {}

class User(UserMixin):
    def __init__(self, email: str, name: str, picture: str = ""):
        self.id      = email
        self.email   = email
        self.name    = name
        self.picture = picture

@login_manager.user_loader
def load_user(user_id: str):
    return _users.get(user_id)


# ════════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        allowed = app.config.get("ALLOWED_EMAILS", set())
        admin_pw = app.config.get("ADMIN_PASSWORD", "")

        if email in allowed and password == admin_pw and admin_pw:
            u = User(email, email.split("@")[0])
            _users[email] = u
            login_user(u, remember=True)
            return redirect(request.args.get("next") or url_for("dashboard"))
        else:
            flash("Email hoặc mật khẩu không đúng.", "error")

    return render_template("auth/login.html")


@app.route("/login/google")
def login_google():
    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/login/google/callback")
def google_callback():
    token     = oauth.google.authorize_access_token()
    user_info = token.get("userinfo") or oauth.google.userinfo()
    email     = user_info.get("email", "").lower()

    allowed = app.config.get("ALLOWED_EMAILS", set())
    if allowed and email not in allowed:
        flash("Tài khoản Google này không được phép truy cập.", "error")
        return redirect(url_for("login"))

    u = User(email, user_info.get("name", email), user_info.get("picture", ""))
    _users[email] = u
    login_user(u, remember=True)
    return redirect(url_for("dashboard"))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ════════════════════════════════════════════════════════════════════════════════
# DASHBOARD ROUTES
# ════════════════════════════════════════════════════════════════════════════════

def _get_df():
    """Lấy DataFrame từ cache (hoặc gọi Sheets nếu hết hạn)."""
    from services.sheets import load_dataframe
    key = "main_df"
    df  = cache.get(key)
    if df is None:
        df = load_dataframe()
        cache.set(key, df)
    return df


@app.route("/")
@login_required
def dashboard():
    from services.sheets import get_dashboard_stats
    df    = _get_df()
    stats = get_dashboard_stats(df)
    return render_template("dashboard/index.html", stats=stats)


@app.route("/bao-cao")
@login_required
def report():
    from services.sheets import get_report_stats
    df     = _get_df()
    period = request.args.get("period", "month")
    rows   = get_report_stats(df, period)
    return render_template("dashboard/report.html", rows=rows, period=period)


@app.route("/bao-cao/data")
@login_required
def report_data():
    """HTMX endpoint — trả về tbody HTML khi đổi kỳ."""
    from services.sheets import get_report_stats
    df     = _get_df()
    period = request.args.get("period", "month")
    rows   = get_report_stats(df, period)
    return render_template("dashboard/_report_rows.html", rows=rows)


@app.route("/benh-nhan")
@login_required
def patients():
    df      = _get_df()
    q       = request.args.get("q", "").strip()
    status  = request.args.get("status", "")
    page    = int(request.args.get("page", 1))
    per_page = 20

    from services.sheets import COL_NAME, COL_STATUS, COL_PHONE
    filtered = df.copy()
    if q:
        mask = filtered.apply(lambda row: q.lower() in " ".join(str(v) for v in row).lower(), axis=1)
        filtered = filtered[mask]
    if status:
        filtered = filtered[filtered.get(COL_STATUS, "") == status]

    total   = len(filtered)
    start   = (page - 1) * per_page
    records = filtered.iloc[start:start + per_page].to_dict("records")

    return render_template("dashboard/patients.html",
                           records=records, total=total,
                           page=page, per_page=per_page,
                           q=q, status=status)


@app.route("/import", methods=["GET", "POST"])
@login_required
def import_excel():
    if request.method == "POST":
        f = request.files.get("excel_file")
        if not f or not f.filename.endswith(".xlsx"):
            flash("Vui lòng chọn file .xlsx hợp lệ.", "error")
            return redirect(url_for("import_excel"))

        from services.excel_parser import parse_minh_lo_excel
        from services.sheets import append_patients

        records, err = parse_minh_lo_excel(f.read())
        if err:
            flash(f"Lỗi đọc file: {err}", "error")
            return redirect(url_for("import_excel"))

        # Lưu preview vào session để xác nhận trước khi ghi
        session["import_preview"] = records[:200]   # giới hạn 200 dòng preview
        session["import_total"]   = len(records)
        return redirect(url_for("import_preview_page"))

    return render_template("dashboard/import.html")


@app.route("/import/preview")
@login_required
def import_preview_page():
    records = session.get("import_preview", [])
    total   = session.get("import_total", 0)
    return render_template("dashboard/import_preview.html", records=records, total=total)


@app.route("/import/confirm", methods=["POST"])
@login_required
def import_confirm():
    from services.sheets import append_patients
    records = session.pop("import_preview", [])
    n, err  = append_patients(records)
    cache.delete("main_df")   # xoá cache để lần sau load lại dữ liệu mới

    if err:
        flash(f"Lỗi khi ghi vào Sheet: {err}", "error")
    else:
        flash(f"✅ Đã import thành công {n} bệnh nhân vào Google Sheet.", "success")
    return redirect(url_for("import_excel"))


# ── API: cập nhật trạng thái bệnh nhân (từ bảng danh sách) ──────────────────
@app.route("/api/status", methods=["POST"])
@login_required
def api_update_status():
    from services.sheets import update_status
    data       = request.get_json()
    row_number = data.get("row")
    new_status = data.get("status")
    ok = update_status(row_number, new_status)
    cache.delete("main_df")
    return jsonify({"ok": ok})


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
    return {
        "current_user": current_user,
        "today_str":    datetime.now().strftime("%A, %d/%m/%Y"),
    }


# ── Chạy local ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
