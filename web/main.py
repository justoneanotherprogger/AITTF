import json
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

load_dotenv()

from datetime import datetime

from db.database import init_db, get_connection, add_chat_message, get_session, extend_timer, reset_timer
from models.models import ChatMessageModel
from llm.turn_processor import process_player_action

app = FastAPI()

_active_ws: list[WebSocket] = []


async def broadcast_message(html_content: str) -> None:
    dead: list[WebSocket] = []
    for ws in _active_ws:
        try:
            await ws.send_text(html_content)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _active_ws.remove(ws)


_INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://unpkg.com/htmx.org@2.0.4"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    .htmx-indicator { opacity: 0; transition: opacity .2s; }
    .htmx-request .htmx-indicator { opacity: 1; }
    .htmx-request.htmx-indicator { opacity: 1; }
  </style>
  <title>AI Tabletop Framework</title>
</head>
<body class="bg-gray-900 text-gray-100 h-screen flex flex-col">
  <header class="bg-gray-800 border-b border-gray-700 px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-4">
      <h1 class="text-xl font-bold tracking-wide">AI Tabletop Framework</h1>
      <span id="current-player" class="text-sm text-emerald-300">{PLAYER_NAME}</span>
    </div>
    <a href="/logout" class="text-xs text-gray-400 hover:text-gray-200 underline">Сменить персонажа</a>
    <a href="/admin" class="text-xs text-gray-500 hover:text-gray-300 underline ml-3">Admin</a>
    <span id="game-status" class="text-sm px-3 py-1 rounded-full bg-emerald-700 text-emerald-200">exploration</span>
  </header>

  <div class="flex flex-1 overflow-hidden">
    <!-- Sidebar -->
    <aside id="sidebar" class="w-72 bg-gray-800 border-r border-gray-700 p-4 overflow-y-auto flex-shrink-0">
      <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Персонажи</h2>
      <div id="players-panel" hx-get="/players_panel" hx-trigger="load, every 5s" hx-swap="innerHTML">
        <div class="text-gray-500 text-sm">Загрузка...</div>
      </div>
    </aside>

    <!-- Main -->
    <main class="flex-1 flex flex-col">
      <div id="chat-messages" class="flex-1 overflow-y-auto p-4 space-y-3"
           hx-get="/chat_fragment" hx-trigger="load, every 3s" hx-swap="innerHTML">
        <div class="text-gray-500 text-sm">Загрузка истории...</div>
      </div>

      <div id="timer-bar" hx-get="/timer" hx-trigger="every 1s" hx-swap="outerHTML"></div>

      <!-- Input area -->
      <div class="border-t border-gray-700 bg-gray-800 p-4">
        <div class="flex gap-2 mb-2">
          <input id="message-input" type="text" name="text" placeholder="Сказать что-то или описать действие..." required autocomplete="off"
                 class="flex-1 bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500">
          <button type="button"
                  hx-post="/send_message" hx-include="#message-input" hx-target="#chat-messages" hx-swap="afterbegin"
                  hx-indicator="#spinner"
                  class="bg-gray-600 hover:bg-gray-500 text-white px-4 py-2 rounded-lg text-sm font-medium transition">
            Сказать
          </button>
          <button id="action-btn" type="button"
                  hx-post="/declare_action" hx-include="#message-input" hx-target="#chat-messages" hx-swap="afterbegin"
                  hx-indicator="#spinner"
                  hx-on::before-request="this.disabled=true; this.textContent='Думает...'"
                  hx-on::after-request="this.disabled=false; this.textContent='Заявить действие'"
                  class="bg-amber-600 hover:bg-amber-500 text-white px-4 py-2 rounded-lg text-sm font-medium transition">
            Заявить действие
          </button>
        </div>
        <div id="spinner" class="htmx-indicator text-amber-400 text-sm text-center">
          ✦ Мастер думает...
        </div>
      </div>
    </main>
  </div>
  <script>
    (function() {
      // WebSocket for live broadcast
      var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      var ws = new WebSocket(proto + '//' + location.host + '/ws/chat');
      ws.onmessage = function(evt) {
        var wrapper = document.createElement('div');
        wrapper.innerHTML = evt.data;
        wrapper.querySelectorAll('[hx-swap-oob]').forEach(function(el) {
          var target = document.getElementById(el.id);
          if (target) target.outerHTML = el.outerHTML;
          el.remove();
        });
        var chat = document.getElementById('chat-messages');
        var html = wrapper.innerHTML;
        if (chat && html.trim()) chat.insertAdjacentHTML('afterbegin', html);
      };
    })();
    // Clear input after any HTMX request from the two buttons
    document.body.addEventListener('htmx:afterRequest', function(evt) {
      var input = document.getElementById('message-input');
      if (input && (evt.detail.pathInfo.requestPath === '/send_message' ||
                    evt.detail.pathInfo.requestPath === '/declare_action')) {
        input.value = '';
      }
    });
  </script>
