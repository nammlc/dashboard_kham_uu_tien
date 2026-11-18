# SMS nhắc lịch & khám lưu động (Giai đoạn 1–3)

Mục **💬 Tin nhắn SMS** ở menu trái (chỉ hiện với email nằm trong `SMS_ADMIN_EMAILS`).

## Đã có trong bản này
- Nhắc lịch hẹn **tự động lúc 18:30** cho người có lịch **ngày mai**.
- Trang xem trước: bao nhiêu người sẽ nhận, bao nhiêu bị loại và **lý do** (đã khám, không có SĐT di động, trùng SĐT, đã từ chối…), số tin tính phí, tin mẫu.
- Chiến dịch: xem từng người nhận, trạng thái, lý do loại; nút **gửi lại tin lỗi**.
- Danh sách **từ chối nhận tin**; **gửi thử** 1 tin tới số của bạn.
- **Chế độ thử (sandbox)**: chạy trọn luồng nhưng **không gửi gì ra ngoài** — dùng cho tới khi chọn xong nhà cung cấp.
- **Mục SMS chia 2 tab con**, bấm qua lại ở đầu trang:
  - **📅 Nhắc lịch hẹn khám**: cài đặt gửi tin, xem trước nhắc ngày mai, **✏️ Sửa mẫu tin**, **✉️ Soạn & gửi tin nhắc lịch**, lịch sử gửi.
  - **🚑 Nhắc lịch khám lưu động**: **➕ Tạo đợt khám mới**, danh sách đợt (👥 Danh bạ / ✉️ Soạn tin), lịch sử gửi.
- **Sửa mẫu tin** (tab Nhắc lịch hẹn khám): sửa nội dung tin nhắc lịch, có đếm ký tự/số tin; **«Khôi phục mẫu gốc»** để quay lại.
  Mẫu mới áp dụng cho tin tự động 18:30, nút «Tạo & gửi ngay», «Gửi thử» và trang soạn tin nhắc lịch (tin đã gửi không đổi). Mẫu lưu trong DB (bảng `sms_settings`), không cần đổi biến môi trường.
- **Trang gửi 3 bước**: ① nội dung (khám lưu động thì thêm: chọn đợt + đối tượng) → ② chọn người nhận (lọc theo **ngày khám, xã/phường,
  khoa, nguồn, trạng thái, tên/SĐT**; **tích từng người**; chọn tất cả/bỏ chọn) → ③ xem trước (số người, số tin tính phí,
  chi phí ước tính, tin mẫu, lý do người bị loại) rồi **Gửi đồng loạt**. Bấm gửi 2 lần không tạo 2 chiến dịch.
- **Lọc theo vùng:** hệ thống tự tách **xã/phường** từ địa chỉ («Phú Quý Xã Trung Chính, , Tỉnh Thanh Hóa» → Xã Trung Chính). Dòng không tách
  được vào nhóm «Chưa rõ xã» — sửa ở trang **Sửa bệnh nhân** (ô «Xã/phường»). Có thể **lưu nhóm vùng** (vd. «Bắc Nông Cống») để chọn nhanh.
- **Khám lưu động**: bấm **➕ Tạo đợt khám mới** (tên, đối tượng dân/học sinh/công ty, ngày, giờ, địa điểm) →
  thêm vào **danh bạ** bằng **từng ô riêng** (Họ tên, Số điện thoại, Xã/Phường, Thôn/Đơn vị; bấm «➕ Thêm một người» hoặc Enter ở ô cuối để thêm dòng;
  có sẵn danh sách thì dán nhiều dòng hoặc tải CSV ở mục gập bên dưới) → soạn tin theo đợt.
  **Bắt buộc chọn hình thức đồng ý nhận tin** (phiếu giấy / QR / nhân viên ghi nhận) — hệ thống lưu hình thức và thời điểm.
  Khi soạn tin có thể **tích cả 2 đối tượng cùng lúc**: «Danh bạ của đợt» và «Bệnh nhân cũ theo vùng» (danh sách gộp chung; trùng SĐT thì ưu tiên người trong danh bạ).
  Mỗi SĐT chỉ nhận 1 tin cho 1 đợt.
- Chưa có: **KSK nhập từ Excel** (giai đoạn 4 — cùng bộ nhập file .xlsx cho danh bạ).

## Cấu hình (biến môi trường trên Coolify)
| Biến | Ý nghĩa |
|---|---|
| `SMS_ADMIN_EMAILS` | Email được gửi tin/cài đặt, cách nhau bằng dấu phẩy. **Để trống = không ai được gửi.** |
| `SMS_HOTLINE` | SĐT bệnh viện ghi trong tin (bắt buộc). |
| `SMS_BRANDNAME` | Tên thương hiệu, **tối đa 11 ký tự**, chữ + số không dấu, không dấu cách. |
| `SMS_PROVIDER` | `sandbox` (mặc định, chỉ thử) hoặc tên nhà cung cấp thật sau khi đã viết kết nối. |
| `SMS_STRIP_ACCENTS` | `1` (mặc định): tự bỏ dấu để vừa 160 ký tự/tin. |
| `SMS_AUTO_WINDOW` | Khung giờ cho phép chạy tự động (mặc định `17:00-21:30`). |
| `SMS_MAX_RECIPIENTS` | Tối đa người nhận mỗi lần gửi (mặc định 1000). |
| `SMS_PRICE_PER_SEGMENT` | Đơn giá/tin để ước tính chi phí (mặc định 450 đ). |

