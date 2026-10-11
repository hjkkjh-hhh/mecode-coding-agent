# mecode · Code Flow Graph

用 [Code Flow Graph skill](https://github.com/biubiubiu533/Code_Flow_Graph) 生成的三个交互视图。

## 打开

让 `code_flow_graph.html` 与 `code_flow_graph_data.js` 位于同一文件夹，直接用浏览器打开 HTML。无需安装依赖、启动 mecode、配置 API Key 或联网。

也可在此目录执行 `python -m http.server 8765 --bind 127.0.0.1`，然后访问 `http://localhost:8765/code_flow_graph.html`。云环境里的 localhost 属于云端；本机观看应下载本目录，或使用平台提供的端口转发。

## 看什么

1. **项目总览**：12 个核心模块/类，展示入口、内核、模型工具、会话与后台任务。
2. **Agent 主循环**：点击 `run_turn()` 查看调用树；展开 `_turn_loop()` 左侧三角；点击 `_exec_parallel_batch()` 查看 Future 与 ToolCall 的配对过程。
3. **UI 事件与停止**：桌面/TUI 如何消费事件、排队用户输入、接续后台结果和发起中断。

悬停函数显示中文说明、真实签名和源码行号。点击高亮关系；Ctrl+K 跨图搜索；滚轮缩放、空白拖拽平移、拖动标题移动节点；Ctrl+Z 撤销布局调整，Reset Layout 恢复布局。

## 边界与来源

这是根据实际源码整理的核心路径导览，不是穷尽所有静态调用的分析器输出。分支、间接调用和条件见节点说明；连线不代表所有路径都会依次执行。

源码固定于提交 [`58c464e`](https://github.com/hjkkjh-hhh/mecode-coding-agent/commit/58c464ee33cd2620e921937465290af521d5eb57)，包含统一 `run_turn(user_input=None)` 的修改。原版 HTML 渲染器逐字复制，没有改造。原版点击函数会高亮/展开调用链，尚不支持点击节点直接跳转编辑器；可使用 [源码索引](SOURCE_INDEX.md) 的 GitHub 链接定位到该版本的代码行。生成基线、源码哈希与上游版本见 `manifest.json`。

上游 MIT 许可保留在 `LICENSE.Code-Flow-Graph`。后续新增视图应追加至数据文件，避免覆盖已生成内容。
