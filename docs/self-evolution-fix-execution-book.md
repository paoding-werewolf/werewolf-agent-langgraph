# 自进化模块修复执行手册

> **目标**: 修复 werewolf-agent-langgraph 自进化模块中已识别的全部 Bug、功能缺失与设计偏离
> **仓库根**: `werewolf-agent-langgraph/`（下文中所有路径均相对于此目录）
> **前置依赖**: 无跨模块依赖，各 TASK 独立可执行（除 TASK-6 依赖 TASK-5）
> **执行顺序建议**: TASK-1 → TASK-2 → TASK-3 → TASK-4 → TASK-5 → TASK-6 → TASK-7 → TASK-8 → TASK-9 → TASK-10
> **验证方式**: 每个 TASK 末尾给出具体验证命令，执行 Agent 必须逐项通过

---

## TASK-1：修复 `config.py` 的 `load_config()` 致命 Bug

### 1.1 问题描述

`load_config()` 第 76 行调用 `_merge_dataclass(cfg, dp)` 会把 `cfg.reflection`、`cfg.buffer` 等 dataclass 实例覆盖成裸 dict。后续任何 `cfg.reflection.causal_analysis_enabled` 访问都会抛 `AttributeError`。

同时，`confirmation.fast_track.*` 配置项永远不会从 YAML 加载，只合并了 `confirmation.normal.*`。

### 1.2 修改文件

`app/evolution/config.py`

### 1.3 精确修改

**修改 `load_config()` 函数**（当前第 68–88 行），替换为：

```python
def load_config() -> EvolutionConfig:
    """从 YAML 文件加载配置，不存在则返回默认值。"""
    config_path = AGENT_HOME / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        dp = raw.get("debounced_policy", {})
        cfg = EvolutionConfig()

        # ── 逐子模块合并，不要把整个 dp 灌进 cfg ──
        _merge_dataclass(cfg.reflection, dp.get("reflection", {}))
        _merge_dataclass(cfg.buffer, dp.get("buffer", {}))

        # confirmation 有两个子分组：normal 和 fast_track
        normal_cfg = dp.get("confirmation", {}).get("normal", {})
        fast_cfg = dp.get("confirmation", {}).get("fast_track", {})
        _merge_dataclass(cfg.confirmation, normal_cfg, prefix="normal_")
        _merge_dataclass(cfg.confirmation, fast_cfg, prefix="fast_track_")

        _merge_dataclass(cfg.versioning, dp.get("versioning", {}))
        _merge_dataclass(cfg.curator, dp.get("curator", {}))

        # top-level 标量字段
        for k in ("enabled", "clustering_model", "reflection_model",
                  "in_game_flag_causal_multiplier", "skills_path"):
            if k in dp:
                setattr(cfg, k, dp[k])
        return cfg
    return EvolutionConfig()
```

**修改 `_merge_dataclass()` 函数**（当前第 91–95 行），替换为：

```python
def _merge_dataclass(obj, overrides: dict, prefix: str = ""):
    """将 dict 中的键值对覆盖到 dataclass 实例上。
    
    prefix 不为空时，会将 overrides 中的 key 加上 prefix 前缀后
    再在 obj 上查找属性。用于 ConfirmationConfig 这种把 normal/fast_track
    分组的场景。
    """
    for k, v in overrides.items():
        target_key = f"{prefix}{k}" if prefix else k
        if hasattr(obj, target_key):
            setattr(obj, target_key, v)
```

### 1.4 关键细节

- **删除** 原来的第 76 行 `_merge_dataclass(cfg, dp)`
- `ConfirmationConfig` 的字段名是 `normal_min_count`、`fast_track_min_causal_strength` 等带前缀的命名，而 YAML 中的结构是 `confirmation.normal.min_count`、`confirmation.fast_track.min_causal_strength`。所以合并 `normal` 组时需要加 `normal_` 前缀，合并 `fast_track` 组时需要加 `fast_track_` 前缀。
- 如果 YAML 中 `confirmation.normal` 下有 `min_count: 5`，合并时会查找 `cfg.confirmation.normal_min_count` 并覆盖。

### 1.5 验证

```bash
cd werewolf-agent-langgraph

# 验证 1: 确认 dataclass 实例不被覆盖
python3 -c "
from app.evolution.config import load_config, EvolutionConfig
cfg = load_config()
assert isinstance(cfg.reflection, type(EvolutionConfig().reflection)), 'reflection 被覆盖成 dict 了'
assert isinstance(cfg.buffer, type(EvolutionConfig().buffer)), 'buffer 被覆盖成 dict 了'
assert isinstance(cfg.confirmation, type(EvolutionConfig().confirmation)), 'confirmation 被覆盖成 dict 了'
print('PASS: all sub-configs are dataclass instances')
"

# 验证 2: fast_track 配置能从 YAML 加载
mkdir -p ~/.werewolf-agent
cat > ~/.werewolf-agent/config.yaml << 'EOF'
debounced_policy:
  confirmation:
    fast_track:
      min_causal_strength: 0.95
      min_count: 3
EOF
python3 -c "
from app.evolution.config import load_config
cfg = load_config()
assert cfg.confirmation.fast_track_min_causal_strength == 0.95, f'Expected 0.95, got {cfg.confirmation.fast_track_min_causal_strength}'
assert cfg.confirmation.fast_track_min_count == 3, f'Expected 3, got {cfg.confirmation.fast_track_min_count}'
print('PASS: fast_track config loads from YAML')
"

# 验证 3: 缺省 YAML 字段回退到默认值
cat > ~/.werewolf-agent/config.yaml << 'EOF'
debounced_policy:
  confirmation:
    normal:
      min_count: 5
EOF
python3 -c "
from app.evolution.config import load_config
cfg = load_config()
assert cfg.confirmation.normal_min_count == 5
assert cfg.confirmation.normal_min_consistency_rate == 0.60  # 默认值
assert cfg.confirmation.fast_track_min_causal_strength == 0.80  # 默认值
print('PASS: missing fields fall back to defaults')
"

# 清理测试配置
rm -f ~/.werewolf-agent/config.yaml
```

