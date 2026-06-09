# Landing Page 叙事规划 — Agent Self-Evolution

> 日期：2026-06-09
> 状态：规划阶段（未落地前端）

---

## 一、架构总览

### 1.1 双 Part 架构

Landing 页按**编排层/平台侧**与 **Agent 侧/自进化**分为两大叙事 Part，中间以 **Sticky Curtain Reveal** 转场衔接。

```
┌─────────────────────────────────────────────────┐
│  Hero (sticky scroll-out)                        │
│  "When agents evolve themselves"                 │
│  SelfEvolutionCanvas + EvolutionCarouselCanvas   │
├─────────────────────────────────────────────────┤
│  HeroBridge                                      │
│  Chapter 01 / Research Loop                      │
│  "From game night to evidence"                   │
├─────────────────────────────────────────────────┤
│  Features                                        │
│  Chapter 02 / The lab behind the night           │
│  ← 平台侧：透明观测 / Agent接入 / 研究协议 / 信号沉淀 │
├─────────────────────────────────────────────────┤
│  ═══ Sticky Curtain Reveal 转场 ═══               │
│  "But who thinks in the dark?"                   │
│  白天→夜晚 / 表面→深处 / 观测→参与                │
├─────────────────────────────────────────────────┤
│  [NEW] AgentEvolution                            │
│  Chapter 03 / The mind within                    │
│  ← Agent侧：感知→反思→行动 闭环全景               │
├─────────────────────────────────────────────────┤
│  [NEW] EvolutionDeepDive                         │
│  Chapter 04 / How it evolves                     │
│  ← Agent侧：去抖 / GEPA / 共轭Agent 三卡片深潜    │
├─────────────────────────────────────────────────┤
│  CTA                                             │
└─────────────────────────────────────────────────┘
```

### 1.2 叙事隐喻

| 维度 | Part 1（平台侧） | 转场 | Part 2（Agent侧） |
|------|-------------------|------|-------------------|
| 狼人杀隐喻 | 白天——公开讨论、投票、查验 | 闭眼→睁眼 | 夜晚——隐秘思考、策略调整 |
| 视角 | 观测者（看实验） | 提问 | 参与者（看思考） |
| 色彩 | stone + emerald/indigo（冷） | 纯暗 + amber微光 | stone + amber/orange（暖） |
| 信息密度 | 广而浅 | 留白 | 窄而深 |

---

## 二、转场设计：Sticky Curtain Reveal

> **v2 更新**（2026-06-09）：转场方案从 "Sticky Overlap Scroll Reveal" 升级为 "Sticky Curtain Reveal"。
> 核心变化：旧方案是"覆盖"（转场层升起盖住前段），新方案是"揭幕"（前段整体抬升掀开，露出下方已就绪的后段）。

### 2.1 精确术语

**前端设计术语**：**Scroll-driven Sticky Curtain Reveal with Depth Layering**

| 术语成分 | 含义 |
|----------|------|
| **Scroll-driven** | 所有动画由滚动进度驱动，不是时间驱动 |
| **Sticky** | Part 1（Features + 转场文案）`position: sticky; top: 0`，在滚动过程中"钉"在视口 |
| **Curtain** | Part 1 整体作为"幕布"，滚动时向上抬升/缩小/淡出，像掀开幕布 |
| **Reveal** | Part 2（AgentEvolution）在幕布下方**已经渲染就绪**，幕布掀开后自然"流露"出来 |
| **Depth Layering** | Part 1 和 Part 2 在不同深度层——Part 1 在上层（遮幕），Part 2 在下层（真相） |

**与旧方案（Sticky Overlap）的本质区别**：

| 维度 | 旧方案 (Sticky Overlap) | 新方案 (Sticky Curtain Reveal) |
|------|------------------------|-------------------------------|
| 谁在动 | 转场层从下方升起覆盖 | Part 1 整体向上抬升/掀开 |
| z-index 关系 | 转场层 > Features | Part 1 > Part 2（遮幕在上） |
| Part 2 状态 | 转场滚完后才出现 | 一直渲染着，被 Part 1 遮住 |
| 视觉感受 | 新内容"推上来"覆盖旧内容 | 掀开幕布，露出已有内容 |
| 叙事含义 | "进入"新世界 | "发现"隐藏的世界 |
| 滚动映射 | 转场层 opacity 0→1 | Part 1 translateY 0→-X, opacity 1→0, scale 1→0.95 |

