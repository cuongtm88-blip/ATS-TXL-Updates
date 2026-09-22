# ATS TXL

Ứng dụng điều phối quy trình:

1. Mở trình duyệt Chromium tương thích với Playwright bằng hồ sơ riêng và đăng
   nhập OneBSS thủ công.
2. Mở đúng mục `Kiểm soát viên - Kiểm soát tồn báo hỏng CNTT` nằm trong
   `Chăm sóc khách hàng` (đây là một tên menu hoàn chỉnh, không phải ba menu con).
3. Chọn ngày, trạng thái, đơn vị và vùng theo cấu hình trong ảnh mẫu.
4. Tìm kiếm, chờ tải dữ liệu, xuất Excel.
5. Xử lý file bằng mã nguồn MonitorTXL và gửi Telegram trực tiếp qua Bot API.
6. Tự động lặp lại toàn bộ quy trình theo số phút được nhập trên giao diện.

## Chạy trên macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python app.py
```

macOS không cần chạy `MonitorTXL-1.exe`; ứng dụng dùng trực tiếp mã nguồn `TXL_Monitor_Tele_Group_All_Over10.py`, xuất báo cáo vào `downloads/` và gửi Telegram qua Bot API.

Nhập `Telegram Bot token`, `Group chat ID` và chu kỳ chạy ngay trên cửa sổ ứng
dụng trước khi bấm `2. Chạy quy trình`. Cấu hình được lưu trong thư mục dữ liệu
riêng của người dùng và tự điền ở lần mở sau. Token được che trên giao diện,
nhưng vẫn là dữ liệu nhạy cảm được lưu cục bộ trên máy; không chia sẻ tệp
`settings.json` cho người khác.

### Cảnh báo lỗi riêng

Mỗi người nhận chỉ cần mở đúng bot Telegram và gửi `/start` hoặc một tin nhắn
mới. Trên ứng dụng, bấm `Lấy Chat ID`, chọn đúng tên Telegram, bấm
`Thêm người đã chọn`, rồi bấm `Gửi thử`. Không cần dùng Terminal. Tại ô
`Chat ID nhận cảnh báo lỗi`, có thể giữ một hoặc nhiều Chat ID cách nhau bằng
dấu phẩy. Khi quy trình
gặp lỗi, trình duyệt bị đóng hoặc phiên đăng nhập OneBSS hết hạn, bot sẽ gửi riêng
thời gian, tên máy, hệ điều hành, giai đoạn và nội dung lỗi đến các Chat ID này.
Thao tác bấm `Dừng` chủ động không phát sinh cảnh báo lỗi.

Các lỗi không được xử lý từ giao diện ứng dụng cũng được gửi theo cùng danh sách.
Trường hợp máy mất mạng, mất điện, tiến trình bị hệ điều hành buộc dừng hoặc bot
token không còn hợp lệ thì ứng dụng không thể tự gửi cảnh báo Telegram.

Mỗi người nhận phải mở cuộc trò chuyện riêng với đúng bot Telegram và bấm
`Start` hoặc gửi `/start` ít nhất một lần; nếu chưa làm bước này, Telegram sẽ
không cho bot chủ động gửi tin nhắn riêng.

Chỉ cần bấm nút `2. Chạy quy trình` một lần. Nếu bật `Tự động lặp sau`, các chu
kỳ tiếp theo sẽ tự tìm kiếm, chờ OneBSS tải xong và xuất Excel mà không cần bấm
thêm nút. Telegram chỉ được gửi khi có ít nhất một phiếu Tiền xử lý báo hỏng có
thời gian xử lý từ 10 phút trở lên; nếu không có, ứng dụng chỉ ghi nhật ký và
chuyển sang chu kỳ tiếp theo.

### Giữ máy thức khi chạy

Chọn `Giữ máy thức khi chạy` để tránh việc máy tự Sleep trong lúc chờ chu kỳ tự
động. Trên macOS, ứng dụng gọi `caffeinate -ims`; trên Windows, ứng dụng dùng yêu
cầu ngăn Sleep gốc của hệ điều hành. Chế độ này được lưu theo từng máy, hoạt động
từ khi bấm bước `2. Cấu hình & chạy` đến khi bấm `Dừng`, phiên làm việc kết thúc
hoặc đóng ứng dụng. Chế độ này không ngăn đăng
xuất, tắt máy, khởi động lại hay Sleep do đóng nắp laptop.

## Đóng gói Windows

Chép toàn bộ thư mục mã nguồn sang Windows rồi chạy `build_windows.bat`.

File build sẽ:

- Tự cài Python 3.12 bằng `winget` nếu máy chưa có Python.
- Tự tạo môi trường, cài thư viện, Chromium tương thích và PyInstaller.
- Đóng gói mã MonitorTXL cùng Chromium vào ứng dụng.

Kết quả là `dist\ATS-TXL-Setup.exe`. Chạy bộ cài này trên máy đích; ứng dụng sẽ
được cài cố định vào `Program Files\ATS TXL`. Máy đích không cần Python, Chrome,
extension hay `MonitorTXL-1.exe`. Python runtime, Playwright và Chromium được
đặt trong thư mục ứng dụng thay vì giải nén vào thư mục tạm mỗi lần chạy.

## Tự cập nhật Windows qua GitHub Releases

Kênh cập nhật công khai của ứng dụng là:
<https://github.com/cuongtm88-blip/ATS-TXL-Updates/releases>

Bản đã cài trên Windows tự kiểm tra cập nhật sau khi mở và kiểm tra lại mỗi 6 giờ.
Người dùng cũng có thể bấm `Kiểm tra cập nhật` trên giao diện. Khi có phiên bản
mới, ứng dụng tải bộ cài, kiểm tra SHA-256, rồi mở bộ cài để cập nhật đè an toàn.

Token Telegram, Chat ID, chu kỳ lặp và danh sách người nhận cảnh báo được lưu
trong thư mục dữ liệu riêng của người dùng, không nằm trong thư mục cài đặt. Vì
vậy việc cập nhật không làm mất cấu hình. GitHub token chẩn đoán được lưu trong
Windows Credential Manager, không ghi vào `settings.json`.

Từ bản 1.1.3, Windows dùng hồ sơ Chromium mới, tách biệt Chrome/Edge; lần đầu
sau cập nhật cần đăng nhập OneBSS và nhập OTP lại. Khi OneBSS hết phiên, ứng dụng
cảnh báo riêng và dừng xuất báo cáo cho tới khi người dùng xác thực lại; bật
"Giữ máy thức" không kéo dài phiên đăng nhập của OneBSS.

Mỗi lần phát hành bản mới trên máy Windows dùng để build:

1. Sửa mã nguồn cần thiết ở thư mục `ATS-TXL-Windows`.
2. Tăng `APP_VERSION` trong `version.py`, ví dụ `1.0.0` thành `1.0.1`.
3. Sửa `RELEASE_NOTES.md` để mô tả thay đổi.
4. Chạy `build_windows.bat`. Script tạo `dist\ATS-TXL-Setup.exe` và
   `dist\ATS-TXL-Setup.exe.sha256`.
5. Chạy `publish_windows_release.bat`, nhập `PHAT HANH` khi được hỏi. Lần đầu,
   script tự cài GitHub CLI nếu cần và mở trình duyệt để đăng nhập GitHub.

Repository công khai chứa mã nguồn và các bản phát hành. Các tệp cấu hình cục
bộ, `settings.json`, token và Chat ID không được đưa lên GitHub. Mỗi phiên bản
phát hành cần tăng `APP_VERSION` để máy cài đặt nhận ra bản mới.

### Build tự động bằng GitHub Actions

Repository công khai hiện cũng chứa mã nguồn ATS TXL và workflow Windows. Không
cần máy Windows để phát hành: sau khi đẩy thay đổi và tăng `APP_VERSION`, vào
tab **Actions** trên GitHub, chọn **Build and release Windows**, bấm **Run
workflow**. GitHub sẽ tạo `ATS-TXL-Setup.exe`, SHA-256 và GitHub Release tương ứng.
Workflow chỉ phát hành các tài sản EXE/SHA-256; các tệp cấu hình cục bộ, token,
Chat ID, báo cáo Excel và Chrome profile bị loại trừ qua `.gitignore`.

## Lưu ý

- Không lưu tài khoản/mật khẩu OneBSS. Token Telegram được lưu cục bộ theo yêu
  cầu để tự điền ở lần mở sau.
- Website có thể thay đổi tên/selector; nếu thay đổi, cần cập nhật các nhãn trong `app.py`.
- Cả macOS và Windows đều dùng trực tiếp mã nguồn MonitorTXL; không cần điều
  khiển giao diện `MonitorTXL-1.exe` để tải file và gửi Telegram.
- Có thể khóa màn hình trong lúc chạy; máy vẫn phải duy trì trạng thái thức.