---

## TASK-2：修复 `clustering.py` 中 `role_survived_rounds` 死代码

### 2.1 问题描述

`_scene_tag_overlap()` 的 `key_fields` 列表不包含 `role_survived_rounds`，但函数体内有 `elif field == "role_survived_rounds"` 分支，该分支永远不会执行。`role_survived_rounds` 是设计书中明确列出的场景隔离维度。

### 2.2 修改文件

`app/evolution/clustering.py`

### 2.3 精确修改

找到 `_scene_tag_overlap` 方法（当前第 108–128 行），将 `key_fields` 的定义从：

```python
key_fields = ["role", "critical_phase", "wolf_aggression", "result",
              "sheriff_contested", "first_night_target"]
```

改为：

```python
key_fields = ["role", "role_survived_rounds", "critical_phase",
              "wolf_aggression", "result", "sheriff_contested",
              "first_night_target"]
```

同时修正模糊匹配逻辑，当前代码中 `elif field == "role_survived_rounds"` 前面是 `if str(val_a) == str(val_b)`，即先尝试精确匹配。对于 `role_survived_rounds` 这个整数字段，需要先尝试精确匹配，失败后再做 ±1 模糊匹配。当前代码结构是正确的（先 `if` 精确，再 `elif` 模糊），只需要确保字段在 `key_fields` 中即可。

但有一个额外问题：`role_survived_rounds` 在 YAML 中存储为 `int`，但 `_scene_tag_overlap` 用 `str(val_a) == str(val_b)` 比较。如果一个是 `int(2)` 另一个是 `str("2")`，`str()` 转换后都是 `"2"`，可以精确匹配。这是 OK 的。

### 2.4 验证

```bash
python3 -c "
from app.evolution.clustering import SuggestionClusterer
from app.evolution.config import EvolutionConfig

clusterer = SuggestionClusterer(EvolutionConfig())

# 测试 1: role_survived_rounds 完全匹配
tags_a = {'role': 'seer', 'role_survived_rounds': 2, 'critical_phase': 'first_day_speech',
          'wolf_aggression': 'high', 'result': 'lost', 'sheriff_contested': True, 'first_night_target': 'villager'}
tags_b = {'role': 'seer', 'role_survived_rounds': 2, 'critical_phase': 'first_day_speech',
          'wolf_aggression': 'high', 'result': 'lost', 'sheriff_contested': True, 'first_night_target': 'villager'}
score = clusterer._scene_tag_overlap(tags_a, tags_b)
assert score == 1.0, f'Expected 1.0, got {score}'
print('PASS: full match = 1.0')

# 测试 2: role_survived_rounds 差 1（模糊匹配）
tags_c = dict(tags_b, role_survived_rounds=3)
score2 = clusterer._scene_tag_overlap(tags_a, tags_c)
expected = 6.5 / 7  # 6 个精确匹配 + 1 个 ±1 模糊匹配
assert abs(score2 - expected) < 0.01, f'Expected {expected}, got {score2}'
print(f'PASS: ±1 fuzzy match = {score2:.3f}')

# 测试 3: role_survived_rounds 差 3（不匹配）
tags_d = dict(tags_b, role_survived_rounds=5)
score3 = clusterer._scene_tag_overlap(tags_a, tags_d)
expected3 = 6.0 / 7
assert abs(score3 - expected3) < 0.01, f'Expected {expected3}, got {score3}'
print(f'PASS: distant value no match = {score3:.3f}')
"
```

---

## TASK-3：修复 `curator.py` 快照打包包含自身的膨胀问题

### 3.1 问题描述

`_snapshot()` 调用 `tarfile.add(self.skills_root)` 会把 `.curator_backups/` 目录也打包进去，导致快照文件随运行次数指数增长。

### 3.2 修改文件

`app/evolution/curator.py`

### 3.3 精确修改

找到 `_snapshot` 方法（当前第 179–192 行），将 `tar.add` 调用替换为带 `filter` 的版本：

```python
def _snapshot(self):
    """创建技能库快照。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_dir = self.backup_dir / ts
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    tar_path = snapshot_dir / "skills.tar.gz"
    backup_dir_name = self.backup_dir.name  # ".curator_backups"

    def _exclude_backups(tarinfo):
        """排除 .curator_backups 目录，避免快照嵌套膨胀。"""
        if backup_dir_name in tarinfo.name:
            return None
        return tarinfo

    with tarfile.open(str(tar_path), "w:gz") as tar:
        tar.add(str(self.skills_root), arcname="skills", filter=_exclude_backups)

    snapshots = sorted(self.backup_dir.iterdir())
    while len(snapshots) > 5:
        oldest = snapshots.pop(0)
        shutil.rmtree(oldest)
```

### 3.4 验证

```bash
python3 -c "
import tempfile, os
from pathlib import Path
from unittest.mock import MagicMock
from app.evolution.config import EvolutionConfig

# 创建临时目录模拟
with tempfile.TemporaryDirectory() as tmp:
    cfg = EvolutionConfig()
    cfg.skills_path = tmp + '/skills'
    
    # 模拟已有策略文件
    skill_dir = Path(cfg.skills_path) / 'seer' / 'identity-timing'
    skill_dir.mkdir(parents=True)
    (skill_dir / 'v1.md').write_text('# test')
    
    # 模拟已有快照
    backup_dir = Path(cfg.skills_path) / '.curator_backups' / 'old_snapshot'
    backup_dir.mkdir(parents=True)
    (backup_dir / 'old.tar.gz').write_bytes(b'fake snapshot data')
    
    from app.evolution.curator import Curator
    curator = Curator(cfg)
    curator._snapshot()
    
    # 验证新快照存在
    snapshots = list((Path(cfg.skills_path) / '.curator_backups').iterdir())
    assert len(snapshots) == 2, f'Expected 2 snapshots, got {len(snapshots)}'
    
    # 验证新快照不包含 .curator_backups
    import tarfile
    new_snap = sorted(snapshots)[-1] / 'skills.tar.gz'
    with tarfile.open(str(new_snap), 'r:gz') as tar:
        names = tar.getnames()
        for name in names:
            assert '.curator_backups' not in name, f'快照包含了 .curator_backups: {name}'
    print('PASS: snapshot excludes .curator_backups')
"
```

