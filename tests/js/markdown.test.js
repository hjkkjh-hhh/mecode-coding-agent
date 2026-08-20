/* desktop/markdown.js 的行为测试。
   直接 `node tests/js/markdown.test.js` 跑，或跟着 pytest 一起跑
   （见 tests/test_desktop_js.py）。

   重点在【注入】那一组：这里渲染的是模型生成的内容，等同不可信输入。 */
const path = require('path');
const { renderMarkdown, mdInline, looksLikeMarkdown } =
  require(path.join(__dirname, '..', '..', 'desktop', 'markdown.js'));

let pass = 0, fail = 0;
function t(name, got, want) {
  const ok = typeof want === 'function' ? want(got) : got === want;
  if (ok) { pass++; return; }
  fail++;
  console.log('✗ ' + name + '\n  实得: ' + JSON.stringify(got) + '\n  期望: ' + JSON.stringify(want));
}
const has = sub => s => s.includes(sub);
const hasnt = sub => s => !s.includes(sub);

// ---- 注入 ----
t('尖括号被转义', renderMarkdown('<img src=x onerror=alert(1)>'), has('&lt;img'));
t('script 标签不成真标签', renderMarkdown('<script>alert(1)</script>'), hasnt('<script>'));
t('javascript: 链接被拦', renderMarkdown('[点我](javascript:alert(1))'), hasnt('href="javascript'));
t('javascript: 原样留文字', renderMarkdown('[点我](javascript:alert(1))'), has('[点我]'));
t('data:text/html 被拦', renderMarkdown('[x](data:text/html,<script>1</script>)'), hasnt('href="data:'));
t('http 链接放行', renderMarkdown('[x](https://a.com)'), has('href="https://a.com"'));
t('链接带 noopener', renderMarkdown('[x](https://a.com)'), has('rel="noopener noreferrer"'));
t('相对路径放行', renderMarkdown('[x](./a.md)'), has('href="./a.md"'));
t('图片不发请求', renderMarkdown('![alt](https://a.com/a.png)'), hasnt('<img'));
t('图片渲成链接', renderMarkdown('![alt](https://a.com/a.png)'), has('md-img'));
t('引号在属性里被转义', renderMarkdown('[x](https://a.com/")'), hasnt('href="https://a.com/""'));

// ---- 行内 ----
t('粗体', mdInline('a **b** c'), 'a <strong>b</strong> c');
t('斜体', mdInline('a *b* c'), 'a <em>b</em> c');
t('粗斜体', mdInline('***b***'), '<strong><em>b</em></strong>');
t('删除线', mdInline('~~b~~'), '<del>b</del>');
t('行内代码', mdInline('用 `a*b*c` 算'), '用 <code>a*b*c</code> 算');
t('代码内的星号不成斜体', mdInline('`*x*`'), '<code>*x*</code>');
t('代码内的尖括号被转义', mdInline('`<b>`'), '<code>&lt;b&gt;</code>');
t('snake_case 不成斜体', mdInline('foo_bar_baz'), 'foo_bar_baz');
t('乘法星号不误伤', mdInline('2 * 3 * 4'), '2 * 3 * 4');
t('裸 URL 成链', mdInline('见 https://a.com 谢谢'), has('<a href="https://a.com"'));
t('句末裸 URL 不吞句号', mdInline('见 https://a.com。'), has('>https://a.com</a>'));
t('占位符字符被剥掉', renderMarkdown('a' + String.fromCharCode(0) + '0' + String.fromCharCode(0) + 'b'),
  s => !s.includes(String.fromCharCode(0)));

// ---- 块级 ----
t('标题', renderMarkdown('## 标题'), '<h2>标题</h2>');
t('六级标题', renderMarkdown('###### x'), '<h6>x</h6>');
t('七个井号不是标题', renderMarkdown('####### x'), has('<p>'));
t('分隔线', renderMarkdown('---'), '<hr>');
t('段落内单换行成 br', renderMarkdown('a\nb'), '<p>a<br>b</p>');
t('空行分段', renderMarkdown('a\n\nb'), '<p>a</p><p>b</p>');
t('引用', renderMarkdown('> 引文'), '<blockquote><p>引文</p></blockquote>');