> **Brandname:** `BvdkTamDucCQ` có **12 ký tự** nên vượt giới hạn 11. Gợi ý: `BvdkTamDuc` (10), `TamDucCQ` (8) hoặc `BVTamDucCQ` (10).
> App sẽ không cho bật gửi thật nếu tên chưa hợp lệ.

## Bật dần, an toàn
1. Đặt `SMS_ADMIN_EMAILS`, `SMS_HOTLINE`, `SMS_BRANDNAME`, redeploy. Chạy `flask --app app init-db` (tạo 4 bảng `sms_*`).
2. Vào trang SMS: xem **Nhắc lịch ngày mai** — kiểm tra danh sách và tin mẫu có đúng không.
3. Bật **Bật gửi SMS** → bấm **Gửi thử** tới số của bạn. Ở chế độ sandbox tin hiện trong chiến dịch nhưng **không** ra điện thoại.
4. Khi đã có nhà cung cấp thật: đặt `SMS_PROVIDER`, gửi thử tới 2–3 số nhân viên, rồi mới bật **Tự động nhắc lịch 18:30**.
5. 1–2 tuần đầu, mỗi ngày xem lại chiến dịch tự động trước 18:30 (nút **Tạo & gửi ngay** chỉ dùng khi cron lỡ chạy).

## Cron (Coolify → Scheduled Tasks, hoặc cron của máy chủ)
Máy chủ thường chạy giờ UTC: **18:30 giờ Việt Nam = 11:30 UTC**.
```
30 11 * * *   flask --app app sms-auto-reminders     # tạo chiến dịch ngày mai + gửi
* * * * *     flask --app app sms-send-queue          # gửi nốt / gửi lại tin lỗi tạm mỗi phút
```
`sms-auto-reminders` tự bỏ qua nếu **ngoài khung 17:00–21:30** giờ Việt Nam (chống cấu hình nhầm giờ) và **mỗi ngày chỉ tạo 1 chiến dịch**
(khoá `auto:<ngày>`), nên chạy hai lần cũng không gửi đôi.

## Các lớp an toàn (mặc định đóng)
- Công tắc tổng **tắt** cho tới khi admin bật; không bật được nếu thiếu hotline hoặc brandname sai (khi dùng nhà cung cấp thật).
- Không gửi **22:00–07:00**; **hạn mức tin/ngày** (mặc định 500).
- Mỗi **SĐT chỉ 1 tin** cho 1 ngày hẹn (kể cả khi người nhà dùng chung số); lịch đã nhắc không bị nhắc lại.
- Chỉ nhắn **số di động 10 số** hợp lệ; số bàn, `N/A`, `0`, số rác bị loại.
- Tin **không rõ kết quả** (mất kết nối giữa chừng) → trạng thái `unknown`, **không tự gửi lại** — kiểm tra với nhà cung cấp rồi mới quyết.
- Chiến dịch "thật" mà chưa có nhà cung cấp thật → **giữ nguyên hàng đợi**, không đánh dấu "đã gửi" oan.
- Mọi tin lưu **đúng nội dung đã gửi**, giờ gửi, mã tin của nhà cung cấp.

## Chọn nhà cung cấp — việc cần làm song song
Hỏi 2–3 nơi (VD. SpeedSMS, VietGuys, TingTing…) và yêu cầu:
1. **Brandname CSKH** cho **cả 3 nhà mạng**, nhóm giá **y tế**; thời gian duyệt; hồ sơ cần (công văn, giấy phép KCB…).
2. **Tin có dấu** được tính mấy tin? Có hỗ trợ **gửi không dấu 160 ký tự** không?
3. **API gửi** (REST), có **báo trạng thái giao tin (DLR)** qua webhook không, có **tài khoản thử**, tài liệu API.
4. Phí duy trì Brandname, phí khởi tạo, **hoá đơn VAT** cho bệnh viện.
5. Phân loại tin **khám lưu động / KSK** là CSKH hay quảng cáo (giá và quy định khác nhau).
6. Giới hạn tốc độ gửi, cam kết uptime, hỗ trợ khi nhà mạng chặn.

Khi đã chọn, gửi mình **tài liệu API** (hoặc trang tài liệu) — mình viết lớp kết nối (khoảng 1 file nhỏ trong `services/sms.py`,
đăng ký bằng `register_provider`) cộng đường nhận trạng thái giao tin; phần còn lại không phải sửa.

## Kiểm thử
`python -m unittest tests.test_sms tests.test_sms_wizard` — chạy bằng SQLite giả + nhà cung cấp sandbox: nội dung tin, người nhận, hàng đợi,
hạn mức, giờ nghỉ, lỗi tạm/vĩnh viễn, tin không rõ kết quả, nhắc tự động 1 lần/ngày, quyền admin.
