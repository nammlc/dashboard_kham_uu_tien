// Test pop-up dùng chung (templates/_rec_popup.html) trên Node, với DOM giả tối thiểu.
// Chạy: node tests/js/popup.test.js     (hoặc python -m unittest tests.test_js)
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const html = fs.readFileSync(path.join(__dirname, '..', '..', 'templates', '_rec_popup.html'), 'utf8');
const script = html.split('{% raw %}')[1].split('{% endraw %}')[0].replace(/^\s*<script>/, '').replace(/<\/script>\s*$/, '');

// ───────── DOM giả ─────────
class ClassList {
  constructor() { this.s = new Set(); }
  add(...c) { c.forEach(x => this.s.add(x)); }
  remove(...c) { c.forEach(x => this.s.delete(x)); }
  contains(c) { return this.s.has(c); }
  toggle(c, f) { const on = f === undefined ? !this.s.has(c) : !!f; on ? this.s.add(c) : this.s.delete(c); return on; }
}
class El {
  constructor(tag = 'div') {
    this.tag = tag; this.classList = new ClassList(); this.children = []; this.attrs = {}; this.handlers = {};
    this.textContent = ''; this._html = ''; this.className = ''; this.type = '';
  }
  set innerHTML(v) { this._html = v; this.children = []; }
  get innerHTML() { return this._html; }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, f) { (this.handlers[t] = this.handlers[t] || []).push(f); }
  focus() { document.activeElement = this; }
  querySelectorAll(sel) { return sel === 'button' ? this.children.filter(c => c.tag === 'button') : []; }
  querySelector(sel) {
    if (sel === 'button') return this.children.find(c => c.tag === 'button') || null;
    if (sel === '[data-primary]') return this.children.find(c => c.attrs['data-primary']) || null;
    return null;
  }
  click() { (this.handlers.click || []).forEach(f => f({ target: this })); }
}
const ids = {};
['rec-modal', 'rec-m-icon', 'rec-m-title', 'rec-m-text', 'rec-m-list', 'rec-m-sub', 'rec-m-actions'].forEach(i => ids[i] = new El());
ids['rec-modal'].classList.add('hidden');
global.document = {
  activeElement: null, body: Object.assign(new El(), {}),
  getElementById: i => ids[i], createElement: t => new El(t), addEventListener() {},
};
global.window = {};
global.location = { href: '/doi-chieu', hash: '', reloaded: 0, reload() { this.reloaded++; } };
let fetchLog = [], fetchImpl = null;
global.fetch = (u, o) => { fetchLog.push({ url: u, opts: o }); return fetchImpl(u, o); };
(0, eval)(script + '\n;globalThis.RecPopup = window.RecPopup;');
const P = globalThis.RecPopup;

// ───────── tiện ích ─────────
const tick = () => new Promise(r => setImmediate(r));
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };
const modalOpen = () => !ids['rec-modal'].classList.contains('hidden');
const title = () => ids['rec-m-title'].textContent;
const sub = () => ids['rec-m-sub'].textContent;
const buttons = () => ids['rec-m-actions'].children;
const clickBtn = label => { const b = buttons().find(x => x.textContent === label); assert(b, 'không thấy nút "' + label + '" (có: ' + buttons().map(x => x.textContent) + ')'); b.click(); };
const json = (body, status = 200) => ({ status, redirected: false, headers: { get: () => 'application/json' }, json: async () => body });
const html404 = () => ({ status: 404, redirected: false, headers: { get: () => 'text/html' }, json: async () => { throw new Error('not json'); } });

class FakeFD { constructor() { this.m = {}; } set(k, v) { this.m[k] = v; } get(k) { return this.m[k]; } }
// Form giả mô phỏng ĐÚNG hành vi trình duyệt: có nút name="action" thì form.action trả về chính nút đó (DOM clobbering)
class FakeForm {
  constructor({ action, withActionButton, dataset }) {
    this._action = action; this._clobber = withActionButton ? new El('button') : null;
    this.dataset = dataset || {}; this.handlers = {};
  }
  get action() { return this._clobber || this._action; }
  getAttribute(n) { return n === 'action' ? this._action : null; }
  addEventListener(t, f) { (this.handlers[t] = this.handlers[t] || []).push(f); }
  async submit(submitter) { for (const f of this.handlers.submit) await f({ preventDefault() { this.prevented = true; }, submitter }); }
}
const reset = () => {
  fetchLog = []; fetchImpl = null; location.reloaded = 0; location.hash = ''; location.href = '/doi-chieu';
  P.state.busy = false; ids['rec-modal'].classList.add('hidden');
};
const env = { makeFormData: () => new FakeFD() };
const actionBtn = value => Object.assign(new El('button'), { name: 'action', value });

let n = 0;
const test = async (name, fn) => { reset(); await fn(); console.log('ok', ++n, name); };

