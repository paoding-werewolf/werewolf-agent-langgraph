# 狼人杀 AI Agent 框架与自进化管线 — 答辩/展示文档

> 项目：werewolf-agent-langgraph
> 日期：2026-06-09

---

## 一、项目定位与架构全景

### 1.1 系统定位

本项目是**狼人杀游戏的 AI Agent 决策服务**，采用 LangGraph 构建智能决策图，为 12 人标准局中每位 AI 玩家提供"感知—思考—行动"的完整决策链路。

系统采用**双服务架构**：

| 层级 | 服务 | 端口 | 职责 |
|------|------|------|------|
| 编排层 | paoding-werewolf-service | 8000 | 游戏流程控制、阶段推进、事件广播、死亡结算 |
| 决策层 | werewolf-agent-langgraph | 7860/7861 | 单个玩家的感知、推理、决策智能 |

**核心关系**：编排层负责"游戏怎么转"，决策层负责"AI 怎么想"。

### 1.2 目录结构

```
werewolf-agent-langgraph/
├── app/
│   ├── main_ws.py                 # WebSocket 服务器入口（与编排层通信）
│   ├── agents/                    # 核心 Agent 决策逻辑
│   │   ├── agent_graph.py         # LangGraph 决策流（感知→反思→行动）
│   │   ├── prompt_builder.py      # 六模块 Prompt 构建管线
│   │   ├── prompt_storage.py      # 静态策略知识库（8000+ 字思维框架）
│   │   ├── llm_caller.py          # LLM 调用封装
│   │   ├── state.py               # AgentState 类型定义
│   │   ├── protocol.py            # 消息协议归一化
│   │   └── session_store.py       # 多实例会话管理
│   ├── evolution/                 # 自进化管线（核心创新）
│   │   ├── reflection_engine.py   # 对局后深度反思
│   │   ├── buffer_pool.py         # 建议缓冲池
│   │   ├── clustering.py          # 语义聚类
│   │   ├── confirmation.py        # 双阈值确认
│   │   ├── version_manager.py     # 策略版本管理
│   │   ├── skill_loader.py        # 渐进式策略加载（三层架构）
│   │   ├── gepa.py                # GEPA 遗传-帕累托离线进化
│   │   ├── conjugate_agent.py     # 共轭 Agent 快照与人格进化
│   │   ├── in_game_flagger.py     # 对局中即时策略标记
│   │   ├── curator.py             # 策略库策展人
│   │   ├── summary.py             # 自进化活动摘要生成
│   │   ├── config.py              # 集中配置
│   │   └── models.py              # 数据库 ORM 模型
│   ├── memory/                    # 四层记忆系统
│   │   ├── working_memory.py      # L1: 局内工作记忆
│   │   ├── opponent_model.py      # L2: 跨局对手画像
│   │   ├── self_model.py          # L3: 自我改进追踪
│   │   └── game_archive.py        # L4: 对局历史归档
│   ├── core/                      # 核心数据结构
│   │   ├── enums.py               # 角色/阶段/事件枚举
│   │   ├── game_state.py          # Agent 视角游戏状态
│   │   └── state_machine.py       # 状态机镜像
│   └── utils/                     # 调试与可视化
│       ├── prompt_logger.py       # Prompt 历史记录
│       ├── debug_view.py          # HTML 调试界面
│       └── tui_visualizer.py      # 终端游戏面板
├── tests/                         # 测试套件
├── docs/                          # 架构与规格文档
├── migrations/                    # 数据库迁移
├── Dockerfile / docker-compose.yml
└── requirements.txt
```

---

## 二、Agent 决策框架

### 2.1 LangGraph 三节点决策图

