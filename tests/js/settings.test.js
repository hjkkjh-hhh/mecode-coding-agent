/* settings.js 的无浏览器自检。

   为什么值得写：面板全是模板字符串拼出来的，写错一个变量名不会报错，
   只会在真机上渲成一片空白或 "undefined"。这里用最小 DOM 桩把五个面板各渲一遍，
   拿真实接口形状的假数据喂进去，检查关键内容有没有出现、有没有 undefined 漏出来。
*/
const fs = require('fs');
const vm = require('vm');

const path = require('path');
const DESK = path.join(__dirname, '..', '..', 'desktop') + path.sep;

let pass = 0, fail = 0;
const t = (name, cond, detail) => {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (detail ? '  ' + detail : '')); }
};

/* ---------- 最小 DOM 桩 ---------- */
function mkEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(), _html: '', children: [], dataset: {}, classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    style: {}, value: '',
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    appendChild(c) { this.children.push(c); return c; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    remove() {},
    closest() { return null; },
  };
  return el;
}

const registry = {};
const sandbox = {
  console, setTimeout, clearTimeout, Math, JSON, Object, Array, String, Number, Boolean,
  Promise, RegExp, Date: { now: () => 0 },
  document: { createElement: mkEl, body: { appendChild() {} } },
  window: {},
  // ---- index.html 提供的那些（settings.js 依赖它们）----
  $: id => registry[id] || (registry[id] = mkEl('div')),
  esc: s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  THEMES: [{ k: 'light', t: '浅色' }, { k: 'dark', t: '深色' },
           { k: 'system', t: '跟随系统' }],
  themeIcon: () => '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/></svg>',
  themeKey: 'system',
  modeName: k => ({normal:'标准', auto:'自动', plan:'计划', yolo:'完全'}[k] || k),
  MODES: [{ k: 'normal', t: '标准', d: '需要审核的改动和命令，每次都问你' },
          { k: 'auto', t: '自动', d: '改动项目内的文件自动放行；其他工具仍会问' },
          { k: 'plan', t: '计划', d: '只读探索、先给出完整计划，你批准后才动手' },
          { k: 'yolo', t: '完全', d: '所有工具自动放行、不再询问（慎用）' }],
  popover(anchor, html) { const p = mkEl('div'); p._html = html; return p; },

  applyTheme() {},
  notice() {},
  modal(html) { const m = mkEl('div'); m._html = html; return m; },
  confirm: () => true,
  api: () => Promise.resolve({ ok: true }),
  get: p => Promise.resolve(FAKE[p] || {}),
};
sandbox.globalThis = sandbox;

/* ---------- 假数据：字段照着真实接口的形状写 ---------- */
const FAKE = {
  '/api/state': {
    busy: false, mode: 'normal', modes: ['normal', 'auto', 'plan', 'yolo'],
    model: 'deepseek-v4-flash', thinking_on: true, effort: 'max',
    context_tokens: 27140, context_limit: 128000, compact_at: 89600,
    cwd: 'C:/proj', session: 'abc-123', tools: ['read_file', 'bash', 'edit_file'],
    skills: [{ name: 'skill-install', description: 'x' }], mcp: 1, mcp_errors: 0,
  },
  '/api/config': {
    providers: [
      { name: 'deepseek', base_url: 'https://api.deepseek.com', key_help: '去控制台建 key',
        key_url: 'https://platform.deepseek.com/api_keys',
        models: [{ id: 'deepseek-v4-flash', window: 1000000, thinks: true, toggleable: true,
                   efforts: ['high', 'max'] }] },
      { name: 'glm', base_url: 'https://open.bigmodel.cn/api/paas/v4', key_help: 'h', key_url: 'u',
        models: [{ id: 'glm-5.2', window: 1000000, thinks: true, toggleable: true, efforts: [] }] },
    ],
    saved: [{ base_url: 'https://api.deepseek.com', model: 'deepseek-v4-flash',
              key_hint: 'sk-abc…9f21', context_cap: 0, window: 1000000, current: true },
            { base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-5.2',
              key_hint: 'xyz…77', context_cap: 64000, window: 1000000, current: false }],
    current: { base_url: 'https://api.deepseek.com', model: 'deepseek-v4-flash' },
    prefs: { context_cap: 0, compact_threshold: 0, context_limit: 128000, threshold_now: 0.7,
             window: 1000000 },
    thinking: { on: true, effort: 'max', supports: true, toggleable: true, efforts: ['high', 'max'] },
    env: { base_url: 'http://localhost:8000/v1', model: 'qwen-local', api_key: 'k' },
    pending_switch: false, config_path: 'C:/Users/x/.mecode/config.json',
  },
  '/api/skills': {
    skills: [
      { name: 'mcp-install', description: '装 MCP server', source: '内置',
        path: 'C:/m/skills/mcp-install/SKILL.md', enabled: true },
      { name: 'my-skill', description: '自定义', source: '用户',
        path: 'C:/u/.mecode/skills/my-skill/SKILL.md', enabled: false },
    ],
  },
  '/api/mcp': {
    servers: [
      { name: 'filesystem', command: 'npx -y @mcp/fs C:/proj', enabled: true, timeout: 15,
        connected: true, tools: 7, scope: 'global' },
      { name: 'broken', command: 'nope', enabled: false, timeout: 30, connected: false, tools: 0,
        scope: 'project' },
    ],
    errors: ['broken: FileNotFoundError'], presets: [15, 30, 60, 120],
    paths: [{ scope: 'global', path: 'C:/Users/x/.mecode/mcp.json', exists: true },
            { scope: 'project', path: 'C:/proj/.mcp.json', exists: false }],
  },
  '/api/system': {
    system: '你是 mecode……\n<tool>read_file</tool>', reminder: '【计划模式】只读，先写计划文件。',
    mode: 'plan', chars: 4210,
  },
};

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(DESK + 'settings.js', 'utf-8'), sandbox, { filename: 'settings.js' });
// settings.js 顶层的 let（setMask/addMode/...）是词法绑定，不是 sandbox 的属性，只能求值拿
const ev = expr => vm.runInContext(expr, sandbox);

