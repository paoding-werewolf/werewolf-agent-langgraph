# Werewolf Agent LangGraph 答辩/展示审查文档

> 审查日期：2026-06-09  
> 审查范围：`werewolf-agent-langgraph` 本地仓库源码、现有文档、测试、部署文件。  
> 约束说明：本次未查询线上数据库、未调用 LLM、未执行会写真实库的脚本；避免对生产数据产生副作用。  
> 结论定位：这是面向答辩/展示的技术审查文档，不是发布说明。

## 1. 总体结论

当前仓库已经形成一条完整的狼人杀外部 Agent 服务链路：

1. 对外提供 WebSocket + HTTP Agent 协议，支持 `init / perceive / act / game_over`。
2. 每次行动采用“先反思、再按阶段选择工具行动”的显式 async 管线。
3. 局后自进化管线可将对局反思结构化为策略建议，进入缓冲池、聚类、确认、策略版本化和版本竞争。
4. 共轭 Agent 将全局 `skill current_default` 快照封装成可参赛的历史 Agent。
5. GEPA 离线模块提供基于遗传算法、Pareto 前沿和 LLM 变异/交叉的批量策略进化。

答辩时需要准确表述一个事实：仓库名称和旧文档仍保留 LangGraph 叙事，但当前 `app/agents/agent_graph.py` 中没有实际编译 `StateGraph`，运行实现是显式 async 函数管线，并通过 `run_perceive/run_act` 兼容旧接口。

## 2. 仓库结构

核心目录如下：

```text
app/
  main_ws.py                 # WS + HTTP 服务入口、自进化 API、后台任务
  agents/                    # 单局决策、状态、Prompt、LLM 工具调用
  evolution/                 # 自进化管线、策略库、共轭 Agent、GEPA
  memory/                    # 工作记忆、自我画像、对手画像、对局归档
  core/                      # 角色、阶段、Agent 视角状态模型
  utils/                     # prompt 日志和调试页
docs/                        # 架构/设计/修复计划
tests/                       # pytest 单元测试
scripts/                     # 历史重放、迁移、恢复计划、备份脚本
recovery/                    # binlog 恢复 SQL
```

重要源码入口：

- `app/main_ws.py`：服务主入口，注册 Agent API 与 evolution dashboard API。
- `app/agents/agent_graph.py`：单局 `perceive / reflect / act` 决策核心。
- `app/evolution/reflection_engine.py`：局后结构化反思。
- `app/evolution/buffer_pool.py`：建议缓冲池，当前为 MySQL 持久化。
- `app/evolution/clustering.py`：场景标签 + 语义一致性聚类。
- `app/evolution/confirmation.py`：双阈值确认与策略合成。
- `app/evolution/skill_loader.py`：策略加载、版本竞争、版本统计、晋升。
- `app/evolution/gepa.py`：GEPA 离线进化。

## 3. 对外运行架构

服务入口为 `app/main_ws.py`。启动后：

- WebSocket 默认监听 `7861`。
- HTTP 默认监听 `7860`。
- Docker Compose 映射为 `8082 -> 7861`、`8083 -> 7860`。
- 共享卷 `/opt/werewolf-agent-data:/root/.werewolf-agent` 用于部分记忆文件和配置。

运行时链路：

```text
编排服务
  -> /provider/agents 或 /provider/health
  -> init 创建 session，固定本局使用的 skill 版本
  -> perceive 写入可见事件
  -> act 触发反思和工具决策
  -> game_over 触发局后自进化

Agent 服务
  -> SessionStore 保存单局状态
  -> LLMCaller 调用模型和工具
  -> evolution.* 写入 MySQL / 共享卷
```

关键代码事实：

- `_process_init()` 创建唯一 `session_id`，构建 `versions_used` 并写入 `SessionStore`，见 `app/main_ws.py:187`。
- `_process_act()` 同步执行 `run_act()`，返回 `thought` 和 `act_result` 两类帧，见 `app/main_ws.py:244`。
- `_process_game_over()` 决定是否进入局后管线，见 `app/main_ws.py:525`。
- HTTP evolution API 在 `_create_http_app()` 中集中注册，见 `app/main_ws.py:877`。
- 主函数启动 HTTP、WS、session 清理、Curator 和确认/过期后台循环，见 `app/main_ws.py:1629`。

## 4. 单局决策链路

单局状态使用 `AgentState` TypedDict，包含身份、当前阶段、玩家视角、事件历史、工作记忆、策略版本和局内 flag。

一次行动的核心流程：