Agent 使用 LangGraph 构建 **"感知—反思—行动"三节点决策流**：

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐      ┌──────┐
│  perceive    │─────▶│   reflect    │─────▶│     act      │─────▶│ END  │
│  (无 LLM)    │      │  (1 次 LLM)  │      │  (1 次 LLM)  │      │      │
└──────────────┘      └──────────────┘      └──────────────┘      └──────┘
     │                      │                      │
  解析事件，更新         生成内心独白，          按阶段路由选择
  AgentState，          提取逻辑矛盾，          工具集，输出
  更新工作记忆          标记策略矛盾[FLAG]       结构化 JSON 动作
```

**设计要点**：
- **perceive 节点无 LLM 调用**：纯确定性逻辑，解析事件、更新玩家状态、维护工作记忆
- **reflect 节点 1 次 LLM**：生成内心独白，发现矛盾，标记策略不匹配
- **act 节点 1 次 LLM**：基于反思结果，结合策略与记忆，输出最终决策

### 2.2 阶段路由系统

act 节点根据当前游戏阶段进行条件路由（`_route_by_phase`）：

| 路由目标 | 触发阶段 | 涉及角色 |
|----------|----------|----------|
| `decide_night_role` | 守卫行动/预言家查验/女巫用药 | Guard, Seer, Witch |
| `decide_wolf_gesture` | 狼人击杀/狼人夜间讨论 | Wolf, Wolf King |
| `decide_election` | 警长竞选报名/演讲/投票 | 全体 |
| `decide_discussion` | 白天讨论/PK演讲/遗言 | 全体 |
| `decide_vote` | 放逐投票 | 全体 |
| `decide_shoot` | 猎人/狼王开枪 | Hunter, Wolf King |
| `decide_generic` | 其他（警长移交警长等） | 特定 |

### 2.3 AgentState 结构

```python
class AgentState(TypedDict):
    # 身份信息
    room_id: str                    # 游戏房间标识
    me_id: str                      # 玩家座位号 (1-12)
    my_role: str                    # 角色名 (seer, witch, wolf...)
    agent_id: str                   # 完整 ID: "{player_id}_{role}_{unique_id}"
    session_id: str                 # WebSocket 会话（多实例支持）

    # 游戏状态
    phase: str                      # 当前游戏阶段
    day: int                        # 当前天数/轮次
    sheriff: Optional[str]          # 当前警长玩家 ID
    players: Dict[str, Dict]        # 玩家感知（角色多数未知）
    events: List[Dict]              # 完整事件历史

    # 认知层
    last_thought: str               # 上一轮反思输出
    next_action: Optional[Dict]     # 最终决策输出
    request: Optional[Dict]         # 编排层发来的当前请求

    # 记忆与进化
    working_memory: Dict            # L1: 局内结构化数据
    strategies_used: List[str]      # 本局注入的策略列表
    versions_used: Dict[str, str]   # {skill_name: version_id} 版本追踪
    in_game_flags: List[Dict]       # 对局中实时策略标记