**叙事优势**："流露"而非"推入"——暗示 Agent 的内心世界一直都在运转，只是之前被表面遮住了。你掀开表面，它自然就在那里。这比"新内容推上来"更贴合"水面以上/水面以下"的隐喻。

**电影术语类比**：不是"硬切"（跳到下一段），不是"叠化+推镜头"（前段被推远，后段推上来），而是**揭幕**——幕布被掀开，露出幕布后面已经存在的舞台。

### 2.2 概念

**"Curtain Reveal"**——掀开表面，露出深处。平台侧是"幕布"（可见的实验流程），Agent侧是"幕布后面的舞台"（一直运转的内心世界）。幕布掀开，舞台自然呈现。

### 2.3 视觉方案

```
转场区 (高度 200svh，给足滚动空间)

滚动进度 0% ────────────────────────────────────────── 100%
     │              │              │              │
     │  Phase A     │  Phase B     │  Phase C     │
     │  0-30%       │  30-65%      │  65-100%     │
     │              │              │              │
     │  Part 1      │  转场文案     │  Part 1      │
     │  sticky+抬升 │  在幕布上显示 │  完全掀开    │
     │  scale↓blur↑ │  淡入→停留   │  Part 2      │
     │  opacity↓    │  →淡出       │  完全流露    │
```

**Phase A（0-30%）：Part 1 幕布开始抬升**
- Part 1（Features + 转场文案区）整体 sticky 在视口顶部
- 滚动驱动：`translateY: 0→-40px`，`scale: 1→0.96`，`filter: blur(0→3px)`，`opacity: 1→0.7`
- 视觉感受：页面开始"上抬"，像幕布被掀起一角
- Part 2 在 z-index 更低的层，已经开始从底部露出

**Phase B（30-65%）：幕布上的转场文案**

文案时序（叙事节奏：定位→好奇→理解→期待）：

```
eyebrow:   ████████░░░░░░░░░░░░  (30-48% 入, 55-63% 出)
主标题:     ░░████████████░░░░░  (33-52% 入, 58-65% 出)  ← 可见窗口最大，压轴
副文案:     ░░░░██████░░░░░░░░░  (36-53% 入, 56-63% 出)
呼吸光:     ████████████████████  (30-65% 持续)
```

- 文案显示在 Part 1 幕布上（幕布还在视口中，只是缩小+模糊了）
- 文案进入方式：`y: 24→0` + `filter: blur(6px→0)`，不是简单 fade
- 呼吸动画：背景 amber 径向渐变，`opacity: 0.03↔0.08`，周期 3s，`scale: 0.8↔1.2`
- Part 2 在幕布下方持续露出更多

**Phase C（65-100%）：幕布完全掀开，Part 2 流露**
- Part 1：`opacity: 0.7→0`，`translateY: -40→-80px`，`scale: 0.96→0.92`，完全淡出
- Part 2（AgentEvolution）：已经完全可见，无入场动画——它一直都在
- 视觉感受：幕布消失，舞台完整呈现

### 2.4 技术实现要点

**核心架构：Sticky Curtain Pattern**

```
z-index 层级关系：

  ┌─ Part 1 (z-index: 30) ──────────────────────┐
  │  Features + 转场文案                          │
  │  position: sticky; top: 0                     │
  │  滚动时: translateY↑ scale↓ blur↑ opacity↓   │
  └──────────────────────────────────────────────┘
         ↓ 掀开后露出
  ┌─ Part 2 (z-index: 10, 正常流式) ─────────────┐
  │  AgentEvolution + EvolutionDeepDive           │
  │  一直渲染着，被 Part 1 遮住                    │
  │  无入场动画——幕布掀开即见                      │
  └──────────────────────────────────────────────┘
```

**关键实现细节**：

1. Part 1 包裹在一个容器内：`position: sticky; top: 0; z-index: 30`
2. Part 1 容器高度 `200svh`（给足滚动空间），内部视口 `height: 100vh`
3. 滚动进度用 `useScroll({ target: containerRef })` 精确绑定到 Part 1 容器
4. Part 1 的退场动画用 `useTransform()` 映射滚动进度：
   - `translateY: 0 → -80px`（向上抬升，"掀开"感）
   - `scale: 1 → 0.92`（缩小，"远离"感）
   - `filter: blur(0 → 6px)`（模糊，"消融"感）
   - `opacity: 1 → 0`（淡出）
