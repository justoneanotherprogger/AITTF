import json
import html
import asyncio
from dotenv import load_dotenv
from pathlib import Path
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
import uvicorn

load_dotenv()

from datetime import datetime, timedelta

from db.database import (
    init_db, get_connection, add_chat_message, get_session,
    extend_timer, reset_timer, clear_game_data, add_or_update_entity,
)
from models.models import PlayerModel, WorldEntityModel
from llm.ai_generator import generate_initial_world
from models.models import ChatMessageModel
from llm.turn_processor import process_player_action
from llm.context_builder import build_player_descriptions

app = FastAPI()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    def connect(self, websocket: WebSocket) -> None:
        self._connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.remove(websocket)

    async def broadcast_html(self, html_content: str) -> None:
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(html_content)
            except WebSocketDisconnect:
                dead.append(ws)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.remove(ws)


manager = ConnectionManager()


_BTN_CLS = 'px-4 py-2 rounded-lg text-sm font-medium transition'
_ENABLED_CLS = 'bg-gray-600 hover:bg-gray-500 text-white'
_ACTION_CLS = 'bg-amber-600 hover:bg-amber-500 text-white'
_DISABLED_CLS = 'bg-gray-700 text-gray-500 cursor-not-allowed'

def _build_input_area_html(locked: bool = False, oob: bool = False) -> str:
    if oob:
        if locked:
            ctrl = (
                f'<div id="chat-controls" hx-swap-oob="true">'
                f'<button type="button" disabled class="{_DISABLED_CLS} {_BTN_CLS}">Сказать</button>'
                f'<button type="button" disabled class="{_DISABLED_CLS} {_BTN_CLS}">Заявить действие</button>'
                f'</div>'
                f'<div id="spinner" hx-swap-oob="true" class="text-amber-400 text-sm text-center animate-pulse">✦ Мастер думает...</div>'
                f'<div id="__lock-input" hx-swap-oob="true" style="display:none"></div>'
            )
        else:
            ctrl = (
                f'<div id="chat-controls" hx-swap-oob="true">'
                f'<button type="button" hx-post="/send_message" hx-include="#message-input" '
                f'hx-target="#chat-messages" hx-swap="beforeend" hx-indicator="#spinner" '
                f'class="{_ENABLED_CLS} {_BTN_CLS}">Сказать</button>'
                f'<button id="action-btn" type="button" hx-post="/declare_action" hx-include="#message-input" '
                f'hx-target="#chat-messages" hx-swap="beforeend" hx-indicator="#spinner" '
                f'hx-on::before-request="this.disabled=true;this.textContent=\'Думает...\'" '
                f'hx-on::after-request="this.disabled=false;this.textContent=\'Заявить действие\'" '
                f'class="{_ACTION_CLS} {_BTN_CLS}">Заявить действие</button>'
                f'</div>'
                f'<div id="spinner" hx-swap-oob="true" class="htmx-indicator text-amber-400 text-sm text-center">✦ Мастер думает...</div>'
                f'<div id="__unlock-input" hx-swap-oob="true" style="display:none"></div>'
            )
        return ctrl

    return (
        '<div id="input-area">'
        '<div class="border-t border-gray-700 bg-gray-800 p-4">'
        '<div class="flex gap-2 mb-2">'
        '<input id="message-input" type="text" name="text" placeholder="Сказать что-то или описать действие..." required autocomplete="off" '
        'class="flex-1 bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500">'
        '<div id="chat-controls">'
        '<button type="button" hx-post="/send_message" hx-include="#message-input" '
        'hx-target="#chat-messages" hx-swap="beforeend" hx-indicator="#spinner" '
        f'class="{_ENABLED_CLS} {_BTN_CLS}">Сказать</button>'
        '<button id="action-btn" type="button" hx-post="/declare_action" hx-include="#message-input" '
        'hx-target="#chat-messages" hx-swap="beforeend" hx-indicator="#spinner" '
        'hx-on::before-request="this.disabled=true;this.textContent=\'Думает...\'" '
        'hx-on::after-request="this.disabled=false;this.textContent=\'Заявить действие\'" '
        f'class="{_ACTION_CLS} {_BTN_CLS}">Заявить действие</button>'
        '</div>'
        '</div>'
        '<div id="spinner" class="htmx-indicator text-amber-400 text-sm text-center">'
        '✦ Мастер думает...'
        '</div>'
        '</div>'
        '</div>'
    )

_INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://unpkg.com/htmx.org@2.0.4"></script>
  <script src="https://unpkg.com/htmx.org@2.0.4/dist/ext/ws.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
    <style>
      .htmx-indicator { opacity: 0; transition: opacity .2s; }
      .htmx-request .htmx-indicator { opacity: 1; }
      .htmx-request.htmx-indicator { opacity: 1; }
      #chat-messages { display: flex; flex-direction: column; }
      #chat-messages > [data-author-id]:not([data-author-id="system"]) {
        align-self: flex-end;
      }
      #chat-messages > [data-author-id] {
        max-width: 80%;
      }
    </style>
  <title>AI Tabletop Framework</title>
</head>
<body class="bg-gray-900 text-gray-100 h-screen flex flex-col"
      hx-ext="ws" ws-connect="/ws/chat">
  <header class="bg-gray-800 border-b border-gray-700 px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-4">
      <h1 class="text-xl font-bold tracking-wide">AI Tabletop Framework</h1>
      <span id="current-player" class="text-sm text-emerald-300">{PLAYER_NAME}</span>
    </div>
    <a href="/logout" class="text-xs text-gray-400 hover:text-gray-200 underline">Сменить персонажа</a>
    <a href="/admin" class="text-xs text-gray-500 hover:text-gray-300 underline ml-3">Admin</a>
    <button type="button" hx-post="/api/game/reset" hx-disabled-elt="this"
            hx-confirm="Вы уверены, что хотите удалить текущую игру и начать заново?"
            class="text-xs text-red-400 hover:text-red-300 underline ml-3">Сбросить партию</button>
    <span id="game-status" class="text-sm px-3 py-1 rounded-full bg-emerald-700 text-emerald-200">exploration</span>
  </header>

  <div class="flex flex-1 overflow-hidden">
    <!-- Sidebar -->
    <aside id="sidebar" class="w-72 bg-gray-800 border-r border-gray-700 p-4 overflow-y-auto flex-shrink-0">
      <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Персонажи</h2>
      <div id="players-panel">{PLAYERS_PANEL}</div>
    </aside>

    <!-- Main -->
    <main class="flex-1 flex flex-col">
      <div id="chat-messages" class="flex-1 overflow-y-auto p-4 space-y-3 relative"
           hx-get="/chat_fragment" hx-trigger="load" hx-swap="innerHTML">
        <div class="text-gray-500 text-sm">Загрузка истории...</div>
      </div>
      <button id="scroll-bottom-btn" onclick="document.getElementById('chat-messages').scrollTop = document.getElementById('chat-messages').scrollHeight"
              class="hidden fixed bottom-24 right-6 z-10 bg-emerald-600 hover:bg-emerald-500 text-white w-10 h-10 rounded-full shadow-lg items-center justify-center transition">
        ↓
      </button>

      <div id="timer-bar" class="bg-gray-800 border-t border-gray-700 px-4 py-3 text-center text-sm text-amber-300 font-medium hidden"></div>

      <div id="__timer-reset" style="display:none"></div>
      <div id="__timer-stop" style="display:none"></div>
      <div id="__lock-input" style="display:none"></div>
      <div id="__unlock-input" style="display:none"></div>

      <div id="input-area">{INPUT_AREA}</div>
    </main>
  </div>
  <script>
    (function() {
      var CURRENT_PLAYER_ID = '{CURRENT_PLAYER_ID}';
      var chat = document.getElementById('chat-messages');
      var scrollBtn = document.getElementById('scroll-bottom-btn');
      var userHasScrolledUp = false;

      function scrollToBottom() {
        chat.scrollTop = chat.scrollHeight;
        userHasScrolledUp = false;
        clearPulse();
      }

      function isNearBottom() {
        return chat.scrollTop + chat.clientHeight >= chat.scrollHeight - 150;
      }

      function clearPulse() {
        scrollBtn.classList.remove('bg-red-500', 'animate-pulse');
        scrollBtn.classList.add('bg-emerald-600');
      }

      function setPulse() {
        if (!scrollBtn.classList.contains('animate-pulse')) {
          scrollBtn.classList.remove('bg-emerald-600', 'hidden');
          scrollBtn.classList.add('flex', 'bg-red-500', 'animate-pulse');
        }
      }

      chat.addEventListener('scroll', function() {
        if (isNearBottom()) {
          userHasScrolledUp = false;
          scrollBtn.classList.add('hidden');
          scrollBtn.classList.remove('flex');
          clearPulse();
        } else if (!userHasScrolledUp) {
          userHasScrolledUp = true;
          scrollBtn.classList.remove('hidden');
          scrollBtn.classList.add('flex');
          clearPulse();
        }
      });

      scrollBtn.addEventListener('click', scrollToBottom);

      // new content arrived — force scroll unless user scrolled up
      function onNewContent() {
        setTimeout(function() {
          if (userHasScrolledUp) {
            setPulse();
          } else {
            scrollToBottom();
          }
        }, 50);
      }

      // MutationObserver: scroll to bottom on new content
      var observer = new MutationObserver(function(mutations) {
        onNewContent();
      });
      if (chat) {
        observer.observe(chat, {childList: true});
      }

      var timerBar = document.getElementById('timer-bar');
      var timerRemaining = 0;
      var timerInterval = null;

      function startTimer(secs) {
        timerRemaining = secs;
        updateTimerDisplay();
        if (timerInterval) clearInterval(timerInterval);
        timerInterval = setInterval(function() {
          timerRemaining--;
          if (timerRemaining <= 0) {
            clearInterval(timerInterval);
            timerInterval = null;
            timerBar.classList.add('hidden');
            timerBar.innerHTML = '';
            fetch('/skip_turn', {method: 'POST'});
            return;
          }
          updateTimerDisplay();
        }, 1000);
      }

      function stopTimer() {
        if (timerInterval) clearInterval(timerInterval);
        timerInterval = null;
        timerBar.classList.add('hidden');
      }

      function updateTimerDisplay() {
        timerBar.classList.remove('hidden');
        timerBar.innerHTML = '⏳ Мастер внимательно слушает и ждет действий группы: осталось <span class="text-amber-200 font-bold">' + timerRemaining + '</span> сек.';
      }

      function setInputLock(locked) {
        var inp = document.getElementById('message-input');
        if (!inp) return;
        inp.disabled = locked;
        inp.classList.toggle('opacity-50', locked);
      }

      document.body.addEventListener('htmx:oobBeforeSwap', function(evt) {
        if (evt.detail.shouldSwap && evt.detail.elt.id === '__lock-input') {
          setInputLock(true);
          evt.preventDefault();
        }
        if (evt.detail.shouldSwap && evt.detail.elt.id === '__unlock-input') {
          setInputLock(false);
          evt.preventDefault();
        }
        if (evt.detail.shouldSwap && evt.detail.elt.id === '__timer-reset') {
          startTimer(15);
          evt.preventDefault();
        }
        if (evt.detail.shouldSwap && evt.detail.elt.id === '__timer-stop') {
          stopTimer();
          evt.preventDefault();
        }
      });
    })();
    document.body.addEventListener('htmx:afterRequest', function(evt) {
      if (evt.detail.pathInfo.requestPath === '/send_message' ||
          evt.detail.pathInfo.requestPath === '/declare_action') {
        startTimer(15);
      }
      var input = document.getElementById('message-input');
      if (input && (evt.detail.pathInfo.requestPath === '/send_message' ||
                    evt.detail.pathInfo.requestPath === '/declare_action')) {
        input.value = '';
        // always scroll to bottom when user sends a message
        var chat = document.getElementById('chat-messages');
        chat.scrollTop = chat.scrollHeight;
        var btn = document.getElementById('scroll-bottom-btn');
        btn.classList.remove('bg-red-500', 'animate-pulse', 'flex');
        btn.classList.add('bg-emerald-600', 'hidden');
      }
    });
  </script>