// ---- 代码块 ----
t('围栏代码块', renderMarkdown('```py\nx=1\n```'), has('<code class="lang-py">x=1</code>'));
t('代码块内容不被解析', renderMarkdown('```\n# 不是标题\n```'), has('# 不是标题'));
t('代码块内容不成标题', renderMarkdown('```\n# 不是标题\n```'), hasnt('<h1>'));
t('未闭合围栏吃到结尾', renderMarkdown('```\nx=1'), has('<code>x=1</code>'));
t('代码块带复制按钮', renderMarkdown('```\nx\n```'), has('md-cp'));
t('波浪围栏', renderMarkdown('~~~\nx\n~~~'), has('<code>x</code>'));

// ---- 列表 ----
t('无序列表', renderMarkdown('- a\n- b'), '<ul><li>a</li><li>b</li></ul>');
t('有序列表', renderMarkdown('1. a\n2. b'), '<ol><li>a</li><li>b</li></ol>');
t('有序列表起始号', renderMarkdown('3. a'), '<ol start="3"><li>a</li></ol>');
t('紧凑列表不包 p', renderMarkdown('- a\n- b'), hasnt('<p>'));
t('松列表包 p', renderMarkdown('- a\n\n- b'), has('<p>a</p>'));
t('嵌套列表', renderMarkdown('- a\n  - b'), has('<ul><li>b</li></ul>'));
t('列表里的行内标记', renderMarkdown('- **粗**'), '<ul><li><strong>粗</strong></li></ul>');
t('任务项未完成', renderMarkdown('- [ ] 待办'), has('md-task"'));
t('任务项已完成', renderMarkdown('- [x] 完成'), has('md-task done'));
t('列表后接段落', renderMarkdown('- a\n\n正文'), has('</ul><p>正文</p>'));
t('列表里的代码块', renderMarkdown('- a\n\n  ```\n  x\n  ```'), has('<code>x</code>'));

// ---- 列表相接（真机跑出来的 bug）----
// 模型真的会这么写：有序列表后空一行接任务清单。只比缩进不比标记类型的话，
// 后面这个 ul 会被整个吞进前面的 ol，任务清单变成有序列表的第 3、4 项。
const mixed = renderMarkdown('1. 甲\n2. 乙\n\n- [x] 已完成\n- [ ] 未完成');
t('有序列表后接无序列表不被吞', mixed, has('</ol><ul>'));
t('只生成一个 ul', (mixed.match(/<ul>/g) || []).length, 1);
t('有序那段只有两项', (mixed.match(/<ol>[\s\S]*?<\/ol>/)[0].match(/<li/g) || []).length, 2);
t('任务项落在 ul 里', mixed, s => /<ul><li class="md-task done"/.test(s));
t('无序列表后接有序列表也不被吞', renderMarkdown('- 甲\n- 乙\n\n1. 一\n2. 二'), has('</ul><ol>'));
t('紧凑项含嵌套时不多包 p', renderMarkdown('1. 甲\n   1. 嵌套'),
  '<ol><li>甲<ol><li>嵌套</li></ol></li></ol>');
t('松列表仍然包 p', renderMarkdown('- 甲\n\n- 乙'), has('<p>甲</p>'));

// ---- 表格 ----
const tbl = renderMarkdown('| a | b |\n| --- | ---: |\n| 1 | 2 |');
t('表头', tbl, has('<th>a</th>'));
t('表体', tbl, has('<td>1</td>'));
t('右对齐', tbl, has('text-align:right'));
t('表格有横向滚动容器', tbl, has('md-tw'));
t('单元格数不齐时补空', renderMarkdown('| a | b |\n|---|---|\n| 1 |'), has('<td></td>'));

// ---- 判定 ----
t('白话不算 markdown', looksLikeMarkdown('好的，我改完了。'), false);
t('带列表算 markdown', looksLikeMarkdown('- a'), true);
t('带代码算 markdown', looksLikeMarkdown('用 `x` 试试'), true);

// ---- 健壮性：不该抛 ----
for (const s of ['', null, undefined, '```', '|', '- ', '>', '#', '1.', '~~', '**', '[](', '- [ ]',
                 '|a|\n|-|\n', '- a\n    - b\n        - c', '\n\n\n', 'a'.repeat(50000)]) {
  try { renderMarkdown(s); pass++; } catch (e) { fail++; console.log('✗ 抛异常 ' + JSON.stringify(s) + ': ' + e.message); }
}

console.log(`\n${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
