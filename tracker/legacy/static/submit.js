'use strict';
const form = document.querySelector('#submit-form');
const student = document.querySelector('#student');
const message = document.querySelector('.submit-message');
const date = form.elements.published_at;
date.value = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Ho_Chi_Minh',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
fetch('/api/dashboard').then(r => r.json()).then(data => {
  student.replaceChildren(new Option('Chọn tên của bạn', ''));
  for (const item of data.students) student.add(new Option(item.name, item.id));
  if (!data.students.length) message.textContent = 'Chưa có học viên. Người hướng dẫn cần thêm học viên trước.';
}).catch(() => { message.textContent = 'Không tải được danh sách học viên. Hãy thử lại.'; });
form.addEventListener('submit', async e => {
  e.preventDefault(); const button = form.querySelector('button'); button.disabled = true; message.textContent = 'Đang gửi…';
  try {
    const response = await fetch('/api/videos', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(form)))});
    const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Không gửi được video.');
    message.textContent = 'Đã ghi nhận video. Cảm ơn bạn!'; form.elements.url.value = ''; form.elements.title.value = '';
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
});
