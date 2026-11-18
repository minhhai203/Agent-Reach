# Theo dõi video học viên

Ứng dụng cục bộ thử nghiệm, chỉ dành cho một người dùng trên máy này.

Chạy từ thư mục repo:

```bash
python3 tracker/server.py
```

Mở http://127.0.0.1:8765. Dữ liệu lưu ở `~/.agent-reach/tracker.sqlite3`. Có thể chọn cổng bằng `TRACKER_PORT` hoặc vị trí dữ liệu bằng `TRACKER_DB`. Không mở cổng ra mạng: ứng dụng chưa có đăng nhập hay phân quyền.

Thêm học viên và mục tiêu video/tuần. Mở `/submit` để chọn học viên, nộp link video và ghi ngày đăng. Trang này chỉ truy cập trên máy chạy ứng dụng; chưa có đường link cho học viên nộp từ xa hoặc tài khoản riêng. Hỗ trợ link TikTok đầy đủ và link chia sẻ vm.tiktok.com/vt.tiktok.com; cùng video được nộp bằng hai loại link có thể bị đếm trùng. Cũng có thể nhập URL kênh TikTok/YouTube rồi thử đồng bộ thủ công. Đồng bộ dùng `yt-dlp` đã cài trên máy, đọc tối đa 15 video mới nhất mỗi kênh và chỉ ghi nhận video có ngày. Kết quả có thể thiếu, bị chặn hoặc không hoạt động; không suy ra học viên không đăng khi thu thập thất bại. Chưa có tác vụ chạy theo lịch. Link thủ công và ngày do người dùng nhập chưa được xác minh. Video YouTube có thể kiểm tra bằng Agent Reach; dữ liệu từ đó không xác minh chủ sở hữu kênh. Một video đăng trên hai nền tảng được tính là hai lượt đăng. Tuần tính theo giờ Việt Nam, từ thứ Hai đến hiện tại.

Nút phân tích gọi CLI Codex hoặc Claude đã cài và đang cấu hình trên máy, chỉ khi người dùng nhấn nút. Cần có phiên đăng nhập/quota tương ứng; không có chuyển đổi provider tự động. AI nhận số liệu tổng hợp, không nhận tên học viên hoặc link video. Đọc điều kiện gói dịch vụ đang dùng trước khi bấm; CLI có thể dùng hạn mức hoặc bị tính phí theo cấu hình hiện tại. App không lưu key và không thay đổi cấu hình model/provider.

Kết nối Agent Reach cho kiểm tra video YouTube dùng Python hiện tại (`python3 -m agent_reach.cli`) và cần các dependency của repo, cùng cấu hình kênh YouTube phù hợp. TikTok trong bản thử nghiệm dùng yt-dlp độc lập, không phải channel Agent Reach. Không có API TikTok chính thức hay sự đảm bảo về độ đầy đủ. Nếu phát triển thành sản phẩm cho nhiều giáo viên/học viên cần bổ sung xác thực, sao lưu, chính sách dữ liệu và tích hợp nguồn dữ liệu được cấp quyền.
