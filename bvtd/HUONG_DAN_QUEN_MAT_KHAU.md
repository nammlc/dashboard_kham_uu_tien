# Mật khẩu · email đăng nhập · điện thoại xác thực (admin)

## ⚡ Bật "Lấy lại mật khẩu bằng Gmail" (3 bước, ~5 phút)
1. **Bật xác minh 2 bước** cho tài khoản Gmail dùng để gửi thư: <https://myaccount.google.com/security>. (Nên dùng 1 Gmail riêng của bệnh viện thay vì Gmail cá nhân.)
2. **Tạo “Mật khẩu ứng dụng”**: <https://myaccount.google.com/apppasswords> → đặt tên (vd. *Dashboard khám*) → Google hiện **16 ký tự** (dạng `abcd efgh ijkl mnop`) → copy. *Đây không phải mật khẩu Gmail.*
3. **Khai báo trên máy chủ** (Coolify → Environment Variables) rồi **Redeploy**:
```
GMAIL_USER=ten-gmail-cua-ban@gmail.com
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
ADMIN_BASE_URL=https://dangkyhenkham.admin.tracuuduoc-bvdktamduccauquan.com
```
4. Đăng nhập → **Tài khoản & bảo mật → thẻ “Gửi email” → 📨 Gửi email thử**. Thấy thư trong hộp thư (xem cả *Thư rác*) là xong; nút **Quên mật khẩu?** sẽ gửi liên kết qua Gmail.

Gặp lỗi? Nút gửi thử báo rõ nguyên nhân: *Gmail từ chối đăng nhập* → sai Mật khẩu ứng dụng / chưa bật xác minh 2 bước / `GMAIL_USER` không khớp; *không gửi được ... 465* → máy chủ chặn cổng 587, thêm `SMTP_SECURITY=ssl` và `SMTP_PORT=465`.
Gmail miễn phí cho gửi tối đa khoảng 500 thư/ngày — thừa cho việc lấy lại mật khẩu. Tài khoản Google Workspace do quản trị tổ chức tắt “Mật khẩu ứng dụng” thì dùng SMTP của nhà cung cấp khác (`SMTP_*` bên dưới).

Chức năng nằm ở **menu tên người dùng (góc dưới trái) → Tài khoản & bảo mật** và ở trang đăng nhập.

## Người dùng làm gì
| Tình huống | Cách làm |
|---|---|
| **Quên mật khẩu — cách 1 (cần email)** | Trang đăng nhập → *Quên mật khẩu?* → nhập email → mở email, bấm liên kết (hiệu lực **30 phút**, **1 lần**) → đặt mật khẩu mới. |
| **Quên mật khẩu — cách 2 (không cần email)** | *Quên mật khẩu?* → nhập email + **mã 6 số trong Google Authenticator** + mật khẩu mới → xong. Dùng được ngay cả khi chưa cấu hình SMTP. |
| Quên mật khẩu, đã đăng nhập được bằng Google | Đăng nhập Google (+ mã OTP) → *Tài khoản & bảo mật → Đổi mật khẩu* → **không phải nhập mật khẩu cũ**. |
| **Đổi mật khẩu** | *Tài khoản & bảo mật → Đổi mật khẩu* → nhập mật khẩu hiện tại + mật khẩu mới. **Mọi thiết bị khác bị đăng xuất**, thiết bị đang dùng ở lại. Tài khoản chỉ-Google dùng nút này để *đặt* mật khẩu lần đầu. |
| **Đổi email đăng nhập** | *Tài khoản & bảo mật → Đổi email* → nhập email mới + mật khẩu hiện tại + mã OTP hiện tại → mở **email MỚI**, bấm liên kết → bấm *Xác nhận đổi email*. Mật khẩu, 2 bước, nhật ký giữ nguyên; email cũ nhận thông báo. |
| **Đổi điện thoại, còn máy cũ** | *Tài khoản & bảo mật → Đổi điện thoại* (nhập mã máy cũ → quét QR máy mới). |
| **Mất điện thoại** | Đăng nhập bước 1 → ở trang nhập mã bấm *Mất / đổi điện thoại?* → liên kết vào email → quét QR máy mới. |
| Không có email/SMTP, mất cả mật khẩu và điện thoại | Quản trị viên chạy lệnh bên dưới. |