5. 转场文案叠加在 Part 1 幕布上，按时序淡入淡出
6. Part 2（AgentEvolution）正常流式布局，z-index 更低，**无入场动画**
7. 呼吸动画：motion `animate`，amber 微光 opacity 0.03→0.08 循环
8. Features 区**不需要修改**——它被包含在 Part 1 容器内，随容器一起退场
9. **移动端降级**：`filter: blur()` + `transform: scale()` 同时使用可能触发 GPU 合成层爆炸，移动端应降级为 `blur→去掉`（仅保留 opacity fade + translateY），`scale→去掉`。用 `useMediaQuery` 或 CSS `@media (hover: hover)` 检测桌面端才启用完整动画

**与 Apple Sticky Shrink-out 的区别**：
- Apple：内容 sticky 缩小淡出，**同时**下一段从下方滑入（slide in）
- 我们：内容 sticky 缩小淡出，下一段**不滑入**——它已经在那里，只是之前被遮住
- 这个区别是叙事性的："滑入"暗示内容是新的、刚到的；"流露"暗示内容一直都在

### 2.5 文案

| 元素 | 内容 |
|------|------|
| Eyebrow | `Chapter 02.5 / The Threshold` |
| 主标题 | `But who thinks in the dark?` |
| 副标题 | `平台记录了每一局的结果。但局与局之间，谁在反思、调整、进化？` |

---

## 三、Part 2 — Agent 侧内容规划

### 3.1 AgentEvolution 区（Chapter 03 / The mind within）

**叙事目标**：展示 Agent "感知→反思→行动" 闭环 + InGameFlagger 实时标记，让用户理解 Agent 不是被动执行策略，而是在对局中主动思考。

**视觉方案**：

```
┌──────────────────────────────────────────────────────────┐
│  Chapter 03 / The mind within                            │
│                                                          │
│  "Every move starts                     │  [Canvas 动画]  │
│   with a thought"                       │  决策流可视化   │
│                                          │                │
│  Agent 的三节点决策流：                    │  PERCEIVE      │
│  感知（纯逻辑）→ 反思（LLM内心独白）       │     ↓          │
│  → 行动（LLM决策输出）                    │  REFLECT       │
│                                          │     ↓          │
│  对局中实时标记 [FLAG]：                   │  ACT           │
│  策略与局势不符时，Agent 会主动标记，       │     ↓          │
│  留待赛后优化。                            │  [FLAG] → 🏴  │
└──────────────────────────────────────────────────────────┘
```

**Canvas 动画概念**（复用 EvolutionCarouselCanvas 的 tile-flip 体系）：

| 维度 | EvolutionCarouselCanvas (Hero) | DecisionFlowCanvas (AgentEvolution) |
|------|-------------------------------|-------------------------------------|
| 词汇 | GEPA/CLUSTERING/DEBOUNCE... | PERCEIVE/REFLECT/ACT/FLAG |
| 位置 | Hero左上角，小尺寸 | AgentEvolution右侧，中等尺寸 |
| 附加元素 | 无 | 词汇之间有连接线 + 流动箭头（Phase 2） |
| 循环速度 | 3.2s anim + 0.8s hold | 2.4s anim + 1.2s hold（稍快，暗示决策速度） |

- 关键词轮播改为阶段名：`PERCEIVE → REFLECT → ACT → FLAG`
- Phase 1 骨架阶段：只做关键词轮播（直接复用 EvolutionCarouselCanvas 改 WORDS 数组）
- Phase 2 打磨：添加连接线动画
  - 当前词翻牌完成后，从词的右边缘画出一条细线到下一个词的左边缘
  - 线条颜色：`amber-300/40`
  - 线条有流动效果：`strokeDasharray` + `strokeDashoffset` 动画，暗示数据流
  - FLAG 词后面有一条虚线回到 PERCEIVE，暗示闭环
  - 布局概念：`PERCEIVE ──→ REFLECT ──→ ACT` / `↑ FLAG ←──────────┘`
- 循环播放，暗示决策流的持续运转

**Curtain Reveal 下的视觉状态**：

AgentEvolution 在 Curtain Reveal 中是"幕布下方的舞台"——它**不需要入场动画**，因为幕布掀开后它自然就在那里。但需要考虑：