</body>
</html>"""


def _render_message(sender: str, text: str, is_action: bool = False) -> str:
    is_gm = sender == "GM"
    align = "text-right" if not is_gm else "text-left"
    bg = "bg-amber-700/40 border-amber-600/30" if is_action else ("bg-emerald-700/30 border-emerald-600/20" if is_gm else "bg-gray-700/50 border-gray-600/30")
    label = "GM" if is_gm else (f"{sender} совершает действие" if is_action else sender)
    return (
        f'<div class="{align}">'
        f'<div class="inline-block max-w-[80%] {bg} border rounded-xl px-4 py-2 text-sm">'
        f'<span class="text-xs font-semibold text-gray-400 block mb-0.5">{label}</span>'
        f'{text}'
        f'</div></div>'
    )


def _render_player_card(row) -> str:
    stats = json.loads(row["stats"])
    inv = json.loads(row["inventory"])
    effects = json.loads(row["status_effects"])
    hp_pct = round(row["hp_current"] / row["hp_max"] * 100) if row["hp_max"] > 0 else 0
    hp_color = "bg-red-500" if hp_pct < 30 else ("bg-amber-500" if hp_pct < 60 else "bg-emerald-500")
    stats_str = " | ".join(f"{k}: {v}" for k, v in stats.items())
    inv_str = ", ".join(inv) if inv else "пусто"

    return (
        f'<div class="bg-gray-750 border border-gray-600 rounded-lg p-3 mb-2">'
        f'<div class="font-semibold text-sm">{row["name"]}</div>'
        f'<div class="text-xs text-gray-400 mt-1">{row["class_archetype"] or "—"}</div>'
        f'<div class="mt-2"><div class="h-2 bg-gray-600 rounded-full overflow-hidden">'
        f'<div class="h-full {hp_color} rounded-full" style="width:{hp_pct}%"></div></div>'
        f'<span class="text-xs text-gray-400">{row["hp_current"]}/{row["hp_max"]} HP</span></div>'
        f'<div class="text-xs text-gray-400 mt-1">{stats_str}</div>'
        f'<div class="text-xs text-gray-500 mt-1">🎒 {inv_str}</div>'
        + (f'<div class="text-xs text-red-400 mt-1">⚠ {", ".join(effects)}</div>' if effects else "")
        + "</div>"
    )


@app.on_event("startup")
def startup():
    init_db()


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    _active_ws.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _active_ws.remove(websocket)
    except Exception:
        _active_ws.remove(websocket)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    pid = request.cookies.get("player_id")
    if not pid:
        return RedirectResponse(url="/choice")
    try:
        player_id = int(pid)
    except (ValueError, TypeError):
        return RedirectResponse(url="/choice")

    conn = get_connection()
    row = conn.execute("SELECT name FROM players WHERE id=?", (player_id,)).fetchone()
    conn.close()

    if not row:
        resp = RedirectResponse(url="/choice")
        resp.delete_cookie("player_id")
        return resp

    return _INDEX_HTML.replace("{PLAYER_NAME}", row["name"])


@app.get("/chat_fragment", response_class=HTMLResponse)
async def chat_fragment():
    conn = get_connection()
    rows = conn.execute(
        "SELECT sender, message_text, is_action, action_type FROM chat_history ORDER BY id DESC"
    ).fetchall()
    conn.close()
    html = "".join(
        _render_message(r["sender"], r["message_text"], bool(r["is_action"]))
        for r in rows[-50:]
    )
    return html or '<div class="text-gray-500 text-sm italic">История пуста. Напишите что-нибудь!</div>'


@app.get("/players_panel", response_class=HTMLResponse)
async def players_panel():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    if not rows:
        return '<div class="text-gray-500 text-sm">Нет персонажей</div>'
    return "".join(_render_player_card(r) for r in rows)


@app.get("/choice", response_class=HTMLResponse)
async def choice():
    conn = get_connection()
    rows = conn.execute("SELECT id, name, class_archetype FROM players WHERE is_occupied = 0").fetchall()
    conn.close()

    cards = "".join(
        f'<form method="post" action="/login" class="mb-3">'
        f'<input type="hidden" name="player_id" value="{r["id"]}">'
        f'<button type="submit" class="w-full text-left bg-gray-800 border border-gray-600 '
        f'hover:border-emerald-500 rounded-lg p-4 transition cursor-pointer">'
        f'<div class="font-bold text-lg">{r["name"]}</div>'
        f'<div class="text-sm text-gray-400">{r["class_archetype"] or "—"}</div>'
        f'</button></form>'
        for r in rows
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://cdn.tailwindcss.com"></script>
  <title>Выбор персонажа — AI Tabletop Framework</title>
</head>
<body class="bg-gray-900 text-gray-100 h-screen flex items-center justify-center">
  <div class="w-full max-w-md p-6">
    <h1 class="text-2xl font-bold mb-6 text-center">Выберите персонажа</h1>
    {cards if cards else '<p class="text-gray-400 text-center">Нет доступных персонажей</p>'}
  </div>
</body>
</html>"""