---

## TASK-4：Curator Phase 2 补全 `patch` 和 `consolidate` 执行逻辑

### 4.1 问题描述

`_phase2_llm_review()` 中 LLM 返回 `patch` 或 `consolidate` 时，代码什么也不做，甚至不计数。设计书要求：
- `patch`: 调用 `skill_manage(patch)` 修补策略文档
- `consolidate`: 找到重叠策略并生成合并版本

### 4.2 修改文件

`app/evolution/curator.py`

### 4.3 精确修改

**步骤 A**: 修改 `_phase2_llm_review()` 方法（当前第 93–139 行），在 LLM 判定后执行对应动作。替换整个方法为：

```python
def _phase2_llm_review(self) -> Dict:
    """LLM 审查：对每个非 pinned、非 bundled 策略做 keep/patch/consolidate/archive 判定。"""
    result = {"reviewed": 0, "kept": 0, "patched": 0, "consolidated": 0, "archived": 0}

    review_llm = LLMCaller()
    review_llm.model = self.cfg.clustering_model

    skills_reviewed = 0
    for skill_dir in self._iter_skill_dirs():
        if skills_reviewed >= self.cfg.curator.max_iterations:
            break

        meta_path = skill_dir / ".versions.json"
        if not meta_path.exists():
            continue

        with open(meta_path) as f:
            meta = json.load(f)

        current_v = meta.get("current_default", "v1")
        v_data = meta.get("versions", {}).get(current_v, {})

        if v_data.get("pinned") or v_data.get("source") == "bundled":
            continue

        v_file = skill_dir / f"{current_v}.md"
        if not v_file.exists():
            continue

        with open(v_file) as f:
            content = f.read()

        usage = v_data.get("usage", {})
        decision = self._llm_review_skill(content, usage, review_llm)

        result["reviewed"] += 1
        skills_reviewed += 1

        if decision == "keep":
            result["kept"] += 1
        elif decision == "patch":
            patched_content = self._llm_patch_skill(content, usage, review_llm)
            if patched_content:
                with open(v_file, "w") as f:
                    f.write(patched_content)
                result["patched"] += 1
            else:
                # patch 失败，保守处理为 keep
                result["kept"] += 1
        elif decision == "consolidate":
            consolidated = self._llm_consolidate_skill(
                skill_dir.name, content, review_llm
            )
            if consolidated:
                # 创建新的合并版本
                new_version = self.loader.create_new_version(
                    skill_name=meta.get("skill_name", skill_dir.name),
                    content=consolidated["content"],
                    source="curator_consolidation",
                    trigger_cluster=consolidated.get("merged_with", ""),
                )
                # 标记当前版本为 archived
                v_data["status"] = "archived"
                with open(meta_path, "w") as f:
                    json.dump(meta, f, indent=2, ensure_ascii=False)
                result["consolidated"] += 1
            else:
                result["kept"] += 1
        elif decision == "archive":
            v_data["status"] = "archived"
            result["archived"] += 1
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

    return result
```

**步骤 B**: 在 `Curator` 类中新增两个方法（添加在 `_llm_review_skill` 之后）：

```python
def _llm_patch_skill(self, content: str, usage: Dict, llm: LLMCaller) -> Optional[str]:
    """LLM 修补策略文档的瑕疵，返回修补后的完整文档。失败返回 None。"""
    prompt = f"""修补以下狼人杀策略文档中的瑕疵。

策略内容：
{content[:3000]}

使用数据：
- 对局数: {usage.get('games_played', 0)}
- 胜率: {usage.get('win_rate', 0):.2f}

要求：
1. 修正明显的逻辑矛盾或表述不清
2. 保留原有策略的核心思路
3. 保持原有的 Markdown + YAML frontmatter 格式
4. 输出修补后的完整文档"""

    try:
        resp = llm.client.chat.completions.create(
            model=llm.model,
            messages=[
                {"role": "system", "content": "你是狼人杀策略文档修补专家。输出完整的修补后文档。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        result = (resp.choices[0].message.content or "").strip()
        # 基本验证：修补后的文档不能太短
        if len(result) > 50 and "---" in result:
            return result
    except Exception:
        pass
    return None

def _llm_consolidate_skill(self, skill_name: str, content: str,
                            llm: LLMCaller) -> Optional[Dict]:
    """LLM 将当前策略与重叠策略合并。返回 {"content": str, "merged_with": str} 或 None。
    
    合并逻辑：
    1. 在同目录中找另一个 active/superseded 策略
    2. LLM 生成合并版本
    3. 被合并的旧版本由调用方标记为 archived
    """
    # 找到同角色目录下可能重叠的策略
    skill_dir = self.loader._find_skill_dir(skill_name)
    if not skill_dir:
        return None

    role_dir = skill_dir.parent
    sibling_skills = []
    for sibling in role_dir.iterdir():
        if sibling.is_dir() and sibling.name != skill_dir.name:
            sibling_meta_path = sibling / ".versions.json"
            if sibling_meta_path.exists():
                with open(sibling_meta_path) as f:
                    sibling_meta = json.load(f)
                sibling_v = sibling_meta.get("current_default", "v1")
                sibling_file = sibling / f"{sibling_v}.md"
                if sibling_file.exists():
                    sibling_content = sibling_file.read_text()
                    sibling_skills.append({
                        "name": sibling_meta.get("skill_name", sibling.name),
                        "content": sibling_content[:1500],
                    })

    if not sibling_skills:
        return None

    siblings_text = "\n\n".join(
        f"### 策略: {s['name']}\n{s['content']}" for s in sibling_skills[:3]
    )

    prompt = f"""判断以下策略是否与相邻策略有显著重叠，如果有则合并。

当前策略 ({skill_name})：
{content[:2000]}

相邻策略：
{siblings_text}

如果存在显著重叠（覆盖相似决策空间），输出合并后的完整策略文档（Markdown + YAML frontmatter）。
如果没有显著重叠，只回答 "no_merge"。"""

    try:
        resp = llm.client.chat.completions.create(
            model=llm.model,
            messages=[
                {"role": "system", "content": "你是策略库策展专家。判断重叠并合并。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        result = (resp.choices[0].message.content or "").strip()
        if "no_merge" in result.lower():
            return None
        if len(result) > 50 and "---" in result:
            return {
                "content": result,
                "merged_with": ", ".join(s["name"] for s in sibling_skills[:3]),
            }
    except Exception:
        pass
    return None
```

