# Landing 页叙事改进规划

> 基于 DEFENSE_DOC.md 亮点提炼 + 现有 landing 页覆盖度审计
> 2026-06-09

---

## 一、现状审计

### 1.1 现有叙事结构

| 顺序 | 组件 | Chapter | 叙事角色 |
|------|------|---------|---------|
| 1 | Hero | — | 钩子："When agents evolve themselves" |
| 2 | HeroBridge | Ch01 | Observe → Replay → Evolve 研究闭环 |
| 3 | Features | Ch02 | 平台侧四维证据系统 |
| 4 | DepthRevealTransition | Ch02.5 | "But who thinks in the dark?" 平台→Agent 过渡 |
| 5 | AgentEvolution | Ch03 | Perceive→Reflect→Act + InGameFlagger |
| 6 | EvolutionDeepDive | Ch04 | 去抖/GEPA/共轭/因果/记忆（5创新点堆叠） |
| 7 | AgentCodexShowcase | Ch05? | 共轭Agent人格卡片 |
| 8 | HotRooms | — | 实时对局 |
| 9 | CTA + Footer | — | 转化 |

### 1.2 DEFENSE_DOC 亮点覆盖度

| # | 亮点 | 覆盖状态 | 问题 |
|---|------|---------|------|
| 1 | LangGraph 三节点决策 | ✅ 已覆盖 | Ch03 AgentEvolution，质量好 |
| 2 | 三层记忆+归档层 | ⚠️ 标签错误 | EvolutionDeepDive 仍写"四层记忆"，归档层间接作用未讲清 |
| 3 | 自进化管线全流程 | ⚠️ 分散 | Ch04 各组件孤立，缺管线全景图 |
| 4 | 去抖策略进化 | ✅ 已覆盖 | Ch04 三卡之一，质量好 |
| 5 | GEPA帕累托优化 | ✅ 已覆盖 | Ch04 三卡之一，质量好 |
| 6 | 共轭Agent人格 | ✅ 已覆盖 | Ch04 三卡 + CodexShowcase，质量好 |
| 7 | 因果强度分离 | ✅ 已覆盖 | Ch04 小卡，质量好 |
| 8 | 三层策略加载 | ⚠️ 与记忆层混淆 | MemoryLoadDiagram 把策略加载和记忆分层混在一起 |
| 9 | **六模块Prompt构建管线** | ❌ **完全缺失** | 答辩核心亮点，无任何展示 |

### 1.3 核心问题诊断

1. **Ch04 信息过载**：5个创新点堆在一个 section，视觉和认知密度过高
2. **闭环缺失**：讲了 Agent 怎么思考（Ch03）、怎么进化（Ch04），但没讲**进化结果如何回到决策中**
3. **记忆描述不诚实**：L4 归档层在代码中是 write-only，不直接注入 Prompt，但 landing 页画得跟 L1-L3 一样
4. **六模块Prompt构建管线完全缺失**：这是"进化了然后呢"的答案，是闭环的关键连接点

---

## 二、改进方案

### 2.1 改动总览

| 改动 | 影响组件 | 优先级 | 工作量 |
|------|---------|--------|--------|
| A. 修正"四层记忆"标签 | EvolutionDeepDive.tsx | P0 | 小 |
| B. 重做 MemoryLoadDiagram | EvolutionDeepDive.tsx | P0 | 中 |
| C. Ch04 瘦身：拆出因果+记忆 | EvolutionDeepDive.tsx | P1 | 中 |
| D. 新增 Ch05 "The loop that learns" | 新组件 | P1 | 大 |
| E. Ch05→CodexShowcase 过渡 | AgentCodexShowcase.tsx | P2 | 小 |

### 2.2 改动 A：修正"四层记忆"标签

**文件**：`EvolutionDeepDive.tsx` 第 85-86 行

**现状**：
```tsx
title="四层记忆 + 三层加载"
body="四层记忆按来源组织，三层加载按证据粒度进入 content window；不是把所有历史塞进 Prompt。"
```

**改为**：
```tsx
title="三层记忆 + 归档层 + 三层加载"
body="三层记忆直接注入 Prompt，归档层间接驱动进化管线（消费归档→产出策略→注入），策略按需加载——不是把所有历史塞进 Prompt。"
```