```

### 2.4 六模块 Prompt 构建管线

每次 LLM 调用前，`PromptBuilder` 组装六模块 Prompt：

| 模块 | 内容 | 加载时机 | Token 预算 |
|------|------|----------|-----------|
| 1. 核心任务 | 角色、ID、阵营分配 | 始终 | ~100 |
| 2. 游戏信息 | 全局信息+阶段摘要+近期公开事件 | 始终 | ~800 |
| 3. 记忆注入 | 工作记忆+对手画像+自我模型 | 条件性 | ~500 |
| 4. 进化策略 | 策略索引+匹配策略全文 | 始终 | ~2,000 |
| 5. 思维框架 | 8000+ 字中文策略指南 | 始终 | ~3,000 |
| 6. 对局标记 | 实时策略矛盾检测 | 条件性 | ~200 |

---

## 三、四层记忆系统

从临时到持久，记忆分为四层：

```
┌─────────────────────────────────────────────────────┐
│ L1: 工作记忆 (Working Memory)                        │
│ 范围: 单局  |  存储: 内存  |  延迟: <1ms              │
│ 内容: 已知信息、发言记录、行动历史、矛盾检测、嫌疑评级  │
│ 特性: 每局结束后清除，支持旧条目自动压缩               │
├─────────────────────────────────────────────────────┤
│ L2: 对手画像 (Opponent Model)                        │
│ 范围: 跨局  |  存储: YAML文件  |  延迟: ~10ms          │
│ 内容: 玩家行为统计（狼人行为/好人行为/弱点/近期对局）  │
│ 路径: ~/.werewolf-agent/memory/opponents/{id}.yaml   │
├─────────────────────────────────────────────────────┤
│ L3: 自我模型 (Self Model)                            │
│ 范围: 跨局  |  存储: YAML文件  |  延迟: ~10ms          │
│ 内容: 各角色胜率、常见错误、优势领域、改进方向          │
│ 路径: ~/.werewolf-agent/memory/self_model/profile.yaml│
├─────────────────────────────────────────────────────┤
│ L4: 对局归档 (Game Archive)                          │
│ 范围: 永久  |  存储: MySQL  |  延迟: ~50ms             │
│ 内容: 完整对局 trace，含策略使用和版本追踪              │
│ 用途: 纵向分析、GEPA 适应度评估、策略回顾              │
└─────────────────────────────────────────────────────┘
```

**工作记忆核心数据结构**：

```python
@dataclass
class WorkingMemory:
    game_id: str
    my_role: str
    my_seat: str
    day: int
    known_info: List[str]           # 夜间结果、查验信息
    speeches: Dict[str, Dict]       # day_key -> 玩家发言
    actions: List[str]              # 所有已执行行动
    my_speeches: Dict[str, str]     # 我在各天的完整发言
    contradictions: List[str]       # 检测到的逻辑矛盾
    flags: List[Dict]               # 对局中策略标记
    suspicion: Dict[str, List]      # HIGH/MED/LOW 嫌疑列表
```

---

## 四、自进化管线（核心创新）

### 4.1 整体架构

自进化系统是本项目**最核心的创新点**，实现了 Agent 从"被动执行策略"到"主动优化策略"的闭环。

```
┌─────────────────────────────────────────────────────────────────┐
│                    对局中 (实时)                                  │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐                   │
│  │  感知    │───▶│  反思    │───▶│  行动    │                   │
│  └──────────┘    └────┬─────┘    └──────────┘                   │
│                       │                                          │
│              ┌────────▼────────┐                                 │
│              │ InGameFlagger   │ ← 从反思文本中提取 [FLAG] 标记  │
│              └─────────────────┘                                 │
└───────────────────────┬─────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│                  对局后 (赛后)                                    │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  ReflectionEngine.reflect()                                 ││
│  │  输入: 完整对局 trace + 工作记忆 + 对局标记 + 当前策略       ││
│  │  输出: ReflectionResult                                      ││
│  │    ├── SceneTags (角色、存活轮数、攻击性、关键阶段...)       ││
│  │    ├── CausalChain (行动 → 中间结果 → 最终结局)              ││
│  │    └── StrategySuggestion (建议文本、置信度、方向)            ││
│  │  核心能力: 区分"策略驱动"与"运气驱动"的胜负                   ││
│  └──────────────────────────┬──────────────────────────────────┘│
│                              ▼                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  BufferPool.ingest()                                        ││
│  │  ├── 过滤低质量建议 (match_level="low")                      ││
│  │  ├── 因果强度折扣 (中等匹配 ×0.7)                            ││
│  │  └── 写入 MySQL (status: PENDING)                            ││
│  └──────────────────────────┬──────────────────────────────────┘│
└──────────────────────────────┬──────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                 后台处理 (周期性)                                 │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  SuggestionClusterer.process_pending()                      ││
│  │  ├── 场景标签相似度匹配 (加权评分)                            ││
│  │  │   ├── 核心维度 (角色/阶段/结果): 各 25% = 75%            ││
│  │  │   └── 次要维度: 各 6.25% = 25%                           ││
│  │  ├── 语义一致性检查 (LLM 仲裁)                               ││
│  │  └── 合并/创建集群                                          ││
│  └──────────────────────────┬──────────────────────────────────┘│
│                              ▼                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  ConfirmationJudge.check_all_clusters()                     ││
│  │  双阈值确认系统:                                            ││
│  │  ├── 快速通道 (因果强度 ≥ 0.8): 建议数 ≥ 2, 一致率 ≥ 60%   ││
│  │  └── 标准通道: 建议数 ≥ 3, 一致率 ≥ 60%, 平均因果 ≥ 0.5    ││
│  │  确认后: LLM 合并建议 → 创建新版本 (candidate)               ││
│  └──────────────────────────┬──────────────────────────────────┘│
│                              ▼                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  Curator (策展人, 周期性后台任务)                            ││
│  │  ├── Phase 1: 归档过期版本 (>30天)                           ││
│  │  ├── Phase 2: LLM 审查 (保留/修补/合并/归档)                 ││
│  │  └── Phase 3: 快照策略库                                     ││
│  └─────────────────────────────────────────────────────────────┘│
└──────────────────────────────┬──────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                 版本竞争 (每局)                                   │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  SkillLoader.get_version_for_game()                         ││
│  │  ├── 存在候选版本: 50% 概率使用候选 (探索)                   ││
│  │  └── 否则: 使用当前默认 (利用)                               ││
│  │                                                              ││
│  │  赛后: record_version_usage(skill, version, won)             ││
│  │  晋升条件: 候选胜率 - 默认胜率 ≥ 10% 且 候选对局 ≥ 5        ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 对局中即时标记 (InGameFlagger)

