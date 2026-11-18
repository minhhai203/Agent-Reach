# video-tracker — đặc tả (nguồn sự thật cho mọi module)

Mục tiêu: 2 người theo dõi chung 1 nhóm học viên có đăng video TikTok/YouTube đều không.
Chạy được local (Mac) và trên VPS (Docker). Python >= 3.10. Phụ thuộc: `mcp` (kéo theo starlette, uvicorn, httpx), `yt-dlp`. Không thêm dependency khác.

Nguyên tắc bắt buộc:
- KHÔNG BAO GIỜ kết luận "chưa đăng". Thiếu dữ liệu thu thập thì trạng thái là `insufficient_data`.
- Phân biệt nguồn: `manual` (người nhập), `observed` (yt-dlp), `api` (YouTube Data API), `reach` (Agent Reach đã kiểm).
- Link kênh do giáo viên khai báo; KHÔNG xác minh chủ sở hữu kênh.
- Không lưu key/token trong DB hay repo. Cấu hình chỉ qua biến môi trường.
- Văn bản hiển thị cho người dùng: tiếng Việt. Tên hàm/biến/JSON key: tiếng Anh.
- Tuần: thứ Hai → Chủ nhật, múi giờ `Asia/Ho_Chi_Minh`.
- Không dùng `shell=True`. Mọi subprocess truyền list argv, có timeout.

## Bố cục

```
tracker/
  pyproject.toml            # package video_tracker, script `tracker = video_tracker.cli:main`
  video_tracker/
    config.py   db.py   urls.py   core.py        # NỀN (agent A)
    collect.py                                    # thu thập (agent B)
    report.py   deliver.py                        # báo cáo (agent C)
    mcp_server.py   http_api.py   scheduler.py    # dịch vụ (agent D)
    cli.py                                        # CLI (agent E)
  tests/test_*.py           # pytest, mỗi test dùng DB tạm (tmp_path), không gọi mạng
  Dockerfile  docker-compose.yml  .env.example  README.md
  legacy/                   # bản thử nghiệm cũ, KHÔNG sửa, chỉ tham khảo logic URL
```

Chạy test: `cd tracker && python3 -m pytest -q`.

## config.py

`Settings` (dataclass, đọc env mỗi lần gọi `load_settings()`; test truyền Settings trực tiếp):

| env | mặc định | ý nghĩa |
|---|---|---|
| TRACKER_DB | ~/.video-tracker/tracker.sqlite3 | file SQLite |
| TRACKER_SYNC_LIMIT | 30 | số video đọc mỗi kênh mỗi lần |
| TRACKER_FRESH_HOURS | 36 | lần sync thành công cũ hơn mức này coi là "cũ" với tuần hiện tại |
| TRACKER_API_TOKEN | (rỗng) | bearer cho /api (và /ingest* khi không có TRACKER_INGEST_TOKEN); rỗng → các endpoint đó trả 503 |
| TRACKER_INGEST_TOKEN | (rỗng) | có → là token DUY NHẤT mở `POST /ingest` và `GET /ingest/targets` (token API không mở nữa); collect-push ưu tiên nó, không có thì dùng TRACKER_API_TOKEN |
| TRACKER_MCP_TOKEN | (rỗng) | bearer cho /mcp HTTP; rỗng → không mount /mcp |
| TRACKER_ALLOWED_HOSTS | (rỗng) | danh sách host (phẩy) cho bảo vệ DNS-rebinding của MCP HTTP |
| TRACKER_HOST / TRACKER_PORT | 127.0.0.1 / 8765 | bind |
| TRACKER_SYNC_EVERY_HOURS | 6 | lịch sync của scheduler (0 = tắt) |
| TRACKER_REPORT_AT | mon 08:00 | lịch báo cáo (giờ VN); rỗng = tắt. Báo cáo LUÔN là tuần thứ Hai–Chủ nhật vừa qua, dù chọn thứ mấy |
| TRACKER_AI | 0 | 1 = báo cáo có nhận xét AI |
| OPENAI_API_KEY | (rỗng) | chỉ đọc từ env |
| TRACKER_OPENAI_MODEL | (rỗng) | bắt buộc nếu bật AI; thiếu → bỏ qua AI, ghi chú trong báo cáo |
| OPENAI_BASE_URL | https://api.openai.com/v1 | |
| TRACKER_DELIVER | stdout | stdout \| file \| webhook \| command \| zalo_mod |
| TRACKER_DELIVER_FILE | (rỗng) | đường dẫn file (ghi nối) |
| TRACKER_DELIVER_WEBHOOK_URL / _TOKEN | (rỗng) | POST JSON {"text": ...}, bearer tùy chọn |
| TRACKER_DELIVER_COMMAND | (rỗng) | argv (shlex.split), nội dung báo cáo qua stdin |
| TRACKER_ZALO_MOD_URL | http://127.0.0.1:18792/api/action | dùng khi TRACKER_DELIVER=zalo_mod |
| TRACKER_ZALO_MOD_TOKEN | (rỗng) | bearer của dashboard zalo-mod (bắt buộc với zalo_mod) |
| TRACKER_ZALO_TARGET_ID | (rỗng) | groupId/userId Zalo thô (bắt buộc với zalo_mod) |
| TRACKER_ZALO_TARGET_TYPE | group | group \| user |
| YOUTUBE_API_KEY | (rỗng) | có → collector YouTube dùng Data API v3 thay yt-dlp |

