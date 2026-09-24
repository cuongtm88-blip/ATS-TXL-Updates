# OneBSS grid data mapping

This mapping is for the deep-diagnostic grid reader only. It does not change
production Excel export or invoke Telegram processing.

The reader preserves every UI column in `ui_record`. It creates a
`canonical_record` only for mappings marked `MAPPED`; ambiguous and unknown
fields remain available in `ui_record` and are not silently collapsed.

| # | OneBSS UI header | Canonical field | Mapping status |
|---:|---|---|---|
| 1 | Tỉnh | `tentinh` | MAPPED |
| 2 | Mã thuê bao | `ma_tb` | MAPPED |
| 3 | Tên thuê bao | `ten_tb` | MAPPED |
| 4 | Loại hình TB | `loaihinh_tb` | MAPPED |
| 5 | Đơn vị nhận | `ten_dv` | MAPPED |
| 6 | Đơn vị xử lí | `ten_dv_xl` | MAPPED |
| 7 | Đơn vị đang thực hiện | `ten_dv_dang_th` | MAPPED |
| 8 | Ngày báo hỏng | `ngay_bh` | MAPPED |
| 9 | SLA | `sla` | MAPPED |
| 10 | Người giữ phiếu | `ma_nd` | MAPPED |
| 11 | Người báo hỏng | `nguoi_cn` | AMBIGUOUS |
| 12 | SĐT BH | `dienthoai_bh` | MAPPED |
| 13 | Điện thoại liên hệ | `dienthoai_lh` | MAPPED |
| 14 | Trạng thái bảo hỏng | `trangthai_bh` | MAPPED |
| 15 | Nhân viên | `ten_nv` | MAPPED |
| 16 | Nội dung hỏng | `ghichu_hong` | MAPPED |
| 17 | Số ảo | — | UNKNOWN |
| 18 | Mã báo hỏng | `ma_bh` | MAPPED; required unique key |
| 19 | Kênh tiếp nhận | `kenh_tn` | MAPPED |
| 20 | Địa chỉ lắp đặt | `diachi_ld` | MAPPED |
| 21 | Máy cập nhật | `may_cn` | MAPPED |
| 22 | Ngày cập nhật | `ngay_cn` | MAPPED |
| 23 | Người cập nhật | `nguoi_cn` | MAPPED |
| 24 | Quy trình | `ten_quytrinh` | MAPPED |
| 25 | Trạng thái xử lý | `ten_trangthai` | MAPPED |

## Header aliases

Header comparison applies Unicode NFC normalization, case folding, and
whitespace collapsing. These observed spelling variants are accepted:

- `Đơn vị nhận` / `Đơn vị nhân`
- `Đơn vị xử lí` / `Đơn vị xử lý`
- `Trạng thái bảo hỏng` / `Trạng thái báo hỏng`

Aliases resolve to the canonical UI header above. They do not alter cell
values.

## Ambiguity and record shape

- `ui_record` contains all 25 canonical UI header keys, including `Số ảo` and
  both person columns.
- `canonical_record` excludes `Người báo hỏng` because its proposed target
  `nguoi_cn` is also used by `Người cập nhật`. The latter supplies canonical
  `nguoi_cn`; the former remains intact in `ui_record` until disambiguated.
- `Số ảo` is retained in `ui_record` and has no canonical field until mapped.
- Record values exist only in memory during the diagnostic. The report and
  logs contain aggregate metadata only, never row values or `ma_bh` values.

## Diagnostic acceptance checks

The reader fails closed if a required header is missing or duplicated, any UI
cell cannot be read, the total cannot be read, row count differs from the
OneBSS total, `ma_bh` is missing/duplicated, or the unique `ma_bh` count differs
from the total. A failed read does not return partial records.