**动机**：策略可能在执行时与现实局势脱节，但等到赛后反思为时已晚。

**实现**：在 act prompt 中注入标记指令：

```
当你执行策略时发现策略与实际局势不符：
1. 不要惊慌，先按当前最佳判断继续行动
2. 在思考过程中标记："[FLAG] 策略 X 与当前局势矛盾：原因 Y"
3. 标记会被自动记录，对局结束后用于策略优化
```

**匹配度三级评估**：
- 高匹配（策略明确覆盖当前场景）→ 按策略执行
- 中匹配（策略部分覆盖，局势有差异）→ 策略参考 + 现场调整
- 低匹配/无覆盖 → 独立判断，不被动跟风

### 4.3 反思引擎 (ReflectionEngine)

**核心职责**：区分"策略驱动"与"运气驱动"的胜负。

**输入**：
- 完整对局 trace（所有事件、行动、投票记录）
- 工作记忆（已知信息、发言记录、嫌疑列表）
- 对局标记（InGameFlagger 提取的 [FLAG] 标记）
- 当前策略文档

**输出** — `ReflectionResult`：

```python
@dataclass
class SceneTags:
    role: str                    # 本局角色
    critical_phase: str          # 关键阶段 (early/mid/late)
    result: str                  # 胜/负
    role_survived_rounds: int    # 存活轮数
    wolf_aggression: str         # 狼队攻击性 (high/medium/low)
    sheriff_contested: bool      # 警长是否竞争激烈
    first_night_target: str      # 首夜目标类型

@dataclass
class CausalStep:
    action: str                  # 我方行动
    intermediate_result: str     # 中间结果
    outcome_contribution: float  # 对最终结果的贡献度

@dataclass
class StrategySuggestion:
    text: str                    # 建议文本
    confidence: float            # 置信度
    direction: str               # 方向: modify / create / discard
    target_skill: str            # 目标策略名
    causal_strength: float       # 因果强度（核心指标）
    match_level: str             # 匹配度: high / medium / low
```

