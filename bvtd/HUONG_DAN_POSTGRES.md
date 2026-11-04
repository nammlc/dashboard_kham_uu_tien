# Hướng dẫn tạo PostgreSQL và deploy (Coolify)

Hệ thống dùng PostgreSQL cho 2 bảng: `admin_users` (tài khoản admin, mật khẩu đã băm) và `access_log` (nhật ký truy cập).
Ứng dụng **tự tạo bảng** ở lần khởi động đầu; file `schema.sql` chỉ để đọc / tạo tay khi cần.

## 1. Tạo database trên Coolify

1. Vào **Projects → (project của bạn) → Environment `production` → + New → Database → PostgreSQL**.
2. Chọn phiên bản **16** (hoặc 17). Điền:
   - **Name**: `bvtd-postgres`
   - **Postgres User**: `bvtd`
   - **Postgres Password**: một chuỗi ngẫu nhiên, nên chỉ dùng chữ + số (vd `openssl rand -hex 16`). Nếu có ký tự đặc biệt (`@ : / # ? %`) phải mã hoá trong URL (`@` → `%40`).
   - **Initial Database**: `bvtd`
3. Bấm **Save → Start**. Chờ trạng thái **Running**.
4. Ở trang database, mục **Postgres URL (internal)**: sao chép chuỗi dạng
   `postgres://bvtd:MAT_KHAU@<ten-service>:5432/bvtd`.
   Đừng bật "Make it publicly available" — app và database nằm cùng mạng nội bộ của Coolify nên không cần mở ra internet.
5. Tab **Persistent Storage**: Coolify đã tự gắn volume cho dữ liệu → dữ liệu không mất khi restart/redeploy.
6. Tab **Backups**: bật **Scheduled Backup**, ví dụ mỗi ngày `0 2 * * *`, giữ 7–14 bản (nên trỏ tới S3 nếu có).

## 2. Khai báo biến môi trường cho ứng dụng

**Application → Environment Variables**, thêm / kiểm tra:

| Biến | Giá trị |
|---|---|
| `DATABASE_URL` | URL internal ở bước 1.4 |
| `SECRET_KEY` | chuỗi ngẫu nhiên dài (vd `openssl rand -hex 32`) — **bắt buộc đổi**, không dùng giá trị mặc định |
| `FLASK_ENV` | `production` |
| `ALLOWED_EMAILS` | email admin, cách nhau bằng dấu phẩy (dùng để tạo tài khoản lần đầu + cho đăng nhập Google) |
| `ADMIN_PASSWORD` | mật khẩu cho các tài khoản tạo lần đầu (sau đó đổi bằng lệnh `admin-passwd`) |
| `PATIENT_HOST` | `dangkyhenkham.tracuuduoc-bvdktamduccauquan.com` |
| `PATIENT_HOST_PUBLIC_ONLY` | `1` |
| `SESSION_IDLE_MINUTES` | `15` (tuỳ chọn) |
| `LOG_RETENTION_DAYS` | `90` (tuỳ chọn) |

Giữ nguyên các biến cũ: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GCP_SERVICE_ACCOUNT`, `SHEET_ID`, `SHEET_NAME`...

## 3. Deploy

1. Đẩy code mới (đã có `psycopg` trong `requirements.txt`) lên Git rồi bấm **Redeploy** trên Coolify.
2. Mở **Application → Terminal** (đứng ở thư mục chứa `app.py`) và chạy:
   ```
   flask --app app init-db
   ```
   Kết quả đúng:
   ```
   Kết nối OK: PostgreSQL 16.x ...
   Các bảng đã có: access_log, admin_users
   Số tài khoản admin: N
   ```
3. Đăng nhập thử bằng email trong `ALLOWED_EMAILS` + `ADMIN_PASSWORD`. Mở menu **Hệ Thống → Nhật Ký Truy Cập** sẽ thấy dòng "Đăng nhập" vừa rồi.

## 4. Quản lý tài khoản admin (Application → Terminal)

```
flask --app app admin-list
flask --app app admin-add bacsi@benhvien.vn          # hỏi mật khẩu (>= 8 ký tự); để trống = chỉ đăng nhập Google
flask --app app admin-passwd bacsi@benhvien.vn
flask --app app admin-disable bacsi@benhvien.vn      # người đó bị đăng xuất ở lần thao tác kế tiếp
flask --app app admin-enable bacsi@benhvien.vn
```
Sau lần chạy đầu, **đổi `ADMIN_PASSWORD` trong biến môi trường không còn tác dụng** — dùng `admin-passwd`.

## 5. Xem / sao lưu dữ liệu

- Database → **Terminal**: `psql -U bvtd -d bvtd` rồi
  ```sql
  \dt
  SELECT count(*) FROM access_log;
  SELECT ts, event, email, path FROM access_log ORDER BY ts DESC LIMIT 20;
  ```
- Sao lưu tay: `pg_dump -U bvtd -d bvtd -Fc -f bvtd.dump` — khôi phục: `pg_restore -U bvtd -d bvtd --clean bvtd.dump`.

## 6. Chạy thử trên máy cá nhân

```
docker run -d --name bvtd-pg -e POSTGRES_USER=bvtd -e POSTGRES_PASSWORD=bvtd_pw -e POSTGRES_DB=bvtd -p 5432:5432 postgres:16
export DATABASE_URL=postgresql://bvtd:bvtd_pw@localhost:5432/bvtd
flask --app app init-db
flask --app app admin-add admin@bvtd.vn
flask --app app run
```
Chạy test đầy đủ: tạo thêm 1 database riêng `bvtd_test` rồi `export TEST_DATABASE_URL=postgresql://bvtd:bvtd_pw@localhost:5432/bvtd_test`
(bảng trong database này bị xoá sạch mỗi test — **đừng trỏ vào database thật**). Không đặt biến này thì các test cần DB tự bỏ qua.

## 7. Xử lý lỗi thường gặp

| Triệu chứng | Nguyên nhân / cách xử lý |
|---|---|
| Trang đăng nhập báo "thiếu DATABASE_URL" | Chưa đặt biến `DATABASE_URL` → thêm rồi Redeploy |
| "Hệ thống chưa kết nối được cơ sở dữ liệu" | Database chưa Running; sai host/user/mật khẩu; app và database khác mạng (Destination) trong Coolify; mật khẩu có ký tự đặc biệt chưa mã hoá URL. Chạy `flask --app app init-db` để xem lỗi chi tiết |
| `password authentication failed` | Sai mật khẩu trong URL (nếu đổi mật khẩu sau khi tạo database, hãy đặt lại cho khớp) |
| `could not translate host name` | Dùng URL **internal** của đúng database, và app / database cùng Destination |
| Đăng nhập được nhưng mất dữ liệu sau khi deploy | Không dính tới app: dữ liệu nằm ở volume của database; kiểm tra tab Persistent Storage của database |
| Không vào được admin sau khi chuyển sang DB | Chạy `flask --app app admin-list`; nếu bảng trống hãy `admin-add` tài khoản mới |
