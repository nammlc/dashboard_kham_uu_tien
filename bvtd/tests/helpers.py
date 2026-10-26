"""Tiện ích dùng chung cho bộ test.

* Thiếu thư viện thật (flask_login, authlib, flask_caching, gspread, google-auth) → tự cài bản giả tối thiểu để
  test vẫn chạy được trong môi trường sạch. Có thư viện thật thì dùng thư viện thật.
* FakeSheet: Google Sheet giả, lưu ô dạng CHUỖI như Sheets trả về (FORMATTED_VALUE), có chèn lỗi (429, timeout…).
"""
import collections
import os
import string
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


def install_stubs():
    try:
        import flask_login  # noqa: F401
    except ImportError:
        class _User:
            is_authenticated = True
            name, email, picture, id = "tester", "t@x.vn", "", "t@x.vn"
        _stub("flask_login", UserMixin=object, LoginManager=type("LoginManager", (), {
            "__init__": lambda self, app=None: None, "user_loader": lambda self, f: f}),
            login_user=lambda *a, **k: None, logout_user=lambda *a, **k: None,
            login_required=lambda f: f, current_user=_User())
    try:
        import authlib  # noqa: F401
    except ImportError:
        _stub("authlib")
        _stub("authlib.integrations")
        _stub("authlib.integrations.flask_client", OAuth=type("OAuth", (), {
            "__init__": lambda self, app=None: None, "register": lambda self, **k: None}))
    try:
        import flask_caching  # noqa: F401
    except ImportError:
        class Cache:
            def __init__(self, app=None):
                self.d = {}
            def get(self, k):
                v = self.d.get(k)
                return v[0] if v else None
            def set(self, k, v, timeout=300):
                self.d[k] = (v, 0)
            def delete(self, k):
                self.d.pop(k, None)
            def clear(self):
                self.d.clear()
        _stub("flask_caching", Cache=Cache)
    try:
        import gspread  # noqa: F401
    except ImportError:
        def rowcol_to_a1(row, col):
            letters = ""
            while col:
                col, r = divmod(col - 1, 26)
                letters = string.ascii_uppercase[r] + letters
            return f"{letters}{row}"
        _stub("gspread", Client=object, authorize=lambda c: None, utils=types.SimpleNamespace(rowcol_to_a1=rowcol_to_a1))
        _stub("google")
        _stub("google.oauth2")
        _stub("google.oauth2.service_account", Credentials=type("Credentials", (), {
            "from_service_account_info": staticmethod(lambda *a, **k: None)}))


install_stubs()

import pandas as pd  # noqa: E402

os.chdir(ROOT)
import app as A  # noqa: E402
import services.sheets as sh  # noqa: E402

HEADERS = ["Dấu thời gian", "NGUỒN BỆNH NHÂN", "TRẠNG THÁI", "STT", "NGÀY KHÁM", "1. HỌ VÀ TÊN BỆNH NHÂN", "NĂM SINH",
           "TUỔI", "5. SỐ ĐIÊN THOẠI", "2. ĐỊA CHỈ (THÔN/XÃ)", "KHOA KHÁM CHỮA BỆNH", "3. GIỚI TÍNH",
           "CHUYÊN KHOA MONG MUỐN KHÁM", "BÁC SĨ MONG MUỐN ( nếu có)", "GIỜ KHÁM DỰ KIẾN", "NGÀY THỰC KHÁM",
           "NGÀY TẠO LỊCH HẸN KHÁM"]
TK = "BỆNH NHÂN ĐIỀU TRỊ NỘI KHOA TÁI KHÁM"
VL = "BỆNH NHÂN VÃNG LAI"
DA_KHAM = "BỆNH NHÂN ĐÃ KHÁM"
CHUA_KHAM = "BỆNH NHÂN CHƯA KHÁM/BỎ KHÁM"


class ReadTimeout(Exception):
    """Tên lớp trùng requests.exceptions.ReadTimeout → được coi là lỗi tạm thời."""


class ApiError(Exception):
    """Giống gspread.exceptions.APIError: có .response.status_code."""
    def __init__(self, code, msg="api error"):
        super().__init__(f"{code}: {msg}")
        self.response = types.SimpleNamespace(status_code=code)


def sheet_row(stt, name, *, birth="", phone="", hen="", source=TK, status=CHUA_KHAM, actual="", khoa="KHOA SẢN", created=""):
    """1 dòng dữ liệu theo đúng thứ tự HEADERS."""
    row = {h: "" for h in HEADERS}
    row.update({"Dấu thời gian": "14/10/2026 16:44:21", "NGUỒN BỆNH NHÂN": source, "TRẠNG THÁI": status, "STT": str(stt),
                "NGÀY KHÁM": hen, "1. HỌ VÀ TÊN BỆNH NHÂN": name, "NĂM SINH": birth, "5. SỐ ĐIÊN THOẠI": phone,
                "KHOA KHÁM CHỮA BỆNH": khoa, "NGÀY THỰC KHÁM": actual,
                "NGÀY TẠO LỊCH HẸN KHÁM": created})
    return [row[h] for h in HEADERS]


