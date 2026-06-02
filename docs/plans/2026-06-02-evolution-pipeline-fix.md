# Self-Evolution Pipeline Fix Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 15 identified bugs in the self-evolution pipeline that prevent data from flowing correctly from agent reflection → buffer pool → clustering → confirmation → skill versioning → dashboard display. Without these fixes, the self-evolution panel will remain empty even after games complete.

---

## Project Background & Essential Knowledge

### What This Project Does

This is a **狼人杀 (Werewolf) AI platform** — a 12-player werewolf game where seats can be filled by builtin AI, human players, or external LLM agents. The core value proposition is the **self-evolution system**: after each game, the LLM agent reflects on its decisions, identifies strategy improvements, and gradually evolves its strategy documents through a debounced policy update pipeline. An admin dashboard ("自进化监控台") visualizes this process.

### Repository Structure (two separate Git repos)

```
werewolf-service/                          # 本地开发根目录
├── werewolf-agent-langgraph/              # Repo 1: Agent 服务
│   ├── app/
│   │   ├── main_ws.py                     # WebSocket + HTTP agent 服务入口
│   │   ├── agents/
│   │   │   ├── agent_graph.py             # LangGraph agent 决策图
│   │   │   ├── llm_caller.py             # OpenAI 兼容 LLM 客户端
│   │   │   ├── prompt_builder.py          # 提示词构建（注入进化策略）
│   │   │   ├── state.py                   # AgentState TypedDict
│   │   │   └── session_store.py           # 会话存储
│   │   ├── evolution/                     # ★ 自进化管道全部模块
│   │   │   ├── config.py                  # EvolutionConfig + AGENT_HOME
│   │   │   ├── reflection_engine.py       # 结构化局后反思
│   │   │   ├── buffer_pool.py             # 建议缓冲池 (pending/clusters/confirmed/expired)
│   │   │   ├── clustering.py              # 场景标签聚类 + LLM 语义一致性
│   │   │   ├── confirmation.py            # 双阈值确认 + LLM 策略合成
│   │   │   ├── skill_loader.py            # 三层渐进式策略加载 + 版本竞争
│   │   │   ├── version_manager.py         # VersionManager 门面
│   │   │   ├── curator.py                 # 自动策展人（两阶段维护）
│   │   │   └── in_game_flagger.py         # [FLAG] 即时标记
│   │   ├── memory/                        # 四层记忆系统
│   │   │   ├── working_memory.py          # L1: 局内工作记忆
│   │   │   ├── opponent_model.py          # L2: 对手建模 (YAML)
│   │   │   ├── self_model.py              # L3: 自我建模 (YAML)
│   │   │   └── game_archive.py            # L4: 对局归档 (SQLite)
│   │   └── utils/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── requirements.txt
│
├── paoding-werewolf-service/              # Repo 2: 游戏服务 + 仪表板
│   ├── backend/                           # FastAPI 后端
│   │   ├── main.py                        # 主入口：房间/对局/WebSocket/Agent 管理
│   │   ├── evolution_dashboard/           # ★ 自进化仪表板 API
│   │   │   ├── router.py                  # /api/evolution/* 端点
│   │   │   └── reader.py                  # 从 ~/.werewolf-agent/ 读文件系统
│   │   ├── agents/                        # 各协议 Agent 客户端
│   │   │   ├── ws_agent_client.py         # WebSocket Agent
│   │   │   ├── http_agent_client.py       # HTTP Agent
│   │   │   ├── wis_agent_client.py        # WIS 协议 Agent
│   │   │   └── builtin_client.py          # 内置 AI
│   │   ├── game/engine.py                 # 对局引擎
│   │   ├── auth/                          # JWT 认证
│   │   ├── core/models.py                 # SQLAlchemy 模型
│   │   └── requirements.txt
│   ├── frontend/                          # Next.js 前端
│   │   ├── src/
│   │   │   ├── app/evolution/page.tsx     # ★ 自进化监控台页面
│   │   │   ├── lib/evolution-api.ts       # 进化 API 客户端
│   │   │   ├── lib/auth.ts               # 共享认证客户端
│   │   │   └── components/Sidebar.tsx     # 侧边栏（管理员可见进化入口）
│   │   └── Dockerfile
│   └── docker-compose.yml
│
└── docs/superpowers/                      # 设计文档
    └── plans/                             # 执行计划书
```

### Production Server

| Item | Value |
|------|-------|
| **Server IP** | `<server-ip>` |
| **SSH** | `ssh <user>@<server-ip> -p <port>` (password: `REMOVED_SSH_PASSWORD`) |
| **Source dirs** | `/root/werewolf-agent-langgraph/`, `/root/werewolf-service/` |
| **CI/CD** | Drone CI at `:8090` — push to `main` triggers auto build+deploy |

### Running Containers

