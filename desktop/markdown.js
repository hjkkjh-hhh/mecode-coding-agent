/* mecode 桌面端的 Markdown 渲染器：零依赖、零网络。

   为什么不引第三方（marked / markdown-it）：
   页面是本地服务直发的单文件，没有打包步骤，引库就得下发一份 vendor 或走 CDN。
   CDN 违背"本地跑、不外联"；vendor 一份 40KB 的库来渲染 agent 的回答，
   而 agent 的输出实际只用到 Markdown 的一个子集（标题/列表/代码块/表格/强调/链接）。

   安全模型 —— 这里渲染的是【模型生成的内容】，等同于不可信输入：
     1. 先把整段源文本转义（mdEsc），之后所有插入的标签都是本文件自己生成的，
        源文本里的 < > & " ' 永远到不了 DOM 解析器手上。
     2. 链接 href 走协议白名单（MD_SAFE_URL），挡掉 javascript: / data:text/html 这类。
     3. 图片【不发网络请求】，渲染成一个链接。模型写的图片地址一旦直接 <img>，
        打开页面就是一次静默外联（暴露"我在看这条回答"）。要看的人自己点。

   流式友好：没闭合的 ``` 当作到结尾都是代码块（渲染半截回答时不会整段炸开）。
*/
'use strict';

const MD_ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const mdEsc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => MD_ESC[c]);

/* 抠走行内代码 / 转义竖线时用的占位符。用控制字符是因为它【转义后不可能出现】：
   源文本进来第一件事就是把这两个字符删掉（见 renderMarkdown），
   所以占位符永远不会和正文串味。 */
const MD_HOLE = String.fromCharCode(0);
const MD_PIPE = String.fromCharCode(1);

/* 只放行这几类 href。相对路径与锚点也放行（本地文档链接）。
   注意 data: 整体不放行——data:text/html 等价于任意脚本。 */
const MD_SAFE_URL = /^(https?:\/\/|mailto:|tel:|file:\/\/|#|\.{0,2}\/)/i;
const mdHref = u => {
  const t = String(u || '').trim();
  return MD_SAFE_URL.test(t) ? mdEsc(t) : '';
};

/* ---------- 行内 ---------- */

/* 行内规则的顺序是有讲究的：
   代码片段先抠走（占位），否则 `a * b * c` 里的星号会被当强调；
   链接/图片在强调之前（URL 里的 _ 和 * 很常见，先成链接就不会被当强调符）；
   最后才是 ** __ * _ ~~。 */
function mdInline(text) {
  const code = [];
  let s = mdEsc(text)
    // 反引号片段：``a`b`` 这种双反引号形式也认（内部允许单个反引号）
    .replace(/(`+)([\s\S]*?[^`])\1(?!`)/g, (_m, _t, body) =>
      MD_HOLE + (code.push('<code>' + body.replace(/^ | $/g, '') + '</code>') - 1) + MD_HOLE);

  s = s
    // 图片：故意【不发请求】，渲染成可点的链接（理由见文件头）
    .replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)/g, (m, alt, url) => {
      const h = mdHref(url);
      return h ? `<a class="md-img" href="${h}" target="_blank" rel="noopener noreferrer">🖼 ${alt || h}</a>` : m;
    })
    // 行内链接
    .replace(/\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)/g, (m, label, url) => {
      const h = mdHref(url);
      return h ? `<a href="${h}" target="_blank" rel="noopener noreferrer">${label || h}</a>` : m;
    })
    // 裸 URL 自动成链（前面不能紧挨引号/等号——那多半已经在属性里了）
    // 尾字符排除表里带上中文标点："见 https://a.com。" 里的句号不属于 URL
    .replace(/(^|[\s(])(https?:\/\/[^\s<>"'`)]+[^\s<>"'`).,;:!?、。，；：！？）」』】》])/g,
      (_m, pre, url) => `${pre}<a href="${mdEsc(url)}" target="_blank" rel="noopener noreferrer">${mdEsc(url)}</a>`)
    .replace(/\*\*\*([^*]+?)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w\\])__([^_]+?)__(?!\w)/g, '$1<strong>$2</strong>')
    .replace(/(^|[^*\w\\])\*([^*\s][^*]*?)\*(?![*\w])/g, '$1<em>$2</em>')
    // _斜体_ 只在词边界生效：snake_case_name 里的下划线不是强调
    .replace(/(^|[^\w\\])_([^_\s][^_]*?)_(?!\w)/g, '$1<em>$2</em>')
    .replace(/~~([^~]+?)~~/g, '<del>$1</del>');

  return s.replace(new RegExp(MD_HOLE + '(\\d+)' + MD_HOLE, 'g'), (_m, i) => code[+i]);
}