`serve` từ chối chạy khi một token đã đặt (API/MCP/INGEST) ngắn hơn 32 ký tự (thông báo nêu tên biến, không nêu giá trị); `build_app` không kiểm điều này.

Tiến trình con (yt-dlp, lệnh gửi báo cáo) chỉ nhận môi trường tối thiểu `config.child_env()`: PATH, HOME, LANG, LC_ALL, TZ, XDG_CACHE_HOME, SSL_CERT_FILE, SSL_CERT_DIR, HTTPS_PROXY/HTTP_PROXY/NO_PROXY (cả dạng chữ thường) nếu có. `config.secret_values(settings)` = mọi trường `repr=False` của Settings (api/mcp/ingest token, OPENAI_API_KEY, YOUTUBE_API_KEY, webhook URL/token, zalo token, zalo target id) — được che `***` trong mọi `detail`/lỗi.

`TZ = ZoneInfo("Asia/Ho_Chi_Minh")`. Hàm `now_vn()` (để test monkeypatch `video_tracker.config.now_vn`; MỌI module lấy thời gian hiện tại qua `config.now_vn()` gọi theo dạng `config.now_vn()` chứ không import tên trực tiếp).

## db.py

`connect(path) -> sqlite3.Connection` (row_factory=Row, foreign_keys=ON, journal_mode=WAL, busy_timeout 10s, tạo thư mục 0700, file 0600, tự chạy migration).
Migration đánh số bằng `PRAGMA user_version`; danh sách `MIGRATIONS = [sql_v1, ...]`.

Schema v1:
```sql
students(id INTEGER PK, code TEXT UNIQUE NOT NULL,        -- 'S01', 'S02'… cấp tự động, không tái dùng
         name TEXT NOT NULL, weekly_goal INTEGER NOT NULL CHECK(weekly_goal BETWEEN 1 AND 50),
         tiktok_url TEXT NOT NULL DEFAULT '', youtube_url TEXT NOT NULL DEFAULT '',
         active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
videos(id INTEGER PK, student_id INTEGER NOT NULL REFERENCES students(id),
       platform TEXT NOT NULL CHECK(platform IN('tiktok','youtube')),
       video_key TEXT NOT NULL,          -- id video trên nền tảng; link rút gọn TikTok: 'short:<mã>'
       url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
       published_date TEXT NOT NULL,     -- YYYY-MM-DD theo giờ VN
       source TEXT NOT NULL CHECK(source IN('manual','observed','api','reach')),
       created_at TEXT NOT NULL,
       UNIQUE(platform, video_key));
sync_runs(id INTEGER PK, student_id INTEGER NOT NULL REFERENCES students(id),
          platform TEXT NOT NULL, collector TEXT NOT NULL,   -- 'yt-dlp' | 'youtube-api' | 'ingest:<tên>'
          status TEXT NOT NULL CHECK(status IN('ok','partial','failed')),
          started_at TEXT NOT NULL, finished_at TEXT NOT NULL,  -- ISO 8601 có offset +07:00
          fetch_limit INTEGER NOT NULL, fetched_count INTEGER NOT NULL,
          dates_json TEXT NOT NULL DEFAULT '[]',  -- danh sách published_date của các video đọc được
          error TEXT NOT NULL DEFAULT '');         -- thông báo ngắn tiếng Việt, không chứa secret
reports(id INTEGER PK, week_start TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL,
        delivered INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL, UNIQUE(week_start, kind));
```

Schema v2 (nối thêm vào `MIGRATIONS`, không sửa v1; DB v1 được nâng cấp tại chỗ, giữ nguyên dữ liệu):
```sql
ALTER TABLE videos ADD COLUMN countable INTEGER NOT NULL DEFAULT 1;
```
`countable = 0`: đã thấy một bài đăng trên kênh vào ngày đó nhưng không đọc được như video
(có thể là bài ảnh/carousel TikTok). Dòng này KHÔNG BAO GIỜ được tính vào mục tiêu tuần.