(async () => {
  await test('form có nút name="action": URL gửi đi là chuỗi /doi-chieu/xu-ly (không phải [object HTMLButtonElement])', async () => {
    const form = new FakeForm({ action: '/doi-chieu/xu-ly', withActionButton: true, dataset: { anchor: 'buoc3' } });
    assert.strictEqual(typeof form.action, 'object', 'giả lập phải tái hiện lỗi ghi đè của trình duyệt');
    fetchImpl = async () => json({ ok: true, count: 1, message: 'xong' });
    P.bindForm(form, () => ({ anchor: 'buoc3' }), env);
    const p = form.submit(actionBtn('confirm'));
    await settle(); clickBtn('Tiếp tục ngay'); await p;
    assert.strictEqual(fetchLog.length, 1);
    assert.strictEqual(fetchLog[0].url, '/doi-chieu/xu-ly');
    assert.strictEqual(typeof fetchLog[0].url, 'string');
  });

  await test('nút bấm được gửi kèm (action=confirm) và header X-Requested-With', async () => {
    const form = new FakeForm({ action: '/doi-chieu/xu-ly', withActionButton: true });
    let fdSeen = null;
    fetchImpl = async (u, o) => { fdSeen = o.body; return json({ ok: true, count: 1, message: 'ok' }); };
    P.bindForm(form, () => ({}), env);
    const p = form.submit(actionBtn('confirm')); await settle();
    assert.strictEqual(fdSeen.get('action'), 'confirm');
    assert.strictEqual(fetchLog[0].opts.headers['X-Requested-With'], 'fetch');
    clickBtn('Tiếp tục ngay'); await p;
  });

  await test('luồng đầy đủ: hỏi xác nhận → đang ghi → thành công → tải lại trang (giữ anchor)', async () => {
    const form = new FakeForm({ action: '/doi-chieu/cap-nhat', dataset: { anchor: 'buoc2' } });
    fetchImpl = async () => json({ ok: true, count: 3, message: 'Đã cập nhật 3', names: ['A', 'B', 'C'] });
    P.bindForm(form, () => ({ anchor: 'buoc2', confirm: { title: 'Cập nhật 3?', text: 't', okLabel: 'Xác nhận cập nhật' } }), env);
    const p = form.submit(null); await settle();
    assert(modalOpen()); assert.strictEqual(title(), 'Cập nhật 3?'); assert.strictEqual(fetchLog.length, 0, 'chưa được gửi trước khi xác nhận');
    clickBtn('Xác nhận cập nhật'); await settle();
    assert.strictEqual(title(), 'Cập nhật thành công'); assert.strictEqual(ids['rec-m-list'].children.length, 3);
    clickBtn('Tiếp tục ngay'); await p; await settle();
    assert.strictEqual(location.reloaded, 1); assert.strictEqual(location.hash, 'buoc2');
  });

  await test('server trả redirect (import xong) → chuyển trang thay vì tải lại', async () => {
    const form = new FakeForm({ action: '/import/confirm' });
    fetchImpl = async () => json({ ok: true, count: 5, message: 'Đã thêm 5', redirect: '/import' });
    P.bindForm(form, () => ({}), env);
    const p = form.submit(null); await settle(); clickBtn('Tiếp tục ngay'); await p; await settle();
    assert.strictEqual(location.href, '/import'); assert.strictEqual(location.reloaded, 0);
  });

  await test('bấm Hủy ở hộp xác nhận → không gửi gì', async () => {
    const form = new FakeForm({ action: '/x' });
    fetchImpl = async () => json({ ok: true });
    P.bindForm(form, () => ({ confirm: { title: 'T', text: '', okLabel: 'OK' } }), env);
    const p = form.submit(null); await settle(); clickBtn('Hủy'); await p;
    assert.strictEqual(fetchLog.length, 0); assert(!modalOpen());
  });

  await test('buildOpts trả null (chưa hợp lệ) → không gửi, không mở pop-up', async () => {
    const form = new FakeForm({ action: '/x' });
    P.bindForm(form, () => null, env);
    await form.submit(null);
    assert.strictEqual(fetchLog.length, 0); assert(!modalOpen());
  });

  await test('lỗi bất ngờ trong code giao diện → pop-up "Lỗi giao diện" (không im lặng)', async () => {
    const form = new FakeForm({ action: '/x' });
    P.bindForm(form, () => { throw new Error('boom trong buildOpts'); }, env);
    const p = form.submit(null); await settle();
    assert.strictEqual(title(), 'Lỗi giao diện'); assert(/boom/.test(sub()));
    clickBtn('Tải lại trang'); await p; await settle();
    assert.strictEqual(location.reloaded, 1); assert.strictEqual(fetchLog.length, 0);
  });

  await test('HTTP 404 (HTML) → pop-up nêu rõ 404 + URL, không cho "Thử lại" vô ích', async () => {
    const form = new FakeForm({ action: '/doi-chieu/xu-ly' });
    fetchImpl = async () => html404();
    P.bindForm(form, () => ({}), env);
    const p = form.submit(null); await settle();
    assert(/HTTP 404/.test(title())); assert(/\/doi-chieu\/xu-ly/.test(sub()));
    assert(!buttons().some(b => b.textContent === 'Thử lại'));
    clickBtn('Đóng'); await p;
  });

  await test('lỗi nghiệp vụ từ server → hiện đúng thông điệp; "Thử lại" gửi lại và thành công', async () => {
    const form = new FakeForm({ action: '/x' });
    let k = 0;
    fetchImpl = async () => (++k === 1 ? json({ ok: false, level: 'error', message: 'Google đang giới hạn' }) : json({ ok: true, count: 1, message: 'ổn rồi' }));
    P.bindForm(form, () => ({}), env);
    const p = form.submit(null); await settle();
    assert.strictEqual(ids['rec-m-text'].textContent, 'Google đang giới hạn');
    clickBtn('Thử lại'); await settle(); clickBtn('Tiếp tục ngay'); await p; await settle();
    assert.strictEqual(fetchLog.length, 2); assert.strictEqual(location.reloaded, 1);
  });

  await test('server không phản hồi (timeout) → báo "chưa phản hồi", khuyên tải lại; không tải lại tự động', async () => {
    const form = new FakeForm({ action: '/x' });
    P.deps.timeoutMs = 30;
    fetchImpl = (u, o) => new Promise((_, rej) => o.signal.addEventListener('abort', () => rej(Object.assign(new Error('a'), { name: 'AbortError' }))));
    P.bindForm(form, () => ({}), env);
    const p = form.submit(null); await new Promise(r => setTimeout(r, 120)); await settle();
    delete P.deps.timeoutMs;
    assert.strictEqual(title(), 'Máy chủ chưa phản hồi'); assert(/Hết thời gian chờ/.test(sub()));
    assert.strictEqual(location.reloaded, 0);
    clickBtn('Đóng'); await p;
  });

  await test('mất mạng / hết phiên đăng nhập có thông báo riêng', async () => {
    let form = new FakeForm({ action: '/x' });
    fetchImpl = async () => { throw new TypeError('Failed to fetch'); };
    P.bindForm(form, () => ({}), env);
    let p = form.submit(null); await settle();
    assert.strictEqual(title(), 'Mất kết nối tới máy chủ'); clickBtn('Đóng'); await p;
    reset(); form = new FakeForm({ action: '/x' });
    fetchImpl = async () => ({ status: 200, redirected: true, headers: { get: () => 'text/html' } });
    P.bindForm(form, () => ({}), env);
    p = form.submit(null); await settle();
    assert.strictEqual(title(), 'Phiên đăng nhập đã hết hạn'); clickBtn('Đóng'); await p;
  });

  await test('chống bấm đúp: lần gửi thứ 2 khi đang chạy bị bỏ qua', async () => {
    const form = new FakeForm({ action: '/x' });
    let release; fetchImpl = () => new Promise(r => { release = () => r(json({ ok: true, count: 1, message: 'm' })); });
    P.bindForm(form, () => ({}), env);
    const p1 = form.submit(null); await settle();
    await form.submit(null);
    assert.strictEqual(fetchLog.length, 1);
    release(); await settle(); clickBtn('Tiếp tục ngay'); await p1;
  });

  await test('thành công MỘT PHẦN: hiện lý do từng ca, không tự chuyển trang cho tới khi bấm', async () => {
    const form = new FakeForm({ action: '/doi-chieu/cap-nhat', dataset: { anchor: 'buoc2' } });
    fetchImpl = async () => json({ ok: true, level: 'warning', count: 2, message: 'Đã cập nhật 2, còn 1 lỗi',
                                   names: ['A', 'B'], problems: ['C: dòng đã đổi chỗ'], more_problems: 0 });
    P.bindForm(form, () => ({ anchor: 'buoc2' }), env);
    const p = form.submit(null); await settle();
    assert.strictEqual(title(), 'Cập nhật một phần');
    assert.strictEqual(ids['rec-m-list'].children.length, 1);
    assert(/C: dòng đã đổi chỗ/.test(ids['rec-m-list'].children[0].textContent));
    await new Promise(r => setTimeout(r, 2500));                        // quá 2,2 giây vẫn phải đang chờ người dùng
    assert.strictEqual(location.reloaded, 0); assert(modalOpen());
    clickBtn('Đã hiểu — tải lại trang'); await p; await settle();
    assert.strictEqual(location.reloaded, 1); assert.strictEqual(location.hash, 'buoc2');
  });

  await test('thất bại hoàn toàn kèm danh sách lý do → lý do hiện trong nội dung pop-up', async () => {
    const form = new FakeForm({ action: '/x' });
    fetchImpl = async () => json({ ok: false, level: 'error', message: 'Không cập nhật được ca nào.', problems: ['A: lỗi 1', 'B: lỗi 2'] });
    P.bindForm(form, () => ({}), env);
    const p = form.submit(null); await settle();
    assert(/A: lỗi 1/.test(ids['rec-m-text'].textContent) && /B: lỗi 2/.test(ids['rec-m-text'].textContent));
    clickBtn('Đóng'); await p;
  });

  console.log('\nALL JS OK (' + n + ' tests)');
  process.exit(0);
})().catch(e => { console.error('FAIL:', e); process.exit(1); });
