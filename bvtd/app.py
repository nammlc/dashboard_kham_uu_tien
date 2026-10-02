"""
app.py — BVĐK Tâm Đức Cầu Quan
Flask app: authentication + dashboard routes.

Chạy local:   flask run  (hoặc python app.py)
Deploy Render: gunicorn app:app
"""

import os, json, uuid, threading
from datetime import datetime, timedelta

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
    from services.sheets import COL_STATUS, DASH_SCOPES, filter_by_date_range, filter_by_scope
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
    if status:
        filtered = filtered[filtered.get(COL_STATUS, "") == status]

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

    from services.importer import delete_import
    delete_import(session.pop("import_token", ""))   # xong → bỏ file, tránh import lại
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


def _mark_attended(entries):
    """Ghi 'Đã khám' (và gán nguồn vãng lai nếu nguồn đang trống). Trả (n, err)."""
    from services.sheets import write_cells, STATUS_ATTENDED, COL_STATUS, COL_SOURCE
    from services.reconcile import blank_source, SOURCE_VANG_LAI
    ups = []
    for r in entries:
        vals = {COL_STATUS: STATUS_ATTENDED}
        if blank_source(r.get("source")):
            vals[COL_SOURCE] = SOURCE_VANG_LAI
        ups.append({"sheet_row": r["sheet_row"], "stt": r.get("stt"), "name": r.get("name"), "values": vals})
    n, err = write_cells(ups)
    if not err:
        for r in entries:
            r["done"] = True
            r["status_now"] = STATUS_ATTENDED
            if blank_source(r.get("source")):
                r["source"] = SOURCE_VANG_LAI
        cache.delete("main_df")
    return n, err


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
            "sot": [r for r in notatt if r.get("near_miss") and not r.get("done")],
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
    from services.sheets import vn_today, vn_now, write_cells, STATUS_NOT_ATTENDED, COL_STATUS, COL_SOURCE
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
            n_auto, err_auto = write_cells([
                {"sheet_row": r["sheet_row"], "stt": r.get("stt"), "name": r.get("name"),
                 "values": {COL_STATUS: STATUS_NOT_ATTENDED, COL_SOURCE: SOURCE_VANG_LAI}} for r in auto])
            if not err_auto:
                for r in auto:
                    r["source"] = SOURCE_VANG_LAI
                cache.delete("main_df")
        store_save(tok, "rec", {"results": results, "ran_at": vn_now().strftime("%H:%M %d/%m/%Y"),
                                "n_auto": n_auto})
    if err_auto:
        flash(f"⚠️ Không tự gán được nguồn/trạng thái cho BN vãng lai chưa khám: {err_auto}", "warning")
    flash(f"📊 Đã đối chiếu {len(results)} bệnh nhân.", "success")
    return _rec_back(anchor="ketqua")


@app.route("/doi-chieu/cap-nhat", methods=["POST"])
@login_required
def reconcile_apply():
    from services.reconcile import store_load, store_save
    tok, tab = _rec_token(), request.form.get("tab", "tk")
    rec = store_load(tok, "rec")
    if not rec:
        flash("Kết quả đối chiếu đã hết hạn — hãy đối chiếu lại.", "info")
        return _rec_back(tab)
    if not request.form.get("confirm"):
        flash("⚠️ Hãy tích ô xác nhận đã xem kỹ danh sách trước khi ghi vào Google Sheet.", "warning")
        return _rec_back(tab, "buoc2")
    picked = set()
    for x in request.form.getlist("row"):
        try:
            picked.add(int(x))
        except ValueError:
            pass
    with _rec_lock:
        entries = [r for r in rec["results"]
                   if r["sheet_row"] in picked and r["status"] == "attended_sure" and not r.get("done")]
        if not entries:
            flash("Không có bệnh nhân nào được chọn để cập nhật.", "warning")
            return _rec_back(tab, "buoc2")
        n, err = _mark_attended(entries)
        if not err:
            store_save(tok, "rec", rec)
    if err:
        flash(f"❌ {err}", "error")
    else:
        flash(f"✅ Đã cập nhật \"Đã khám\" cho {n} bệnh nhân.", "success")
    return _rec_back(tab, "buoc2")


@app.route("/doi-chieu/xu-ly", methods=["POST"])
@login_required
def reconcile_one():
    """Bước 3 (chỉ khớp tên) & Bước 4 (nghi bị sót): lưu SĐT/năm sinh hoặc xác nhận Đã khám."""
    from services.reconcile import store_load, store_save
    from services.sheets import write_cells, COL_PHONE, COL_BIRTH_YEAR
    tok, tab = _rec_token(), request.form.get("tab", "tk")
    rec = store_load(tok, "rec")
    try:
        row = int(request.form.get("sheet_row", ""))
    except ValueError:
        row = -1
    entry = next((r for r in (rec or {}).get("results", []) if r["sheet_row"] == row), None)
    if not entry or entry.get("done"):
        flash("Không tìm thấy bệnh nhân này trong kết quả (có thể đã xử lý) — hãy đối chiếu lại.", "info")
        return _rec_back(tab)
    anchor = request.form.get("anchor", "buoc3")
    action = request.form.get("action")
    with _rec_lock:
        if action == "save":
            phone = request.form.get("phone", "").strip()
            birth = request.form.get("birth", "").strip()
            n, err = write_cells([{"sheet_row": row, "stt": entry.get("stt"), "name": entry["name"],
                                   "values": {COL_PHONE: phone, COL_BIRTH_YEAR: birth}}], raw=True)
            if err:
                flash(f"❌ {err}", "error")
            else:
                entry["phone"], entry["birth_year"] = phone, birth
                store_save(tok, "rec", rec)
                cache.delete("main_df")
                flash(f"💾 Đã lưu SĐT/Năm sinh cho {entry['name']} — lần đối chiếu sau sẽ tự khớp đúng hơn.", "success")
        elif action == "confirm" and (entry["status"] == "attended_unsure" or entry.get("near_miss")):
            n, err = _mark_attended([entry])
            if err:
                flash(f"❌ {err}", "error")
            else:
                store_save(tok, "rec", rec)
                flash(f"✅ Đã đánh dấu Đã khám cho {entry['name']}.", "success")
    return _rec_back(tab, anchor)


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
    from services.reconcile import store_load, store_delete
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
    sel = [d for d in dup["pairs"] if d["form"]["sheet_row"] in picked]
    if not sel:
        flash("Chưa chọn dòng Form nào để xoá.", "warning")
        return back
    with _rec_lock:
        transfer = [d for d in sel if d.get("needs_status_transfer")]
        if transfer:
            _, err = write_cells([{"sheet_row": d["khoa"]["sheet_row"], "stt": d["khoa"].get("stt"),
                                   "name": d["khoa"]["name"], "values": {COL_STATUS: STATUS_ATTENDED}}
                                  for d in transfer])
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
