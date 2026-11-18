# video-tracker

Theo dõi việc học viên đăng video TikTok/YouTube hằng tuần (tuần thứ Hai → Chủ nhật, giờ `Asia/Ho_Chi_Minh`). Dành cho một nhóm học viên và hai người theo dõi. Bản cũ (thử nghiệm một trang web) nằm ở `legacy/README.md`.

## Là gì / không là gì

Là:
- Công cụ **chỉ theo dõi**: học viên khai kênh, công cụ đọc video công khai của kênh, đếm trong tuần, so với mục tiêu, tạo báo cáo.
- CLI `tracker`, API HTTP nhỏ, máy chủ MCP chỉ-đọc, lịch đồng bộ và báo cáo chạy nền.

Không là:
- **Không có trang để học viên nộp bài.** Học viên không đăng nhập hay nộp gì cả.
- **Không xác minh chủ sở hữu kênh.** Link kênh do giáo viên khai báo; công cụ tin nó.
- **Không bao giờ kết luận "chưa đăng" khi dữ liệu chưa đủ.** Thiếu dữ liệu thì trạng thái là "Chưa đủ dữ liệu để kết luận".
- `yt-dlp` là công cụ **không chính thức**, đọc dữ liệu web công khai; nền tảng đổi giao diện hay chặn IP là có thể hỏng bất cứ lúc nào.
- Không có giao diện web.

## Trạng thái kiểm chứng

| Hạng mục | Mức | Ghi chú |
|---|---|---|
| Test tự động | kiểm thử cục bộ | Chạy `python3 -m pytest -q`; test dùng DB tạm và mock, không gọi mạng thật |
| Lịch sử chỉ số từng video | đã thử trên Mac | `yt-dlp` thu hai kênh thật, lưu ảnh chụp vào DB tạm; chênh lệch giữa các ngày/tuần đã kiểm bằng dữ liệu giả, chưa chờ qua ngày để kiểm thật |
| TikTok và YouTube qua yt-dlp, báo cáo text, CLI | đã chạy thật trên Mac trước bản sửa nghiệm thu | đồng bộ kênh thật, `progress`, `report`; chưa chạy lại kênh thật sau thay đổi quy tắc trạng thái |
| yt-dlp từ VPS (16/11/2026) | chỉ thử tay bằng yt-dlp | TikTok đọc được; YouTube bị chặn "Sign in to confirm you're not a bot" → YouTube cần `collect-push` từ Mac hoặc `YOUTUBE_API_KEY`. Chưa chạy chính `tracker` trên VPS |
| HTTP API và MCP qua HTTP | đã chạy thật trên Mac trước bản sửa nghiệm thu | `tracker serve` thật: kiểm ma trận token, `initialize`, `tools/list` (4 tool chỉ đọc), `tools/call week_progress`; chưa chạy lại server thật sau bản sửa |
| Nhận xét AI qua OpenAI thật | chưa kiểm | chỉ test bằng mock |
| Gửi Zalo qua zalo_mod | chưa kiểm | chỉ mock; dạng endpoint lấy từ việc đọc repo OpenClaw |
| YouTube Data API (`YOUTUBE_API_KEY`) | chưa kiểm | chỉ mock |
| Docker image (build/chạy) | đã chạy thật bản trước trên Mac (17/11/2026) | Trước bản sửa nghiệm thu: build được, container read-only chạy và 523 test pass trong image. Bản hiện tại chưa build/chạy lại; `docker compose up` và VPS chưa kiểm |
| Kết nối ChatGPT (connector) | chưa kiểm, hiện KHÔNG dùng được | ChatGPT cần OAuth, **chưa cài**. `/mcp` dùng bearer token tĩnh: chạy được với Claude Code / Codex / MCP Inspector, không với app ChatGPT |
| Triển khai | chưa làm | chưa có gì chạy trên VPS; chủ dự án tự triển khai |

### Kết quả build Docker

