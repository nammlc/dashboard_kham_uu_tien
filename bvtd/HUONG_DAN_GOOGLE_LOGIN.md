# Bật "Đăng nhập bằng Google" cho trang admin

Nút Google chỉ hiện khi đã khai báo `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`. Chưa khai báo thì trang đăng nhập chỉ có ô email + mật khẩu, không báo lỗi.

## 1. Tạo OAuth client trên Google (làm 1 lần)
1. Vào <https://console.cloud.google.com> → chọn (hoặc tạo) một project.
2. **APIs & Services → OAuth consent screen**: chọn *External* (hoặc *Internal* nếu dùng Google Workspace của bệnh viện), điền tên ứng dụng, email hỗ trợ. Scope chỉ cần `openid`, `email`, `profile`.
   - Nếu để ở chế độ *Testing*: thêm email các admin vào mục **Test users**. Muốn mọi tài khoản trong danh sách admin vào được mà không cần thêm từng người, bấm **Publish app**.
3. **Credentials → Create credentials → OAuth client ID → Web application**.
4. **Authorized redirect URIs** — điền đúng địa chỉ trang ADMIN (không phải trang bệnh nhân), ví dụ:
   `https://quanly.ten-mien-cua-ban.vn/login/google/callback`
   (đúng giao thức `https`, không có dấu `/` ở cuối).
5. Bấm Create → copy **Client ID** và **Client secret**.

## 2. Khai báo trên máy chủ (Coolify → Environment Variables)
```
GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxx
# chỉ cần khi báo lỗi redirect_uri_mismatch (proxy không gửi đúng tên miền):
GOOGLE_REDIRECT_URI=https://quanly.ten-mien-cua-ban.vn/login/google/callback
```
Redeploy để áp dụng.

## 3. Cho phép tài khoản nào vào được
Google chỉ **xác minh danh tính**. Ai được vào do bảng `admin_users` quyết định: vào trang quản lý admin, thêm email Gmail/Workspace của người đó (để trống mật khẩu nếu chỉ dùng Google). Email phải **đã được Google xác minh** và đang ở trạng thái bật; tắt tài khoản trong bảng là mất quyền ngay.

## Lỗi thường gặp
| Thông báo | Cách xử lý |
|---|---|
| Google báo `redirect_uri_mismatch` | Địa chỉ ở bước 1.4 chưa khớp từng ký tự với địa chỉ trang đang mở. Sửa trên Google hoặc đặt `GOOGLE_REDIRECT_URI`. |
| "Tài khoản … không được phép truy cập" | Email chưa có trong bảng admin hoặc đã bị tắt. |
| "Chưa hoàn tất được đăng nhập Google" | Mở link đăng nhập cũ, đổi trình duyệt giữa chừng hoặc mạng lỗi. Bấm đăng nhập lại. |
| Không thấy nút Google | Chưa khai báo `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`. |
| Google báo "Access blocked: app not verified" | App đang ở *Testing* mà email chưa nằm trong Test users — thêm email hoặc Publish app. |

Mọi lần đăng nhập (thành công / thất bại, kèm lý do) được ghi vào nhật ký truy cập.