**因果强度调节**：
- 对局标记加成：`causal_strength × 1.3`（有实时标记佐证更可信）
- 中等匹配折扣：`causal_strength × 0.7`（策略只部分适用时降低因果性）

### 4.4 场景标签相似度算法

聚类的基础是"这些建议是否在说同一件事"，通过场景标签的加权匹配来判断：

```
核心维度 (各 0.25 = 共 0.75):
  ├── role: 角色完全匹配
  ├── critical_phase: 关键阶段匹配
  └── result: 胜/负结果匹配

次要维度 (各 0.0625 = 共 0.25):
  ├── role_survived_rounds: ±1 轮模糊匹配（半分 0.03125）
  ├── wolf_aggression: 狼队攻击性匹配
  ├── sheriff_contested: 警长竞争性匹配
  └── first_night_target: 首夜目标类型匹配

阈值: score ≥ 0.5 才进入语义一致性检查
```

### 4.5 双阈值确认系统

为防止"噪声建议"导致策略频繁变动，采用**双阈值去抖 (Debounced)** 确认：

| 通道 | 因果强度 | 最少建议数 | 一致率 | 平均因果强度 |
|------|----------|-----------|--------|-------------|
| **快速通道** | ≥ 0.8 | ≥ 2 | ≥ 60% | — |
| **标准通道** | — | ≥ 3 | ≥ 60% | ≥ 0.5 |

**设计理念**：借鉴电路中的"去抖"概念——只有持续、一致、有因果支撑的建议才能触发策略变更，单次偶然胜负不会导致策略震荡。

### 4.6 版本竞争（多臂赌博机）

采用 **Thompson Sampling 思想**的探索-利用平衡：

```
每局开始时:
  if 存在 candidate 版本:
      50% 概率使用 candidate (探索期)
      50% 概率使用 current_default (利用)
  else:
      使用 current_default

每局结束后:
  更新版本胜率统计

晋升判定:
  candidate.games_played ≥ 5  AND
  candidate.win_rate - default.win_rate ≥ 10%
  → candidate 晋升为 active, 原 active 降为 superseded
```

### 4.7 三层策略加载架构

为控制 Token 开销，策略按需渐进加载：

| 层级 | 内容 | 加载时机 | Token 预算 |
|------|------|----------|-----------|
| Layer 1 | 策略索引（名称 + 描述） | 始终加载 | ~200 |
| Layer 2 | 匹配度最高的 3 个策略全文 | 对局开始 | ~2,000 |
| Layer 3 | 非默认版本对比 | 反思阶段 | 按需 |

---

## 五、GEPA 离线进化引擎

### 5.1 概述

GEPA（**G**enetic-**P**areto **P**rompt **E**volution）是一个离线批量优化引擎，利用**遗传算法 + LLM** 对策略文档进行多目标进化。

### 5.2 进化流程

```
┌─────────────────────────────────────────────────┐
│  GEPA 主循环 (每代)                              │
│                                                  │
│  1. 初始化种群                                   │
│     └── 从 DB 加载所有非归档策略版本              │
│                                                  │
│  2. 适应度评估 (四维度)                           │
│     ├── win_rate: 历史胜率 (数据驱动)             │
│     ├── consistency: 策略一致性 (LLM-as-Judge)   │
│     ├── deception: 欺骗质量 (LLM-as-Judge)       │
│     └── info_utilization: 信息利用 (LLM-as-Judge)│
│                                                  │
│  3. Pareto 前沿选择                              │
│     └── 非支配排序，保留多维度最优策略             │
│                                                  │
│  4. LLM 诊断 + 变异                              │
│     ├── 对非前沿个体：LLM 分析失败模式            │
│     └── 基于诊断，LLM 生成有意义的策略修改         │
│                                                  │
│  5. 系统感知交叉                                 │
│     ├── 在 Pareto 前沿个体间交叉                  │
│     └── LLM 混合不同策略的成功部分                │
│                                                  │
│  6. 创建新版本                                   │
│     └── 变异/交叉策略以 source="gepa_evolution"  │
│         入库                                     │
│                                                  │
│  7. 更新种群: 保留 Pareto 前沿 + 新个体           │
│                                                  │
│  8. 持久化代际状态                               │
└─────────────────────────────────────────────────┘
```