Build và chạy thử ngày 17/11/2026 trên Mac (`docker build -t video-tracker:dev .`). Lần build đầu lộ lỗi: pip kéo `mcp` 2.x (đổi API) làm container sập khi khởi động; đã chốt `mcp>=1.27,<2` cùng `httpx`, `starlette<1`, `uvicorn`, `pydantic>=2.11` trong `pyproject.toml`. Đừng nới các ràng buộc này khi chưa chạy lại test.

## Cài đặt local

Cần Python >= 3.10.

```bash
cd tracker
python3 -m venv .venv && source .venv/bin/activate
pip install -e .          # kéo theo mcp và yt-dlp
tracker --help            # hoặc: python3 -m video_tracker --help
```

Không cài cũng chạy được từ thư mục `tracker/`: `python3 -m video_tracker <lệnh>` (cần đã cài `mcp`, `yt-dlp`).

Dữ liệu mặc định ở `~/.video-tracker/tracker.sqlite3`; đổi bằng `TRACKER_DB` hoặc cờ `--db PATH` (đặt trước tên lệnh: `tracker --db ./t.sqlite3 list`).

### Bắt đầu nhanh

```bash
tracker add-student "Lan" --goal 3 --tiktok https://www.tiktok.com/@lan.demo --youtube https://www.youtube.com/@landemo
tracker sync              # đọc kênh của mọi học viên (tối đa TRACKER_SYNC_LIMIT video mỗi kênh)
tracker progress          # tuần hiện tại
tracker report            # báo cáo tuần trước (chỉ in ra, không gửi)
```

Dạng kết quả (ví dụ minh họa, số liệu không phải dữ liệu thật):

```
Đã thêm học viên S01.
S01  Lan  mục tiêu 3/tuần  TikTok: https://www.tiktok.com/@lan.demo | YouTube: https://www.youtube.com/@landemo

S01 Lan · TikTok: ok, đọc 12, thêm mới 12
S01 Lan · YouTube: lỗi, đọc 0, thêm mới 0 — Nền tảng yêu cầu xác minh (chặn theo IP)
Tổng: 2 lượt (ok 1, một phần 0, lỗi 1), thêm 12 video.

Tuần 2026-11-16 → 2026-11-22 (tuần hiện tại)
Tổng 1 học viên: đủ 0, ít nhất 0, thiếu 0, chưa đủ dữ liệu 1

Chưa đủ dữ liệu để kết luận (1)
  • S01 Lan 2/3 (…)
```

Các lệnh khác (mọi lệnh có `--json`): `list [--all]`, `update REF [--name --goal --tiktok --youtube --activate|--deactivate]`, `deactivate REF`, `record REF URL --date YYYY-MM-DD [--title]` (ghi tay một video), `sync [REF] [--platform tiktok|youtube]`, `history REF [--weeks N]`, `metrics REF [--period day|week] [--count N] [--offset N]`, `sync-status`, `report [--week-offset N] [--ai] [--send] [--force]`, `serve [--scheduler]`, `mcp`, `collect-push --server URL [--platform] [--limit]`. `REF` là mã (`S01`) hoặc id.

## Lượt xem, thích và bình luận

Khi `sync` hoặc `collect-push`, mỗi video đọc được có một ảnh chụp gồm lượt xem, lượt thích,
lượt bình luận và thời điểm đo. Các giá trị không đọc được lưu là `null` và hiện **thiếu dữ liệu**;
giá trị 0 thật vẫn là 0. Bản ghi cũ và video nhập tay chưa có ảnh chụp cho đến khi được thu thập.
Lịch sử chỉ bắt đầu từ các lần đồng bộ sau khi cài bản này, không thể khôi phục số đo trong quá khứ.

```bash
tracker sync S01
tracker metrics S01 --period day --count 7
tracker metrics S01 --period week --count 4 --json
tracker metrics S01 --period day --offset 7 --count 7  # bảy ngày trước đó
```