| Container | Image | Ports | Compose Project | Role |
|-----------|-------|-------|-----------------|------|
| `werewolf-agent-langgraph` | `werewolf-agent-langgraph-werewolf-agent` | `8082→7861`(WS), `8083→7860`(HTTP) | `werewolf-agent-langgraph` | LLM Agent 服务，运行对局决策 + 自进化管道 |
| `werewolf-backend-new` | `werewolf-service-werewolf-backend` | `8081→8000` | `werewolf-service` | FastAPI 游戏服务，房间/对局管理 + 进化仪表板 API |
| `werewolf-frontend-new` | `werewolf-service-werewolf-frontend` | `3000→3000` | `werewolf-service` | Next.js 前端 |
| `werewolf-mysql` | `mysql:8.0.39` | `3307→3306` | default bridge | 游戏服务 MySQL |
| `werewolf-redis` | `redis:7.4-alpine` | `6380→6379` | default bridge | 会话/缓存 Redis |

### Shared Volume (Critical for Evolution Dashboard)

Both `werewolf-agent-langgraph` and `werewolf-backend-new` mount the same host directory:

```
Host: /opt/werewolf-agent-data  →  Container: /root/.werewolf-agent
```

The agent writes evolution data here; the backend reads it. Both containers set `WEREWOLF_AGENT_HOME=/root/.werewolf-agent`.

### Data Flow: Self-Evolution Pipeline

```
游戏结束 (game_over)
  │
  ▼ main_ws.py: _process_game_over()
  │  skip_evolution=True? ──YES──▶ 跳过（局内有 builtin AI 时）
  │  NO
  ▼ _run_post_game_pipeline() [asyncio.create_task]
  │
  ├─1. format_game_trace()
  ├─2. InGameFlagger.extract_flags()   ← 提取 [FLAG] 标记
  ├─3. VersionManager 加载当前策略
  ├─3.5 WorkingMemory 格式化
  ├─4. ReflectionEngine.reflect()      ← LLM 调用，产出 ReflectionResult
  │     ↓ match_level=high/medium → 进入缓冲池
  │     ↓ match_level=low/strategy_gap → 仅记录 gap
  ├─5. BufferPool.ingest()             → pending/*.yaml
  ├─6. SuggestionClusterer.process_pending() → clusters/*.yaml
  ├─7. ConfirmationJudge.check_all_clusters() → confirmed/*.yaml + 新策略版本
  ├─8. record_strategy_gap()           → SQLite strategy_gaps 表
  ├─9. save_game()                     → SQLite games 表
  ├─10. update_self_model()            → memory/self_model/profile.yaml
  ├─10.5 update_opponent_from_game()   → memory/opponents/*.yaml
  ├─10.5 record_version_usage()        → skills/*/.versions.json
  └─11. expire_old_suggestions()
```

### Data on Shared Volume

```
/root/.werewolf-agent/
├── skills/                             # 策略版本库
│   ├── .skill_index.json               # 全局索引
│   ├── .curator_backups/               # Curator 快照
│   ├── seer/identity-timing/           # 角色子目录
│   │   ├── .versions.json              # 版本元数据
│   │   ├── v1.md                       # 策略 Markdown
│   │   └── v2.md
│   └── wolf/...
├── policy_buffer/                      # 建议缓冲池
│   ├── pending/*.yaml                  # 待处理建议
│   ├── clusters/*.yaml                 # 聚簇（含多个相似建议）
│   ├── confirmed/*.yaml                # 已确认聚簇
│   └── expired/*.yaml                  # 已过期建议
├── memory/
│   ├── curator_state.json              # Curator 运行状态
│   ├── self_model/profile.yaml         # 自我建模
│   ├── opponents/*.yaml                # 对手建模
│   └── game_archive/games.db           # SQLite (games + strategy_gaps 表)
└── config.yaml                         # 自进化配置（可选，覆盖默认值）
```

### Backend API: Evolution Dashboard

All endpoints under `/api/evolution`, require **admin JWT** (`Depends(require_admin)`):

| Method | Path | Data Source |
|--------|------|-------------|
| GET | `/overview` | Filesystem + MySQL Game count |
| GET | `/skills` | `skills/` directory scan |
| GET | `/skills/{name}` | `.versions.json` + current `.md` |
| POST | `/skills/{name}/rollback` | Modify `.versions.json` |
| POST | `/skills/{name}/pin` | Modify `.versions.json` |
| GET | `/buffer` | `policy_buffer/` YAML files |
| GET | `/buffer/clusters/{id}` | Single cluster YAML |
| POST | `/buffer/clusters/{id}/force-confirm` | ★ BROKEN (dynamic import fails in Docker) |
| GET | `/skills/{name}/versions/{v}/content` | Version `.md` file |
| DELETE | `/skills/{name}/versions/{v}` | Delete `.md` + update `.versions.json` |
| POST | `/skills/{name}/versions` | ★ BROKEN (dynamic import fails in Docker) |
| GET | `/skills/{name}/diff` | Two version `.md` files |
| GET | `/gaps` | SQLite `strategy_gaps` table |
| GET | `/games` | MySQL `games` + `rooms` tables |

### LLM Configuration

Agent uses **OpenAI-compatible API** via `llm_caller.py`:
- Base URL: `https://claude35.shop/v1` (hardcoded in `llm_caller.py:108`)
- Default model: `deepseek-v4-pro` (gameplay), configurable for evolution modules
- Evolution config can override: `clustering_model`, `reflection_model`