Schema v3 (không sửa v1/v2):
```sql
ALTER TABLE sync_runs ADD COLUMN channel_url TEXT NOT NULL DEFAULT '';    -- kênh THỰC SỰ đã đọc (canonical)
ALTER TABLE sync_runs ADD COLUMN segments_json TEXT NOT NULL DEFAULT '[]'; -- [{"name","fetched_count","dates"}]
ALTER TABLE reports ADD COLUMN state TEXT NOT NULL DEFAULT '';             -- '' | sending | sent | failed
ALTER TABLE reports ADD COLUMN claimed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN claim_id TEXT NOT NULL DEFAULT '';
```
Run cũ (trước v3) có `channel_url = ''` → không bao giờ khớp kênh hiện tại → học viên về `never_synced`/`stale` cho tới lần đồng bộ mới.

Schema v4: bảng `video_metric_snapshots` có `video_id` tham chiếu `videos`, `sync_run_id`
tham chiếu `sync_runs` với `ON DELETE SET NULL`, `observed_at` giờ VN, các cột nullable
`view_count`, `like_count`, `comment_count` (số nguyên không âm), và unique `(video_id, observed_at)`.
Nâng cấp không tạo dữ liệu lịch sử giả; xóa sync run cũ vẫn giữ ảnh chụp. Cùng video/thời điểm
đến giây được coi là gửi lại và giữ ảnh chụp đầu tiên.

`migrate` an toàn khi nhiều kết nối cùng mở một file mới: `BEGIN IMMEDIATE`, đọc lại `user_version` bên trong giao dịch; chuyển WAL được thử lại có giới hạn (đổi journal mode không qua busy handler). `connect` đóng kết nối nếu bước thiết lập/migration lỗi.

## urls.py

- `platform_of(url) -> 'tiktok'|'youtube'` — chỉ HTTPS, không userinfo, cổng None/443, host trong allowlist (xem legacy/server.py `HOSTS`). Sai → `ValueError` tiếng Việt.
- `canonical_channel(platform, url) -> str` — TikTok: `https://www.tiktok.com/@handle`; YouTube: `https://www.youtube.com/@handle` | `/channel/UC…` | `/c/name`. Từ chối query/fragment, link video, redirector.
  Handle/tên phải có ít nhất một chữ hoặc số, không được là `.`/`..`, không bắt đầu hay kết thúc bằng dấu chấm (từ chối `@..`, `/c/..`, `@.x`, `@x.`; áp dụng cả cho handle trong link video TikTok).
- `canonical_video(url) -> (platform, video_key, canonical_url)` — YouTube: watch?v=, youtu.be/, /shorts/, /live/ → key 11 ký tự; TikTok: `/@h/video/<id>` → key = id; `vm.|vt.tiktok.com/<mã>` → key `short:<mã>`.

## core.py (mọi hàm nhận `con` là tham số đầu; raise `ValueError` với thông báo tiếng Việt khi input sai)

```python
add_student(con, name, weekly_goal=3, tiktok_url='', youtube_url='') -> dict   # student dict
update_student(con, student_id, **fields) -> dict   # name, weekly_goal, tiktok_url, youtube_url, active
list_students(con, include_inactive=False) -> list[dict]
get_student(con, student_ref) -> dict               # nhận id (int) hoặc code 'S01'
record_video(con, student_id, url, published_date, title='', source='manual') -> dict  # trùng → ValueError
    # key chỉ tồn tại dưới dạng countable=0 của CÙNG học viên → nâng thành video countable
    # (source/ngày/tiêu đề theo lần ghi tay); thuộc học viên khác → vẫn ValueError trùng
upsert_observed_videos(con, student_id, platform, videos, source) -> {"added": int, "foreign": [video_key…]}
    # videos: list[{'video_key','url','title','published_date', 'countable'?: bool (mặc định True)}]
    # INSERT OR IGNORE; "added" = số dòng MỚI; "foreign" = key đã thuộc HỌC VIÊN KHÁC (không gán lại,
    # không đếm cho học viên này) → người gọi (sync_student, /ingest) ghi run là `partial` với lỗi
    # "N video đã được ghi nhận cho học viên khác" (core.foreign_error(N)). Video countable trùng key với dòng countable=0 của
    # cùng học viên → nâng dòng đó lên countable=1 và điền tiêu đề (không tính là "thêm mới").
    # Không bao giờ hạ countable=1 xuống 0.
record_sync_run(con, student_id, platform, collector, status, started_at, finished_at,
                fetch_limit, fetched_count, dates, error='', *, channel_url='', segments=None) -> int
    # channel_url: kênh đã đọc; segments: tối đa 4 mục {"name": [a-z]{1,16}, "fetched_count": 0..2×limit,
    # "dates": [YYYY-MM-DD] (số ngày ≤ fetched_count)}
prune_sync_runs(con, days=180) -> int   # xóa run cũ hơn N ngày, GIỮ run thành công mới nhất mỗi (học viên, nền tảng, channel_url)
week_bounds(week_offset=0) -> (monday: date, sunday: date)     # theo config.now_vn()
week_progress(con, week_offset=0, settings=None) -> dict
student_history(con, student_ref, weeks=4, settings=None) -> dict
sync_status(con) -> list[dict]                      # lần sync gần nhất mỗi học viên × nền tảng (chỉ run của kênh HIỆN TẠI)
```