```text
act(request)
  -> normalize_action_status
  -> _reflect_node
      -> 构建 Prompt
      -> 注入 evolution_strategies
      -> LLM 输出 last_thought
      -> 提取 [FLAG]
  -> _route_by_phase
  -> 对应阶段决策函数
      -> LLM tool call
      -> next_action
```

阶段路由支持夜间神职行动、狼人夜聊/刀人、警长竞选、白天发言、放逐投票、猎人/狼王开枪和通用兜底，见 `app/agents/agent_graph.py:220`。

Prompt 构成：

- 核心任务与角色视角。
- 当前公开信息和事件摘要。
- 演化策略注入：从 `versions_used` 按指定版本读取策略全文。
- 静态村民白天思维框架。
- 局内 `[FLAG]` 标记提示。

LLM 行动通过 OpenAI function tools 表达，包含 `speak / vote / wolf_kill / wolf_chat / seer_check / witch_heal / witch_poison / guard_protect / shoot / decide_signup / vote_sheriff / choose_speech_order / pass_turn`，见 `app/agents/llm_caller.py:26`。

## 5. 局后自进化闭环

局后入口是 `_run_post_game_pipeline()`，由 `_process_game_over()` 异步触发。跳过条件：

- `skip_evolution=True`，通常代表局内存在 builtin AI。
- 当前 `external_agent_id` 是历史冻结共轭 Agent。
- session 已丢失时只写最小归档，跳过反思。

完整局后链路：

```text
game_over
  -> format_game_trace
  -> 读取 in_game_flags
  -> 读取本局 current_strategies
  -> ReflectionEngine.reflect()
  -> BufferPool.ingest()
  -> 8 秒防抖后统一 process_pending()
  -> 每小时后台 check_all_clusters() + expire_old_suggestions()
  -> save_game()
  -> update_self_model()
  -> update_opponent_from_game()
  -> record_version_usage()
  -> 满足晋升条件时创建新共轭 Agent
```

关键实现：

- 局后 trace、flags、策略、工作记忆组装和反思调用见 `app/main_ws.py:630`。
- 入池后只调度聚类，确认和过期清理已经移到小时级后台循环，见 `app/main_ws.py:687`。
- 版本使用统计在管线末尾记录，见 `app/main_ws.py:743` 附近。

### 5.1 结构化反思

`ReflectionEngine` 要求 LLM 输出 YAML，核心字段包括：

- `scene_tags`：角色、存活轮数、关键阶段、结果、死亡原因等。
- `causal_chain`：行动、中间结果、最终结果，区分策略因素和运气因素。
- `suggestion`：建议文本、置信度、方向、目标策略、匹配等级。
- `causal_strength`：本局策略选择与结果之间的因果强度。

反思结果建模见 `app/evolution/reflection_engine.py:21`，Prompt schema 见 `app/evolution/reflection_engine.py:67`，反思执行与解析见 `app/evolution/reflection_engine.py:144`。

当前实现会：

- 对有 `[FLAG]` 的建议提高因果强度。
- 对 `match_level=medium` 的建议打折。
- YAML 解析失败时记录 warning 并返回 `None`。

### 5.2 缓冲池

`BufferPool` 已从旧文档中的文件系统方案迁移为 MySQL 表 `evolution_buffer_items`：

- `pending`：刚入池的单条建议。
- `cluster`：聚类后的建议集合。
- `confirmed`：已通过确认阈值并生成策略版本。
- `expired`：过期 pending 或长期单建议 cluster。

入池逻辑见 `app/evolution/buffer_pool.py:26`。需要注意：当前代码对 `match_level=low` 不再直接丢弃，而是因果强度打 0.5 折后进入缓冲池；这与反思 Prompt 中“low 不进入策略更新管道”的描述不完全一致。

### 5.3 聚类

聚类流程：

1. 读取所有 pending。
2. 与已有 cluster 做场景标签相似度匹配。
3. 相似度超过阈值后，再做语义一致性判断。
4. 写回 cluster，并删除 pending。

标签相似度为加权方案：

- `role / critical_phase / result` 为核心维度，各 0.25。
- `role_survived_rounds / wolf_aggression / sheriff_contested / first_night_target` 为次要维度。
- 核心三维全匹配时达到 0.75，可跳过语义 LLM 检查。

实现见 `app/evolution/clustering.py:24`、`app/evolution/clustering.py:107`、`app/evolution/clustering.py:143`。

### 5.4 确认与策略合成

确认判定包含两条通道：

