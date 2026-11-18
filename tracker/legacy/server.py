"""Local-only learner video tracker. Run: python3 tracker/server.py."""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('TRACKER_DB', Path.home() / '.agent-reach' / 'tracker.sqlite3'))
TZ = ZoneInfo('Asia/Ho_Chi_Minh')
PORT = int(os.environ.get('TRACKER_PORT', '8765'))
HOSTS = {'youtube.com': 'youtube', 'www.youtube.com': 'youtube', 'm.youtube.com': 'youtube', 'youtu.be': 'youtube', 'tiktok.com': 'tiktok', 'www.tiktok.com': 'tiktok', 'm.tiktok.com': 'tiktok', 'vm.tiktok.com': 'tiktok', 'vt.tiktok.com': 'tiktok'}

def db():
    DB.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript('''CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, name TEXT NOT NULL, tiktok TEXT NOT NULL DEFAULT '', youtube TEXT NOT NULL DEFAULT '', weekly_goal INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS videos(id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id), url TEXT NOT NULL UNIQUE, platform TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT NOT NULL, verification TEXT NOT NULL);
    ''')
    try: os.chmod(DB, 0o600)
    except OSError: pass
    return con

def platform_url(url):
    u = urlsplit(str(url).strip())
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443) or u.hostname not in HOSTS:
        raise ValueError('Chỉ chấp nhận liên kết HTTPS TikTok hoặc YouTube hợp lệ.')
    return HOSTS[u.hostname]

def date_ok(value):
    try: return dt.date.fromisoformat(str(value)).isoformat()
    except ValueError: raise ValueError('Ngày đăng phải theo định dạng YYYY-MM-DD.')

def dashboard():
    today = dt.datetime.now(TZ).date()
    start = today - dt.timedelta(days=today.weekday())
    with db() as c:
        students = [dict(x) for x in c.execute('SELECT * FROM students ORDER BY name COLLATE NOCASE')]
        videos = [dict(x) for x in c.execute('SELECT v.*,s.name student_name FROM videos v JOIN students s ON s.id=v.student_id ORDER BY published_at DESC, id DESC')]
    for s in students:
        own = [v for v in videos if v['student_id'] == s['id']]
        s['count_week'] = sum(start.isoformat() <= v['published_at'] <= today.isoformat() for v in own)
        s['last_video'] = own[0]['published_at'] if own else None
    return {'students':students,'videos':videos,'summary':{'students':len(students),'total_week':sum(s['count_week'] for s in students),'on_track':sum(s['count_week']>=s['weekly_goal'] for s in students),'needs_attention':sum(s['count_week']<s['weekly_goal'] for s in students),'week_start':start.isoformat()},'providers':{p:bool(shutil.which(p)) for p in ('codex','claude')}}

def add_student(data):
    name = str(data.get('name','')).strip()
    if not name or len(name)>100: raise ValueError('Nhập tên học viên (tối đa 100 ký tự).')
    try: goal = int(data.get('weekly_goal',3))
    except (TypeError,ValueError): raise ValueError('Mục tiêu phải là số nguyên từ 1 đến 50.')
    if not 1 <= goal <= 50: raise ValueError('Mục tiêu phải từ 1 đến 50.')
    accounts=[]
    for p in ('tiktok','youtube'):
        url=str(data.get(p,'')).strip()
        if url and platform_url(url)!=p: raise ValueError('Liên kết kênh không đúng nền tảng.')
        accounts.append(url)
    with db() as c: c.execute('INSERT INTO students(name,tiktok,youtube,weekly_goal) VALUES (?,?,?,?)',(name,*accounts,goal))
    return {'message':'Đã thêm học viên.'}