### MySQL Schema (relevant tables)

```sql
-- games table (large JSON columns!)
games (
  room_id       VARCHAR(64) PK,
  round_count   INT,
  duration_seconds INT,
  players_json  JSON,        -- seat config, can be large
  events_json   JSON,        -- ★ full replay events, VERY large
  total_events  INT,
  sheriff_elected TINYINT(1),
  created_at    DATETIME
)

-- rooms table
rooms (
  room_id       VARCHAR(64) PK,
  winner        VARCHAR(32),
  -- ... other fields
)
```

### Deploy Workflow

Push to `main` branch → Drone CI auto-triggers:

1. **Agent repo**: `pip install` + `compileall` check → `docker compose build` → `docker compose up -d --force-recreate`
2. **Service repo**: backend `pytest` + frontend `npm run build` → `docker compose build` → `docker compose up -d --force-recreate`

For manual deploy on the server:
```bash
cd /root/werewolf-agent-langgraph && git pull origin main && docker compose -p werewolf-agent-langgraph up -d --build --force-recreate
cd /root/werewolf-service && git pull origin main && docker compose -p werewolf-service up -d --build --force-recreate
```

### Key Design Decisions to Be Aware Of

1. **`skip_evolution=True` when builtin AI is present** — `main.py:591-595` checks if ANY player is builtin AI and broadcasts `skip_evolution=True` to ALL agents. This is intentional to prevent noisy data from contaminating the evolution pipeline. Only games with 100% external LLM agents produce evolution data.

2. **Filesystem-based storage** — The evolution system uses YAML/JSON/Markdown files on the shared volume, not a database. The backend `reader.py` reads these files directly. This means: no transactions, no file locking, concurrent write risks.

3. **Evolution modules are Python-only** — The agent's `evolution/` package is pure Python with no web server. It runs as library code called from `main_ws.py`'s post-game pipeline. The backend cannot import it (separate Docker image).

4. **Admin-only dashboard** — All `/api/evolution/*` endpoints require a valid admin JWT. The frontend sends this via `Authorization: Bearer <token>` header. Non-admin users cannot access the evolution page (hidden in sidebar).

---

**Architecture:** Three-service system. (1) `werewolf-agent-langgraph` runs the post-game evolution pipeline (reflection, buffer, clustering, confirmation, versioning, memory). (2) `paoding-werewolf-service/backend` serves the evolution dashboard API, reading agent data from a shared volume. (3) `paoding-werewolf-service/frontend` renders the dashboard UI.

**Tech Stack:** Python (FastAPI, asyncio, OpenAI SDK, SQLite), TypeScript (Next.js/React)

**Prerequisite:** Shared volume already configured (`/opt/werewolf-agent-data` bind-mounted to `/root/.werewolf-agent` in both agent and backend containers).

---

## File Map

### Modified files

| File | Responsibility |
|------|---------------|
| `werewolf-agent-langgraph/app/main_ws.py` | Fix in_game_flags accumulation, add curator invocation, fix versions_used population |
| `werewolf-agent-langgraph/app/agents/agent_graph.py` | Accumulate in_game_flags during gameplay, populate versions_used |
| `werewolf-agent-langgraph/app/agents/state.py` | No changes needed (types already correct) |
| `werewolf-agent-langgraph/app/evolution/reflection_engine.py` | Fix LLM error handling |
| `werewolf-agent-langgraph/app/evolution/clustering.py` | Fix semantic consistency default, fix non-unique cluster ID |
| `werewolf-agent-langgraph/app/evolution/confirmation.py` | No structural changes needed |
| `werewolf-agent-langgraph/app/evolution/skill_loader.py` | Fix `last_used` update in `record_version_usage` |
| `werewolf-agent-langgraph/app/evolution/curator.py` | Fix `should_run` to respect `min_idle_hours` |
| `paoding-werewolf-service/backend/evolution_dashboard/reader.py` | Remove broken dynamic imports, add path validation, fix double-load bug, add error handling |
| `paoding-werewolf-service/backend/evolution_dashboard/router.py` | Fix MySQL query, implement force_confirm/create_version without dynamic import |
| `paoding-werewolf-service/backend/requirements.txt` | Add PyYAML dependency |
| `paoding-werewolf-service/frontend/src/app/evolution/page.tsx` | Use Promise.allSettled, add expired_count/curator_last_run display, fix diff UX |
| `paoding-werewolf-service/frontend/src/lib/evolution-api.ts` | Fix header merging, add 204 handling |

### Created files

| File | Responsibility |
|------|---------------|
| `werewolf-agent-langgraph/app/evolution/file_lock.py` | Simple file-based locking for concurrent pipeline access |

---

## Part 1: CRITICAL — Backend API Fixes (dashboard 不可用的直接原因)

### Task 1: Remove broken dynamic import, reimplement force_confirm and create_version without langgraph dependency

**Problem:** `reader.py:243,330` computes `Path(__file__).parents[3] / "werewolf-agent-langgraph"` to dynamically import agent code. This path does not exist in the backend Docker container, so `force_confirm_cluster()` and `create_manual_version()` always fail with `ImportError`. Additionally, `sys.modules["evolution"]` is popped but never restored (`reader.py:249`), corrupting future imports.