### 5.3 适应度四维度

| 维度 | 评估方式 | 说明 |
|------|----------|------|
| `win_rate` | 数据驱动 | 版本胜率，数据不足时用策略级胜率作先验并惩罚 |
| `consistency` | LLM-as-Judge | 策略文档内部逻辑是否一致，是否自相矛盾 |
| `deception` | LLM-as-Judge | 狼人相关角色的伪装能力、话术自然度 |
| `info_utilization` | LLM-as-Judge | 策略对已知信息的利用程度、决策的信息基础 |

**胜率评估的数据处理**：
- 版本数据充足 (≥ min_games) → 直接使用版本胜率
- 版本不足但策略级充足 → 策略胜率作先验 + 版本数据微调
- 都不足 → 线性惩罚至 0.1
- 无数据 → 最低分 0.05

### 5.4 Pareto 前沿选择

对多目标优化问题的经典解法——一个策略在所有维度上都不劣于另一个，才支配它：

```
策略 A 支配策略 B ⟺ ∀d: A.fitness[d] ≥ B.fitness[d] ∧ ∃d: A.fitness[d] > B.fitness[d]

Pareto 前沿 = 不被任何其他策略支配的个体集合
```

### 5.5 LLM 驱动的变异与交叉

**变异**（非前沿个体）：
1. LLM 分析该策略在对局 trace 中的失败模式
2. 基于诊断，LLM 生成有针对性的策略修改
3. 修改需保持策略文档的结构完整性

**交叉**（前沿个体之间）：
1. 选取 Pareto 前沿中的策略对
2. LLM 混合两个策略各自擅长的部分
3. 生成新的"后代"策略

---

## 六、共轭 Agent 快照与人格进化

### 6.1 概念

一个 **ConjugateAgent** = 全局所有 skill 的 `current_default` 快照。当任何一个策略版本晋升导致全局指纹变化时，就诞生一个新的共轭 Agent。

**类比**：如果策略是基因，共轭 Agent 就是基因组的完整快照——基因突变不影响个体，但基因组组合的变化意味着新个体的诞生。

### 6.2 指纹机制

```python
fingerprint = SHA256(全局 current_default 快照)
# 相同组合 → 相同指纹 → 不创建新 Agent
# 任何 skill 的 default 变化 → 指纹变化 → 创建新 Agent
```

### 6.3 人格生成

每次新共轭 Agent 诞生时，LLM 根据进化事实生成：

| 字段 | 说明 | 风格 |
|------|------|------|
| `agent_name` | 2-6 个中文字符 | 不延续前代命名风格，按进化角色更换方向 |
| `changelog` | 120-220 字 | 技术变更描述，可追溯到 diff 和版本变化 |
| `lore` | 200-300 字 | 3A 游戏角色介绍风格，用隐喻呼应技术进化 |


### 6.4 共轭体的参赛规则

- 只有**最新共轭体**参与 warmup、反思和版本竞争（可进化）
- 旧共轭体作为**冻结版本**仍可参赛（不可进化）
- 通过 `agent_id` 标识：最新为 `agent:{id}`，默认为 `default:common`

---

## 七、策略库策展人 (Curator)

### 7.1 职责

策略库不能只增不减，需要周期性维护：

| 阶段 | 操作 | 条件 |
|------|------|------|
| Phase 1 | 标记过时 | 版本 >30 天未使用 |
| Phase 2 | LLM 审查 | 对每个策略做出 keep/patch/consolidate/archive 判定 |
| Phase 3 | 快照 | 维护策略库一致性 |

### 7.2 审查动作

