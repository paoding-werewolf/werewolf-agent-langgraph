# Landing 页亮点叙事规划

> 基于 DEFENSE_DOC.md 5 大技术创新点，重新规划 landing 页故事叙事
> 2026-06-10

---

## 一、现状诊断

### 1.1 DEFENSE_DOC 5 大创新点 → 当前 landing 覆盖度

| # | 创新点 | 当前组件 | 覆盖度 | 问题 |
|---|--------|----------|--------|------|
| ① | 去抖策略进化 | EvolutionDeepDive 卡片1 | ✅ | 与因果强度挤在一起，信息过载 |
| ② | 因果强度分离 | EvolutionDeepDive 卡片1（部分） | ⚠️ 弱 | 没有独立叙事位，Why/How 混淆 |
| ③ | GEPA 帕累托优化 | EvolutionDeepDive 卡片2 | ✅ | OK |
| ④ | 共轭Agent人格进化 | AgentCodexShowcase | ✅ | OK |
| ⑤ | 三层记忆+归档层+三层策略加载 | EvolutionDeepDive MemoryLoadDiagram | ⚠️ 弱 | 只占一个子图，归档层间接价值未展示 |

### 1.2 关键缺失

1. **因果强度分离没有独立叙事位** — 它是"评估信号质量"的洞察（Why），去抖是"防止噪声触发变更"的机制（How），两者本质不同
2. **六模块 Prompt 构建管线完全缺失** — 这是闭环收束点："进化了然后呢？"
3. **叙事缺少主线** — 当前是平铺罗列式（Features 列 6 特性，DeepDive 列 3 卡片），没有一条线把亮点串起来

---

## 二、叙事主线

DEFENSE_DOC 隐含一条主线：**从"被动执行策略"到"主动进化策略"的闭环**。

```
感知→反思→行动（决策框架）
    ↓ 反思中发现矛盾
FLAG 实时标记（对局中）
    ↓ 对局后深度反思
因果强度分离（知道为什么赢）     ← Why
    ↓ 去抖确认
去抖策略进化（一局不能定义策略）  ← How
    ↓ 多目标优化
GEPA 帕累托优化（不能只看胜率）  ← How
    ↓ 全局快照
共轭Agent人格进化（策略晋升=新人格）← Result
    ↓ 闭环收束
六模块Prompt构建（进化回到决策）  ← Loop
```

---

## 三、三幕叙事弧

### Act 1 — 问题引入（Hero + Features）

**核心信息**：AI 玩狼人杀，最难的不是规则，是进化。

三个痛点（对应三个创新点的 Problem）：
- 一局赢了可能是运气 → 因果强度分离
- 策略只看胜率会退化 → GEPA 帕累托
- 进化了然后呢 → Prompt 构建闭环

**组件**：Hero / HeroBridge / Features（已有，文案微调即可）

### Act 2 — 核心洞察（EvolutionDeepDive 重构）

**核心信息**：三个洞察，解决三个问题。

从 3 卡片扩展为 4 卡片：

| 卡片 | 洞察 | 对应创新点 | 视觉重点 |
|------|------|-----------|---------|
| 1 | 因果强度分离 — 知道为什么赢比知道赢了更重要 | ② | 三路计量公式动画 |
| 2 | 去抖策略进化 — 一局胜负不能定义策略好坏 | ① | 管线流程图 |
| 3 | GEPA 帕累托优化 — 策略不能只看胜率 | ③ | Pareto 前沿散点图 |
| 4 | 三层记忆+归档层 — Token 预算有限，不能全塞 | ⑤ | 记忆分层图 + 归档层间接路径 |

**组件**：EvolutionDeepDive（重构，4 卡片）

### Act 3 — 闭环收束（AgentCodexShowcase + PromptAssembly）

**核心信息**：进化不是终点，是下一轮决策的起点。

| 区块 | 内容 | 对应创新点 | 视觉重点 |
|------|------|-----------|---------|
| AgentCodexShowcase | 共轭Agent人格进化 — 策略晋升=新人格 | ④ | 人格卡片 + 变更日志 |
| PromptAssembly（新增） | 六模块Prompt构建 — 进化结果回到决策 | 闭环收束 | 6 模块拼合动画 + amber 高亮进化注入点 |

**组件**：AgentCodexShowcase（已有）+ PromptAssembly（新增）

---

## 四、页面组装顺序

### 当前

