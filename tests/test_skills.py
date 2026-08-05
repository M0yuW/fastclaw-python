from __future__ import annotations

from pathlib import Path

import pytest

from fastclaw.execution import ExecutionContext
from fastclaw.skills import SkillCatalog, SkillNotPreparedError
from fastclaw.tools import SkillScriptTool


def context() -> ExecutionContext:
    return ExecutionContext(
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
        root_execution_id="root-1",
    )


def make_skill(root: Path, *, requirements: str | None = None) -> Path:
    skill = root / "skills" / "findata-toolkit"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        """---
name: findata-toolkit-us
description: Fixture financial data Skill
env:
  - name: ODDS_API_KEY
    required: false
---

Run scripts below /old/.fastclaw and never install at runtime.
""",
        encoding="utf-8",
    )
    (skill / "scripts" / "run.py").write_text(
        (
            "import os, sys\n"
            "print(sys.argv[1], bool(os.environ.get('ODDS_API_KEY')), "
            "bool(os.environ.get('UNDECLARED_SECRET')), os.environ['HOME'])\n"
        ),
        encoding="utf-8",
    )
    if requirements is not None:
        (skill / "requirements.txt").write_text(requirements, encoding="utf-8")
    return skill


def test_skill_is_indexed_by_frontmatter_name_and_prompt_paths_are_mapped(
    tmp_path: Path,
) -> None:
    make_skill(tmp_path)
    catalog = SkillCatalog(tmp_path / "skills")

    skills = catalog.discover()
    selected = catalog.require("findata-toolkit-us")
    prompt = catalog.prompt(
        selected,
        source_root=Path("/old/.fastclaw"),
        target_root=Path("/new/.fastclaw-python"),
    )

    assert [skill.name for skill in skills] == ["findata-toolkit-us"]
    assert selected.root.name == "findata-toolkit"
    assert "/old/.fastclaw" not in prompt
    assert "/new/.fastclaw-python" in prompt
    with pytest.raises(SkillNotPreparedError):
        catalog.interpreter(selected)


async def test_prepared_skill_exec_rejects_inline_code_and_injects_only_declared_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_skill(tmp_path)
    monkeypatch.setenv("ODDS_API_KEY", "fixture-secret")
    monkeypatch.setenv("UNDECLARED_SECRET", "must-not-pass")
    catalog = SkillCatalog(tmp_path / "skills")
    skill = catalog.discover()[0]
    await catalog.prepare(skill)
    tool = SkillScriptTool(catalog, (skill,), forbidden_roots=(Path("/old/.fastclaw"),))

    result = await tool.execute(
        {"argv": ["python3", "scripts/run.py", "ok"]},
        context(),
    )

    assert result.content.strip() == f"ok True False {tmp_path}"
    assert catalog.is_prepared(skill)
    with pytest.raises(ValueError, match="inline"):
        await tool.execute({"argv": ["python3", "-c", "print('no')"]}, context())
    with pytest.raises(ValueError, match="scripts directory"):
        await tool.execute({"argv": ["python3", str(Path(__file__))]}, context())
    with pytest.raises(ValueError, match="legacy"):
        await tool.execute(
            {"argv": ["python3", "scripts/run.py", "/old/.fastclaw/private"]},
            context(),
        )


async def test_requirements_change_selects_a_new_unprepared_environment(tmp_path: Path) -> None:
    skill_root = make_skill(tmp_path)
    catalog = SkillCatalog(tmp_path / "skills")
    original = catalog.discover()[0]
    await catalog.prepare(original)
    (skill_root / "requirements.txt").write_text("example==1\n", encoding="utf-8")

    changed = catalog.discover()[0]

    assert changed.requirements_hash != original.requirements_hash
    assert catalog.is_prepared(original)
    assert not catalog.is_prepared(changed)