- 快速通道：平均因果强度达到 fast track 阈值，且数量和一致率达标。
- 普通通道：数量、一致率、平均因果强度同时达标。

默认阈值位于 `app/evolution/config.py:25`：

- 普通确认：`count >= 2`、`consistency >= 0.50`、`avg_causal >= 0.35`。
- 快速确认：`causal >= 0.70`、`count >= 1`。

确认后通过 LLM 合成完整 Markdown 策略，调用 `VersionManager.create_new_version()` 创建候选版本，并把 cluster 移到 confirmed。实现见 `app/evolution/confirmation.py:56` 和 `app/evolution/confirmation.py:127`。

## 6. 策略库与版本竞争

策略库由两张主表承载：

- `evolution_skills`：策略元信息、角色、当前默认版本、策略级胜率。
- `evolution_skill_versions`：具体版本 Markdown、状态、来源、触发 cluster、版本级胜率。

模型见 `app/evolution/models.py:17` 和 `app/evolution/models.py:33`。

`SkillLoader` 负责：

- 加载策略索引，并按当前角色和 common 策略筛选。
- 最多加载 3 个相关策略全文注入 Prompt。
- 为每局抽取版本：默认版本 + candidate warmup 分流。
- 局后记录版本胜负。
- candidate 胜率超过当前默认版本阈值时晋升。
- 提供列表、详情、版本内容、diff、rollback、pin、delete 等 API 支撑。

关键实现见 `app/evolution/skill_loader.py:32`、`app/evolution/skill_loader.py:82`、`app/evolution/skill_loader.py:134`、`app/evolution/skill_loader.py:166`、`app/evolution/skill_loader.py:205`。

## 7. 共轭 Agent

共轭 Agent 是“全局所有 skill 当前默认版本”的冻结快照，用于把不同代策略组合包装成可参赛 Agent。

核心机制：

- 对当前所有 `skill_name -> current_default` 做稳定排序和 SHA-256 指纹。
- 首次有 skill 但没有共轭体时，创建初代。
- 某个策略版本晋升导致全局指纹变化时，创建新共轭 Agent。
- 历史共轭 Agent 使用冻结 `skill_versions_json`，不参与反思和版本竞争。
- 最新共轭 Agent 或 `default:common` 继续参与 warmup、反思和自进化。

实现见 `app/evolution/conjugate_agent.py:42`、`app/evolution/conjugate_agent.py:82`、`app/evolution/conjugate_agent.py:96`、`app/evolution/conjugate_agent.py:174`。

展示价值：

- 可以在 `/provider/agents` 中展示“不同进化代”的 Agent。
- 每个 Agent 有 `agent_name / fingerprint / changelog / lore / skill_versions`。
- 便于答辩中说明“不是单个 prompt 覆盖更新，而是策略组合形成可追溯谱系”。

## 8. GEPA 离线进化

GEPA 是在线防抖自进化之外的离线批量优化模块。它的目标不是等待同类失败场景累积，而是基于已有策略库和对局数据进行批量变异、交叉和筛选。

核心步骤见 `app/evolution/gepa.py:1`：

```text
触发
  -> 前置条件检查
  -> 初始化种群
  -> 适应度评估
  -> Pareto 前沿选择
  -> LLM 诊断 + 变异
  -> LLM 交叉
  -> 创建 source=gepa_evolution 的新版本
  -> 保存 generation 状态
```

前置条件：

- GEPA 开启。
- 策略数量达到 `min_skills_in_library`。
- 至少一个策略的策略级对局数达到 `min_games_for_fitness`。

实现见 `app/evolution/gepa.py:65`、`app/evolution/gepa.py:144`、`app/evolution/gepa.py:333`。

适应度维度：

- `win_rate`
- `consistency`
- `deception`
- `info_utilization`

其中胜率来自历史统计；后三项由 LLM-as-Judge 结合策略文档和近期对局 trace 评估，见 `app/evolution/gepa.py:409`。

Pareto 前沿计算见 `app/evolution/gepa.py:595`。非前沿且弱项低于 0.6 的个体进入 LLM 诊断与变异，见 `app/evolution/gepa.py:644`。同角色或 common 策略之间可做交叉，见 `app/evolution/gepa.py:807`。运行状态写入 `evolution_runtime_state(state_key="gepa")`，见 `app/evolution/gepa.py:999`。

## 9. 记忆与持久化分层

当前是混合持久化：

### MySQL

