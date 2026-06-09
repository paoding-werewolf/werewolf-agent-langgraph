# Landing Page 叙事规划 — Agent Self-Evolution

> 日期：2026-06-09
> 状态：规划阶段（未落地前端）

---

## 一、架构总览

### 1.1 双 Part 架构

Landing 页按**编排层/平台侧**与 **Agent 侧/自进化**分为两大叙事 Part，中间以 Sticky Scroll Reveal 转场衔接。

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
│  ═══ Sticky Scroll Reveal 转场 ═══               │
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

## 二、转场设计：Sticky Scroll Reveal

### 2.1 概念

**"Depth Reveal"**——从表面潜入深处。平台侧是"水面以上"（可见的实验流程），Agent侧是"水面以下"（不可见的内心世界）。

### 2.2 视觉方案

```
转场区 (高度 150svh，给足滚动空间)

滚动进度 0% ────────────────────────────────────────── 100%
     │              │              │              │
     │  Phase A     │  Phase B     │  Phase C     │
     │  0-35%       │  35-65%      │  65-100%     │
     │              │              │              │
     │  Features区  │  暗色层+文案  │  Agent区     │
     │  fade-out    │  淡入→停留→淡出│  从底部升起  │
```

**Phase A（0-35%）：Features区退场**
- Features区整体：`opacity: 1→0`，`scale: 1→0.94`，`filter: blur(0→4px)`
- 底部渐变遮罩：从透明到 stone-950，高度从 0 增长到 100%
- 实现方式：Features区不需要改，转场区自身用 sticky 定位覆盖上去

**Phase B（35-65%）：暗色层+文案**

文案时序（叙事节奏：定位→好奇→理解→期待）：

```
eyebrow:   ████████░░░░░░░░░░░░  (35-48% 入, 55-63% 出)
主标题:     ░░████████████░░░░░  (38-52% 入, 58-65% 出)  ← 可见窗口最大，压轴
副文案:     ░░░░██████░░░░░░░░░  (41-53% 入, 56-63% 出)
呼吸光:     ████████████████████  (35-65% 持续)
```

- 背景：纯 stone-950，带微弱网格纹理（复用 LandingSection 的网格 pattern）
- 文案进入方式：`y: 24→0` + `filter: blur(6px→0)`，不是简单 fade
- 呼吸动画：背景 amber 径向渐变，`opacity: 0.03↔0.08`，周期 3s，`scale: 0.8↔1.2`

**Phase C（65-100%）：Agent区入场**
- AgentEvolution区：`position: sticky; top: 0; z-index: 20`
- 入场动画：`y: 60→0`，`opacity: 0→1`
- 覆盖暗色层，视觉上像"新页面从下方推上来"

### 2.3 技术实现要点

- 转场区自身 `position: relative`，高度 150svh
- 内部有一个 `position: sticky; top: 0` 的视口固定层，承载文案和呼吸动画
- AgentEvolution区紧跟在转场区后面，也是 `sticky; top: 0`，z-index 更高
- 滚动进度用 `useScroll({ target: transitionRef })` 精确绑定到转场区
- Features区 fade-out 复用 Hero 的 scroll-out 模式（opacity + scale + blur）
- 转场文案用 `useTransform()` 精确映射滚动进度到 opacity/y/blur
- 呼吸动画：CSS `@keyframes` 或 motion `animate`，amber 微光 opacity 0.03→0.08 循环

### 2.4 文案

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
| 转场区 | `DepthRevealTransition.tsx` | Sticky Scroll Reveal 转场，"But who thinks in the dark?" |
| Agent 闭环全景 | `AgentEvolution.tsx` | Chapter 03，感知→反思→行动 + FLAG |
| 决策流 Canvas | `DecisionFlowCanvas.tsx` | 复用 tile-flip 体系，PERCEIVE→REFLECT→ACT→FLAG 轮播 |
| 自进化深潜 | `EvolutionDeepDive.tsx` | Chapter 04，三卡片：去抖/GEPA/共轭Agent |
| 去抖 Skeleton | `DebouncedSkeleton.tsx` | 双阈值进度条动画 |
| Pareto Skeleton | `ParetoFrontSkeleton.tsx` | Pareto 前沿散点图动画 |
| 人格时间线 | `ConjugateTimelineSkeleton.tsx` | 共轭Agent人格时间线 |

### 6.2 复用/修改组件

| 组件 | 修改内容 |
|------|---------|
| `EvolutionCarouselCanvas.tsx` | WORDS 数组改为 `["PERCEIVE", "REFLECT", "ACT", "FLAG"]`，供 DecisionFlowCanvas 复用 |
| `Features.tsx` | 保持不变，作为 Part 1 内容 |
| `LandingSection.tsx` | 可能需要扩展，支持 Part 2 的不同布局模式 |

### 6.3 页面组装变更

现有页面组装文件（推测在 `page.tsx` 或类似入口）需修改：

```tsx
// 现有
<Hero />
<HeroBridge />
<Features />
<LandingCTA />

// 新增后
<Hero />
<HeroBridge />
<Features />
<DepthRevealTransition />      {/* 新增：转场 */}
<AgentEvolution />             {/* 新增：Chapter 03 */}
<EvolutionDeepDive />          {/* 新增：Chapter 04 */}
<LandingCTA />
```

---

## 七、实施优先级

### Phase 1：骨架（先出结构，动画用占位）
1. `DepthRevealTransition.tsx` — 转场区，文案 + 基础 sticky 布局
2. `AgentEvolution.tsx` — 文案 + 三步骤列表，Canvas 用静态占位
3. `EvolutionDeepDive.tsx` — 三卡片，Skeleton 用静态占位

### Phase 2：Canvas 动画
4. `DecisionFlowCanvas.tsx` — 复用 tile-flip，PERCEIVE→REFLECT→ACT→FLAG
5. `DebouncedSkeleton.tsx` — 双阈值进度条
6. `ParetoFrontSkeleton.tsx` — Pareto 散点图
7. `ConjugateTimelineSkeleton.tsx` — 人格时间线

### Phase 3：打磨
8. 转场区呼吸动画 + 滚动进度映射微调
9. 色彩语义一致性检查
10. 移动端适配

---

## 八、风险与待决

| # | 问题 | 状态 | 备注 |
|---|------|------|------|
| 1 | 转场区高度 120svh 在移动端是否过长？ | 待验证 | 可能需要根据视口调整 |
| 2 | DecisionFlowCanvas 是否直接复用 EvolutionCarouselCanvas 还是新建？ | 待决 | 复用改 WORDS 数组最省，但连线动画需要扩展 |
| 3 | Pareto 散点图是否需要真实数据？ | 待决 | 可以用 mock 数据，但如果有真实进化数据更有说服力 |
| 4 | 共轭Agent人格时间线是否调用真实 API？ | 待决 | Features 区有调用真实 API 的先例（AgentConnectSkeleton） |
| 5 | Part 2 是否需要独立的 CTA？ | 待决 | 可以在 EvolutionDeepDive 底部加一个"查看进化面板"入口 |