/* ============================================================
   StockData 前端共用工具
   - token 管理
   - API 请求封装
   - 提示框
   ============================================================ */

const SD = {
  /* 所有业务接口的统一前缀。
     后端在 main.py 里用 prefix="/api/v1" 挂载 auth/apikey/admin，
     页面里一律传相对路径（如 '/auth/login'），由这里拼前缀，
     避免以后改版本号时又要满项目找字符串。 */
  API_BASE: '/api/v1',

  get token() {
    return localStorage.getItem('sd_token') || '';
  },
  set token(v) {
    if (v) localStorage.setItem('sd_token', v);
    else localStorage.removeItem('sd_token');
  },

  /* refresh_token 有 7 天寿命，access_token 只有 2 小时。
     这里必须一并保存，否则用户每两小时就会被踢回登录页。 */
  get refreshToken() {
    return localStorage.getItem('sd_refresh') || '';
  },
  set refreshToken(v) {
    if (v) localStorage.setItem('sd_refresh', v);
    else localStorage.removeItem('sd_refresh');
  },

  /* 登录成功后统一从这里落盘，避免各处漏存 refresh_token */
  saveSession(data) {
    if (!data) return;
    if (data.access_token) this.token = data.access_token;
    if (data.refresh_token) this.refreshToken = data.refresh_token;
  },

  logout() {
    this.token = '';
    this.refreshToken = '';
    location.href = '/';
  },

  /* 用 refresh_token 换新令牌。并发调用时共用同一个 Promise，
     避免多个请求同时 401 时打出 N 次刷新请求。 */
  _refreshing: null,
  _refresh() {
    if (this._refreshing) return this._refreshing;
    if (!this.refreshToken) return Promise.resolve(false);

    const url = this.API_BASE + '/auth/refresh';
    this._refreshing = fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: this.refreshToken }),
    })
      .then(async (res) => {
        if (!res.ok) return false;
        const json = await res.json().catch(() => null);
        if (!json || !json.data || !json.data.access_token) return false;
        this.saveSession(json.data);
        return true;
      })
      .catch(() => false)
      .finally(() => {
        this._refreshing = null;
      });

    return this._refreshing;
  },

  /* 带鉴权的请求。path 传相对路径即可，外部绝对 URL 原样透传 */
  async api(path, options = {}, _retried = false) {
    const headers = Object.assign(
      { 'Content-Type': 'application/json' },
      options.headers || {}
    );
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;

    const url = /^https?:\/\//.test(path) ? path : this.API_BASE + path;

    // fetch 本身失败（服务没起来、断网、被网关断开）要和"接口返回了错误"区分开。
    // 否则用户只会看到一句没头没脑的 "Failed to fetch"。
    let res;
    try {
      res = await fetch(url, { ...options, headers });
    } catch (e) {
      throw new Error('无法连接服务器，请检查服务是否已启动（' + url + '）');
    }

    // 204 之类没有响应体的情况不要当成解析失败
    const text = await res.text();
    let json = null;
    if (text) {
      try {
        json = JSON.parse(text);
      } catch {
        // 404/502 时后端可能直接吐一段 HTML 错误页，这时 json 保持 null，
        // 交给下面的状态码分支给出更准确的提示
        if (res.ok) throw new Error('响应解析失败');
      }
    }
    json = json || {};

    // 401：access_token 多半只是过期了。先尝试用 refresh_token 续期并重放原请求；
    // 只有续期也失败（refresh 也过期 / 被吊销）才真正登出。
    // 加 _retried 标记防止无限递归。
    if (res.status === 401 && this.token) {
      if (!_retried && this.refreshToken) {
        const ok = await this._refresh();
        if (ok) return this.api(path, options, true);
      }
      this.token = '';
      this.refreshToken = '';
      location.href = '/login.html';
      throw new Error('登录已失效，请重新登录');
    }
    // 404 有两种可能：路径真的不存在（前端写错），或反向代理把请求转丢了。
    // 区分不了时给一句能指导操作的提示，而不是干巴巴的"接口不存在"。
    if (res.status === 404) {
      throw new Error(
        json.detail
          ? `${json.detail}（${url}）`
          : `接口不存在或服务未就绪：${url}`
      );
    }
    if (res.status === 502 || res.status === 503 || res.status === 504) {
      throw new Error(`服务暂时不可用（${res.status}），请稍后重试`);
    }
    if (!res.ok || (json.code && json.code !== 0)) {
      throw new Error(json.msg || json.detail || `请求失败 (${res.status})`);
    }
    return json;
  },

  /* 简单提示 */
  alert(container, msg, type = 'info') {
    const el = document.createElement('div');
    el.className = `alert alert-${type}`;
    el.textContent = msg;
    container.prepend(el);
    setTimeout(() => el.remove(), 5000);
  },

  fmtTime(d) {
    if (!d) return '-';
    const dt = new Date(d);
    if (isNaN(dt)) return d;
    return dt.toLocaleString('zh-CN', { hour12: false });
  },

  fmtNum(n, digits = 2) {
    if (n === null || n === undefined) return '-';
    return Number(n).toFixed(digits);
  },

  /* 涨跌着色（A股：涨红跌绿） */
  chgClass(v) {
    return v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
  },

  /* 复制文本 */
  copy(text) {
    navigator.clipboard.writeText(text).then(
      () => {},
      () => {}
    );
  },

  getQuery(name) {
    return new URLSearchParams(location.search).get(name);
  },
};