### Luật trạng thái (trọng tâm sản phẩm)

Với tuần W = [monday, sunday]; `is_current = (week_offset == 0)`; `end_day = min(sunday, hôm nay)`.
`count` = số video **countable=1** của học viên có `monday <= published_date <= end_day` (mọi nguồn, mọi nền tảng).
`by_source` và luật met/below chỉ dùng dòng countable=1. `unreadable_posts` = số dòng countable=0 trong tuần
(chỉ để thông tin, không ảnh hưởng trạng thái).

Độ phủ của từng nền tảng mà học viên CÓ khai báo kênh:
- Chỉ xét các run có `channel_url` BẰNG đúng link kênh hiện tại của học viên trên nền tảng đó (đổi link kênh →
  run của kênh cũ không còn là bằng chứng; run `channel_url=''` trước v3 không bao giờ khớp). `sync_status` cũng vậy.
- Nếu lần sync mới nhất của kênh hiện tại là `failed`, độ phủ là `unknown` với lý do
  `last_sync_failed`, kể cả khi lần thành công trước đó còn mới. Sau lần `ok`/`partial` mới hơn,
  xét lại độ phủ bình thường.
- Nếu lần mới nhất không lỗi, lấy lần sync thành công gần nhất (`status in ('ok','partial')`) có `finished_at >= needed_after`, với
  `needed_after = now - FRESH_HOURS` nếu tuần hiện tại; `= 00:00 thứ Hai kế tiếp (giờ VN)` nếu tuần đã qua.
  (Tuần tương lai: `week_offset > 0` → ValueError.)
- Không có lần nào như vậy → `unknown` (kèm lý do: `never_synced` | `last_sync_failed` | `stale`).
- Có: đặt `older = số ngày trong dates_json < monday`, `guard = 3` với tiktok (video ghim có thể là video cũ), `0` với youtube.
  - `fetched_count < fetch_limit` (đã đọc hết kênh) HOẶC `older > guard` → `complete`
  - ngược lại → `lower_bound` (có thể còn video trong tuần chưa đọc tới)
  - `status == 'partial'` thì tối đa là `lower_bound`.
  - Run có `segments` (YouTube qua yt-dlp: `videos`, `shorts`): luật trên áp dụng RIÊNG cho từng segment
    (fetched_count và dates của segment, `fetch_limit` của run, guard theo nền tảng); `complete` chỉ khi MỌI
    segment complete (lấy mức tệ nhất). Không có segments → dùng số liệu cấp run như trên.

Trạng thái học viên (`status`):
1. học viên không có kênh nào → `insufficient_data` (reason `no_channel`), kể cả khi có video ghi tay
2. có nền tảng `unknown` → `insufficient_data`, kể cả khi số video đã đạt mục tiêu
3. `count >= weekly_goal` → `met`
4. có nền tảng `lower_bound` → `at_least`
5. còn lại → `below_goal`  (nhãn: "Thiếu theo dữ liệu hiện có")

Nhãn tiếng Việt (`status_label`): met "Đủ mục tiêu"; at_least "Ít nhất {count} video (có thể còn video chưa đọc tới)"; below_goal "Thiếu theo dữ liệu hiện có"; insufficient_data "Chưa đủ dữ liệu để kết luận".