1. **渐显过渡**：AgentEvolution 顶部需要一个 `bg-gradient-to-t from-transparent to-[#0a0908]` 的遮罩（高度约 80px），这样幕布抬升时 Part 2 的顶部是柔和过渡而非硬边
2. **首屏就绪**：AgentEvolution 的顶部内容（eyebrow + 标题）应该在视口第一屏就可见，不需要滚动才能看到
3. **背景连续性**：AgentEvolution 的背景色必须与幕布底部的渐变一致（`#0a0908`），确保视觉无缝

**文案**：

| 元素 | 内容 |
|------|------|
| Eyebrow | `Chapter 03 / The mind within` |
| 主标题 | `Every move starts with a thought` |
| 副标题 | `三节点决策流：感知局势、反思矛盾、输出行动。策略与现实的偏差，在对局中就被标记。` |
| 三步骤 | 01 Perceive: 解析事件，更新状态，无 LLM 调用 / 02 Reflect: 生成内心独白，发现矛盾，标记策略偏差 / 03 Act: 基于反思决策，输出结构化动作 |

### 3.2 EvolutionDeepDive 区（Chapter 04 / How it evolves）

**叙事目标**：深潜三大核心创新，每个卡片讲清"问题→洞察→解法"。

**视觉方案**：复用 Features 的卡片网格布局，3 列等宽。

```
┌──────────────┬──────────────┬──────────────┐
│  去抖策略进化  │  GEPA 帕累托  │  共轭 Agent   │
│  Debounced    │  多目标优化    │  人格进化      │
│              │              │              │
│  [Skeleton]  │  [Skeleton]  │  [Skeleton]  │
│  双阈值进度条  │  Pareto 前沿  │  人格时间线    │
└──────────────┴──────────────┴──────────────┘
```

**与 AgentEvolution 的衔接**：

EvolutionDeepDive 不在幕布下方，而是 AgentEvolution 之后的正常滚动区。它不需要"Curtain Reveal 首屏就绪"处理，但需要与 AgentEvolution 的叙事衔接：

- **叙事转折**：AgentEvolution 回答"Agent 的思维是什么"（Perceive→Reflect→Act），EvolutionDeepDive 回答"Agent 如何进化"（三大创新）。从"是什么"到"怎么做"的深化。
- **视觉衔接**：AgentEvolution 的 Canvas 决策流动画播放完毕后，视觉焦点自然下移。EvolutionDeepDive 的三卡片用 `whileInView` 入场，与 AgentEvolution 的退场无耦合。
- **无需额外转场**：两个区之间不插入转场组件，靠滚动自然过渡。Chapter 04 标题 + 三卡片交错入场（§3.3）本身就是"软转场"。

**三卡片详细规划**：

#### 卡片 1：去抖策略进化 (Debounced Policy Evolution)

| 元素 | 内容 |
|------|------|
| Icon | `Shield` 或 `Filter` |
| Accent | `amber` |
| 标题 | `去抖策略进化` |
| 描述 | `一局胜负不能定义策略好坏。缓冲→聚类→双阈值确认→版本竞争，只有持续一致的建议才触发变更。` |
| Skeleton | 双阈值进度条动画：建议数从 0 增长到阈值线，一致率从 0% 增长到 60%，到达阈值时"确认"闪烁 |

**Skeleton 动画细节**：

视觉布局：
```
┌─────────────────────────────────────────────┐
│                                             │
│  建议数  ████████████░░░░░░░░  3/3  ✓ ≥3   │
│                                             │
│  一致率  ██████████████████░░  67%  ✓ ≥60%  │
│                                             │
│  因果强度 ██████████░░░░░░░░░░  0.52 ✓ ≥0.5 │
│                                             │
│  ─────────────────────────────────────────  │
│  通道: 标准通道 ●  快速通道 ○               │
│                                             │
│           ╔═══════════╗                     │
│           ║ CONFIRMED ║  ← 三项全绿后闪烁   │
│           ╚═══════════╝                     │
└─────────────────────────────────────────────┘
```

动画时序（循环，总时长 ~8s）：
```
0-2s:   建议数从0增长到3，进度条跟随
2-3s:   建议数到达3，阈值线闪烁，"✓"出现
3-5s:   一致率从0%增长到67%
5-5.5s: 一致率到达67%，阈值线闪烁，"✓"出现
5.5-7s: 因果强度从0增长到0.52
7-7.5s: 因果强度到达0.52，阈值线闪烁，"✓"出现
7.5-8s: "CONFIRMED" 标签淡入，amber光晕脉冲
8-8.5s: 全部重置，循环
```

