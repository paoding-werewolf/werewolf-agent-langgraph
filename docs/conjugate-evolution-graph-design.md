# 共轭 Agent 进化遗传图谱 — 设计方案

> 专家组：图灵（林致远/知识图谱）、达尔文（陈进化/系统发育可视化）、像素（赵前端/D3可视化）、架构师（王基石/后端架构）  
> 主持人：snapsheep  
> 日期：2026-06-09

---

## 1. 背景与问题

当前共轭 Agent 进化系统存在两个核心可视化缺口：

1. **宏观谱系不可见**：ConjugateAgent 之间是线性后继关系（id=N → id=N+1），但缺乏直观的进化谱系展示，无法一眼看出"谁进化自谁、哪次进化改变了什么"。
2. **微观遗传断裂**：GEPA 的交叉（crossover）和变异（mutation）操作产生了新的 SkillVersion，但**父子关系没有持久化**——`EvolutionSkillVersion` 只记录了 `source="gepa_evolution"`，丢失了父版本信息。这意味着无法追溯策略的遗传路径。

本方案旨在：
- 补全数据层的遗传关系记录
- 构建三层图数据模型（Agent层 / SkillVersion层 / 跨层关联）
- 实现交互式进化遗传图谱可视化

---

## 2. 数据层补全

### 2.1 EvolutionSkillVersion 增加 parent_versions_json

**问题**：GEPA 交叉产生双亲遗传，变异产生单亲遗传，但当前 `EvolutionSkillVersion` 没有记录父版本。

**方案**：新增 `parent_versions_json` 字段，存储父版本的 key 列表。

```sql
-- Migration: 002_add_parent_versions.sql
ALTER TABLE evolution_skill_versions
  ADD COLUMN parent_versions_json JSON NOT NULL DEFAULT ('[]')
  AFTER trigger_cluster_id;
```

```python
# models.py 变更
class EvolutionSkillVersion(Base):
    # ... 现有字段 ...
    parent_versions_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
```

**写入时机**：

| 操作 | parent_versions_json 值 | 改动位置 |
|------|------------------------|---------|
| GEPA 交叉 | `["skill_a:v2", "skill_b:v1"]` | [`_llm_crossover()`](app/evolution/gepa.py:807) 返回值加 `parent_keys` |
| GEPA 变异 | `["skill_a:v2"]` | [`_llm_diagnose_and_mutate()`](app/evolution/gepa.py:643) 返回值加 `parent_keys` |
| 反思进化 | `["skill_a:v2"]` | `create_new_version()` 调用时传入 |
| 初始版本 | `[]` | 默认空列表 |

**GEPA 代码改动**：

```python
# gepa.py _llm_crossover 返回值增加
child["parent_keys"] = [parent_a["key"], parent_b["key"]]

# gepa.py _llm_diagnose_and_mutate 返回值增加
mutated_ind["parent_keys"] = [individual["key"]]

# gepa.py run() 中 create_new_version 调用处
version_name = self.loader.create_new_version(
    skill_name=ind["skill_name"],
    content=ind["content"],
    source="gepa_evolution",
    trigger_cluster=f"gepa_g{gen}",
    parent_versions=ind.get("parent_keys", []),
)
```

**SkillLoader 改动**：

```python
# skill_loader.py create_new_version 签名变更
def create_new_version(self, skill_name: str, content: str,
                       source: str = "debounced_update",
                       trigger_cluster: str = "",
                       role: str = "",
                       parent_versions: list[str] = None) -> str:
    # ... 创建 version 时写入 parent_versions_json ...
    version = EvolutionSkillVersion(
        # ... 现有字段 ...
        parent_versions_json=parent_versions or [],
    )
```

### 2.2 ConjugateAgent 增加 parent_agent_id

**问题**：Agent 之间的进化前驱依赖 id 顺序隐式推断，在回滚场景下不可靠。

**方案**：新增 `parent_agent_id` 字段，显式记录进化前驱。

```sql
-- Migration: 002_add_parent_versions.sql（同一文件）
ALTER TABLE conjugate_agents
  ADD COLUMN parent_agent_id INT NULL,
  ADD INDEX idx_conjugate_agents_parent (parent_agent_id);
```

```python
# models.py 变更
class ConjugateAgent(Base):
    # ... 现有字段 ...
    parent_agent_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
```

**写入时机**：

| 场景 | parent_agent_id 值 |
|------|-------------------|
| 初代 Agent | `NULL` |
| 正常进化 | 前一个 agent 的 id |
| 回滚后重新进化 | 被回滚到的 agent 的 id |