**Fix:** Reimplement both operations using only local filesystem operations (write `.versions.json`, write `.md` files, move YAML files). No dynamic imports needed.

- [ ] 1.1 In `reader.py`, rewrite `force_confirm_cluster()` to:
  - Read cluster YAML from `clusters/{cluster_id}.yaml`
  - Synthesize a simple merged strategy text from the cluster's suggestions (concatenate top suggestion texts with a header, no LLM call needed — LLM synthesis is a nice-to-have that can be re-added later via a dedicated microservice call)
  - Call `_create_version_from_content()` helper (new) to write the `.md` file and update `.versions.json`
  - Move cluster YAML from `clusters/` to `confirmed/`
  - Remove all `importlib` / `sys.modules` manipulation

- [ ] 1.2 In `reader.py`, rewrite `create_manual_version()` to:
  - Use the same `_create_version_from_content()` helper
  - Remove all `importlib` / `sys.modules` manipulation

- [ ] 1.3 Add `_create_version_from_content(skill_name, content, source, trigger_cluster)` helper to `reader.py`:
  ```python
  def _create_version_from_content(skill_name: str, content: str,
                                    source: str = "manual_create",
                                    trigger_cluster: str = "") -> Optional[str]:
      skills_root = AGENT_HOME / "skills"
      skill_dir = _find_skill_dir(skills_root, skill_name)

      if not skill_dir:
          # Create new skill directory
          parts = skill_name.split("-", 1)
          if len(parts) == 2:
              skill_dir = skills_root / parts[0] / parts[1]
          else:
              skill_dir = skills_root / "common" / skill_name
          skill_dir.mkdir(parents=True, exist_ok=True)
          meta = {"skill_name": skill_name, "current_default": "v0", "versions": {}}
      else:
          meta = _load_json(skill_dir / ".versions.json") or {
              "skill_name": skill_name, "current_default": "v1", "versions": {}
          }

      existing = meta.get("versions", {})
      version_num = max(
          (int(v.replace("v", "")) for v in existing if v.startswith("v")),
          default=0
      ) + 1
      version_name = f"v{version_num}"

      (skill_dir / f"{version_name}.md").write_text(content)

      meta["versions"][version_name] = {
          "created_at": datetime.now(timezone.utc).isoformat(),
          "source": source,
          "trigger_cluster": trigger_cluster,
          "pinned": False,
          "status": "candidate",
          "usage": {"games_played": 0, "wins": 0, "win_rate": 0, "last_used": None}
      }

      with open(skill_dir / ".versions.json", "w") as f:
          json.dump(meta, f, indent=2, ensure_ascii=False)

      _rebuild_index(skills_root)
      return version_name
  ```

**Verify:** Call `POST /api/evolution/buffer/clusters/{id}/force-confirm` and `POST /api/evolution/skills/{name}/versions` in the running container. Both should succeed without ImportError.

---

### Task 2: Fix MySQL sort buffer overflow in `_get_recent_games_from_db`

**Problem:** `router.py:151` loads full `Game` rows including `events_json` (large JSON column) for sorting. MySQL sort buffer overflows on large tables. This is already happening in production (`Out of sort memory` error). Additionally, N+1 query pattern (separate `Room` query per game).

**Fix:** Defer the large column and use JOIN for Room.

- [ ] 2.1 In `router.py`, replace `_get_recent_games_from_db()`:
  ```python
  def _get_recent_games_from_db(limit: int = 20):
      from core.models import Game, Room
      from sqlalchemy import desc, defer

      db = _get_db()
      try:
          games = (
              db.query(Game)
              .options(defer(Game.events_json))  # Don't load the huge replay column
              .order_by(desc(Game.created_at))
              .limit(limit)
              .all()
          )
          # Batch-load rooms instead of N+1
          room_ids = [g.room_id for g in games]
          rooms = {r.room_id: r for r in db.query(Room).filter(Room.room_id.in_(room_ids)).all()} if room_ids else {}

          result = []
          for game in games:
              room = rooms.get(game.room_id)
              players = game.seats_json or []
              if not isinstance(players, list):
                  players = []
              has_builtin_ai = any(
                  (p.get("agent", {}).get("type") or "").lower() == "builtin"
                  for p in players
              )
              result.append({
                  "game_id": game.room_id,
                  "winner": room.winner if room else None,
                  "round_count": game.round_count,
                  "players_count": len(players),
                  "duration_seconds": game.duration_seconds or 0,
                  "created_at": game.created_at.isoformat() if game.created_at else None,
                  "has_builtin_ai": has_builtin_ai,
                  "players": [
                      {
                          "id": p.get("id"),
                          "name": p.get("name") or p.get("label"),
                          "role": p.get("role"),
                          "agent_id": (p.get("agent") or {}).get("id"),
                          "agent_name": (p.get("agent") or {}).get("name"),
                          "is_alive": p.get("is_alive", False),
                      }
                      for p in players
                  ],
              })
          return result
      except Exception:
          db.rollback()
          return []
      finally:
          db.close()
  ```

