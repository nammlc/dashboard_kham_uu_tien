# Hướng dẫn lập chỉ mục website cho Google (và Bing)

Website: https://dangkyhenkham.tracuuduoc-bvdktamduccauquan.com
Địa chỉ gốc `/` tự chuyển vĩnh viễn (301) sang `/dang-ky`, nên trang sẽ được lập chỉ mục là `/dang-ky`.

## A. Chuẩn bị trên máy chủ (Coolify)
1. Biến môi trường của app: `PATIENT_HOST=dangkyhenkham.tracuuduoc-bvdktamduccauquan.com`, `PATIENT_HOST_PUBLIC_ONLY=1`. Redeploy.
2. Kiểm tra bằng trình duyệt (cửa sổ ẩn danh):
   - `https://dangkyhenkham.tracuuduoc-bvdktamduccauquan.com/robots.txt` → thấy `Allow: /dang-ky` và dòng `Sitemap: ...`
   - `.../sitemap.xml` → thấy `<loc>https://dangkyhenkham.tracuuduoc-bvdktamduccauquan.com/dang-ky</loc>`
   - `.../dang-ky` → mở được, bấm chuột phải → Xem nguồn trang (View source), tìm `index, follow` và `rel="canonical"`.
3. Chứng chỉ HTTPS phải hợp lệ (ổ khoá xanh), không bị Cloudflare "Under Attack / Bot Fight" chặn Googlebot.

## B. Google Search Console
1. Vào https://search.google.com/search-console , đăng nhập Gmail → **Thêm tài sản (Add property)**.
2. Chọn kiểu **Tiền tố URL (URL prefix)**, nhập đúng: `https://dangkyhenkham.tracuuduoc-bvdktamduccauquan.com/`
3. Xác minh quyền sở hữu — chọn MỘT cách:
   - **Thẻ HTML (dễ nhất, không cần đụng DNS):** Google đưa thẻ `<meta name="google-site-verification" content="ABC123...">`. Chỉ lấy phần ABC123..., đặt biến môi trường `GOOGLE_SITE_VERIFICATION=ABC123...` trên Coolify, Redeploy, rồi bấm **Xác minh**.
   - **Bản ghi DNS TXT:** vào nơi quản lý DNS của tên miền, thêm bản ghi TXT theo Google hướng dẫn, chờ vài phút rồi bấm **Xác minh**.
4. **Gửi sơ đồ trang**: menu **Sơ đồ trang web (Sitemaps)** → nhập `sitemap.xml` → **Gửi**. Trạng thái phải là "Thành công".
5. **Yêu cầu lập chỉ mục**: ô tìm kiếm trên cùng, dán `https://dangkyhenkham.tracuuduoc-bvdktamduccauquan.com/dang-ky` → Enter →
   - Nếu hiện "URL không có trên Google": bấm **Kiểm tra URL trực tiếp (Test live URL)** → khi báo "URL có thể được lập chỉ mục" thì bấm **Yêu cầu lập chỉ mục (Request indexing)**.
   - Mỗi ngày chỉ có hạn mức vài lượt yêu cầu, không cần bấm lại nhiều lần.
6. Chờ 1 – 14 ngày. Theo dõi ở **Trang (Pages)**; kiểm tra nhanh bằng cách gõ lên Google: `site:dangkyhenkham.tracuuduoc-bvdktamduccauquan.com`

## C. Bing / Cốc Cốc (tuỳ chọn)
1. https://www.bing.com/webmasters → đăng nhập → **Import from Google Search Console** (nhập thẳng, không cần xác minh lại) hoặc thêm site thủ công và dùng `BING_SITE_VERIFICATION=...` (phần content của thẻ msvalidate.01).
2. Gửi sitemap `https://dangkyhenkham.tracuuduoc-bvdktamduccauquan.com/sitemap.xml`.

## D. Giúp Google chịu lập chỉ mục nhanh hơn
- Đặt **liên kết** từ website chính của bệnh viện (hoặc fanpage, Google Business Profile / Google Maps của bệnh viện) tới trang `/dang-ky`, dùng chữ như "Đăng ký khám online". Đây là yếu tố mạnh nhất với website mới.
- Thêm nội dung thật, ngắn gọn, hiển thị trên trang: giờ khám, địa chỉ, số điện thoại bệnh viện (chỉ ghi thông tin chính xác).
- Không để trang đăng ký bị "tạm đóng" (`REG_ENABLED=0`) lúc Google đang thu thập.

## E. Lỗi thường gặp
| Báo cáo của Google | Cách xử lý |
|---|---|
| "Đã phát hiện – hiện chưa được lập chỉ mục" | Bình thường với website mới, chờ thêm; làm mục D |
| "Bị loại trừ bởi thẻ noindex" | Mở trang `/dang-ky` xem nguồn, phải là `index, follow`; kiểm tra không có header `X-Robots-Tag: noindex` (trình duyệt F12 → Network) |
| "Bị chặn bởi robots.txt" | Mở `/robots.txt`, phải có `Allow: /dang-ky`; tên miền admin cố ý `Disallow: /` |
| "Trang có chuyển hướng" cho địa chỉ gốc `/` | Bình thường — `/` chuyển 301 sang `/dang-ky` |
| Không xác minh được | Thẻ meta: đã Redeploy chưa, biến chỉ chứa phần content; DNS TXT: chờ lan truyền |
