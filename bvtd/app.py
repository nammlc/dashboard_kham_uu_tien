"""
app.py — BVĐK Tâm Đức Cầu Quan
Flask app: authentication + dashboard routes.

Chạy local:   flask run  (hoặc python app.py)
Deploy Render: gunicorn app:app
"""

import os, json, time, uuid, threading
from datetime import datetime, timedelta

from flask import (Flask, render_template, redirect, url_for,
                   session, request, flash, jsonify, g, abort)
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
# TÁCH TÊN MIỀN: bệnh nhân  ↔  nhân viên (admin)
# ════════════════════════════════════════════════════════════════════════════════
# PATIENT_HOST (tuỳ chọn) = tên miền dành cho bệnh nhân, ví dụ dangkyhenkham.tracuuduoc-bvdktamduccauquan.com
#   • Mở trang chủ "/" của tên miền này → tự chuyển sang /dang-ky.
#   • Thêm PATIENT_HOST_PUBLIC_ONLY=1 → tên miền này CHỈ phục vụ trang công khai (/dang-ky, /dang-ky/xong,
#     /static); mọi đường dẫn khác (đăng nhập, bảng điều khiển...) trả 404. Trang admin chỉ vào được bằng
#     tên miền admin.
# Không đặt PATIENT_HOST → không thay đổi gì (chạy local / test như cũ).
_PATIENT_HOST = os.environ.get("PATIENT_HOST", "").strip().lower()
_PATIENT_PUBLIC_ONLY = os.environ.get("PATIENT_HOST_PUBLIC_ONLY", "0") == "1"
_PATIENT_PUBLIC_PATHS = ("/dang-ky", "/dang-ky/xong")


@app.before_request
def _patient_host_gate():
    if not _PATIENT_HOST:
        return None
    if request.host.split(":")[0].lower() != _PATIENT_HOST:
        return None
    path = request.path
    if path == "/":
        return redirect(url_for("register_page"), code=302)
    if _PATIENT_PUBLIC_ONLY and not (path in _PATIENT_PUBLIC_PATHS or path.startswith("/static/")):
        abort(404)
    return None


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
    from services.sheets import COL_BIRTH_YEAR, COL_ACTUAL_DATE, to_dmy
    has_actual_col = COL_ACTUAL_DATE in d.columns

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
    if key != "all" and total:
        from services.sheets import _period_label
        ky = _period_label(d["_start"].iloc[0], period)

    return render_template("dashboard/report_detail.html",
                           records=records, total=total, page=page, per_page=per_page,
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


def _reg_no_store(resp):
    resp.headers["Cache-Control"] = "no-store"          # có thông tin cá nhân → không để trình duyệt/proxy lưu
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def _reg_render(values=None, errors=None, banner="", status=200):
    from services import registration as RG
    now = RG.vn_now()
    today = now.date()
    ctx = {
        "v": values or {}, "errors": errors or {}, "banner": banner,
        "start_step": RG.first_error_step(errors) if errors else 1,
        "csrf": session.get("reg_csrf", ""),
        "specialties": RG.SPECIALTIES, "doctors": RG.DOCTORS, "OTHER": RG.OTHER,
        "slots_morning": RG.SLOTS_MORNING, "slots_afternoon": RG.SLOTS_AFTERNOON,
        "today": today.isoformat(), "max_date": (today + timedelta(days=RG.MAX_DAYS_AHEAD)).isoformat(),
        "now_hm": now.strftime("%H:%M"), "max_days": RG.MAX_DAYS_AHEAD,
    }
    return _reg_no_store(app.make_response((render_template("register/form.html", **ctx), status)))


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
        "specialty": next((lbl for v, lbl, _ in RG.SPECIALTIES if v == clean["specialty"]), clean["specialty"]),
        "doctor": next((nm for v, nm, _, _ in RG.DOCTORS if v == clean["doctor"]), ""),
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
    if not n and problems:
        return _rec_reply(False, "Không cập nhật được ca nào. " + problems[0] +
                          (f" (và {len(problems) - 1} ca khác cũng lỗi)" if len(problems) > 1 else ""),
                          "error", tab, anchor, problems=problems[:12], more=max(0, len(problems) - 12))
    msg = f"Đã cập nhật \"Đã khám\" cho {n} bệnh nhân và kiểm tra lại trên Google Sheet."
    if already:
        msg += f" ({already} bệnh nhân khác đã được cập nhật từ trước.)"
    if problems:
        msg += f" ⚠️ {len(problems)} ca CHƯA cập nhật được — xem lý do bên dưới, bấm Làm mới rồi xử lý lại các ca đó."
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
                    return _rec_reply(True, f"{already} bệnh nhân đã chọn đã được cập nhật \"Đã khám\" trước đó — "
                                      "không cần ghi lại.", "success", tab, "buoc2", count=0, already=already)
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
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
