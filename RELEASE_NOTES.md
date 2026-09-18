## Nội dung cập nhật 1.0.6

- Tách hoàn toàn đăng nhập OneBSS thành bước 1: ứng dụng chỉ mở OneBSS và chờ người dùng nhập OTP, không tự làm mới trang, không tự chuyển menu và không giới hạn thời gian chờ.
- Bước 2 mới kiểm tra phiên đăng nhập, vào màn hình Kiểm soát tồn báo hỏng CNTT, cấu hình bộ lọc và chạy quy trình. Nếu OTP chưa hoàn tất, trang được giữ nguyên để người dùng bấm lại bước 2 sau.
- Chỉ bật chức năng giữ máy thức từ khi bước 2 thực sự chạy.
- Khi trình duyệt bị đóng trong lúc xuất Excel, tăng khả năng phục hồi tối đa ba lần và không thử lại browser backend đã thất bại: trên Windows luân phiên Chromium, Google Chrome và Microsoft Edge.
- Giữ lại gói chẩn đoán Export của bản 1.0.5 để phân tích sự cố Windows/Chromium/OneBSS.

Trước mỗi lần phát hành tiếp theo, hãy sửa nội dung tệp này và tăng `APP_VERSION` trong `version.py`.