**conjugate_agent.py 改动**：

```python
# maybe_create_conjugate_agent 中
agent = ConjugateAgent(
    # ... 现有字段 ...
    parent_agent_id=latest.id if latest else None,
)

# ensure_initial_conjugate_agent 中
agent = ConjugateAgent(
    # ... 现有字段 ...
    parent_agent_id=None,  # 初代无前驱
)
```

---

## 3. 图数据模型

### 3.1 三层图结构

```
┌──────────────── Agent 层 ────────────────┐
│  节点: ConjugateAgent                     │
│  边:   EVOLVED_FROM (agent → agent)       │
│  布局: 时间轴（x=born_at, y=固定）        │
└──────────────┬───────────────────────────┘
               │ CONTAINS 边（跨层）
┌──────────────▼───────────────────────────┐
│  节点: SkillVersion                       │
│  边:   MUTATED_FROM (单亲遗传)            │
│        CROSSED_FROM (双亲遗传)            │
│        SUPERSEDED_BY (版本晋升链)         │
│  布局: 泳道式（每 skill 一列，版本纵向）   │
└──────────────┬───────────────────────────┘
               │ BELONGS_TO 边（跨层）
┌──────────────▼───────────────────────────┐
│  节点: Skill                              │
│  边:   无（叶子节点分组）                  │
└──────────────────────────────────────────┘
```

### 3.2 节点类型定义

```typescript
// 前端 TypeScript 类型定义
interface GraphNode {
  id: string;           // 格式: "agent:{id}" | "sv:{skill_name}:{version}" | "skill:{skill_name}"
  type: "ConjugateAgent" | "SkillVersion" | "Skill";
  label: string;
  data: Record<string, any>;
}

interface ConjugateAgentNodeData {
  fingerprint: string;
  agent_name: string;
  avatar_seed: string;
  born_at: string;       // ISO 8601
  win_rate: number;
  games_played: number;
  changelog: string;
  lore: string;
  skill_count: number;   // skill_versions_json 中的 skill 数量
}

interface SkillVersionNodeData {
  skill_name: string;
  version: string;
  source: string;        // "gepa_evolution" | "debounced_update" | ...
  status: string;        // "active" | "candidate" | "superseded" | "archived"
  role: string;
  win_rate: number;
  games_played: number;
  parent_versions: string[];  // ["skill_name:version", ...]
}

interface SkillNodeData {
  skill_name: string;
  role: string;
  description: string;
  current_default: string;
  skill_win_rate: number;
}
```

### 3.3 边类型定义

```typescript
interface GraphEdge {
  source: string;       // 节点 id
  target: string;       // 节点 id
  type: EdgeType;
  data?: Record<string, any>;
}

type EdgeType =
  | "EVOLVED_FROM"     // ConjugateAgent → ConjugateAgent（宏观进化）
  | "CONTAINS"         // ConjugateAgent → SkillVersion（跨层包含）
  | "MUTATED_FROM"     // SkillVersion → SkillVersion（单亲遗传）
  | "CROSSED_FROM"     // SkillVersion → SkillVersion（双亲遗传）
  | "SUPERSEDED_BY"    // SkillVersion → SkillVersion（版本晋升链）
  | "BELONGS_TO";      // SkillVersion → Skill（归属关系）

// EVOLVED_FROM 边的附加数据
interface EvolvedFromEdgeData {
  trigger_skill_name: string;
  previous_version: string;
  new_version: string;
  changelog: string;
}

// CROSSED_FROM 边的附加数据
interface CrossedFromEdgeData {
  gepa_generation: number;   // GEPA 第几代产生的
  trigger_cluster: string;
}

// MUTATED_FROM 边的附加数据
interface MutatedFromEdgeData {
  gepa_generation: number;
  weakest_dimension: string;  // 变异针对的适应度维度
}
```

### 3.4 图的 DAG 性质

- **Agent 层**：严格线性链（每个 agent 最多一个前驱），退化为路径图
- **SkillVersion 层**：真正的 DAG——GEPA 交叉产生双亲边，形成网状结构
- **跨层**：CONTAINS 是一对多映射，不形成环

整体图是 DAG，不存在环。这保证了布局算法的稳定性和可渲染性。

---

## 4. 后端 API 设计

### 4.1 图数据查询 API