`week_progress` trả:
```json
{"week_start":"2026-11-16","week_end":"2026-11-22","is_current":true,"generated_at":"…+07:00",
 "timezone":"Asia/Ho_Chi_Minh",
 "summary":{"students":4,"met":1,"at_least":0,"below_goal":1,"insufficient_data":2},
 "students":[{"id":1,"code":"S01","name":"Lan","weekly_goal":3,"count":2,"unreadable_posts":0,
   "status":"below_goal","status_label":"…","reasons":[],
   "by_source":{"manual":0,"observed":2,"api":0,"reach":0},
   "platforms":{"tiktok":{"channel_url":"…","coverage":"complete","reason":"","last_synced_at":"…","last_status":"ok","last_error":"",
                          "segments":[]}},   # segments: [{"name":"videos","fetched_count":2,"coverage":"complete"}, …] khi run có segments
   "videos":[{"platform":"tiktok","url":"…","title":"…","published_date":"…","source":"observed","countable":true}]}],
 "disclaimer":"Số liệu dựa trên dữ liệu thu thập được; kênh do giáo viên khai báo, chưa xác minh chủ sở hữu. Khi dữ liệu chưa đủ thì không kết luận về việc đăng video của học viên."}
```
`videos` gồm cả bài countable=0 trong tuần (`"countable": false`). `core.DISCLAIMER` không được chứa cụm "chưa đăng"/"không đăng".
Chỉ tính học viên `active=1`. `student_history` trả `{"student":{…},"weeks":[<mục students[] của từng tuần, kèm week_start/week_end>]}` từ tuần hiện tại lùi về.

## collect.py (agent B)

```python
@dataclass
class CollectResult: platform; collector; status; fetch_limit; fetched_count; videos: list[dict]; error: str; started_at; finished_at;
               channel_url: str (canonical, kênh đã đọc); segments: list[{"name","fetched_count","dates"}]
collect_channel(platform, channel_url, limit, settings) -> CollectResult   # không đụng DB
sync_student(con, student, settings, platforms=None) -> list[dict]         # collect + upsert + record_sync_run(channel_url, segments)
sync_all(con, settings, platforms=None) -> dict                            # tóm tắt từng học viên
```
- yt-dlp: tìm bằng `shutil.which('yt-dlp')`, không có thì `[sys.executable,'-m','yt_dlp']`; không có cả hai → run `failed`, error "Chưa cài yt-dlp".
  argv: `--ignore-config --no-warnings --skip-download --dump-json --playlist-end <limit> --socket-timeout 15 -- <url>`; timeout tổng 180s.
- TikTok: URL kênh chuẩn. YouTube: đọc CẢ `<kênh>/videos` và `<kênh>/shorts` (mỗi tab `limit`), gộp, khử trùng; tab không tồn tại không tính là lỗi nếu tab kia đọc được.
  Mỗi tab YouTube TỒN TẠI thành một segment `{"name":"videos"|"shorts","fetched_count":<số mục có ngày của tab>,"dates":[…]}`;
  tab không tồn tại thì vắng mặt. TikTok và đường YouTube Data API không có segment.
- yt-dlp: lưu riêng `metrics` gồm `view_count`, `like_count`, `comment_count`; chỉ nhận số nguyên
  không âm trong phạm vi SQLite, mọi giá trị thiếu/sai thành null. Hai tab cùng video thì điền
  trường còn thiếu từ tab kia. Với sync/ingest, lưu một ảnh chụp cho video của đúng học viên tại
  `finished_at`; không lấy số liệu từ bài không đọc được hoặc video thuộc người khác. Đường
  YouTube Data API hiện không thu thập các trường này, chúng là null.
- Ngày: ưu tiên `timestamp` (epoch) đổi sang ngày giờ VN; không có thì `upload_date` (YYYYMMDD). Không có ngày → bỏ video đó, và run là `partial` (TikTok: thử ngày suy từ id trước, xem dưới).
- returncode != 0 nhưng có dòng JSON → `partial`. Không có dòng nào → `failed`. stderr chứa "not a bot"/"Sign in to confirm" → error "Nền tảng yêu cầu xác minh (chặn theo IP)"; 429 → "Bị giới hạn tần suất".
- TikTok — ngày suy từ id: id bài TikTok mã hóa thời điểm tạo, `id >> 32` = Unix timestamp (giây, UTC) → đổi sang ngày giờ VN.
  Timestamp trước 2016-01-01 hoặc ở tương lai → coi như không xác định được ngày.
  - Video TikTok đọc được nhưng thiếu cả `timestamp` lẫn `upload_date` → dùng ngày suy từ id (vẫn countable), không bỏ.
  - Lỗi từng bài trong stderr dạng `ERROR: [TikTok] <số>: …` (không phân biệt hoa thường thẻ extractor): với mỗi id 10–25 chữ số
    KHÔNG nằm trong các video đã đọc được và có ngày suy từ id hợp lệ → thêm một mục `{"video_key": id, "url": "https://www.tiktok.com/@<handle>/video/<id>",
    "title": "", "published_date": <ngày>, "countable": false}`; ngày của nó vào `dates` và nó được tính vào `fetched_count`.
  - Trạng thái: returncode != 0 nhưng MỌI dòng `ERROR:` đều là lỗi từng bài TikTok đã được tính (đọc được hoặc đã suy ngày từ id) → `ok`
    (không còn gì chưa biết). Có lỗi khác, bài lỗi không suy được ngày, hoặc mã thoát khác 0 mà không có dòng `ERROR:` → vẫn `partial`.
  - Không áp dụng gì ở trên cho YouTube.