- 策略库和版本：`evolution_skills`、`evolution_skill_versions`。
- 共轭 Agent：`conjugate_agents`。
- 缓冲池：`evolution_buffer_items`。
- 策略缺口：`evolution_strategy_gaps`。
- 对局归档：`evolution_game_archive`。
- 运行时状态：`evolution_runtime_state`。
- 管线日志：`evolution_pipeline_logs`。

### 共享卷文件

- `~/.werewolf-agent/config.yaml`：可选配置覆盖。
- `~/.werewolf-agent/memory/self_model/profile.yaml`：自我画像。
- `~/.werewolf-agent/memory/opponents/*.yaml`：对手画像。

### 本地进程内

- `SessionStore`：单局 AgentState，默认 2 小时 TTL。
- `PromptLogger.history`：启动后只保留最近 200 条；完整 JSONL 文件持续追加。

注意：`app/evolution/db.py` 当前存在默认远程数据库连接串，虽然支持 `DATABASE_URL` 覆盖，但展示/部署前必须改为纯环境变量配置，不应保留明文默认值。

## 10. 展示建议脚本

建议准备两条演示路径：一条在线实时路径，一条可控重放路径。

### 10.1 在线实时路径

1. 健康检查：

```bash
curl http://localhost:8083/health
curl http://localhost:8083/provider/health
curl http://localhost:8083/provider/agents
```

2. 开一局全外部 Agent，确保不要混入 builtin AI，否则 `skip_evolution=True` 会跳过局后反思。

3. 行动中展示：

- WS/HTTP `act_result`。
- `thought` 帧。
- `/debug/view` 的 Prompt 结构。

4. 对局结束后展示：

```bash
curl http://localhost:8083/evolution/overview
curl http://localhost:8083/evolution/buffer
curl http://localhost:8083/evolution/skills
curl http://localhost:8083/evolution/games
curl http://localhost:8083/evolution/logs
```

5. 如果刚结束的局只完成了入池，手动触发：

```bash
curl -X POST http://localhost:8083/evolution/buffer/cluster-pending
curl -X POST http://localhost:8083/evolution/buffer/confirm-clusters
```

6. 准备一个已有 cluster 时，可展示人工强制确认：

```bash
curl -X POST http://localhost:8083/evolution/buffer/clusters/<cluster_id>/force-confirm
```

### 10.2 可控重放路径

如果现场不想等待完整对局和 LLM 反思，可以提前用历史 room 数据准备展示：

```bash
python app/replay_pipeline.py <room_id> --seat <seat_id> --dry-run
```

正式执行会调用 LLM 并写库，应只在确认目标环境和数据备份后运行：

```bash
python app/replay_pipeline.py <room_id> --seat <seat_id>
```

批量补历史数据脚本：

```bash
python scripts/replay_missed_games.py --limit 1 --dry-run
python scripts/replay_missed_games.py --limit 1
```

### 10.3 GEPA 展示路径

建议只在已有充分策略和对局数据的环境演示：

```bash
curl http://localhost:8083/evolution/gepa/status
curl -X POST http://localhost:8083/evolution/gepa/trigger
curl http://localhost:8083/evolution/gepa/status
```

GEPA 可能产生大量 LLM 调用，不建议现场临时跑完整 10 代。更稳妥的展示方式是准备已有 `evolution_runtime_state.gepa` 历史结果，并展示状态、generation history、新版本列表和 summary。

## 11. 已验证测试

本次执行了不访问线上 DB、不调用 LLM 的本地测试：

```bash
python3 -m pytest "tests/test_protocol_prompt.py" "test_gepa_balanced_select.py" -q
python3 -m pytest "tests/test_provider_agents.py" -q
```

结果：

- `tests/test_protocol_prompt.py` + `test_gepa_balanced_select.py`：24 passed。
- `tests/test_provider_agents.py`：15 passed。
- 合计：39 passed。
- 仅有 `pytest_asyncio` loop scope 配置弃用警告。

未执行：

- `test_evolution_pipeline.py` 默认会写数据库并可能触发策略版本创建。
- `replay_pipeline.py` 和 `scripts/replay_missed_games.py` 会调用 LLM 并写库。

## 12. 答辩前风险清单

### P0 必须处理或明确规避

1. **硬编码敏感配置**  
   `app/agents/llm_caller.py:105` 和 `app/evolution/db.py:8` 存在明文默认凭据/连接配置。答辩前应改为环境变量必填，并轮换已暴露密钥。本文不复述具体值。