def add_video(data):
    url=str(data.get('url','')).strip()
    platform=platform_url(url)
    parts=urlsplit(url)
    if platform=='youtube':
        video_id=parts.path.strip('/') if parts.hostname=='youtu.be' else (parse_qs(parts.query).get('v',[''])[0] if parts.path=='/watch' else (parts.path.split('/')[2] if parts.path.startswith(('/shorts/','/live/')) and len(parts.path.split('/'))>2 else ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id): raise ValueError('Hãy nhập link video YouTube đầy đủ.')
        url='https://www.youtube.com/watch?v='+video_id
    else:
        match=re.fullmatch(r'/(@[A-Za-z0-9._-]+)/video/(\d{10,25})/?',parts.path)
        short=re.fullmatch(r'/([A-Za-z0-9]+)/?',parts.path) if parts.hostname in ('vm.tiktok.com','vt.tiktok.com') else None
        if match: url=f'https://www.tiktok.com/{match.group(1)}/video/{match.group(2)}'
        elif short: url=f'https://{parts.hostname}/{short.group(1)}/'
        else: raise ValueError('Hãy nhập link video TikTok hoặc link chia sẻ vm.tiktok.com / vt.tiktok.com.')
    if len(url)>2048: raise ValueError('Liên kết quá dài.')
    published=date_ok(data.get('published_at',''))
    if published > dt.datetime.now(TZ).date().isoformat(): raise ValueError('Ngày đăng không thể ở tương lai.')
    title=str(data.get('title','')).strip()[:300] or 'Video đã gửi'
    try:
        student_id=int(data.get('student_id'))
        with db() as c:
            if not c.execute('SELECT id FROM students WHERE id=?',(student_id,)).fetchone(): raise ValueError('Không tìm thấy học viên.')
            c.execute('INSERT INTO videos(student_id,url,platform,title,published_at,verification) VALUES(?,?,?,?,?,?)',(student_id,url,platform,title,published,'manual'))
    except sqlite3.IntegrityError: raise ValueError('Liên kết này đã được ghi nhận.')
    except (TypeError, ValueError) as e: raise ValueError(str(e))
    return {'message':'Đã ghi nhận link và ngày do người dùng cung cấp; chưa xác minh.'}

def sync(data):
    try: student_id=int(data.get('student_id'))
    except (ValueError,TypeError): raise ValueError('Hãy chọn học viên.')
    with db() as c: student=c.execute('SELECT * FROM students WHERE id=?',(student_id,)).fetchone()
    if not student: raise ValueError('Không tìm thấy học viên.')
    tool=shutil.which('yt-dlp')
    if not tool: raise ValueError('Chưa tìm thấy yt-dlp; hãy cài công cụ này trước khi đồng bộ.')
    added=0; problems=[]; checked=0
    for platform in ('youtube','tiktok'):
        account=student[platform]
        if not account: continue
        u=urlsplit(account)
        # Only canonical account pages are accepted. Exclude redirectors, queries, and videos.
        valid=(platform=='tiktok' and u.hostname in ('tiktok.com','www.tiktok.com','m.tiktok.com') and re.fullmatch(r'/@[A-Za-z0-9._-]+/?',u.path)) or (platform=='youtube' and u.hostname in ('youtube.com','www.youtube.com','m.youtube.com') and re.fullmatch(r'/(?:@[A-Za-z0-9._-]+|channel/[A-Za-z0-9_-]+|c/[A-Za-z0-9._-]+)/?',u.path))
        if not valid or u.query or u.fragment:
            problems.append(f'{platform}: cần link trang kênh chuẩn.'); continue
        try:
            proc=subprocess.run([tool,'--ignore-config','--no-warnings','--skip-download','--dump-json','--playlist-end','15','--socket-timeout','12','--',account],capture_output=True,text=True,timeout=90)
            if proc.returncode and not proc.stdout.strip(): problems.append(f'{platform}: không đọc được kênh hoặc nền tảng từ chối.'); continue
            checked+=1
            with db() as c:
                for line in proc.stdout.splitlines():
                    try:
                        item=json.loads(line); video_id=str(item.get('id') or '')
                        upload=str(item.get('upload_date') or '')
                        if not re.fullmatch(r'\d{8}',upload): continue
                        date=date_ok(f'{upload[:4]}-{upload[4:6]}-{upload[6:]}')
                        if date>dt.datetime.now(TZ).date().isoformat(): continue
                        if platform=='youtube' and re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id): url='https://www.youtube.com/watch?v='+video_id
                        elif platform=='tiktok' and re.fullmatch(r'\d{10,25}',video_id): url=f'https://www.tiktok.com/{u.path.strip("/")}/video/{video_id}'
                        else: continue
                        result=c.execute('INSERT OR IGNORE INTO videos(student_id,url,platform,title,published_at,verification) VALUES(?,?,?,?,?,?)',(student_id,url,platform,str(item.get('title') or 'Video công khai')[:300],date,'observed'))
                        added+=result.rowcount
                    except (ValueError,TypeError,json.JSONDecodeError): continue
            if proc.returncode: problems.append(f'{platform}: chỉ lấy được một phần kết quả.')
        except subprocess.TimeoutExpired: problems.append(f'{platform}: quá thời gian chờ.')
    if not checked and not problems: raise ValueError('Học viên chưa có link kênh.')
    return {'added':added,'message':f'Đã ghi nhận {added} video có ngày đăng trong tối đa 15 video gần nhất mỗi kênh. Kết quả có thể chưa đầy đủ. '+ ' '.join(problems)}