### 4.4 验证

```bash
# 验证 patch 分支
python3 -c "
from unittest.mock import MagicMock, patch
from app.evolution.curator import Curator
from app.evolution.config import EvolutionConfig

cfg = EvolutionConfig()
curator = Curator(cfg)

# Mock LLM 返回修补后的文档
mock_llm = MagicMock()
mock_resp = MagicMock()
mock_resp.choices = [MagicMock()]
mock_resp.choices[0].message.content = '---\nname: test\n---\n## Patched Strategy\nFixed content'
mock_llm.client.chat.completions.create.return_value = mock_resp
mock_llm.model = 'test-model'

result = curator._llm_patch_skill('original content', {'games_played': 5, 'win_rate': 0.4}, mock_llm)
assert result is not None, 'patch should return content'
assert 'Patched' in result, 'patched content should contain the LLM output'
print('PASS: _llm_patch_skill returns patched content')

# Mock LLM 返回 no_merge
mock_resp2 = MagicMock()
mock_resp2.choices = [MagicMock()]
mock_resp2.choices[0].message.content = 'no_merge'
mock_llm.client.chat.completions.create.return_value = mock_resp2

result2 = curator._llm_consolidate_skill('seer-identity-timing', 'content', mock_llm)
assert result2 is None, 'no_merge should return None'
print('PASS: _llm_consolidate_skill returns None for no_merge')
"
```

---

## TASK-5：接入版本竞争追踪（`get_version_for_game` + `record_version_usage`）

### 5.1 问题描述

版本竞争的两个核心函数从未被业务代码调用：
- `get_version_for_game(skill_name)` — 应在对局开始时决定使用哪个版本
- `record_version_usage(skill_name, version, won)` — 应在对局结束时记录胜负

没有这两个调用，新版本永远是 `candidate`，永远无法 `promote`。

### 5.2 修改文件

1. `app/agents/agent_graph.py` — `_build_extra_data()` 函数（当前第 157–189 行）
2. `app/main_ws.py` — `_run_post_game_pipeline()` 函数（当前第 287–388 行）
3. `app/agents/state.py` — `AgentState` TypedDict（当前第 4–34 行）

### 5.3 精确修改

**步骤 A**: 在 `AgentState` 中增加版本追踪字段。在 `strategies_used` 之后添加：

```python
    strategies_used: List[str]
    versions_used: Dict[str, str]      # {skill_name: version_name} 本局实际使用的版本
    in_game_flags: List[Dict[str, Any]]
```

在 `make_initial_state` 的返回 dict 中，`strategies_used` 之后添加：

```python
        "versions_used": {},
```

**步骤 B**: 修改 `app/agents/agent_graph.py` 的 `_build_extra_data()` 函数。

当前代码（第 178–187 行）：
```python
    try:
        from evolution.config import load_config
        from evolution.version_manager import VersionManager
        cfg = load_config()
        vm = VersionManager(cfg)
        extra["evolution_strategies"] = vm.format_skills_for_prompt(
            state["my_role"], phase
        )
    except Exception:
        pass
```

替换为：
```python
    try:
        from evolution.config import load_config
        from evolution.version_manager import VersionManager
        cfg = load_config()
        vm = VersionManager(cfg)
        extra["evolution_strategies"] = vm.format_skills_for_prompt(
            state["my_role"], phase
        )

        # 版本竞争：记录本局各策略实际使用了哪个版本
        versions_used = {}
        index = vm.loader.load_index()
        for skill in index:
            if skill.get("role") in (state["my_role"], "common"):
                skill_name = skill["name"]
                version = vm.get_version_for_game(skill_name)
                versions_used[skill_name] = version
        extra["versions_used"] = versions_used
    except Exception:
        pass
```

同时，在 `_build_extra_data` 返回 `extra` 后，调用方需要将 `versions_used` 写入 state。在 `agent_graph.py` 中找到所有调用 `_build_extra_data` 的地方，确认 `versions_used` 被写回 state。

最简单的做法是修改 `run_act()` 函数（当前第 570–598 行），在函数开头添加：

```python
    # 将版本选择结果写回 state（在 decider 运行前）
```

在 decider 运行后、return 之前，从 extra_data 中提取 versions_used 并合并到 state：

在 `run_act()` 的 `return {**state, "last_thought": thought}` 之前插入：

```python
    # 写回版本使用记录
    # 找到本局使用的 extra_data（可能有多个 decider，取最后一次的）
    # 这里通过重新获取来确保一致性
    try:
        ed = _build_extra_data(state, state.get("phase", ""))
        vu = ed.get("versions_used", {})
        if vu:
            state = {**state, "versions_used": vu}
    except Exception:
        pass
```

