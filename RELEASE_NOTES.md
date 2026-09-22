## Nội dung cập nhật 1.1.12

- Bản vận hành chính thức Windows: chọn **Google Chrome**, **Microsoft Edge**
  hoặc **Chromium tích hợp (Playwright)** ngay trên giao diện. Google Chrome là
  mặc định vì bản A/B trên máy lỗi đã cho thấy Chrome chạy ổn định hơn Chromium
  tích hợp tại bước Xuất Excel.
- Mỗi browser dùng profile OneBSS riêng; khi đổi browser, cần đăng nhập và OTP
  lại một lần trong browser vừa chọn. ATS TXL không tự luân chuyển browser khi
  có lỗi, mà giữ đúng browser người dùng đã chọn.
- Đọc duy nhất trường hết hạn (`exp`) của access token OneBSS, hiển thị đếm
  ngược thời gian còn lại trên giao diện; không ghi hoặc gửi token.
- Gửi Telegram riêng khi token còn tối đa 15 phút và khi phiên đã hết hạn. Cảnh
  báo hết hạn ghi rõ ATS TXL đã giữ browser mở, người dùng cần đăng nhập/OTP lại
  rồi bấm bước 2; không nhầm với cảnh báo browser crash.

## Nội dung cập nhật 1.1.11-diagnostic

- Bản test xác minh nguyên nhân Chromium crash tại lúc Xuất Excel; không phải
  bản vận hành chính thức và không tham gia tự cập nhật.
- Có lựa chọn A/B trong giao diện test: **Chromium tích hợp (Playwright)** hoặc
  **Google Chrome cài sẵn**. Mỗi lần chỉ dùng một browser, dừng tại lỗi đầu
  tiên, không đổi browser hoặc tự phục hồi. Khi đổi browser, người dùng phải
  đăng nhập OneBSS/OTP lại trong hồ sơ browser tương ứng.
- Playwright đang ở `1.63.0`, bản phát hành ổn định mới nhất tại thời điểm build;
  do đó bản này không tuyên bố nâng Chromium mà dùng đối chứng Chrome để cô lập
  nguyên nhân.
- `chromium-native.log` vẫn chỉ lưu cục bộ khi cần đối chiếu, nhưng đã bị loại
  khỏi mọi gói tự gửi lên GitHub vì có thể chứa dữ liệu OneBSS nhạy cảm.

## Nội dung cập nhật 1.1.10-diagnostic

- Bản riêng để truy nguyên Chromium đóng bất thường; là GitHub prerelease, không tham gia kênh tự cập nhật của các máy đang vận hành.
- Chỉ dùng Playwright Chromium và dừng ngay ở lỗi đầu tiên; không tự khởi động lại browser, đổi browser hay làm mới OneBSS để không làm mất hiện trường.
- Ghi PID, tiến trình cha, command line, profile, bộ nhớ và handle của browser lúc khởi chạy và trước/sau lỗi.
- Theo dõi `Win32_ProcessStopTrace` để lấy PID và ExitStatus khi `chrome.exe` kết thúc, kể cả khi Windows không tạo WER dump.
- Bật native Chromium log theo từng lần khởi chạy; thu chỉ phần cuối log, chỉ mục Crashpad và sao chép Crashpad report cục bộ. Crashpad dump không tự upload.
- Gói private GitHub bổ sung browser launch, process exit, native log và Crashpad index. Các dump/Crashpad có thể chứa dữ liệu OneBSS vẫn chỉ lưu tại máy test.

## Nội dung cập nhật 1.1.9-diagnostic

- Gói upload GitHub tự động bao gồm thêm `windows-extended-diagnostics.json`.
- Crash dump đầy đủ vẫn giữ tại `C:\ATS-TXL-Dumps` và trong gói cục bộ; không tự upload vì GitHub Contents API giới hạn 25 MB và dump có thể chứa dữ liệu nhạy cảm.
- Nếu cần phân tích sâu, gửi riêng dump sau khi kiểm tra quyền truy cập và nội dung.

## Nội dung cập nhật 1.1.8-diagnostic