/* ---------- 逐个面板渲染 ---------- */
async function run() {
  console.log('— 打开设置窗 —');
  sandbox.openSettings('general');
  t('窗建出来了', !!ev('setMask'));
  t('导航含六个页签', ['general', 'model', 'skills', 'mcp', 'dev', 'about']
    .every(k => ev('setMask').innerHTML.includes(`data-tab="${k}"`)));

  const panes = { general: 'paneGeneral', model: 'paneModel', skills: 'paneSkills',
                  mcp: 'paneMcp', dev: 'paneDev', about: 'paneAbout' };
  const out = {};
  for (const [k, fn] of Object.entries(panes)) {
    const box = mkEl('div');
    await sandbox[fn](box);
    out[k] = box.innerHTML;
    console.log(`— 面板 ${k}（${out[k].length} 字符）—`);
    t('渲出了内容', out[k].length > 200, out[k].slice(0, 120));
    t('没有 undefined 漏进页面', !out[k].includes('undefined'),
      (out[k].match(/.{0,40}undefined.{0,40}/) || [''])[0]);
    t('没有 [object Object]', !out[k].includes('[object Object]'));
    t('没有 NaN', !out[k].includes('NaN'));
    t('标签开合平衡', balanced(out[k]), imbalance(out[k]));
  }

  console.log('— 通用页内容 —');
  // 下拉换成了自定义控件：选项不在 HTML 里（那是原生 select 的做法），而在 SEL 表里，
  // 点开时才渲进浮层。所以这里两头都查：控件本体在页面上，选项表内容对得上。
  t('模式用的是自定义下拉、不是原生 select',
    out.general.includes('data-act="sel" data-k="set-mode"') && !out.general.includes('<select'));
  t('当前模式回填进隐藏 input', out.general.includes('id="set-mode" value="normal"'));
  t('按钮上写的是中文名', out.general.includes('标准（normal）'), out.general.slice(0, 200));
  const modeOpts = ev('SEL["set-mode"]');
  t('四种模式都在选项表', ['normal', 'auto', 'plan', 'yolo']
    .every(m => modeOpts.some(o => o.v === m)));
  t('每一档都带说明', modeOpts.every(o => o.d && o.d.length > 5),
    JSON.stringify(modeOpts.map(o => o.d)));
  t('三个主题都在', sandbox.THEMES.every(x => out.general.includes(`data-k="${x.k}"`)));
  t('外观是一行分段控件、不是三张大卡片',
    out.general.includes('class="seg"') && !out.general.includes('class="cards"'));

  console.log('— 模型页内容 —');
  t('显示当前模型', out.model.includes('deepseek-v4-flash'));
  t('显示压缩点 89,600', out.model.includes('89,600'), '(128000×0.7)');
  t('显示有效上限 128,000', out.model.includes('128,000'));
  t('思考开关在', out.model.includes('data-act="think-on"'));
  t('深度档 high/max 都在', out.model.includes('data-k="high"') && out.model.includes('data-k="max"'));
  t('max 是选中态', /class="chip on" data-act="effort" data-k="max"/.test(out.model));
  t('已保存两条', (out.model.match(/data-act="del-backend"/g) || []).length === 2);
  t('当前那条不给切换按钮', (out.model.match(/data-act="switch"/g) || []).length === 1);
  t('key 只显示掩码', out.model.includes('sk-abc…9f21'));
  t('.env 导入行出现', out.model.includes('data-act="env-import"') && out.model.includes('qwen-local'));
  t('一键页有提供商下拉', out.model.includes('id="nb-prov"'));
  t('模型下拉默认选中第一个', out.model.includes('id="nb-pmodel" value="deepseek-v4-flash"'),
    '不回填的话点保存会带一个空模型名过去');
  t('模型选项标了窗口', ev('SEL["nb-pmodel"]')[0].d.includes('1,000,000'));
  t('一键页有 key 输入', out.model.includes('id="nb-pkey"'));
  t('key 输入是 password', /id="nb-pkey" type="password"/.test(out.model));
  t('有测试连接按钮', out.model.includes('data-act="test-new"'));
  t('上限/阈值输入框在', out.model.includes('id="pf-cap"') && out.model.includes('id="pf-th"'));
  t('两个框各自标了名', out.model.includes('>上下文上限<') && out.model.includes('>压缩阈值<'),
    '光两个框谁也认不出哪个是哪个');
  t('摆成算式', out.model.includes('×') && out.model.includes('id="pf-trig"'));
  t('算式带上了模型窗口', out.model.includes('data-win="1000000"'),
    '真实上限是 min(窗口, CAP)；窗口必须用服务端给的 prefs.window，不能从 saved[] 猜');

  console.log('— 模型页：切到手动子页 —');
  ev("addMode = 'manual'");
  const b2 = mkEl('div'); await sandbox.paneModel(b2);
  t('手动页三个输入框', ['nb-base', 'nb-model', 'nb-key'].every(i => b2.innerHTML.includes(`id="${i}"`)));
  t('手动页没有提供商下拉', !b2.innerHTML.includes('id="nb-prov"'));
  ev("addMode = 'preset'");

  console.log('— 模型页：切提供商 —');
  ev('addProv = 1');
  const b3 = mkEl('div'); await sandbox.paneModel(b3);
  t('模型列表跟着换成 glm', b3.innerHTML.includes('glm-5.2'));
  t('base_url 提示也跟着换', b3.innerHTML.includes('open.bigmodel.cn'));
  ev('addProv = 0');

  console.log('— 技能页内容 —');
  t('两个技能都在', out.skills.includes('mcp-install') && out.skills.includes('my-skill'));
  t('来源徽标', out.skills.includes('内置') && out.skills.includes('用户'));
  t('启用的给"使用"按钮', (out.skills.match(/data-act="skill-run"/g) || []).length === 1);
  t('停用的行有 off 类', out.skills.includes('lrow off'));
  t('提示了新会话生效', out.skills.includes('新会话生效'));
  t('开关带 name', out.skills.includes('data-act="skill-on"') && out.skills.includes('data-name="my-skill"'));

  console.log('— MCP 页内容 —');
  t('两个 server 都在', out.mcp.includes('filesystem') && out.mcp.includes('broken'));
  t('已连的显示工具数', out.mcp.includes('已连 · 7 个工具'));
  t('连接错误显示出来', out.mcp.includes('FileNotFoundError'));
  t('超时 chip 可点', out.mcp.includes('data-act="mcp-timeout"'));
  t('有重连按钮', out.mcp.includes('data-act="mcp-reload"'));
  t('新增表单齐全', ['mc-name', 'mc-cmd', 'mc-scope'].every(i => out.mcp.includes(`id="${i}"`)));
  t('落点下拉默认全局', out.mcp.includes('id="mc-scope" value="global"'));
  t('占位符说明没被模板吃掉', out.mcp.includes('${cwd}') && out.mcp.includes('${env:NAME}'),
    '模板字符串里的 ${} 要转义');
  t('两个配置路径都列出', out.mcp.includes('.mecode/mcp.json') && out.mcp.includes('.mcp.json'));
  t('删除按钮带上它自己的作用域', out.mcp.includes('data-scope="project"'),
    '写死 global 的话项目级那条永远删不掉');
  t('标出来自哪个配置', out.mcp.includes('来自项目配置') && out.mcp.includes('来自全局配置'));

  console.log('— 开发者页内容 —');
  t('渲出 system prompt', out.dev.includes('read_file'));
  t('尖括号被转义（不当成真标签）', out.dev.includes('&lt;tool&gt;'), '注入防线');
  t('显示字数', out.dev.includes('4,210'));
  t('模式提示单列出来', out.dev.includes('计划模式') && out.dev.includes('plan'));
  t('有复制按钮', out.dev.includes('data-act="copy-sys"'));

  console.log('— 通用页快捷键 —');
  t('列出快捷键', out.general.includes('<kbd>Enter</kbd>')
    && out.general.includes('Shift + Enter'));

  console.log('— 关于页内容 —');
  t('工作区', out.about.includes('C:/proj'));
  t('会话 id', out.about.includes('abc-123'));
  t('上下文用量', out.about.includes('27,140') && out.about.includes('89,600'));
  t('工具清单', ['read_file', 'bash', 'edit_file'].every(x => out.about.includes(x)));

  console.log('— splitCmd（命令行切分）—');
  const sc = sandbox.splitCmd;
  t('普通切分', JSON.stringify(sc('npx -y pkg')) === '["npx","-y","pkg"]', JSON.stringify(sc('npx -y pkg')));
  t('引号保住带空格的路径',
    JSON.stringify(sc('npx -y fs "C:/我的 项目"')) === '["npx","-y","fs","C:/我的 项目"]',
    JSON.stringify(sc('npx -y fs "C:/我的 项目"')));
  t('空串给空数组', sc('').length === 0);

  console.log('— 空数据不炸 —');
  FAKE['/api/config'] = { providers: [{ name: 'x', base_url: '', key_help: '', key_url: '', models: [] }],
    saved: [], current: { base_url: '', model: '' },
    prefs: { context_cap: 0, compact_threshold: 0, context_limit: 0, threshold_now: 0 },
    thinking: { on: false, effort: '', supports: false, toggleable: false, efforts: [] },
    env: {}, pending_switch: false, config_path: '' };
  FAKE['/api/skills'] = { skills: [] };
  FAKE['/api/mcp'] = { servers: [], errors: [], presets: [], paths: [] };
  FAKE['/api/system'] = { system: '', reminder: '', mode: 'normal', chars: 0 };
  for (const [k, fn] of Object.entries(panes)) {
    const box = mkEl('div');
    try { await sandbox[fn](box); t(`${k} 空数据不抛`, true); }
    catch (e) { t(`${k} 空数据不抛`, false, e.message); }
    t(`${k} 空数据无 undefined`, !box.innerHTML.includes('undefined'),
      (box.innerHTML.match(/.{0,40}undefined.{0,40}/) || [''])[0]);
  }
  const bd = mkEl('div'); await sandbox.paneDev(bd);
  t('没有 system 消息时给话术', bd.innerHTML.includes('当前没有 system 消息'));
  t('没有模式提示时也说清楚', bd.innerHTML.includes('没有额外注入'));

  const b4 = mkEl('div'); await sandbox.paneModel(b4);
  t('未配置时给引导话术', b4.innerHTML.includes('未配置模型'));
  t('没有已存后端时给提示', b4.innerHTML.includes('还没有保存过'));

  console.log('— 接口报错时 —');
  FAKE['/api/skills'] = { error: '读不到技能目录' };
  const b5 = mkEl('div'); await sandbox.paneSkills(b5);
  t('错误被显示出来', b5.innerHTML.includes('读不到技能目录'), b5.innerHTML.slice(0, 120));

  console.log(`\n${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
}

/* 简易标签平衡检查：只看成对标签，忽略自闭合与 void 元素 */
const VOID = new Set(['input', 'br', 'hr', 'img', 'meta', 'link']);
function tally(html) {
  const n = {};
  // 连属性一起吃掉才认得出自闭合（<path … />）。属性值里可能有 > ，所以引号内单独放行。
  for (const m of html.matchAll(/<(\/?)([a-z0-9]+)((?:"[^"]*"|'[^']*'|[^>"'])*)>/gi)) {
    const tag = m[2].toLowerCase();
    if (VOID.has(tag) || m[3].trimEnd().endsWith('/')) continue;
    n[tag] = (n[tag] || 0) + (m[1] ? -1 : 1);
  }
  return n;
}
const balanced = html => Object.values(tally(html)).every(v => v === 0);
const imbalance = html => JSON.stringify(
  Object.fromEntries(Object.entries(tally(html)).filter(([, v]) => v !== 0)));

run();
