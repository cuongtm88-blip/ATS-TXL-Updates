## Nội dung cập nhật 1.0.5

- Lưu gói chẩn đoán cục bộ cho lỗi Export: sự kiện Playwright, trace thao tác/network và Windows Application Event Log.
- Phân biệt được page crash/page close, browser disconnected, lỗi JavaScript, request lỗi và dấu vết crash do Windows.
- Khi Chromium bị đóng trên Windows, tự chuyển sang Chrome rồi Microsoft Edge cài sẵn trước khi thử lại bằng Chromium đóng gói.
- Vẫn giới hạn một lần phục hồi mỗi chu kỳ; phiên OneBSS hết hạn vẫn được báo ngay để người dùng đăng nhập lại.
- Khóa phiên bản Playwright/Chromium đã kiểm thử để các bản phát hành sau không tự đổi browser runtime.

Trước mỗi lần phát hành tiếp theo, hãy sửa nội dung tệp này và tăng `APP_VERSION` trong `version.py`.
