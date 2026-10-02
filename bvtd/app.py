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
    total = len(d)
    page_df = d.iloc[(page - 1) * per_page: page * per_page] if total else d
    records = [{
        "stt":    str(r.get(COL_STT, "") or "—"),
        "name":   str(r.get(COL_NAME, "") or "—"),
        "phone":  str(r.get(COL_PHONE, "") or "—"),
        "date":   r["_date"].strftime("%d/%m/%Y"),
        "khoa":   str(r.get(COL_KHOA, "") or "—"),
        "source": str(r.get(COL_SOURCE, "") or "—"),
        "att":    bool(r["_att"]),
    } for _, r in page_df.iterrows()]

    ky = "Toàn bộ"
    if key != "all" and total:
        from services.sheets import _period_label
        ky = _period_label(d["_start"].iloc[0], period)

    return render_template("dashboard/report_detail.html",
                           records=records, total=total, page=page, per_page=per_page,
                           kind=kind, kind_label=REPORT_KINDS[kind], key=key, ky=ky,
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
    from services.sheets import COL_STATUS, filter_by_date_range
    df       = _get_df()
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

    filtered = filter_by_date_range(df.copy(), d_from, d_to)
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
                           q=q, status=status,
                           date_from=d_from.isoformat() if d_from else "",
                           date_to=d_to.isoformat() if d_to else "")


@app.route("/lich-kham")
@login_required
def upcoming():
    """Bệnh nhân sẽ đến khám trong 3 ngày tới, chia tab theo khoa."""
    from services.sheets import get_upcoming_patients
    data = get_upcoming_patients(_get_df(), days=3)
    return render_template("dashboard/upcoming.html", data=data)


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