@app.post("/login")
async def login(player_id: int = Form(...)):
    conn = get_connection()
    conn.execute("UPDATE players SET is_occupied = 1 WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(key="player_id", value=str(player_id))
    return resp


@app.get("/logout")
async def logout(request: Request):
    pid = request.cookies.get("player_id")
    if pid:
        conn = get_connection()
        conn.execute("UPDATE players SET is_occupied = 0 WHERE id = ?", (int(pid),))
        conn.commit()
        conn.close()
    resp = RedirectResponse(url="/choice")
    resp.delete_cookie("player_id")
    return resp


@app.get("/timer", response_class=HTMLResponse)
async def timer():
    sess = get_session()
    if not sess or not sess.timer_ends_at:
        return ""

    try:
        remaining = datetime.fromisoformat(sess.timer_ends_at) - datetime.utcnow()
    except Exception:
        return ""

    if remaining.total_seconds() <= 0:
        reset_timer()
        asyncio.ensure_future(_auto_respond())
        return ""

    secs = int(remaining.total_seconds())
    return f'<div id="timer-bar" class="text-center text-sm text-gray-400 py-1 bg-gray-800 border-t border-gray-700">Мастер внимательно слушает и ждет действий группы: осталось {secs} сек.</div>'


async def _auto_respond():
    conn = get_connection()
    row = conn.execute(
        "SELECT player_id, message_text FROM chat_history WHERE sender != 'GM' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return
    try:
        turn = await process_player_action(
            row["player_id"] or 0,
            row["message_text"],
            save_message=False,
        )
        html = _build_chat_response(turn, limit=2)
        await broadcast_message(html)
    except Exception as e:
        print(f"ERROR IN _auto_respond: {e}")


@app.post("/send_message", response_class=HTMLResponse)
async def send_message(request: Request, text: str = Form(...)):
    try:
        player_id = int(request.cookies.get("player_id", 0))
        conn = get_connection()
        row = conn.execute("SELECT name FROM players WHERE id=?", (player_id,)).fetchone()
        player_name = row["name"] if row else f"Player{player_id}"
        conn.close()
        msg = ChatMessageModel(sender=player_name, message_text=text, is_action=False, timestamp="")
        add_chat_message(msg, player_id=player_id)
        extend_timer()
    except Exception as e:
        print(f"ERROR IN ENDPOINT /send_message: {e}")
        return '<div class="text-red-400 text-sm">Ошибка отправки сообщения</div>'

    return _render_message(player_name, text, is_action=False)


@app.post("/declare_action", response_class=HTMLResponse)
async def declare_action(request: Request, text: str = Form(...)):
    try:
        player_id = int(request.cookies.get("player_id", 0))
        conn = get_connection()
        row = conn.execute("SELECT name FROM players WHERE id=?", (player_id,)).fetchone()
        player_name = row["name"] if row else f"Player{player_id}"
        conn.close()
        msg = ChatMessageModel(sender=player_name, message_text=text, is_action=True, timestamp="")
        add_chat_message(msg, player_id=player_id)
        extend_timer()
    except Exception as e:
        print(f"ERROR IN ENDPOINT /declare_action: {e}")
        return '<div class="text-red-400 text-sm">Ошибка обработки действия</div>'

    return _render_message(player_name, text, is_action=True)


def _build_chat_response(turn, limit=2):
    conn = get_connection()
    rows = conn.execute(
        "SELECT sender, message_text, is_action FROM chat_history ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    player_rows = conn.execute("SELECT * FROM players").fetchall()
    conn.close()

    parts = []
    for r in reversed(rows):
        parts.append(_render_message(r["sender"], r["message_text"], bool(r["is_action"])))
    parts.append(_render_message("GM", turn.narrative_text))

    panel = "".join(_render_player_card(r) for r in player_rows)

    return (
        "".join(parts)
        + f'<div id="players-panel" hx-swap-oob="true">{panel}</div>'
        + f'<div id="game-status" hx-swap-oob="true" class="text-sm px-3 py-1 rounded-full '
        + ('bg-red-700 text-red-200">combat' if turn.game_state_trigger == "combat_start"
           else ('bg-emerald-700 text-emerald-200">exploration' if turn.game_state_trigger == "combat_end"
                 else 'bg-emerald-700 text-emerald-200">exploration'))
        + "</div>"
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin():
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    sess = conn.execute("SELECT * FROM game_session WHERE session_id = 1").fetchone()
    conn.close()

    status = sess["game_status"] if sess else "exploration"
    next_status = "combat" if status == "exploration" else "exploration"

    rows_html = "".join(
        f"""<tr class="border-b border-gray-700">
          <td class="p-2">{r["id"]}</td>
          <td class="p-2 font-medium">{r["name"]}</td>
          <td class="p-2 text-xs text-gray-400">{r["class_archetype"] or "—"}</td>
          <td class="p-2">
            <form method="post" action="/admin/update_player" class="flex items-center gap-2">
              <input type="hidden" name="player_id" value="{r["id"]}">
              HP <input type="number" name="hp_current" value="{r["hp_current"]}"
                     class="w-16 bg-gray-700 border border-gray-600 rounded px-1 py-0.5 text-sm text-center">
              / <input type="number" name="hp_max" value="{r["hp_max"]}"
                     class="w-16 bg-gray-700 border border-gray-600 rounded px-1 py-0.5 text-sm text-center">
              <button type="submit" class="bg-emerald-600 hover:bg-emerald-500 text-white px-2 py-1 rounded text-xs">OK</button>
            </form>
          </td>
        </tr>"""
        for r in players
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://unpkg.com/htmx.org@2.0.4"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <title>Admin — AI Tabletop Framework</title>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen">
  <div class="max-w-4xl mx-auto p-6">
    <h1 class="text-2xl font-bold mb-6">Панель управления</h1>

    <section class="mb-8">
      <h2 class="text-lg font-semibold mb-3">Статус игры</h2>
      <div class="flex items-center gap-4 bg-gray-800 border border-gray-700 rounded-lg p-4">
        <span class="text-sm">Текущий статус:</span>
        <span id="admin-game-status" class="text-sm px-3 py-1 rounded-full
          {'bg-red-700 text-red-200' if status == 'combat' else 'bg-emerald-700 text-emerald-200'}">{status}</span>
        <form method="post" action="/admin/toggle_status">
          <button type="submit"
                  class="bg-amber-600 hover:bg-amber-500 text-white px-4 py-2 rounded-lg text-sm transition">
            Переключить в {next_status}
          </button>
        </form>
      </div>
    </section>

    <section>
      <h2 class="text-lg font-semibold mb-3">Персонажи</h2>
      <table class="w-full bg-gray-800 border border-gray-700 rounded-lg text-sm">
        <thead>
          <tr class="border-b border-gray-700 text-gray-400 text-left">
            <th class="p-2">ID</th>
            <th class="p-2">Имя</th>
            <th class="p-2">Класс</th>
            <th class="p-2">HP</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </section>

    <a href="/" class="inline-block mt-6 text-sm text-gray-400 hover:text-gray-200 underline">← Назад в игру</a>
  </div>
</body>
</html>"""


@app.post("/admin/update_player", response_class=HTMLResponse)
async def admin_update_player(
    player_id: int = Form(...),
    hp_current: int = Form(...),
    hp_max: int = Form(...),
):
    conn = get_connection()
    conn.execute(
        "UPDATE players SET hp_current = ?, hp_max = ? WHERE id = ?",
        (hp_current, hp_max, player_id),
    )
    conn.commit()
    conn.close()

    await _broadcast_panel_and_status()
    return ""


@app.post("/admin/toggle_status", response_class=HTMLResponse)
async def admin_toggle_status():
    conn = get_connection()
    row = conn.execute("SELECT game_status FROM game_session WHERE session_id = 1").fetchone()
    old = row["game_status"] if row else "exploration"
    new = "combat" if old == "exploration" else "exploration"
    conn.execute("UPDATE game_session SET game_status = ? WHERE session_id = 1", (new,))
    conn.commit()
    conn.close()

    await _broadcast_panel_and_status()
    return ""


async def _broadcast_panel_and_status():
    conn = get_connection()
    player_rows = conn.execute("SELECT * FROM players").fetchall()
    sess = conn.execute("SELECT game_status FROM game_session WHERE session_id = 1").fetchone()
    conn.close()

    panel = "".join(_render_player_card(r) for r in player_rows)
    status = sess["game_status"] if sess else "exploration"
    status_class = "bg-red-700 text-red-200" if status == "combat" else "bg-emerald-700 text-emerald-200"

    html = (
        f'<div id="players-panel" hx-swap-oob="true">{panel}</div>'
        f'<div id="game-status" hx-swap-oob="true" class="text-sm px-3 py-1 rounded-full {status_class}">{status}</div>'
    )
    try:
        await broadcast_message(html)
    except Exception as e:
        print(f"ERROR broadcasting admin update: {e}")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