```
GET /api/evolution/graph
```

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `depth` | string | `"full"` | `"agent"` 仅 Agent 层 / `"full"` 含 SkillVersion 层 |
| `from_agent_id` | int | 无 | 起始 agent id（时间范围过滤） |
| `to_agent_id` | int | 无 | 结束 agent id |
| `include_gepa` | bool | `true` | 是否包含 GEPA 遗传边（MUTATED_FROM / CROSSED_FROM） |
| `include_superseded` | bool | `false` | 是否包含 SUPERSEDED_BY 边 |
| `skill_name` | string | 无 | 过滤特定 skill 的版本节点 |

**返回格式**：

```json
{
  "meta": {
    "agent_count": 12,
    "skill_version_count": 47,
    "edge_count": 89,
    "depth": "full",
    "generated_at": "2026-06-09T12:00:00Z"
  },
  "nodes": [
    {
      "id": "agent:1",
      "type": "ConjugateAgent",
      "label": "影刃-初纪",
      "data": {
        "fingerprint": "a1b2c3...",
        "agent_name": "影刃-初纪",
        "avatar_seed": "conjugate-a1b2c3...",
        "born_at": "2026-06-01T08:00:00",
        "win_rate": 0.55,
        "games_played": 20,
        "changelog": "Genesis: initial skill library snapshot",
        "lore": "影刃-初纪是共轭谱系的初代形态...",
        "skill_count": 5,
        "parent_agent_id": null
      }
    },
    {
      "id": "sv:seer-identity-timing:v3",
      "type": "SkillVersion",
      "label": "seer-identity-timing v3",
      "data": {
        "skill_name": "seer-identity-timing",
        "version": "v3",
        "source": "gepa_evolution",
        "status": "active",
        "role": "seer",
        "win_rate": 0.62,
        "games_played": 15,
        "parent_versions": ["seer-identity-timing:v2", "wolf-bluff:v1"]
      }
    }
  ],
  "edges": [
    {
      "source": "agent:1",
      "target": "agent:2",
      "type": "EVOLVED_FROM",
      "data": {
        "trigger_skill_name": "seer-identity-timing",
        "previous_version": "v1",
        "new_version": "v2",
        "changelog": "预言家身份时机策略从保守等待转为主动跳身份..."
      }
    },
    {
      "source": "sv:seer-identity-timing:v2",
      "target": "sv:seer-identity-timing:v3",
      "type": "CROSSED_FROM",
      "data": {
        "gepa_generation": 3,
        "trigger_cluster": "gepa_g3"
      }
    },
    {
      "source": "sv:wolf-bluff:v1",
      "target": "sv:seer-identity-timing:v3",
      "type": "CROSSED_FROM",
      "data": {
        "gepa_generation": 3,
        "trigger_cluster": "gepa_g3"
      }
    }
  ]
}
```

### 4.2 图构建服务

新增 `app/evolution/graph_builder.py`，负责从 MySQL 关系数据组装图结构：

```python
class EvolutionGraphBuilder:
    """从关系数据构建进化遗传图。"""

    def build_graph(
        self,
        depth: str = "full",
        from_agent_id: int | None = None,
        to_agent_id: int | None = None,
        include_gepa: bool = True,
        include_superseded: bool = False,
        skill_name: str | None = None,
    ) -> dict:
        """构建完整图数据，返回符合 API 规范的 JSON 结构。"""
        nodes = []
        edges = []

        # 1. 构建 Agent 层节点和边
        agents = self._load_agents(from_agent_id, to_agent_id)
        for agent in agents:
            nodes.append(self._agent_to_node(agent))
            if agent.parent_agent_id:
                edges.append(self._evolved_from_edge(agent))

        # 2. 构建 SkillVersion 层（depth=full 时）
        if depth == "full":
            skill_versions = self._load_skill_versions(agents, skill_name)
            for sv in skill_versions:
                nodes.append(self._sv_to_node(sv))
                # GEPA 遗传边
                if include_gepa and sv.parent_versions_json:
                    edges.extend(self._heritage_edges(sv))
                # 版本晋升链
                if include_superseded:
                    edges.extend(self._superseded_edges(sv))
            # 跨层 CONTAINS 边
            edges.extend(self._contains_edges(agents))

        return {
            "meta": self._build_meta(nodes, edges, depth),
            "nodes": nodes,
            "edges": edges,
        }
```

### 4.3 API 路由注册

在 `app/main_ws.py` 中注册：

