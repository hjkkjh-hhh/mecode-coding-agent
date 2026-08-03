"""mecode 为证券多专家辩论项目(systemic_risk)做的专属适配包。

serve.py 是通用 OpenAI 兼容网关的门面;辩论专属的部分收拢在此:
- debate_tools:主持人网关的 dispatch_speakers(结构化调度)与 manage_disputes(分歧账本)工具
- debate_meta:场次/全场序/调用类型推断/全场账本(第D场.jsonl)/请求触发段截取
- lean_system:辩论专属精简 system(替换默认编程助手模板)

可变全局(功能开关/--shared-dir/存活会话表)留在门面 serve.py——测试与运维只面向门面,
本包各模块无可变配置,共享目录等由门面按调用传入。
"""