**重要**: 上面这段代码会额外调用一次 `_build_extra_data`，导致重复计算。更优雅的做法是让各 decider 返回 `extra_data`，但这需要改动多个 decider 函数。为了最小化修改范围，这里采用在 `run_act` 末尾重新获取的方式。**如果后续需要优化，可以在 decider 中把 `extra_data` 透传出来。**

**步骤 C**: 修改 `app/main_ws.py` 的 `_run_post_game_pipeline()` 函数。

在当前的 `# 11. Expire old suggestions`（约第 382 行）之前，添加版本使用记录：

```python
        # 10.5 Record version usage for version competition
        versions_used = state.get("versions_used", {})
        if versions_used:
            vm = VersionManager(cfg)
            won = (result == "won")
            for skill_name, version in versions_used.items():
                vm.record_usage(skill_name, version, won)
```

### 5.4 关键细节

- `get_version_for_game()` 内部有 `random.random()` 调用，每次调用结果可能不同。必须在对局开始时调用一次并记录到 state，确保整局使用同一版本。
- `record_version_usage()` 内部会调用 `_check_promotion()`，这是版本竞争的核心升级逻辑。
- `versions_used` 是 `Dict[str, str]`，key 是策略名，value 是版本号（如 `"v2"`）。

### 5.5 验证

```bash
# 验证 1: AgentState 包含 versions_used 字段
python3 -c "
from app.agents.state import AgentState, make_initial_state
state = make_initial_state('1_seer')
assert 'versions_used' in state, 'versions_used not in initial state'
assert isinstance(state['versions_used'], dict), 'versions_used should be dict'
print('PASS: versions_used in AgentState')
"

# 验证 2: 端到端版本追踪
# 需要 mock LLM，此处只验证代码路径不报错
python3 -c "
from app.evolution.version_manager import VersionManager
from app.evolution.config import EvolutionConfig
import tempfile, json
from pathlib import Path

cfg = EvolutionConfig()
cfg.skills_path = tempfile.mkdtemp()

# 创建一个测试策略
vm = VersionManager(cfg)
skill_dir = Path(cfg.skills_path) / 'seer' / 'identity-timing'
skill_dir.mkdir(parents=True)
(skill_dir / 'v1.md').write_text('# v1')

# 手动创建 .versions.json
meta = {
    'skill_name': 'seer-identity-timing',
    'current_default': 'v1',
    'versions': {
        'v1': {'created_at': '2026-01-01', 'source': 'bundled', 'pinned': False,
               'status': 'active', 'usage': {'games_played': 5, 'wins': 2, 'win_rate': 0.4}}
    }
}
with open(skill_dir / '.versions.json', 'w') as f:
    json.dump(meta, f)

# 创建 v2 作为 candidate
v2 = vm.create_new_version('seer-identity-timing', '# v2 improved')
assert v2 == 'v2', f'Expected v2, got {v2}'

# 模拟使用并记录
vm.record_usage('seer-identity-timing', 'v2', True)
vm.record_usage('seer-identity-timing', 'v2', True)

# 验证 v2 的 usage 被记录
updated_meta = vm.get_status('seer-identity-timing')
v2_usage = updated_meta['versions']['v2']['usage']
assert v2_usage['games_played'] == 2, f'Expected 2 games, got {v2_usage[\"games_played\"]}'
assert v2_usage['wins'] == 2, f'Expected 2 wins, got {v2_usage[\"wins\"]}'
print(f'PASS: version usage tracked correctly: {v2_usage}')
"
```

---

## TASK-6：接入对手画像更新（Layer 2）

### 6.1 问题描述

`_run_post_game_pipeline` 中导入了 `update_opponent_from_game` 但从未调用。对手画像（Layer 2）完全不工作。

### 6.2 修改文件

`app/main_ws.py` — `_run_post_game_pipeline()` 函数

### 6.3 精确修改

在 `_run_post_game_pipeline` 函数中，找到 `# 10. Update self model`（约第 374 行），在其后添加对手画像更新：

```python
        # 10. Update self model
        update_self_model(
            my_role=state["my_role"],
            result=result,
            key_decisions=state.get("last_thought", ""),
            llm_caller=llm,
        )

        # 10.1 Update opponent models (Layer 2)
        from memory.opponent_model import update_opponent_from_game
        all_roles = all_roles or {}  # game_over 消息中传入的 {player_id: role}
        my_seat = state.get("me_id", "")
        for player_id, player_role in all_roles.items():
            if player_id == my_seat:
                continue  # 跳过自己
            # 为每个对手生成行为摘要
            behavior_summary = _extract_player_behavior(state, player_id)
            if behavior_summary:
                update_opponent_from_game(
                    player_id=player_id,
                    role=player_role,
                    behavior_summary=behavior_summary,
                    llm_caller=llm,
                )
```

**步骤 B**: 在 `_run_post_game_pipeline` 之前（或同文件底部），新增辅助函数：

```python
def _extract_player_behavior(state: dict, player_id: str) -> str:
    """从对局 events 中提取某位玩家的行为摘要。"""
    events = state.get("events", [])
    behaviors = []
    for event in events:
        content = event.get("content", "")
        traces = event.get("traces", [])
        status = event.get("status", "")
        round_num = event.get("round", 1)

        # 从 traces 中提取该玩家的动作
        for t in traces:
            if t.get("from") == player_id or t.get("to") == player_id:
                action_desc = f"R{round_num} {status}: {t.get('from','')}->{t.get('to','')}({t.get('action','')})"
                behaviors.append(action_desc)

        # 提取该玩家的发言
        if status == "discussion" and player_id in content:
            behaviors.append(f"R{round_num} speech: {content[:100]}")

        # 提取投票
        if status in ("vote", "vote_result") and player_id in content:
            behaviors.append(f"R{round_num} vote: {content}")

    return "\n".join(behaviors[:20]) if behaviors else ""
```

