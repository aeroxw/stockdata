/**
 * 用户控制台渲染冒烟测试（Node 里跑，不需要浏览器）。
 *
 * 做法：抽出 console.html 的内联脚本，用最小 DOM 桩喂真实接口数据，
 * 逐个调用渲染函数，只要抛异常就算失败。
 * 目的不是做视觉校验，而是确保在真实数据结构下模板拼接不会炸、
 * 并且关键字段（余额/套餐/配额）确实渲染出来了。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;
const html = fs.readFileSync(path.join(ROOT, 'static', 'console.html'), 'utf8');
const blocks = [...html.matchAll(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!blocks.length) { console.error('没找到内联脚本'); process.exit(1); }

/* ---------- 假数据（结构与后端返回一致） ---------- */
const PLAN_FREE = {
  code: 'free', name: '免费版', level: 0, price_month: 0, price_year: 0,
  max_keys: 3, rate_limit: 60, daily_quota: 1000,
  features: ['实时行情快照', '日 K 线'], description: '个人尝鲜',
  is_public: true, is_active: true, sort_order: 1,
};
const PLAN_PRO = {
  code: 'pro', name: '专业版', level: 10, price_month: 9900, price_year: 99000,
  max_keys: 10, rate_limit: 300, daily_quota: 50000,
  features: ['包含免费版全部能力', '龙虎榜 / 涨停池', '10 个 API Key'],
  description: '量化爱好者主力档位', is_public: true, is_active: true, sort_order: 2,
  price_month_yuan: '99.00', price_year_yuan: '990.00',
  is_current: true, can_upgrade: false,
};
const PLAN_VIP = {
  code: 'vip', name: '旗舰版', level: 20, price_month: 29900, price_year: 299000,
  max_keys: 50, rate_limit: 1200, daily_quota: -1,
  features: ['包含专业版全部能力', '不限调用量'], description: '机构与高频场景',
  is_public: true, is_active: true, sort_order: 3,
  price_month_yuan: '299.00', price_year_yuan: '2990.00',
  is_current: false, can_upgrade: true,
};
const ME = {
  user_id: 7, email: 'demo@stockdata.dev', balance: 12500, balance_yuan: '125.00',
  plan: PLAN_PRO, plan_expires_at: '2026-10-28T09:00:00', days_left: 29, expired: false,
  quota: { max_keys: 10, rate_limit: 300, daily_quota: 50000, unlimited: false },
  usage: { active_keys: 4, calls_24h: 1234, daily_quota: 50000, unlimited: false, pct: 2.5 },
};
const KEYS = [
  { id: 1, name: '生产环境', key_prefix: 'sk_live_a1b2', masked: 'sk_live_a1b2****',
    scopes: ['quote', 'kline'], rate_limit: 300, is_active: true, total_calls: 8921,
    last_used: '2026-09-28T08:00:00', expires_at: null, created_at: '2026-09-01T08:00:00' },
  { id: 2, name: '回测脚本', key_prefix: 'sk_live_c3d4', masked: 'sk_live_c3d4****',
    scopes: ['quote'], rate_limit: 120, is_active: false, total_calls: 12,
    last_used: null, expires_at: '2026-12-01T00:00:00', created_at: '2026-09-10T08:00:00' },
];
const BILLS = {
  total: 2,
  income: 30000, income_yuan: '300.00',
  outcome: 9900, outcome_yuan: '99.00',
  items: [
    { id: 2, amount: -9900, amount_yuan: '99.00', balance_after: 12500,
      balance_after_yuan: '125.00', type: 'consume', type_label: '套餐消费',
      ref: 'UP20260928AB12CD34', remark: '购买「专业版」月度', created_at: '2026-09-28T08:10:00' },
    { id: 1, amount: 30000, amount_yuan: '300.00', balance_after: 22400,
      balance_after_yuan: '224.00', type: 'recharge', type_label: '充值入账',
      ref: 'RC20260928XY88ZZ99', remark: '账户充值 ¥300.00（mock）', created_at: '2026-09-28T08:05:00' },
  ],
};
const ORDERS = {
  total: 2, items: [
    { id: 2, order_no: 'UP20260928AB12CD34', kind: 'upgrade', title: '「专业版」月度',
      amount: 9900, amount_yuan: '99.00', plan_code: 'pro', period: 'month',
      pay_channel: 'balance', status: 'paid', trade_no: null, remark: '到期时间 2026-10-28',
      created_at: '2026-09-28T08:10:00', paid_at: '2026-09-28T08:10:00' },
    { id: 1, order_no: 'RC20260928XY88ZZ99', kind: 'recharge', title: '账户充值 ¥300.00',
      amount: 30000, amount_yuan: '300.00', plan_code: null, period: null,
      pay_channel: 'mock', status: 'pending', trade_no: null, remark: null,
      created_at: '2026-09-28T08:04:00', paid_at: null },
  ],
};
const USAGE = {
  plan: PLAN_PRO,
  daily: Array.from({ length: 14 }, (_, i) => ({
    date: `2026-09-${String(15 + i).padStart(2, '0')}`,
    total: 100 + i * 37, errors: i % 5 === 0 ? 3 : 0,
  })),
  calls_24h: 1234, calls_7d: 8888, calls_total: 54321, errors_24h: 5, error_rate: 0.41,
  top_paths: [
    { path: '/api/v1/quote', count: 4200 },
    { path: '/api/v1/kline', count: 1300 },
    { path: '/api/v1/public/market', count: 260 },
  ],
  quota: { daily_quota: 50000, unlimited: false, used: 1234, pct: 2.5 },
};