def verify(data):
    try: video_id=int(data.get('video_id'))
    except (TypeError,ValueError): raise ValueError('Không tìm thấy video.')
    with db() as c: video=c.execute('SELECT * FROM videos WHERE id=?',(video_id,)).fetchone()
    if not video or video['platform']!='youtube': raise ValueError('Chỉ kiểm tra video YouTube đã ghi nhận.')
    try:
        proc=subprocess.run([sys.executable,'-m','agent_reach.cli','youtube','read',video['url']],cwd=ROOT.parent,capture_output=True,text=True,timeout=60)
        result=json.loads(proc.stdout)
        if not result.get('ok') or not result.get('items'): raise ValueError('Agent Reach không lấy được dữ liệu video; hãy kiểm tra kết nối hoặc cấu hình.')
        item=result['items'][0]; published=date_ok(item.get('published_at',''))
        if published > dt.datetime.now(TZ).date().isoformat(): raise ValueError('Ngày video không hợp lệ.')
        with db() as c: c.execute('UPDATE videos SET published_at=?,title=?,verification=? WHERE id=?',(published,str(item.get('title') or video['title'])[:300],'reach',video_id))
        return {'message':'Đã cập nhật ngày và tiêu đề từ Agent Reach. Chủ sở hữu kênh chưa được xác minh.'}
    except (subprocess.TimeoutExpired, json.JSONDecodeError): raise ValueError('Agent Reach không trả về kết quả hợp lệ.')

def analyze(data):
    provider=data.get('provider')
    if provider not in ('codex','claude'): raise ValueError('Chọn Codex hoặc Claude.')
    exe=shutil.which(provider)
    if not exe: raise ValueError(f'Không tìm thấy {provider} CLI trên máy.')
    state=dashboard()
    # No names, links, titles, credentials or untrusted video text sent to provider.
    payload={'week_start':state['summary']['week_start'],'students':[{'weekly_goal':s['weekly_goal'],'count_week':s['count_week']} for s in state['students']], 'note':'Số video bao gồm khai báo thủ công chưa xác minh. Dữ liệu có thể chưa đầy đủ.'}
    prompt='Bạn là trợ lý cho người hướng dẫn. Phân tích dữ liệu tổng hợp sau bằng tiếng Việt, ngắn gọn, đề xuất hành động thực tế. Không suy đoán nguyên nhân, không khẳng định chưa đăng khi dữ liệu có thể thiếu. Chỉ dùng dữ liệu đã cho.\n'+json.dumps(payload,ensure_ascii=False)
    cmd=([exe,'exec','--ephemeral','--sandbox','read-only','--skip-git-repo-check','-'] if provider=='codex' else [exe,'-p','--tools','','--strict-mcp-config','--no-session-persistence','--output-format','text'])
    try: p=subprocess.run(cmd,input=prompt,text=True,capture_output=True,timeout=120,cwd=ROOT,check=False)
    except subprocess.TimeoutExpired: raise ValueError('AI quá thời gian chờ. Hãy thử lại sau.')
    if p.returncode: raise ValueError(f'{provider} không chạy thành công. Kiểm tra đăng nhập và quota của CLI.')
    return {'provider':provider,'report':p.stdout.strip()[:15000] or 'AI không trả về nội dung.'}

class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, content_type='application/json; charset=utf-8'):
        body=content if isinstance(content,bytes) else (json.dumps(content,ensure_ascii=False).encode() if isinstance(content,dict) else content.encode())
        self.send_response(status); self.send_header('Content-Type',content_type); self.send_header('Content-Length',str(len(body))); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body)
    def valid_host(self): return self.headers.get('Host','') in (f'127.0.0.1:{PORT}',f'localhost:{PORT}')
    def do_GET(self):
        if not self.valid_host(): return self.send(403,{'error':'Host không hợp lệ.'})
        if self.path=='/api/dashboard': return self.send(200,dashboard())
        files={'/submit':('submit.html','text/html; charset=utf-8'),'/submit.js':('submit.js','text/javascript; charset=utf-8'),'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
        if self.path not in files: return self.send(404,{'error':'Không tìm thấy.'})
        name,kind=files[self.path]
        try: self.send(200,(ROOT/'static'/name).read_bytes(),kind)
        except FileNotFoundError: self.send(404,{'error':'Giao diện chưa sẵn sàng.'})
    def do_POST(self):
        if not self.valid_host() or self.headers.get('Origin') not in (f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}'):
            return self.send(403,{'error':'Yêu cầu không hợp lệ.'})
        routes={'/api/students':add_student,'/api/videos':add_video,'/api/sync':sync,'/api/verify':verify,'/api/analyze':analyze}
        if self.path not in routes: return self.send(404,{'error':'Không tìm thấy.'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<1 or length>16384: raise ValueError('Dữ liệu không hợp lệ.')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict): raise ValueError('Dữ liệu không hợp lệ.')
            self.send(200,routes[self.path](data))
        except (ValueError, TypeError, json.JSONDecodeError) as exc: self.send(400,{'error':str(exc)})
    def log_message(self, fmt, *args): pass

if __name__=='__main__':
    db().close()
    print(f'Tracker: http://127.0.0.1:{PORT}',flush=True)
    ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
