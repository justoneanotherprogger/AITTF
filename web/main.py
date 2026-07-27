import json
import html
import asyncio
from dotenv import load_dotenv
from pathlib import Path
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
import uvicorn

load_dotenv()

from datetime import datetime, timedelta

from db.database import (
    init_db, get_connection, add_chat_message, get_session,
    extend_timer, reset_timer, clear_game_data, add_or_update_entity,
    get_player_stats_descriptions, get_player_stat_types,
)
from core.game_engine import calc_hp_max
from models.models import PlayerModel, WorldEntityModel
from llm.ai_generator import generate_initial_world
from models.models import ChatMessageModel
from llm.turn_processor import process_player_action
from llm.context_builder import build_player_descriptions, get_pending_actions

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


async def _require_localhost(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Доступ только с localhost")


_BTN_CLS = 'px-4 py-2 rounded-lg text-sm font-medium transition'
_ENABLED_CLS = 'bg-gray-600 hover:bg-gray-500 text-white'
_ACTION_CLS = 'bg-amber-600 hover:bg-amber-500 text-white'
_DISABLED_CLS = 'bg-gray-700 text-gray-500 cursor-not-allowed'

def _build_input_area_html(locked: bool = False, oob: bool = False) -> str:
    if oob:
        if locked:
            ctrl = (
                f'<div id="chat-controls" class="flex gap-2" hx-swap-oob="true">'
                f'<button type="button" disabled class="{_DISABLED_CLS} {_BTN_CLS}">Сказать</button>'
                f'<button type="button" disabled class="{_DISABLED_CLS} {_BTN_CLS}">Заявить действие</button>'
                f'</div>'
                f'<div id="spinner" hx-swap-oob="true" class="text-amber-400 text-sm text-center animate-pulse">✦ Мастер думает...</div>'
                f'<div id="__lock-input" hx-swap-oob="true" style="display:none"></div>'
            )
        else:
            ctrl = (
                f'<div id="chat-controls" class="flex gap-2" hx-swap-oob="true">'
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
        '<div id="chat-controls" class="flex gap-2">'
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
        '<div class="text-sm text-center h-5 mt-1">'
        '<span id="timer-text" class="text-amber-300 font-medium"></span>'
        '<span id="spinner" class="htmx-indicator text-amber-400">✦ Мастер думает...</span>'
        '</div>'
        '</div>'
        '</div>'
    )

def _render_entry_block(current_player_id: int | None = None, player_name: str | None = None) -> str:
    if current_player_id and player_name:
        escaped = html.escape(player_name)
        return (
            f'<div id="entry-block">'
            f'<span id="entry-has-player" style="display:none"></span>'
            f'<div class="bg-gray-800 rounded-lg border border-emerald-700 p-4 mb-6">'
            f'<div class="flex items-center justify-between">'
            f'<div>'
            f'<span class="font-medium text-lg">{escaped}</span>'
            f'<span class="text-xs text-emerald-400 ml-2">— это вы</span>'
            f'</div>'
            f'<div class="flex gap-3">'
            f'<button hx-get="/lobby/rename_form" hx-target="#entry-block" hx-swap="outerHTML"'
            f' class="text-xs text-gray-400 hover:text-gray-200 underline">Сменить имя</button>'
            f'<button hx-post="/lobby/leave" hx-target="#entry-block" hx-swap="outerHTML"'
            f' class="text-xs text-red-400 hover:text-red-300 underline">Выйти</button>'
            f'</div>'
            f'</div>'
            f'</div>'
            f'</div>'
        )
    return (
        '<div id="entry-block">'
        '<form hx-post="/lobby/add_player" hx-target="#entry-block" hx-swap="outerHTML"'
        ' class="flex gap-3 mb-6">'
        '<input type="text" name="name" placeholder="Имя персонажа" required'
        ' class="flex-1 bg-gray-800 border border-gray-600 rounded-lg px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500">'
        '<button type="submit"'
        ' class="bg-emerald-600 hover:bg-emerald-500 text-white px-6 py-2 rounded-lg text-sm font-medium transition">Войти</button>'
        '</form>'
        '</div>'
    )


def _render_lobby_locked_block() -> str:
    return (
        '<div id="entry-block">'
        '<span id="locked-flag" style="display:none"></span>'
        '<div class="bg-gray-800 rounded-lg border border-gray-700 p-6 mb-6 text-center">'
        '<p class="text-amber-400 font-medium text-lg">🎲 Игра уже началась</p>'
        '<p class="text-gray-400 text-sm mt-2">Вы не можете присоединиться к текущей партии.</p>'
        '<p class="text-gray-500 text-xs mt-2">Дождитесь следующей игры или попросите ведущего сбросить партию.</p>'
        '</div>'
        '</div>'
    )


def _render_player_count() -> str:
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    conn.close()
    return f'<div id="player-count" class="text-sm text-gray-400 text-center mb-4">В лобби: {count}</div>'


def _render_lobby_oob(current_player_id: int | None = None) -> str:
    slots = _render_slots(current_player_id=current_player_id)
    conn = get_connection()
    player_count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    conn.close()
    disabled = "disabled" if player_count == 0 else ""
    count = (
        f'<div id="player-count" hx-swap-oob="true" class="text-sm text-gray-400 text-center mb-4">'
        f'В лобби: {player_count}'
        f'</div>'
    )
    start = (
        f'<div id="start-area" hx-swap-oob="true" class="w-full flex justify-center mb-6">'
        f'<button id="start-btn" type="button" hx-post="/lobby/start" hx-disabled-elt="this"'
        f' class="bg-amber-600 hover:bg-amber-500 text-white px-8 py-3 rounded-lg text-lg font-medium transition" {disabled}>'
        f'Начать игру'
        f'</button></div>'
    )
    return f'<div id="lobby-slots" hx-swap-oob="true">{slots}</div>{count}{start}'


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
    <span id="game-status" class="text-sm px-3 py-1 rounded-full bg-emerald-700 text-emerald-200">exploration</span>
  </header>

  <div class="flex flex-1 overflow-hidden">
    <!-- Sidebar -->
    <aside id="sidebar" class="w-72 bg-gray-800 border-r border-gray-700 p-4 flex-shrink-0 flex flex-col">
      <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Персонажи</h2>
      <div id="players-panel" class="flex-1 overflow-y-auto min-h-0">{PLAYERS_PANEL}</div>
      <div class="border-t-2 border-gray-600 my-3"></div>
      <div id="lore-card">{LORE_CARD}</div>
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

      var timerText = document.getElementById('timer-text');
      var timerRemaining = 0;
      var timerInterval = null;
      var timerPaused = false;

      function startTimer(secs) {
        timerPaused = false;
        timerRemaining = secs;
        updateTimerDisplay();
        if (timerInterval) clearInterval(timerInterval);
        timerInterval = setInterval(function() {
          timerRemaining--;
          if (timerRemaining <= 0) {
            clearInterval(timerInterval);
            timerInterval = null;
            timerText.textContent = '';
            fetch('/timer_expired', {method: 'POST'});
            return;
          }
          updateTimerDisplay();
        }, 1000);
      }

      function stopTimer() {
        timerPaused = false;
        if (timerInterval) clearInterval(timerInterval);
        timerInterval = null;
        timerText.textContent = '';
      }

      function pauseTimer(remaining) {
        timerPaused = true;
        if (remaining !== undefined) timerRemaining = remaining;
        if (timerInterval) clearInterval(timerInterval);
        timerInterval = null;
        updateTimerDisplay();
      }

      function updateTimerDisplay() {
        if (timerPaused) {
          timerText.innerHTML = `⏸ На паузе <button onclick="fetch('/resume_timer',{method:'POST'});startTimer(${timerRemaining})" class="ml-2 px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded text-sm">▶ продолжить</button>`;
        } else {
          timerText.innerHTML = `⏳ Мастер внимательно слушает и ждет действий группы: осталось <span class="text-amber-200 font-bold">${timerRemaining}</span> сек. <button onclick="fetch('/pause_timer',{method:'POST'});pauseTimer(${timerRemaining})" class="ml-2 px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded text-sm">⏸ Пауза</button>`;
        }
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
          timerText.textContent = '';
          evt.preventDefault();
        }
        if (evt.detail.shouldSwap && evt.detail.elt.id === '__unlock-input') {
          setInputLock(false);
          evt.preventDefault();
        }
        if (evt.detail.shouldSwap && evt.detail.elt.id === '__timer-reset') {
          var remaining = parseInt(evt.detail.elt.getAttribute('data-remaining')) || 15;
          startTimer(remaining);
          evt.preventDefault();
        }
        if (evt.detail.shouldSwap && evt.detail.elt.id === '__timer-stop') {
          stopTimer();
          evt.preventDefault();
        }
        if (evt.detail.shouldSwap && evt.detail.elt.id === '__timer-pause') {
          var rem = parseInt(evt.detail.elt.getAttribute('data-remaining')) || 0;
          pauseTimer(rem);
          evt.preventDefault();
        }
      });
      document.body.addEventListener('htmx:afterRequest', function(evt) {
        var path = evt.detail.pathInfo.requestPath;
        if (path === '/send_message' || path === '/declare_action') {
          startTimer(15);
          var inp = document.getElementById('message-input');
          if (inp) inp.value = '';
          var ch = document.getElementById('chat-messages');
          if (ch) { ch.scrollTop = ch.scrollHeight; }
          var btn = document.getElementById('scroll-bottom-btn');
          if (btn) {
            btn.classList.remove('bg-red-500', 'animate-pulse', 'flex');
            btn.classList.add('bg-emerald-600', 'hidden');
          }
        }
      });
      window.startTimer = startTimer;
      window.pauseTimer = pauseTimer;
      window.stopTimer = stopTimer;
    })();
    document.body.addEventListener('htmx:wsAfterMessage', function(evt) {
      if (evt.detail.message.indexOf('__ws-marker-game-reset') !== -1) {
        window.location.href = '/';
      }
    });
  </script>

  <div id="player-modal" onclick="if(event.target===this)this.classList.add('hidden')"
       class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black/60">
    <div id="player-modal-content"
         class="relative bg-gray-800 rounded-xl border border-gray-700 max-w-lg w-full mx-4 max-h-[80vh] overflow-y-auto p-6 shadow-2xl">
    </div>
  </div>

  <div id="lore-modal" onclick="if(event.target===this)this.classList.add('hidden')"
       class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black/60">
    <div id="lore-modal-content"
         class="relative bg-gray-800 rounded-xl border border-gray-700 max-w-lg w-full mx-4 max-h-[80vh] overflow-y-auto p-6 shadow-2xl">
    </div>
  </div>
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