### 6.4 关键细节

- `all_roles` 参数已在 `_process_game_over` 中从 WebSocket 消息传入（`msg.get("all_roles", {})`），格式是 `{player_id: role}`。
- `update_opponent_from_game` 内部调用 LLM，每个对手一次调用。如果一桌有 11 个对手，就是 11 次 LLM 调用。**这是 fire-and-forget 的后台任务**（`_run_post_game_pipeline` 本身就是 `asyncio.create_task`），不会阻塞游戏结算。
- `_extract_player_behavior` 是轻量级的纯字符串操作，不调用 LLM。

### 6.5 验证

```bash
# 验证辅助函数
python3 -c "
import sys
sys.path.insert(0, 'app')

# 模拟 state
state = {
    'events': [
        {'round': 1, 'status': 'seer_check', 'content': '',
         'traces': [{'from': '3', 'to': '5', 'action': 'check'}]},
        {'round': 1, 'status': 'wolf_kill', 'content': '',
         'traces': [{'from': '7', 'to': '2', 'action': 'kill'}]},
        {'round': 1, 'status': 'discussion', 'content': '3号: 我怀疑5号是狼人',
         'traces': []},
        {'round': 1, 'status': 'vote', 'content': '5号被投票出局, 3号投了5号',
         'traces': []},
    ]
}

# 需要在 main_ws 模块级别找到这个函数，所以这里手动测试逻辑
events = state.get('events', [])
player_id = '3'
behaviors = []
for event in events:
    content = event.get('content', '')
    traces = event.get('traces', [])
    status = event.get('status', '')
    round_num = event.get('round', 1)
    for t in traces:
        if t.get('from') == player_id or t.get('to') == player_id:
            behaviors.append(f'R{round_num} {status}: {t.get(\"from\",\"\")}→{t.get(\"to\",\"\")}({t.get(\"action\",\"\")})')
    if status == 'discussion' and player_id in content:
        behaviors.append(f'R{round_num} speech: {content[:100]}')
    if status in ('vote', 'vote_result') and player_id in content:
        behaviors.append(f'R{round_num} vote: {content}')

result = '\n'.join(behaviors)
assert 'seer_check' in result, 'Should include seer check action'
assert 'speech' in result, 'Should include speech'
assert 'vote' in result, 'Should include vote'
print(f'PASS: behavior extraction works:\n{result}')
"
```

---

## TASK-7：补充 `force-confirm` API

### 7.1 问题描述

设计书要求 `POST /agent/policy-buffer/force-confirm` 人工干预通道，但未实现。

### 7.2 修改文件

`app/main_ws.py`

### 7.3 精确修改

**步骤 A**: 在 WebSocket 消息路由中增加 `force_confirm` 消息类型。

找到 `elif msg_type == "rollback":` 分支（约第 209 行），在其后添加：

```python
            elif msg_type == "force_confirm":
                resp = await _process_force_confirm(
                    _route_thread_id(msg, agent_id or ""),
                    req_id,
                    msg.get("cluster_id", ""),
                )
```

**步骤 B**: 新增处理函数（在 `_process_rollback` 之后添加）：

```python
async def _process_force_confirm(session_id: str, req_id: str,
                                  cluster_id: str) -> dict:
    """人工强制确认某个 cluster，跳过防抖阈值检查。"""
    if not cluster_id:
        return {"type": "error", "req_id": req_id,
                "detail": "cluster_id is required"}
    try:
        from evolution.config import load_config, ensure_directories
        from evolution.buffer_pool import BufferPool
        from evolution.clustering import SuggestionClusterer
        from evolution.confirmation import ConfirmationJudge
        from evolution.version_manager import VersionManager

        cfg = load_config()
        ensure_directories(cfg)

        pool = BufferPool(cfg)
        cluster = pool.load_cluster(cluster_id)
        if not cluster:
            return {"type": "error", "req_id": req_id,
                    "detail": f"Cluster '{cluster_id}' not found"}

        vm = VersionManager(cfg)
        judge = ConfirmationJudge(cfg, pool, vm)

        # 强制确认：直接执行，不检查阈值
        target_skill = cluster.get("target_skill", "")
        suggestions = cluster.get("suggestions", [])
        if not target_skill or not suggestions:
            return {"type": "error", "req_id": req_id,
                    "detail": "Cluster has no target_skill or suggestions"}

        new_content = judge._synthesize_strategy(suggestions, target_skill)
        version_name = vm.create_new_version(
            skill_name=target_skill,
            content=new_content,
            source="manual_force_confirm",
            trigger_cluster=cluster_id,
        )
        pool.move_to_confirmed(cluster_id)

        return {
            "type": "force_confirm_result",
            "req_id": req_id,
            "success": True,
            "cluster_id": cluster_id,
            "skill_name": target_skill,
            "new_version": version_name,
        }
    except Exception as e:
        return {"type": "error", "req_id": req_id, "detail": str(e)}
```

### 7.4 验证

```bash
# 验证消息路由不报错（不启动完整服务，只检查代码路径）
python3 -c "
import ast
with open('app/main_ws.py') as f:
    tree = ast.parse(f.read())
# 检查 force_confirm 相关的函数定义存在
func_names = [node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
assert '_process_force_confirm' in func_names, '_process_force_confirm function not found'
print('PASS: force_confirm handler defined')
"
```

---

## TASK-8：`match_level = medium` 的折扣系数移入配置

### 8.1 问题描述

`reflection_engine.py` 第 188 行硬编码了 `causal_strength *= 0.7`。设计原则是"所有阈值集中配置，不硬编码"。

### 8.2 修改文件

1. `app/evolution/config.py` — `EvolutionConfig` dataclass
2. `app/evolution/reflection_engine.py` — `reflect()` 方法

### 8.3 精确修改

**步骤 A**: 在 `EvolutionConfig` 中添加字段（当前第 55–65 行区域）：