Với mỗi video và chỉ số, chênh lệch là **lần đo cuối trong kỳ trừ lần đo cuối của kỳ liền trước**,
theo ngày hoặc tuần thứ Hai–Chủ nhật giờ VN. Hai thời điểm đo đều được hiển thị; đây là thay đổi
giữa hai lần đo, không nhất thiết chính xác từ 00:00 đến 00:00. Nếu một trong hai kỳ không có
lần đo hoặc chỉ số bị thiếu thì chênh lệch là thiếu dữ liệu; số âm được giữ nguyên khi nền tảng
điều chỉnh số liệu. Nhiều lần đo trong một kỳ được lưu, phép so sánh dùng lần cuối. Hai lần nhận
cùng video và thời điểm đến giây được coi là gửi lại, giữ ảnh chụp đầu tiên.

yt-dlp đọc số công khai, nền tảng có thể ẩn hoặc không trả từng trường; không cần API trả phí.
Bộ thu YouTube Data API sẵn có hiện không lấy chỉ số, nên khi dùng đường đó các trường là thiếu
dữ liệu. Ảnh chụp là số tích lũy tại lúc đo, không phải số lượt phát sinh kể từ ngày đăng.

## Bốn trạng thái

Mỗi học viên trong tuần có đúng một trong bốn trạng thái:

| Trạng thái | Khi nào | Nhãn |
|---|---|---|
| `met` | dữ liệu kênh đủ mới, không có kênh lỗi hoặc chưa đồng bộ, và số video đếm được trong tuần >= mục tiêu | Đủ mục tiêu |
| `at_least` | chưa đủ mục tiêu, nhưng còn nền tảng mà công cụ **có thể chưa đọc tới hết** video của tuần | Ít nhất N video (có thể còn video chưa đọc tới) |
| `below_goal` | chưa đủ mục tiêu và mọi kênh đã khai báo đều được đọc đủ, mới | Thiếu theo dữ liệu hiện có |
| `insufficient_data` | không có kênh; hoặc có kênh chưa đồng bộ, lần gần nhất lỗi hay dữ liệu cũ — kể cả khi số video ghi nhận đã đạt mục tiêu | Chưa đủ dữ liệu để kết luận |

Chi tiết quy tắc:
- Đếm video từ mọi nguồn (yt-dlp, API, ghi tay, `collect-push`) và mọi nền tảng, từ thứ Hai đến hôm nay (hoặc Chủ nhật nếu tuần đã qua).
- **Độ mới 36 giờ:** với tuần hiện tại, lần đồng bộ thành công (ok hoặc một phần) phải trong `TRACKER_FRESH_HOURS` (mặc định 36) giờ gần nhất. Với tuần đã qua, phải sau 00:00 thứ Hai kế tiếp. Không có → `insufficient_data` (chưa đồng bộ / lần cuối lỗi / dữ liệu cũ).
- **Lần gần nhất lỗi:** trạng thái là `insufficient_data` dù lần thành công trước còn mới. Khi kênh được đồng bộ thành công trở lại, công cụ xét độ phủ lại.
- **Đã đọc đủ?** Một kênh được coi là đọc đủ nếu số video đọc được ít hơn giới hạn, hoặc đã thấy video cũ hơn tuần đang xét. Ngược lại là `at_least`. Lần đồng bộ "một phần" tối đa chỉ là `at_least`.
- **YouTube hai tab:** `/videos` và `/shorts` được xét **riêng từng tab**; chỉ khi cả hai tab đều đọc đủ mới coi là đủ (tab Shorts bị cắt ở giới hạn mà chưa tới thứ Hai → `at_least`, không bao giờ `below_goal`). `progress --json` có `platforms.youtube.segments`.
- **Đổi link kênh:** mỗi lần đồng bộ ghi lại link kênh đã đọc. Chỉ lần đồng bộ của **link kênh hiện tại** được dùng; đổi link (`update --youtube …`) thì học viên về "Chưa đủ dữ liệu" (chưa đồng bộ) cho tới lần đồng bộ kênh mới. Dữ liệu đồng bộ tạo trước bản này (không có link) cũng coi như chưa đồng bộ.
- **Video đã thuộc học viên khác:** nếu kênh trả về video đã ghi cho học viên khác, video đó không được tính và lần đồng bộ ghi "một phần" (tối đa `at_least`).
- **Video ghim TikTok:** TikTok có thể ghim một video cũ lên đầu kênh, nên với TikTok phải thấy **hơn 3** video cũ hơn tuần mới coi là đã đọc qua tuần đó (YouTube: 1 video cũ là đủ).
- **"Bài đăng không đọc được":** đôi khi TikTok báo lỗi cho một bài (thường là bài ảnh/carousel). Công cụ suy ngày từ id bài (id mã hóa thời điểm tạo) và ghi nhận là *bài không đọc được*. Nó hiển thị `(+N bài đăng không đọc được, có thể là bài ảnh)` nhưng **không tính vào mục tiêu** và không đổi trạng thái.
- Chỉ học viên đang theo dõi (`active`) được tính.

