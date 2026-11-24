'use strict';
const $ = id => document.getElementById(id);
let csrf = '', selected = null, page = 1, toastTimer;
function toast(message, persistent = false) { $('toast').textContent = message; $('toast').classList.remove('hidden'); clearTimeout(toastTimer); if (!persistent) toastTimer = setTimeout(() => $('toast').classList.add('hidden'), 7000); }
async function call(url, options = {}) {
  const headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...(options.headers || {})};
  const response = await fetch(url, {...options, headers});
  let data; try { data = await response.json(); } catch { throw new Error('پاسخ سرور قابل خواندن نیست.'); }
  if (!response.ok) { if (data.recovery) toast(`${data.error}\n${data.recovery.address}\n${data.recovery.password}`, true); const error = new Error(data.error || 'خطایی رخ داد.'); error.recovery = !!data.recovery; error.status = response.status; throw error; }
  return data;
}
function fail(e) { if (!e.recovery) toast(e.message || 'ارتباط برقرار نشد.'); }
function view(auth) { $('login-view').classList.toggle('hidden', auth); $('dashboard').classList.toggle('hidden', !auth); $('logout').classList.toggle('hidden', !auth); }
async function loadAccounts() {
  const items = await call('/api/mailboxes');
  $('count').textContent = items.length.toLocaleString('fa-IR');
  const list = $('accounts-list'); list.replaceChildren();
  if (!items.length) { const p = document.createElement('p'); p.className='empty-small'; p.textContent='هنوز ایمیلی نساخته‌اید.'; list.append(p); return; }
  for (const item of items) {
    const box = document.createElement('div'); box.className = 'account' + (selected?.id === item.id ? ' active' : '');
    const address = document.createElement('strong'); address.className='account-address'; address.textContent=item.address; box.append(address);
    box.addEventListener('click', () => { selected = item; page=1; loadAccounts().catch(fail); loadMessages().catch(fail); });
    const actions = document.createElement('div'); actions.className='account-actions';
    const copy = document.createElement('button'); copy.textContent='کپی آدرس'; copy.addEventListener('click', async e => {e.stopPropagation(); try {await navigator.clipboard.writeText(item.address); toast('آدرس کپی شد.');} catch {toast('کپی خودکار انجام نشد؛ آدرس را دستی کپی کنید.');}});
    const pass = document.createElement('button'); pass.textContent='نمایش رمز'; pass.addEventListener('click', async e => {e.stopPropagation(); try {const data = await call(`/api/mailboxes/${item.id}/credentials`); window.prompt('رمز این ایمیل (برای ذخیره، آن را کپی کنید):', data.password);} catch(err) {fail(err);} });
    const del = document.createElement('button'); del.className='danger'; del.textContent='حذف از فهرست'; del.addEventListener('click', async e => {e.stopPropagation(); if (!confirm('فقط از فهرست این برنامه حذف شود؟ خود حساب در سرویس حذف نمی‌شود و اطلاعات آن از این برنامه پاک خواهد شد.')) return; try { await call(`/api/mailboxes/${item.id}`, {method:'DELETE'}); if (selected?.id === item.id) { selected=null; resetInbox(); } await loadAccounts(); toast('از فهرست حذف شد.'); } catch(err) {fail(err);} });
    actions.append(copy, pass, del); box.append(actions); list.append(box);
  }
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let bulkRunning = false, bulkStop = false;
const BULK_LIMIT = 100;
async function bulkCreate() {
  const btn = $('bulk-create'), status = $('bulk-status'), single = document.querySelector('#create-form button[type="submit"]');
  if (bulkRunning) { bulkStop = true; btn.textContent = 'در حال توقف…'; return; }
  bulkRunning = true; bulkStop = false;
  btn.classList.add('stopping'); single.disabled = true; status.classList.remove('hidden');
  let made = 0, target = 0, reason = '';
  try {
    const items = await call('/api/mailboxes');
    target = Math.max(0, BULK_LIMIT - items.length);
    if (!target) { status.textContent = 'هم‌اکنون ۱۰۰ ایمیل یا بیشتر در فهرست ذخیره شده است.'; toast('سقف ۱۰۰ ایمیل قبلاً پر شده است.'); return; }
    btn.textContent = 'توقف ساخت ⏹';
    for (let i = 1; i <= target && !bulkStop; i++) {
      status.textContent = `ساخت خودکار: ${made.toLocaleString('fa-IR')} از ${target.toLocaleString('fa-IR')} ساخته و ذخیره شد…`;
      let ok = false;
      for (let attempt = 0; attempt < 3 && !bulkStop && !ok; attempt++) {
        try {
          await call('/api/mailboxes', {method: 'POST', body: JSON.stringify({username: ''})});
          made++; ok = true;
          if (made % 10 === 0) await loadAccounts().catch(() => {});
          await sleep(900 + Math.floor(Math.random() * 500));
        } catch (e) {
          reason = e.message || 'خطای نامشخص';
          if (e.status === 429) await sleep(8000);
          else if (e.recovery) { bulkStop = true; }
          else await sleep(2500);
        }
      }
      if (!ok) bulkStop = true;
    }
    await loadAccounts().catch(() => {});
    if (made) toast(`${made.toLocaleString('fa-IR')} ایمیل ساخته و ذخیره شد.`);
    status.textContent = bulkStop
      ? `متوقف شد — ${made.toLocaleString('fa-IR')} ایمیل ساخته و ذخیره شد.` + (reason ? ` دلیل: ${reason}` : '')
      : `${made.toLocaleString('fa-IR')} ایمیل ساخته و ذخیره شد. ✓`;
  } finally {
    bulkRunning = false; bulkStop = false;
    btn.textContent = 'ساخت گروهی تا ۱۰۰ ایمیل ⚡'; btn.classList.remove('stopping');
    single.disabled = false;
  }
}
function resetInbox() { $('selected-address').textContent='یک ایمیل را انتخاب کنید'; $('refresh').disabled=true; $('inbox-placeholder').classList.remove('hidden'); $('messages').classList.add('hidden'); $('message-detail').classList.add('hidden'); $('pagination').classList.add('hidden'); }
async function loadMessages() {
  if (!selected) return; const id = selected.id, requestedPage = page;
  $('selected-address').textContent=selected.address; $('refresh').disabled=true; $('inbox-placeholder').classList.remove('hidden'); $('inbox-placeholder').querySelector('h3').textContent='در حال دریافت پیام‌ها...'; $('inbox-placeholder').querySelector('p').textContent='لطفاً کمی صبر کنید.'; $('messages').classList.add('hidden'); $('message-detail').classList.add('hidden'); $('pagination').classList.add('hidden');
  try {
    const result = await call(`/api/mailboxes/${id}/messages?page=${requestedPage}`);
    if (!selected || selected.id !== id || page !== requestedPage) return;
    const list = $('messages'); list.replaceChildren();
    if (!result.messages.length) { $('inbox-placeholder').querySelector('h3').textContent='هنوز پیامی نرسیده'; $('inbox-placeholder').querySelector('p').textContent='برای بررسی پیام‌های جدید روی بروزرسانی بزنید.'; }
    else { $('inbox-placeholder').classList.add('hidden'); list.classList.remove('hidden'); for (const msg of result.messages) { const btn=document.createElement('button'); btn.className='message'; const subject=document.createElement('strong'); subject.textContent=msg.subject; const meta=document.createElement('small'); meta.textContent=`${msg.from}  •  ${msg.createdAt ? new Date(msg.createdAt).toLocaleString('fa-IR') : ''}`; const preview=document.createElement('small'); preview.textContent=msg.intro || ''; btn.append(subject,meta,preview); btn.addEventListener('click', () => openMessage(id,msg.id).catch(fail)); list.append(btn); } }
    $('pagination').classList.toggle('hidden', page===1 && !result.hasNext); $('page-number').textContent='صفحه ' + page.toLocaleString('fa-IR'); $('previous').disabled=page<=1; $('next').disabled=!result.hasNext;
  } catch(e) { if (selected?.id === id) { $('inbox-placeholder').querySelector('h3').textContent='دریافت پیام ناموفق بود'; $('inbox-placeholder').querySelector('p').textContent='دوباره تلاش کنید یا وضعیت سرویس را بررسی کنید.'; } throw e; }
  finally { if (selected?.id === id) $('refresh').disabled=false; }
}
async function openMessage(id, messageId) { const msg=await call(`/api/mailboxes/${id}/messages/${encodeURIComponent(messageId)}`); if (selected?.id !== id) return; $('messages').classList.add('hidden'); $('pagination').classList.add('hidden'); $('detail-subject').textContent=msg.subject; $('detail-meta').textContent=`${msg.sender} • ${msg.date ? new Date(msg.date).toLocaleString('fa-IR') : ''}`; $('detail-body').textContent=msg.text; $('attachment-note').classList.toggle('hidden', !msg.hasAttachments); $('message-detail').classList.remove('hidden'); }
$('login-form').addEventListener('submit', async e => {e.preventDefault(); const btn=e.currentTarget.querySelector('button'); btn.disabled=true; try {const data=await call('/api/login',{method:'POST',body:JSON.stringify({password:$('admin-password').value})}); csrf=data.csrf; $('admin-password').value=''; view(true); await loadAccounts();} catch(err){fail(err);} finally {btn.disabled=false;} });
$('logout').addEventListener('click', async () => {if (bulkRunning) bulkStop = true; try {await call('/api/logout',{method:'POST'}); selected=null; resetInbox(); const s=await call('/api/state'); csrf=s.csrf; view(false);} catch(err){fail(err);} });
$('create-form').addEventListener('submit', async e => {e.preventDefault(); const btn=e.currentTarget.querySelector('button'); btn.disabled=true; try { const item=await call('/api/mailboxes',{method:'POST',body:JSON.stringify({username:$('username').value})}); $('username').value=''; selected=item; page=1; await loadAccounts(); toast('ایمیل با موفقیت ساخته و ذخیره شد.'); await loadMessages(); } catch(err){fail(err);} finally {btn.disabled=false;} });
$('refresh').addEventListener('click', () => loadMessages().catch(fail)); $('back').addEventListener('click', () => loadMessages().catch(fail)); $('previous').addEventListener('click', () => {if(page>1){page--;loadMessages().catch(fail);}}); $('next').addEventListener('click', () => {page++;loadMessages().catch(fail);});
$('bulk-create').addEventListener('click', () => bulkCreate().catch(e => {toast(e.message || 'خطایی رخ داد.');}));
(async () => {try {const s=await call('/api/state'); csrf=s.csrf; view(s.authenticated); if(s.authenticated) await loadAccounts();} catch(err){view(false);fail(err);} })();