- Tự động phát hiện và sao chép Chromium crash dump mới từ `C:\ATS-TXL-Dumps` vào gói chẩn đoán lỗi.
- Ghi số lượng và đường dẫn dump trong `summary.json`; nếu không đọc/copy được sẽ ghi `dump-copy-error.txt`.
- Vẫn là GitHub prerelease dành riêng cho máy test, không thay thế bản ổn định.

## Nội dung cập nhật 1.1.7-diagnostic

- Bản riêng cho máy test, phát hành dạng GitHub prerelease; không thay thế bản ổn định 1.1.6.
- Bộ cài bật Windows LocalDumps cho Chromium `chrome.exe`: dump đầy đủ, tối đa 5 file, lưu tại `C:\ATS-TXL-Dumps`.
- Khi gỡ bản diagnostic, bộ cài gỡ cấu hình LocalDumps và thư mục dump của bản test.
- Chỉ dùng trên máy test; dump có thể chứa dữ liệu đang hiển thị trong OneBSS và không được gửi lên GitHub nếu chưa kiểm tra nội dung.

## Nội dung cập nhật 1.1.6

- Mở rộng gói chẩn đoán Windows khi browser đóng: đọc thêm Application, System, Security, Windows Error Reporting, Defender, AppLocker, Reliability Monitor và các thư mục WER gần thời điểm lỗi.
- Bổ sung dữ liệu trước/sau lỗi để đối chiếu tiến trình Chromium/Playwright biến mất.
- Không bật crash dump hoặc thay đổi Registry tự động; ứng dụng chỉ đọc dữ liệu chẩn đoán hiện có, không cần quyền quản trị và không thay đổi chính sách Windows.
- Bản này tập trung thu thập bằng chứng, không thay đổi browser backend hay cơ chế phục hồi.

## Nội dung cập nhật 1.1.5

- Gửi cảnh báo Telegram riêng ngay khi Chromium/OneBSS đóng ngoài dự kiến và ATS TXL bắt đầu tự phục hồi, kể cả khi chu kỳ cuối cùng vẫn thành công. Cảnh báo ghi rõ số lần phục hồi và tên gói chẩn đoán; mỗi lần browser đóng sẽ có một cảnh báo.
- Việc gửi cảnh báo thất bại không cản trở mở lại trình duyệt. Gói chẩn đoán vẫn được lưu/gửi riêng như trước.
- Hai gói chẩn đoán 22/09/2026 cho thấy toàn bộ tiến trình Chromium tích hợp của ATS TXL biến mất lúc Xuất Excel; Windows Application Event Log không có sự kiện crash phù hợp. Bản này sửa điểm thiếu cảnh báo, chưa khẳng định hay sửa được nguyên nhân hệ điều hành/trình duyệt đóng tiến trình.

## Nội dung cập nhật 1.1.4

- Khi OneBSS tìm kiếm quá 10 phút hoặc không bắt đầu xử lý, ATS TXL không xuất Excel thiếu bản ghi và không kết thúc Playwright/đóng trình duyệt. Ứng dụng chờ 30 giây, làm mới tab hiện tại, cấu hình lại toàn bộ bộ lọc và thử tìm kiếm từ đầu; các lần thất bại sau tăng khoảng nghỉ đến tối đa 5 phút, tiếp tục cho tới khi thành công hoặc người dùng bấm Dừng.
- Nếu làm mới/cấu hình lại gặp lỗi mạng tạm thời, ATS TXL tiếp tục thử trong cùng browser. Nếu chính browser bị đóng từ bên ngoài, cơ chế phục hồi browser hiện có mới được sử dụng.
- Đọc đúng access token `OneBSS-Token` mà giao diện OneBSS dùng, không nhầm với refresh token. Khi token hết hạn, ứng dụng giữ browser mở, báo riêng qua Telegram và chờ người dùng đăng nhập/OTP lại rồi bấm bước 2; không tự lặp lại tìm kiếm bằng phiên đã hết hạn.
- Chỉ gửi cảnh báo riêng một lần cho chuỗi lỗi tìm kiếm kéo dài của một chu kỳ, tránh làm đầy Telegram; vẫn lưu chẩn đoán từng lần lỗi.