```python
from evolution.graph_builder import EvolutionGraphBuilder

@app.get("/api/evolution/graph")
async def get_evolution_graph(
    depth: str = "full",
    from_agent_id: int | None = None,
    to_agent_id: int | None = None,
    include_gepa: bool = True,
    include_superseded: bool = False,
    skill_name: str | None = None,
):
    builder = EvolutionGraphBuilder()
    return builder.build_graph(
        depth=depth,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        include_gepa=include_gepa,
        include_superseded=include_superseded,
        skill_name=skill_name,
    )
```

---

## 5. 前端可视化设计

### 5.1 技术栈

| 组件 | 选型 | 理由 |
|------|------|------|
| 图渲染 | D3.js v7 + dagre-d3 | DAG 布局成熟，交互定制能力强 |
| UI 框架 | React 18 | 组件化封装，方便集成 |
| 样式 | Tailwind CSS + CSS Variables | 暗色主题快速实现 |
| 头像生成 | DiceBear API | 用 avatar_seed 生成唯一头像 |
| 图标 | Lucide React | 轻量、风格统一 |
| 构建 | Vite | 快速开发体验 |

### 5.2 布局策略：时间轴 + 泳道混合布局

**Agent 层**（上层）：
- 水平时间轴布局，x 轴 = `born_at` 时间戳，y 轴固定
- 节点间距按时间差等比缩放，最小间距 120px
- 进化边为水平方向的有向线段

**SkillVersion 层**（下层，点击 Agent 展开时显示）：
- 泳道式布局：每个 skill 一条垂直泳道
- 同一 skill 的版本按时间从上到下排列
- 交叉边跨越泳道，用贝塞尔曲线连接
- 变异边在泳道内垂直连接

**跨层关联**：
- Agent 节点与它包含的 SkillVersion 之间用半透明连线
- 不参与布局计算，纯视觉叠加

### 5.3 交互设计

#### 5.3.1 默认视图：Agent 时间线

```
┌──────────────────────────────────────────────────────────────────┐
│  共轭进化图谱                    [全屏] [设置] [时间轴缩放]      │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ●影刃-初纪 ──▶ ●霜牙-二纪 ──▶ ●烬瞳-三纪 ──▶ ●夜弦-四纪      │
│   55%wr          58%wr          61%wr          59%wr            │
│   6/1            6/3            6/5            6/7              │
│                                                                  │
│  ─── 图例 ───                                                    │
│  ● Agent节点（圆=胜率热力图色）  ──▶ 进化边                      │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

- 节点：圆形，直径 48px，内嵌 DiceBear 头像
- 外圈：胜率热力图色环（<40% 红，40-55% 橙，55-70% 绿，>70% 金）
- 下方标注：胜率 + 诞生日期
- 悬停：弹出卡片显示 agent_name、changelog 摘要（前100字）、lore 片段

#### 5.3.2 展开视图：Agent 基因组

点击 Agent 节点后，下方展开该 agent 的 skill 版本组成：

```
┌──────────────────────────────────────────────────────────────────┐
│  ●烬瞳-三纪 ──▶ ●夜弦-四纪                                      │
│                  │ [展开]                                        │
│                  ▼                                               │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  夜弦-四纪 的基因组                                       │    │
│  │                                                          │    │
│  │  seer-timing    wolf-bluff     common-base   witch-save │    │
│  │  ┌──────┐      ┌──────┐      ┌──────┐      ┌──────┐  │    │
│  │  │ v2 ● │      │ v3 ◆ │      │ v1 ● │      │ v2 ● │  │    │
│  │  └──────┘      └──────┘      └──────┘      └──────┘  │    │
│  │   active        active        active        active     │    │
│  │                                                          │    │
│  │  ● = active  ◆ = crossover origin  ○ = candidate       │    │
│  └─────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

- 每个 skill 版本用小卡片展示
- 卡片颜色 = 角色色（seer=蓝，wolf=红，common=灰，witch=紫，guard=绿）
- 卡片标记 = 状态图标（active=实心圆，candidate=空心圆，crossover=菱形）

#### 5.3.3 深入视图：Skill 版本遗传网

双击 SkillVersion 卡片，进入该 skill 的版本遗传网：