## Báo cáo tuần

`tracker report` tạo báo cáo cho tuần trước (`--week-offset -1`, mặc định), nhóm theo bốn trạng thái, mỗi học viên một dòng, kèm lý do ngắn nếu thiếu dữ liệu và dòng lưu ý cuối. Không có chữ "chưa đăng".

- **Không AI** (mặc định): văn bản xác định, không gọi dịch vụ ngoài.
- **Có AI**: `TRACKER_AI=1` (hoặc `--ai`) cùng `OPENAI_API_KEY` và `TRACKER_OPENAI_MODEL`. Gửi tới OpenAI (`OPENAI_BASE_URL`) **chỉ mã học viên và số liệu**, không có tên, URL hay tiêu đề. AI chỉ được chọn các mã hành động hỗ trợ; chương trình hiển thị câu cố định tương ứng, không hiển thị câu do AI viết. Phản hồi tự do hoặc sai định dạng bị bỏ toàn bộ. AI lỗi hay thiếu cấu hình thì báo cáo vẫn tạo/gửi; lý do nằm trong `ai_note` của JSON (và được báo trên CLI khi dùng `--ai`).
- **Báo cáo dài:** không cắt bớt học viên. Kênh chat (webhook, command, zalo_mod) nhận nhiều tin, mỗi tin ≤ 1800 ký tự, đánh dấu `(1/3)`, `(2/3)`…; gửi lỗi giữa chừng thì dừng và báo đã gửi mấy phần. Bảng `reports` lưu toàn văn.
- **Kênh gửi** (`TRACKER_DELIVER`), chỉ khi dùng `--send` hoặc scheduler:

| Kênh | Biến môi trường |
|---|---|
| `stdout` (mặc định) | không cần; in ra stdout/log |
| `file` | `TRACKER_DELIVER_FILE` (ghi nối) |
| `webhook` | `TRACKER_DELIVER_WEBHOOK_URL`, `TRACKER_DELIVER_WEBHOOK_TOKEN` (tùy chọn); POST JSON `{"text": ...}` |
| `command` | `TRACKER_DELIVER_COMMAND` (argv tách bằng shlex, không qua shell; nội dung vào stdin) |
| `zalo_mod` (chưa kiểm) | `TRACKER_ZALO_MOD_URL`, `TRACKER_ZALO_MOD_TOKEN`, `TRACKER_ZALO_TARGET_ID`, `TRACKER_ZALO_TARGET_TYPE` (`group`/`user`) |