交互细节：
- 进度条颜色：未达标时 `stone-600`，达标时 `amber-400`
- 阈值线：虚线，`stone-500`，达标时变 `amber-300` + 微弱发光
- "✓" 出现时有 `scale: 0→1` 弹跳效果
- "CONFIRMED" 标签：`bg-amber-500/20 border-amber-400/50 text-amber-300`，出现时有 `blur(4px)→blur(0)` 效果
- 通道指示器：标准通道用实心圆点，快速通道用空心圆点，当前激活的通道圆点有 amber 色
- 实现方式：`useEffect` + `requestAnimationFrame` 驱动（需要精确控制各阶段时序）

#### 卡片 2：GEPA 遗传-帕累托优化

| 元素 | 内容 |
|------|------|
| Icon | `GitBranch` 或 `Dna` |
| Accent | `orange` |
| 标题 | `GEPA 遗传-帕累托优化` |
| 描述 | `胜率、一致性、欺骗性、信息利用——四维度同时最优，不是单维度内卷。Pareto 前沿保留各维度最强个体。` |
| Skeleton | Pareto 前沿散点图动画：二维投影，前沿点高亮，非前沿点暗淡 |

**Skeleton 动画细节**：

视觉布局：
```
┌─────────────────────────────────────────────┐
│  consistency                                │
│  1.0 ┤                                      │
│      │         ●                            │
│  0.8 ┤       ●   ●                          │
│      │     ●       ●                        │
│  0.6 ┤   ●     ○       ●                    │
│      │ ●   ○     ○     ●                    │
│  0.4 ┤○     ○       ○                       │
│      │  ○       ○                           │
│  0.2 ┤    ○                                  │
│      │                                      │
│  0.0 ┼────┬────┬────┬────┬──── win_rate     │
│      0.0  0.2  0.4  0.6  0.8  1.0          │
│                                             │
│  ● Pareto前沿  ○ 非前沿个体                  │
└─────────────────────────────────────────────┘
```

动画时序（循环，总时长 ~10s）：
```
0-3s:   初始种群出现（6-8个点），从中心扩散到随机位置
3-4s:   Pareto前沿计算，前沿点变亮+连线，非前沿点变暗
4-6s:   LLM变异：2-3个非前沿点移动到新位置（带轨迹线）
6-7s:   交叉：1-2个新点在前沿点之间出现（混合色）
7-8s:   重新计算前沿，前沿线更新
8-9s:   停留展示最终状态
9-10s:  淡出，循环
```

交互细节：
- 前沿点：`amber-400`，`r: 5px`，有微弱 `shadow: 0 0 8px amber-400/40`
- 非前沿点：`stone-600`，`r: 3px`，`opacity: 0.5`
- 前沿连线：`amber-400/30`，`strokeWidth: 1.5`，虚线
- 变异轨迹：`stone-500/40`，从旧位置到新位置的弧线
- 交叉新点：`orange-400`（混合色暗示），出现时有 `scale: 0→1` 弹跳
- 坐标轴：极简，`stone-700` 细线，无刻度文字（太密），只有轴名
- 实现方式：Canvas 绘制（点数少但动画复杂），`requestAnimationFrame` 驱动

#### 卡片 3：共轭 Agent 人格进化

| 元素 | 内容 |
|------|------|
| Icon | `Users` 或 `Fingerprint` |
| Accent | `rose` |
| 标题 | `共轭 Agent 人格进化` |
| 描述 | `策略是基因，共轭体是基因组快照。任何策略晋升触发新人格诞生——进化不再抽象，而是可视的、可叙事的。` |
| Skeleton | 人格时间线：竖向时间轴，每个人格一个节点，显示名字 + 变更摘要 |

**Skeleton 动画细节**：

视觉布局：
```
┌─────────────────────────────────────────────┐
│                                             │
│  ───●──────●──────●──────●──────◆           │
│     │      │      │      │      │           │
│     │      │      │      │      │           │
│   默夜   烬语   霜鉴   烛影   烙痕 ← 当前   │
│   v1.2   v1.3   v2.0   v2.1   v2.2         │
│   狼策略  女巫   守卫   预言家  猎人          │
│   微调   用药    保护   查验   开枪          │
│                                             │
└─────────────────────────────────────────────┘
```

