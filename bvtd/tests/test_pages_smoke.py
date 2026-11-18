"""Mọi trang chính phải render được (200) với dữ liệu mẫu — bắt lỗi template / route sau mỗi lần sửa."""
import unittest

from tests.helpers import AppTestCase, FakeSheet, sheet_row, TK, VL, DA_KHAM, CHUA_KHAM


class PagesSmoke(AppTestCase):
    def setUp(self):
        super().setUp()
        self.install_sheet(FakeSheet([
            sheet_row(1, "NGUYỄN THỊ A", birth="1959", phone="0912345678", hen="21/10/2026", status=DA_KHAM, actual="20/10/2026"),
            sheet_row(2, "TRẦN VĂN B", birth="N/A", phone="0", hen="22/10/2026"),
            sheet_row(3, "LÊ THỊ C", birth="1990", phone="0987654321", hen="01/10/2026", source=VL, status=DA_KHAM),
        ]))

    def test_all_get_pages_render(self):
        for url in ["/", "/benh-nhan", "/lich-kham", "/bao-cao", "/doi-chieu", "/doi-chieu?tab=vl", "/import",
                    "/bao-cao/chi-tiet?period=month&key=2026-10-01&kind=tk_att",
                    "/bao-cao/chi-tiet?period=month&key=2026-10-01&kind=all"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)

    def test_import_page_shows_version_marker(self):
        self.assertIn("v4 · 24/10/2026", self.client.get("/import").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