**Verify:** Call `GET /api/evolution/games?limit=5` in production. Should return game list without MySQL error.

---

### Task 3: Add PyYAML to backend requirements.txt

**Problem:** `reader.py` uses `import yaml` but `PyYAML` is not listed in `paoding-werewolf-service/backend/requirements.txt`. Will crash at runtime if not transitively installed.

- [ ] 3.1 Add `pyyaml>=6.0` to `paoding-werewolf-service/backend/requirements.txt`

**Verify:** `docker exec werewolf-backend-new python -c "import yaml; print(yaml.__version__)"`

---

### Task 4: Add path validation to prevent directory traversal

**Problem:** `reader.py:218,292,316` — user-supplied `cluster_id`, `version`, `skill_name` are directly concatenated into file paths. A value like `../../etc/passwd` could read or delete arbitrary files.

**Fix:** Add a validation helper.

- [ ] 4.1 Add helper function to `reader.py`:
  ```python
  def _safe_path_component(value: str) -> str:
      """Reject path traversal attempts."""
      if not value or ".." in value or "/" in value or "\\" in value:
          raise ValueError(f"Invalid path component: {value!r}")
      return value
  ```

- [ ] 4.2 Apply `_safe_path_component()` to `cluster_id` in `get_cluster_detail()`, `force_confirm_cluster()`; `version` in `get_version_content()`, `delete_version()`, `diff_versions()`; `skill_name` in functions that accept it.

**Verify:** Call `GET /api/evolution/buffer/clusters/..%2F..%2Fetc%2Fpasswd` — should return 400, not 500 or file content.

---

## Part 2: CRITICAL — Agent Pipeline Fixes (自进化管道无法产数据的直接原因)

### Task 5: Accumulate in_game_flags during gameplay

**Problem:** `state.py:83` initializes `in_game_flags: []` but no code in `agent_graph.py` ever appends to it. The `[FLAG]` prompt is injected into the agent's system prompt (`prompt_builder.py:78-79`), and the agent may output `[FLAG]` markers in its thoughts during any round. But only `main_ws.py:360` extracts flags from the **last** `last_thought`, missing all flags from earlier rounds.

**Fix:** After each `_reflect_node` call in `agent_graph.py`, extract `[FLAG]` markers and append to state.

- [ ] 5.1 In `agent_graph.py`, add a post-processing step to `_reflect_node`:
  ```python
  def _reflect_node(state: AgentState) -> AgentState:
      # ... existing code ...
      reflection = llm.call_with_log_sync(...)
      update = {"last_thought": reflection}

      # Extract in-game flags from this round's thought
      from evolution.in_game_flagger import InGameFlagger
      flagger = InGameFlagger()
      new_flags = flagger.extract_flags(reflection)
      if new_flags:
          existing_flags = state.get("in_game_flags", [])
          update["in_game_flags"] = existing_flags + new_flags

      return {**state, **update}
  ```

- [ ] 5.2 In `main_ws.py`, remove the redundant `flagger.extract_flags(state.get("last_thought", ""))` call at line 360, since flags are now accumulated during gameplay. Keep the `flags.extend(state.get("in_game_flags", []))` line.

**Verify:** Play a game with the agent. After game_over, check that `in_game_flags` in the state contains flags from multiple rounds, not just the last one.

---

### Task 6: Populate `versions_used` during gameplay to enable version competition

**Problem:** `state.py:82` initializes `versions_used: {}` but no code ever writes to it. The `VersionManager.get_version_for_game()` method exists to select candidate vs default versions, but is never called from the game loop. Consequently, `main_ws.py:456-459` never records any version usage, and the version competition/promotion system is entirely dead.

**Fix:** At game init time, load strategies and record which version was selected.

- [ ] 6.1 In `agent_graph.py` or the init handler, when loading strategies for the agent's role, call `VersionManager.get_version_for_game()` for each relevant skill and store the result in `state["versions_used"]`:
  ```python
  # In _process_init or equivalent, after role is known:
  from evolution.config import load_config
  from evolution.version_manager import VersionManager
  cfg = load_config()
  vm = VersionManager(cfg)
  index = vm.loader.load_index()
  versions_used = {}
  for skill in index:
      if skill.get("role") in (role, "common"):
          v = vm.loader.get_version_for_game(skill["name"])
          versions_used[skill["name"]] = v
  initial["versions_used"] = versions_used
  ```

- [ ] 6.2 Verify that `main_ws.py:456-459` now correctly iterates non-empty `versions_used` and calls `vm.record_usage()`.

**Verify:** Play a game. After game_over, check that `.versions.json` for the used skill has updated `games_played`, `wins`, `win_rate`, and `last_used` values.

---

### Task 7: Invoke Curator after post-game pipeline

**Problem:** `Curator` class is defined but never instantiated or called from `main_ws.py`. Phase 1 state transitions (active→stale→archived) and Phase 2 LLM reviews never execute.

**Fix:** Add curator check at the end of the post-game pipeline.

