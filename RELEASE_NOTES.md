## Nội dung cập nhật 1.1.0

- Chuyển bản Windows sang bộ cài Inno Setup và cấu trúc PyInstaller `onedir`: runtime Python, Playwright và Chromium được đặt cố định trong thư mục ứng dụng thay vì giải nén vào thư mục tạm mỗi lần chạy.
- Thêm chẩn đoán Windows cho lỗi Export: snapshot browser process trước/sau lỗi, Event Log, timeline Playwright, screenshot lỗi và trace tùy chọn.
- Có thể tự gửi gói chẩn đoán vào GitHub repository private `cuongtm88-blip/ATS-TXL-Diagnostics`; token được lưu trong Windows Credential Manager/Keychain, không nằm trong settings.json hoặc mã nguồn.
- Cho phép kiểm tra kết nối GitHub bằng nút “Kiểm tra & gửi thử”; các gói chưa gửi được sẽ được thử lại khi mở ứng dụng.
- Trace và ảnh OneBSS chỉ được tải lên khi người dùng tự bật, vì có thể chứa dữ liệu nghiệp vụ.
- Giữ nguyên luồng OTP hai bước và cơ chế phục hồi trình duyệt của bản 1.0.6.

Trước mỗi lần phát hành tiếp theo, hãy sửa nội dung tệp này và tăng `APP_VERSION` trong `version.py`.