### 2.3 改动 B：重做 MemoryLoadDiagram

**核心变化**：区分"直接注入"和"间接注入"

**新数据**（基于 `prompt_builder.py` 实际组装逻辑）：

| 模块 | 注入方式 | 估算 tokens | 占比 |
|------|---------|------------|------|
| CRITICAL_THINKING_FRAMEWORK | 静态/始终 | ~1500 | 7.5% |
| IN_GAME_FLAG_PROMPT | 静态/始终 | ~200 | 1% |
| 游戏状态 | 动态/直接 | ~800 | 4% |
| 任务指令 | 动态/直接 | ~500 | 2.5% |
| L1 工作记忆 | 动态/直接 | ~2000 | 10% |
| L2 对手画像 | 动态/直接 | ~1200 | 6% |
| L3 自我模型 | 动态/直接 | ~600 | 3% |
| evolution_strategies | 动态/间接 | ~1500 | 7.5% |
| Free space | — | ~11700 | 58% |

**视觉方案**：
- Context window 网格图中，L1/L2/L3 用实线边框（rose/orange/sky），表示"直接注入"
- evolution_strategies 用 amber 虚线边框 + 脉冲动画，表示"间接注入（进化管线产出）"
- 归档层不在 context window 中出现，而是在旁边画一个虚线箭头：归档层 → 进化管线 → evolution_strategies
- 图例增加"直接注入"和"间接注入"两种标记

### 2.4 改动 C：Ch04 瘦身

**现状**：EvolutionDeepDive 包含 3张大卡 + 2张小卡

**改为**：
- **保留 3 张大卡**：去抖策略进化 / GEPA帕累托优化 / 共轭Agent人格进化
- **移除 2 张小卡**（因果强度分离 + 记忆加载）→ 移至新 Ch05

**理由**：
1. Ch04 聚焦"进化机制"（如何进化），Ch05 聚焦"进化闭环"（进化结果如何回到决策）
2. 因果强度分离是"如何确保进化方向正确"，属于闭环的保障机制
3. 记忆+加载是"进化结果如何注入决策"，属于闭环的连接机制
4. 瘦身后 Ch04 视觉密度降低，用户有呼吸空间

### 2.5 改动 D：新增 Chapter 05 / The loop that learns

**组件名**：`EvolutionClosedLoop.tsx`

**叙事定位**：闭环收束——回答"进化了然后呢？"

**内容结构**：

#### 5.1 顶部：环形闭环全景图

```
对局 → 归档 → 反思 → 进化 → 策略注入 → 决策 → 对局
  ↑                                                    |
  └────────────────────────────────────────────────────┘
```

- 环形 SVG/Canvas 动画，6个节点沿环排列
- 每个节点有简短标签和图标
- 当前激活节点有 amber 脉冲
- 滚动时依次激活各节点（可选交互）

#### 5.2 中部：六模块 Prompt 组装动画

**核心视觉**：一个 "content window" 容器，6个模块按实际组装顺序逐个飞入：

1. `CRITICAL_THINKING_FRAMEWORK` — 静态基座（stone 色）
2. `IN_GAME_FLAG_PROMPT` — 静态标记（stone 色）
3. 游戏状态 + 任务指令 — 动态上下文（emerald 色）
4. L1 工作记忆 + L2 对手画像 + L3 自我模型 — 三层记忆（rose/orange/sky）
5. `evolution_strategies` — **进化注入点**（amber 高亮 + 脉冲）

**第5模块飞入时的特殊效果**：
- amber 虚线弧线从第5模块飞回第3模块（游戏状态），表示"进化策略影响下一局决策"
- 这条弧线就是闭环的视觉化

#### 5.3 底部：因果强度分离（从 Ch04 移来）

- 保持现有 CausalStrengthDiagram 组件不变
- 叙事定位从"进化机制之一"变为"闭环的保障机制——确保进化方向不被单局噪声绑架"

### 2.6 改动 E：Ch05 → CodexShowcase 过渡

**现状**：EvolutionDeepDive 之后直接接 AgentCodexShowcase，无过渡