- `fetched_count` = số mục có ngày hợp lệ (gồm cả bài countable=false của TikTok); `dates` = ngày của chúng. Với YouTube 2 tab: `fetch_limit` ghi = limit, `fetched_count` = min(số video tab đầy nhất…) — đơn giản hóa: ghi `fetch_limit = limit` và `fetched_count = max(số video của mỗi tab)` để luật "đã đọc hết kênh" chỉ đúng khi KHÔNG tab nào chạm limit.
- `YOUTUBE_API_KEY` có → collector `youtube-api` (httpx): resolve kênh (`channels?part=contentDetails&forHandle=` | `id=`), lấy `uploads` playlist, `playlistItems?part=contentDetails,snippet&maxResults=min(limit,50)`; ngày từ `contentDetails.videoPublishedAt`. Kênh `/c/name` không resolve được → failed với thông báo rõ. Không log key.
- Video ngày tương lai (so với hôm nay VN) → bỏ.

## metrics.py

`history(con, student_ref, period='day', count=7, offset=0)` trả video countable và các kỳ gần nhất
(1–52 kỳ; `offset` lùi 0–5200 kỳ), mới nhất trước. Mỗi kỳ chứa ngày bắt đầu/kết thúc, `observed_at`, `baseline_at`,
`values` và `change` cho từng trường. Ngày tính theo VN, tuần thứ Hai–Chủ nhật; giá trị là ảnh
chụp cuối kỳ, change = ảnh chụp cuối kỳ trừ ảnh chụp cuối kỳ liền trước. Không có ảnh chụp
hoặc một trường null ở một trong hai kỳ thì change trường đó là null. Không điền số 0 hay dùng
kỳ cũ hơn thay cho kỳ thiếu; số âm được giữ. Hai mốc đo hiển thị rõ, vì chênh lệch không nhất
thiết trùng đúng ranh giới kỳ. Các video nhập tay chưa được thu thập có values/change null.

## report.py + deliver.py (agent C)

```python
build_report(con, settings, week_offset=-1, use_ai=None, http_client=None) -> dict
   # {"week_start","week_end","text": <toàn văn, không cắt>, "parts": [<tin ≤1800 ký tự>…],
   #  "ai_used": bool, "ai_note": str, "progress": <week_progress>}
render_text(progress, commentary='') -> str; render_parts(progress, commentary='', limit=1800) -> list[str]
filter_commentary(text, students) -> (commentary, note)
anonymized_payload(progress) -> dict     # CHỈ: code, weekly_goal, count, unreadable_posts (số), status, coverage từng nền tảng, by_source. KHÔNG tên, URL, tiêu đề.
ai_commentary(payload, settings, http_client=None) -> str   # POST {OPENAI_BASE_URL}/chat/completions, timeout 60s
send_report(con, settings, week_offset=-1, force=False, http_client=None) -> dict
   # NHẬN VIỆC trước khi gửi: BEGIN IMMEDIATE, dòng (week_start,'weekly') → state='sending', claimed_at, claim_id.
   # Đã delivered và không force → bỏ qua. Dòng 'sending' của người khác nhận < 10 phút → bỏ qua (kể cả force)
   # kèm lý do; nhận việc cũ hơn → tiếp quản. Gửi xong: delivered=1 chỉ khi thành công (không hạ về 0),
   # state 'sent'/'failed', giải phóng claim (gửi lỗi → lần sau thử lại được). body = toàn văn.
   # Kết quả thêm "parts".
deliver(text, settings, http_client=None, parts=None) -> dict    # theo TRACKER_DELIVER
```
- Bản text xác định (không AI) nhóm theo 4 trạng thái, mỗi học viên một dòng `Tên count/goal`, kèm lý do ngắn cho `insufficient_data` (vd "TikTok: đồng bộ lỗi"), dòng disclaimer cuối.
  Học viên có `unreadable_posts > 0`: nối ` (+N bài đăng không đọc được, có thể là bài ảnh)` vào cuối dòng. Toàn bộ text xác định (kể cả disclaimer) không chứa "chưa đăng"/"không đăng".
  `text` là toàn văn (không rút gọn). `parts`: các tin ≤ 1800 ký tự, chỉ cắt giữa nhóm/giữa dòng (dòng quá dài mới cắt theo khoảng trắng), tiêu đề ở phần 1, disclaimer ở phần cuối, nhóm bị cắt lặp tiêu đề kèm "(tiếp)", nhiều phần thì mỗi phần bắt đầu bằng "(i/n) ".