## Nội dung cập nhật 1.1.3

- Giữ Chromium tích hợp làm trình duyệt mặc định trên Windows; tạm dừng chuyển tự động sang Chrome/Edge. Khi trình duyệt bị đóng, ứng dụng chỉ thử mở lại cùng loại tối đa 3 lần và giữ nguyên bước đăng nhập/OTP nếu phiên đã hết hạn.
- Tách hồ sơ Chromium, Chrome và Edge trên Windows, tránh dùng chung thư mục dữ liệu giữa các trình duyệt. Lần chạy đầu tiên sau cập nhật cần đăng nhập OneBSS/OTP lại một lần; hồ sơ cũ không bị xóa.
- Phát hiện access token OneBSS đã hết hạn ngay cả khi giao diện vẫn hiện trang danh sách, không đợi 10 phút rồi báo sai là OneBSS đang xử lý. Không ghi token vào log hoặc gói chẩn đoán thông thường.
- Ẩn cửa sổ PowerShell khi thu thập tiến trình và Windows Event Log cho chẩn đoán. Cửa sổ đen từng xuất hiện khi chẩn đoán không phải bằng chứng rằng Chrome/Edge bị crash.
- Chưa thể khẳng định nguyên nhân khiến toàn bộ tiến trình trình duyệt bị Windows đóng; tiếp tục lưu sự kiện và chẩn đoán để đối chiếu sau bản vá.

## Nội dung cập nhật 1.1.2

- Mở rộng phục hồi browser cho toàn bộ chu kỳ tự động. Nếu `Target page, context or browser has been closed` xảy ra khi kiểm tra phiên, cập nhật ngày/bộ lọc, tìm kiếm hoặc xuất Excel, ATS TXL tự luân phiên Chromium → Chrome → Edge, mở lại OneBSS, áp dụng lại bộ lọc và thử tiếp thay vì dừng ngay.
- Sửa tình huống browser đóng đúng trong khoảng chờ giữa hai chu kỳ khiến bản 1.1.1 vẫn hiển thị hộp lỗi ở bước cập nhật ngày và bộ lọc.

## Nội dung cập nhật 1.1.1

- Sửa lỗi browser bị đóng trong thời gian chờ giữa hai chu kỳ tự động: trước khi bắt đầu chu kỳ mới, ứng dụng phát hiện page đã đóng, tự mở lại OneBSS, áp dụng lại bộ lọc và tiếp tục xuất Excel. Không còn dừng ngay với lỗi `Target page, context or browser has been closed` ở nhánh này.
- Phiên OneBSS hết hạn vẫn được báo riêng để người dùng đăng nhập/OTP lại; ứng dụng không tự vượt qua bước xác thực.

## Nội dung cập nhật 1.1.0

- Chuyển bản Windows sang bộ cài Inno Setup và cấu trúc PyInstaller `onedir`: runtime Python, Playwright và Chromium được đặt cố định trong thư mục ứng dụng thay vì giải nén vào thư mục tạm mỗi lần chạy.
- Thêm chẩn đoán Windows cho lỗi Export: snapshot browser process trước/sau lỗi, Event Log, timeline Playwright, screenshot lỗi và trace tùy chọn.
- Có thể tự gửi gói chẩn đoán vào GitHub repository private `cuongtm88-blip/ATS-TXL-Diagnostics`; token được lưu trong Windows Credential Manager/Keychain, không nằm trong settings.json hoặc mã nguồn.
- Cho phép kiểm tra kết nối GitHub bằng nút “Kiểm tra & gửi thử”; các gói chưa gửi được sẽ được thử lại khi mở ứng dụng.
- Trace và ảnh OneBSS chỉ được tải lên khi người dùng tự bật, vì có thể chứa dữ liệu nghiệp vụ.
- Giữ nguyên luồng OTP hai bước và cơ chế phục hồi trình duyệt của bản 1.0.6.

Trước mỗi lần phát hành tiếp theo, hãy sửa nội dung tệp này và tăng `APP_VERSION` trong `version.py`.
