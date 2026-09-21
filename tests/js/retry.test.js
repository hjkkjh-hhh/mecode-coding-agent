/* 执行真实页面的流式/重试处理函数：跨通知清理失败思考，不删除已完成回合。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'desktop', 'index.html'), 'utf8');
const nodes = [];
function node() {
  const parts = {};
  return {
    textContent: '', removed: false, children: [],
    appendChild(child) { this.children.push(child); },
    remove() { this.removed = true; },
    querySelector(selector) { return parts[selector] ||= node(); },
  };
}
const context = vm.createContext({
  document: {createElement: node},
  msg: () => { const n = node(); nodes.push(n); return n; },
  atBottom: () => false, scroll: () => {}, paintMd: () => {}, copyBtn: () => node(),
  toolNode: () => node(), renderStat: () => {},
});
const run = s => vm.runInContext(s, context);
run('let live = null, liveKind = null, pendingReasoning = [], toolCalls = 0; const toolBox = new Map();');
const block = (start, end) => {
  const a = html.indexOf(start), b = html.indexOf(end, a);
  assert.ok(a >= 0 && b > a);
  return html.slice(a, b);
};
run(block('function stream(kind, text)', '/* 工具图标'));
run(block('function notice(text, cls)', '// ---- 弹窗'));
run(block('function toolStart(m)', 'function fillResult'));

run("stream('reasoning', 'completed thought'); stream('text', 'completed answer');");
const completed = nodes.slice();
run("stream('reasoning', 'failed part 1');");
const failed1 = nodes.at(-1);
run("notice('background task finished'); stream('reasoning', 'failed part 2');");
const failed2 = nodes.at(-1);
run("retrying({text: 'retry 1/3'});");
assert.ok(failed1.removed && failed2.removed);
assert.ok(completed.every(n => !n.removed));
assert.equal(run('pendingReasoning.length'), 0);
run("stream('reasoning', 'new thought');");
assert.equal(run('live.textContent'), 'new thought');
const toolThought = nodes.at(-1);
run("toolStart({name:'noop', args:{}, id:'c1'}); retrying({text:'next request timed out'});");
assert.equal(toolThought.removed, false, '上一次已完成工具回合的思考必须保留');
assert.equal(run('toolBox.size'), 1);
console.log('重试显示边界通过');