- **Một báo cáo mỗi tuần:** `report --send` ghi bảng `reports`; tuần đã gửi thành công thì bị bỏ qua. Muốn gửi lại: `tracker report --send --force`. Trước khi gửi, tiến trình "nhận việc" tuần đó một cách nguyên tử: nếu CLI và scheduler cùng gửi, chỉ một bên gửi, bên kia bỏ qua kèm lý do (nhận việc quá 10 phút thì coi như bỏ dở và được tiếp quản). Gửi lỗi thì nhả việc để lần sau gửi lại.
- **Kênh `file`:** file mới tạo với quyền 0600, thư mục mới 0700. **Kênh `command`** và yt-dlp chạy với môi trường tối thiểu (PATH, HOME, LANG, LC_ALL, TZ, XDG_CACHE_HOME, SSL_CERT_*, proxy) — không thấy token/API key.
- **Scheduler** (`serve --scheduler`): đồng bộ mỗi `TRACKER_SYNC_EVERY_HOURS` giờ (0 = tắt; khi khởi động chỉ đồng bộ nếu lần cuối cũ hơn chu kỳ; sau mỗi lần xóa lịch sử đồng bộ cũ hơn 180 ngày, giữ lần thành công mới nhất của mỗi kênh) và gửi báo cáo vào `TRACKER_REPORT_AT` (mặc định `mon 08:00`, để trống = tắt). Báo cáo **luôn là tuần thứ Hai–Chủ nhật vừa qua**, dù đặt thứ mấy (vd `wed 08:00` vẫn báo cáo tuần trước). Gửi thất bại **không tự thử lại** trong cùng tiến trình (tránh gửi đúp); xem log rồi chạy tay `report --send`. Lưu ý: khởi động lại dịch vụ sau khi gửi lỗi sẽ khiến lần chạy sau thử gửi lại tuần đó.

## MCP

Máy chủ MCP tên `video-tracker`, **chỉ đọc**, 4 công cụ: `week_progress(week_offset=0)` (-520..0), `student_history(student, weeks=4, metrics_period='', metrics_count=7, metrics_offset=0)` (chỉ học viên đang theo dõi; chọn `metrics_period='day'` hoặc `'week'` để lấy chỉ số), `list_students()`, `sync_status()`; thêm prompt `weekly_report`.

Văn bản bên ngoài trong kết quả MCP (tiêu đề video, lỗi đồng bộ gửi qua `/ingest`) được làm sạch (bỏ ký tự điều khiển, xuống dòng, cắt 120 ký tự); tiêu đề nằm ở trường `title_from_platform` và kết quả có `untrusted_text_note` nhắc đây là dữ liệu, không phải chỉ dẫn. CLI và API vẫn trả `title` nguyên bản.

**stdio (cho Claude Code):**

```bash
claude mcp add video-tracker -- tracker mcp
# hoặc, không cài package:
claude mcp add video-tracker -- python3 -m video_tracker mcp
```

Dùng DB khác mặc định: thêm `--db /đường/dẫn.sqlite3` trước `mcp` (hoặc đặt `TRACKER_DB`).

**Codex:** công cụ này **không bao giờ sửa `~/.codex/config.toml`**. Bạn tự chạy `codex mcp add ...` với lệnh `tracker mcp` theo cú pháp của Codex đang cài.

**HTTP:** đặt `TRACKER_MCP_TOKEN` rồi chạy `tracker serve`; endpoint `/mcp` (Streamable HTTP, không phiên, trả JSON) yêu cầu `Authorization: Bearer <TRACKER_MCP_TOKEN>`. Không đặt token thì `/mcp` không được mở. Token API và token MCP là hai thứ riêng, không dùng thay nhau. Phía sau reverse proxy phải đặt `TRACKER_ALLOWED_HOSTS` (bảo vệ DNS-rebinding), nếu không các request có Host lạ bị từ chối dù có token.

**ChatGPT:** chưa hỗ trợ (cần OAuth, chưa cài).

## HTTP API

`tracker serve` (thêm `--scheduler` để chạy nền). Mặc định bind `127.0.0.1:8765`. Nếu bind địa chỉ không phải loopback mà chưa đặt token nào, dịch vụ từ chối khởi động. Token đã đặt (`TRACKER_API_TOKEN`, `TRACKER_MCP_TOKEN`, `TRACKER_INGEST_TOKEN`) ngắn hơn 32 ký tự → dịch vụ từ chối khởi động (báo tên biến, không in giá trị).