- `deliver` với webhook/command/zalo_mod gửi lần lượt từng phần, dừng ở phần lỗi đầu tiên; stdout/file ghi toàn văn một lần. Kết quả thêm `parts_sent`, `parts_total`.
- AI chỉ chọn hành động hỗ trợ bằng JSON `{"actions":[...]}` với 1–3 mã duy nhất trong
  `check_channels`, `offer_support`, `share_resources`. `filter_commentary` chỉ dựng các câu cố định
  tương ứng do code định nghĩa, không hiển thị văn bản từ AI. JSON sai, có trường thừa, mã lạ,
  mã lặp hoặc danh sách rỗng → bỏ toàn bộ phần AI và ghi `ai_note`.
  AI lỗi/thiếu key/thiếu model → báo cáo vẫn tạo hoặc gửi, `ai_used=False`.
- Không bao giờ ghi key vào log/exception message.
- Kênh `file`: file mới tạo quyền 0600, thư mục mới 0700. Kênh `command`: môi trường tối thiểu (`config.child_env()`).
- Kênh `zalo_mod` (bot Zalo của OpenClaw trên cùng VPS, CHƯA kiểm thật): `POST {TRACKER_ZALO_MOD_URL}` với header `Authorization: Bearer <token>`, body
  `{"action":"send-message","payload":{"targetId":"<id>","text":"<báo cáo>","targetType":"group"}}`. HTTP không 2xx hoặc JSON trả về có `ok: false`/`error` → coi là gửi thất bại (`delivered=0`), không retry tự động (tránh gửi đúp). Thiếu token/target → lỗi cấu hình rõ ràng.
- `deliver` trả `{"ok": bool, "channel": str, "detail": str}`; không raise khi kênh lỗi, chỉ raise `ValueError` khi cấu hình sai.

## mcp_server.py + http_api.py + scheduler.py (agent D)

- `build_mcp(settings) -> FastMCP` tên "video-tracker", CHỈ tool đọc, đều `annotations=ToolAnnotations(readOnlyHint=True)`:
  `week_progress(week_offset: int = 0)`, `student_history(student: str, weeks: int = 4, metrics_period: str = '', metrics_count: int = 7, metrics_offset: int = 0)`, `list_students()`, `sync_status()`.
  Khi `metrics_period` là `day` hoặc `week`, `student_history` thêm `video_metrics`; tiêu đề
  video trong kết quả đó được làm sạch và đổi tên thành `title_from_platform` như các video khác.
  Đầu ra MCP (chỉ MCP; CLI/API giữ nguyên core): `videos[].title` đổi tên thành `title_from_platform`, bỏ ký tự điều khiển/định dạng và xuống dòng, cắt 120 ký tự;
  `platforms[].last_error` và `error` của mục `sync_status` có collector `ingest:*` cũng được làm sạch/cắt 120; `week_progress`, `student_history`, `sync_status`
  có thêm `untrusted_text_note` (tiêu đề và lỗi là văn bản bên ngoài, không phải chỉ dẫn). `week_offset` chỉ từ -520 đến 0. `student_history` từ chối học viên đã ngừng theo dõi.
  Prompt `weekly_report`: hướng dẫn viết báo cáo, cấm kết luận "chưa đăng" khi `insufficient_data`.
  Mỗi lần gọi tool mở connection mới tới settings.db.