**改为**：在 CodexShowcase 顶部加一行过渡文案：

```
上面是闭环机制。下面是闭环产出的真实人格。
```

或者更优雅地，把 CodexShowcase 的 eyebrow 从无改为：

```
Chapter 06 / Born from the loop
```

---

## 三、改动后完整叙事结构

| 顺序 | 组件 | Chapter | 叙事角色 | 变化 |
|------|------|---------|---------|------|
| 1 | Hero | — | 钩子 | 无变化 |
| 2 | HeroBridge | Ch01 | 研究闭环概览 | 无变化 |
| 3 | Features | Ch02 | 平台侧证据系统 | 无变化 |
| 4 | DepthRevealTransition | Ch02.5 | 平台→Agent 过渡 | 无变化 |
| 5 | AgentEvolution | Ch03 | Agent 决策流 | 无变化 |
| 6 | EvolutionDeepDive | Ch04 | 进化机制（3卡） | **瘦身：移出因果+记忆** |
| 7 | **EvolutionClosedLoop** | **Ch05** | **闭环收束** | **新增** |
| 8 | AgentCodexShowcase | Ch06 | 进化产出展示 | **加 eyebrow** |
| 9 | HotRooms | — | 实时对局 | 无变化 |
| 10 | CTA + Footer | — | 转化 | 无变化 |

**叙事弧线**：
- Ch01-02：**平台能观测什么**（证据系统）
- Ch02.5：**过渡**（谁在暗处思考？）
- Ch03：**Agent 怎么思考**（决策流）
- Ch04：**Agent 怎么进化**（进化机制）
- Ch05：**进化如何回到决策**（闭环收束）← **新增，回答"所以呢？"**
- Ch06：**进化的真实产出**（人格卡片）

---

## 四、EvolutionClosedLoop 组件设计

### 4.1 组件结构

```tsx
export function EvolutionClosedLoop() {
  return (
    <LandingSection id="evolution-closed-loop">
      <LandingSectionHeader
        eyebrow="Chapter 05 / The loop that learns"
        title={<>The loop<br />that learns</>}
        description="进化不是终点。策略注入决策、决策产生对局、对局驱动进化——闭环自学习。"
      />

      {/* 5.1 环形闭环全景图 */}
      <ClosedLoopDiagram />

      {/* 5.2 六模块 Prompt 组装动画 */}
      <PromptAssemblyAnimation />

      {/* 5.3 因果强度分离（从 Ch04 移来） */}
      <CompactHighlight
        icon={GitBranch}
        title="因果强度分离"
        body="赢了不等于策略正确。因果链、实时 FLAG 与匹配度折扣分开计量，让进化建议不被单局结果绑架。"
        accent="orange"
        visual={<CausalStrengthDiagram />}
      />
    </LandingSection>
  );
}
```

### 4.2 ClosedLoopDiagram 视觉规格

- **布局**：水平环形，6个节点等距排列
- **节点**：圆形图标 + 标签，stone-800 边框，amber-300/80 文字
- **连接线**：stone-700 实线箭头，顺时针方向
- **特殊路径**：从"策略注入"到"决策"的箭头用 amber 虚线，表示"进化结果回到决策"
- **归档层标注**：在"对局→归档"的连接旁加一个小标签"write-only"，在"归档→进化"旁加"消费归档数据"
- **尺寸**：桌面端 ~600px 高，移动端简化为垂直流程图

### 4.3 PromptAssemblyAnimation 视觉规格

- **容器**：圆角矩形，stone-950 背景，模拟 content window
- **模块飞入**：从左侧滑入，每个模块是一个带色标的横条
- **顺序和色标**：
  1. CRITICAL_THINKING_FRAMEWORK — stone-600（静态基座）
  2. IN_GAME_FLAG_PROMPT — stone-600（静态标记）
  3. 游戏状态 + 任务指令 — emerald-400（动态上下文）
  4. L1 + L2 + L3 — rose-400 / orange-400 / sky-400（三层记忆）
  5. evolution_strategies — **amber-400 + 脉冲动画**（进化注入点）