## Lệnh cho quản trị viên (terminal của container)
```
flask --app app admin-passwd  email                     # đổi mật khẩu
flask --app app admin-2fa-reset email                   # xoá 2 bước (lần sau quét QR lại)
flask --app app admin-email  cu@... moi@...             # đổi email đăng nhập, không cần SMTP
flask --app app mail-test    email-cua-ban@...          # thử cấu hình gửi email
flask --app app admin-list                              # xem tài khoản (có cột 2 bước)
```

## Cấu hình
```
# Gửi email (cho: liên kết quên mật khẩu, mất điện thoại, ĐỔI EMAIL, thông báo bảo mật) — điền đủ cả 3 nhóm:
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=tai-khoan@benhvien.vn
SMTP_PASSWORD=<mật khẩu ứng dụng>
SMTP_FROM=BVĐK Tâm Đức <no-reply@benhvien.vn>
SMTP_SECURITY=starttls            # starttls (587) | ssl (465) | none
ADMIN_BASE_URL=https://quanly.ten-mien-cua-ban.vn

# Quên mật khẩu bằng mã OTP (mặc định bật; cần TWO_FACTOR_ENABLED=1)
PASSWORD_RESET_VIA_OTP=1
```
- Thiếu cấu hình email: các liên kết gửi email tự ẩn; nút *Quên mật khẩu?* vẫn hiện nếu còn cách dùng mã OTP.
- `ADMIN_BASE_URL` là địa chỉ trang **admin**; liên kết trong email luôn dựng từ đây, không lấy từ header `Host`.
- Gmail: bật xác minh 2 bước rồi tạo *Mật khẩu ứng dụng*. Thử bằng `mail-test`.

## Bảo mật
- **Mã OTP chỉ dùng 1 lần**; khi đặt lại mật khẩu bằng OTP, mật khẩu mới được kiểm tra **trước** nên nhập sai độ dài không làm mất mã.
- Mọi lý do thất bại ở "quên mật khẩu" đều báo **cùng một câu** — không lộ email nào là admin. Giới hạn: 10 lần sai/15 phút/email, 20/giờ/IP; đổi mật khẩu sai mật khẩu hiện tại 5 lần/15 phút.
- **Đổi hoặc đặt lại mật khẩu đăng xuất mọi phiên cũ** (mỗi phiên ghi nhớ "vân tay" băm mật khẩu lúc đăng nhập). Phiên tạo trước bản nâng cấp được nhận một lần rồi theo dõi như bình thường.
- **Đổi email**: bắt buộc chứng minh đúng chủ (mật khẩu + OTP), liên kết gửi tới **email mới**, ký bằng `SECRET_KEY`, 30 phút, 1 lần, mất hiệu lực nếu mật khẩu/điện thoại đổi trước đó; trang xác nhận **không đổi khi chỉ mở liên kết** (tránh trình quét thư bấm hộ), phải bấm nút. Liên kết phải mở trên trình duyệt đang đăng nhập đúng tài khoản đó.
- Mọi biểu mẫu có **CSRF**; mọi bước ghi vào nhật ký truy cập (đổi mật khẩu, đổi email, đặt lại, sai mật khẩu/mã...). Đặt lại/đổi mật khẩu và đổi email gửi **thông báo** tới chủ tài khoản khi đã cấu hình email.

## Đánh đổi cần biết
- **Quên mật khẩu bằng OTP** nghĩa là ai cầm điện thoại đã mở khoá (có Google Authenticator) thì đặt lại được mật khẩu và đăng nhập được. Nếu bệnh viện muốn bắt buộc cả "biết mật khẩu + cầm điện thoại", đặt `PASSWORD_RESET_VIA_OTP=0` (chỉ còn đặt lại qua email hoặc quản trị viên).
- Đăng nhập Google chỉ dùng được nếu email đăng nhập **cũng là tài khoản Google** của người đó — đổi sang email không phải Google thì dùng mật khẩu.
- Bộ đếm giới hạn nằm trong bộ nhớ tiến trình (đủ cho 1 container).