- [ ] 7.1 In `main_ws.py`, at the end of `_run_post_game_pipeline()` (after the existing step 11), add:
  ```python
  # 12. Check if Curator should run
  from evolution.curator import Curator
  curator = Curator(cfg)
  if curator.should_run(is_game_in_progress=False):
      try:
          summary = curator.run()
          logger.info(f"Curator run completed: {summary}")
      except Exception as e:
          logger.warning(f"Curator run failed: {e}")
  ```

**Verify:** After enough games to exceed `interval_hours` (or temporarily set `interval_hours: 0` in config), check that `curator_state.json` updates and stale versions get demoted.

---

### Task 8: Fix Curator `should_run()` to respect `min_idle_hours`

**Problem:** `curator.py:28-42` — `should_run()` checks `interval_hours` but ignores `min_idle_hours` from config. The `is_game_in_progress` parameter is passed but nobody tracks game state.

**Fix:** Use a simple heuristic — if a game just ended, we're likely not idle. Track idle time via `curator_state.json`.

- [ ] 8.1 In `curator.py`, modify `should_run()`:
  ```python
  def should_run(self, is_game_in_progress: bool = False) -> bool:
      if is_game_in_progress:
          return False
      if not self.cfg.curator.enabled:
          return False

      state = self._load_state()
      last_run = state.get("last_run_at")

      if not last_run:
          # First run — record timestamp, don't run yet
          self._save_state({"last_run_at": datetime.now(timezone.utc).isoformat()})
          return False

      now = datetime.now(timezone.utc)
      last_run_dt = datetime.fromisoformat(last_run)
      hours_since_run = (now - last_run_dt).total_seconds() / 3600

      if hours_since_run < self.cfg.curator.interval_hours:
          return False

      # Check idle: if any game ended recently (within min_idle_hours), skip
      last_game_end = state.get("last_game_end_at")
      if last_game_end:
          hours_since_game = (now - datetime.fromisoformat(last_game_end)).total_seconds() / 3600
          if hours_since_game < self.cfg.curator.min_idle_hours:
              return False

      return True
  ```

- [ ] 8.2 In `main_ws.py`, at the end of `_run_post_game_pipeline()`, record game end time:
  ```python
  # Update curator state with game end time
  curator_state_path = AGENT_HOME / "memory" / "curator_state.json"
  curator_state = {}
  if curator_state_path.exists():
      with open(curator_state_path) as f:
          curator_state = json.load(f)
  curator_state["last_game_end_at"] = datetime.now(timezone.utc).isoformat()
  curator_state_path.parent.mkdir(parents=True, exist_ok=True)
  with open(curator_state_path, "w") as f:
      json.dump(curator_state, f)
  ```

**Verify:** Set `curator.min_idle_hours: 0` temporarily. After a game ends, curator should run on the next pipeline invocation.

---

## Part 3: HIGH — Data Correctness Fixes

### Task 9: Fix LLM error handling in reflection engine

**Problem:** `reflection_engine.py:220` — on exception, `_call_llm` returns `f"ERROR: {e}"` string instead of raising or returning empty. This string gets parsed as YAML, potentially producing garbage data.

**Fix:** Return empty string on error, and add an early check before YAML parsing.

- [ ] 9.1 In `reflection_engine.py`, change `_call_llm`:
  ```python
  def _call_llm(self, user_msg: str) -> str:
      client = self.reflect_llm.client
      try:
          resp = client.chat.completions.create(
              model=self.reflect_llm.model,
              messages=[
                  {"role": "system", "content": REFLECT_SYSTEM_PROMPT},
                  {"role": "user", "content": user_msg},
              ],
              temperature=0.3,
          )
          return resp.choices[0].message.content or ""
      except Exception as e:
          logger.warning(f"Reflection LLM call failed: {e}")
          return ""
  ```

- [ ] 9.2 In `reflect()`, add early return on empty response:
  ```python
  response = self._call_llm(user_msg)
  if not response:
      return None
  ```

**Verify:** Simulate an LLM API failure (wrong API key). Verify that `reflect()` returns `None` instead of a garbage `ReflectionResult`.

---

### Task 10: Fix semantic consistency check default on LLM failure

**Problem:** `clustering.py:175` — `_check_semantic_consistency` returns `True` (pass) when LLM call fails. Inconsistent suggestions get silently added to clusters.

**Fix:** Default to `False` on failure.

- [ ] 10.1 In `clustering.py`, change the exception handler in `_check_semantic_consistency`:
  ```python
  except Exception:
      return False  # Reject on uncertainty
  ```

**Verify:** Simulate LLM failure during clustering. Verify that the suggestion is NOT added to the existing cluster.

---

### Task 11: Fix `last_used` not being updated in `record_version_usage`

**Problem:** `skill_loader.py:160-176` — `record_version_usage` updates `games_played`, `wins`, `win_rate` but never sets `last_used`. Curator's staleness check (`curator.py:74`) falls back to `created_at`, so actively used versions get marked stale.

**Fix:** Set `last_used` to current timestamp.

- [ ] 11.1 In `skill_loader.py`, in `record_version_usage`, add after `usage["win_rate"]` update:
  ```python
  usage["last_used"] = datetime.now(timezone.utc).isoformat()
  ```

**Verify:** Play a game. Check that `.versions.json` has `last_used` populated for the used version.

