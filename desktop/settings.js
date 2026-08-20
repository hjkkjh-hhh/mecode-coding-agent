/* 设置面板：通用 / 模型 / 技能 / MCP / 开发者 / 关于。

   与 TUI 斜杠命令的对应关系（桌面端要有 TUI 的全部能力）：
     /config /model → 模型　　/skill → 技能　　/mcp → MCP
     /system        → 开发者　/tool  → 关于　　/help → 通用页底部的快捷键表

   为什么单独一个文件：index.html 已经是"页面 + 全部样式 + 主循环"的单文件，
   设置面板自成一块（五个面板、各自拉数据、互不相干），塞进去会把主循环埋掉。
   它是【经典脚本】，和 index.html 的内联脚本共享同一个全局词法环境，
   所以直接用得上那边的 $ / esc / api / get / modal / notice / applyTheme / themeKey。
   加载顺序因此是硬要求：本文件必须排在内联脚本【之后】。
   同理，index.html 里绑按钮要写成 onclick = () => openSettings()——
   写成 onclick = openSettings 会在绑定那一刻取到 undefined。

   数据流：每个面板自己 fetch 自己的接口，切到哪个才拉哪个（懒加载）。
   改完【就地重拉本面板】而不是整窗重建——不然每点一个开关，滚动位置就跳回顶部。
*/
'use strict';

const SET_TABS = [
  { k: 'general', t: '通用' },
  { k: 'model', t: '模型' },
  { k: 'skills', t: '技能' },
  { k: 'mcp', t: 'MCP' },
  { k: 'dev', t: '开发者' },
  { k: 'about', t: '关于' },
];

/* 快捷键表。写在这里而不是散落在各处：它同时是"帮助"面板的内容来源，
   两边分开写迟早对不上（改了绑定忘了改帮助）。 */
const SET_KEYS = [
  ['Enter', '发送；agent 在跑时则排队，下一轮开头注入'],
  ['Shift + Enter', '输入框内换行'],
  ['Esc', '关掉弹窗 / 收起模式菜单'],
  ['点上下文环', '立刻压缩一次（环满 = 下一轮自动压）'],
  ['点模型名', '直接打开设置的模型页'],
];

let setMask = null;          // 当前打开的设置窗（null = 没开）
let setTab = 'general';
let setCfg = null;           // /api/config 的最近一份，供面板内的联动读取
let addMode = 'preset';      // 新增模型停在哪个子页：一键 / 手动
let addProv = 0;             // 一键页选中的提供商下标

/* ---------- 小组件 ---------- */

const swHtml = (on, act, extra) =>
  `<button class="sw${on ? ' on' : ''}" role="switch" aria-checked="${on}" data-act="${act}"
     ${extra || ''}><span class="knob"></span></button>`;

const rowHtml = (title, desc, right) =>
  `<div class="set-row"><div class="lab"><div class="t">${title}</div>` +
  `${desc ? `<div class="d">${desc}</div>` : ''}</div>${right || ''}</div>`;

/** 面板里的一行提示条。kind: ok / warn / bad */
const hintHtml = (kind, text) => `<div class="set-hint ${kind}">${esc(text)}</div>`;

/* ---------- 主窗 ---------- */

function openSettings(tab) {
  if (setMask) { setMask.remove(); setMask = null; }
  setTab = tab || setTab;
  setMask = modal(`
    <div class="set-head"><h2>设置</h2><button class="btn" data-close="1">关闭</button></div>
    <div class="set-body">
      <div class="set-nav">
        ${SET_TABS.map(t => `<button data-tab="${t.k}">${t.t}</button>`).join('')}
      </div>
      <div class="set-main" id="set-main"><div class="set-load">载入中…</div></div>
    </div>`, onSetClick, 'settings');
  setMask.addEventListener('change', onSetChange);
  setMask.addEventListener('input', e => {                   // 上限/阈值一改就重算触发点
    if (e.target.id === 'pf-cap' || e.target.id === 'pf-th') paintTrigger();
  });
  setMask.addEventListener('keydown', e => {                 // 输入框里回车 = 点它旁边的主按钮
    if (e.key !== 'Enter' || e.target.tagName !== 'INPUT') return;
    const go = e.target.closest('.set-form, .set-row');
    const btn = go && go.querySelector('.btn.primary');
    if (btn) { e.preventDefault(); btn.click(); }
  });
  paintTab();
}