def _render_player_detail_modal(row) -> str:
    stats_values = json.loads(row["stats"])
    inv = json.loads(row["inventory"])
    effects = json.loads(row["status_effects"])
    stats_desc = get_player_stats_descriptions(row["name"])
    stat_types = get_player_stat_types(row["name"])

    hp_pct = round(row["hp_current"] / row["hp_max"] * 100) if row["hp_max"] > 0 else 0
    hp_color = "bg-red-500" if hp_pct < 30 else ("bg-amber-500" if hp_pct < 60 else "bg-emerald-500")

    type_icons = {"offensive": "⚔️", "defensive": "🛡️", "other": "🔧"}

    stats_rows = ""
    for stat_name, stat_value in stats_values.items():
        desc = stats_desc.get(stat_name, "")
        st = stat_types.get(stat_name, "other")
        icon = type_icons.get(st, "🔧")
        s_name = html.escape(stat_name)
        s_val = html.escape(str(stat_value))
        s_desc = html.escape(desc) if desc else '<span class="text-gray-500 italic">—</span>'
        stats_rows += (
            f'<div class="flex justify-between items-start py-1.5 border-b border-gray-700/50 last:border-0">'
            f'<div class="flex-1">'
            f'<span class="text-sm font-medium text-gray-200">{icon} {s_name}</span>'
            f'<span class="text-xs text-gray-400 ml-2">({s_val})</span>'
            f'<div class="text-xs text-gray-500 mt-0.5">{s_desc}</div>'
            f'</div>'
            f'</div>'
        )

    inv_html = ", ".join(html.escape(i) for i in inv) if inv else '<span class="text-gray-500 italic">пусто</span>'
    effects_html = ", ".join(html.escape(e) for e in effects) if effects else '<span class="text-gray-500 italic">нет</span>'
    name = html.escape(row["name"])
    cls = html.escape(row["class_archetype"]) if row["class_archetype"] else "—"
    has_cls_desc = "class_description" in row.keys() and row["class_description"]
    cls_desc = html.escape(row["class_description"]) if has_cls_desc else ""

    cls_desc_block = f'<p class="text-xs text-gray-500 italic mb-3">{cls_desc}</p>' if cls_desc else '<div class="mb-3"></div>'
    return (
        f'<button onclick="document.getElementById(\'player-modal\').classList.add(\'hidden\')"'
        f' class="absolute top-3 right-3 text-gray-400 hover:text-white text-2xl leading-none cursor-pointer">&times;</button>'
        f'<h2 class="text-lg font-bold mb-1">{name}</h2>'
        f'<p class="text-xs text-gray-400 mb-1">{cls}</p>'
        f'{cls_desc_block}'
        f'<div class="mb-4">'
        f'<div class="h-2 bg-gray-600 rounded-full overflow-hidden">'
        f'<div class="h-full {hp_color} rounded-full" style="width:{hp_pct}%"></div></div>'
        f'<span class="text-xs text-gray-400">{row["hp_current"]}/{row["hp_max"]} HP</span></div>'
        f'<h3 class="text-sm font-semibold text-gray-300 mb-2">Характеристики</h3>'
        f'<div class="mb-4">{stats_rows}</div>'
        f'<h3 class="text-sm font-semibold text-gray-300 mb-1">Инвентарь</h3>'
        f'<p class="text-xs text-gray-400 mb-3">{inv_html}</p>'
        f'<h3 class="text-sm font-semibold text-gray-300 mb-1">Эффекты</h3>'
        f'<p class="text-xs text-gray-400">{effects_html}</p>'
    )


