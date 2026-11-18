'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
let data = {students: [], videos: [], summary: {}, providers: {}};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl = value => { try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : '#'; } catch { return '#'; } };
const fmt = value => { if (!value) return 'Chưa có ngày'; const date = new Date(String(value).slice(0,10) + 'T12:00:00'); return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleDateString('vi-VN'); };
let toastTimer;
function toast(message, error = false) { const el = $('#toast'); el.textContent = message; el.className = `visible${error ? ' error' : ''}`; clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.className = ''; }, 6500); }
async function request(url, body) {
  const response = await fetch(url, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  let result; try { result = await response.json(); } catch { throw new Error('Máy chủ trả về dữ liệu không hợp lệ.'); }
  if (!response.ok || result.error) throw new Error(result.error || `Lỗi máy chủ (${response.status})`);
  return result;
}
function renderStudents() {
  const term = $('#search').value.trim().toLocaleLowerCase('vi');
  const filter = $('#filter').value;
  const students = data.students.filter(s => String(s.name).toLocaleLowerCase('vi').includes(term) && (filter === 'all' || (filter === 'met' ? Number(s.count_week) >= Number(s.weekly_goal) : Number(s.count_week) < Number(s.weekly_goal))));
  $('#student-rows').innerHTML = students.map(s => {
    const count = Number(s.count_week) || 0, goal = Number(s.weekly_goal) || 1, met = count >= goal;
    const initial = String(s.name || '?').trim().charAt(0).toLocaleUpperCase('vi');
    return `<tr><td><div class="person"><span class="avatar">${esc(initial)}</span><div><strong>${esc(s.name)}</strong><small>${s.last_video ? `Gần nhất: ${esc(fmt(s.last_video))}` : 'Chưa có video'}</small></div></div></td><td>${s.tiktok ? '<span class="platform">TikTok</span>' : ''}${s.youtube ? '<span class="platform">YouTube</span>' : ''}${!s.tiktok && !s.youtube ? '<span class="platform">Chưa có kênh</span>' : ''}</td><td><span class="progress-text">${count} / ${goal} video</span><div class="progress"><span style="width:${Math.min(100, Math.max(0,count / goal * 100))}%"></span></div></td><td><span class="badge ${met ? 'met' : ''}">${met ? 'Đã đạt mục tiêu' : 'Chưa đủ video ghi nhận'}</span></td><td>${s.youtube || s.tiktok ? `<button class="sync" data-sync="${esc(s.id)}" title="Thử đồng bộ kênh công khai">Đồng bộ</button>` : ''}</td></tr>`;
  }).join('');
  const empty = $('#students-empty'); empty.hidden = students.length > 0;
  if (!students.length && data.students.length) empty.innerHTML = '<div class="empty-icon">⌕</div><strong>Không tìm thấy học viên</strong><p>Thử thay đổi từ khóa hoặc bộ lọc.</p>';
  else if (!students.length) empty.innerHTML = '<div class="empty-icon">✳</div><strong>Chưa có học viên nào</strong><p>Thêm học viên đầu tiên để bắt đầu theo dõi nhịp đăng.</p><button class="button secondary" data-open="student-dialog">+ Thêm học viên</button>';
}
function render() {
  const summary = data.summary || {};
  $('#stat-students').textContent = summary.students ?? data.students.length;
  $('#stat-videos').textContent = summary.total_week ?? '—';
  $('#stat-met').textContent = summary.on_track ?? '—';
  $('#stat-attention').textContent = summary.needs_attention ?? '—';
  if (summary.week_start) { const start = new Date(String(summary.week_start).slice(0,10) + 'T12:00:00'); const end = new Date(start); end.setDate(start.getDate() + 6); $('#week-range').textContent = `${fmt(summary.week_start)} – ${end.toLocaleDateString('vi-VN')}`; }
  const chart = $('#rhythm-days');
  $('#rhythm-total').textContent = `${Number(summary.total_week) || 0} video`;
  if (summary.week_start) {
    const days = Array.from({length: 7}, (_, i) => { const d = new Date(`${summary.week_start}T12:00:00Z`); d.setUTCDate(d.getUTCDate() + i); return d.toISOString().slice(0,10); });
    const counts = days.map(day => data.videos.filter(video => video.published_at === day).length);
    const max = Math.max(1, ...counts);
    chart.innerHTML = days.map((day, i) => `<div class="rhythm-day" title="${esc(fmt(day))}: ${counts[i]} video"><span class="rhythm-count">${counts[i]}</span><div class="rhythm-track"><span style="height:${counts[i] ? Math.max(8, counts[i] / max * 100) : 0}%"></span></div><small>${['T2','T3','T4','T5','T6','T7','CN'][i]}</small></div>`).join('');
  }
  renderStudents();
  $('#video-student').innerHTML = data.students.length ? '<option value="">Chọn học viên</option>' + data.students.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('') : '<option value="">Hãy thêm học viên trước</option>';
  $('#video-list').innerHTML = data.videos.length ? data.videos.slice(0,15).map(v => `<div class="video-row"><span class="video-icon">▷</span><div><a href="${esc(safeUrl(v.url))}" target="_blank" rel="noopener noreferrer">${esc(v.title || 'Mở video')}</a><small>${esc(v.student_name || 'Học viên')} · ${esc(v.platform || 'Video')} · ${v.verification === 'manual' ? 'Người dùng ghi nhận' : v.verification === 'reach' ? 'Thông tin từ Agent Reach' : 'Thông tin công khai'}${String(v.platform).toLowerCase() === 'youtube' ? ` · <button class="sync" data-verify="${esc(v.id)}">Kiểm tra với Agent Reach</button>` : ''}</small></div><time>${esc(fmt(v.published_at))}</time></div>`).join('') : '<div class="empty-inline">Chưa có video nào được ghi nhận.</div>';
  for (const option of $$('#provider option')) { const available = data.providers?.[option.value]; option.textContent = `${option.value === 'codex' ? 'Codex' : 'Claude'}${available ? '' : ' · Chưa tìm thấy CLI'}`; }
}
async function refresh() { data = await request('/api/dashboard'); data.students ||= []; data.videos ||= []; render(); }
async function run(button, work) { const text = button.textContent; button.disabled = true; button.textContent = 'Đang xử lý…'; try { await work(); } catch (err) { toast(err.message || 'Có lỗi xảy ra.', true); } finally { button.disabled = false; button.textContent = text; } }
$('#today').textContent = new Intl.DateTimeFormat('vi-VN', {weekday:'long', day:'numeric', month:'long', year:'numeric'}).format(new Date());
$('#video-form input[name="published_at"]').value = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0,10);
$('#search').addEventListener('input', renderStudents); $('#filter').addEventListener('change', renderStudents);
document.addEventListener('click', event => {
  const open = event.target.closest('[data-open]'); if (open) { document.getElementById(open.dataset.open).showModal(); return; }
  const close = event.target.closest('[data-close]'); if (close) { close.closest('dialog').close(); return; }
  const sync = event.target.closest('[data-sync]'); if (sync) run(sync, async () => { const result = await request('/api/sync', {student_id:Number(sync.dataset.sync)}); await refresh(); toast(result.message || `Đã thêm ${result.added || 0} video.`); });
  const verify = event.target.closest('[data-verify]'); if (verify) run(verify, async () => { const result = await request('/api/verify', {video_id:Number(verify.dataset.verify)}); await refresh(); toast(result.message || 'Đã kiểm tra.'); });
});
for (const [id, endpoint] of [['student-form','/api/students'], ['video-form','/api/videos']]) {
  const form = $(`#${id}`); form.addEventListener('submit', event => { event.preventDefault(); const button = $('button[type="submit"]', form); run(button, async () => { const payload = Object.fromEntries(new FormData(form)); if (payload.weekly_goal) payload.weekly_goal = Number(payload.weekly_goal); if (payload.student_id) payload.student_id = Number(payload.student_id); await request(endpoint, payload); form.closest('dialog').close(); form.reset(); if (id === 'video-form') form.elements.published_at.value = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0,10); await refresh(); toast(id === 'student-form' ? 'Đã thêm học viên.' : 'Đã ghi nhận video.'); }); });
}
$('#analyze').addEventListener('click', event => run(event.currentTarget, async () => { const result = await request('/api/analyze', {provider:$('#provider').value}); const output = $('#analysis'); output.textContent = result.report || 'Không có nội dung phân tích.'; output.hidden = false; }));
refresh().catch(err => toast(`Không tải được dữ liệu: ${err.message}`, true));