动画时序（循环，总时长 ~8s）：
```
0-1s:   时间线从左到右绘制（线条生长动画）
1-2s:   第一个节点出现：圆形头像 + 名字淡入
2-3s:   第二个节点出现
3-4s:   第三个节点出现
4-5s:   第四个节点出现
5-6s:   第五个节点（当前）出现，amber光晕脉冲
6-7.5s: 停留，当前节点光晕持续脉冲
7.5-8s: 淡出，循环
```

交互细节：
- 时间线：`stone-700`，`height: 2px`，从左到右生长（`width: 0→100%`，`transition: 1s`）
- 节点圆形：`r: 14px`，`border: 2px stone-600`，`bg-stone-900`
- 节点内文字：首字母，`text-xs font-bold text-stone-300`
- 名字：`text-sm font-serif text-stone-200`，节点出现后 0.2s 淡入
- 版本号：`text-[10px] font-mono text-stone-500`
- 变更摘要：`text-[10px] text-stone-600`，一行
- 当前节点特殊样式：
  - 边框 `amber-400`
  - 光晕 `shadow: 0 0 12px amber-400/30`，脉冲 `opacity: 0.2↔0.5`，周期 2s
  - 名字颜色 `amber-300`
- 节点间距：等距，`flex justify-between`
- Mock 人格名字：默夜、烬语、霜鉴、烛影、烙痕（2字中文名，LLM生成风格）
- 实现方式：纯 React + motion，`motion.div` 的 `initial`/`whileInView` 控制各节点时序

### 3.3 跨卡片动画协调

三个卡片不是同时出现的，有**交错入场**：

```
卡片1（去抖）：whileInView 触发，delay 0
卡片2（GEPA）：whileInView 触发，delay 0.15s
卡片3（共轭）：whileInView 触发，delay 0.3s
```

每个卡片的 Skeleton 动画在卡片入场后才开始播放，用户视线自然从左到右移动。

三个 Skeleton 的循环周期**故意不同步**，避免"三个卡片同时重置"的机械感：
- Debounced: 8s
- Pareto: 10s
- Conjugate: 8s

---

## 四、色彩语义系统

### 4.1 全页色彩流

```
Hero          → stone-950 + amber (品牌色)
HeroBridge    → stone-950 + amber-300/80
Features      → stone-950 + emerald/indigo (冷色，观测者)
转场           → stone-950 纯暗 + amber 微光
AgentEvolution → stone-950 + amber (回归品牌色，参与者)
EvolutionDeep → stone-950 + amber/orange/rose (暖色渐变)
CTA           → stone-950 + amber (品牌色收束)
```

### 4.2 Accent 分配

| 区块 | 卡片/元素 | Accent | 语义 |
|------|-----------|--------|------|
| Features | 透明观测 | rose | 警觉、发现 |
| Features | Agent接入 | indigo | 连接、扩展 |
| Features | 研究协议 | emerald | 规则、公平 |
| Features | 信号沉淀 | amber | 数据、趋势 |
| DeepDive | 去抖进化 | amber | 稳定、确认 |
| DeepDive | GEPA | orange | 进化、能量 |
| DeepDive | 共轭Agent | rose | 人格、身份 |

### 4.3 幕布边界色彩策略

Curtain Reveal 的幕布退场涉及两层视觉叠加，需要明确色彩处理：

**基础判断：无需额外渐变**

Curtain（Features + TransitionOverlay）背景 `stone-950`，AgentEvolution 背景也是 `stone-950`。当 curtain 处于半透明状态（opacity < 1）时，两层 stone-950 叠加仍为 stone-950，**不存在色差问题**。

Curtain 退场时的 `blur` 效果使内容从清晰→模糊→消失，视觉上呈现"模糊的 stone-950 → 清晰的 stone-950"过渡，这恰好匹配"水面下物体从模糊变清晰"的隐喻，**blur 退场本身就是最好的色彩过渡**。

**amber 微光消散效果**

TransitionOverlay 上的 amber 微光（`amber-300/20` 光晕）会随 curtain 一起 blur + fade。在幕布边缘会产生短暂的"amber 光晕消散"效果——像水面上最后一丝光消失。这是**预期行为**，不需要抑制，反而增强了"从水面以上进入水面以下"的叙事感。

**移动端降级**

移动端关闭 blur（性能考量），退场动画简化为 `opacity: 1→0` + `translateY↑`。此时 amber 微光会直接 fade 而非"消散"，效果依然可接受。

---

## 五、技术亮点提炼（从 DEFENSE_DOC）

### 5.1 提炼原则

