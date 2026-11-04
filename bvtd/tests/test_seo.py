"""Lập chỉ mục: tên miền gốc → /dang-ky (301), robots.txt, sitemap.xml, thẻ meta cho công cụ tìm kiếm."""
import re
from unittest import mock

from tests.helpers import A, AppTestCase

PATIENT = "dangkyhenkham.tracuuduoc-bvdktamduccauquan.com"
ADMIN = "dangkyhenkham.admin.tracuuduoc-bvdktamduccauquan.com"


class SeoTests(AppTestCase):
    def setUp(self):
        super().setUp()
        for target, val in (("_PATIENT_HOSTS", {PATIENT}), ("_PATIENT_PUBLIC_ONLY", True)):
            p = mock.patch.object(A, target, val); p.start(); self.addCleanup(p.stop)
        A.current_user = type("Anon", (), {"is_authenticated": False})()

    def get(self, path, host=PATIENT):
        return self.client.get(path, base_url=f"https://{host}")

    def test_root_redirects_permanently_to_register(self):
        r = self.get("/")
        self.assertEqual(r.status_code, 301); self.assertTrue(r.headers["Location"].endswith("/dang-ky"))

    def test_register_page_is_indexable(self):
        r = self.get("/dang-ky"); h = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200); self.assertIsNone(r.headers.get("X-Robots-Tag"))
        self.assertIn('content="index, follow"', h)
        self.assertIn(f'<link rel="canonical" href="https://{PATIENT}/dang-ky">', h)
        self.assertIn('property="og:title"', h); self.assertIn('name="description"', h)

    def test_robots_and_sitemap_for_patient_host(self):
        t = self.get("/robots.txt").get_data(as_text=True)
        self.assertIn("Allow: /dang-ky", t); self.assertIn(f"Sitemap: https://{PATIENT}/sitemap.xml", t)
        x = self.get("/sitemap.xml"); body = x.get_data(as_text=True)
        self.assertEqual(x.mimetype, "application/xml"); self.assertIn(f"<loc>https://{PATIENT}/dang-ky</loc>", body)

    def test_admin_host_is_blocked_from_search_engines(self):
        self.assertIn("Disallow: /", self.get("/robots.txt", ADMIN).get_data(as_text=True))
        self.assertIn("noindex", self.get("/login", ADMIN).headers.get("X-Robots-Tag", ""))

    def test_verification_meta_tags_from_env(self):
        self.assertNotIn("google-site-verification", self.get("/dang-ky").get_data(as_text=True))
        A.app.config.update(GOOGLE_SITE_VERIFICATION="abc123TOKEN", BING_SITE_VERIFICATION="bing456")
        self.addCleanup(A.app.config.update, GOOGLE_SITE_VERIFICATION="", BING_SITE_VERIFICATION="")
        h = self.get("/dang-ky").get_data(as_text=True)
        self.assertIn('<meta name="google-site-verification" content="abc123TOKEN">', h)
        self.assertIn('<meta name="msvalidate.01" content="bing456">', h)