```
Navbar → Hero → HeroBridge → Features → AgentNarrativeBridge 
→ EvolutionDeepDive(3卡片) → AgentCodexShowcase → HotRooms → CTA → Footer
```

### 调整后

```
Navbar → Hero → HeroBridge → Features → AgentNarrativeBridge 
→ EvolutionDeepDive(4卡片) → AgentCodexShowcase → PromptAssembly(新增) 
→ HotRooms → CTA → Footer
```

变更点：
1. EvolutionDeepDive：3 卡片 → 4 卡片（因果强度独立 + 记忆层独立）
2. PromptAssembly：新增组件，放在 AgentCodexShowcase 之后
3. HotRooms/CTA/Footer：不变

---

## 五、组件详细规格

### 5.1 EvolutionDeepDive（重构）

**Chapter 标题**：`Chapter 04 / How it evolves`

**4 卡片内容**：

#### 卡片 1：因果强度分离

| 元素 | 内容 |
|------|------|
| Eyebrow | `Causal Strength Separation` |
| 标题 | 知道为什么赢，比知道赢了更重要 |
| 核心公式 | `进化建议强度 = causal_strength × flag_bonus × match_discount` |
| 三路说明 | ① 因果链分析（LLM 判断策略→结果因果强度）② FLAG加成（实时标记 ×1.3）③ 匹配度折扣（场景不匹配 ×0.7） |
| 视觉 | 三路计量动画：三条信号线汇入一个节点，每路有独立权重标注 |
| Accent | amber（因果强度是核心洞察） |

#### 卡片 2：去抖策略进化

| 元素 | 内容 |
|------|------|
| Eyebrow | `Debounced Policy Evolution` |
| 标题 | 一局胜负，不能定义策略好坏 |
| 核心流程 | 缓冲池收集 → 场景聚类 → 双阈值确认（快速3局/标准5局）→ 版本竞争 |
| 关键数据 | 策略变更延迟从"1局"提升到"至少3-5局一致" |
| 视觉 | 管线流程图，信号从噪声→滤波→确认 |
| Accent | emerald（去抖=稳定性） |

#### 卡片 3：GEPA 帕累托优化

| 元素 | 内容 |
|------|------|
| Eyebrow | `Genetic-Pareto Prompt Evolution` |
| 标题 | 策略不能只看胜率 |
| 四维适应度 | win_rate / consistency / deceptiveness / info_utilization |
| 核心机制 | Pareto 前沿保留各维度最优 → LLM 驱动变异与交叉 |
| 视觉 | 2D 散点图（win_rate vs deceptiveness），Pareto 前沿连线高亮 |
| Accent | violet（多目标=复杂性） |

#### 卡片 4：三层记忆 + 归档层

| 元素 | 内容 |
|------|------|
| Eyebrow | `Layered Memory Architecture` |
| 标题 | Token 预算有限，不能全塞进 Prompt |
| 三层直接注入 | L1 工作记忆(~2000t) + L2 对手画像(~1200t) + L3 自我模型(~600t) |
| 归档层间接注入 | L4 对局归档(write-only) → 进化管线消费 → 产出 evolution_strategies → 注入 Prompt |
| 策略三层加载 | 索引层(10k) → 相关全文(26k) → 版本对比(8k) |
| 视觉 | 分层图：L1/L2/L3 直接箭头→Prompt，L4 间接箭头→进化管线→Prompt |
| Accent | sky（记忆=信息流） |

#### 卡片间动画

- 交错入场：每卡片延迟 150ms
- 卡片 1 最短（核心就一句话+公式），卡片 2/3 中等，卡片 4 较长（分层图）
- 滚动触发：IntersectionObserver，threshold 0.2

---

### 5.2 PromptAssembly（新增组件）

**Chapter 标题**：`Chapter 05 / The loop that learns`

**核心信息**：进化不是终点，是下一轮决策的起点。六模块 Prompt 组装，第⑥模块是进化注入点。

#### 视觉方案：模块拼合动画

```
┌─────────────────────────────────────────────────┐
│  ① CRITICAL_THINKING_FRAMEWORK    ~1500 tokens  │  ← 静态基座
│  ② IN_GAME_FLAG_PROMPT             ~200 tokens  │  ← 静态标记
│  ③ game_state                       ~800 tokens  │  ← 动态
│  ④ task_instructions                ~500 tokens  │  ← 动态
│  ⑤ L1+L2+L3 Memory               ~3800 tokens  │  ← 三层记忆
│  ⑥ evolution_strategies           ~1500 tokens  │  ← ★ 进化注入点 (amber)
│                                                   │
│  Total: ~91k / 200k tokens (46%)                 │
└─────────────────────────────────────────────────┘
```