2. **Agent 服务 evolution API 缺少鉴权**  
   `main_ws.py` 直接暴露 `/evolution/*`，包含 rollback、pin、delete、force-confirm、create-version、GEPA trigger 等写操作。如果 `8083` 可被外部访问，风险很高。展示环境至少应通过反向代理、内网访问或临时 token 控制。

3. **调试页泄露完整 Prompt 和响应**  
   `/debug/view` 与 `/debug/prompts` 会展示 system prompt、user prompt、LLM response。只适合受控内网展示。

4. **不要宣称当前实现是已编译 LangGraph 图**  
   当前是 LangGraph 风格的显式 async 管线。可以表述为“保留 LangGraph 三阶段设计思想，工程上改为可控的显式管线”。

### P1 建议修复

1. **结果枚举不一致**  
   局后保存通常使用 `won/lost`，但 `EvolutionSummary._agg_game_stats()` 统计 `win/loss`。这会导致摘要胜负统计可能为 0。

2. **工作记忆未真正注入在线决策 Prompt**  
   `PromptBuilder` 支持 `working_memory / opponent_profiles / self_model_text`，但当前 `agent_graph.py` 调用只传入 `evolution_strategies`。工作记忆主要用于局后反思，不是在线决策的实际输入。

3. **确认任务小时级延迟不利于现场展示**  
   在线对局结束后只会防抖聚类；确认默认每小时跑一次。展示时应使用手动 API 或准备历史数据。

4. **GEPA cancel 不是强一致取消**  
   模块级 `cancel()` 会新建 GEPA 实例并更新状态，但正在运行的实例只读自己的 `_cancel_flag`。实际运行可能不会立即停止。

5. **数据库 schema 迁移不完整**  
   仓库内只有 `migrations/001_create_conjugate_agents.sql`，完整 evolution 表依赖线上已有 schema 或恢复 SQL。新环境部署前需要补齐迁移。

### P2 可作为后续优化

1. Prompt 日志文件无限追加，应增加轮转或按大小清理。
2. LLM 调用缺少统一超时、重试、熔断和成本控制。
3. `evolution_skill_versions.skill_id` 等字段没有 ORM 层外键约束。
4. `low` 建议是否入池的策略与 Prompt 文案不一致，应统一。
5. Recovery SQL 文件巨大且包含大量策略正文，不适合随展示材料公开。

## 13. 答辩叙事建议

建议用三层叙事组织：

1. **运行时智能**  
   Agent 通过 `perceive -> reflect -> phase-specific tool act` 完成单局实时决策，决策输出是可执行工具调用，不是自由文本。

2. **在线自进化**  
   每局结束后把经验转化为结构化反思，用“场景聚类 + 因果强度 + 一致率”防止单局噪声直接污染策略库。

3. **谱系化进化**  
   策略版本经过 warmup、胜率竞争和晋升，形成新的全局策略快照，也就是共轭 Agent；GEPA 则提供离线批量探索能力。

一句话版本：

> 这个系统不是简单把输局总结追加进 Prompt，而是把“对局事实 -> 因果反思 -> 聚类确认 -> 策略版本 -> 版本竞争 -> Agent 谱系”做成了闭环。

## 14. 推荐展示顺序

1. 展示 `/provider/agents`：说明共轭 Agent 和版本快照。
2. 展示一局 `act`：解释 thought 和 tool action。
3. 展示 `/debug/view`：解释 Prompt 中的策略注入。
4. 展示 `game_over` 后 `/evolution/buffer`：说明建议入池和聚类。
5. 展示 `/evolution/skills/{skill_name}`：说明策略版本、胜率和 current_default。
6. 展示 diff/rollback/pin：说明可审计、可回滚。
7. 展示 GEPA status 或历史结果：说明离线批量进化。
8. 最后讲风险控制：防抖、阈值、人工确认、回滚、冻结历史 Agent。

## 15. 后续工程建议

按优先级建议：

1. 先清理敏感配置和 API 鉴权，再进入公开答辩环境。
2. 补齐数据库迁移，确保新环境可一键初始化。
3. 统一结果枚举 `won/lost` 与 `win/loss`。
4. 把 `working_memory / self_model / opponent_profiles` 明确接入在线 Prompt，或从文档中弱化“在线记忆注入”表述。
5. 为 evolution API 增加只读演示模式，避免现场误触发写操作。
6. 为 GEPA 增加基于数据库状态的协作取消，避免后台任务不可控。
7. 增加一组纯 SQLite、mock LLM 的自进化端到端测试，覆盖 reflection -> buffer -> cluster -> confirm -> version。