```
┌──────────────────────────────────────────────────────────────────┐
│  wolf-bluff 版本遗传网                          [返回] [全屏]    │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  v1 ──mutated──▶ v2 ─┐                                          │
│                      ├──crossed──▶ v3 ──mutated──▶ v4           │
│  seer-timing:v2 ─────┘                                          │
│                                                                  │
│  v1 [superseded]  v2 [superseded]  v3 [active]  v4 [candidate] │
│   wr: 45%          wr: 52%          wr: 61%      wr: --         │
│                                                                  │
│  ─── 图例 ───                                                    │
│  ──▶ 变异边(橙)   ──▶ 交叉边(紫虚线)   ──▶ 晋升链(绿)         │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

- dagre 布局，自动处理 DAG 的层级
- 交叉边用紫色虚线 + 双箭头起点
- 变异边用橙色实线 + 单箭头
- 晋升链用绿色细线

#### 5.3.4 右侧详情面板

选中任意节点后，右侧弹出详情面板：

```
┌─────────────────────────┐
│  ● 夜弦-四纪             │
│  ┌───────────────────┐  │
│  │  [DiceBear 头像]   │  │
│  └───────────────────┘  │
│                         │
│  胜率: 59% (23/39)      │
│  诞生: 2026-06-07       │
│  前驱: 烨瞳-三纪        │
│                         │
│  ── Changelog ──        │
│  预言家身份时机策略从    │
│  保守等待转为主动跳身    │
│  份...                  │
│                         │
│  ── Lore ──             │
│  夜弦继承了烨瞳的暗影    │
│  直觉，但将预言家的信    │
│  号解读从被动等待改为    │
│  主动出击...            │
│                         │
│  ── 基因组 ──           │
│  seer-timing v2 [active]│
│  wolf-bluff  v3 [active]│
│  common-base v1 [active]│
│  witch-save  v2 [active]│
│                         │
│  ── 胜率趋势 ──         │
│  [迷你折线图]           │
└─────────────────────────┘
```

### 5.4 视觉风格规范

#### 5.4.1 配色方案

```css
:root {
  /* 背景 */
  --bg-primary: #0a0e17;
  --bg-secondary: #111827;
  --bg-card: #1a2234;
  --bg-hover: #243049;

  /* 文字 */
  --text-primary: #e2e8f0;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;

  /* 节点颜色 */
  --node-agent: #60a5fa;          /* Agent 节点默认蓝 */
  --node-seer: #38bdf8;           /* 预言家蓝 */
  --node-wolf: #f87171;           /* 狼人红 */
  --node-common: #94a3b8;         /* 通用灰 */
  --node-witch: #c084fc;          /* 女巫紫 */
  --node-guard: #4ade80;          /* 守卫绿 */

  /* 边颜色 */
  --edge-evolved: #60a5fa;        /* 进化边蓝 */
  --edge-mutated: #fb923c;        /* 变异边橙 */
  --edge-crossed: #c084fc;        /* 交叉边紫 */
  --edge-superseded: #4ade80;     /* 晋升链绿 */
  --edge-contains: rgba(148,163,184,0.2); /* 包含边半透明 */

  /* 胜率热力图 */
  --winrate-bad: #ef4444;         /* <40% */
  --winrate-mid: #f97316;         /* 40-55% */
  --winrate-good: #22c55e;        /* 55-70% */
  --winrate-great: #eab308;       /* >70% */

  /* 发光效果 */
  --glow-agent: 0 0 12px rgba(96,165,250,0.5);
  --glow-active: 0 0 16px rgba(34,197,94,0.6);
}
```

#### 5.4.2 节点样式

- **Agent 节点**：圆形 48px，DiceBear 头像居中，外圈 3px 胜率色环，选中时发光
- **SkillVersion 节点**：菱形 24px，填充色=角色色，边框色=状态（active=实线，candidate=虚线）
- **Skill 节点**：圆角矩形，仅作为泳道标签，不参与交互

#### 5.4.3 边样式

| 边类型 | 线型 | 颜色 | 箭头 | 特效 |
|--------|------|------|------|------|
| EVOLVED_FROM | 实线 2px | `--edge-evolved` | 单箭头 | 渐变流光 |
| MUTATED_FROM | 实线 1.5px | `--edge-mutated` | 单箭头 | 无 |
| CROSSED_FROM | 虚线 2px | `--edge-crossed` | 双起点标记 | 贝塞尔曲线 |
| SUPERSEDED_BY | 细实线 1px | `--edge-superseded` | 单箭头 | 无 |
| CONTAINS | 极细线 0.5px | `--edge-contains` | 无 | 半透明 |

#### 5.4.4 动画

- **节点出现**：从 0 缩放到 1，300ms ease-out
- **边绘制**：stroke-dashoffset 动画，500ms
- **悬停发光**：box-shadow 过渡，200ms
- **展开/收起**：高度过渡 + 透明度，400ms ease-in-out
- **GEPA 运行中**：当前进化边脉冲动画（stroke 闪烁）

### 5.5 组件结构

```
src/
├── components/
│   ├── EvolutionGraph/
│   │   ├── EvolutionGraph.tsx        # 主容器组件
│   │   ├── AgentTimeline.tsx         # Agent 层时间线
│   │   ├── AgentNode.tsx             # Agent 节点（D3 渲染）
│   │   ├── GenomePanel.tsx           # Agent 基因组展开面板
│   │   ├── SkillVersionNet.tsx       # Skill 版本遗传网
│   │   ├── DetailPanel.tsx           # 右侧详情面板
│   │   ├── GraphLegend.tsx           # 图例
│   │   ├── TimeAxis.tsx              # 时间轴缩放控件
│   │   └── types.ts                  # TypeScript 类型定义
│   └── ...
├── hooks/
│   ├── useGraphData.ts               # 图数据获取 hook
│   └── useGraphLayout.ts             # 布局计算 hook
├── services/
│   └── graphApi.ts                   # API 调用
└── utils/
    ├── layoutEngine.ts               # dagre 布局封装
    ├── colorScales.ts                # 胜率热力图色阶
    └── avatar.ts                     # DiceBear 头像生成