- **keep**: 策略有效，保留不动
- **patch**: 小幅修正（如措辞优化、补充边界情况）
- **consolidate**: 多个策略重叠，合并为一个
- **archive**: 策略过时或冗余，归档

---

## 八、自进化摘要系统

### 8.1 功能

`EvolutionSummary` 聚合所有自进化活动，LLM 生成中文可读摘要，以"团队周报"风格呈现。

### 8.2 聚合维度

1. **策略版本确认** — 新版本创建及来源
2. **版本竞争结果** — 候选版本晋升/降级
3. **缓冲池确认** — 通过阈值的建议集群
4. **策略缺口** — 缺乏有效指导的场景
5. **策展人行动** — 维护操作记录
6. **GEPA 进化** — 离线进化代际详情
7. **近期对局统计** — 胜负趋势与各角色表现

---

## 九、数据持久化

### 9.1 四级存储架构

| 层级 | 技术 | 范围 | 延迟 |
|------|------|------|------|
| 内存状态 | Python dicts (AgentState) | 单局 | < 1ms |
| 文件系统 | YAML 文件 | 跨局记忆 (L2-L3) | ~10ms |
| 关系数据库 | MySQL + SQLAlchemy ORM | 永久记录 | ~50ms |
| 外部通信 | WebSocket/HTTP | 实时游戏通信 | ~100ms |

### 9.2 核心数据表 (9 张)

| 表名 | 用途 | 关键字段 |
|------|------|----------|
| `evolution_skills` | 策略主表 | skill_name, role, current_default |
| `evolution_skill_versions` | 版本控制 | version, status, content_markdown, win_rate |
| `evolution_buffer_items` | 建议管线 | item_type, suggestion_count, avg_causal_strength |
| `evolution_game_archive` | 对局历史 | game_id, my_role, result, payload_json |
| `evolution_strategy_gaps` | 策略缺口 | scene_description, gap_count |
| `conjugate_agents` | 共轭体 | fingerprint, agent_name, skill_versions_json |
| `evolution_runtime_state` | 运行时状态 | state_key, payload_json |
| `evolution_pipeline_logs` | 审计日志 | 全流程追踪 |

### 9.3 版本状态流转

```
candidate → active → superseded → archived
     ↑                                  ↑
     └── gepa_evolution / debounced_update ──┘
```

---

## 十、配置系统

### 10.1 层级

```
环境变量 (WEREWOLF_AGENT_HOME)
    ↓ 覆盖
YAML 配置文件 (~/.werewolf-agent/config.yaml)
    ↓ 默认
EvolutionConfig 数据类
```

### 10.2 关键配置项

```yaml
debounced_policy:
  enabled: true

  buffer:
    max_age_days: 30              # 建议过期天数
    max_cluster_size: 20          # 集群最大建议数
    semantic_similarity_threshold: 0.5  # 语义相似度阈值

  confirmation:
    normal:
      min_count: 2                # 标准通道最少建议数
      min_consistency_rate: 0.50  # 标准通道最低一致率
      min_avg_causal_strength: 0.35  # 标准通道最低平均因果强度
    fast_track:
      min_causal_strength: 0.70   # 快速通道最低因果强度
      min_count: 1                # 快速通道最少建议数

  versioning:
    warmup_games: 5               # 探索期局数
    warmup_allocation: 0.5        # 探索分配比例 (50%)
    promotion_min_games: 5        # 晋升最少对局数
    promotion_min_win_rate_delta: 0.10  # 晋升最低胜率差

gepa:
  enabled: true
  population_size: 12
  num_generations: 5
  min_skills_in_library: 3
  min_games_for_fitness: 10

curator:
  enabled: true
  interval_hours: 12
```

---

## 十一、部署架构

### 11.1 Docker 部署

