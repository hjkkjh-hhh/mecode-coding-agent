---
name: mcp-install
description: 安装/配置 MCP server 的完整流程。当用户给出 MCP 链接/包名、要求安装或接入 MCP server 时使用。
---

# 安装 MCP server（用户给你 MCP 链接/包名、让你装时照这个做）

MCP server 是个子进程，mecode 启动时通过 stdio JSON-RPC 连它、把它的工具接进来。"装" = 让 mecode 知道怎么启动它：

0. 先确认运行时装没装：MCP server 多数靠 Node 的 npx 或 Python 的 uvx 启动。动手前用 bash 查
   `npx --version`（Node）和 `uv --version`（uv 提供 uvx）。把用户当【小白】——缺什么就【帮他装通】，
   别甩一句"你自己去装 Node"：
   - 缺 uv：有 pip 就 `pip install uv`（多数环境装完即有 uvx；若 uvx 仍找不到，是 PATH 未刷新——提醒用户重开终端）。
   - 缺 Node/npx：优先用现成包管理器装、同环境立即可用——有 conda 用 `conda install -y nodejs`；
     否则按系统来：Windows `winget install OpenJS.NodeJS.LTS`、macOS `brew install node`、Linux 用 apt/dnf。
     用 winget/系统包管理器装的，要提醒用户【重开终端再启 mecode】才认到。
   - 实在装不了（没权限/网络差/装失败）：给官网链接（Node 走 nodejs.org 的 LTS）+ 简短三步，让他装完再回来，
     别让他卡这一步。
   - 选 server 时【迁就现有运行时】：只有 npx 就挑 npx 系（filesystem/memory/everything），
     只有 uvx 就挑 uvx 系（fetch/time/git），都没有就先把上面缺的运行时装通。

1. 搞清楚怎么启动它（看它的 README / 仓库页）：
   - 用户只给了名字/描述、没给链接？用 web_search 搜"<名字> MCP server"找官方仓库，
     再用 web_fetch 读它的 README 拿启动命令。
   - 已发布的包多数不用预装、直接 npx/uvx 跑：`npx -y <npm 包> <参数>`、`uvx <pypi 包>`。
   - GitHub 源码仓库：先 git clone 到本地，按 README 装依赖（npm install / pip install -e .），再找它的启动命令。
   - 留意它需要的环境变量（API key 等），写进配置的 env。

2. 写进 mecode 的 MCP 配置（用 write_file/edit_file；文件已存在就【合并】进 mcpServers，别整个覆盖掉别的 server）：
   - 默认写当前项目：<当前工作目录>/.mcp.json —— 它在项目根内、写它不用额外授权，且符合"这 MCP 给本项目用"。
   - 仅当用户明说要【所有项目都能用】时，才写全局 ~/.mecode/mcp.json —— 它在项目根外，写入会向用户弹一次授权。
   - 格式：{"mcpServers": {"<名字>": {"command": "...", "args": ["...", "..."], "env": {"KEY": "值"}}}}
   - 【占位符，项目级配置必须遵守】.mcp.json 会随仓库提交分享，写死的绝对路径在别人机器上必坏：
     * 指向项目内的路径一律写 `${cwd}`（= 项目根，连接时自动展开）或 `${cwd}/子目录`，不要写死 C:/... 
     * API key 等机密写 `${env:变量名}`（连接时读环境变量），并告诉用户去设置该环境变量——
       【绝不】把 key 明文写进 .mcp.json（它会被提交进 git）。

3. 告诉用户：写好配置后【Ctrl+Q 退出 mecode 再重新打开】（不是 /rl /rs 续会话——那不会重连），
   启动时自动连上，右侧面板 MCP 一栏会显示已连服务名。之后那个 server 的工具就能用了
   （工具名带 <server>__ 前缀，外部工具默认每次调用会向你确认）。

例（文件系统 MCP，允许它访问当前项目目录；${cwd} 连接时自动展开成项目根）：
  {"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "${cwd}"]}}}