/* ---------- 块级 ---------- */

const MD_FENCE = /^(\s{0,3})(`{3,}|~{3,})\s*([^\s`]*)/;
const MD_HEAD = /^(\s{0,3})(#{1,6})\s+(.*)$/;
const MD_HR = /^\s{0,3}([-*_])\s*(?:\1\s*){2,}$/;
const MD_QUOTE = /^\s{0,3}>\s?(.*)$/;
const MD_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])(\s+)(.*)$/;
const MD_TABLE_SEP = /^\s{0,3}\|?[\s:|-]*-[\s:|-]*\|?\s*$/;
const MD_TASK = /^\[([ xX])\]\s+/;

/** 表格一行 → 单元格数组。转义过的 \| 不当分隔符。 */
function mdCells(line) {
  return line.replace(/\\\|/g, MD_PIPE).trim().replace(/^\||\|$/g, '').split('|')
    .map(c => c.split(MD_PIPE).join('|').trim());
}

/** 一段列表：从 lines[start] 开始，返回 [html, 下一行下标]。按缩进递归嵌套。 */
function mdList(lines, start) {
  const first = MD_ITEM.exec(lines[start]);
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const items = [];           // 每项 = { lines, pad }
  let i = start, loose = false, blanks = 0;

  while (i < lines.length) {
    const ln = lines[i];
    if (!ln.trim()) { blanks++; i++; continue; }
    const ind = ln.search(/\S/);
    const m = MD_ITEM.exec(ln);

    // 同级的下一项。【标记类型也要一致】——有序和无序是两个列表，
    // "- 甲" 不能续 "1. 乙" 的命；只比缩进的话，紧跟在有序列表后的无序列表会被整个吞进去。
    if (m && m[1].length >= base && m[1].length <= base + 1 && /\d/.test(m[2]) === ordered) {
      if (items.length && blanks) loose = true;   // 空行分隔 → 松列表（每项包 <p>，行距更大）
      items.push({ lines: [m[4]], pad: m[1].length + m[2].length + m[3].length });
      blanks = 0; i++; continue;
    }
    if (!items.length) break;
    if (ind > base) {
      // 缩进的续行 / 嵌套内容：按本项的内容缩进剥掉前导空白，交给递归
      const cur = items[items.length - 1];
      if (blanks) { cur.lines.push(''); loose = true; }
      cur.lines.push(ln.slice(Math.min(ind, cur.pad)));
      blanks = 0; i++; continue;
    }
    if (blanks) break;                       // 空行之后回到基准缩进 → 列表结束
    items[items.length - 1].lines.push(ln.trim());   // 懒延续：段落换行接着上一项
    i++;
  }

  const html = items.map(it => {
    const body = it.lines.join('\n');
    // 内容里有块级结构（空行/嵌套列表/围栏/标题/引用）才递归整套块解析，
    // 否则只走行内——不然每个 "- 一句话" 都被包成 <p>，行距凭空撑开。
    const block = loose || /\n\s*\n/.test(body) ||
      body.split('\n').slice(1).some(l =>
        MD_ITEM.test(l) || MD_FENCE.test(l) || MD_HEAD.test(l) || MD_QUOTE.test(l));
    const task = MD_TASK.exec(it.lines[0] || '');
    if (task) {
      const done = task[1].toLowerCase() === 'x';
      const rest = body.replace(MD_TASK, '');
      return `<li class="md-task${done ? ' done' : ''}"><span class="md-box">${done ? '✓' : ''}</span>`
        + inner(rest, block, loose) + '</li>';
    }
    return `<li>${inner(body, block, loose)}</li>`;
  }).join('');

  // 紧凑列表里某项含嵌套结构时会走块解析，首行凭空包上 <p>、和同列表其它项行距对不上。
  // 剥掉首段的 <p>（CommonMark 的 tight list 就是这个渲法）；松列表则保留。
  function inner(body, block, loose) {
    if (!block) return mdInline(body);
    const html = mdBlocks(body.split('\n'));
    return loose ? html : html.replace(/^<p>([\s\S]*?)<\/p>/, '$1');
  }

  const startAt = ordered ? parseInt(first[2], 10) : 1;
  const open = ordered ? `<ol${startAt !== 1 ? ` start="${startAt}"` : ''}>` : '<ul>';
  return [open + html + (ordered ? '</ol>' : '</ul>'), i];
}