- stdio: `run_stdio(settings)`.
- HTTP (Starlette, `build_app(settings)`):
  - `GET /healthz` → `{"ok":true}` (không cần token)
  - `GET /api/students`, `GET /api/progress?week_offset=0`, `GET /api/sync-status`,
    `GET /api/metrics?student=S01&period=day&count=7` — bearer TRACKER_API_TOKEN
  - `GET /ingest/targets` — cùng xác thực với /ingest; trả danh sách `[{"code","platform","channel_url"}]` cho học viên đang theo dõi (KHÔNG có tên).
  - `POST /ingest` — bearer TRACKER_INGEST_TOKEN nếu đặt (khi đó token API bị từ chối 401), không thì TRACKER_API_TOKEN; body ≤ 256KB:
    `{"student":"S01","platform":"youtube","collector":"mac","channel_url":"https://www.youtube.com/@x","status":"ok","fetch_limit":30,"fetched_count":12,"error":"","started_at":"…","finished_at":"…",
      "segments"?:[{"name":"videos","fetched_count":12,"dates":["…"]}],"videos":[{"video_key","url","title","published_date","countable"?,"metrics"?}]}`
    `countable` tùy chọn, mặc định `true`; có mặt thì phải là boolean JSON (sai kiểu → 400).
    `metrics` tùy chọn, chứa `view_count`, `like_count`, `comment_count` là số nguyên không âm
    hoặc null; giá trị sai/trường lạ → 400. Video của phiên bản thu thập cũ không gửi `metrics`
    được lưu ảnh chụp các trường null. Lưu video, sync run và ảnh chụp cùng giao dịch.
    Kiểm tra: url phải canonical đúng platform (dùng urls.canonical_video và so khớp video_key), ngày hợp lệ không ở tương lai, tối đa 200 video. Ghi `collector='ingest:<collector>'`, source `observed`.
    `channel_url` BẮT BUỘC (thiếu/sai → 400); khác link kênh hiện tại của học viên trên nền tảng đó → **409** (thông báo tiếng Việt), không ghi gì.
    `segments` tùy chọn (≤ 4 mục, tên `[a-z]{1,16}`, không trùng, `fetched_count` 0..2×fetch_limit, số ngày ≤ fetched_count, ngày không ở tương lai; sai → 400).
    Số video (sau khử trùng) > `fetched_count` (hoặc > tổng fetched_count các segment khi có segments) → 400.
    `status='ok'` nhưng số liệu không nhất quán → lưu `partial` (thêm lỗi "Số liệu gửi lên không nhất quán…"): không segments: `fetched_count != số video`;
    có segments: mỗi segment phải có đúng `fetched_count` ngày và các ngày đó có trong video gửi lên, `fetched_count` = segment lớn nhất, mọi video thuộc ít nhất một segment.
    Video đã thuộc học viên khác → `partial` (như sync_student). Phản hồi: `{"ok":true,"added":N,"run_id":…,"status":"<trạng thái đã lưu>"}`.
    `student` số nguyên ngoài 1..2^63-1, chuỗi > 64 ký tự, JSON lồng quá sâu → 400 (không 500).
  - Chuyển hướng dấu `/` cuối bị tắt: `/mcp/`, `/api/students/`… trả 404/401, không bao giờ 307.
  - `/mcp` — MCP Streamable HTTP, bearer TRACKER_MCP_TOKEN (so sánh `hmac.compare_digest`); không có token cấu hình → không mount.
  - Token rỗng không bao giờ được coi là hợp lệ. Lỗi trả JSON `{"error": "..."}` tiếng Việt.
- `scheduler.py`: vòng lặp nền (thread) chạy `sync_all` mỗi TRACKER_SYNC_EVERY_HOURS (sau mỗi lần: `prune_sync_runs(con, 180)`) và `send_report` theo TRACKER_REPORT_AT (mỗi tuần một lần, dựa bảng reports để không gửi trùng). Báo cáo LUÔN là tuần thứ Hai–Chủ nhật vừa qua (`week_offset=-1`), bất kể thứ trong TRACKER_REPORT_AT. Lỗi trong job không làm chết vòng lặp. Hàm thuần `due_report(now, report_at, last_week_start) -> bool` và `parse_report_at(text)` để test.

## cli.py (agent E)

`tracker <lệnh>` (argparse, mọi lệnh có `--json`):
`add-student NAME --goal N [--tiktok URL] [--youtube URL]`, `list`, `update REF [...]`, `deactivate REF`,
`record REF URL --date YYYY-MM-DD [--title]`, `sync [REF] [--platform]`, `progress [--week-offset N]`,
`history REF [--weeks N]`, `metrics REF [--period day|week] [--count 1..52] [--offset 0..5200]`, `sync-status`, `report [--week-offset N] [--ai] [--send] [--force]`,
`serve [--scheduler]`, `mcp` (stdio), `collect-push --server URL [--platform youtube] [--limit N]` (token: TRACKER_INGEST_TOKEN, không có thì TRACKER_API_TOKEN; lấy `GET /ingest/targets` (không gọi /api/students), chạy collect_channel local, POST /ingest kèm `channel_url` đã đọc, `segments` và `countable` của từng video; dòng kết quả không còn tên học viên, chỉ mã).
Text của `progress`/`history`: học viên/tuần có `unreadable_posts > 0` nối thêm ` (+N bài đăng không đọc được, có thể là bài ảnh)`.
Mã thoát 0/1; lỗi in tiếng Việt ra stderr.
