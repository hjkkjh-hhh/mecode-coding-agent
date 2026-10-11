// Generated from the current mecode working tree; renderer from Code Flow Graph.
var DIAGRAMS = {};
DIAGRAMS._projectTitle = "mecode / Code Flow Graph";

DIAGRAMS.overview = {
  "title": "mecode · 从入口到 Agent 核心",
  "sub": "01 / 项目总览 · 选取核心模块与类；悬停查看职责和源码行号，点击函数高亮关联，Ctrl+K 搜索",
  "navLabel": "01  项目总览",
  "navSub": "入口 / 主循环 / 工具 / 会话",
  "NODES": [
    {
      "id": "CLI",
      "label": "cli.py · 启动入口",
      "type": "module",
      "x": 30,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "命令分发",
          "attrs": [
            {
              "id": "CLI.main",
              "name": "main()",
              "sig": "<span class=\"sig-name\">main() → None</span>\nsrc/mecode/cli.py:30",
              "desc": "选择桌面、终端或无头模式。",
              "detail": "mecode desk 启动 deskserve；无参数进入 TUI；其他参数交给无头入口。",
              "source": {
                "path": "src/mecode/cli.py",
                "line": 30,
                "symbol": "main"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Desk",
      "label": "Desk · 桌面服务",
      "type": "widget",
      "x": 30,
      "y": 270,
      "w": 280,
      "sections": [
        {
          "title": "scripts/deskserve.py",
          "attrs": [
            {
              "id": "Desk.init",
              "name": "__init__()",
              "sig": "<span class=\"sig-name\">Desk.__init__(self, cwd: Path, mode: str, mcp: bool, **build_kwargs) → None</span>\nscripts/deskserve.py:313",
              "desc": "使用工厂创建 Agent，并接好界面回调。",
              "detail": "初始化会话、事件总线、权限问答通道；通过 build_agent 装配内核。",
              "source": {
                "path": "scripts/deskserve.py",
                "line": 313,
                "symbol": "Desk.__init__"
              }
            },
            {
              "id": "Desk.pump",
              "name": "_pump()",
              "sig": "<span class=\"sig-name\">Desk._pump(self, text: str | None) → None</span>\nscripts/deskserve.py:443",
              "desc": "把 Agent 事件转换成前端消息。",
              "detail": "消费 run_turn(text)，经 encode 和 Bus 转发；finally 发 turn_end，并检查是否需要自动接续。",
              "source": {
                "path": "scripts/deskserve.py",
                "line": 443,
                "symbol": "Desk._pump"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "TUI",
      "label": "MecodeApp · 终端界面",
      "type": "widget",
      "x": 30,
      "y": 480,
      "w": 280,
      "sections": [
        {
          "title": "scripts/tui.py",
          "attrs": [
            {
              "id": "TUI.run",
              "name": "_run_agent()",
              "sig": "<span class=\"sig-name\">MecodeApp._run_agent(self, user: str) → None</span>\nscripts/tui.py:3277",
              "desc": "工作线程消费用户轮的事件。",
              "detail": "调用 run_turn(user)，逐个 post_message(AgentEvent(ev))；UI 线程更新界面。",
              "source": {
                "path": "scripts/tui.py",
                "line": 3277,
                "symbol": "MecodeApp._run_agent"
              }
            },
            {
              "id": "TUI.bg",
              "name": "_run_bg_agent()",
              "sig": "<span class=\"sig-name\">MecodeApp._run_bg_agent(self) → None</span>\nscripts/tui.py:3358",
              "desc": "无新输入时，让同一个 Agent 接续。",
              "detail": "调用 run_turn()；后台完成和排队用户消息在主循环里注入。这里是界面事件泵。",
              "source": {
                "path": "scripts/tui.py",
                "line": 3358,
                "symbol": "MecodeApp._run_bg_agent"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Bootstrap",
      "label": "bootstrap.py · 装配",
      "type": "module",
      "x": 350,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "依赖组装",
          "attrs": [
            {
              "id": "Bootstrap.build",
              "name": "build_agent()",
              "sig": "<span class=\"sig-name\">build_agent(cwd: str | Path | None=None, *, mode: str=&#x27;auto&#x27;, session_id: str | None=None, mcp: bool=True, ask_permission=None, registry: ToolRegistry | None=None, provider: Provider | None=None) → Agent</span>\nsrc/mecode/bootstrap.py:39",
              "desc": "将 Provider、工具、会话和提示词交给 Agent。",
              "detail": "装配 SessionStore、default_registry、记忆工具、可选 MCP、Provider 和系统提示词；恢复会话时加载工作上下文。TUI 有自己的装配路径。",
              "source": {
                "path": "src/mecode/bootstrap.py",
                "line": 39,
                "symbol": "build_agent"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Agent",
      "label": "Agent · 对话调度核心",
      "type": "class",
      "x": 350,
      "y": 270,
      "w": 280,
      "sections": [
        {
          "title": "src/mecode/agent.py",
          "attrs": [
            {
              "id": "Agent.run_turn",
              "name": "run_turn()",
              "sig": "<span class=\"sig-name\">Agent.run_turn(self, user_input: str | None=None) → Iterator[Event]</span>\nsrc/mecode/agent.py:290",
              "desc": "普通输入与自动接续共用的唯一回合入口。",
              "detail": "None 表示没有新用户输入；空字符串仍然是一条用户消息。准备上下文 → 执行循环 → 保存上下文用量。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 290,
                "symbol": "Agent.run_turn"
              }
            },
            {
              "id": "Agent._turn_loop",
              "name": "_turn_loop()",
              "sig": "<span class=\"sig-name\">Agent._turn_loop(self) → Iterator[Event]</span>\nsrc/mecode/agent.py:553",
              "desc": "重复执行模型请求、工具调用与结果回填。",
              "detail": "无工具调用时结束；有工具则执行后继续请求模型。调用前补入后台结果、排队消息和模式提醒。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 553,
                "symbol": "Agent._turn_loop"
              }
            },
            {
              "id": "Agent._record",
              "name": "_record()",
              "sig": "<span class=\"sig-name\">Agent._record(self, msg: dict) → None</span>\nsrc/mecode/agent.py:209",
              "desc": "统一更新内存消息与会话存档。",
              "detail": "先追加到 self.messages；存在 store 时追加到 transcript.jsonl。界面收到事件不等于模型自动得到上下文。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 209,
                "symbol": "Agent._record"
              }
            },
            {
              "id": "Agent.request_interrupt",
              "name": "request_interrupt()",
              "sig": "<span class=\"sig-name\">Agent.request_interrupt(self) → None</span>\nsrc/mecode/agent.py:275",
              "desc": "请求协作停止并清理活动进程。",
              "detail": "设置 threading.Event，并启动清理线程；Provider、循环边界和支持取消的进程分别响应。普通同步工具没有通用强杀线程能力。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 275,
                "symbol": "Agent.request_interrupt"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Compact",
      "label": "compact.py · 上下文压缩",
      "type": "module",
      "x": 350,
      "y": 550,
      "w": 280,
      "sections": [
        {
          "title": "工作记忆管理",
          "attrs": [
            {
              "id": "Compact.compact",
              "name": "compact()",
              "sig": "<span class=\"sig-name\">compact(summarize: Callable[[list[dict]], str], messages: list[dict], *, read_tools: tuple[str, ...]=(&#x27;read_file&#x27;,), rescue_count: int=0, rescue_max_tokens: int=0, rescue_max_files: int=0, transcript_pointer: str | None=None, on_compacted: Callable[[list[dict]], None] | None=None, claude_md: str | None=None, mcp_instructions: str | None=None) → list[dict] | None</span>\nsrc/mecode/compact.py:291",
              "desc": "用摘要和关键原文重建工作上下文。",
              "detail": "保留系统提示、最后一轮 user/assistant 和配置允许抢救的读取结果；通过回调写 compaction marker。空摘要不会替换现有上下文。",
              "source": {
                "path": "src/mecode/compact.py",
                "line": 291,
                "symbol": "compact"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Provider",
      "label": "Provider · 模型通信",
      "type": "class",
      "x": 670,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "src/mecode/provider.py",
          "attrs": [
            {
              "id": "Provider.stream",
              "name": "stream()",
              "sig": "<span class=\"sig-name\">Provider.stream(self, messages: list[dict], tools: list[dict] | None=None, should_stop: Callable[[], bool] | None=None) → Iterator[Event]</span>\nsrc/mecode/provider.py:314",
              "desc": "把模型响应转换为统一事件流。",
              "detail": "同步生成器对外提供 TextDelta、ReasoningDelta、ToolCall、Usage 等事件；独立线程中的异步 HTTP 负责网络 I/O。",
              "source": {
                "path": "src/mecode/provider.py",
                "line": 314,
                "symbol": "Provider.stream"
              }
            },
            {
              "id": "Provider._for_wire",
              "name": "_for_wire()",
              "sig": "<span class=\"sig-name\">Provider._for_wire(self, messages: list[dict]) → list[dict]</span>\nsrc/mecode/provider.py:289",
              "desc": "发送前按后端规则整理消息。",
              "detail": "剥离 mecode 私有字段；按模型配置决定 reasoning_content 的传递方式。存档事实与请求载荷分开处理。",
              "source": {
                "path": "src/mecode/provider.py",
                "line": 289,
                "symbol": "Provider._for_wire"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Tools",
      "label": "ToolRegistry · 工具注册表",
      "type": "class",
      "x": 670,
      "y": 270,
      "w": 280,
      "sections": [
        {
          "title": "src/mecode/tools.py",
          "attrs": [
            {
              "id": "Tools.schemas",
              "name": "schemas()",
              "sig": "<span class=\"sig-name\">ToolRegistry.schemas(self) → list[dict]</span>\nsrc/mecode/tools.py:91",
              "desc": "给模型提供工具名称、说明与 JSON 参数格式。",
              "detail": "只提供可调用能力的 schema；模型选择后产生 ToolCall，由 Agent 做审批和执行调度。",
              "source": {
                "path": "src/mecode/tools.py",
                "line": 91,
                "symbol": "ToolRegistry.schemas"
              }
            },
            {
              "id": "Tools.execute",
              "name": "execute()",
              "sig": "<span class=\"sig-name\">ToolRegistry.execute(self, name: str, arguments: dict, slot: &#x27;ProcSlot | None&#x27;=None, bg: &#x27;BackgroundManager | None&#x27;=None) → str</span>\nsrc/mecode/tools.py:118",
              "desc": "按名字找到工具 handler 并执行。",
              "detail": "内置文件、Shell、搜索工具与动态注册工具共用分发入口；处理参数传递，并将普通执行异常转换为结果文本。",
              "source": {
                "path": "src/mecode/tools.py",
                "line": 118,
                "symbol": "ToolRegistry.execute"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "MCP",
      "label": "MCPClient · 外部工具",
      "type": "class",
      "x": 670,
      "y": 480,
      "w": 280,
      "sections": [
        {
          "title": "src/mecode/mcp.py",
          "attrs": [
            {
              "id": "MCP.start",
              "name": "start()",
              "sig": "<span class=\"sig-name\">MCPClient.start(self, timeout: float=15.0) → list[dict]</span>\nsrc/mecode/mcp.py:72",
              "desc": "启动 stdio MCP 服务并发现工具。",
              "detail": "连接后把服务工具包装为 mecode Tool，注册进统一 ToolRegistry。",
              "source": {
                "path": "src/mecode/mcp.py",
                "line": 72,
                "symbol": "MCPClient.start"
              }
            },
            {
              "id": "MCP.call_tool",
              "name": "call_tool()",
              "sig": "<span class=\"sig-name\">MCPClient.call_tool(self, name: str, arguments: dict, timeout: float=60.0) → str</span>\nsrc/mecode/mcp.py:100",
              "desc": "通过 JSON-RPC 调用外部服务工具。",
              "detail": "外部工具结果返回注册表，再沿 Agent 的结果处理与记录路径进入上下文。",
              "source": {
                "path": "src/mecode/mcp.py",
                "line": 100,
                "symbol": "MCPClient.call_tool"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Session",
      "label": "SessionStore · 会话持久化",
      "type": "class",
      "x": 990,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "src/mecode/session.py",
          "attrs": [
            {
              "id": "Session.append",
              "name": "append_transcript()",
              "sig": "<span class=\"sig-name\">SessionStore.append_transcript(self, entry: dict) → None</span>\nsrc/mecode/session.py:93",
              "desc": "逐条追加会话事实到 JSONL。",
              "detail": "保存 user、assistant、tool 消息以及压缩标记；工作上下文与完整会话历史的读取方式不同。",
              "source": {
                "path": "src/mecode/session.py",
                "line": 93,
                "symbol": "SessionStore.append_transcript"
              }
            },
            {
              "id": "Session.load",
              "name": "load_messages()",
              "sig": "<span class=\"sig-name\">SessionStore.load_messages(self) → list[dict] | None</span>\nsrc/mecode/session.py:107",
              "desc": "从存档恢复模型要用的工作上下文。",
              "detail": "折叠最近的 compaction marker，再接上后续消息；UI 可通过另一条接口读取完整历史。",
              "source": {
                "path": "src/mecode/session.py",
                "line": 107,
                "symbol": "SessionStore.load_messages"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Background",
      "label": "BackgroundManager · 后台任务",
      "type": "class",
      "x": 990,
      "y": 270,
      "w": 280,
      "sections": [
        {
          "title": "src/mecode/background.py",
          "attrs": [
            {
              "id": "Background.start",
              "name": "start()",
              "sig": "<span class=\"sig-name\">BackgroundManager.start(self, command: str, timeout: int | None=None) → int</span>\nsrc/mecode/background.py:72",
              "desc": "启动后台 Shell，立即返回任务编号。",
              "detail": "后台监控记录完成状态和输出；执行结果不会自动变成当前工具调用的最终结果。",
              "source": {
                "path": "src/mecode/background.py",
                "line": 72,
                "symbol": "BackgroundManager.start"
              }
            },
            {
              "id": "Background.drain",
              "name": "drain_completions()",
              "sig": "<span class=\"sig-name\">BackgroundManager.drain_completions(self) → list[BackgroundTask]</span>\nsrc/mecode/background.py:265",
              "desc": "取走待注入的后台完成通知。",
              "detail": "Agent 每次请求模型前读取队列，并以 user + system-reminder 的约定写入上下文。",
              "source": {
                "path": "src/mecode/background.py",
                "line": 265,
                "symbol": "BackgroundManager.drain_completions"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Subagent",
      "label": "SubagentRunner · 子 Agent",
      "type": "class",
      "x": 990,
      "y": 480,
      "w": 280,
      "sections": [
        {
          "title": "src/mecode/subagent.py",
          "attrs": [
            {
              "id": "Subagent.run",
              "name": "run()",
              "sig": "<span class=\"sig-name\">SubagentRunner.run(self, prompt: str) → str</span>\nsrc/mecode/subagent.py:106",
              "desc": "在隔离上下文里运行前台子任务。",
              "detail": "创建子 Agent、收集结果，向主 Agent 返回总结；前台子 Agent 与主任务共享中断信号。",
              "source": {
                "path": "src/mecode/subagent.py",
                "line": 106,
                "symbol": "SubagentRunner.run"
              }
            },
            {
              "id": "Subagent.bg",
              "name": "run_for_bg()",
              "sig": "<span class=\"sig-name\">SubagentRunner.run_for_bg(self, prompt: str, interrupt: threading.Event, on_slot) → str</span>\nsrc/mecode/subagent.py:130",
              "desc": "用独立中断信号运行后台子任务。",
              "detail": "后台任务独立管理停止和进程槽；完成后交给 BackgroundManager 发布结果。",
              "source": {
                "path": "src/mecode/subagent.py",
                "line": 130,
                "symbol": "SubagentRunner.run_for_bg"
              }
            }
          ]
        }
      ]
    }
  ],
  "CONNECTIONS": [
    [
      "CLI.main",
      "Desk.init",
      "#a6e3a1",
      false,
      "desk"
    ],
    [
      "CLI.main",
      "TUI.run",
      "#6c7086",
      false,
      "TUI 路径"
    ],
    [
      "Desk.init",
      "Bootstrap.build",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Bootstrap.build",
      "Agent.run_turn",
      "#6c7086",
      false,
      "构造 Agent"
    ],
    [
      "Desk.pump",
      "Agent.run_turn",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "TUI.run",
      "Agent.run_turn",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "TUI.bg",
      "Agent.run_turn",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Agent._turn_loop",
      "Provider.stream",
      "#a6e3a1",
      false,
      "模型请求"
    ],
    [
      "Agent._turn_loop",
      "Tools.execute",
      "#a6e3a1",
      false,
      "审批后执行"
    ],
    [
      "Tools.execute",
      "MCP.call_tool",
      "#a6e3a1",
      false,
      "注册的 handler"
    ],
    [
      "Agent._record",
      "Session.append",
      "#89b4fa",
      false,
      "保存消息"
    ],
    [
      "Bootstrap.build",
      "Session.load",
      "#a6e3a1",
      false,
      "恢复会话"
    ],
    [
      "Agent.run_turn",
      "Compact.compact",
      "#6c7086",
      false,
      "达到阈值时"
    ],
    [
      "Agent._turn_loop",
      "Background.drain",
      "#a6e3a1",
      false,
      "请求前取结果"
    ],
    [
      "Tools.execute",
      "Background.start",
      "#a6e3a1",
      false,
      "bash background"
    ],
    [
      "Tools.execute",
      "Subagent.run",
      "#a6e3a1",
      false,
      "subagent"
    ]
  ],
  "GROUPS": [
    {
      "id": "entry",
      "label": "用户入口",
      "nodes": [
        "CLI",
        "Desk",
        "TUI"
      ],
      "color": "#f5c2e7",
      "bg": "rgba(137,180,250,0.025)"
    },
    {
      "id": "core",
      "label": "内核与上下文",
      "nodes": [
        "Bootstrap",
        "Agent",
        "Compact"
      ],
      "color": "#89b4fa",
      "bg": "rgba(137,180,250,0.025)"
    },
    {
      "id": "capability",
      "label": "模型与工具能力",
      "nodes": [
        "Provider",
        "Tools",
        "MCP"
      ],
      "color": "#a6e3a1",
      "bg": "rgba(137,180,250,0.025)"
    },
    {
      "id": "lifecycle",
      "label": "会话与任务生命周期",
      "nodes": [
        "Session",
        "Background",
        "Subagent"
      ],
      "color": "#fab387",
      "bg": "rgba(137,180,250,0.025)"
    }
  ]
};

DIAGRAMS.agent_main_loop = {
  "title": "Agent.run_turn() · 一轮对话怎样执行",
  "sub": "02 / 主循环 · 点击 run_turn() 展开调用树；连线表示调用或数据传递，分支顺序请看节点说明",
  "navLabel": "02  Agent 主循环",
  "navSub": "准备 → 请求模型 ⇄ 执行工具 → 收尾",
  "NODES": [
    {
      "id": "Agent",
      "label": "Agent · 回合入口",
      "type": "entry",
      "x": 30,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "入口与异常边界",
          "attrs": [
            {
              "id": "Agent.run_turn",
              "name": "run_turn()",
              "sig": "<span class=\"sig-name\">Agent.run_turn(self, user_input: str | None=None) → Iterator[Event]</span>\nsrc/mecode/agent.py:290",
              "desc": "一轮对话的总入口：用户输入或自动接续。",
              "detail": "① None 且无待处理任务/消息时直接返回。② _pre_turn 准备。③ 有新输入则 _record。④ _run_loop_guarded。⑤ 正常收尾路径保存上下文用量。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 290,
                "symbol": "Agent.run_turn"
              },
              "io": {
                "input": "user_input: str | None；None 与空字符串含义不同",
                "output": "Iterator[Event]，由界面持续消费"
              },
              "callChain": [
                {
                  "id": "Agent.run_turn",
                  "name": "run_turn()",
                  "module": "agent.py:290",
                  "desc": "一轮对话的总入口：用户输入或自动接续。",
                  "calls": [
                    {
                      "id": "Agent._pre_turn",
                      "name": "_pre_turn()",
                      "module": "agent.py:366",
                      "desc": "重置中断状态，并准备工作上下文。",
                      "calls": [
                        {
                          "id": "Context._maybe_compact",
                          "name": "_maybe_compact()",
                          "module": "agent.py:325",
                          "desc": "轮开始和后续工具迭代之间检查压缩阈值。",
                          "calls": []
                        }
                      ]
                    },
                    {
                      "id": "Loop._record",
                      "name": "_record()",
                      "module": "agent.py:209",
                      "desc": "有新输入时，先记录本轮 user 消息。",
                      "calls": []
                    },
                    {
                      "id": "Agent._run_loop_guarded",
                      "name": "_run_loop_guarded()",
                      "module": "agent.py:421",
                      "desc": "包住主循环，处理停止时的上下文修复。",
                      "calls": [
                        {
                          "id": "Loop._turn_loop",
                          "name": "_turn_loop()",
                          "module": "agent.py:553",
                          "desc": "请求模型，保存 assistant，执行工具，然后继续。",
                          "calls": [
                            {
                              "id": "Context._inject_bg_events",
                              "name": "_inject_bg_events()",
                              "module": "agent.py:445",
                              "desc": "在模型请求前注入后台事件与排队消息。",
                              "calls": []
                            },
                            {
                              "id": "Context._inject_mode_reminder",
                              "name": "_inject_mode_reminder()",
                              "module": "agent.py:390",
                              "desc": "比较当前模式与上下文最近的模式记录。",
                              "calls": []
                            },
                            {
                              "id": "Provider.stream",
                              "name": "stream()",
                              "module": "provider.py:314",
                              "desc": "传入 messages、schemas、should_stop；消费流式事件。",
                              "calls": []
                            },
                            {
                              "id": "Loop._record",
                              "name": "_record()",
                              "module": "agent.py:209",
                              "desc": "保存完整 assistant；无 tool_calls 就返回。",
                              "calls": [
                                {
                                  "id": "Session.append_transcript",
                                  "name": "append_transcript()",
                                  "module": "session.py:93",
                                  "desc": "向 transcript.jsonl 追加消息。",
                                  "calls": []
                                }
                              ]
                            },
                            {
                              "id": "Exec._exec_tool",
                              "name": "_exec_tool()",
                              "module": "agent.py:737",
                              "desc": "非只读工具按模型顺序执行；输出处理后先记录再展示。",
                              "calls": [
                                {
                                  "id": "Registry.execute",
                                  "name": "execute()",
                                  "module": "tools.py:118",
                                  "desc": "按工具名称执行对应 handler。",
                                  "calls": []
                                },
                                {
                                  "id": "Exec._process_result",
                                  "name": "_process_result()",
                                  "module": "agent.py:729",
                                  "desc": "限制结果体积，必要时把完整输出转存。",
                                  "calls": []
                                }
                              ]
                            },
                            {
                              "id": "Exec._exec_reads_batch",
                              "name": "_exec_reads_batch()",
                              "module": "agent.py:785",
                              "desc": "连续只读调用可以并行；等齐结果再进入后续步骤。",
                              "calls": [
                                {
                                  "id": "Exec._exec_parallel_batch",
                                  "name": "_exec_parallel_batch()",
                                  "module": "agent.py:793",
                                  "desc": "串行审批，线程池并发执行，按完成顺序记录。",
                                  "calls": []
                                }
                              ]
                            }
                          ]
                        },
                        {
                          "id": "Loop._fill_interrupted_tool_results",
                          "name": "_fill_interrupted_tool_results()",
                          "module": "agent.py:503",
                          "desc": "仅停止分支：补齐未完成工具调用的结果。",
                          "calls": []
                        }
                      ]
                    },
                    {
                      "id": "Agent._persist_context_size",
                      "name": "_persist_context_size()",
                      "module": "agent.py:319",
                      "desc": "把当前上下文用量保存到会话元信息。",
                      "calls": []
                    }
                  ]
                }
              ]
            },
            {
              "id": "Agent._pre_turn",
              "name": "_pre_turn()",
              "sig": "<span class=\"sig-name\">Agent._pre_turn(self) → Iterator[Event]</span>\nsrc/mecode/agent.py:366",
              "desc": "重置中断状态，并准备工作上下文。",
              "detail": "清除上轮中断标记；检查是否需要压缩；注入待补充的续接提醒。新用户消息在准备之后记录。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 366,
                "symbol": "Agent._pre_turn"
              }
            },
            {
              "id": "Agent._run_loop_guarded",
              "name": "_run_loop_guarded()",
              "sig": "<span class=\"sig-name\">Agent._run_loop_guarded(self) → Iterator[Event]</span>\nsrc/mecode/agent.py:421",
              "desc": "包住主循环，处理停止时的上下文修复。",
              "detail": "KeyboardInterrupt / _TurnInterrupted：补齐缺失工具结果或追加中断标记，并发 Notice。GeneratorExit：修复后重新抛出，不再 yield。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 421,
                "symbol": "Agent._run_loop_guarded"
              }
            },
            {
              "id": "Agent._persist_context_size",
              "name": "_persist_context_size()",
              "sig": "<span class=\"sig-name\">Agent._persist_context_size(self) → None</span>\nsrc/mecode/agent.py:319",
              "desc": "把当前上下文用量保存到会话元信息。",
              "detail": "位于 run_turn 的收尾路径；不能将它理解为所有异常出口都会无条件执行的 finally。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 319,
                "symbol": "Agent._persist_context_size"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Context",
      "label": "Agent · 请求前准备",
      "type": "class",
      "x": 350,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "这些方法仍属于 Agent",
          "attrs": [
            {
              "id": "Context._maybe_compact",
              "name": "_maybe_compact()",
              "sig": "<span class=\"sig-name\">Agent._maybe_compact(self, resume_hint: str=&#x27;&#x27;) → Iterator[Event]</span>\nsrc/mecode/agent.py:325",
              "desc": "轮开始和后续工具迭代之间检查压缩阈值。",
              "detail": "利用上下文用量或估算判断是否压缩；调用 compact，成功才替换 self.messages。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 325,
                "symbol": "Agent._maybe_compact"
              }
            },
            {
              "id": "Context._inject_bg_events",
              "name": "_inject_bg_events()",
              "sig": "<span class=\"sig-name\">Agent._inject_bg_events(self) → None</span>\nsrc/mecode/agent.py:445",
              "desc": "在模型请求前注入后台事件与排队消息。",
              "detail": "依次处理完成、到期 check-in、被杀通知和排队用户输入。都经过 _record；后台事件采用 user 身份的提醒。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 445,
                "symbol": "Agent._inject_bg_events"
              }
            },
            {
              "id": "Context._inject_mode_reminder",
              "name": "_inject_mode_reminder()",
              "sig": "<span class=\"sig-name\">Agent._inject_mode_reminder(self) → None</span>\nsrc/mecode/agent.py:390",
              "desc": "比较当前模式与上下文最近的模式记录。",
              "detail": "set_mode 更新内存中的模式；此处只在历史记录需要补充时追加提醒，减少重复上下文。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 390,
                "symbol": "Agent._inject_mode_reminder"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Loop",
      "label": "Agent · 模型与工具循环",
      "type": "class",
      "x": 350,
      "y": 360,
      "w": 280,
      "sections": [
        {
          "title": "src/mecode/agent.py",
          "attrs": [
            {
              "id": "Loop._turn_loop",
              "name": "_turn_loop()",
              "sig": "<span class=\"sig-name\">Agent._turn_loop(self) → Iterator[Event]</span>\nsrc/mecode/agent.py:553",
              "desc": "请求模型，保存 assistant，执行工具，然后继续。",
              "detail": "每轮有次数上限；空响应只有限次重试。正文碎片累积后记入上下文。没有工具调用就结束；工具执行完才开启下次请求。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 553,
                "symbol": "Agent._turn_loop"
              },
              "children": [
                {
                  "id": "Loop._assistant_msg",
                  "name": "_assistant_msg()",
                  "sig": "<span class=\"sig-name\">Agent._assistant_msg(text: str, tool_calls: list[ToolCall], reasoning: str=&#x27;&#x27;, usage: tuple[int, int]=(0, 0)) → dict</span>\nsrc/mecode/agent.py:874",
                  "desc": "构造完整的 assistant 消息。",
                  "detail": "包含累积正文、完整 tool_calls、reasoning 与用量信息。流中断时只保存已出现的正文，不提交未执行的工具调用。",
                  "source": {
                    "path": "src/mecode/agent.py",
                    "line": 874,
                    "symbol": "Agent._assistant_msg"
                  }
                },
                {
                  "id": "Loop._tool_order_error",
                  "name": "_tool_order_error()",
                  "sig": "<span class=\"sig-name\">_tool_order_error(tool_calls: list[ToolCall]) → str | None</span>\nsrc/mecode/agent.py:70",
                  "desc": "执行前检查整批调用的顺序是否合法。",
                  "detail": "前台 subagent 只能出现在批次末尾；发现违规则给整批写入错误结果，避免前面的写操作先发生。",
                  "source": {
                    "path": "src/mecode/agent.py",
                    "line": 70,
                    "symbol": "_tool_order_error"
                  }
                }
              ]
            },
            {
              "id": "Loop._record",
              "name": "_record()",
              "sig": "<span class=\"sig-name\">Agent._record(self, msg: dict) → None</span>\nsrc/mecode/agent.py:209",
              "desc": "每条真实消息同时更新内存与存档。",
              "detail": "模型看到 self.messages；store 存在时另行 append_transcript。先记录工具结果再 yield UI 事件，防止界面中断造成孤儿调用。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 209,
                "symbol": "Agent._record"
              }
            },
            {
              "id": "Loop._fill_interrupted_tool_results",
              "name": "_fill_interrupted_tool_results()",
              "sig": "<span class=\"sig-name\">Agent._fill_interrupted_tool_results(self) → bool</span>\nsrc/mecode/agent.py:503",
              "desc": "停止时补齐已登记但尚无结果的工具调用。",
              "detail": "保证 assistant.tool_calls 的每个 id 都有对应 tool 消息；补的是中断说明，不是伪造执行成功。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 503,
                "symbol": "Agent._fill_interrupted_tool_results"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Provider",
      "label": "Provider · 统一模型事件流",
      "type": "class",
      "x": 670,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "同步接口 / 异步 HTTP",
          "attrs": [
            {
              "id": "Provider.stream",
              "name": "stream()",
              "sig": "<span class=\"sig-name\">Provider.stream(self, messages: list[dict], tools: list[dict] | None=None, should_stop: Callable[[], bool] | None=None) → Iterator[Event]</span>\nsrc/mecode/provider.py:314",
              "desc": "接收 messages 和 schemas，产出统一事件。",
              "detail": "Agent 累积 TextDelta / ReasoningDelta；ToolCall 收集完整后才执行；Usage 更新上下文大小。should_stop 回调传入 Event.is_set。",
              "source": {
                "path": "src/mecode/provider.py",
                "line": 314,
                "symbol": "Provider.stream"
              }
            },
            {
              "id": "Provider._for_wire",
              "name": "_for_wire()",
              "sig": "<span class=\"sig-name\">Provider._for_wire(self, messages: list[dict]) → list[dict]</span>\nsrc/mecode/provider.py:289",
              "desc": "按后端配置生成实际请求消息。",
              "detail": "私有元信息不会原样发给模型。模型无法读取未加入请求的内存队列或本地日志。",
              "source": {
                "path": "src/mecode/provider.py",
                "line": 289,
                "symbol": "Provider._for_wire"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Exec",
      "label": "Agent · 工具执行策略",
      "type": "class",
      "x": 670,
      "y": 360,
      "w": 280,
      "sections": [
        {
          "title": "审批 / 屏障 / 并发",
          "attrs": [
            {
              "id": "Exec._exec_tool",
              "name": "_exec_tool()",
              "sig": "<span class=\"sig-name\">Agent._exec_tool(self, tc: ToolCall) → Iterator[Event]</span>\nsrc/mecode/agent.py:737",
              "desc": "单工具：审批 → 执行 → 处理结果 → 记录。",
              "detail": "拒绝也生成配对 tool 结果；exit_plan 有专门分支。其余调用 tools.execute 后处理输出，先 _record 再 yield ToolResult。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 737,
                "symbol": "Agent._exec_tool"
              }
            },
            {
              "id": "Exec._exec_reads_batch",
              "name": "_exec_reads_batch()",
              "sig": "<span class=\"sig-name\">Agent._exec_reads_batch(self, reads: list[ToolCall]) → Iterator[Event]</span>\nsrc/mecode/agent.py:785",
              "desc": "连续只读工具组成并发批次。",
              "detail": "一个只读调用走单工具路径；多个调用交给 _exec_parallel_batch。非只读工具形成屏障，前一个只读批次全部结束后才能执行它。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 785,
                "symbol": "Agent._exec_reads_batch"
              }
            },
            {
              "id": "Exec._exec_parallel_batch",
              "name": "_exec_parallel_batch()",
              "sig": "<span class=\"sig-name\">Agent._exec_parallel_batch(self, tcs: list[ToolCall], deny_msg: str, cap: int | None=None) → Iterator[Event]</span>\nsrc/mecode/agent.py:793",
              "desc": "串行审批，线程池并发执行，按完成顺序记录。",
              "detail": "fut2tc 把 Future 映射回原 ToolCall；as_completed 只交出已完成的 Future；fut.result() 取返回值。结果通过 tool_call_id 配对，不重排为原调用顺序。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 793,
                "symbol": "Agent._exec_parallel_batch"
              },
              "callChain": [
                {
                  "id": "Exec._exec_parallel_batch",
                  "name": "_exec_parallel_batch()",
                  "module": "agent.py:793",
                  "desc": "串行审批，线程池并发执行，按完成顺序记录。",
                  "calls": [
                    {
                      "id": "Registry.execute",
                      "name": "execute()",
                      "module": "tools.py:118",
                      "desc": "每个获准调用提交为 Future；fut2tc 保留 Future → ToolCall 映射。",
                      "calls": []
                    },
                    {
                      "id": "Exec._process_result",
                      "name": "_process_result()",
                      "module": "agent.py:729",
                      "desc": "as_completed 给出已完成 Future，result() 取得文本，再执行输出处理。",
                      "calls": []
                    },
                    {
                      "id": "Loop._record",
                      "name": "_record()",
                      "module": "agent.py:209",
                      "desc": "使用原 ToolCall.id 记录 role=tool；记录顺序就是完成顺序。",
                      "calls": [
                        {
                          "id": "Session.append_transcript",
                          "name": "append_transcript()",
                          "module": "session.py:93",
                          "desc": "向 transcript.jsonl 追加消息。",
                          "calls": []
                        }
                      ]
                    }
                  ]
                }
              ]
            },
            {
              "id": "Exec._process_result",
              "name": "_process_result()",
              "sig": "<span class=\"sig-name\">Agent._process_result(self, name: str, full: str) → str</span>\nsrc/mecode/agent.py:729",
              "desc": "限制结果体积，必要时把完整输出转存。",
              "detail": "按工具配置选择限制；有会话存储时长输出可落盘并给模型路径。bash 侧重保留尾部；指定读取工具走截断策略。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 729,
                "symbol": "Agent._process_result"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Session",
      "label": "SessionStore · 保留事实",
      "type": "class",
      "x": 990,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "消息与长输出",
          "attrs": [
            {
              "id": "Session.append_transcript",
              "name": "append_transcript()",
              "sig": "<span class=\"sig-name\">SessionStore.append_transcript(self, entry: dict) → None</span>\nsrc/mecode/session.py:93",
              "desc": "向 transcript.jsonl 追加消息。",
              "detail": "内存用于本次请求，存档用于恢复与界面历史；两者通过 Agent._record 协同更新。",
              "source": {
                "path": "src/mecode/session.py",
                "line": 93,
                "symbol": "SessionStore.append_transcript"
              }
            },
            {
              "id": "Session.offload_tool_output",
              "name": "offload_tool_output()",
              "sig": "<span class=\"sig-name\">SessionStore.offload_tool_output(self, tool: str, content: str) → str</span>\nsrc/mecode/session.py:330",
              "desc": "存储完整工具输出并返回可读取路径。",
              "detail": "工具结果进入上下文的是摘要/片段和指针；完整输出留在会话目录里，避免直接占满模型上下文。",
              "source": {
                "path": "src/mecode/session.py",
                "line": 330,
                "symbol": "SessionStore.offload_tool_output"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Registry",
      "label": "ToolRegistry · 分发执行",
      "type": "class",
      "x": 990,
      "y": 360,
      "w": 280,
      "sections": [
        {
          "title": "模型调用实际落地",
          "attrs": [
            {
              "id": "Registry.schemas",
              "name": "schemas()",
              "sig": "<span class=\"sig-name\">ToolRegistry.schemas(self) → list[dict]</span>\nsrc/mecode/tools.py:91",
              "desc": "构造可供模型选择的工具 schema。",
              "detail": "Agent 在每次请求中传入最新 schemas，包含已经注册的内置和扩展工具。",
              "source": {
                "path": "src/mecode/tools.py",
                "line": 91,
                "symbol": "ToolRegistry.schemas"
              }
            },
            {
              "id": "Registry.execute",
              "name": "execute()",
              "sig": "<span class=\"sig-name\">ToolRegistry.execute(self, name: str, arguments: dict, slot: &#x27;ProcSlot | None&#x27;=None, bg: &#x27;BackgroundManager | None&#x27;=None) → str</span>\nsrc/mecode/tools.py:118",
              "desc": "按工具名称执行对应 handler。",
              "detail": "调用实际文件、进程、网络或子 Agent 操作；普通异常转为结果文本，由 Agent 决定后续记录与展示。",
              "source": {
                "path": "src/mecode/tools.py",
                "line": 118,
                "symbol": "ToolRegistry.execute"
              }
            }
          ]
        }
      ]
    }
  ],
  "CONNECTIONS": [
    [
      "Agent.run_turn",
      "Agent._pre_turn",
      "#a6e3a1",
      false,
      "①"
    ],
    [
      "Agent.run_turn",
      "Agent._run_loop_guarded",
      "#a6e3a1",
      false,
      "②"
    ],
    [
      "Agent.run_turn",
      "Agent._persist_context_size",
      "#a6e3a1",
      false,
      "③"
    ],
    [
      "Agent._pre_turn",
      "Context._maybe_compact",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Agent._run_loop_guarded",
      "Loop._turn_loop",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Agent._run_loop_guarded",
      "Loop._fill_interrupted_tool_results",
      "#6c7086",
      false,
      "中断时"
    ],
    [
      "Loop._turn_loop",
      "Context._inject_bg_events",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Loop._turn_loop",
      "Context._inject_mode_reminder",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Loop._turn_loop",
      "Provider.stream",
      "#a6e3a1",
      false,
      "每次迭代"
    ],
    [
      "Loop._turn_loop",
      "Registry.schemas",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Loop._turn_loop",
      "Loop._record",
      "#89b4fa",
      false,
      "完整 assistant"
    ],
    [
      "Loop._turn_loop",
      "Exec._exec_tool",
      "#a6e3a1",
      false,
      "非只读工具"
    ],
    [
      "Loop._turn_loop",
      "Exec._exec_reads_batch",
      "#a6e3a1",
      false,
      "只读组"
    ],
    [
      "Exec._exec_reads_batch",
      "Exec._exec_parallel_batch",
      "#a6e3a1",
      false,
      "多个调用"
    ],
    [
      "Exec._exec_tool",
      "Registry.execute",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Exec._exec_parallel_batch",
      "Registry.execute",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Exec._process_result",
      "Loop._record",
      "#89b4fa",
      false,
      "处理后的 tool 结果"
    ],
    [
      "Loop._record",
      "Session.append_transcript",
      "#89b4fa",
      false,
      "持久化"
    ],
    [
      "Exec._process_result",
      "Session.offload_tool_output",
      "#6c7086",
      false,
      "超长输出"
    ]
  ],
  "GROUPS": []
};

DIAGRAMS.ui_events = {
  "title": "界面事件流 · 输入、后台接续与停止",
  "sub": "03 / UI 事件 · 粉色虚线是通知/事件；后台完成可触发 run_turn(None)，主循环仍由同一个 Agent 执行",
  "navLabel": "03  UI 事件与停止",
  "navSub": "Desktop / TUI / 后台通知 / 中断",
  "NODES": [
    {
      "id": "Desk",
      "label": "Desk · 浏览器界面的后端",
      "type": "widget",
      "x": 30,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "scripts/deskserve.py",
          "attrs": [
            {
              "id": "Desk.send",
              "name": "send()",
              "sig": "<span class=\"sig-name\">Desk.send(self, text: str) → str</span>\nscripts/deskserve.py:426",
              "desc": "收到消息：空闲时启动，忙时排队。",
              "detail": "空闲时创建线程执行 _pump(text)；忙时调用 agent.queue_user(text)，并通知前端 queued。",
              "source": {
                "path": "scripts/deskserve.py",
                "line": 426,
                "symbol": "Desk.send"
              },
              "callChain": [
                {
                  "id": "Desk.send",
                  "name": "send()",
                  "module": "deskserve.py:426",
                  "desc": "收到消息：空闲时启动，忙时排队。",
                  "calls": [
                    {
                      "id": "Agent.queue_user",
                      "name": "queue_user()",
                      "module": "agent.py:260",
                      "desc": "忙时分支：先排队，模型下一次请求前再注入。",
                      "calls": []
                    },
                    {
                      "id": "Desk._pump",
                      "name": "_pump()",
                      "module": "deskserve.py:443",
                      "desc": "空闲分支：启动事件泵线程。",
                      "calls": [
                        {
                          "id": "Agent.run_turn",
                          "name": "run_turn()",
                          "module": "agent.py:290",
                          "desc": "界面的两种运行方式使用同一个生成器入口。",
                          "calls": []
                        },
                        {
                          "id": "Bus.emit",
                          "name": "emit()",
                          "module": "deskserve.py:228",
                          "desc": "将消息送给当前订阅的界面连接。",
                          "calls": []
                        },
                        {
                          "id": "Desk.maybe_bg_turn",
                          "name": "maybe_bg_turn()",
                          "module": "deskserve.py:470",
                          "desc": "finally：有剩余事件且空闲时启动 _pump(None)。",
                          "calls": []
                        }
                      ]
                    }
                  ]
                }
              ]
            },
            {
              "id": "Desk._pump",
              "name": "_pump()",
              "sig": "<span class=\"sig-name\">Desk._pump(self, text: str | None) → None</span>\nscripts/deskserve.py:443",
              "desc": "消费 Agent 事件，转换后通过 Bus 发布。",
              "detail": "run_turn(text) 统一处理新输入和 None 接续。finally 解除 busy、发 turn_end，再检查是否还有待处理消息。",
              "source": {
                "path": "scripts/deskserve.py",
                "line": 443,
                "symbol": "Desk._pump"
              }
            },
            {
              "id": "Desk.maybe_bg_turn",
              "name": "maybe_bg_turn()",
              "sig": "<span class=\"sig-name\">Desk.maybe_bg_turn(self) → None</span>\nscripts/deskserve.py:470",
              "desc": "待处理事件存在且空闲时自动接续。",
              "detail": "检查后台队列与用户队列；锁内确认空闲后启动 _pump(None)。当前正在运行时由当前循环取走队列内容。",
              "source": {
                "path": "scripts/deskserve.py",
                "line": 470,
                "symbol": "Desk.maybe_bg_turn"
              }
            },
            {
              "id": "Desk.interrupt",
              "name": "interrupt()",
              "sig": "<span class=\"sig-name\">Desk.interrupt(self) → None</span>\nscripts/deskserve.py:883",
              "desc": "把界面停止请求交给 Agent。",
              "detail": "调用 request_interrupt；实际停止效果由 Provider、进程槽和工具响应能力决定。",
              "source": {
                "path": "scripts/deskserve.py",
                "line": 883,
                "symbol": "Desk.interrupt"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "TUI",
      "label": "MecodeApp · 终端事件桥",
      "type": "widget",
      "x": 30,
      "y": 390,
      "w": 280,
      "sections": [
        {
          "title": "scripts/tui.py",
          "attrs": [
            {
              "id": "TUI._run_agent",
              "name": "_run_agent()",
              "sig": "<span class=\"sig-name\">MecodeApp._run_agent(self, user: str) → None</span>\nscripts/tui.py:3277",
              "desc": "工作线程迭代 run_turn(user)。",
              "detail": "每个事件包装成 AgentEvent，post_message 回 UI 线程；finally 无条件发送结束哨兵。",
              "source": {
                "path": "scripts/tui.py",
                "line": 3277,
                "symbol": "MecodeApp._run_agent"
              }
            },
            {
              "id": "TUI._run_bg_agent",
              "name": "_run_bg_agent()",
              "sig": "<span class=\"sig-name\">MecodeApp._run_bg_agent(self) → None</span>\nscripts/tui.py:3358",
              "desc": "工作线程迭代 run_turn()。",
              "detail": "不创建一条假的空用户消息；后台结果或排队消息由内核注入。",
              "source": {
                "path": "scripts/tui.py",
                "line": 3358,
                "symbol": "MecodeApp._run_bg_agent"
              }
            },
            {
              "id": "TUI._on_event",
              "name": "_on_event()",
              "sig": "<span class=\"sig-name\">MecodeApp._on_event(self, msg: AgentEvent) → None</span>\nscripts/tui.py:3722",
              "desc": "在 UI 线程更新正文、工具卡片和状态。",
              "detail": "处理模型和工具事件；结束哨兵让界面退出忙状态。",
              "source": {
                "path": "scripts/tui.py",
                "line": 3722,
                "symbol": "MecodeApp._on_event"
              }
            },
            {
              "id": "TUI._begin_interrupt",
              "name": "_begin_interrupt()",
              "sig": "<span class=\"sig-name\">MecodeApp._begin_interrupt(self) → None</span>\nscripts/tui.py:2825",
              "desc": "终端界面的停止入口。",
              "detail": "界面发起 request_interrupt 后显示停止状态；不直接强行杀 Python 工作线程。",
              "source": {
                "path": "scripts/tui.py",
                "line": 2825,
                "symbol": "MecodeApp._begin_interrupt"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Agent",
      "label": "Agent · 线程间协作入口",
      "type": "class",
      "x": 350,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "消息队列与 threading.Event",
          "attrs": [
            {
              "id": "Agent.run_turn",
              "name": "run_turn()",
              "sig": "<span class=\"sig-name\">Agent.run_turn(self, user_input: str | None=None) → Iterator[Event]</span>\nsrc/mecode/agent.py:290",
              "desc": "界面的两种运行方式使用同一个生成器入口。",
              "detail": "传入 str 处理新输入；传入 None 或不传参表示接续。没有待处理内容时，不发空模型请求。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 290,
                "symbol": "Agent.run_turn"
              }
            },
            {
              "id": "Agent.queue_user",
              "name": "queue_user()",
              "sig": "<span class=\"sig-name\">Agent.queue_user(self, msg: str) → None</span>\nsrc/mecode/agent.py:260",
              "desc": "把忙时发送的用户消息加入待处理队列。",
              "detail": "在锁内更新队列；这里还没有 _record。模型请求前由 _inject_bg_events 取走并写入上下文。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 260,
                "symbol": "Agent.queue_user"
              }
            },
            {
              "id": "Agent._inject_bg_events",
              "name": "_inject_bg_events()",
              "sig": "<span class=\"sig-name\">Agent._inject_bg_events(self) → None</span>\nsrc/mecode/agent.py:445",
              "desc": "请求模型前处理后台和排队消息。",
              "detail": "后台完成提醒采用 user 身份。取队列、写入 messages 和 transcript 后，Provider 才能在本次请求中看到内容。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 445,
                "symbol": "Agent._inject_bg_events"
              }
            },
            {
              "id": "Agent.request_interrupt",
              "name": "request_interrupt()",
              "sig": "<span class=\"sig-name\">Agent.request_interrupt(self) → None</span>\nsrc/mecode/agent.py:275",
              "desc": "设置停止信号并异步清理活动进程。",
              "detail": "Event 可跨线程读取；进程清理由 _kill_running_procs 完成，避免界面停止按钮被进程等待阻塞。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 275,
                "symbol": "Agent.request_interrupt"
              },
              "callChain": [
                {
                  "id": "Agent.request_interrupt",
                  "name": "request_interrupt()",
                  "module": "agent.py:275",
                  "desc": "设置停止信号并异步清理活动进程。",
                  "calls": [
                    {
                      "id": "Cancel.stream",
                      "name": "Provider.stream()",
                      "module": "provider.py:314",
                      "desc": "模型请求：检查信号并取消异步 I/O。",
                      "calls": []
                    },
                    {
                      "id": "Cancel.kill",
                      "name": "Agent._kill_running_procs()",
                      "module": "agent.py:284",
                      "desc": "前台进程：清理本轮 Shell 与子 Agent。",
                      "calls": []
                    },
                    {
                      "id": "Cancel.guarded",
                      "name": "Agent._run_loop_guarded()",
                      "module": "agent.py:421",
                      "desc": "循环边界：停止继续工作并修复消息。",
                      "calls": []
                    }
                  ]
                }
              ]
            }
          ]
        }
      ]
    },
    {
      "id": "Background",
      "label": "BackgroundManager · 任务完成",
      "type": "class",
      "x": 350,
      "y": 390,
      "w": 280,
      "sections": [
        {
          "title": "独立任务 / 完成队列",
          "attrs": [
            {
              "id": "Background._finish",
              "name": "_finish()",
              "sig": "<span class=\"sig-name\">BackgroundManager._finish(self, task: BackgroundTask) → None</span>\nsrc/mecode/background.py:210",
              "desc": "把完成任务加入队列并触发回调。",
              "detail": "界面回调先更新展示，空闲时发起自动接续；模型上下文仍由 Agent 显式注入。",
              "source": {
                "path": "src/mecode/background.py",
                "line": 210,
                "symbol": "BackgroundManager._finish"
              }
            },
            {
              "id": "Background.drain_completions",
              "name": "drain_completions()",
              "sig": "<span class=\"sig-name\">BackgroundManager.drain_completions(self) → list[BackgroundTask]</span>\nsrc/mecode/background.py:265",
              "desc": "让 Agent 取出尚未处理的完成任务。",
              "detail": "队列转移在锁内完成；后续任务仍可继续入队。普通停止当前回合不等于杀掉所有独立后台任务。",
              "source": {
                "path": "src/mecode/background.py",
                "line": 265,
                "symbol": "BackgroundManager.drain_completions"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Bus",
      "label": "Bus · 桌面事件总线",
      "type": "class",
      "x": 670,
      "y": 60,
      "w": 280,
      "sections": [
        {
          "title": "scripts/deskserve.py",
          "attrs": [
            {
              "id": "Bus.emit",
              "name": "emit()",
              "sig": "<span class=\"sig-name\">Bus.emit(self, msg: dict) → None</span>\nscripts/deskserve.py:228",
              "desc": "将消息送给当前订阅的界面连接。",
              "detail": "由 HTTP SSE 输出给桌面前端；这些是展示事件，不会因此自动加入模型 messages。",
              "source": {
                "path": "scripts/deskserve.py",
                "line": 228,
                "symbol": "Bus.emit"
              }
            }
          ]
        }
      ]
    },
    {
      "id": "Cancel",
      "label": "取消路径 · 支持者各自响应",
      "type": "module",
      "x": 670,
      "y": 300,
      "w": 280,
      "sections": [
        {
          "title": "中断机制的三个落点",
          "attrs": [
            {
              "id": "Cancel.stream",
              "name": "Provider.stream()",
              "sig": "<span class=\"sig-name\">Provider.stream(self, messages: list[dict], tools: list[dict] | None=None, should_stop: Callable[[], bool] | None=None) → Iterator[Event]</span>\nsrc/mecode/provider.py:314",
              "desc": "模型请求：检查信号并取消异步 I/O。",
              "detail": "同步侧轮询事件队列并检查 should_stop；finally 关闭 worker，等待网络资源清理。",
              "source": {
                "path": "src/mecode/provider.py",
                "line": 314,
                "symbol": "Provider.stream"
              }
            },
            {
              "id": "Cancel.kill",
              "name": "Agent._kill_running_procs()",
              "sig": "<span class=\"sig-name\">Agent._kill_running_procs(self) → None</span>\nsrc/mecode/agent.py:284",
              "desc": "前台进程：清理本轮 Shell 与子 Agent。",
              "detail": "调用当前 ProcSlot.kill() 与 SubagentRunner.kill_active()；进程树终止和 Python 线程取消是不同机制。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 284,
                "symbol": "Agent._kill_running_procs"
              }
            },
            {
              "id": "Cancel.guarded",
              "name": "Agent._run_loop_guarded()",
              "sig": "<span class=\"sig-name\">Agent._run_loop_guarded(self) → Iterator[Event]</span>\nsrc/mecode/agent.py:421",
              "desc": "循环边界：停止继续工作并修复消息。",
              "detail": "支持协作取消的路径能尽快退出；正在执行的普通同步网络工具或线程任务可能仍需等待返回。",
              "source": {
                "path": "src/mecode/agent.py",
                "line": 421,
                "symbol": "Agent._run_loop_guarded"
              }
            }
          ]
        }
      ]
    }
  ],
  "CONNECTIONS": [
    [
      "Desk.send",
      "Desk._pump",
      "#a6e3a1",
      false,
      "空闲"
    ],
    [
      "Desk.send",
      "Agent.queue_user",
      "#a6e3a1",
      false,
      "忙时"
    ],
    [
      "Desk._pump",
      "Agent.run_turn",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Desk._pump",
      "Bus.emit",
      "#f5c2e7",
      true,
      "encode(Event)"
    ],
    [
      "Desk._pump",
      "Desk.maybe_bg_turn",
      "#a6e3a1",
      false,
      "finally"
    ],
    [
      "Desk.maybe_bg_turn",
      "Desk._pump",
      "#a6e3a1",
      false,
      "None 接续"
    ],
    [
      "TUI._run_agent",
      "Agent.run_turn",
      "#a6e3a1",
      false,
      "user"
    ],
    [
      "TUI._run_bg_agent",
      "Agent.run_turn",
      "#a6e3a1",
      false,
      "None"
    ],
    [
      "TUI._run_agent",
      "TUI._on_event",
      "#f5c2e7",
      true,
      "AgentEvent"
    ],
    [
      "Background._finish",
      "Desk.maybe_bg_turn",
      "#f5c2e7",
      true,
      "经 _on_bg_done"
    ],
    [
      "Background._finish",
      "TUI._run_bg_agent",
      "#f5c2e7",
      true,
      "经 BgComplete / 空闲检查"
    ],
    [
      "Agent._inject_bg_events",
      "Background.drain_completions",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Desk.interrupt",
      "Agent.request_interrupt",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "TUI._begin_interrupt",
      "Agent.request_interrupt",
      "#a6e3a1",
      false,
      ""
    ],
    [
      "Agent.request_interrupt",
      "Cancel.stream",
      "#f5c2e7",
      true,
      "should_stop"
    ],
    [
      "Agent.request_interrupt",
      "Cancel.kill",
      "#a6e3a1",
      false,
      "清理线程"
    ],
    [
      "Agent.request_interrupt",
      "Cancel.guarded",
      "#f5c2e7",
      true,
      "Event / 异常边界"
    ]
  ],
  "GROUPS": []
};