/** 行数组 → HTML。renderMarkdown 的实际主体（列表/引用递归也走这里）。 */
function mdBlocks(lines) {
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const ln = lines[i];
    if (!ln.trim()) { i++; continue; }

    // 围栏代码块。没闭合就吃到结尾——流式渲染时半个代码块也不会炸开
    const fence = MD_FENCE.exec(ln);
    if (fence) {
      const close = new RegExp('^\\s{0,3}' + fence[2][0] + '{' + fence[2].length + ',}\\s*$');
      const body = [];
      i++;
      while (i < lines.length && !close.test(lines[i])) body.push(lines[i++]);
      if (i < lines.length) i++;                     // 吃掉闭合行
      const lang = fence[3] ? mdEsc(fence[3]) : '';
      out.push(`<div class="md-code"${lang ? ` data-lang="${lang}"` : ''}>` +
        '<button class="md-cp" type="button" title="复制代码">复制</button>' +
        `<pre><code${lang ? ` class="lang-${lang}"` : ''}>${mdEsc(body.join('\n'))}</code></pre></div>`);
      continue;
    }

    if (MD_HR.test(ln)) { out.push('<hr>'); i++; continue; }

    const head = MD_HEAD.exec(ln);
    if (head) {
      const lv = head[2].length;
      out.push(`<h${lv}>${mdInline(head[3].replace(/\s+#+\s*$/, ''))}</h${lv}>`);
      i++; continue;
    }

    if (MD_QUOTE.test(ln)) {
      const body = [];
      while (i < lines.length && (MD_QUOTE.test(lines[i]) || (body.length && lines[i].trim()))) {
        const q = MD_QUOTE.exec(lines[i]);
        body.push(q ? q[1] : lines[i].trim());       // 懒延续
        i++;
      }
      out.push('<blockquote>' + mdBlocks(body) + '</blockquote>');
      continue;
    }

    if (MD_ITEM.test(ln)) {
      const [html, next] = mdList(lines, i);
      out.push(html); i = next; continue;
    }

    // 表格：本行有 |，下一行是分隔行
    if (ln.includes('|') && i + 1 < lines.length && lines[i + 1].includes('-')
        && MD_TABLE_SEP.test(lines[i + 1])) {
      const cols = mdCells(ln);
      const align = mdCells(lines[i + 1]).map(c =>
        c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right'
        : c.startsWith(':') ? 'left' : '');
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) rows.push(mdCells(lines[i++]));
      const sty = k => (align[k] ? ` style="text-align:${align[k]}"` : '');
      const th = cols.map((c, k) => `<th${sty(k)}>${mdInline(c)}</th>`).join('');
      const tb = rows.map(r => '<tr>' + cols.map((_c, k) =>
        `<td${sty(k)}>${mdInline(r[k] || '')}</td>`).join('') + '</tr>').join('');
      out.push(`<div class="md-tw"><table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table></div>`);
      continue;
    }

    // 段落：吃到空行或下一个块级开头为止
    const para = [];
    while (i < lines.length && lines[i].trim() && !MD_FENCE.test(lines[i]) && !MD_HEAD.test(lines[i])
           && !MD_HR.test(lines[i]) && !MD_QUOTE.test(lines[i]) && !MD_ITEM.test(lines[i])) {
      para.push(lines[i]); i++;
    }
    // Markdown 原义里单换行不算换行，但 agent 的回答几乎都按"我换行就是要换行"写，
    // 按原义折行会把清单挤成一坨。这里按 GFM 的 breaks 模式处理。
    out.push('<p>' + para.map(l => mdInline(l.replace(/\s+$/, ''))).join('<br>') + '</p>');
  }
  return out.join('');
}

/** 入口：Markdown 源文本 → 安全 HTML 字符串。 */
function renderMarkdown(src) {
  const text = String(src == null ? '' : src)
    .split(MD_HOLE).join('').split(MD_PIPE).join('')   // 占位符字符，源文本里出现会串味
    .replace(/\r\n?/g, '\n')
    .replace(/\t/g, '    ');
  return mdBlocks(text.split('\n'));
}

/** 这段文本值不值得走 Markdown：一句纯白话没有任何标记时，当纯文本更快也更保真。 */
const looksLikeMarkdown = s =>
  /(^|\n)\s{0,3}(#{1,6}\s|[-*+]\s|\d{1,9}[.)]\s|>\s|```|~~~|\|)|\*\*|`|\[[^\]]*\]\(/.test(s || '');

if (typeof module !== 'undefined' && module.exports) {     // Node 里跑单测用
  module.exports = { renderMarkdown, mdInline, mdEsc, looksLikeMarkdown };
}
