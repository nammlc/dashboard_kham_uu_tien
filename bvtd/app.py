"""
app.py — BVĐK Tâm Đức Cầu Quan
Flask app: authentication + dashboard routes.

Chạy local:   flask run  (hoặc python app.py)
Deploy Render: gunicorn app:app
"""

import os, json, uuid, threading
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


# ════════════════════════════════════════════════════════════════════════════════
# IMPORT LỊCH HẸN TÁI KHÁM (file Minh Lộ 04-4)
#   /import          → upload file
#   /import/preview  → chọn Ngày lập · cảnh báo trùng · xem trước · import
#   /import/confirm  → ghi vào Google Sheet
#   /import/csv      → tải CSV các BN mới
# Dữ liệu đã đọc được giữ trên server (cache) theo token trong session — KHÔNG nhét
# vào cookie (cookie tối đa ~4KB, không chứa nổi danh sách BN).
# ════════════════════════════════════════════════════════════════════════════════
_IMPORT_TTL  = 3600                # giữ file đã upload 1 giờ
_import_lock = threading.Lock()    # chặn 2 người/2 cú click cùng ghi 1 lúc


def _import_data():
    token = session.get("import_token")
    return (cache.get(f"import:{token}") if token else None)


def _import_context(selected_arg: list[str] | None, has_sel: bool, fresh: bool = False):
    """Tính toàn bộ số liệu cho trang preview / confirm / csv. Trả None nếu phiên hết hạn."""
    from services.importer import group_by_lap, filter_by_lap, UNKNOWN_LAP
    from services.sheets import check_sheet_duplicates, vn_today

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

    if fresh:                                       # lúc ghi: luôn đọc lại Sheet mới nhất
        cache.delete("main_df")
    new_records, dup_records = check_sheet_duplicates(_get_df(), filtered)

    return {
        "filename": data["filename"], "records": records, "total": len(records),
        "order": order, "counts": counts, "selected": selected,
        "today_str": today_str, "UNKNOWN_LAP": UNKNOWN_LAP,
        "filtered": filtered, "new": new_records, "dup": dup_records,
    }


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

        old = session.get("import_token")
        if old:
            cache.delete(f"import:{old}")
        token = uuid.uuid4().hex
        cache.set(f"import:{token}", {"records": records, "filename": f.filename}, timeout=_IMPORT_TTL)
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
    from services.sheets import append_patients
    laps = request.form.getlist("lap")
    back = url_for("import_preview_page", sel=1, lap=laps)

    with _import_lock:
        # Đọc lại Sheet mới nhất rồi kiểm tra trùng LẦN NỮA ngay trước khi ghi:
        # chặn bấm 2 lần / refresh / người khác vừa nhập cùng lịch.
        ctx = _import_context(laps, True, fresh=True)
        if ctx is None:
            flash("Phiên import đã hết hạn — vui lòng upload lại file.", "info")
            return redirect(url_for("import_excel"))
        new, dup = ctx["new"], ctx["dup"]

        if not ctx["selected"]:
            flash("⚠️ Chưa chọn ngày lập nào — hãy tích chọn ít nhất 1 ngày.", "warning")
            return redirect(back)
        if not new:
            flash(f"⚠️ Không có bệnh nhân mới để import — {len(dup)} bệnh nhân đã được nhập trước đó, "
                  "hệ thống không ghi lại.", "warning")
            return redirect(back)

        n, err = append_patients(new)

    cache.delete("main_df")
    if err:
        flash(f"❌ {err}", "error")
        flash("💡 Nếu lỗi Permission: vào Google Sheet → Share → đổi Service Account từ Viewer thành Editor.", "info")
        return redirect(back)

    token = session.pop("import_token", None)       # xong → bỏ file, tránh import lại
    if token:
        cache.delete(f"import:{token}")
    flash(f"✅ Đã thêm thành công {n} dòng mới vào Google Sheet "
          f"(theo {len(ctx['selected'])} ngày lập đã chọn"
          + (f" · đã tự động bỏ qua {len(dup)} bệnh nhân trùng" if dup else "") + ").", "success")
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
