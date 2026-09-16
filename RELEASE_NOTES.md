## Nội dung cập nhật 1.0.4

- Tự khởi chạy lại Chromium/OneBSS nếu browser hoặc context bị đóng khi xuất Excel.
- Sau khi khởi chạy lại, chương trình kiểm tra phiên đăng nhập, cấu hình lại toàn bộ bộ lọc và thử lại một lần.
- Vẫn giới hạn một lần phục hồi mỗi chu kỳ để tránh lặp vô hạn; phiên OneBSS hết hạn vẫn được báo ngay để người dùng đăng nhập lại.

Trước mỗi lần phát hành tiếp theo, hãy sửa nội dung tệp này và tăng `APP_VERSION` trong `version.py`.