/** 重画当前面板。【返回 promise】：面板内容是 await 出来的，
    调用方要等它画完才能往里写提示——不然提示写进了马上被替换掉的旧节点。 */
let painted = '';                    // 上次画的是哪个页签，用来决定要不要把滚动条拉回顶

/* 重画编号。切页签切得快时两次重画会并行，先发的请求可能后回来——
   回来时若已经不是最新那次，它写下的就是【上一页的内容】，而左边导航高亮的是新页。
   每个面板 await 完先过这道闸。默认值 paintSeq 是给直接调用（测试）留的：不传就不算过期。 */
let paintSeq = 0;

function paintTab() {
  if (!setMask) return Promise.resolve();
  setMask.querySelectorAll('.set-nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === setTab));
  const box = $('set-main');
  // 【不立刻清空】。接口都在本机，几十毫秒就回来；先清空再填等于每次切页都闪一帧白。
  // 只有真的慢下来（>150ms）才把"载入中"顶上去——快的时候用户根本看不到中间态。
  const slow = setTimeout(() => { box.innerHTML = '<div class="set-load">载入中…</div>'; }, 150);
  const swap = setTab, seq = ++paintSeq;
  const settle = () => {
    clearTimeout(slow);
    if (seq !== paintSeq) return;                  // 已经有更晚的一次重画，别去动它的滚动位置
    // 换页签才回顶；原地重画（保存后刷新那种）保留滚动位置，不然刚改的那行会跳走
    if (swap !== painted) { painted = swap; if (box.scrollTo) box.scrollTo({ top: 0, behavior: 'auto' }); }
  };
  return ({ general: paneGeneral, model: paneModel, skills: paneSkills, mcp: paneMcp,
            dev: paneDev, about: paneAbout }[setTab])(box, seq)
    .then(r => { settle(); return r; }, e => { settle(); throw e; });
}

/** 事件委托。面板内容是反复重画的，逐个挂 onclick 会在重画后全部失效。 */
function onSetClick(e) {
  const tabBtn = e.target.closest('[data-tab]');
  if (tabBtn) { setTab = tabBtn.dataset.tab; return paintTab(); }
  const el = e.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;
  const d = el.dataset;
  if (act === 'sel') return openSel(el, d.k);

  if (act === 'theme') { themeKey = d.k; applyTheme(themeKey); markTheme(); return; }
  if (act === 'copy-sys') {
    const dump = $('dev-sys');                 // 别叫 el：外层已有一个同名的，遮蔽了读代码的人会看错
    if (dump && navigator.clipboard) navigator.clipboard.writeText(dump.textContent)
      .then(() => setMsg('已复制 system prompt', 'ok'));
    return;
  }
  if (act === 'addmode') { addMode = d.k; return paintTab(); }

  // ---- 模型 ----
  if (act === 'think-on') return post('/api/thinking', { on: !el.classList.contains('on') });
  if (act === 'effort') return post('/api/thinking', { effort: d.k });
  if (act === 'switch') return post('/api/config/switch', { base_url: d.base, model: d.model },
    `已切换到 ${d.model}`);
  if (act === 'del-backend') {
    if (!confirm(`从列表里删掉 ${d.model}？（当前连接不受影响）`)) return;
    return post('/api/config/delete', { base_url: d.base, model: d.model }, '已从列表删除');
  }
  if (act === 'test-saved') return probe(el, { base_url: d.base, model: d.model, api_key: '' });
  if (act === 'test-new') return probe(el, newBackend());
  if (act === 'save-new') {
    const b = newBackend();
    if (!b.api_key) return setMsg('请先填 api_key', 'bad');
    return post('/api/config/save', b, `已保存并切换到 ${b.model}`);
  }
  if (act === 'env-import') {
    addMode = 'manual';
    paintTab().then(() => {                  // 面板是异步渲的，输入框这时才存在
      const v = (setCfg && setCfg.env) || {};
      setVal('nb-base', v.base_url); setVal('nb-model', v.model); setVal('nb-key', v.api_key);
      setMsg('已按 .env 填好，确认无误再点保存', 'ok');
    });
    return;
  }
  if (act === 'save-prefs') {
    const cap = $('pf-cap').value.trim(), th = $('pf-th').value.trim();
    return post('/api/prefs', { context_cap: cap === '' ? 0 : cap, compact_threshold: th === '' ? 0 : th },
      '已保存，即刻生效');
  }

  // ---- 技能 ----
  if (act === 'skill-on') return post('/api/skills/toggle', { name: d.name, enabled: !el.classList.contains('on') });
  if (act === 'skill-run') {
    api('/api/skills/run', { name: d.name }).then(r => {
      if (r.error) return setMsg(r.error, 'bad');
      setMask.remove(); setMask = null;                     // 起了一轮，让位给对话
    });
    return;
  }

  // ---- MCP ----
  if (act === 'mcp-on') return post('/api/mcp/toggle', { name: d.name, enabled: !el.classList.contains('on') });
  if (act === 'mcp-timeout') {
    const list = (setCfg && setCfg.presets) || [15, 30, 60, 120];
    const next = list[(list.indexOf(+d.cur) + 1) % list.length];
    return post('/api/mcp/timeout', { name: d.name, seconds: next });
  }
  if (act === 'mcp-del') {
    if (!confirm(`删掉 MCP server「${d.name}」？`)) return;
    return post('/api/mcp/delete', { name: d.name, scope: d.scope });
  }
  if (act === 'mcp-reload') {
    el.disabled = true; el.textContent = '重连中…';
    api('/api/mcp/reload', {}).then(r => {
      if (r.error) setMsg(r.error, 'bad');
      paintTab();
    });
    return;
  }
  if (act === 'mcp-save') {
    const parts = splitCmd($('mc-cmd').value.trim());
    if (!parts.length) return setMsg('命令不能为空', 'bad');
    return post('/api/mcp/save', {
      name: $('mc-name').value.trim(), command: parts[0], args: parts.slice(1),
      scope: $('mc-scope').value,
    }, '已写入配置——点上面的「重连」才会真的连上');
  }
}

function onSetChange(e) {
  const sel = e.target;
  if (sel.id === 'set-mode') return api('/api/mode', { mode: sel.value });
  if (sel.id === 'nb-prov') { addProv = +sel.value; return paintTab(); }
}

/* ---------- 通用 ---------- */

/** 提交一次改动 → 重画面板 → 再写反馈。
    顺序不能反：重画会把承载提示的那个节点整个换掉，先写就等于没写。 */
const post = (path, body, okMsg) => api(path, body).then(r => {
  if (r.error) return setMsg(r.error, 'bad');
  const deferred = r.deferred;
  const text = r.note || (deferred ? '已保存；本轮结束后自动切换' : (okMsg || '已保存'));
  return paintTab().then(() => setMsg(text, deferred || r.note ? 'warn' : 'ok'));
});

function setMsg(text, kind) {
  const el = setMask && setMask.querySelector('.set-msg');
  if (!el) return;
  el.className = 'set-msg ' + (kind || '');
  el.textContent = text;
}
const setVal = (id, v) => { const el = $(id); if (el) el.value = v || ''; };
const markTheme = () => setMask && setMask.querySelectorAll('[data-act="theme"]')
  .forEach(c => c.classList.toggle('on', c.dataset.k === themeKey));

/** 探活按钮：结果就地显示在按钮旁，不弹窗——弹窗会把刚填的表单盖住。 */
function probe(btn, body) {
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = '测试中…';
  api('/api/config/test', body).then(r => {
    btn.disabled = false; btn.textContent = was;
    setMsg((r.ok ? '✓ ' : '✗ ') + (r.msg || ''), r.ok ? 'ok' : 'bad');
  });
}

const newBackend = () => addMode === 'preset'
  ? {
      base_url: (setCfg.providers[addProv] || {}).base_url || '',
      model: ($('nb-pmodel') || {}).value || '',
      api_key: ($('nb-pkey') || {}).value.trim() || '',
    }
  : {
      base_url: ($('nb-base') || {}).value.trim() || '',
      model: ($('nb-model') || {}).value.trim() || '',
      api_key: ($('nb-key') || {}).value.trim() || '',
    };

/** 把一行命令切成 [command, ...args]，认双引号包住的整段（路径带空格很常见）。 */
function splitCmd(line) {
  const out = [];
  for (const m of String(line).matchAll(/"([^"]*)"|(\S+)/g)) out.push(m[1] !== undefined ? m[1] : m[2]);
  return out;
}

const num = n => Number(n || 0).toLocaleString();

/* ---------- 下拉：按钮 + 浮层，取代原生 select ---------- */

/* id → 选项表。面板是反复重画的，把选项挂在 DOM 上（data-json 之类）每次都要序列化再解析，
   直接留在这张表里更省事——id 在一个面板内唯一，重画时覆盖即可。 */
const SEL = {};

/** 画一个下拉。opts = [{v, t, d?}]。
    真值放在一个隐藏 input 里：这样 $('set-mode').value 照旧能读，
    选完派发 change 事件、原生 select 的老约定（onSetChange）也照旧成立。 */
function selHtml(id, opts, cur, cls) {
  SEL[id] = opts;
  const now = opts.find(o => o.v === cur) || opts[0] || { v: '', t: '' };
  return `<div class="sel${cls ? ' ' + cls : ''}">
    <input type="hidden" id="${esc(id)}" value="${esc(now.v)}">
    <button type="button" class="selbtn" data-act="sel" data-k="${esc(id)}">
      <span class="selv">${esc(now.t)}</span>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"
        stroke-linecap="round" stroke-linejoin="round"><path d="M6 9.5 12 15.5 18 9.5"/></svg>
    </button></div>`;
}

const SEL_CK = `<svg class="ck" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7"/></svg>`;

function openSel(btn, id) {
  const inp = $(id), opts = SEL[id] || [];
  popover(btn, opts.map(o => `
    <div class="pop-opt${o.v === inp.value ? ' on' : ''}" data-v="${esc(o.v)}" tabindex="0">
      <div class="x"><div class="pt">${esc(o.t)}</div>${
        o.d ? `<div class="pd">${esc(o.d)}</div>` : ''}</div>${SEL_CK}</div>`).join(''),
    (e, close) => {
      const opt = e.target.closest('[data-v]');
      if (!opt) return;
      close();
      if (opt.dataset.v === inp.value) return;          // 没换就别惊动 change
      inp.value = opt.dataset.v;
      btn.querySelector('.selv').textContent = (opts.find(o => o.v === inp.value) || {}).t || '';
      inp.dispatchEvent(new Event('change', { bubbles: true }));
    }, btn.offsetWidth);
}

/* ---------- 面板：通用 ---------- */

async function paneGeneral(box, seq = paintSeq) {
  const s = await get('/api/state');
  if (seq !== paintSeq) return;      // 这次重画已经过期，写下去会盖住新页
  box.innerHTML =
    rowHtml('权限模式', '四档差别不小，尤其「完全」——展开看每一档具体放行什么',
      selHtml('set-mode', (s.modes || []).map(m => {
        const spec = MODES.find(x => x.k === m) || {};
        return { v: m, t: `${modeName(m)}（${m}）`, d: spec.d || '' };
      }), s.mode)) +
    rowHtml('外观', '跟随系统时随系统的深浅色切换',
      `<div class="seg">${THEMES.map(t =>
        `<button type="button" class="segb" data-act="theme" data-k="${t.k}">
          ${themeIcon(t.k)}<span>${t.t}</span></button>`).join('')}</div>`) +
    rowHtml('忙时按 Enter', 'agent 还在跑时发的消息会排队，下一轮开头注入（即时转向）',
      '<span class="tag">排队发送</span>') +
    `<div class="set-sec"><h4>快捷键</h4><div class="lst">${SET_KEYS.map(([k, v]) =>
      `<div class="lrow"><div class="lmain"><div class="ld">${esc(v)}</div></div>
       <kbd>${esc(k)}</kbd></div>`).join('')}</div></div>` +
    '<div class="set-msg"></div>';
  markTheme();
}

/* ---------- 面板：模型 ---------- */

async function paneModel(box, seq = paintSeq) {
  const c = await get('/api/config');
  if (seq !== paintSeq) return;
  setCfg = c;
  if (c.error) return box.innerHTML = hintHtml('bad', c.error);
  const th = c.thinking, pf = c.prefs, cur = c.current;
  const compactAtNow = Math.round(pf.context_limit * pf.threshold_now);

  // 当前后端
  let html = `<div class="cur-be">
    <div class="m">${esc(cur.model || '未配置模型')}</div>
    <div class="u">${esc(cur.base_url || '去下面新增一个后端')}</div>
    ${cur.model ? `<div class="k">有效上限 ${num(pf.context_limit)} tok　·　压缩点 ${num(compactAtNow)} tok
      　·　思考 ${th.supports ? (th.on ? '开' : '关') + (th.effort ? ' / ' + esc(th.effort) : '') : '不支持'}</div>` : ''}
    ${c.pending_switch ? '<div class="k warn">已保存新后端，本轮结束后自动切换</div>' : ''}
  </div>`;

  // 思考
  if (th.supports) {
    html += rowHtml('思考链', th.toggleable ? '关掉后不再发 thinking 参数（省 token，但会变笨）'
      : '这个模型强制开启思考，关不掉',
      th.toggleable ? swHtml(th.on, 'think-on') : '<span class="tag">强制开启</span>');
  }
  if (th.efforts.length) {
    html += rowHtml('思考深度', '越深越慢也越贵；档位由模型决定',
      `<div class="chips">${th.efforts.map(x =>
        `<button class="chip${x === th.effort ? ' on' : ''}" data-act="effort" data-k="${esc(x)}">${esc(x)}</button>`
      ).join('')}</div>`);
  }

  // 上限 / 阈值：直接摆成算式，边打字边把触发点算出来。
  // 真实上限还要被模型窗口截一刀（config._context_limit 取 min(窗口, CAP)），
  // 所以把窗口一并带上——不然填 200000 会算出一个永远到不了的触发点。
  // 窗口取服务端给的 prefs.window（= config.effective_window），【不要】从 saved[].window 猜：
  // 那个字段对注册表不认识的本地模型是 0，而服务端那边会走 100K 兜底，两边对不上。
  html += `<div class="set-row"><div class="lab">
      <div class="t">上下文上限与压缩阈值</div>
      <div class="d">自动压缩触发点 = 上下文上限 × 压缩阈值</div></div>
    <div class="set-form inline" data-win="${pf.window || 0}">
      <label class="fld"><span class="cap">上下文上限</span>
        <input id="pf-cap" class="ipt sm" inputmode="numeric" placeholder="128000"
               value="${pf.context_cap || ''}"></label>
      <span class="op">×</span>
      <label class="fld"><span class="cap">压缩阈值</span>
        <input id="pf-th" class="ipt sm" inputmode="decimal" placeholder="0.7"
               value="${pf.compact_threshold || ''}"></label>
      <span class="op" id="pf-trig"></span>
      <button class="btn primary" data-act="save-prefs">保存</button>
    </div></div>`;

  // 已保存
  html += `<div class="set-sec"><h4>已保存的后端 <span class="n">${c.saved.length}</span></h4>`;
  html += c.saved.length ? `<div class="lst">${c.saved.map(e => `
    <div class="lrow${e.current ? ' cur' : ''}">
      <div class="lmain">
        <div class="ln">${esc(e.model)}${e.current ? '<span class="badge2">当前</span>' : ''}</div>
        <div class="ld">${esc(e.base_url)}　·　key ${esc(e.key_hint || '—')}${
          e.window ? '　·　窗口 ' + num(e.window) : ''}${e.context_cap ? '　·　上限 ' + num(e.context_cap) : ''}</div>
      </div>
      <div class="lact">
        <button class="btn xs" data-act="test-saved" data-base="${esc(e.base_url)}" data-model="${esc(e.model)}">测试</button>
        ${e.current ? '' : `<button class="btn xs primary" data-act="switch" data-base="${esc(e.base_url)}" data-model="${esc(e.model)}">切换</button>`}
        <button class="btn xs danger" data-act="del-backend" data-base="${esc(e.base_url)}" data-model="${esc(e.model)}">删除</button>
      </div>
    </div>`).join('')}</div>` : hintHtml('warn', '还没有保存过任何后端。在下面新增一个。');

  // .env 导入：检测到 .env 配了模型、而 config 里没有同一条时才给
  const ev = c.env || {};
  if (ev.model && !c.saved.some(e => e.model === ev.model && e.base_url === ev.base_url)) {
    html += `<div class="set-imp" data-act="env-import">检测到 .env 里配了
      <b>${esc(ev.model)}</b>，点此导入并填进表单 →</div>`;
  }
  html += '</div>';

  // 新增
  html += `<div class="set-sec"><h4>新增后端</h4>
    <div class="subtabs">
      <button class="subtab${addMode === 'preset' ? ' on' : ''}" data-act="addmode" data-k="preset">一键选择</button>
      <button class="subtab${addMode === 'manual' ? ' on' : ''}" data-act="addmode" data-k="manual">手动填写</button>
    </div>`;

  if (addMode === 'preset') {
    const p = c.providers[addProv] || c.providers[0];
    html += `<div class="set-form">
      <label>提供商${selHtml('nb-prov', c.providers.map((x, i) =>
        ({ v: String(i), t: x.name, d: x.base_url })), String(addProv), 'pill')}</label>
      <label>模型${selHtml('nb-pmodel', p.models.map(m =>
        ({ v: m.id, t: m.id, d: `窗口 ${num(m.window)}${
          m.efforts.length ? '　·　思考深度 ' + m.efforts.join(' / ') : ''}` })),
        (p.models[0] || {}).id, 'pill wide')}</label>
      <label>api_key<input class="ipt" id="nb-pkey" type="password" autocomplete="off"
        placeholder="${esc(p.key_help)}"></label>
      <div class="set-note">base_url 自动填 <code>${esc(p.base_url)}</code>　·
        <a href="${esc(p.key_url)}" target="_blank" rel="noopener noreferrer">去开放平台创建 key →</a></div>
      <div class="row"><button class="btn" data-act="test-new">测试连接</button>
        <button class="btn primary" data-act="save-new">保存并切换</button></div>
    </div>`;
  } else {
    html += `<div class="set-form">
      <label>base_url<input class="ipt" id="nb-base" placeholder="https://api.deepseek.com"></label>
      <label>模型名<input class="ipt" id="nb-model" placeholder="deepseek-v4-flash"></label>
      <label>api_key<input class="ipt" id="nb-key" type="password" autocomplete="off" placeholder="sk-…"></label>
      <div class="set-note">自建 / 本地后端填这里。地址填到 <code>/v1</code> 那一层，
        末尾不用带 <code>/chat/completions</code>。</div>
      <div class="row"><button class="btn" data-act="test-new">测试连接</button>
        <button class="btn primary" data-act="save-new">保存并切换</button></div>
    </div>`;
  }
  html += `</div><div class="set-msg"></div>
    <div class="set-note dim">配置文件：<code>${esc(c.config_path)}</code>　·　api_key 只存在本机，页面上只显示掩码</div>`;
  box.innerHTML = html;
  paintTrigger();
}

/** 把"上限 × 阈值 = 触发点"实时算给用户看。
    公式写在说明里不如把结果摆出来——填 200000 却被模型窗口截住这种事，光看说明看不出来。 */
function paintTrigger() {
  const out = setMask && setMask.querySelector('#pf-trig');
  if (!out) return;
  // 空 = 用默认，别的一律照字面解析：'abc' 解析成 NaN 会一路算到"= NaN tok"，
  // 所以下面的范围检查要能挡住 NaN（NaN 参与的比较恒假，写成 >= && <= 正好挡得住）
  const capRaw = $('pf-cap').value.trim(), thRaw = $('pf-th').value.trim();
  const cap = capRaw === '' ? 128000 : parseInt(capRaw, 10);
  const th = thRaw === '' ? 0.7 : parseFloat(thRaw);
  const win = parseInt(out.parentNode.dataset.win, 10) || 0;
  const eff = win ? Math.min(win, cap) : cap;
  if (!(cap >= 1000)) {                             // 和后端 update_prefs 同一条界
    out.className = 'op bad';
    out.textContent = '= 上限要么留空，要么 ≥1000';
    return;
  }
  if (!(th >= 0.3 && th <= 0.95)) {
    out.className = 'op bad';
    out.textContent = '= 阈值要在 0.3 ~ 0.95 之间';
    return;
  }
  out.className = 'op';
  out.innerHTML = `= <b>${num(Math.round(eff * th))}</b> tok 时自动压缩` + (eff < cap
    ? ` <span class="why">（上限被模型窗口 ${num(win)} 截住）</span>` : '');
}

/* ---------- 面板：技能 ---------- */

async function paneSkills(box, seq = paintSeq) {
  const r = await get('/api/skills');
  if (seq !== paintSeq) return;
  if (r.error) return box.innerHTML = hintHtml('bad', r.error);
  const list = r.skills || [];
  box.innerHTML = `<div class="set-sec"><h4>技能 <span class="n">${list.length}</span></h4>
    ${hintHtml('warn', '启停改的是禁用名单；技能索引在建会话时拼进 system prompt，故【新会话生效】。「使用」是立刻把该技能全文注入本轮，不受此限。')}
    ${list.length ? `<div class="lst">${list.map(s => `
      <div class="lrow${s.enabled ? '' : ' off'}">
        <div class="lmain">
          <div class="ln">${esc(s.name)}<span class="badge2">${esc(s.source)}</span></div>
          <div class="ld">${esc(s.description)}</div>
          <div class="lp">${esc(s.path)}</div>
        </div>
        <div class="lact">
          ${s.enabled ? `<button class="btn xs" data-act="skill-run" data-name="${esc(s.name)}">使用</button>` : ''}
          ${swHtml(s.enabled, 'skill-on', `data-name="${esc(s.name)}"`)}
        </div>
      </div>`).join('')}</div>` : hintHtml('warn', '没有发现任何技能。')}
    </div>
    <div class="set-note dim">技能目录：项目 <code>&lt;工作区&gt;/.mecode/skills/</code>　·
      用户 <code>~/.mecode/skills/</code>　·　内置随 mecode 一起装。
      每个技能是一个文件夹，里面必须有 <code>SKILL.md</code>。</div>
    <div class="set-msg"></div>`;
}

/* ---------- 面板：MCP ---------- */

async function paneMcp(box, seq = paintSeq) {
  const r = await get('/api/mcp');
  if (seq !== paintSeq) return;
  if (r.error) return box.innerHTML = hintHtml('bad', r.error);
  setCfg = Object.assign(setCfg || {}, { presets: r.presets });
  const list = r.servers || [];
  const paths = r.paths || [];
  box.innerHTML = `<div class="set-sec">
    <h4>MCP Server <span class="n">${list.length}</span>
      <button class="btn xs" data-act="mcp-reload" style="margin-left:auto">重连</button></h4>
    ${(r.errors || []).length ? hintHtml('bad', '未连上：' + r.errors.join('；')) : ''}
    ${list.length ? `<div class="lst">${list.map(sv => `
      <div class="lrow${sv.enabled ? '' : ' off'}">
        <div class="lmain">
          <div class="ln">${esc(sv.name)}${sv.connected
            ? `<span class="badge2 ok">已连 · ${sv.tools} 个工具</span>`
            : sv.enabled ? '<span class="badge2">未连</span>' : ''}</div>
          <div class="ld mono">${esc(sv.command)}</div>
          <div class="lp">来自${sv.scope === 'project' ? '项目' : '全局'}配置</div>
        </div>
        <div class="lact">
          <button class="chip xs" data-act="mcp-timeout" data-name="${esc(sv.name)}" data-cur="${sv.timeout}"
                  title="握手超时，点击切换档位">${sv.timeout}s</button>
          <button class="btn xs danger" data-act="mcp-del" data-name="${esc(sv.name)}"
                  data-scope="${esc(sv.scope || 'global')}">删除</button>
          ${swHtml(sv.enabled, 'mcp-on', `data-name="${esc(sv.name)}"`)}
        </div>
      </div>`).join('')}</div>` : hintHtml('warn', '还没有配置任何 MCP server。')}
    </div>

    <div class="set-sec"><h4>新增 Server</h4>
      <div class="set-form">
        <label>名称<input class="ipt" id="mc-name" placeholder="filesystem"></label>
        <label>启动命令<input class="ipt" id="mc-cmd"
          placeholder='npx -y @modelcontextprotocol/server-filesystem "C:/我的项目"'></label>
        <label>写到哪${selHtml('mc-scope', [
          { v: 'global', t: '全局', d: '~/.mecode/mcp.json　·　所有项目都能用' },
          { v: 'project', t: '项目', d: '<工作区>/.mcp.json　·　可提交进仓库，队友一起用' },
        ], 'global', 'pill')}</label>
        <div class="set-note">命令和参数写成一行，带空格的路径用双引号包起来。
          项目级配置里可以用占位符 <code>${'$'}{cwd}</code>（项目根）和
          <code>${'$'}{env:NAME}</code>（环境变量）——key 这类机密要走后者，别明文写进要提交的文件。</div>
        <div class="row"><button class="btn primary" data-act="mcp-save">保存</button></div>
      </div>
    </div>
    <div class="set-note dim">配置文件：${paths.map(p =>
      `<code>${esc(p.path)}</code>${p.exists ? '' : '（不存在）'}`).join('　·　')}
      <br>改动要点【重连】才生效——工具表是发请求那一刻交给模型的，中途换会让它调到不存在的名字。</div>
    <div class="set-msg"></div>`;
}

/* ---------- 面板：开发者 ---------- */

/** 对应 TUI 的 /system。模型被训练成不吐自己的 system prompt，问它拿到的是编的，
    要看真的只能从服务端读 messages[0]。 */
async function paneDev(box, seq = paintSeq) {
  const r = await get('/api/system');
  if (seq !== paintSeq) return;
  if (r.error) return box.innerHTML = hintHtml('bad', r.error);
  box.innerHTML = `
    <div class="set-sec"><h4>System Prompt <span class="n">${num(r.chars)} 字</span>
      <button class="btn xs" data-act="copy-sys" style="margin-left:auto">复制</button></h4>
      ${hintHtml('warn', '这是此刻真的发给模型的第一条消息。问模型它自己的提示词拿不到真话——它被训练成不吐。')}
      <pre class="dump" id="dev-sys">${esc(r.system || '（当前没有 system 消息）')}</pre>
    </div>
    <div class="set-sec"><h4>模式提示（${esc(r.mode)}）</h4>
      ${hintHtml('warn', '模式提示【不进 system prompt】，而是每轮注入一条 <system-reminder>——前缀不变才不会击穿服务端的提示缓存。normal / auto 没有这一段。')}
      ${r.reminder ? `<pre class="dump">${esc(r.reminder)}</pre>`
        : '<div class="set-note">当前模式没有额外注入。</div>'}
    </div>
    <div class="set-msg"></div>`;
}

/* ---------- 面板：关于 ---------- */

async function paneAbout(box, seq = paintSeq) {
  const s = await get('/api/state');
  if (seq !== paintSeq) return;
  box.innerHTML =
    rowHtml('工作区', esc(s.cwd || '—'), '') +
    rowHtml('会话', esc(s.session || '—'), '') +
    rowHtml('模型', esc(s.model || '未配置') + (s.thinking_on ? '　·　思考 ' + esc(s.effort || '开') : ''), '') +
    rowHtml('上下文', `${num(s.context_tokens)} / ${num(s.compact_at)} tok（上限 ${num(s.context_limit)}）`, '') +
    rowHtml('工具', `${(s.tools || []).length} 个${s.mcp ? '，其中 MCP server ' + s.mcp + ' 个' : ''}`, '') +
    rowHtml('技能', `${(s.skills || []).length} 个已启用`, '') +
    `<div class="set-sec"><h4>已注册的工具</h4>
      <div class="toolgrid">${(s.tools || []).map(t => `<code>${esc(t)}</code>`).join('')}</div></div>`;
}