/* ---------- 最小 DOM 桩 ---------- */
const els = new Map();
function makeEl(id) {
  return {
    id, _html: '', _text: '', className: '', value: '', checked: true,
    dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
    appendChild() {}, remove() {}, scrollIntoView() {},
    onclick: null, onchange: null, oninput: null,
  };
}
const document = {
  getElementById(id) { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  querySelectorAll() { return []; },
  createElement() { return makeEl('tmp'); },
  addEventListener() {},
};
const location = { origin: 'http://127.0.0.1:9850', href: 'http://127.0.0.1:9850/console.html' };

const SD = {
  API_BASE: '/api/v1',
  token: 'fake-token',
  async api(p) {
    if (p === '/auth/me') return { data: { id: 7, email: 'demo@stockdata.dev', tier: 'pro', is_admin: false } };
    if (p === '/billing/me') return { data: ME };
    if (p === '/billing/plans') return { data: { items: [PLAN_FREE, PLAN_PRO, PLAN_VIP], current: PLAN_PRO, presets: [{ amount: 5000, yuan: '50.00' }, { amount: 10000, yuan: '100.00' }] } };
    if (p === '/apikey') return { data: KEYS };
    if (p.startsWith('/billing/bills')) return { data: BILLS };
    if (p.startsWith('/billing/orders')) return { data: ORDERS };
    if (p.startsWith('/billing/usage')) return { data: USAGE };
    throw new Error('未桩接的接口: ' + p);
  },
  alert() {}, logout() {}, copy() {},
  fmtTime(d) { return d ? new Date(d).toLocaleString('zh-CN') : '-'; },
  fmtNum(n, d = 2) { return Number(n).toFixed(d); },
  chgClass() { return 'flat'; },
  getQuery() { return null; },
};

const sandbox = {
  document, SD, location,
  localStorage: { getItem: () => 'x', setItem() {}, removeItem() {} },
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
  confirm: () => true, prompt: () => null, alert: () => {},
  URLSearchParams, Math, Date, JSON, Number, String, Object, Array, isNaN, parseInt, parseFloat,
  Promise,
};

let fail = 0;
function check(label, fn) {
  try { fn(); console.log('  PASS  ' + label); }
  catch (e) { fail++; console.log('  FAIL  ' + label + '  -> ' + e.message); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || '断言失败'); }

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(
    blocks.join('\n') +
    '\n;__api = { state, esc, nfmt, setBar, go, loadMe, loadKeys, loadOverview, ' +
    'loadPlanView, renderPlans, renderPresets, pickAmount, updateRcHint, loadOrders, ' +
    'loadBills, loadOrderList, loadUsage, renderUsageChart, renderTopPaths, ' +
    'renderPager, gotoOffset, resetPage, buyPlan, needRecharge, confirmBox };',
    sandbox
  );
  const api = sandbox.__api;
  const st = api.state;
  const $ = (id) => document.getElementById(id);

  console.log('=== 控制台渲染冒烟测试 ===');

  // 1) 概览
  await check('loadOverview 拉套餐/密钥/用量', async () => {});
  try {
    await api.loadOverview();
    console.log('  PASS  loadOverview 执行无异常');
  } catch (e) { fail++; console.log('  FAIL  loadOverview -> ' + e.message); }

  check('套餐名渲染到概览', () => assert($('ovPlan').textContent === '专业版', $('ovPlan').textContent));
  check('余额渲染到概览', () => assert($('ovBalance').textContent === '¥125.00', $('ovBalance').textContent));
  check('活跃密钥数渲染', () => assert(String($('ovKeys').textContent) === '4', String($('ovKeys').textContent)));
  check('到期提示渲染', () => assert(/还剩 29 天/.test($('topExpire').textContent), $('topExpire').textContent));
  check('配额进度条有宽度', () => assert(/%$/.test($('q1').style.width), $('q1').style.width));
  check('套餐权益 kv 渲染', () => assert($('ovBenefits').innerHTML.includes('API Key 上限')));

  // 2) 密钥列表
  try {
    await api.loadKeys();
    console.log('  PASS  loadKeys 执行无异常');
  } catch (e) { fail++; console.log('  FAIL  loadKeys -> ' + e.message); }
  check('密钥表格渲染两行', () => {
    const h = $('keyTableWrap').innerHTML;
    assert(h.includes('生产环境') && h.includes('回测脚本'), '缺少密钥行');
    assert(h.includes('已吊销'), '缺少吊销状态');
  });
  check('密钥统计文案', () => assert(/1 个生效中 \/ 共 2 个/.test($('keysNote').textContent), $('keysNote').textContent));

  // 3) 套餐视图
  try {
    st.me = ME;
    await api.loadPlanView();
    console.log('  PASS  loadPlanView 执行无异常');
  } catch (e) { fail++; console.log('  FAIL  loadPlanView -> ' + e.message); }
  check('三张套餐卡渲染', () => {
    const h = $('planGrid').innerHTML;
    assert(h.includes('免费版') && h.includes('专业版') && h.includes('旗舰版'), '套餐卡不全');
    assert(h.includes('使用中'), '缺少当前套餐标记');
  });
  check('月付价格 99.00', () => assert($('planGrid').innerHTML.includes('99.00')));
  check('余额不足时给"去充值"按钮', () => assert(/还需 ¥/.test($('planGrid').innerHTML), '缺少去充值入口'));
  check('充值档位渲染', () => assert($('rcGrid').innerHTML.includes('¥50.00')));
  check('套餐说明文案', () => assert(/余额 ¥125.00/.test($('planNote').textContent), $('planNote').textContent));

  // 切到年付，价格应该变
  check('切换到年付', () => {
    sandbox.__api.go('plan');
    st.period = 'year';
    api.renderPlans();
    assert($('planGrid').innerHTML.includes('990.00'), '年付价格未生效');
    assert(/年付立省/.test($('planGrid').innerHTML), '缺少省多少钱提示');
  });

  // 4) 充值金额选择
  check('选择充值档位', () => {
    api.pickAmount(10000);
    assert(st.rechargeCents === 10000, String(st.rechargeCents));
    assert(/本次充值 ¥100.00/.test($('rcHint').textContent), $('rcHint').textContent);
  });
  check('needRecharge 自动填入差额', () => {
    api.needRecharge(17400);
    assert(st.rechargeCents === 17400, String(st.rechargeCents));
    assert($('rcCustom').value === '174', $('rcCustom').value);
  });

  // 5) 账单与订单
  try {
    await api.loadBills();
    console.log('  PASS  loadBills 执行无异常');
  } catch (e) { fail++; console.log('  FAIL  loadBills -> ' + e.message); }
  check('流水表格渲染', () => {
    const h = $('billWrap').innerHTML;
    assert(h.includes('套餐消费') && h.includes('充值入账'), '流水类型缺失');
    assert(h.includes('money-out') && h.includes('money-in'), '金额着色缺失');
  });
  check('收支汇总渲染', () => {
    assert($('billIncome').textContent === '¥300.00', $('billIncome').textContent);
    assert($('billOutcome').textContent === '¥99.00', $('billOutcome').textContent);
  });

  try {
    await api.loadOrderList();
    console.log('  PASS  loadOrderList 执行无异常');
  } catch (e) { fail++; console.log('  FAIL  loadOrderList -> ' + e.message); }
  check('订单表格渲染', () => {
    const h = $('orderWrap').innerHTML;
    assert(h.includes('UP20260928AB12CD34'), '订单号缺失');
    assert(h.includes('已支付') && h.includes('待支付'), '订单状态缺失');
  });

  // 6) 待支付订单（含支付按钮）
  try {
    await api.loadOrders();
    console.log('  PASS  loadOrders 执行无异常');
  } catch (e) { fail++; console.log('  FAIL  loadOrders -> ' + e.message); }
  check('待支付订单有支付入口', () => {
    const h = $('pendingWrap').innerHTML;
    assert(h.includes('立即支付'), '缺少支付按钮');
    assert(h.includes('RC20260928XY88ZZ99'), '订单号缺失');
  });

  // 7) 用量
  try {
    await api.loadUsage();
    console.log('  PASS  loadUsage 执行无异常');
  } catch (e) { fail++; console.log('  FAIL  loadUsage -> ' + e.message); }
  check('用量 KPI 渲染', () => {
    assert($('us24').textContent === '1,234', $('us24').textContent);
    assert($('usAll').textContent === '54,321', $('usAll').textContent);
    assert($('usErr').textContent === '0.41%', $('usErr').textContent);
  });
  check('趋势图渲染 14 根柱子', () => {
    const h = $('usChart').innerHTML;
    assert((h.match(/<rect/g) || []).length >= 14, '柱子数量不足');
    assert(h.includes('成功调用') && h.includes('失败调用'), '缺少图例');
  });
  check('Top 接口渲染', () => {
    const h = $('usTop').innerHTML;
    assert(h.includes('/quote') && h.includes('4,200'), 'Top 接口缺失');
  });

  // 8) 分页
  check('分页渲染', () => {
    st.bills.total = 60; st.bills.offset = 20; st.bills.limit = 20;
    api.renderPager('billPager', st.bills);
    assert(/第 2 \/ 3 页/.test($('billPager').innerHTML), $('billPager').innerHTML);
    api.gotoOffset('billPager', -1);
    assert(st.bills.offset === 0, String(st.bills.offset));
  });
  check('数据不足一页时不渲染分页', () => {
    st.orders.total = 5;
    api.renderPager('orderPager', st.orders);
    assert($('orderPager').innerHTML === '', '应为空');
  });

  // 9) 转义（防 XSS）
  check('esc 转义尖括号与引号', () => {
    assert(api.esc('<img src=x onerror=1>') === '&lt;img src=x onerror=1&gt;');
    assert(api.esc('a"b') === 'a&quot;b');
  });
  check('恶意套餐名不会注入标签', () => {
    const evil = { ...PLAN_VIP, name: '<img src=x onerror=alert(1)>', features: ['<script>'] };
    st.plans = [evil];
    api.renderPlans();
    assert(!$('planGrid').innerHTML.includes('<img src=x'), '未转义');
    assert($('planGrid').innerHTML.includes('&lt;img'), '转义结果不符');
  });

  console.log('\n' + (fail ? `失败 ${fail} 项` : '全部通过'));
  process.exit(fail ? 1 : 0);
})();
