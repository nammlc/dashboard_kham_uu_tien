# Bộ test

Chạy từ thư mục `bvtd/`:

```bash
python -m unittest discover -s tests -t .     # toàn bộ (Python + JavaScript qua Node nếu có)
python -m unittest tests.test_import_flow     # riêng phần import
node tests/js/popup.test.js                   # riêng pop-up JavaScript
```

* Không cần Google thật: `tests/helpers.py` có `FakeSheet` mô phỏng Google Sheet (chèn lỗi 429/timeout, ghi mà không đổi…).
* Thiếu thư viện (flask_login, authlib, flask_caching, gspread…) thì helpers tự cài bản giả; có thì dùng thư viện thật.
* `test_templates_static.py` cấm dùng `form.action` trong JavaScript: form có nút/ô tên `action` làm thuộc tính này trả về
  **phần tử** thay vì URL → `fetch` tới `/…/[object HTMLButtonElement]` → HTTP 404. Luôn dùng `form.getAttribute('action')`.