| Đường dẫn | Xác thực | Ý nghĩa |
|---|---|---|
| `GET /healthz` | không | `{"ok": true}` |
| `GET /api/students` | Bearer `TRACKER_API_TOKEN` | danh sách học viên |
| `GET /api/progress?week_offset=0` | như trên | tiến độ tuần (không nhận số dương) |
| `GET /api/sync-status` | như trên | lần đồng bộ gần nhất |
| `GET /api/metrics?student=S01&period=day&count=7` | như trên | chỉ số từng video và chênh lệch theo ngày hoặc tuần (`period=week`) |
| `GET /ingest/targets` | Bearer `TRACKER_INGEST_TOKEN` (nếu đặt), không thì `TRACKER_API_TOKEN` | danh sách kênh cần thu thập `[{"code","platform","channel_url"}]`, không có tên |
| `POST /ingest` | như dòng trên | nhận kết quả thu thập từ máy khác (tối đa 256 KB, 200 video); bắt buộc `channel_url`, tùy chọn `segments`; kênh đã đổi → 409 |
| `/mcp` | Bearer `TRACKER_MCP_TOKEN` | MCP HTTP |

`TRACKER_API_TOKEN` rỗng thì `/api/*` trả 503 (và `/ingest*` nếu cũng không có `TRACKER_INGEST_TOKEN`). Sai/thiếu token trả 401. Khi đặt `TRACKER_INGEST_TOKEN`, token API **không** mở được `/ingest*` nữa và token ingest không mở `/api/*`. `/ingest` không tin số liệu máy thu thập: gửi nhiều video hơn `fetched_count` → 400; số liệu không khớp → lưu "một phần". Đường dẫn có `/` cuối (vd `/mcp/`) trả 404, không chuyển hướng.

```bash
export TRACKER_API_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"   # cùng giá trị với server
curl -s http://127.0.0.1:8765/healthz
curl -s -H "Authorization: Bearer $TRACKER_API_TOKEN" "http://127.0.0.1:8765/api/progress?week_offset=0"
```

`/ingest` thường không gọi tay mà dùng `tracker collect-push` (xem bên dưới).

## YouTube trên VPS

IP của VPS thường bị YouTube chặn ("Sign in to confirm you're not a bot"; đã gặp ngày 16/11/2026 khi thử tay yt-dlp). Hai cách:

1. **Mac thu thập rồi đẩy lên (`collect-push`)**: chạy trên Mac có IP dân dụng, lấy danh sách kênh từ `GET /ingest/targets` (chỉ mã + link, không tên), chạy yt-dlp tại chỗ rồi POST `/ingest` kèm link kênh đã đọc và số liệu từng tab. Không cần key Google, nhưng Mac phải bật đúng giờ. Token chỉ nhận qua biến môi trường (ưu tiên `TRACKER_INGEST_TOKEN`, không có thì `TRACKER_API_TOKEN`), server phải là `https://` (http chỉ cho localhost). Nên dùng token ingest riêng cho Mac để lộ token Mac không mở được `/api`.

   ```bash
   export TRACKER_INGEST_TOKEN=...   # cùng giá trị TRACKER_INGEST_TOKEN trên server
   tracker collect-push --server https://tracker.example.com --platform youtube
   ```

   Ví dụ cron (mỗi 6 giờ; thay đường dẫn, token đặt trong file env chỉ bạn đọc được, không ghi thẳng vào crontab):

   ```
   0 */6 * * * . $HOME/.video-tracker/push.env && /đường/dẫn/.venv/bin/tracker collect-push --server https://tracker.example.com --platform youtube >> $HOME/.video-tracker/push.log 2>&1
   ```

   Hoặc dùng launchd với `StartCalendarInterval`/`StartInterval` tương đương.
2. **`YOUTUBE_API_KEY` trên server (YouTube Data API v3)**: chạy hoàn toàn trên VPS, ổn định hơn, nhưng cần tạo key Google Cloud, có hạn mức; kênh dạng `/c/name` không resolve được qua API. **Chưa kiểm với API thật.**

TikTok đọc được từ VPS bằng yt-dlp (thử tay 16/11/2026), nhưng cũng có thể bị chặn sau này.

## Triển khai VPS bằng Docker

**Việc triển khai CHƯA được làm.** Chủ dự án tự thực hiện; các bước dưới đây chưa được chạy thử (xem trạng thái Docker ở trên).