class FakeSheet:
    """Worksheet giả (đồng thời là Spreadsheet giả). `faults[tên_hàm]` = danh sách lỗi sẽ ném lần lượt."""
    id = 7

    def __init__(self, rows=(), headers=HEADERS):
        self.grid = [list(headers)] + [list(r) for r in rows]
        self.calls = collections.Counter()
        self.faults = collections.defaultdict(list)
        self.spreadsheet = self
        self.drop_writes = False            # Google báo OK nhưng ô không đổi
        self.fail_reads_after_write = False  # đọc lại sau khi ghi bị lỗi
        self.written = False

    def _enter(self, name):
        self.calls[name] += 1
        if self.faults[name]:
            f = self.faults[name].pop(0)
            if isinstance(f, BaseException):
                raise f
            f(self)                                   # callable: được sửa trạng thái trước khi ném lỗi

    # ── đọc ──
    def row_values(self, n):
        self._enter("row_values")
        return list(self.grid[n - 1]) if n <= len(self.grid) else []

    def col_values(self, c):
        self._enter("col_values")
        if self.fail_reads_after_write and self.written:
            raise ReadTimeout("đọc lại sau khi ghi bị timeout")
        out = [r[c - 1] if c - 1 < len(r) else "" for r in self.grid]
        while out and out[-1] == "":
            out.pop()
        return out

    def get_all_values(self):
        self._enter("get_all_values")
        return [list(r) for r in self.grid]

    def batch_get(self, ranges):
        """Như gspread: mỗi range 'C:C' → danh sách các dòng [giá trị] (dòng trống cuối bị cắt)."""
        self._enter("batch_get")
        if self.fail_reads_after_write and self.written:
            raise ReadTimeout("đọc lại sau khi ghi bị timeout")
        out = []
        for rg in ranges:
            letters = rg.split(":")[0]
            col = 0
            for ch in letters:
                col = col * 26 + ord(ch) - 64
            vals = [[r[col - 1]] if col - 1 < len(r) and r[col - 1] != "" else [] for r in self.grid]
            while vals and not vals[-1]:
                vals.pop()
            out.append(vals)
        return out

    # ── ghi ──
    def batch_update(self, body, value_input_option=None):
        self._enter("batch_update")
        if isinstance(body, dict):                    # spreadsheet.batch_update({"requests": [deleteDimension…]})
            for req in sorted(body["requests"], key=lambda q: -q["deleteDimension"]["range"]["startIndex"]):
                del self.grid[req["deleteDimension"]["range"]["startIndex"]]
            return {}
        self.written = True
        if self.drop_writes:
            return {}
        for item in body:
            letters = "".join(c for c in item["range"] if c.isalpha())
            row = int("".join(c for c in item["range"] if c.isdigit()))
            col = 0
            for ch in letters:
                col = col * 26 + ord(ch) - 64
            self.grid[row - 1] += [""] * (col - len(self.grid[row - 1]))
            self.grid[row - 1][col - 1] = str(item["values"][0][0])
        return {}

    def append_rows(self, rows, value_input_option=None, insert_data_option=None, table_range=None):
        self._enter("append_rows")
        date_col = self.grid[0].index("NGÀY KHÁM")
        stt_col = self.grid[0].index("STT")
        from datetime import datetime, timedelta
        for r in rows:
            r = list(r)
            if isinstance(r[date_col], int):          # serial → hiển thị dd/mm/yyyy như ô đã định dạng ngày
                r[date_col] = (datetime(1899, 12, 30) + timedelta(days=r[date_col])).strftime("%d/%m/%Y")
            r[stt_col] = str(len(self.grid))          # cột STT của Sheet tự đánh số
            self.grid.append([str(x)[1:] if str(x).startswith("'") else str(x) for x in r])   # dấu ' đầu ô = ép chữ, không hiện ra
        return {"updates": {"updatedRows": len(rows)}}

    # ── tiện cho test ──
    def names(self):
        i = self.grid[0].index("1. HỌ VÀ TÊN BỆNH NHÂN")
        return [r[i] for r in self.grid[1:]]

    def cell(self, row, header):
        return self.grid[row - 1][self.grid[0].index(header)]


class FakeClient:
    opened = 0                                   # số lần mở Sheet (kiểm tra việc dùng lại worksheet)

    def __init__(self, sheet):
        self.sheet = sheet

    def open_by_key(self, key):
        FakeClient.opened += 1
        return self

    def worksheet(self, name):
        return self.sheet


class AppTestCase(unittest.TestCase):
    maxDiff = 600                      # lỗi so khớp trang HTML dài: chỉ in phần đầu cho dễ đọc

    """Cài Sheet giả vào app, xoá mọi cache giữa các test, không chờ thật khi backoff."""
    TODAY = None

    def install_sheet(self, sheet):
        self.sheet = sheet
        FakeClient.opened = 0
        sh._get_client = lambda: FakeClient(sheet)
        sh._WS_CACHE.update(ws=None, key=None, t=0.0)
        A.cache.clear()
        return sheet

    def setUp(self):
        import datetime as dt
        from unittest import mock
        A.app.config.update(TESTING=True, LOGIN_DISABLED=True)
        A.current_user = type("U", (), {"is_authenticated": True, "name": "tester", "email": "t@x.vn", "picture": ""})()
        patches = [mock.patch.object(sh.time, "sleep", lambda s: None),
                   mock.patch.object(sh, "vn_today", lambda: self.TODAY or dt.date(2026, 10, 21))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = A.app.test_client()
        self.install_sheet(FakeSheet())

    JSON = {"X-Requested-With": "fetch", "Accept": "application/json"}

    def post_json(self, url, data):
        return self.client.post(url, data=data, headers=self.JSON)
