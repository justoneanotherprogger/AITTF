import json
import asyncio
from dotenv import load_dotenv
from pathlib import Path
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
import uvicorn

load_dotenv()

from datetime import datetime

from db.database import (
    init_db, get_connection, add_chat_message, get_session,
    extend_timer, reset_timer, clear_game_data, add_or_update_entity,
)
from models.models import PlayerModel, WorldEntityModel
from llm.ai_generator import generate_initial_world
from models.models import ChatMessageModel
from llm.turn_processor import process_player_action

app = FastAPI()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

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
    <button type="button" hx-post="/api/game/reset" hx-disabled-elt="this"
            onclick="if(!confirm('Вы уверены, что хотите удалить текущую игру и начать заново?')) return false;"
            class="text-xs text-red-400 hover:text-red-300 underline ml-3">Сбросить партию</button>
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
      <div id="chat-messages" class="flex-1 overflow-y-auto p-4 space-y-3 relative"
           hx-get="/chat_fragment" hx-trigger="load, every 3s" hx-swap="innerHTML">
        <div class="text-gray-500 text-sm">Загрузка истории...</div>
      </div>
      <button id="scroll-bottom-btn" onclick="document.getElementById('chat-messages').scrollTop = document.getElementById('chat-messages').scrollHeight"
              class="hidden fixed bottom-24 right-6 z-10 bg-emerald-600 hover:bg-emerald-500 text-white w-10 h-10 rounded-full shadow-lg items-center justify-center transition">
        ↓
      </button>

      <div id="timer-poll" hx-get="/timer" hx-trigger="every 1s" hx-swap="none scroll:none" class="hidden"></div>
      <div id="timer-bar"></div>

      <div id="input-area">{INPUT_AREA}</div>
    </main>
  </div>
  <script>
    (function() {
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
          setPulse();
        }
      });

      scrollBtn.addEventListener('click', scrollToBottom);

      // scroll to bottom on any HTMX settle into chat-messages
      document.body.addEventListener('htmx:afterSettle', function(evt) {
        if (evt.detail.target && evt.detail.target.id === 'chat-messages') {
          onNewContent();
        }
      });

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

      // WebSocket for live broadcast
      var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      var ws = new WebSocket(proto + '//' + location.host + '/ws/chat');
      function setInputLock(locked) {
        var inp = document.getElementById('message-input');
        if (!inp) return;
        inp.disabled = locked;
        inp.classList.toggle('opacity-50', locked);
      }

      ws.onmessage = function(evt) {
        var wrapper = document.createElement('div');
        wrapper.innerHTML = evt.data;
        wrapper.querySelectorAll('[hx-swap-oob]').forEach(function(el) {
          if (el.id === '__lock-input') { setInputLock(true); return; }
          if (el.id === '__unlock-input') { setInputLock(false); return; }
          var target = document.getElementById(el.id);
          if (!target) return;
          var swap = el.getAttribute('hx-swap-oob');
          if (swap === 'afterbegin') {
            target.insertAdjacentHTML('afterbegin', el.innerHTML);
          } else if (swap === 'beforeend') {
            target.insertAdjacentHTML('beforeend', el.innerHTML);
          } else {
            target.outerHTML = el.outerHTML;
          }
        });
        htmx.process(document.body);
        onNewContent();
      };
    })();
    // Clear input after any HTMX request from the two buttons
    document.body.addEventListener('htmx:afterRequest', function(evt) {
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
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    sess = conn.execute("SELECT game_status FROM game_session WHERE session_id = 1").fetchone()
    conn.close()
    game_active = sess and sess["game_status"] == "active"

    if not players:
        return templates.TemplateResponse(request, "lobby.html", {"players": [], "max_slots": 4, "game_active": False})

    pid = request.cookies.get("player_id")
    if not pid:
        return templates.TemplateResponse(request, "lobby.html", {"players": players, "max_slots": 4, "game_active": game_active})
    try:
        player_id = int(pid)
    except (ValueError, TypeError):
        return templates.TemplateResponse(request, "lobby.html", {"players": players, "max_slots": 4, "game_active": game_active})

    conn = get_connection()
    row = conn.execute("SELECT name FROM players WHERE id=?", (player_id,)).fetchone()
    conn.close()

    if not row:
        resp = templates.TemplateResponse(request, "lobby.html", {"players": players, "max_slots": 4, "game_active": game_active})
        resp.delete_cookie("player_id")
        return resp

    return _INDEX_HTML.replace("{PLAYER_NAME}", row["name"]).replace("{INPUT_AREA}", _build_input_area_html(locked=False))


@app.get("/chat_fragment", response_class=HTMLResponse)
async def chat_fragment():
    conn = get_connection()
    rows = conn.execute(
        "SELECT sender, message_text, is_action, action_type FROM chat_history ORDER BY id ASC LIMIT 100"
    ).fetchall()
    conn.close()
    html = "".join(
        _render_message(r["sender"], r["message_text"], bool(r["is_action"]))
        for r in rows
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
              onclick="if(!confirm('Вы уверены, что хотите удалить текущую игру и начать заново?')) return false;"
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


def _render_slots():
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    sess = conn.execute("SELECT game_status FROM game_session WHERE session_id = 1").fetchone()
    conn.close()
    game_active = sess and sess["game_status"] == "active"
    tmpl = templates.env.get_template("slots.html")
    return tmpl.render(players=players, max_slots=4, game_active=game_active)


@app.get("/lobby/slots", response_class=HTMLResponse)
async def lobby_slots():
    return _render_slots()


@app.post("/lobby/add_player", response_class=HTMLResponse)
async def lobby_add_player(name: str = Form(...)):
    conn = get_connection()
    conn.execute("INSERT INTO players (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()
    return _render_slots()


@app.post("/lobby/remove_player", response_class=HTMLResponse)
async def lobby_remove_player(player_id: int = Form(...)):
    conn = get_connection()
    conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    return _render_slots()


_DEFAULT_STAT_VALUE = 1


@app.post("/lobby/start", response_class=HTMLResponse)
async def lobby_start():
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    conn.close()

    if not players:
        return Response(status_code=400, content="Нет игроков")

    player_inputs = [{"name": r["name"], "description": r["name"]} for r in players]
    descriptions = [p["description"] for p in player_inputs]

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

    # update existing players with generated stats
    conn = get_connection()
    for inp in player_inputs:
        conn.execute(
            "UPDATE players SET hp_current = 10, hp_max = 10, stats = ?, class_archetype = '' WHERE name = ?",
            (json.dumps({s: _DEFAULT_STAT_VALUE for s in phase_zero.character_stats_templates}, ensure_ascii=False), inp["name"]),
        )
    conn.commit()
    conn.close()

    # update game session
    conn = get_connection()
    conn.execute(
        "UPDATE game_session SET game_status = 'active', setting_blob = ?, global_lore = ? WHERE session_id = 1",
        (json.dumps({"name": phase_zero.setting_name, "description": phase_zero.setting_description}, ensure_ascii=False),
         phase_zero.global_conflict),
    )
    conn.commit()
    conn.close()

    return Response(headers={"HX-Redirect": "/choice"})


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
        clear_oob = '<div id="timer-bar" hx-swap-oob="true"></div>'
        try:
            await broadcast_message(clear_oob)
        except Exception:
            pass
        asyncio.ensure_future(_auto_respond())
        return clear_oob

    secs = int(remaining.total_seconds())
    return (
        f'<div id="timer-bar" hx-swap-oob="true" '
        f'class="text-center text-sm text-gray-400 py-1 bg-gray-800 border-t border-gray-700">'
        f'Мастер внимательно слушает и ждет действий группы: осталось {secs} сек.</div>'
    )


async def _auto_respond():
    conn = get_connection()
    row = conn.execute(
        "SELECT player_id, message_text FROM chat_history WHERE sender != 'GM' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        unlocked = _build_input_area_html(locked=False, oob=True)
        await broadcast_message(unlocked)
        return
    locked = _build_input_area_html(locked=True, oob=True)
    await broadcast_message(locked)
    try:
        turn = await process_player_action(
            row["player_id"] or 0,
            row["message_text"],
            save_message=False,
        )
    except Exception as e:
        print(f"ERROR IN _auto_respond: {e}")
        unlocked = _build_input_area_html(locked=False, oob=True)
        await broadcast_message(unlocked)
        return
    html = _build_chat_response(turn) + _build_input_area_html(locked=False, oob=True)
    await broadcast_message(html)


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

    result = _render_message(player_name, text, is_action=False)
    timer_oob = _build_timer_bar_oob()
    if timer_oob:
        result += timer_oob
        try:
            await broadcast_message(timer_oob)
        except Exception as e:
            print(f"ERROR broadcasting timer: {e}")

    return result


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

    result = _render_message(player_name, text, is_action=True)
    timer_oob = _build_timer_bar_oob()
    if timer_oob:
        result += timer_oob
        try:
            await broadcast_message(timer_oob)
        except Exception as e:
            print(f"ERROR broadcasting timer: {e}")

    return result


def _build_chat_response(turn):
    conn = get_connection()
    rows = conn.execute(
        "SELECT sender, message_text, is_action FROM chat_history ORDER BY id DESC LIMIT 100"
    ).fetchall()
    player_rows = conn.execute("SELECT * FROM players").fetchall()
    conn.close()

    parts = [_render_message(r["sender"], r["message_text"], bool(r["is_action"])) for r in rows]
    parts.append(_render_message("GM", turn.narrative_text))

    panel = "".join(_render_player_card(r) for r in player_rows)

    messages_html = "".join(parts)

    return (
        f'<div id="chat-messages" hx-swap-oob="beforeend">{messages_html}</div>'
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


def _build_timer_bar_oob() -> str:
    sess = get_session()
    if not sess or not sess.timer_ends_at:
        return ""
    try:
        remaining = datetime.fromisoformat(sess.timer_ends_at) - datetime.utcnow()
    except Exception:
        return ""
    if remaining.total_seconds() <= 0:
        return ""
    secs = int(remaining.total_seconds())
    return (
        f'<div id="timer-bar" hx-swap-oob="true" '
        f'class="text-center text-sm text-gray-400 py-1 bg-gray-800 border-t border-gray-700">'
        f'Мастер внимательно слушает и ждет действий группы: осталось {secs} сек.</div>'
    )


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


@app.post("/api/game/reset", response_class=HTMLResponse)
async def reset_game():
    clear_game_data()
    resp = Response(status_code=200, headers={"HX-Redirect": "/"})
    resp.delete_cookie("player_id")
    return resp


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