</body>
</html>"""


def _render_message(sender: str, text: str, is_action: bool = False, oob_target: str = "", sender_id: str = "") -> str:
    is_gm = sender == "GM"
    bg = "bg-amber-700/40 border-amber-600/30" if is_action else ("bg-emerald-700/30 border-emerald-600/20" if is_gm else "bg-gray-700/50 border-gray-600/30")
    label = "GM" if is_gm else (f"{sender} совершает действие" if is_action else sender)
    author_attr = f' data-author-id="{sender_id if sender_id else "system"}"'
    inner = (
        f'<div class="inline-block max-w-[80%] {bg} border rounded-xl px-4 py-2 text-sm"{author_attr}>'
        f'<span class="text-xs font-semibold text-gray-400 block mb-0.5">{label}</span>'
        f'{text}'
        f'</div>'
    )
    if oob_target:
        return f'<div hx-swap-oob="{oob_target}">{inner}</div>'
    return inner


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
    manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    sess = conn.execute("SELECT game_status FROM game_session WHERE session_id = 1").fetchone()
    conn.close()
    game_active = sess and sess["game_status"] == "active"

    if sess and sess["game_status"] == "backstory_gathering":
        return RedirectResponse(url="/backstories")

    pid = request.cookies.get("player_id")
    current_player_id = int(pid) if pid else None

    if current_player_id:
        my_player = next((dict(p) for p in players if p["id"] == current_player_id), None)
        if my_player is None:
            resp = templates.TemplateResponse(request, "lobby.html", {
                "players": players, "max_slots": 4, "game_active": game_active,
                "current_player_id": None
            })
            resp.delete_cookie("player_id")
            return resp
    else:
        my_player = None

    if sess and sess["game_status"] == "exploration" and current_player_id and my_player:
        panel_html = await _render_players_panel_str()
        return _INDEX_HTML.replace("{PLAYER_NAME}", my_player["name"]).replace("{CURRENT_PLAYER_ID}", str(current_player_id)).replace("{INPUT_AREA}", _build_input_area_html(locked=False)).replace("{PLAYERS_PANEL}", panel_html)

    return templates.TemplateResponse(request, "lobby.html", {
        "players": players, "max_slots": 4, "game_active": game_active,
        "current_player_id": current_player_id
    })


@app.get("/chat_fragment", response_class=HTMLResponse)
async def chat_fragment():
    conn = get_connection()
    rows = conn.execute(
        "SELECT sender, player_id, message_text, is_action, action_type FROM chat_history ORDER BY id ASC LIMIT 100"
    ).fetchall()
    conn.close()
    html = "".join(
        _render_message(r["sender"], r["message_text"], bool(r["is_action"]), sender_id=str(r["player_id"] or ""))
        for r in rows
    )
    return html or '<div class="text-gray-500 text-sm italic">История пуста. Напишите что-нибудь!</div>'


async def _render_players_panel_str() -> str:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    if not rows:
        return '<div class="text-gray-500 text-sm">Нет персонажей</div>'
    return "".join(_render_player_card(r) for r in rows)


@app.get("/players_panel", response_class=HTMLResponse)
async def players_panel():
    return await _render_players_panel_str()


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
  <script src="https://unpkg.com/htmx.org@2.0.4"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <title>Выбор персонажа — AI Tabletop Framework</title>
</head>
<body class="bg-gray-900 text-gray-100 h-screen flex items-center justify-center">
  <div class="w-full max-w-md p-6">
    <h1 class="text-2xl font-bold mb-6 text-center">Выберите персонажа</h1>
    {cards if cards else '<p class="text-gray-400 text-center">Нет доступных персонажей</p>'}
    <div class="mt-6 text-center">
      <button type="button" hx-post="/api/game/reset" hx-disabled-elt="this"
              hx-confirm="Вы уверены, что хотите удалить текущую игру и начать заново?"
              class="text-xs text-red-400 hover:text-red-300 underline">Сбросить партию</button>
    </div>
  </div>
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
    resp = RedirectResponse(url="/")
    resp.delete_cookie("player_id")
    return resp


@app.get("/backstories", response_class=HTMLResponse)
async def backstories_page(request: Request):
    sess = get_session()
    status_from_db = sess.game_status if sess else "None"
    print(f"[/backstories] Статус из БД: {status_from_db}")
    if not sess or sess.game_status != "backstory_gathering":
        print(f"[/backstories] Статус '{status_from_db}' != 'backstory_gathering' — редирект на /")
        return RedirectResponse(url="/")
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    print(f"[/backstories] Игроков найдено: {len(players)}")

    pid = request.cookies.get("player_id")
    current_player = next((dict(p) for p in players if p["id"] == int(pid)), None) if pid else None

    return templates.TemplateResponse(request, "backstories.html", {
        "players": players, "current_player": current_player
    })


async def _broadcast_lobby_refresh():
    await manager.broadcast_html('<div id="__refresh-lobby-slots" hx-swap-oob="true" style="display:none"></div>')


def _render_slots(current_player_id: int | None = None):
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    sess = conn.execute("SELECT game_status FROM game_session WHERE session_id = 1").fetchone()
    conn.close()
    game_active = sess and sess["game_status"] == "active"
    tmpl = templates.env.get_template("slots.html")
    return tmpl.render(players=players, max_slots=4, game_active=game_active, current_player_id=current_player_id)


@app.get("/lobby/slots", response_class=HTMLResponse)
async def lobby_slots(request: Request):
    pid = request.cookies.get("player_id")
    return _render_slots(current_player_id=int(pid) if pid else None)


@app.post("/lobby/add_player", response_class=HTMLResponse)
async def lobby_add_player(request: Request, name: str = Form(...)):
    pid = request.cookies.get("player_id")
    if pid:
        conn = get_connection()
        existing = conn.execute("SELECT id FROM players WHERE id = ?", (int(pid),)).fetchone()
        conn.close()
        if existing:
            return Response(status_code=400, content="Вы уже создали персонажа")
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO players (name) VALUES (?)", (name,))
    conn.commit()
    player_id = cur.lastrowid
    conn.close()
    slots_html = _render_slots(current_player_id=player_id)
    form_oob = '<div id="lobby-form-area" hx-swap-oob="true"><p class="text-gray-400 text-sm text-center mb-6">Вы уже создали персонажа. Ожидайте старта игры</p></div>'
    resp = HTMLResponse(content=slots_html + form_oob)
    resp.set_cookie(key="player_id", value=str(player_id), httponly=False)
    asyncio.ensure_future(_broadcast_lobby_refresh())
    return resp


@app.post("/lobby/remove_player", response_class=HTMLResponse)
async def lobby_remove_player(request: Request, player_id: int = Form(...)):
    conn = get_connection()
    conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    pid = request.cookies.get("player_id")
    current = int(pid) if pid else None
    slots_html = _render_slots(current_player_id=current)
    if current and current == player_id:
        form_oob = (
            '<div id="lobby-form-area" hx-swap-oob="true">'
            '<form hx-post="/lobby/add_player" hx-target="#lobby-slots" hx-swap="innerHTML"'
            ' class="flex gap-3 mb-8">'
            '<input type="text" name="name" placeholder="Имя персонажа" required'
            ' class="flex-1 bg-gray-800 border border-gray-600 rounded-lg px-4 py-2 text-sm'
            ' focus:outline-none focus:ring-2 focus:ring-emerald-500">'
            '<button type="submit"'
            ' class="bg-emerald-600 hover:bg-emerald-500 text-white px-6 py-2 rounded-lg'
            ' text-sm font-medium transition">Добавить</button>'
            '</form></div>'
        )
        resp = HTMLResponse(content=slots_html + form_oob)
        resp.delete_cookie("player_id")
    else:
        resp = HTMLResponse(content=slots_html)
    asyncio.ensure_future(_broadcast_lobby_refresh())
    return resp


@app.post("/player/{player_id}/backstory", response_class=HTMLResponse)
async def player_backstory(player_id: int, backstory: str = Form(default="")):
    conn = get_connection()
    conn.execute("UPDATE players SET backstory = ? WHERE id = ?", (backstory, player_id))
    conn.commit()
    row = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    conn.close()

    name = row["name"]
    esc_name = html.escape(name)
    esc_backstory = html.escape(row["backstory"] or "")

    card = (
        f'<div class="bg-gray-800 rounded-lg border border-gray-700 p-4" id="player-card-{player_id}">'
        f'  <div class="font-medium text-lg mb-2">{esc_name}</div>'
        f'  <div class="flex gap-2 items-start">'
        f'    <textarea id="backstory-{player_id}" name="backstory" rows="4" disabled'
        f'      class="flex-1 bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-sm resize-y placeholder-gray-500 opacity-60 cursor-not-allowed">{esc_backstory}</textarea>'
        f'    <button type="button" disabled'
        f'      class="px-3 py-2 rounded-lg text-sm font-medium transition whitespace-nowrap bg-gray-700 text-gray-500 cursor-not-allowed">'
        f'      Закрепить'
        f'    </button>'
        f'  </div>'
        f'</div>'
    )
    resp = HTMLResponse(content=card)
    resp.headers["HX-Trigger"] = "backstory-updated"
    return resp


@app.get("/lobby/backstory_status", response_class=HTMLResponse)
async def backstory_status():
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    for p in players:
        val = p["backstory"] if "backstory" in p.keys() else "N/A"
        print(f"[/backstory_status]  player id={p['id']} name={p['name']}  backstory='{val}' (длина={len(val)})")

    all_filled = all(p["backstory"] for p in players) if players else False

    btn_cls = "w-full bg-amber-600 hover:bg-amber-500 text-white px-8 py-3 rounded-lg text-lg font-medium transition"
    if not all_filled:
        btn_cls += " opacity-50 cursor-not-allowed"
    disabled = " disabled" if not all_filled else ""

    parts = [
        '<button id="gen-btn" type="button"'
        f' hx-post="/generate_world"'
        f' hx-indicator="#gen-spinner"'
        f' hx-disabled-elt="this"'
        f' onclick="document.getElementById(\'generate-status\').classList.add(\'loading\')"'
        f' class="{btn_cls}"{disabled}>'
        f'Сгенерировать мир'
        f'</button>'
        f'<span id="gen-spinner" class="htmx-indicator text-amber-400 text-sm text-center block mt-2">✦ Мастер создаёт мир...</span>'
    ]
    if not all_filled and players:
        remaining = sum(1 for p in players if not p["backstory"])
        parts.append(f'<p class="text-gray-400 text-sm mt-2 text-center">Осталось заполнить предысторий: {remaining}</p>')
    return "".join(parts)


@app.post("/generate_world", response_class=HTMLResponse)
async def generate_world():
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    conn.close()

    if not players:
        return Response(status_code=400, content="Нет игроков")

    # all backstories must be filled
    all_filled = all(p["backstory"] for p in players)
    if not all_filled:
        return Response(status_code=400, content="Не все предыстории заполнены")

    descriptions = build_player_descriptions()

    phase_zero = await generate_initial_world(descriptions)

    # save world entities
    add_or_update_entity(WorldEntityModel(
        entity_type="setting", name=phase_zero.setting_name,
        data={"description": phase_zero.setting_description},
    ))
    add_or_update_entity(WorldEntityModel(
        entity_type="rule", name="global_conflict",
        data={"description": phase_zero.global_conflict},
    ))
    add_or_update_entity(WorldEntityModel(
        entity_type="rule", name="stats_system",
        data={"stats": phase_zero.character_stats_templates},
    ))
    add_or_update_entity(WorldEntityModel(
        entity_type="rule", name="initial_narrative",
        data={"text": phase_zero.initial_narrative_text},
    ))

    # update existing players with generated stats and archetypes
    conn = get_connection()
    for player in players:
        archetype = phase_zero.character_classes.get(player["name"], "")
        conn.execute(
            "UPDATE players SET hp_current = 10, hp_max = 10, stats = ?, class_archetype = ? WHERE name = ?",
            (json.dumps({s: _DEFAULT_STAT_VALUE for s in phase_zero.character_stats_templates}, ensure_ascii=False),
             archetype, player["name"]),
        )
    conn.commit()

    # update game session
    conn.execute(
        "UPDATE game_session SET game_status = 'exploration', setting_blob = ?, global_lore = ? WHERE session_id = 1",
        (json.dumps({"name": phase_zero.setting_name, "description": phase_zero.setting_description}, ensure_ascii=False),
         phase_zero.global_conflict),
    )
    conn.commit()
    conn.close()

    # save setting, conflict and initial narrative as separate chat messages
    setting_text = f"<strong>📖 СЕТТИНГ: {phase_zero.setting_name}</strong><br><br>{phase_zero.setting_description}"
    add_chat_message(ChatMessageModel(sender="GM", message_text=setting_text, is_action=False, timestamp=""))
    add_chat_message(ChatMessageModel(sender="GM", message_text=f"<strong>⚔️ СУТЬ КОНФЛИКТА</strong><br><br>{phase_zero.global_conflict}", is_action=False, timestamp=""))
    add_chat_message(ChatMessageModel(sender="GM", message_text=phase_zero.initial_narrative_text, is_action=False, timestamp=""))

    return Response(headers={"HX-Redirect": "/"})


_DEFAULT_STAT_VALUE = 1


@app.post("/lobby/start", response_class=HTMLResponse)
async def lobby_start():
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    conn.close()

    if not players:
        print("[/lobby/start] Нет игроков — возвращаю 400")
        return Response(status_code=400, content="Нет игроков")

    print(f"[/lobby/start] Ставлю game_status='backstory_gathering', игроков: {len(players)}")
    conn = get_connection()
    conn.execute("UPDATE game_session SET game_status = 'backstory_gathering' WHERE session_id = 1")
    conn.commit()
    conn.close()

    print("[/lobby/start] Возвращаю HX-Redirect: /backstories")
    return Response(headers={"HX-Redirect": "/backstories"})


async def _auto_respond():
    conn = get_connection()
    row = conn.execute(
        "SELECT player_id, message_text FROM chat_history WHERE sender != 'GM' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        unlocked = _build_input_area_html(locked=False, oob=True)
        await manager.broadcast_html(unlocked)
        return
    locked = _build_input_area_html(locked=True, oob=True)
    await manager.broadcast_html(locked)
    try:
        turn = await process_player_action(
            row["player_id"] or 0,
            row["message_text"],
            save_message=False,
        )
    except Exception as e:
        print(f"ERROR IN _auto_respond: {e}")
        unlocked = _build_input_area_html(locked=False, oob=True)
        await manager.broadcast_html(unlocked)
        return

    gm_html = _render_message("GM", turn.narrative_text, oob_target="beforeend:#chat-messages", sender_id="")
    conn2 = get_connection()
    player_rows = conn2.execute("SELECT * FROM players").fetchall()
    sess2 = conn2.execute("SELECT game_status FROM game_session WHERE session_id = 1").fetchone()
    conn2.close()
    panel = "".join(_render_player_card(r) for r in player_rows)
    status = sess2["game_status"] if sess2 else "exploration"
    status_cls = "bg-red-700 text-red-200" if status == "combat" else "bg-emerald-700 text-emerald-200"
    html = (
        gm_html
        + f'<div id="players-panel" hx-swap-oob="true">{panel}</div>'
        + f'<div id="game-status" hx-swap-oob="true" class="text-sm px-3 py-1 rounded-full {status_cls}">{status}</div>'
        + _build_input_area_html(locked=False, oob=True)
        + '<div id="__timer-stop" hx-swap-oob="true" style="display:none"></div>'
    )
    await manager.broadcast_html(html)


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
        return Response(status_code=500)

    rendered = _render_message(player_name, text, is_action=False, oob_target="beforeend:#chat-messages", sender_id=str(player_id))
    timer_reset = '<div id="__timer-reset" hx-swap-oob="true" style="display:none"></div>'
    await manager.broadcast_html(rendered)
    await manager.broadcast_html(timer_reset)
    return timer_reset + '<input id="message-input" type="text" name="text" value="" class="flex-1 bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500" hx-swap-oob="true">'


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
        return Response(status_code=500)

    rendered = _render_message(player_name, text, is_action=True, oob_target="beforeend:#chat-messages", sender_id=str(player_id))
    timer_reset = '<div id="__timer-reset" hx-swap-oob="true" style="display:none"></div>'
    await manager.broadcast_html(rendered)
    await manager.broadcast_html(timer_reset)
    return timer_reset + '<input id="message-input" type="text" name="text" value="" class="flex-1 bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500" hx-swap-oob="true">'


@app.post("/skip_turn", response_class=HTMLResponse)
async def skip_turn():
    sess = get_session()
    if not sess or not sess.timer_ends_at:
        return ""
    try:
        remaining = datetime.fromisoformat(sess.timer_ends_at) - datetime.utcnow()
    except Exception:
        remaining = timedelta(seconds=0)
    if remaining.total_seconds() > 1:
        return ""
    reset_timer()
    asyncio.ensure_future(_auto_respond())
    return ""


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
            <form hx-post="/admin/update_player" hx-swap="none" class="flex items-center gap-2">
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
      <div id="admin-status-block" class="flex items-center gap-4 bg-gray-800 border border-gray-700 rounded-lg p-4">
        <span class="text-sm">Текущий статус:</span>
        <span class="text-sm px-3 py-1 rounded-full
          {'bg-red-700 text-red-200' if status == 'combat' else 'bg-emerald-700 text-emerald-200'}">{status}</span>
        <form hx-post="/admin/toggle_status" hx-target="#admin-status-block" hx-swap="outerHTML">
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

    next_status = "exploration" if new == "combat" else "combat"
    status_class = "bg-red-700 text-red-200" if new == "combat" else "bg-emerald-700 text-emerald-200"
    return f"""<div id="admin-status-block" class="flex items-center gap-4 bg-gray-800 border border-gray-700 rounded-lg p-4">
        <span class="text-sm">Текущий статус:</span>
        <span class="text-sm px-3 py-1 rounded-full {status_class}">{new}</span>
        <form hx-post="/admin/toggle_status" hx-target="#admin-status-block" hx-swap="outerHTML">
          <button type="submit"
                  class="bg-amber-600 hover:bg-amber-500 text-white px-4 py-2 rounded-lg text-sm transition">
            Переключить в {next_status}
          </button>
        </form>
      </div>"""


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
        await manager.broadcast_html(html)
    except Exception as e:
        print(f"ERROR broadcasting admin update: {e}")


@app.post("/api/game/reset", response_class=HTMLResponse)
async def reset_game():
    clear_game_data()
    resp = Response(status_code=200, headers={"HX-Redirect": "/"})
    resp.delete_cookie("player_id")
    return resp


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