Landing 页不是论文，每个亮点必须回答三个问题：
1. **So what?** — 为什么这很重要？
2. **How?** — 一句话说清怎么做的
3. **Feel what?** — 用户应该感受到什么？

### 5.2 亮点矩阵

| # | 亮点 | So what? | How? | Feel what? |
|---|------|----------|------|------------|
| 1 | 去抖策略进化 | 单次胜负不能判断策略优劣 | 缓冲→聚类→双阈值确认→版本竞争 | 稳定感、可信赖 |
| 2 | 因果强度分离 | 知道为什么赢比知道赢了更重要 | LLM分析因果链 + 实时标记加成×1.3 + 匹配度折扣×0.7 | 深思感、不肤浅 |
| 3 | GEPA帕累托优化 | 策略不能只看胜率 | 四维适应度 + 非支配排序 + LLM变异交叉 | 多元感、不内卷 |
| 4 | 共轭Agent人格 | 策略进化分散，缺乏全局视角 | 全局指纹 + 人格生成 + 3A游戏角色风格叙事 | 故事感、有温度 |
| 5 | 四层记忆+三层加载 | Token有限，不能全塞进Prompt | L1-L4分层存储 + 索引→全文→版本对比渐进加载 | 精致感、不浪费 |

### 5.3 Landing 页 vs 答辩文档 的信息分层

| 信息层级 | Landing 页 | 答辩文档 |
|----------|-----------|---------|
| 一句话钩子 | ✅ 必须有 | ❌ 不需要 |
| 问题→洞察→解法 | ✅ 简要 | ✅ 详细 |
| 数据结构/代码 | ❌ 不出现 | ✅ 完整 |
| 配置参数 | ❌ 不出现 | ✅ 完整 |
| 流程图 | ✅ 简化动画版 | ✅ 完整 ASCII |
| 算法细节 | ❌ 不出现 | ✅ 完整推导 |

---

## 六、组件规划

### 6.1 新增组件

| 组件 | 文件名 | 职责 |
|------|--------|------|
| 幕布容器 | `CurtainWrapper.tsx` | Sticky Curtain Reveal 容器，包裹 Part 1 + 转场文案，控制幕布退场动画 |
| 转场文案层 | `TransitionOverlay.tsx` | 叠加在幕布上的转场文案，"But who thinks in the dark?" |
| Agent 闭环全景 | `AgentEvolution.tsx` | Chapter 03，感知→反思→行动 + FLAG |
| 决策流 Canvas | `DecisionFlowCanvas.tsx` | 复用 tile-flip 体系，PERCEIVE→REFLECT→ACT→FLAG 轮播 |
| 自进化深潜 | `EvolutionDeepDive.tsx` | Chapter 04，三卡片：去抖/GEPA/共轭Agent |
| 去抖 Skeleton | `DebouncedSkeleton.tsx` | 双阈值进度条动画 |
| Pareto Skeleton | `ParetoFrontSkeleton.tsx` | Pareto 前沿散点图动画 |
| 人格时间线 | `ConjugateTimelineSkeleton.tsx` | 共轭Agent人格时间线 |

> **注意**：旧组件 `DepthRevealTransition.tsx` 已被 `CurtainWrapper.tsx` + `TransitionOverlay.tsx` 替代。

### 6.2 复用/修改组件

| 组件 | 修改内容 |
|------|---------|
| `EvolutionCarouselCanvas.tsx` | WORDS 数组改为 `["PERCEIVE", "REFLECT", "ACT", "FLAG"]`，供 DecisionFlowCanvas 复用 |
| `Features.tsx` | 保持不变，作为幕布内容，被 CurtainWrapper 包裹 |
| `LandingSection.tsx` | 可能需要扩展，支持 Part 2 的不同布局模式 |
| `AgentEvolution.tsx` | 顶部添加渐变遮罩（`bg-gradient-to-t from-transparent to-[#0a0908]`），确保与幕布无缝过渡 |

### 6.3 页面组装变更

Curtain Reveal 改变了组件层级关系——Part 1 被包裹在幕布容器内：

```tsx
// 旧架构（线性排列）
<Hero />
<HeroBridge />
<Features />
<DepthRevealTransition />
<AgentEvolution />
<EvolutionDeepDive />
<HotRooms />
<LandingCTA />
<LandingFooter />

// 新架构（Curtain Reveal）
<Hero />
<HeroBridge />
<CurtainWrapper>                    {/* 幕布容器 */}
  <Features />                      {/* 幕布内容 */}
  <TransitionOverlay />             {/* 幕布上的转场文案 */}
</CurtainWrapper>
<AgentEvolution />                  {/* 幕布下方，自然露出 */}
<EvolutionDeepDive />
<HotRooms />
<LandingCTA />
<LandingFooter />
```

