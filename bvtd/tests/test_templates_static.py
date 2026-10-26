"""Kiểm tra tĩnh template: không dùng form.action (bị nút name="action" ghi đè), pop-up dùng chung đúng chỗ."""
import glob
import os
import re
import unittest

from tests.helpers import ROOT

TEMPLATES = sorted(glob.glob(os.path.join(ROOT, "templates", "**", "*.html"), recursive=True))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def js_code(html):
    """Chỉ phần JavaScript trong <script>…</script>, đã bỏ chú thích."""
    out = []
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S):
        code = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
        out.append(re.sub(r"(?m)(^|\s)//.*$", "", code))
    return "\n".join(out)


class Templates(unittest.TestCase):
    def test_no_form_action_property_in_javascript(self):
        """form.action trả về PHẦN TỬ (không phải URL) khi form có nút/ô tên "action" → fetch tới /…/[object HTMLButtonElement] → 404."""
        bad = {}
        for path in TEMPLATES:
            hits = re.findall(r"\b\w+\.action\b", js_code(read(path)))
            if hits:
                bad[os.path.relpath(path, ROOT)] = hits
        self.assertEqual(bad, {})

    def test_no_form_elements_by_name_lookup(self):
        """form.elements['x'] / form.x cũng bị ghi đè khi trùng tên thuộc tính của form → dùng querySelector."""
        bad = [os.path.relpath(p, ROOT) for p in TEMPLATES if re.search(r"\.elements\[", js_code(read(p)))]
        self.assertEqual(bad, [])

    def test_pages_with_background_forms_include_the_shared_popup_once(self):
        for path in TEMPLATES:
            html = read(path)
            if "_rec_popup.html" in path:
                continue
            uses = 'class="js-rec"' in html or " js-rec" in html
            if uses:
                self.assertEqual(html.count('{% include "_rec_popup.html" %}'), 1, path)
                self.assertNotIn('id="rec-modal"', html, f"{path}: pop-up phải đến từ partial, không sao chép")

    def test_popup_partial_defines_api_once(self):
        html = read(os.path.join(ROOT, "templates", "_rec_popup.html"))
        self.assertEqual(html.count("window.RecPopup ="), 1)
        self.assertEqual(html.count('id="rec-modal"'), 1)
        self.assertTrue("form.getAttribute('action')" in html, "partial phải lấy URL bằng getAttribute('action')")

    def test_no_native_confirm_dialogs_on_pop_up_forms(self):
        for name in ("dashboard/reconcile.html", "dashboard/import_preview.html"):
            html = read(os.path.join(ROOT, "templates", name))
            form_part = html.split("LỌC TRÙNG")[0] if "reconcile" in name else html
            self.assertNotIn("onsubmit=\"return confirm(", form_part, name)
            self.assertNotIn("onclick=\"return confirm(", form_part, name)


if __name__ == "__main__":
    unittest.main()
