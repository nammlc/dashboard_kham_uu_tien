# Quên mật khẩu · Mất / đổi điện thoại (admin)

## Người dùng làm gì
| Tình huống | Cách làm |
|---|---|
| **Quên mật khẩu** | Trang đăng nhập → **Quên mật khẩu?** → nhập email → mở email, bấm liên kết (hiệu lực **30 phút**, dùng **1 lần**) → đặt mật khẩu mới (≥ 8 ký tự). Sau đó đăng nhập như thường (vẫn cần mã OTP). |
| **Đổi điện thoại, còn dùng được máy cũ** | Đăng nhập → menu tên người dùng (góc dưới trái) → **Tài khoản & bảo mật → Đổi điện thoại** → nhập mã OTP của máy cũ → quét QR bằng máy mới → nhập mã mới. Máy cũ ngừng dùng được ngay. |
| **Mất điện thoại** | Đăng nhập bước 1 (mật khẩu hoặc Google) → ở trang nhập mã OTP bấm **Mất / đổi điện thoại? Gửi liên kết vào email** → mở liên kết → quét QR bằng máy mới. Mở email ở máy khác thì phải nhập mật khẩu; tài khoản chỉ dùng Google phải đăng nhập Google ở đúng trình duyệt đó trước. |
| Không có email / SMTP chưa cấu hình | Quản trị viên chạy `flask --app app admin-passwd email` (đổi mật khẩu) hoặc `admin-2fa-reset email` (xoá 2 bước, lần sau quét QR lại). |

## Bật gửi email (làm 1 lần trên Coolify → Environment Variables)
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=tai-khoan@benhvien.vn
SMTP_PASSWORD=<mật khẩu ứng dụng>
SMTP_FROM=BVĐK Tâm Đức <no-reply@benhvien.vn>
SMTP_SECURITY=starttls          # starttls (587) | ssl (465) | none
ADMIN_BASE_URL=https://quanly.ten-mien-cua-ban.vn
```
- **Phải có cả `SMTP_HOST`, người gửi (`SMTP_FROM` hoặc `SMTP_USER` dạng email) và `ADMIN_BASE_URL`**, thiếu một thì các liên kết "Quên mật khẩu" / "Mất điện thoại" tự ẩn (không báo lỗi).
- `ADMIN_BASE_URL` là địa chỉ trang **admin** — liên kết trong email luôn dựng từ giá trị này, không lấy từ header `Host` (chống giả mạo địa chỉ để đánh cắp liên kết).
- Gmail: bật xác minh 2 bước rồi tạo **Mật khẩu ứng dụng** (App password); Google Workspace / Brevo / SendGrid / Mailgun dùng thông tin SMTP họ cấp.
- Thử: `flask --app app mail-test email-cua-ban@gmail.com` (báo rõ lỗi đăng nhập / kết nối nếu có).

## Cơ chế bảo vệ
- Liên kết do hệ thống **ký bằng `SECRET_KEY`**, không lưu trong DB, hiệu lực 30 phút. Dùng xong (mật khẩu hoặc mã 2 bước đổi) thì **mọi liên kết cũ tự vô hiệu**. Liên kết đặt mật khẩu không dùng được cho đổi điện thoại và ngược lại.
- **Không lộ email nào là admin**: trả lời luôn giống nhau dù email có hay không; email gửi ở luồng nền nên thời gian phản hồi cũng không khác nhau. Tài khoản chỉ-Google và tài khoản bị tắt không nhận email.
- Giới hạn **3 yêu cầu / giờ / email** và **20 / giờ / IP** cho mỗi loại.
- Các biểu mẫu đổi thông tin đăng nhập đều có **mã CSRF**.
- Đặt lại mật khẩu **không bỏ qua** mã OTP; đổi điện thoại khi mất máy cần **cả email lẫn bước 1** (mật khẩu/Google).
- Đổi điện thoại khi đã đăng nhập luôn bắt nhập **mã OTP hiện tại** (mỗi mã dùng 1 lần), rồi nhập thử mã của máy mới mới đổi.
- Nhật ký truy cập ghi: yêu cầu gửi email, đặt lại mật khẩu, đổi điện thoại, nhập sai mật khẩu/mã.

## Lưu ý
- Đặt lại mật khẩu **không đăng xuất** các phiên đang mở ở máy khác (phiên tự hết sau 15 phút không thao tác). Nghi bị lộ tài khoản thì đổi `SECRET_KEY` (mọi phiên và liên kết đang có đều mất hiệu lực; mọi admin quét lại QR 2 bước một lần).
- Bộ đếm giới hạn nằm trong bộ nhớ của tiến trình (đủ cho 1 container).