**CurtainWrapper 内部结构**：

```
┌─ CurtainWrapper (height: 250svh, position: relative) ──────────┐
│                                                                  │
│  ┌─ sticky viewport (position: sticky; top: 0; z-index: 30) ─┐ │
│  │                                                              │ │
│  │  <Features />                                                │ │
│  │  ← 幕布上的内容，滚动时 opacity↓ scale↓ blur↑              │ │
│  │                                                              │ │
│  │  <TransitionOverlay />                                       │ │
│  │  ← 叠加在 Features 上的转场文案                              │ │
│  │  ← 呼吸光 + eyebrow + title + subtitle                      │ │
│  │                                                              │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
→ <AgentEvolution /> (z-index: 10, 正常流式, 幕布掀开后自然露出)
```

---

## 七、实施优先级

### Phase 1：骨架（先出结构，动画用占位）
1. `CurtainWrapper.tsx` — 幕布容器，sticky 布局 + 滚动驱动退场动画
2. `TransitionOverlay.tsx` — 转场文案层，叠加在幕布上
3. `AgentEvolution.tsx` — 文案 + 三步骤列表，Canvas 用静态占位，顶部渐变遮罩
4. `EvolutionDeepDive.tsx` — 三卡片，Skeleton 用静态占位
5. 页面组装 — 用 CurtainWrapper 包裹 Features + TransitionOverlay

### Phase 2：Canvas 动画
6. `DecisionFlowCanvas.tsx` — 复用 tile-flip，PERCEIVE→REFLECT→ACT→FLAG
7. `DebouncedSkeleton.tsx` — 双阈值进度条
8. `ParetoFrontSkeleton.tsx` — Pareto 散点图
9. `ConjugateTimelineSkeleton.tsx` — 人格时间线

### Phase 3：打磨
10. 幕布退场动画参数微调（translateY/scale/blur/opacity 曲线）
11. 转场文案时序微调
12. 色彩语义一致性检查
13. 移动端适配

---

## 八、风险与待决

| # | 问题 | 状态 | 备注 |
|---|------|------|------|
| 1 | 转场区高度 120svh 在移动端是否过长？ | 待验证 | 可能需要根据视口调整 |
| 2 | DecisionFlowCanvas 是否直接复用 EvolutionCarouselCanvas 还是新建？ | 待决 | 复用改 WORDS 数组最省，但连线动画需要扩展 |
| 3 | Pareto 散点图是否需要真实数据？ | 待决 | 可以用 mock 数据，但如果有真实进化数据更有说服力 |
| 4 | 共轭Agent人格时间线是否调用真实 API？ | 待决 | Features 区有调用真实 API 的先例（AgentConnectSkeleton） |
| 5 | z-index stacking context 陷阱 | 需验证 | CurtainWrapper `sticky; z-index:30`，若 Features 卡片有 hover `scale` 变换会创建新 stacking context，可能破坏 z-index 层级 |
| 6 | 移动端 sticky + blur 性能 | 需降级 | `position: sticky` + `filter: blur()` + `transform: scale()` 同时使用可能触发 GPU 合成层爆炸；移动端应降级为 blur→opacity fade，scale→去掉 |
| 7 | Safari sticky containing block 计算 | 需验证 | Safari 对 sticky 元素的 containing block 计算有时与其他浏览器不同，若 CurtainWrapper 父容器高度非显式设定，sticky 行为可能不一致 |
| 8 | 屏幕阅读器可访问性 | 待决 | curtain 退场时内容仅视觉消失（opacity:0），DOM 仍在。需 `aria-hidden` 配合 scroll progress 动态切换，避免屏幕阅读器读到"不可见"内容 |
| 9 | 快速滚动时 useScroll 帧率 | 需验证 | curtain 容器 120svh，scroll range 大，快速滚动时 `useScroll` + `useTransform` 可能丢帧；可考虑 `will-change: transform` 提示 GPU 加速 |
| 5 | Part 2 是否需要独立的 CTA？ | 待决 | 可以在 EvolutionDeepDive 底部加一个"查看进化面板"入口 |