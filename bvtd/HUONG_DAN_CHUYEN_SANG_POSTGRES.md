# Chuyển dữ liệu bệnh nhân từ Google Sheet sang PostgreSQL

**Nguyên tắc:** sau khi chuyển, **PostgreSQL là nơi duy nhất giữ dữ liệu gốc**. Mọi chỉnh sửa đi qua web
(form «Thêm bệnh nhân», «Sửa», nhập Minh Lộ, đối chiếu, đăng ký online). Google Sheet chỉ còn là **bản sao một chiều**
(DB → Sheet) để xem. Không đồng bộ hai chiều nên không có chuyện hai nơi lệch nhau.

Lớp DB nằm ở `services/patient_db.py`. Biến `STORAGE_BACKEND` chọn nơi lưu:
`sheets` (mặc định, như cũ) hoặc `postgres`. Quay lại Sheet bất cứ lúc nào = đổi biến này rồi redeploy.

## 0. Chuẩn bị (10 phút)
1. Trên Google Sheet: **Tệp → Tạo bản sao** (đặt tên «Sao lưu trước khi chuyển DB + ngày»). Đây là đường lui.
2. Đã có `DATABASE_URL` (Coolify) và đã chạy `flask --app app init-db` — lệnh này giờ tạo thêm 2 bảng
   `patients` và `patient_history`.
3. Bật **backup tự động** của PostgreSQL trong Coolify (nếu chưa), và thử khôi phục một lần.

## 1. Chạy thử (KHÔNG ghi gì) — xem báo cáo đối soát
Trong Terminal của container trên Coolify:

```
flask --app app init-db
flask --app app import-sheet --report /tmp/baocao.json
```

Lệnh đọc cả Sheet, chuẩn hoá và in báo cáo, **chưa ghi gì vào DB**:
- số dòng đọc được, số dòng thiếu họ tên / sai ngày khám, STT trùng;
- số bệnh nhân theo từng trạng thái và từng tháng (so với Sheet bằng mắt);
- **cảnh báo dữ liệu**: SĐT `0`/`N/A` được đưa về trống, SĐT bất thường, năm sinh lỗi, ngày không có thật (vd. 31/02),
  trạng thái lạ… Giá trị gốc của mọi ô bị đổi/bỏ đều được **giữ lại** trong cột `extra`, không mất.

Sửa các lỗi dữ liệu nghiêm trọng trên Sheet (nếu có) rồi chạy lại cho tới khi báo cáo ổn.

## 2. Nhập thật
```
flask --app app import-sheet --commit
```
- Chỉ chạy khi bảng `patients` còn **trống** (chống nhập hai lần).
- Mỗi dòng ghi số dòng gốc trên Sheet vào `legacy_row`, và mỗi thay đổi đều có trong `patient_history`.

## 3. Đối soát (tiêu chí nghiệm thu)
1. Chạy lại `flask --app app import-sheet` (không `--commit`): kỳ vọng
   `thêm mới 0 · cập nhật 0 · giống hệt N`.
2. Trong app: đặt `STORAGE_BACKEND=postgres` trên **một bản chạy thử** (hoặc tạm thời trên bản thật vào giờ vắng), mở
   **Báo cáo**, **Lịch khám 3 ngày**, **Tổng quan** và so số liệu với bản cũ (Sheet). Hai bên phải **giống nhau**.
3. Mở ngẫu nhiên 20–30 bệnh nhân so với Sheet (họ tên, SĐT, ngày khám, trạng thái, nguồn).

## 4. Chạy song song (1–2 tuần, tuỳ chọn nhưng nên làm)
Vẫn để `STORAGE_BACKEND=sheets` (mọi người tiếp tục dùng như cũ). Mỗi đêm cập nhật DB từ Sheet:
```
flask --app app import-sheet --resync --commit
```
`--resync` thêm dòng mới và cập nhật dòng **chưa từng sửa trong DB**; dòng đã sửa trong DB không bị đụng tới.
Đặt lệnh này trong **Scheduled Tasks** của Coolify (nếu bản của bạn có) hoặc cron của máy chủ, chạy ~23:30.
Dòng «bỏ qua vì đã sửa trong DB» ≠ 0 nghĩa là ai đó đã sửa trong DB — bình thường trước ngày chuyển thì phải bằng 0.

## 5. Ngày chuyển đổi (chọn ngày vắng, không trùng khám lưu động)
1. Báo mọi người: từ giờ **không sửa trên Sheet**.
2. Trên Sheet: **Dữ liệu → Bảo vệ trang tính và dải ô** → khoá tab gốc (chỉ chủ sở hữu sửa được).
3. Nhập lần cuối: `flask --app app import-sheet --resync --commit`.
4. Đặt `STORAGE_BACKEND=postgres` trong biến môi trường của Coolify → **Redeploy**.
5. Kiểm tra nhanh: thêm 1 bệnh nhân thử bằng form, sửa, xoá, xem lịch sử.

