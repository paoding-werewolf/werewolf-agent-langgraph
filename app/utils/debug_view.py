"""Shared rendering helpers for the /debug/prompts and /debug/view endpoints.

Used by both the HTTP service (main.py) and the WebSocket service (main_ws.py)
so the prompt debugger looks and behaves identically across transports.
"""
from html import escape
from typing import Any, Dict, List


def _card(index: int, item: Dict[str, Any]) -> str:
    session_id = item.get("session_id", "")
    session_badge = (
        f'<span style="background: #ede7f6; color: #5e35b1; padding: 2px 8px; '
        f'border-radius: 4px; margin-left: 10px; font-size: 0.9em;">'
        f'session: {escape(str(session_id))}</span>'
        if session_id else ""
    )
    return f"""
        <div class="card" style="border: 1px solid #ccc; margin-bottom: 10px; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <div class="card-header" onclick="toggle('card-{index}')" style="background: #f5f5f5; padding: 12px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eee;">
                <span>
                    <span style="color: #666; font-size: 0.9em;">[{escape(str(item.get('timestamp', '')))}]</span>
                    <strong style="margin-left: 10px;">Agent: {escape(str(item.get('agent_id', '')))}</strong>
                    <span style="background: #e3f2fd; color: #1976d2; padding: 2px 8px; border-radius: 4px; margin-left: 10px; font-size: 0.9em;">{escape(str(item.get('phase', '')))}</span>
                    {session_badge}
                </span>
                <span id="icon-card-{index}">&#x25BC;</span>
            </div>
            <div id="card-{index}" class="card-body" style="display: none; padding: 15px; white-space: pre-wrap; font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; background: #fff; font-size: 13px;">
                <div style="background: #fff3e0; padding: 10px; border-radius: 4px; border-left: 4px solid #ff9800; margin-bottom: 15px;">
                    <strong style="color: #e65100;">[SYSTEM PROMPT]</strong><br/>{escape(str(item.get('system_prompt', '')))}
                </div>
                <div style="background: #f1f8e9; padding: 10px; border-radius: 4px; border-left: 4px solid #4caf50; margin-bottom: 15px;">
                    <strong style="color: #1b5e20;">[USER MESSAGE]</strong><br/>{escape(str(item.get('user_msg', '')))}
                </div>
                <div style="background: #eceff1; padding: 10px; border-radius: 4px; border-left: 4px solid #607d8b;">
                    <strong style="color: #263238;">[LLM RESPONSE]</strong><br/>{escape(str(item.get('response', '')))}
                </div>
            </div>
        </div>
        """


def render_prompts_html(history: List[Dict[str, Any]], title: str = "Agent Prompt Debugger") -> str:
    """Render the prompt history into the collapsible HTML debugger page."""
    cards = [_card(i, item) for i, item in enumerate(reversed(history))]
    safe_title = escape(title)
    return f"""
    <!DOCTYPE html>
    <html>
        <head>
            <meta charset="UTF-8">
            <title>{safe_title}</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; padding: 30px; background: #fafafa; line-height: 1.5; }}
                .container {{ max-width: 1100px; margin: 0 auto; }}
                h1 {{ color: #24292e; border-bottom: 1px solid #eaecef; padding-bottom: 10px; }}
                .card:hover {{ box-shadow: 0 4px 8px rgba(0,0,0,0.15); }}
                hr {{ border: 0; border-top: 1px solid #eee; margin: 15px 0; }}
            </style>
            <script>
                function toggle(id) {{
                    var el = document.getElementById(id);
                    var icon = document.getElementById('icon-' + id);
                    if (el.style.display === 'none') {{
                        el.style.display = 'block';
                        icon.innerHTML = '&#x25B2;';
                    }} else {{
                        el.style.display = 'none';
                        icon.innerHTML = '&#x25BC;';
                    }}
                }}
            </script>
        </head>
        <body>
            <div class="container">
                <h1>{safe_title}</h1>
                <p style="color: #666;"></p>
                <div id="list">
                    {''.join(cards) if cards else '<p style="text-align: center; color: #999; margin-top: 50px;"></p>'}
                </div>
            </div>
        </body>
    </html>
    """