---

### Task 12: Add basic file locking for concurrent pipeline access

**Problem:** `main_ws.py:303` uses `asyncio.create_task` for fire-and-forget post-game pipeline. Two games ending simultaneously can cause concurrent writes to the same files (`.versions.json`, `profile.yaml`, cluster YAML files), leading to data corruption.

**Fix:** Add an asyncio Lock in `main_ws.py` to serialize pipeline execution. Also add a simple advisory lock for file-level operations.

- [ ] 12.1 In `main_ws.py`, add a module-level lock:
  ```python
  _pipeline_lock = asyncio.Lock()
  ```

- [ ] 12.2 In `_run_post_game_pipeline`, acquire the lock:
  ```python
  async def _run_post_game_pipeline(state, result, winner_role, all_roles, session_id, req_id):
      async with _pipeline_lock:
          try:
              # ... existing pipeline code ...
          except Exception as e:
              logger.error(f"Post-game pipeline failed: {e}", exc_info=e)
  ```

**Verify:** Run two games simultaneously. Verify no file corruption in `.versions.json` or cluster files.

---

### Task 13: Fix non-unique cluster IDs causing silent overwrites

**Problem:** `clustering.py:89` — cluster ID is `f"cluster_{target_skill}_{scene_tag_key}"`. Two suggestions with the same target_skill and scene tags produce the same ID, and the second `_create_cluster` call overwrites the first.

**Fix:** Append a short UUID to cluster IDs.

- [ ] 13.1 In `clustering.py`, change cluster ID generation:
  ```python
  import uuid

  # In _assign_to_cluster:
  cluster_id = f"cluster_{target_skill}_{self._scene_tag_key(scene_tags)}_{uuid.uuid4().hex[:6]}"
  ```

- [ ] 13.2 When matching against existing clusters, match by `target_skill` + `scene_tag_key` prefix (existing behavior for finding the best cluster), but use the full unique ID for the filename.

**Verify:** Submit two suggestions with the same target_skill and scene tags. Both should create separate cluster files, not overwrite.

---

## Part 4: MEDIUM — Frontend & UX Fixes

### Task 14: Use `Promise.allSettled` for partial page rendering

**Problem:** `page.tsx:602-607` — `Promise.all` causes the entire page to show an error state if any single API call fails. The `games` endpoint is already failing due to the MySQL sort buffer bug, so the entire page is broken.

**Fix:** Use `Promise.allSettled` and render whatever succeeded.

- [ ] 14.1 In `page.tsx`, replace the `load` function's `Promise.all` with:
  ```typescript
  const results = await Promise.allSettled([
    getEvolutionOverview(),
    getEvolutionSkills(),
    getBufferStatus(),
    getStrategyGaps(),
    getRecentGames(),
  ]);

  const [o, s, b, g, gm] = results.map((r) =>
    r.status === "fulfilled" ? r.value : null
  );

  if (o) setOverview(o);
  if (s) setSkills(s);
  if (b) setBuffer(b);
  if (g) setGaps(g);
  if (gm) setGames(gm);

  const failed = results.filter((r) => r.status === "rejected");
  if (failed.length === results.length) {
    setError("所有数据加载失败");
  } else if (failed.length > 0) {
    setError(`${failed.length} 个数据源加载失败`);
  }
  ```

**Verify:** Simulate one API endpoint failing (e.g., stop MySQL). Other sections of the page should still render.

---

### Task 15: Display `expired_count` and `curator_last_run` in overview

**Problem:** Both fields are in the TypeScript type and returned by the backend, but have no corresponding UI element in the dashboard.

**Fix:** Add stat card for `expired_count` and timestamp display for `curator_last_run`.

- [ ] 15.1 In `page.tsx`, add an `expired_count` stat card to the overview grid (change from 5-col to 6-col, or add a second row).

- [ ] 15.2 In `page.tsx`, add `curator_last_run` display below the stat cards:
  ```tsx
  {overview?.curator_last_run && (
    <p className="text-sm text-gray-500">
      Curator 上次运行: {new Date(overview.curator_last_run).toLocaleString()}
    </p>
  )}
  ```

**Verify:** Load the evolution page. Both new elements should appear (expired_count as a stat card, curator timestamp as text).

---

### Task 16: Fix diff version selection UX

**Problem:** `page.tsx:420-423` — clicking diff on a version row picks an arbitrary "other" version. User cannot choose which two versions to compare when there are 3+ versions.

**Fix:** Add a two-step selection flow.

- [ ] 16.1 When user clicks diff on a version row, set `versionA` to that version. Show a prompt or dropdown to select `versionB` from the remaining versions. Alternatively, add a dedicated diff button at the skill level that opens a modal with two dropdowns.

**Verify:** Click diff on a skill with 3+ versions. User should be able to choose both versions.

---

### Task 17: Fix header merging in `evolution-api.ts`

**Problem:** `evolution-api.ts:21` — `{ ...init, headers }` overwrites any `init.headers`. Currently no callers pass headers via `init`, so it's latent, but it's a correctness issue and diverges from the shared `auth.ts` pattern.

**Fix:** Merge headers properly.