#### 交互

1. 初始状态：6 个模块从左依次滑入，每个模块是一个色块
2. 前 5 个模块：stone-700 色块，带模块名和 token 数
3. 第 6 个模块：amber-500 色块，高亮标注"进化注入点"
4. 拼合完成后：底部出现总 token 数和占比
5. 点击第⑥模块：展开显示进化策略的来源路径（归档层→进化管线→evolution_strategies→Prompt）

#### 文案

| 元素 | 内容 |
|------|------|
| Eyebrow | `Chapter 05 / The loop that learns` |
| 标题 | 进化不是终点，是下一轮决策的起点 |
| 副标题 | 六模块 Prompt 构建管线，第⑥模块是归档层间接价值的终点 |
| CTA 微文案 | "L4 归档数据被进化管线消费，产出的策略通过第⑥模块注入——闭环收束" |

#### Accent

amber-500（与进化注入点一致）

---

### 5.3 AgentCodexShowcase（微调）

当前已有，无需大改。只需确保：
- 人格卡片展示 `name` + `lore` + `changelog` + `fingerprint`
- 变更日志可追溯到具体策略版本变化
- 与 PromptAssembly 的叙事衔接：共轭Agent是"进化的结果"，PromptAssembly是"结果如何回到决策"

---

## 六、DEFENSE_DOC 亮点 → Landing 叙事映射总表

| DEFENSE_DOC 创新点 | Landing 位置 | 叙事角色 | 信息密度 |
|---|---|---|---|
| ① 去抖策略进化 | DeepDive 卡片2 | How — 防止噪声触发变更 | 中 |
| ② 因果强度分离 | DeepDive 卡片1 | Why — 评估信号质量 | 短（核心一句话） |
| ③ GEPA 帕累托优化 | DeepDive 卡片3 | How — 多目标优化 | 中 |
| ④ 共轭Agent人格进化 | AgentCodexShowcase | Result — 进化可视化 | 中 |
| ⑤ 三层记忆+归档层 | DeepDive 卡片4 | How — 信息分层 | 中长 |
| 六模块Prompt构建 | PromptAssembly（新增） | Loop — 闭环收束 | 中 |
| LangGraph三节点决策 | Features / HeroBridge | Context — 决策框架 | 轻 |
| InGameFlagger | DeepDive 卡片1（FLAG加成部分） | Detail — 实时标记 | 轻 |

---

## 七、与现有规划文档的关系

| 文档 | 状态 | 与本文档关系 |
|------|------|-------------|
| `landing-narrative-plan.md` | 已有，640行 | 本文覆盖其 §3.2 EvolutionDeepDive 部分，其余（转场/色彩/组件规划）仍有效 |
| `landing-narrative-improvement-plan.md` | 已有，338行 | 本文是其升级版——Change A（记忆标签修正）已执行，Change B-E 被本文吸收并扩展 |

**本文档是 EvolutionDeepDive + 新增 PromptAssembly 的权威规划**，覆盖 `landing-narrative-plan.md` 的 §3.2 和 `landing-narrative-improvement-plan.md` 的全部内容。

---

## 八、实施优先级

| 优先级 | 变更 | 工作量 | 依赖 |
|--------|------|--------|------|
| P0 | EvolutionDeepDive 3→4 卡片（因果强度独立） | 中 | 无 |
| P0 | PromptAssembly 新增组件 | 中 | 无 |
| P1 | 卡片1 因果强度三路计量动画 | 中 | P0 |
| P1 | PromptAssembly 模块拼合动画 | 中 | P0 |
| P2 | 卡片4 记忆分层图重做（归档层间接路径） | 小 | P0 |
| P2 | page.tsx 组装顺序调整 | 小 | P0+P1 |

---

## 九、风险项

| 风险 | 影响 | 缓解 |
|------|------|------|
| 4 卡片导致 EvolutionDeepDive 过长 | 用户滚动疲劳 | 卡片1刻意做短（核心一句话+公式），卡片间交错入场制造呼吸感 |
| PromptAssembly 太技术化 | 非技术用户看不懂 | 模块拼合动画本身有视觉冲击力，技术细节可折叠 |
| 新增组件增加页面加载时间 | 性能 | PromptAssembly 用 lazy import |