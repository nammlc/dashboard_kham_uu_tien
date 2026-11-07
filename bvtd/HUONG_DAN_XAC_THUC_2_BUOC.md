# Xác thực 2 bước (mã OTP Google Authenticator) cho trang admin

Sau khi đăng nhập bằng **mật khẩu hoặc Google**, admin phải nhập thêm **mã 6 số** trong ứng dụng Google Authenticator (hoặc Microsoft Authenticator, Authy…). Chưa nhập đúng mã thì chưa vào được trang quản trị.

## Lần đầu của mỗi admin
1. Đăng nhập như bình thường → hệ thống hiện trang **Thiết lập xác thực 2 bước** với mã QR.
2. Trên điện thoại: mở Google Authenticator → **+ → Quét mã QR** (hoặc nhập khoá thủ công / bấm liên kết mở ứng dụng nếu đang dùng chính điện thoại đó).
3. Nhập mã 6 số vừa hiện ra → xong, vào hệ thống luôn. Từ lần sau chỉ cần nhập mã.

## Triển khai
- Cài thêm thư viện: `pip install -r requirements.txt` (thêm `pyotp`, `qrcode`, `cryptography`).
- Cơ sở dữ liệu: 3 cột mới trong `admin_users` (`totp_secret`, `totp_enabled_at`, `totp_last_step`) **tự được thêm** khi ứng dụng khởi động (không mất dữ liệu). Cũng có trong `schema.sql` nếu muốn chạy tay.
- Mặc định **bật** cho mọi admin. Muốn tắt khẩn cấp: đặt `TWO_FACTOR_ENABLED=0` rồi redeploy.
- **Không đổi `SECRET_KEY`** sau khi triển khai: mã bí mật được mã hoá bằng khoá dẫn xuất từ `SECRET_KEY`; nếu đổi, mọi admin sẽ phải quét QR lại một lần (không bị khoá, chỉ phải thiết lập lại).

## Mất / đổi điện thoại
Quản trị viên khác (hoặc người có quyền terminal máy chủ) chạy:
```
flask --app app admin-2fa-reset email@benhvien.vn
```
Lần đăng nhập tới của người đó sẽ hiện mã QR mới. `flask --app app admin-list` cho biết ai đã thiết lập (`2 bước: đã thiết lập / chưa`).
> Admin duy nhất bị mất điện thoại: chạy lệnh trên trong terminal của container, hoặc `UPDATE admin_users SET totp_secret='', totp_enabled_at=NULL, totp_last_step=0 WHERE email='...';`

## Cơ chế bảo vệ
- Chuẩn TOTP (RFC 6238), mã đổi mỗi 30 giây, chấp nhận lệch đồng hồ ±30 giây.
- **Mỗi mã chỉ dùng được 1 lần** (kẻ nhìn lén mã vừa dùng cũng không vào được).
- Sai quá **5 lần** trong một lượt đăng nhập → phải đăng nhập lại từ đầu; sai **10 lần / 15 phút** cho một email → tạm khoá.
- Từ lúc đúng mật khẩu/Google đến lúc nhập mã tối đa **5 phút**.
- Tài khoản **đã thiết lập không thể tự thiết lập lại** qua trang web (người biết mật khẩu không thể thay bằng điện thoại của họ) — chỉ lệnh `admin-2fa-reset` mới đặt lại được.
- Mã bí mật lưu trong DB ở dạng **mã hoá**; mọi lần nhập mã sai / thiết lập mới đều có trong nhật ký truy cập.