Sau bước này mọi thao tác ghi đều vào PostgreSQL: form đăng ký online, nhập Minh Lộ, đối chiếu tái khám,
đổi trạng thái, xoá trùng. Danh sách bệnh nhân có nút **➕ Thêm bệnh nhân**, **✏️ Sửa**, **🕘 Lịch sử**.

## Trang đăng ký công khai (`/dang-ky`) ghi vào DB
Không cần sửa gì thêm: khi `STORAGE_BACKEND=postgres`, bệnh nhân bấm **Gửi** ở
`dangkyhenkham.tracuuduoc-bvdktamduccauquan.com/dang-ky` thì **1 dòng được ghi vào bảng `patients`**
(thay vì thêm dòng vào Google Sheet). Nhân viên thấy ngay trong **Bệnh nhân / Lịch khám 3 ngày**.

- Dòng mới có `source` trống, `status` trống, `khoa` trống — giống dòng Google Form trước đây, chờ nhân viên phân loại.
- Lịch sử ghi rõ nguồn: `dang-ky-online`.
- Kiểm tra trùng (cùng tên + SĐT + ngày khám) và chống bấm đúp/spam giữ nguyên.
- Nếu DB tạm lỗi: bệnh nhân thấy «Hệ thống đang bận…», **giữ nguyên những gì đã điền**, không lộ thông tin hệ thống;
  chi tiết lỗi chỉ nằm trong log của máy chủ.

**Cách chuyển đổi mà không mất đăng ký nào** (làm vào giờ vắng):
1. `flask --app app import-sheet --resync --commit` (nhập nốt các dòng mới trên Sheet).
2. Đặt `STORAGE_BACKEND=postgres` → Redeploy (khoảng 1–2 phút; trong lúc này đơn gửi vào đúng lúc đó có thể báo «đang bận» — bệnh nhân bấm gửi lại).
3. Chạy lại bước 1 **một lần nữa** để bắt các đơn lọt vào Sheet trong lúc đang redeploy.
   (Các dòng nhập thêm này giữ STT cũ của Sheet nên có thể trùng STT với dòng mới trong DB — STT chỉ để hiển thị,
   hệ thống vẫn phân biệt từng người bằng mã nội bộ.)
4. Gửi thử 1 đơn đăng ký thật bằng số của bạn, rồi xem nó có trong danh sách Bệnh nhân không.

## 6. Bản sao một chiều sang Google Sheet (để xem như quen)
```
flask --app app export-sheet
```
Ghi toàn bộ bệnh nhân vào tab «Bản sao từ CSDL (chỉ đọc)» (đổi tên bằng `SHEET_MIRROR_NAME`). Lệnh này **từ chối**
ghi vào tab gốc. Có thể đặt chạy mỗi đêm. Sửa trên tab bản sao **không có tác dụng** (lần xuất sau sẽ ghi đè).

## Các lớp bảo vệ dữ liệu
| Việc | Cách bảo đảm |
|---|---|
| Nhập sai kiểu | Ngày = DATE; trạng thái/giới tính là danh sách cố định (có `CHECK`); SĐT chỉ chữ số |
| 2 người sửa cùng 1 bệnh nhân | Cột `version`: người lưu sau được báo «vừa được người khác sửa», không ghi đè |
| Ghi nhầm người | Mọi ghi đều đối chiếu lại **họ tên (có dấu thanh)** với dòng đích |
| Xoá nhầm | Xoá **mềm**; khôi phục: `flask --app app restore-patient <mã>` |
| Biết ai sửa gì | Bảng `patient_history`: ai, lúc nào, cột nào, cũ → mới (xem 🕘 trên mỗi bệnh nhân) |
| Nhập trùng | Form cảnh báo cùng người (tên + SĐT/năm sinh, không mâu thuẫn giới tính/năm sinh) trong ±5 ngày |
| Ghi giá trị sai vào ô | Giá trị không hợp lệ bị **từ chối**, không âm thầm xoá ô |

## Quay lại Google Sheet (nếu cần)
Đặt `STORAGE_BACKEND=sheets` → Redeploy. Lưu ý: dữ liệu nhập/sửa **sau ngày chuyển** chỉ có trong DB; xuất bằng
`export-sheet` rồi chép sang tab gốc nếu thật sự muốn dùng lại Sheet.

## Kiểm thử
`python -m unittest tests.test_patient_db` — chạy bằng SQLite giả (không cần PostgreSQL): chuẩn hoá, nhập, sửa/xoá,
khoá phiên bản, xuất bản sao, và kiểm tra báo cáo/đối chiếu cho kết quả giống bản Sheet.