在 `in_game_flag_causal_multiplier: float = 1.3` 之后添加：

```python
    medium_match_causal_discount: float = 0.7  # match_level=medium 时因果强度打折系数
```

**步骤 B**: 修改 `reflection_engine.py` 第 187–188 行：

将：
```python
        if suggestion.match_level == "medium":
            suggestion.causal_strength *= 0.7
```

替换为：
```python
        if suggestion.match_level == "medium":
            suggestion.causal_strength *= self.cfg.medium_match_causal_discount
```

### 8.4 验证

```bash
python3 -c "
from app.evolution.config import EvolutionConfig
cfg = EvolutionConfig()
assert cfg.medium_match_causal_discount == 0.7, f'Expected 0.7, got {cfg.medium_match_causal_discount}'
print('PASS: medium_match_causal_discount defaults to 0.7')

# 验证可通过 YAML 覆盖
import tempfile, os
from pathlib import Path
tmp = tempfile.mkdtemp()
os.environ['WEREWOLF_AGENT_HOME'] = tmp
config_path = Path(tmp) / 'config.yaml'
config_path.write_text('debounced_policy:\n  medium_match_causal_discount: 0.5\n')

from app.evolution.config import load_config
cfg2 = load_config()
assert cfg2.medium_match_causal_discount == 0.5, f'Expected 0.5, got {cfg2.medium_match_causal_discount}'
print('PASS: medium_match_causal_discount overridable via YAML')

# 清理
del os.environ['WEREWOLF_AGENT_HOME']
import shutil
shutil.rmtree(tmp)
"
```

---

## TASK-9：反思引擎传入 Working Memory

### 9.1 问题描述

设计书要求反思引擎读取 Layer 1 工作记忆作为输入，但当前 `_run_post_game_pipeline` 只传了 `game_trace` 和 `in_game_flags`。

### 9.2 修改文件

1. `app/evolution/reflection_engine.py` — `reflect()` 方法签名和 prompt 模板
2. `app/main_ws.py` — `_run_post_game_pipeline()` 函数

### 9.3 精确修改

**步骤 A**: 在 `reflection_engine.py` 的 `REFLECT_USER_TEMPLATE` 中（当前第 73–125 行），在 `{current_strategies}` 之后、`## 请输出以下 YAML 结构` 之前，新增一段：

```python
REFLECT_USER_TEMPLATE = """## 对局信息
- 房间: {game_id}
- 我的角色: {my_role}
- 我的座位: {my_seat}
- 结果: {result}

## 完整对局 Trace
{game_trace}

## 对局中的即时标记（如果有）
{in_game_flags}

## 工作记忆（本局积累的结构化信息）
{working_memory_text}

## 当前使用的策略
{current_strategies}

## 请输出以下 YAML 结构
...（后续不变）
```

**步骤 B**: 修改 `reflect()` 方法签名，增加 `working_memory_text` 参数：

```python
    def reflect(self,
                game_id: str,
                my_role: str,
                my_seat: str,
                result: str,
                game_trace: str,
                in_game_flags: List[Dict],
                current_strategies: str = "",
                working_memory_text: str = "") -> Optional[ReflectionResult]:
```

**步骤 C**: 修改 `reflect()` 方法体内的 `user_msg` 构建：

```python
        user_msg = REFLECT_USER_TEMPLATE.format(
            game_id=game_id,
            my_role=my_role,
            my_seat=my_seat,
            result=result,
            game_trace=game_trace,
            in_game_flags=flags_text,
            current_strategies=current_strategies or "（无策略文档）",
            working_memory_text=working_memory_text or "（无工作记忆）",
        )
```

**步骤 D**: 修改 `app/main_ws.py` 的 `_run_post_game_pipeline()`，在调用 `engine.reflect()` 时传入 working memory：

找到：
```python
        # 4. Execute reflection
        engine = ReflectionEngine(cfg)
        reflection = engine.reflect(
            game_id=state.get("room_id", "unknown"),
            my_role=state["my_role"],
            my_seat=state["me_id"],
            result=result,
            game_trace=game_trace,
            in_game_flags=flags,
            current_strategies=current_strategies,
        )
```

替换为：
```python
        # 3.5 Format working memory
        working_memory_text = ""
        wm_data = state.get("working_memory")
        if wm_data:
            from memory.working_memory import WorkingMemory
            wm = WorkingMemory.from_dict(wm_data)
            working_memory_text = wm.format_for_prompt()

        # 4. Execute reflection
        engine = ReflectionEngine(cfg)
        reflection = engine.reflect(
            game_id=state.get("room_id", "unknown"),
            my_role=state["my_role"],
            my_seat=state["me_id"],
            result=result,
            game_trace=game_trace,
            in_game_flags=flags,
            current_strategies=current_strategies,
            working_memory_text=working_memory_text,
        )
```

### 9.4 验证

```bash
# 验证 reflect() 方法签名包含 working_memory_text
python3 -c "
import inspect
from app.evolution.reflection_engine import ReflectionEngine
sig = inspect.signature(ReflectionEngine.reflect)
params = list(sig.parameters.keys())
assert 'working_memory_text' in params, f'working_memory_text not in params: {params}'
print(f'PASS: reflect() accepts working_memory_text. Params: {params}')
"
```

---

## TASK-10：快速通道增加一致率检查

### 10.1 问题描述

快速通道（`causal_strength ≥ 0.8`）不检查 `consistency_rate`。如果两条建议因果强度都很高但方向矛盾，快速通道仍会确认——这违反了防抖的降噪目标。

### 10.2 修改文件

`app/evolution/confirmation.py`

### 10.3 精确修改

找到 `judge()` 方法中的快速通道分支（当前第 53–72 行）：

当前代码：
```python
        # 快速通道：高因果强度
        if avg_causal >= cfm.fast_track_min_causal_strength:
            if count >= cfm.fast_track_min_count:
                return {
                    "confirmed": True,
                    ...
                }
```