```

---

## 6. 实施路线图

### Phase 1：数据补全（1-2天）

- [ ] 执行 migration：`parent_versions_json` + `parent_agent_id`
- [ ] 修改 `EvolutionSkillVersion` model
- [ ] 修改 `ConjugateAgent` model
- [ ] 修改 GEPA `_llm_crossover` / `_llm_diagnose_and_mutate` 写入 parent_keys
- [ ] 修改 `SkillLoader.create_new_version` 接收 parent_versions 参数
- [ ] 修改 `conjugate_agent.py` 写入 parent_agent_id
- [ ] 编写数据回填脚本（为现有 GEPA 版本补全 parent_versions_json）

### Phase 2：后端图构建服务（2-3天）

- [ ] 实现 `EvolutionGraphBuilder` 类
- [ ] 实现 Agent 层节点/边构建
- [ ] 实现 SkillVersion 层节点/边构建（含 GEPA 遗传边）
- [ ] 实现跨层 CONTAINS 边构建
- [ ] 注册 API 路由
- [ ] 编写单元测试

### Phase 3：前端可视化（3-5天）

- [ ] 搭建 React + Vite + D3 项目骨架
- [ ] 实现 Agent 时间线视图
- [ ] 实现 Agent 节点渲染（DiceBear 头像 + 胜率色环）
- [ ] 实现基因组展开面板
- [ ] 实现 Skill 版本遗传网视图
- [ ] 实现右侧详情面板
- [ ] 实现暗色主题 + 动画
- [ ] 响应式适配

### Phase 4：增强功能（后续迭代）

- [ ] WebSocket 实时更新（GEPA 运行时图谱动态更新）
- [ ] GEPA 代际时间旅行（查看特定代的种群快照）
- [ ] 从图谱触发操作（手动交叉、回滚等）
- [ ] 导出图谱为 SVG/PNG
- [ ] 图谱嵌入到现有 TUI/管理界面

---

## 7. 关键决策记录

| # | 决策 | 选项 | 结论 | 理由 |
|---|------|------|------|------|
| 1 | 是否引入图数据库 | Neo4j vs MySQL+应用层图 | MySQL+应用层图 | 数据量级小（<1000节点），不值得引入新基础设施 |
| 2 | 可视化布局 | 纯dagre vs 时间轴+泳道混合 | 混合布局 | Agent层是时间序列，纯dagre会退化为直线；泳道让交叉边更清晰 |
| 3 | 第一版范围 | 展示+操作 vs 纯展示 | 纯展示 | 操作风险高，展示已提供巨大价值，操作后续迭代 |
| 4 | 更新机制 | WebSocket vs 轮询 | 轮询（后续升级WS） | GEPA进化非高频，轮询够用，WS增加复杂度 |
| 5 | GEPA遗传数据补全 | 加字段 vs 从state反推 | 加parent_versions_json字段 | 源数据比推导可靠，改动量小，向后兼容 |
| 6 | Agent前驱关系 | 隐式推断(id顺序) vs 显式记录 | 显式parent_agent_id | 回滚场景下隐式推断不可靠 |