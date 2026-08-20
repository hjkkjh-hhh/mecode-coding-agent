/* 工具卡片摘要的行为测试。
   这几个函数写在 index.html 的内联 script 里，没法 require，只能按标记把那段抠出来跑。
   抠的区间是 `const TI = {` 到 `function toolNode(` —— 改动那一段时注意别把边界挪没了。

   摘要是 [主体, 限定语] 两段：
     主体   = 作用在【谁】身上（文件名/命令/模式），长度不定，由 CSS 按真实宽度省略
     限定语 = 【怎么】作用（行范围/字数/后台），短且定长，贴右显示、永不省略
   合成一串的话，省略号切掉的往往正是后半截那个定长信息——而它恰恰是放得下的。 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'desktop', 'index.html'), 'utf-8');
const start = html.indexOf('const TI = {');
const end = html.indexOf('function toolNode(name, args)');
if (start < 0 || end < 0) { console.log('X 抠不到那段代码，边界标记被改动了'); process.exit(1); }
const sandbox = { console, JSON, Object, Array, String, Number,
  esc: s => String(s == null ? '' : s) };
vm.createContext(sandbox);
vm.runInContext(html.slice(start, end), sandbox);
const ev = e => vm.runInContext(e, sandbox);

let pass = 0, fail = 0;
const t = (name, got, want) => {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) { pass++; return; }
  fail++; console.log(`X ${name}\n  实得 ${g}\n  期望 ${w}`);
};
const brief = (n, o) =>
  ev(`toolBrief(${JSON.stringify(n)}, toolArgs(${JSON.stringify(JSON.stringify(o))}))`);

// ---- 主体 / 限定语 的分工 ----
t('read_file 行范围归到右边（不跟文件名抢省略号）',
  brief('read_file', {path:'scripts/tui.py', offset:0, limit:900}),
  ['scripts/tui.py', '第 1 行起，共 900 行']);
t('read_file 没给范围就没有右段', brief('read_file', {path:'a.py'}), ['a.py', '']);
t('read_file 只给 offset', brief('read_file', {path:'a.py', offset:99}), ['a.py', '第 100 行起']);
t('write_file 字数在右', brief('write_file', {path:'a.md', content:'12345'}), ['a.md', '写入 5 字']);
t('edit_file 替换全部在右', brief('edit_file', {path:'a.py', replace_all:true}), ['a.py', '替换全部']);
t('edit_file 普通替换没有右段', brief('edit_file', {path:'a.py'}), ['a.py', '']);
t('bash 后台标在右', brief('bash', {command:'ping x', background:true}), ['ping x', '后台']);
t('bash 前台没有右段', brief('bash', {command:'ls'}), ['ls', '']);
t('subagent 后台标在右', brief('subagent', {description:'数 py 文件', background:true}),
  ['数 py 文件', '后台']);
t('wait_bgtask 秒数在右', brief('wait_bgtask', {id:3, seconds:10}), ['#3', '等 10 秒']);
t('task_update 目标状态在右', brief('task_update', {task_id:'t1', status:'done'}),
  ['t1', '→ done']);
t('run_workflow 阶段数在右',
  brief('run_workflow', {stages:[{description:'审查 README'},{id:'b'}]}),
  ['审查 README → b', '2 个阶段']);
t('ask_user 问题数在右', brief('ask_user', {questions:[{question:'甲'},{question:'乙'}]}),
  ['甲 / 乙', '2 个问题']);

// ---- 主体本身 ----
t('grep 模式和路径都在主体', brief('grep', {pattern:'def foo', path:'src'}),
  ['def foo\u3000在 src', '']);
t('glob 同理', brief('glob', {pattern:'**/*.py', path:'C:/Users/31784/Desktop/mecode/src'}),
  ['**/*.py\u3000在 C:/Users/31784/Desktop/mecode/src', '']);
t('exit_plan', brief('exit_plan', {}), ['提交计划待批准', '']);
t('未知工具回落到首个参数', brief('mcp__thing', {url:'http://a.com'}), ['url=http://a.com', '']);
t('空参数不炸', brief('mcp__thing', {}), ['', '']);

// ---- 截断只是护栏，不是排版宽度 ----
t('正常长度的路径完整保留',
  brief('glob', {pattern:'**/*.py', path:'C:/Users/31784/Desktop/mecode/src/mecode'})[0],
  '**/*.py\u3000在 C:/Users/31784/Desktop/mecode/src/mecode');
t('离谱长度才截断', brief('bash', {command:'x'.repeat(500)})[0].length, 321);
t('换行被压平', brief('bash', {command:'a\nb\nc'})[0], 'a b c');

// ---- 参数与图标 ----
t('完整参数是缩进过的', ev(`toolFullArgs('{"a":1,"b":2}')`), '{\n  "a": 1,\n  "b": 2\n}');
t('坏 JSON 原样给出', ev(`toolFullArgs('{坏的')`), '{坏的');
t('图标是 svg', ev(`toolIcon('read_file')`).includes('<svg'), true);
t('未知工具也有图标', ev(`toolIcon('mcp__x')`).includes('<svg'), true);
t('写类工具配色', ev(`toolKindClass('write_file')`), 'k-write');
t('读类无配色', ev(`toolKindClass('read_file')`), '');

console.log(`\n${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
