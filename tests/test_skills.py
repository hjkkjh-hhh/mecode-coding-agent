"""Skill 系统：发现（三级目录/同名覆盖）、frontmatter 解析、索引生成（封顶/空省略）、启停、正文注入。"""
from pathlib import Path

import mecode.skills as sk


def _mk_skill(root: Path, name: str, desc: str = "描述", body: str = "正文流程") -> Path:
    d = root / name
    d.mkdir(parents=True)
    f = d / "SKILL.md"
    f.write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8")
    return f


def _isolate(tmp_path, monkeypatch):
    """把用户级目录/状态文件/内置目录都指到临时处，隔离真实环境。"""
    monkeypatch.setattr(sk, "USER_SKILLS_DIR", tmp_path / "user_skills")
    monkeypatch.setattr(sk, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(sk, "BUILTIN_DIR", tmp_path / "builtin")


def test_split_frontmatter():
    meta, body = sk._split_frontmatter("---\nname: a\ndescription: b\n---\n\n正文")
    assert meta == {"name": "a", "description": "b"} and body.strip() == "正文"
    meta2, body2 = sk._split_frontmatter("没有头的全文")
    assert meta2 == {} and body2 == "没有头的全文"


def test_discover_三级目录_项目级覆盖用户级(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    _mk_skill(proj / ".mecode" / "skills", "commit", desc="项目版")
    _mk_skill(tmp_path / "user_skills", "commit", desc="用户版")     # 同名 → 被项目级盖
    _mk_skill(tmp_path / "user_skills", "review", desc="审查")
    _mk_skill(tmp_path / "builtin", "mcp-install", desc="内置")
    skills = sk.discover_skills(proj)
    got = {s.name: (s.description, s.source) for s in skills}
    assert got["commit"] == ("项目版", "项目")                        # 项目级赢
    assert got["review"] == ("审查", "用户") and got["mcp-install"] == ("内置", "内置")


def test_discover_缺description跳过_缺name用文件夹名(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    root = tmp_path / "user_skills"
    (root / "nodesc").mkdir(parents=True)
    (root / "nodesc" / "SKILL.md").write_text("---\nname: nodesc\n---\n正文", encoding="utf-8")
    (root / "noname").mkdir()
    (root / "noname" / "SKILL.md").write_text("---\ndescription: 有描述\n---\n正文", encoding="utf-8")
    skills = sk.discover_skills(None)
    names = [s.name for s in skills]
    assert "nodesc" not in names          # 没描述 → 索引里是噪音，跳过
    assert "noname" in names              # 没 name → 文件夹名兜底


def test_启停_写状态文件_停用不进索引(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _mk_skill(tmp_path / "user_skills", "commit", desc="提交")
    _mk_skill(tmp_path / "user_skills", "review", desc="审查")
    sk.set_enabled("review", False)
    skills = sk.discover_skills(None)
    assert {s.name: s.enabled for s in skills} == {"commit": True, "review": False}
    prompt = sk.build_skills_prompt(skills)
    assert "commit" in prompt and "review" not in prompt   # 停用的不进索引
    sk.set_enabled("review", True)                         # 重新启用
    assert all(s.enabled for s in sk.discover_skills(None))


def test_项目级禁用_不动用户全局(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _mk_skill(tmp_path / "builtin", "mcp-install", desc="内置")
    _mk_skill(tmp_path / "builtin", "skill-install", desc="内置")
    proj = tmp_path / "proj"
    (proj / ".mecode").mkdir(parents=True)
    # 故意带 BOM 写(Windows 的 Out-File/记事本常这么干),锁定读取侧的容错
    (proj / ".mecode" / "skills_state.json").write_bytes(
        b'\xef\xbb\xbf{"disabled": ["mcp-install", "skill-install"]}')
    enabled_in_proj = {s.name: s.enabled for s in sk.discover_skills(proj)}
    assert enabled_in_proj == {"mcp-install": False, "skill-install": False}   # 项目内被屏蔽
    assert all(s.enabled for s in sk.discover_skills(None))                    # 用户全局不受影响


def test_索引段_空则省略_含路径和指引(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert sk.build_skills_prompt([]) == ""                # 没 skill → 整段省略
    f = _mk_skill(tmp_path / "user_skills", "commit", desc="生成规范提交。用户要提交代码时使用。")
    prompt = sk.build_skills_prompt(sk.discover_skills(None))
    assert "# 可用技能" in prompt and "read_file" in prompt and "/skill" in prompt
    assert f.as_posix() in prompt                          # 全文路径在（模型 read_file 用）


def test_索引段_超预算截断_提示模型自己glob目录(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for i in range(60):
        _mk_skill(tmp_path / "user_skills", f"s{i:02d}", desc="很长的描述" * 20)
    prompt = sk.build_skills_prompt(sk.discover_skills(None))
    assert len(prompt) < sk.INDEX_BUDGET + 600             # 总预算（累计）+ 头部/截断提示余量
    # 截断提示要对【模型】可执行：给技能目录路径让它自己 glob，而非让它去按 /skill（那是人的入口）
    assert "未列出" in prompt and "glob" in prompt and "SKILL.md" in prompt


def test_body和reminder_取正文不含frontmatter(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _mk_skill(tmp_path / "user_skills", "commit", body="第一步 git status\n第二步 提交")
    s = sk.discover_skills(None)[0]
    assert s.body() == "第一步 git status\n第二步 提交"     # 不含 --- 头
    rem = sk.skill_reminder(s)
    assert "commit" in rem and "git status" in rem          # 注入文案带名字+全文


def test_validate_skill_与发现逻辑同源(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ok = _mk_skill(tmp_path / "user_skills", "good", desc="合法描述")
    assert sk.validate_skill(ok.parent) == "ok"
    # 校验通过的一定能被发现（同源判定）
    assert "good" in [s.name for s in sk.discover_skills(None)]
    # 各种不合法
    assert "不是文件夹" in sk.validate_skill(tmp_path / "不存在")
    empty = tmp_path / "empty"; empty.mkdir()
    assert "缺 SKILL.md" in sk.validate_skill(empty)
    nohead = tmp_path / "nohead"; nohead.mkdir()
    (nohead / "SKILL.md").write_text("没有头", encoding="utf-8")
    assert "缺 YAML frontmatter" in sk.validate_skill(nohead)
    nodesc = tmp_path / "nodesc"; nodesc.mkdir()
    (nodesc / "SKILL.md").write_text("---\nname: x\n---\n正文", encoding="utf-8")
    assert "缺 description" in sk.validate_skill(nodesc)
    nobody = tmp_path / "nobody"; nobody.mkdir()
    (nobody / "SKILL.md").write_text("---\nname: x\ndescription: d\n---\n\n", encoding="utf-8")
    assert "正文为空" in sk.validate_skill(nobody)
    longdesc = tmp_path / "longdesc"; longdesc.mkdir()
    (longdesc / "SKILL.md").write_text(f"---\nname: x\ndescription: {'很长'*101}\n---\n正文", encoding="utf-8")
    assert "超 200 字符" in sk.validate_skill(longdesc)


def test_内置skill_install_本身合法():
    assert sk.validate_skill(sk.BUILTIN_DIR / "skill-install") == "ok"
    assert sk.validate_skill(sk.BUILTIN_DIR / "mcp-install") == "ok"


def test_frontmatter_yaml多行块_竖线和折叠():
    text = ("---\nname: x\ndescription: |\n  第一行描述，\n  第二行继续。\n"
            "  当用户提到某某时使用。\n---\n\n正文")
    meta, body = sk._split_frontmatter(text)
    assert meta["name"] == "x"
    assert meta["description"] == "第一行描述， 第二行继续。 当用户提到某某时使用。"   # 缩进行拼成一行
    assert body.strip() == "正文"
    # > 折叠块同样认
    meta2, _ = sk._split_frontmatter("---\ndescription: >\n  a\n  b\n---\n正文")
    assert meta2["description"] == "a b"