```yaml
services:
  werewolf-agent:
    build: .
    container_name: werewolf-agent-langgraph
    ports:
      - "8082:7861"  # WebSocket (主协议)
      - "8083:7860"  # HTTP (调试接口)
    environment:
      - APP_ENV=production
      - WEREWOLF_AGENT_HOME=/root/.werewolf-agent
      - PROVIDER_PUBLIC_HOST=172.17.0.1
    volumes:
      - /opt/werewolf-agent-data:/root/.werewolf-agent
```

### 11.2 调试接口

- `GET /debug/prompts` — 查看 Prompt 历史
- `GET /debug/view` — HTML 调试面板

---

## 十二、测试基础设施

| 测试类型 | 文件 | 覆盖范围 |
|----------|------|----------|
| 单元测试 | `test_provider_agents.py` | Provider Agent 接口 |
| 单元测试 | `test_protocol_prompt.py` | 协议与 Prompt 构建 |
| 集成测试 | `test_evolution_pipeline.py` | 端到端自进化模拟 |
| 算法测试 | `test_gepa_balanced_select.py` | GEPA 均衡选择 |

**端到端进化管线测试流程**：
1. 生成伪造反思结果
2. 注入缓冲池
3. 验证聚类逻辑
4. 测试确认阈值
5. 清理测试数据

---

## 十三、技术创新点总结

### 1. 去抖策略进化 (Debounced Policy Evolution)

**问题**：单次胜负无法判断策略优劣（可能是运气），立即修改会震荡。

**解法**：缓冲→聚类→多轮确认→版本竞争，只有持续一致的建议才触发变更。

### 2. 因果强度 vs 置信度分离

**问题**：传统方法只关注"建议多可信"，忽略"策略对结果有多少因果贡献"。

**解法**：`causal_strength` 独立评估，结合实时标记加成 (×1.3) 和匹配度折扣 (×0.7)。

### 3. 共轭 Agent 人格进化

**问题**：策略进化是分散的（每个 skill 独立进化），缺乏全局视角。

**解法**：全局指纹机制，任何策略晋升触发新"人格"诞生，LLM 生成名字和叙事，让进化可视化、故事化。

### 4. GEPA 遗传-帕累托多目标优化

**问题**：策略不能只看胜率，还需考虑一致性、欺骗性、信息利用等多维度。

**解法**：Pareto 前沿选择保留多维度最优解，LLM 驱动的变异和交叉实现语义级别的策略优化。

### 5. 四层记忆 + 三层策略加载

**问题**：Token 预算有限，不能把所有信息都塞进 Prompt。

**解法**：记忆分层存储（临时→永久），策略按需加载（索引→全文→版本对比），信息密度最大化。

---

## 十四、答辩要点速查

| 问题方向 | 回答要点 |
|----------|----------|
| "为什么用 LangGraph？" | 三节点图天然对应感知-反思-行动，条件路由支持多阶段决策，状态传递清晰 |
| "自进化和直接 fine-tuning 有什么区别？" | 无需重新训练模型，策略即文档可解释；去抖机制避免震荡；版本竞争实现 A/B 测试 |
| "GEPA 的 Pareto 前沿有什么意义？" | 多目标优化不存在唯一最优解；前沿保留各维度最优个体，避免单维度退化 |
| "共轭 Agent 有什么用？" | 全局一致性追踪；人格进化让系统演进可视化；旧版本冻结可参赛实现回退 |
| "为什么不在对局中实时修改策略？" | 对局中修改会破坏行为一致性；去抖需要多局统计验证；实时修改不可解释 |
| "因果强度怎么计算？" | LLM 分析行动→中间结果→最终结局的因果链；结合实时标记加成和匹配度折扣 |
| "系统怎么防止策略退化？" | 版本竞争 (候选 vs 默认)；晋升需统计显著 (≥5局, ≥10%胜率差)；策展人定期审查 |
| "Token 开销怎么控制？" | 三层策略加载；工作记忆自动压缩；只有匹配度 Top3 策略注入全文 |
