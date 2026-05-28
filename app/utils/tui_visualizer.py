from core.game_state import AgentGameState
from core.enums import Role
from core.state_machine import PHASE_TRANSITIONS, _resolve_skip

def render_game_board(state: AgentGameState) -> str:
    """为 LLM 生成 TUI 风格的游戏面板"""
    
    board = []
    board.append("=" * 60)
    board.append(f" ROOM: {state.room_id} | DAY: {state.day} | PHASE: {state.phase}")
    board.append(f" MY ROLE: {state.my_role.value} (Player {state.me_id})")
    board.append("=" * 60)
    
    # 玩家列表
    board.append(f"{'ID':<4} | {'NAME':<15} | {'STATUS':<8} | {'ROLE (known)':<15}")
    board.append("-" * 60)
    
    # 按 ID 排序
    sorted_players = sorted(state.players.values(), key=lambda p: int(p.id))
    
    for p in sorted_players:
        status = "ALIVE" if p.is_alive else "DEAD"
        role_str = p.role.value if p.role else "?"
        if p.id == state.me_id:
            role_str = f"*{role_str}*"
        
        sheriff_mark = " [S]" if p.is_sheriff else ""
        name_str = f"{p.name}{sheriff_mark}"
        
        board.append(f"{p.id:<4} | {name_str:<15} | {status:<8} | {role_str:<15}")
    
    board.append("=" * 60)
    
    # 状态预测 (State Machine Logic)
    try:
        current_p = state.phase
        # 尝试通过状态机寻找下一个可能的交互环节
        next_p = PHASE_TRANSITIONS.get(current_p, lambda s: "unknown")(state)
        next_p = _resolve_skip(state, next_p)
        board.append(f" NEXT PREDICTED PHASE: {next_p}")
        board.append("=" * 60)
    except:
        pass
    
    # 最近事件
    board.append("\nRECENT LOGS:")
    for event in state.events[-10:]: # 只显示最近 10 条
        content = event.get("content", "")
        if content:
            board.append(f" [{event.get('status')}] {content}")
            
    return "\n".join(board)