1. Chép thư mục `tracker/` lên VPS (không cần `legacy/`, `tests/`, `docs/`; chúng đã bị `.dockerignore` loại).
2. Tạo `.env` từ mẫu và điền token: `cp .env.example .env`, rồi sinh token bằng `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` cho `TRACKER_API_TOKEN` (và `TRACKER_MCP_TOKEN` nếu dùng MCP HTTP, `TRACKER_INGEST_TOKEN` nếu Mac đẩy dữ liệu lên). Token phải ≥ 32 ký tự. Container bind `0.0.0.0` nên **bắt buộc có ít nhất một token**, nếu không dịch vụ từ chối chạy. `chmod 600 .env`.
3. `docker compose up -d --build`
4. `docker compose logs -f tracker`
5. Kiểm tra: `curl -s http://127.0.0.1:8765/healthz` (cổng chỉ mở trên loopback của VPS).

Cấu hình compose: dữ liệu ở volume `tracker-data` (`/data`), cổng chỉ publish `127.0.0.1:8765`, hệ file gốc chỉ đọc (`/tmp` là tmpfs), `cap_drop: ALL`, `no-new-privileges`, user uid 10001.

**Reverse proxy:** phải có HTTPS trước khi mở `/mcp` hoặc `/api` ra Internet (token đi trong header). Proxy (nginx/Caddy...) trỏ về `127.0.0.1:8765`; đặt `TRACKER_ALLOWED_HOSTS=tracker.example.com` (hostname công khai).

**Gửi báo cáo qua Zalo từ trong container** (chưa kiểm): `TRACKER_ZALO_MOD_URL=http://host.docker.internal:18792/api/action`. Chỉ chạy nếu dashboard bot nhận kết nối từ mạng bridge Docker; nếu nó chỉ bind `127.0.0.1` của host thì container không tới được.

**Sao lưu:** SQLite ở chế độ WAL, đừng chép file khi đang chạy. Cách an toàn nhất là `sqlite3 /đường/dẫn/tracker.sqlite3 ".backup '/nơi/lưu/backup.sqlite3'"` trên bản sao DB (cần `sqlite3` trên host và biết đường dẫn volume, xem `docker volume inspect`), hoặc dừng container (`docker compose stop tracker`), chép volume, rồi `docker compose start tracker`. Image không có công cụ `sqlite3`. Nên thử khôi phục ít nhất một lần.

**Cập nhật:** chép mã mới, `docker compose up -d --build`. DB tự nâng cấp (migration theo `PRAGMA user_version`), dữ liệu trong volume được giữ. Nên sao lưu trước. Lưu ý khi lên schema v3: lịch sử đồng bộ cũ không có link kênh nên mọi học viên tạm về "Chưa đủ dữ liệu" cho tới lần đồng bộ kế tiếp (và máy chạy `collect-push` phải cập nhật cùng phiên bản, vì `/ingest` bắt buộc `channel_url`).

## Giới hạn và việc còn mở

- Chưa có OAuth nên chưa nối được với ChatGPT.
- Chưa kiểm thật: OpenAI, Zalo (zalo_mod), YouTube Data API; Docker chưa kiểm lại sau bản sửa nghiệm thu.
- Không có giao diện web; thêm/sửa học viên qua CLI (hoặc ghi tay bằng `record`).
- Chỉ một nhóm học viên (một cơ sở dữ liệu), không có nhiều giáo viên/nhiều nhóm.
- Phụ thuộc yt-dlp không chính thức: có thể hỏng khi nền tảng đổi hoặc chặn IP.
- Không xác minh chủ kênh; video đăng hai nền tảng tính là hai video.

## Phát triển

```bash
cd tracker
python3 -m pytest -q     # DB tạm, không gọi mạng
```

Bố cục: `video_tracker/` (`config.py`, `db.py`, `urls.py`, `core.py`, `collect.py`, `report.py`, `deliver.py`, `mcp_server.py`, `http_api.py`, `scheduler.py`, `cli.py`), `tests/`, `docs/SPEC.md`, `legacy/` (bản thử nghiệm cũ, không sửa). Đặc tả đầy đủ và luật trạng thái: `docs/SPEC.md`.