@app.get("/player/{player_id}/detail", response_class=HTMLResponse)
async def player_detail(player_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    conn.close()
    if not row:
        return '<p class="text-gray-400">Персонаж не найден</p>'
    return _render_player_detail_modal(row)


def _render_player_card(row) -> str:
    stats = json.loads(row["stats"])
    inv = json.loads(row["inventory"])
    effects = json.loads(row["status_effects"])
    hp_pct = round(row["hp_current"] / row["hp_max"] * 100) if row["hp_max"] > 0 else 0
    hp_color = "bg-red-500" if hp_pct < 30 else ("bg-amber-500" if hp_pct < 60 else "bg-emerald-500")
    stats_str = " | ".join(f"{k}: {v}" for k, v in stats.items())
    inv_str = ", ".join(inv) if inv else "пусто"

    return (
        f'<div class="cursor-pointer hover:border-emerald-500 transition"'
        f' onclick="htmx.ajax(\'GET\',\'/player/{row["id"]}/detail\',{{target:\'#player-modal-content\',swap:\'innerHTML\'}});document.getElementById(\'player-modal\').classList.remove(\'hidden\')">'
        f'<div class="bg-gray-750 border border-gray-600 rounded-lg p-3 mb-2">'
        f'<div class="font-semibold text-sm">{row["name"]}</div>'
        f'<div class="text-xs text-gray-400 mt-1">{row["class_archetype"] or "—"}</div>'
        f'<div class="mt-2"><div class="h-2 bg-gray-600 rounded-full overflow-hidden">'
        f'<div class="h-full {hp_color} rounded-full" style="width:{hp_pct}%"></div></div>'
        f'<span class="text-xs text-gray-400">{row["hp_current"]}/{row["hp_max"]} HP</span></div>'
        f'<div class="text-xs text-gray-400 mt-1">{stats_str}</div>'
        f'<div class="text-xs text-gray-500 mt-1">🎒 {inv_str}</div>'
        + (f'<div class="text-xs text-red-400 mt-1">⚠ {", ".join(effects)}</div>' if effects else "")
        + "</div></div>"
    )


def _render_lore_card() -> str:
    return (
        '<div class="cursor-pointer hover:border-emerald-500 transition"'
        ' onclick="htmx.ajax(\'GET\',\'/lore/data\',{target:\'#lore-modal-content\',swap:\'innerHTML\'});document.getElementById(\'lore-modal\').classList.remove(\'hidden\')">'
        '<div class="bg-gray-750 border border-gray-600 rounded-lg p-3">'
        '<div class="font-semibold text-sm">📖 Лор / Сюжет</div>'
        '<div class="text-xs text-gray-400 mt-1">Сеттинг и суть конфликта</div>'
        '</div></div>'
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

    # locked for non-players during any active game stage
    if sess and sess["game_status"] in ("backstory_gathering", "exploration", "combat"):
        pid = request.cookies.get("player_id")
        current_player = next((dict(p) for p in players if p["id"] == int(pid)), None) if pid else None
        if current_player:
            if sess["game_status"] == "backstory_gathering":
                return RedirectResponse(url="/backstories")
            panel_html = await _render_players_panel_str()
            lore_card_html = _render_lore_card()
            return _INDEX_HTML.replace("{PLAYER_NAME}", current_player["name"]).replace("{CURRENT_PLAYER_ID}", str(current_player["id"])).replace("{INPUT_AREA}", _build_input_area_html(locked=False)).replace("{PLAYERS_PANEL}", panel_html).replace("{LORE_CARD}", lore_card_html)
        return templates.TemplateResponse(request, "lobby.html", {
            "players": players, "current_player_id": None,
            "entry_block": _render_lobby_locked_block(),
        })

    # lobby — normal flow
    pid = request.cookies.get("player_id")
    current_player_id = int(pid) if pid else None

    if current_player_id:
        my_player = next((dict(p) for p in players if p["id"] == current_player_id), None)
        if my_player is None:
            resp = templates.TemplateResponse(request, "lobby.html", {
                "players": players, "current_player_id": None,
                "entry_block": _render_entry_block(None),
            })
            resp.delete_cookie("player_id")
            return resp
    else:
        my_player = None

    entry_block = _render_entry_block(current_player_id, my_player["name"] if my_player else None)
    return templates.TemplateResponse(request, "lobby.html", {
        "players": players, "current_player_id": current_player_id,
        "entry_block": entry_block,
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


@app.get("/lore/data", response_class=HTMLResponse)
async def lore_data():
    conn = get_connection()
    sess = conn.execute(
        "SELECT setting_blob, global_lore FROM game_session WHERE session_id = 1"
    ).fetchone()
    conn.close()
    if not sess:
        return '<p class="text-gray-400">Данные о мире не найдены</p>'
    blob = json.loads(sess["setting_blob"])
    name = blob.get("name", "Мир")
    description = blob.get("description", "")
    conflict = sess["global_lore"] or ""
    return (
        '<button onclick="document.getElementById(\'lore-modal\').classList.add(\'hidden\')"'
        ' class="absolute top-3 right-3 text-gray-400 hover:text-white text-2xl leading-none cursor-pointer">&times;</button>'
        f'<h2 class="text-lg font-bold mb-3">📖 {html.escape(name)}</h2>'
        f'<div class="text-sm text-gray-300 leading-relaxed mb-6">{html.escape(description)}</div>'
        '<h3 class="text-md font-semibold text-amber-400 mb-2">⚔️ Суть конфликта</h3>'
        f'<div class="text-sm text-gray-300 leading-relaxed">{html.escape(conflict)}</div>'
    )


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
  <script src="https://unpkg.com/htmx.org@2.0.4/dist/ext/ws.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <title>Выбор персонажа — AI Tabletop Framework</title>
</head>
<body class="bg-gray-900 text-gray-100 h-screen flex items-center justify-center"
      hx-ext="ws" ws-connect="/ws/chat">
  <div class="w-full max-w-md p-6">
    <h1 class="text-2xl font-bold mb-6 text-center">Выберите персонажа</h1>
    {cards if cards else '<p class="text-gray-400 text-center">Нет доступных персонажей</p>'}
  </div>
  <script>
    document.body.addEventListener('htmx:wsAfterMessage', function(evt) {{
      if (evt.detail.message.indexOf('__ws-marker-game-reset') !== -1) {{
        window.location.href = '/';
      }}
    }});
  </script>
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

    sess = get_session()
    game_in_progress = sess and sess.game_status in ("backstory_gathering", "exploration", "combat")
    target = "/choice" if game_in_progress else "/"

    resp = RedirectResponse(url=target)
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

    if not current_player:
        return RedirectResponse(url="/")

    return templates.TemplateResponse(request, "backstories.html", {
        "players": players, "current_player": current_player
    })


async def _broadcast_lobby_refresh():
    await asyncio.sleep(0.05)
    await manager.broadcast_html('<span id="__ws-marker-refresh-lobby" style="display:none"></span>')


async def _broadcast_backstory_refresh():
    await manager.broadcast_html('<span id="__ws-marker-backstory-updated" style="display:none"></span>')


async def _broadcast_game_started():
    await manager.broadcast_html('<span id="__ws-marker-game-started" style="display:none"></span>')


async def _broadcast_game_reset():
    await manager.broadcast_html('<span id="__ws-marker-game-reset" style="display:none"></span>')


async def _broadcast_generating_world():
    await manager.broadcast_html('<span id="__ws-marker-generating-world" style="display:none"></span>')


def _render_slots(current_player_id: int | None = None):
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    tmpl = templates.env.get_template("slots.html")
    return tmpl.render(players=players, current_player_id=current_player_id)


@app.get("/lobby/entry_block", response_class=HTMLResponse)
async def lobby_entry_block(request: Request):
    pid = request.cookies.get("player_id")
    if pid:
        conn = get_connection()
        row = conn.execute("SELECT * FROM players WHERE id = ?", (int(pid),)).fetchone()
        conn.close()
        if row:
            return _render_entry_block(int(pid), row["name"])
    return _render_entry_block(None)


@app.get("/lobby/lobby_meta", response_class=HTMLResponse)
async def lobby_meta():
    count = _render_player_count()
    conn = get_connection()
    player_count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    conn.close()
    disabled = "disabled" if player_count == 0 else ""
    start = (
        f'<div id="start-area" class="w-full flex justify-center mb-6">'
        f'<button id="start-btn" type="button" hx-post="/lobby/start" hx-disabled-elt="this"'
        f' class="bg-amber-600 hover:bg-amber-500 text-white px-8 py-3 rounded-lg text-lg font-medium transition" {disabled}>'
        f'Начать игру'
        f'</button></div>'
    )
    return f'<div id="lobby-meta">{count}{start}</div>'


@app.get("/lobby/rename_form", response_class=HTMLResponse)
async def lobby_rename_form(request: Request):
    pid = request.cookies.get("player_id")
    if not pid:
        return _render_entry_block(None)
    conn = get_connection()
    row = conn.execute("SELECT * FROM players WHERE id = ?", (int(pid),)).fetchone()
    conn.close()
    if not row:
        return _render_entry_block(None)
    escaped = html.escape(row["name"])
    return (
        f'<div id="entry-block">'
        f'<form hx-post="/lobby/rename_player" hx-target="#entry-block" hx-swap="outerHTML"'
        f' class="flex gap-3 mb-6">'
        f'<input type="text" name="name" value="{escaped}" required'
        f' class="flex-1 bg-gray-800 border border-gray-600 rounded-lg px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500">'
        f'<button type="submit"'
        f' class="bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-2 rounded-lg text-sm font-medium transition">Сохранить</button>'
        f'<button type="button" hx-get="/lobby/entry_block" hx-target="#entry-block" hx-swap="outerHTML"'
        f' class="text-gray-400 hover:text-gray-200 underline text-sm">Отмена</button>'
        f'</form>'
        f'</div>'
    )


@app.post("/lobby/rename_player", response_class=HTMLResponse)
async def lobby_rename_player(request: Request, name: str = Form(...)):
    pid = request.cookies.get("player_id")
    if not pid:
        return _render_entry_block(None)
    conn = get_connection()
    conn.execute("UPDATE players SET name = ? WHERE id = ?", (name, int(pid)))
    conn.commit()
    conn.close()
    entry = _render_entry_block(int(pid), name)
    oob = _render_lobby_oob(current_player_id=int(pid))
    resp = HTMLResponse(content=entry + oob)
    asyncio.create_task(_broadcast_lobby_refresh())
    return resp


@app.post("/lobby/leave", response_class=HTMLResponse)
async def lobby_leave(request: Request):
    pid = request.cookies.get("player_id")
    if pid:
        conn = get_connection()
        conn.execute("DELETE FROM players WHERE id = ?", (int(pid),))
        conn.commit()
        conn.close()
    entry = _render_entry_block(None)
    oob = _render_lobby_oob()
    resp = HTMLResponse(content=entry + oob)
    resp.delete_cookie("player_id")
    asyncio.create_task(_broadcast_lobby_refresh())
    return resp


@app.get("/lobby/slots", response_class=HTMLResponse)
async def lobby_slots(request: Request):
    pid = request.cookies.get("player_id")
    return _render_slots(current_player_id=int(pid) if pid else None)


@app.get("/lobby/backstory_players", response_class=HTMLResponse)
async def backstory_players(request: Request):
    conn = get_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    pid = request.cookies.get("player_id")
    current_player = next((dict(p) for p in players if p["id"] == int(pid)), None) if pid else None
    return templates.TemplateResponse(request, "backstories_players.html", {
        "players": players, "current_player": current_player
    })


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
    player_id = cur.lastrowid
    cur.execute("UPDATE players SET is_occupied = 1 WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    entry = _render_entry_block(player_id, name)
    oob = _render_lobby_oob(current_player_id=player_id)
    resp = HTMLResponse(content=entry + oob)
    resp.set_cookie(key="player_id", value=str(player_id))
    asyncio.create_task(_broadcast_lobby_refresh())
    return resp


@app.post("/lobby/remove_player", response_class=HTMLResponse)
async def lobby_remove_player(request: Request, player_id: int = Form(...)):
    conn = get_connection()
    conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    pid = request.cookies.get("player_id")
    current = int(pid) if pid else None
    if current and current == player_id:
        entry = _render_entry_block(None)
        oob = _render_lobby_oob()
        resp = HTMLResponse(content=entry + oob)
        resp.delete_cookie("player_id")
    else:
        oob = _render_lobby_oob(current_player_id=current)
        resp = HTMLResponse(content=oob)
    asyncio.create_task(_broadcast_lobby_refresh())
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
    asyncio.create_task(_broadcast_backstory_refresh())
    return resp


@app.get("/lobby/backstory_status", response_class=HTMLResponse)
async def backstory_status(request: Request):
    pid = request.cookies.get("player_id")
    if not pid:
        return '<p class="text-gray-400 text-sm text-center mt-4">У вас нет персонажа. Генерация мира недоступна.</p>'
    conn = get_connection()
    player = conn.execute("SELECT id FROM players WHERE id = ?", (int(pid),)).fetchone()
    players = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    if not player:
        return '<p class="text-gray-400 text-sm text-center mt-4">У вас нет персонажа. Генерация мира недоступна.</p>'

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

    asyncio.create_task(_broadcast_generating_world())

    try:
        phase_zero = await generate_initial_world(descriptions)
    except Exception as e:
        print(f"[generate_world] ERROR: {e}")
        asyncio.create_task(_broadcast_backstory_refresh())
        return Response(status_code=500, content=f"Ошибка генерации мира: {e}")

    # save world entities
    add_or_update_entity(WorldEntityModel(
        entity_type="setting", name=phase_zero.setting_name,
        data={"description": phase_zero.setting_description},
    ))
    add_or_update_entity(WorldEntityModel(
        entity_type="rule", name="global_conflict",
        data={"description": phase_zero.global_conflict},
    ))
    stats_for_db = {
        p: {s: sd.model_dump() for s, sd in pstats.items()}
        for p, pstats in phase_zero.character_stats_templates.items()
    }
    add_or_update_entity(WorldEntityModel(
        entity_type="rule", name="stats_system",
        data={"stats": stats_for_db},
    ))
    add_or_update_entity(WorldEntityModel(
        entity_type="rule", name="initial_narrative",
        data={"text": phase_zero.initial_narrative_text},
    ))

    # update existing players with generated stats and archetypes
    conn = get_connection()
    for player in players:
        cls_def = phase_zero.character_classes.get(player["name"])
        class_name = cls_def.name if cls_def else ""
        class_desc = cls_def.description if cls_def else ""
        player_stats = phase_zero.character_stats_templates.get(player["name"], {})
        def_sum = sum(sd.initial_value for sd in player_stats.values() if sd.stat_type == "defensive")
        hp_max = calc_hp_max(def_sum)
        conn.execute(
            "UPDATE players SET hp_current = ?, hp_max = ?, stats = ?, class_archetype = ?, class_description = ? WHERE name = ?",
            (hp_max, hp_max,
             json.dumps({s: sd.initial_value for s, sd in player_stats.items()}, ensure_ascii=False),
             class_name, class_desc, player["name"]),
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

    asyncio.create_task(manager.broadcast_html('<span id="__ws-marker-world-generated" style="display:none"></span>'))
    return Response(headers={"HX-Redirect": "/"})


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
    asyncio.create_task(_broadcast_game_started())
    return Response(headers={"HX-Redirect": "/backstories"})


async def _auto_respond():
    pending = get_pending_actions()
    if not pending:
        unlocked = _build_input_area_html(locked=False, oob=True)
        await manager.broadcast_html(unlocked)
        return
    locked = _build_input_area_html(locked=True, oob=True)
    await manager.broadcast_html(locked)
    try:
        turn, extra = await process_player_action()
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
    extra_html = "".join(extra) if extra else ""
    html = (
        gm_html
        + extra_html
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


@app.post("/timer_expired", response_class=HTMLResponse)
async def timer_expired():
    sess = get_session()
    if not sess or not sess.timer_ends_at or sess.timer_ends_at.startswith("PAUSED:"):
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


@app.post("/pause_timer", response_class=HTMLResponse)
async def pause_timer():
    conn = get_connection()
    row = conn.execute("SELECT timer_ends_at FROM game_session WHERE session_id = 1").fetchone()
    conn.close()
    if not row or not row["timer_ends_at"] or row["timer_ends_at"].startswith("PAUSED:"):
        return ""
    try:
        ends = datetime.fromisoformat(row["timer_ends_at"])
        remaining = int((ends - datetime.utcnow()).total_seconds())
        if remaining < 1:
            return ""
    except Exception:
        return ""
    conn = get_connection()
    conn.execute("UPDATE game_session SET timer_ends_at = ? WHERE session_id = 1", (f"PAUSED:{remaining}",))
    conn.commit()
    conn.close()
    pause_html = f'<div id="__timer-pause" hx-swap-oob="true" data-remaining="{remaining}" style="display:none"></div>'
    await manager.broadcast_html(pause_html)
    return pause_html


@app.post("/resume_timer", response_class=HTMLResponse)
async def resume_timer():
    conn = get_connection()
    row = conn.execute("SELECT timer_ends_at FROM game_session WHERE session_id = 1").fetchone()
    conn.close()
    remaining = 15
    if row and row["timer_ends_at"].startswith("PAUSED:"):
        try:
            remaining = int(row["timer_ends_at"].split(":", 1)[1])
        except (ValueError, IndexError):
            remaining = 15
    extend_timer(remaining)
    timer_reset = f'<div id="__timer-reset" hx-swap-oob="true" data-remaining="{remaining}" style="display:none"></div>'
    await manager.broadcast_html(timer_reset)
    return timer_reset


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return """<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="https://cdn.tailwindcss.com"></script>
<title>Доступ запрещён</title>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen flex items-center justify-center">
  <div class="text-center max-w-md p-6">
    <h1 class="text-2xl font-bold mb-4">🔒 Доступ запрещён</h1>
    <p class="text-gray-400">Админ-панель доступна только с локального компьютера сервера (localhost).</p>
    <a href="/" class="inline-block mt-6 text-sm text-gray-500 hover:text-gray-300 underline">← Назад в игру</a>
  </div>
</body>
</html>"""

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
      <div id="admin-status-block" class="flex items-center gap-4 bg-gray-800 border border-gray-700 rounded-lg p-4 flex-wrap">
        <span class="text-sm">Текущий статус:</span>
        <span class="text-sm px-3 py-1 rounded-full
          {'bg-red-700 text-red-200' if status == 'combat' else 'bg-emerald-700 text-emerald-200'}">{status}</span>
        <form hx-post="/admin/toggle_status" hx-target="#admin-status-block" hx-swap="outerHTML">
          <button type="submit"
                  class="bg-amber-600 hover:bg-amber-500 text-white px-4 py-2 rounded-lg text-sm transition">
            Переключить в {next_status}
          </button>
        </form>
        <button type="button" hx-post="/api/game/reset" hx-disabled-elt="this"
                hx-confirm="Вы уверены, что хотите удалить текущую игру и начать заново?"
                class="text-xs text-red-400 hover:text-red-300 underline">Сбросить партию</button>
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
    request: Request,
    player_id: int = Form(...),
    hp_current: int = Form(...),
    hp_max: int = Form(...),
    _localhost: None = Depends(_require_localhost),
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
async def admin_toggle_status(_: None = Depends(_require_localhost)):
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
    asyncio.create_task(_broadcast_game_reset())
    resp = Response(status_code=200, headers={"HX-Redirect": "/"})
    resp.delete_cookie("player_id")
    return resp


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
