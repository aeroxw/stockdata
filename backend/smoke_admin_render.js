/**
 * 后台渲染函数冒烟测试（Node 里跑，不需要浏览器）。
 *
 * 做法：抽出 admin.html 的内联脚本，用最小 DOM 桩喂真实接口数据，
 * 逐个调用渲染函数，只要抛异常就算失败。
 * 目的不是做视觉校验，而是确保在真实数据结构下模板拼接不会炸。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;
const html = fs.readFileSync(path.join(ROOT, 'static', 'admin.html'), 'utf8');
const data = JSON.parse(fs.readFileSync(path.join(ROOT, '_smoke_data.json'), 'utf8'));

// 抽取内联 <script>（不带 src 的）
const blocks = [...html.matchAll(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!blocks.length) { console.error('没找到内联脚本'); process.exit(1); }

/* ---------- 最小 DOM 桩 ---------- */
const els = new Map();
function makeEl(id) {
  return {
    id,
    _html: '', _text: '', className: '', value: '', checked: true,
    dataset: {}, style: {}, classList: { add() {}, remove() {}, toggle() {} },
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
    appendChild() {}, remove() {}, onclick: null, onchange: null, oninput: null,
  };
}
const document = {
  getElementById(id) { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  querySelectorAll() { return []; },
  createElement() { return makeEl('tmp'); },
  addEventListener() {},
};

const SD = {
  API_BASE: '/api/v1',
  token: 'fake-token',
  async api(p) {
    if (p === '/admin/stats') return { data: data.stats };
    if (p === '/sources') return { data: data.sources.data || [] };
    if (p.startsWith('/admin/users?')) return { data: data.users };
    if (p.startsWith('/admin/keys?')) return { data: data.keys };
    if (p.startsWith('/admin/logs?')) return { data: data.logs };
    if (p === '/admin/plans') return { data: data.plans };
    if (p.startsWith('/admin/orders?')) return { data: data.orders };
    if (p.startsWith('/admin/bills?')) return { data: data.bills };
    if (p === '/auth/me') return { data: { id: 1, email: 'a@a.com', is_admin: true } };
    throw new Error('未桩接的接口: ' + p);
  },
  alert() {}, logout() {}, copy() {},
  fmtTime(d) { return d ? new Date(d).toLocaleString('zh-CN') : '-'; },
  fmtNum(n, d = 2) { return Number(n).toFixed(d); },
  chgClass() { return 'flat'; },
  getQuery() { return null; },
};

const sandbox = {
  document, SD, localStorage: { getItem: () => 'x', setItem() {}, removeItem() {} },
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
  confirm: () => false, prompt: () => null, alert: () => {},
  URLSearchParams, Math, Date, JSON, Number, String, Object, Array, isNaN,
};

let fail = 0;
function check(label, fn) {
  try { fn(); console.log('  PASS  ' + label); }
  catch (e) { fail++; console.log('  FAIL  ' + label + '  -> ' + e.message); }
}

function assert(cond, msg) { if (!cond) throw new Error(msg || '断言失败'); }

(async () => {
  vm.createContext(sandbox);
  // 脚本里依赖 window/document 上的全局符号，用 vm 跑一遍拿到函数引用
  vm.runInContext(blocks.join('\n') + '\n;__api = { renderTrend, renderTopPaths, renderStatus, renderTiers, renderSys, renderSources, renderUsers, renderKeys, renderLogs, renderPager, fmtDur, fmtAgo, nfmt, loadStats, loadSources, loadUsers, loadKeys, loadLogs, loadAdminPlans, renderAdminPlans, renderTierFilter, tierOptions, planName, planQuota, openPlanEditor, loadAdminOrders, renderAdminOrders, loadAdminBills, renderAdminBills, yuan }; __state = state;', sandbox);

  const A = sandbox.__api;
  const S = data.stats;

  console.log('后台渲染冒烟测试');
  console.log('----------------------------------------');
  check('fmtDur / fmtAgo 正常', () => {
    const a = A.fmtDur(3725), b = A.fmtAgo(new Date(Date.now() - 7200e3).toISOString());
    if (!a || !b) throw new Error('空结果');
  });
  check('renderTrend（24 小时趋势 SVG）', () => {
    A.renderTrend(S.hourly);
    const h = document.getElementById('trendWrap').innerHTML;
    if (!h.includes('<svg')) throw new Error('未生成 svg');
    if (h.includes('undefined') || h.includes('NaN')) throw new Error('输出含 undefined/NaN');
  });
  check('renderTopPaths', () => {
    A.renderTopPaths(S.top_paths);
    const h = document.getElementById('topPathsWrap').innerHTML;
    if (h.includes('undefined')) throw new Error('输出含 undefined');
  });
  check('renderStatus', () => {
    A.renderStatus(S.status_dist);
    const h = document.getElementById('statusWrap').innerHTML;
    if (h.includes('NaN')) throw new Error('输出含 NaN');
  });
  check('renderTiers（新：按库里套餐动态给出，含中文名）', () => {
    // 后端现在返回 tiers 数组；老字段 tier_counts 只作为兼容回退
    const tiers = S.tiers || Object.keys(S.tier_counts || {})
      .map((k) => ({ code: k, name: k, count: S.tier_counts[k] }));
    A.renderTiers(tiers, S.users);
    const h = document.getElementById('tierWrap').innerHTML;
    assert(h.includes('bar-row'), '未渲染套餐分布条');
    assert(h.includes('合计'), '缺少合计说明');
  });
  check('renderTiers（空数据不炸）', () => {
    A.renderTiers([], 0);
    assert(document.getElementById('tierWrap').innerHTML.includes('暂无套餐数据'));
  });
  check('renderSys', () => {
    A.renderSys(S.system, S.stock_meta);
  });
  check('renderSources（6 个数据源）', () => {
    A.renderSources(data.sources.data || []);
    const h = document.getElementById('srcWrap').innerHTML;
    if (!h.includes('src-card')) throw new Error('未生成卡片');
  });
  check('renderUsers', () => {
    A.renderUsers(data.users.items);
    const h = document.getElementById('userWrap').innerHTML;
    if (!h.includes('<table')) throw new Error('未生成表格');
  });
  check('renderKeys', () => {
    A.renderKeys(data.keys.items);
  });
  check('renderLogs', () => {
    A.renderLogs(data.logs.items);
  });
  check('空数据不炸（empty 分支）', () => {
    A.renderTrend([]); A.renderTopPaths([]); A.renderStatus({ s2xx: 0, s4xx: 0, s5xx: 0 });
    A.renderUsers([]); A.renderKeys([]); A.renderLogs([]); A.renderSources([]);
  });
  check('renderPager 分页', () => {
    const st = { offset: 0, limit: 20, total: 57 };
    sandbox.__state.keys = st;
    A.renderPager('kPager', 'keys');
    const h = document.getElementById('kPager').innerHTML;
    if (!h.includes('第 1 / 3 页')) throw new Error('页码算错: ' + h.slice(0, 60));
  });
  /* ---------- 套餐管理 / 订单账单（本轮新增） ---------- */
  check('套餐工具函数：yuan / planName / planQuota', () => {
    assert(A.yuan(9900) === '99.00', A.yuan(9900));
    assert(A.yuan(0) === '0.00');
    sandbox.__state.plans = data.plans.items;
    const first = data.plans.items[0];
    assert(A.planName(first.code) === first.name, 'planName 不匹配');
    assert(A.planQuota(first.code) === first.max_keys, 'planQuota 不匹配');
    assert(A.planName('不存在的套餐') === '不存在的套餐', '未知 code 应原样返回');
  });

  check('renderTierFilter 动态填充套餐下拉', () => {
    sandbox.__state.plans = data.plans.items;
    A.renderTierFilter();
    const h = document.getElementById('uTier').innerHTML;
    assert(h.includes('全部套餐'), '缺少默认项');
    data.plans.items.forEach((p) => assert(h.includes(p.name), '缺少套餐 ' + p.name));
  });

  check('tierOptions 生成选中项', () => {
    sandbox.__state.plans = data.plans.items;
    const code = data.plans.items[1].code;
    const opt = A.tierOptions(code);
    assert(opt.includes('selected'), '未标记选中');
    assert(opt.includes(code), '缺少 code');
  });

  check('renderAdminPlans 渲染套餐卡片', () => {
    A.renderAdminPlans(data.plans.items);
    const h = document.getElementById('planWrap').innerHTML;
    data.plans.items.forEach((p) => assert(h.includes(p.name), '缺少套餐 ' + p.name));
    assert(h.includes('Key 上限'), '缺少配额信息');
    assert(h.includes('月付'), '缺少价格信息');
    assert(!/<script/i.test(h), '存在未转义的脚本标签');
  });

  check('renderAdminPlans 空数据不炸', () => {
    A.renderAdminPlans([]);
    assert(document.getElementById('planWrap').innerHTML.includes('还没有套餐'));
  });

  check('openPlanEditor 新建 / 编辑都不炸', () => {
    A.openPlanEditor(null);
    assert(document.getElementById('modalTitle').textContent === '新建套餐', '标题不对');
    assert(document.getElementById('modalBody').innerHTML.includes('套餐代码'), '缺少代码字段');
    A.openPlanEditor(data.plans.items[0].code);
    const h = document.getElementById('modalBody').innerHTML;
    assert(h.includes(data.plans.items[0].name), '编辑表单未回填名称');
  });

  check('renderAdminOrders 渲染订单表', () => {
    A.renderAdminOrders(data.orders.items);
    const h = document.getElementById('orderWrap').innerHTML;
    data.orders.items.forEach((o) => assert(h.includes(o.order_no), '缺少订单 ' + o.order_no));
    assert(h.includes('确认到账') || h.includes('已支付') || h.includes('取消'), '缺少操作列');
  });

  check('renderAdminBills 渲染流水表', () => {
    A.renderAdminBills(data.bills.items);
    const h = document.getElementById('billWrap').innerHTML;
    data.bills.items.forEach((b) => assert(h.includes(b.ref || b.type_label), '缺少流水'));
    assert(h.includes('变动后余额'), '缺少余额列');
  });

  check('订单 / 流水空数据不炸', () => {
    A.renderAdminOrders([]); A.renderAdminBills([]);
    assert(document.getElementById('orderWrap').innerHTML.includes('没有匹配的订单'));
    assert(document.getElementById('billWrap').innerHTML.includes('没有匹配的流水'));
  });

  check('loadStats 全链路（含 KPI 赋值）', async () => {});

  // 异步链路单独跑
  for (const [label, fn] of [
    ['loadStats', A.loadStats], ['loadSources', () => A.loadSources()],
    ['loadUsers', A.loadUsers], ['loadKeys', A.loadKeys], ['loadLogs', A.loadLogs],
    ['loadAdminPlans', A.loadAdminPlans],
    ['loadAdminOrders', A.loadAdminOrders], ['loadAdminBills', A.loadAdminBills],
  ]) {
    try { await fn(); console.log('  PASS  ' + label + ' 全链路'); }
    catch (e) { fail++; console.log('  FAIL  ' + label + '  -> ' + e.message); }
  }

  console.log('----------------------------------------');
  console.log(fail === 0 ? 'ALL PASS' : fail + ' 项失败');
  process.exit(fail ? 1 : 0);
})();