- **第5模块飞入后**：amber 虚线弧线从第5模块飞回第3模块
- **右侧**：token 占比柱状图（与 MemoryLoadDiagram 共享数据源）
- **动画**：whileInView 触发，每个模块 delay 0.15s

### 4.4 因果强度分离

- 直接复用现有 `CausalStrengthDiagram` 组件，无需修改
- 仅调整外层 `CompactHighlight` 的 accent 从 "orange" 保持不变

---

## 五、MemoryLoadDiagram 重做规格

### 5.1 数据源变更

**现状**（硬编码假数据）：
```
L1 对局上下文: 18k tokens (9.0%)
L2 对手建模: 12k tokens (6.0%)
L3 自我画像: 6k tokens (3.0%)
L4 对局历史: 11k tokens (5.5%)  ← 错误：L4 不直接注入
```

**改为**（基于 prompt_builder.py 实际逻辑）：
```
静态框架 (CRITICAL_THINKING): ~1500 tokens (7.5%)
对局标记 (IN_GAME_FLAG): ~200 tokens (1.0%)
游戏状态: ~800 tokens (4.0%)
任务指令: ~500 tokens (2.5%)
L1 工作记忆: ~2000 tokens (10.0%)  ← 直接注入
L2 对手画像: ~1200 tokens (6.0%)   ← 直接注入
L3 自我模型: ~600 tokens (3.0%)    ← 直接注入
进化策略: ~1500 tokens (7.5%)      ← 间接注入（进化管线产出）
Free space: ~11700 tokens (58.5%)
Buffer: ~3200 tokens (16.0%)       ← 调整以凑满 200k
```

### 5.2 视觉变更

- Context window 网格图中增加两个色标类别：
  - `static`: stone-500（静态框架+标记）
  - `evolution`: amber-400 + 虚线边框（进化策略，间接注入）
- 图例增加：
  - ■ 实线边框 = 直接注入 Prompt
  - □ 虚线边框 = 间接注入（进化管线消费归档数据→产出策略→注入）
- 归档层不在 context window 网格中出现
- 在网格图右侧增加一个小型流程图：`归档层 → 进化管线 → evolution_strategies`（虚线箭头）

---

## 六、page.tsx 变更

**现状**：
```tsx
<AgentNarrativeBridge />   {/* DepthRevealTransition + AgentEvolution */}
<EvolutionDeepDive />
<AgentCodexShowcase />
```

**改为**：
```tsx
<AgentNarrativeBridge />   {/* DepthRevealTransition + AgentEvolution */}
<EvolutionDeepDive />      {/* 瘦身：仅3张大卡 */}
<EvolutionClosedLoop />    {/* 新增：闭环收束 */}
<AgentCodexShowcase />     {/* 加 eyebrow: Chapter 06 / Born from the loop */}
```

---

## 七、实施优先级

| 优先级 | 改动 | 预计工时 | 依赖 |
|--------|------|---------|------|
| P0 | A. 修正"四层记忆"标签 | 0.5h | 无 |
| P0 | B. 重做 MemoryLoadDiagram | 2h | 需确认 token 数据 |
| P1 | C. Ch04 瘦身 | 1h | 无 |
| P1 | D. 新增 EvolutionClosedLoop | 6h | 需设计闭环图+Prompt组装动画 |
| P2 | E. CodexShowcase 过渡 | 0.5h | 无 |

**建议实施顺序**：A → C → B → D → E

---

## 八、风险项

| # | 风险 | 缓解措施 |
|---|------|---------|
| 1 | Prompt 组装动画在移动端空间不足 | 移动端改为垂直堆叠，取消飞入动画，改为逐个 fade-in |
| 2 | 环形闭环图在小屏幕上不可读 | 移动端退化为垂直流程图 |
| 3 | Token 数据是估算值，答辩时可能被质疑 | 标注"基于典型对局的估算值"，不声称精确 |
| 4 | EvolutionClosedLoop 新增组件增加页面长度 | 闭环是叙事收束，用户到此已有足够投入，长度可接受 |
| 5 | MemoryLoadDiagram 重做可能影响 EvolutionDeepDive 布局 | 仅修改内部数据和视觉，不改变外层布局结构 |