- [ ] 17.1 In `evolution-api.ts`, change the request function:
  ```typescript
  const reqHeaders = new Headers(init?.headers);
  reqHeaders.set("Content-Type", "application/json");
  if (token) reqHeaders.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${API_BASE}${path}`, { ...init, headers: reqHeaders });
  ```

- [ ] 17.2 Add HTTP 204 handling:
  ```typescript
  if (res.status === 204) return null as any;
  ```

**Verify:** All evolution API calls still work. Test with an admin token.

---

## Part 5: Robustness — Error Handling & Edge Cases

### Task 18: Fix `curator_state.json` double-load and TOCTOU in `get_overview`

**Problem:** `reader.py:50` loads `curator_state.json` twice in the same expression. Between the two reads, the file could be deleted or modified.

**Fix:** Load once.

- [ ] 18.1 In `reader.py`, fix `get_overview()`:
  ```python
  def get_overview() -> Dict[str, Any]:
      skills_root = AGENT_HOME / "skills"
      buffer_root = _buffer_root()
      # ... skill_count logic ...

      curator_state = _load_json(AGENT_HOME / "memory" / "curator_state.json")
      curator_last_run = curator_state.get("last_run_at") if curator_state else None

      return {
          "pending_count": _count_yaml(buffer_root / "pending"),
          "cluster_count": _count_yaml(buffer_root / "clusters"),
          "confirmed_count": _count_yaml(buffer_root / "confirmed"),
          "expired_count": _count_yaml(buffer_root / "expired"),
          "skill_count": skill_count,
          "curator_last_run": curator_last_run,
      }
  ```

**Verify:** Call `/api/evolution/overview` while `curator_state.json` exists. Should return the value without error.

---

### Task 19: Wrap SQLite connections in try/finally

**Problem:** `reader.py:371-379,390-398` — SQLite connections are not wrapped in try/finally. If a query throws, the connection leaks.

- [ ] 19.1 In `reader.py`, wrap both `get_strategy_gaps` and `get_recent_games`:
  ```python
  def get_strategy_gaps(min_count: int = 3) -> List[Dict[str, Any]]:
      db_path = AGENT_HOME / "memory" / "game_archive" / "games.db"
      if not db_path.exists():
          return []
      import sqlite3
      conn = sqlite3.connect(str(db_path))
      try:
          conn.row_factory = sqlite3.Row
          rows = conn.execute(...).fetchall()
          return [dict(r) for r in rows]
      except Exception:
          return []
      finally:
          conn.close()
  ```

- [ ] 19.2 Apply same pattern to `get_recent_games`.

**Verify:** Corrupt the `games.db` file. Call the gaps endpoint. Should return `[]` without hanging or leaking connections.

---

### Task 20: Handle malformed JSON/YAML in reader

**Problem:** `reader.py:14-26` — `_load_json` and `_load_yaml` don't handle malformed files. A corrupted `.versions.json` or cluster YAML causes a 500 error on the API.

**Fix:** Wrap in try/except.

- [ ] 20.1 In `reader.py`:
  ```python
  def _load_json(path: Path) -> Optional[dict]:
      if not path.exists():
          return None
      try:
          with open(path) as f:
              return json.load(f)
      except (json.JSONDecodeError, OSError):
          return None

  def _load_yaml(path: Path) -> Optional[dict]:
      if not path.exists():
          return None
      try:
          import yaml
          with open(path) as f:
              return yaml.safe_load(f) or {}
      except (yaml.YAMLError, OSError):
          return {}
  ```

**Verify:** Create a malformed `.versions.json` in a skill directory. Call `/api/evolution/skills`. Should return the other skills without 500 error.

---

## Execution Order

Execute in this order to maximize impact per deployment:

| Step | Task IDs | Impact | Deploy Required |
|------|----------|--------|-----------------|
| 1 | Task 1, 2, 3, 4, 18, 19, 20 | Backend API fixes — dashboard becomes functional | Backend only |
| 2 | Task 5, 6, 7, 8 | Agent pipeline fixes — evolution data actually gets produced | Agent only |
| 3 | Task 9, 10, 11, 12, 13 | Data correctness — produced data is accurate and safe | Agent only |
| 4 | Task 14, 15, 16, 17 | Frontend UX — dashboard displays everything properly | Frontend only |

Steps 1 and 2 can be deployed independently. Step 4 depends on Step 1 (API must work first).

---

## Verification Checklist

After all tasks are complete:

- [ ] Start a game with external LLM agents (no builtin AI)
- [ ] After game ends, check `/opt/werewolf-agent-data/policy_buffer/pending/` — should contain YAML files
- [ ] After 2+ games, check `/opt/werewolf-agent-data/policy_buffer/clusters/` — should contain cluster YAML
- [ ] Check `/opt/werewolf-agent-data/skills/` — should have skill directories with `.versions.json`
- [ ] Open the evolution dashboard in the browser — all sections should show data
- [ ] Click "强制确认" on a cluster — should succeed without 500
- [ ] Create a manual version — should succeed without 500
- [ ] Check "近期对局" section — should show games without MySQL error
- [ ] Refresh the page with one API endpoint down — other sections should still render