替换为：
```python
        # 快速通道：高因果强度 + 方向一致
        if avg_causal >= cfm.fast_track_min_causal_strength:
            # 即使走快速通道，也要求方向一致率 ≥ 普通通道的阈值
            # 防止两条高因果但方向矛盾的建议被错误确认
            if count >= cfm.fast_track_min_count and consistency >= cfm.normal_min_consistency_rate:
                return {
                    "confirmed": True,
                    "reason": (
                        f"Fast track: causal={avg_causal:.2f} >= {cfm.fast_track_min_causal_strength}, "
                        f"count={count} >= {cfm.fast_track_min_count}, "
                        f"consistency={consistency:.2f} >= {cfm.normal_min_consistency_rate}"
                    ),
                    "count": count,
                    "consistency_rate": consistency,
                    "avg_causal_strength": avg_causal,
                    "fast_track": True,
                }
            else:
                reasons = []
                if count < cfm.fast_track_min_count:
                    reasons.append(f"count={count}<{cfm.fast_track_min_count}")
                if consistency < cfm.normal_min_consistency_rate:
                    reasons.append(f"consistency={consistency:.2f}<{cfm.normal_min_consistency_rate}")
                return {
                    "confirmed": False,
                    "reason": f"Fast track eligible but {', '.join(reasons)}",
                    "count": count,
                    "consistency_rate": consistency,
                    "avg_causal_strength": avg_causal,
                    "fast_track": True,
                }
```

### 10.4 关键细节

快速通道的优势在于：
- 只需 `count ≥ 2`（普通通道要 `≥ 3`）
- 不要求 `avg_causal ≥ 0.5`（因为已经 `≥ 0.8`）

但它仍然应该要求 `consistency_rate ≥ 0.60`。这是防止矛盾建议被确认的安全网。

### 10.5 验证

```bash
python3 -c "
from app.evolution.confirmation import ConfirmationJudge
from app.evolution.config import EvolutionConfig

cfg = EvolutionConfig()

# Mock dependencies
class MockPool:
    def list_clusters(self): return []
    def load_cluster(self, cid): return None
    def move_to_confirmed(self, cid): pass

class MockVM:
    def create_new_version(self, **kw): return 'v2'
    def load_skill_full(self, name, version=None): return ''

judge = ConfirmationJudge(cfg, MockPool(), MockVM())

# 测试 1: 高因果 + 高一致 → 快速通道确认
cluster1 = {
    'suggestions': [{'suggestion': {'text': 'a', 'direction': 'modify', 'causal_strength': 0.9}},
                    {'suggestion': {'text': 'b', 'direction': 'modify', 'causal_strength': 0.85}}],
    'avg_causal_strength': 0.875,
    'consistency_rate': 1.0,
    'target_skill': 'test-skill',
}
r1 = judge.judge(cluster1)
assert r1['confirmed'] == True, f'Should confirm: {r1[\"reason\"]}'
assert r1['fast_track'] == True
print(f'PASS: fast track confirms with high causal + high consistency: {r1[\"reason\"]}')

# 测试 2: 高因果 + 低一致（方向矛盾）→ 不确认
cluster2 = {
    'suggestions': [{'suggestion': {'text': 'a', 'direction': 'modify', 'causal_strength': 0.9}},
                    {'suggestion': {'text': 'b', 'direction': 'discard', 'causal_strength': 0.85}}],
    'avg_causal_strength': 0.875,
    'consistency_rate': 0.5,  # 50% < 60%
    'target_skill': 'test-skill',
}
r2 = judge.judge(cluster2)
assert r2['confirmed'] == False, f'Should NOT confirm: {r2[\"reason\"]}'
assert r2['fast_track'] == True
print(f'PASS: fast track rejects with low consistency: {r2[\"reason\"]}')

# 测试 3: 高因果 + count 不足 → 不确认
cluster3 = {
    'suggestions': [{'suggestion': {'text': 'a', 'direction': 'modify', 'causal_strength': 0.9}}],
    'avg_causal_strength': 0.9,
    'consistency_rate': 1.0,
    'target_skill': 'test-skill',
}
r3 = judge.judge(cluster3)
assert r3['confirmed'] == False, f'Should NOT confirm with count=1: {r3[\"reason\"]}'
print(f'PASS: fast track rejects with insufficient count: {r3[\"reason\"]}')
"
```

---

## 附录：修改文件清单

| 文件 | 涉及 TASK |
|------|-----------|
| `app/evolution/config.py` | TASK-1, TASK-8 |
| `app/evolution/clustering.py` | TASK-2 |
| `app/evolution/curator.py` | TASK-3, TASK-4 |
| `app/evolution/reflection_engine.py` | TASK-9 |
| `app/evolution/confirmation.py` | TASK-10 |
| `app/agents/state.py` | TASK-5 |
| `app/agents/agent_graph.py` | TASK-5 |
| `app/main_ws.py` | TASK-5, TASK-6, TASK-7, TASK-9 |

## 附录：不修改但需知晓的设计偏离（已评估为可接受）

| 偏离 | 原因 | 处置 |
|------|------|------|
| InGameFlagger 只提取 description，不提取 skill/severity/phase/day | 当前 prompt 只要求 `[FLAG] 描述`，结构化字段需要 prompt 配合，属于迭代优化 | 暂不修改，后续 prompt 工程时一并调整 |
| Thompson Sampling 未实现 | 设计书标注为 Phase 3 任务 | 保持现状，记录到 backlog |
| GEPA 离线进化未实现 | 设计书标注为 Phase 4 任务 | 保持现状 |
| `_run_post_game_pipeline` 中 `pool = BufferPool(cfg)` 实例化两次 | 功能无影响，代码风格问题 | 可顺手合并为一次实例化，非阻塞 |
