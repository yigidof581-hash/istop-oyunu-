#!/usr/bin/env python3
"""
İstop - Yerel Ağ Çok Oyunculu Oyun
Kullanım: python server.py
Sonra aynı WiFi'daki herkes http://localhost:8080 adresine girsin
"""

import http.server
import json
import uuid
import time
import threading
import random
import os
import html as html_mod
from urllib.parse import urlparse, parse_qs
from socketserver import ThreadingMixIn
import websockets
import asyncio

import os
PORT = int(os.environ.get('PORT', '8080'))
HOST = '0.0.0.0'
APP_VERSION = 'v20-ws'

# ============================================================
# OYUN DURUMU (bellekte)
# ============================================================
games = {}
nick_uids = {}  # nickname -> uid (çakışma önleme)
bots = {}       # game_id -> set of bot uids

# ============================================================
# WEBSOCKET + KEEP-ALIVE
# ============================================================
WS_PORT_OFFSET = 1  # WS sunucu HTTP portu + 1’de çalışır
ws_clients = {}     # game_id -> set of websocket
ws_meta = {}        # websocket -> {'gameId':..,'uid':..}
ws_lock = threading.Lock()
ws_loop = None      # WS sunucunun event loop referansı

def build_public_state(game, uid):
    player = game.get('players', {}).get(uid)
    if not player:
        if game.get('status') in ('lobby', 'waiting'):
            players = game.get('players', {})
            player = next(iter(players.values())) if players else None
        if not player:
            return None
    me = {
        'uid': uid,
        'nickname': player.get('nickname', '?'),
        'lives': player.get('lives', 0),
        'isEbe': player.get('isEbe', False),
        'eliminated': player.get('eliminated', False),
    }
    include_players = uid == game.get('host') or uid in game.get('players', {})
    state = {
        'phase': game.get('phase', 'waiting'),
        'phaseVersion': game.get('phaseVersion', 0),
        'currentEbe': game.get('currentEbe'),
        'targetPlayer': game.get('targetPlayer'),
        'selectedColor': game.get('selectedColor'),
        'catchDeadline': game.get('catchDeadline', 0),
        'colorDeadline': game.get('colorDeadline', 0),
        'message': game.get('message', ''),
        'players': {},
        'eliminated': game.get('eliminated', {}) if include_players else {},
        'winner': game.get('winner'),
        'status': game.get('status', 'lobby'),
        'host': game.get('host'),
    }
    for puid, p in game.get('players', {}).items():
        state['players'][puid] = {
            'nickname': p.get('nickname', '?'),
            'lives': p.get('lives', 0),
            'isEbe': p.get('isEbe', False),
        }
    state['colorHitOrder'] = dict(game.get('colorHitOrder', {}))
    return {'ok': True, 'state': state, 'me': me, 'version': game.get('phaseVersion', 0)}

def broadcast_state(game_id):
    game = games.get(game_id)
    if not game:
        return
    with ws_lock:
        clients = list(ws_clients.get(game_id, set()))
    for ws in clients:
        meta = ws_meta.get(ws)
        if not meta:
            continue
        result = build_public_state(game, meta['uid'])
        if not result:
            continue
        msg = json.dumps({'type': 'state', 'data': result})
        if ws_loop:
            try:
                asyncio.run_coroutine_threadsafe(ws.send(msg), ws_loop)
            except Exception:
                pass

async def ws_handler(websocket, path):
    global ws_loop
    ws_loop = asyncio.get_event_loop()
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get('type') == 'subscribe':
                gid = msg.get('gameId')
                uid = msg.get('uid')
                if gid and uid:
                    with ws_lock:
                        ws_clients.setdefault(gid, set()).add(websocket)
                        ws_meta[websocket] = {'gameId': gid, 'uid': uid}
                    # Hemen mevcut state’i yolla
                    game = games.get(gid)
                    if game:
                        result = build_public_state(game, uid)
                        if result:
                            await websocket.send(json.dumps({'type': 'state', 'data': result}))
            elif msg.get('type') == 'unsubscribe':
                with ws_lock:
                    for gid in list(ws_clients.keys()):
                        ws_clients[gid].discard(websocket)
                    ws_meta.pop(websocket, None)
            elif msg.get('type') == 'ping':
                try:
                    await websocket.send(json.dumps({'type': 'pong'}))
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        with ws_lock:
            for gid in list(ws_clients.keys()):
                ws_clients[gid].discard(websocket)
            ws_meta.pop(websocket, None)

def start_ws_server():
    port = PORT + WS_PORT_OFFSET
    async def main():
        async with websockets.serve(ws_handler, "0.0.0.0", port, ping_interval=20, ping_timeout=20):
            await asyncio.Future()
    asyncio.run(main())

# ============================================================
# BOT SİSTEMİ
# ============================================================
BOT_NAMES = ['Bot Ali','Bot Zeynep','Bot Mert','Bot Elif','Bot Can','Bot Ada']

def bot_add(game, host_uid):
    game_id = id(game)
    if game_id not in bots:
        bots[game_id] = set()
    existing = bots[game_id]
    if len(existing) >= 2:
        return None, 'En fazla 2 bot eklenebilir'
    used = set(BOT_NAMES) - {game['players'][u]['nickname'] for u in game['players']}
    if not used:
        return None, 'Bot adı kalmadı'
    name = sorted(used)[0]
    buid = new_uid()
    game['players'][buid] = {
        'nickname': name,
        'lives': 3,
        'isEbe': False,
        'eliminated': False,
        'joinedAt': time.time(),
        'lastSeen': time.time(),
    }
    game.setdefault('readyPlayers', {})[buid] = True
    bots[game_id].add(buid)
    touch(game)
    game['phaseVersion'] += 1
    game['message'] = f'🤖 {name} katıldı!'
    return buid, None

def bot_remove(game, buid):
    game_id = id(game)
    if game_id in bots:
        bots[game_id].discard(buid)
    remove_player(game, buid)

def bot_tick():
    now = time.time()
    for gid, game in list(games.items()):
        if game.get('status') != 'playing':
            continue
        gid_bots = bots.get(id(game), set())
        if not gid_bots:
            continue
        phase = game.get('phase')
        ebe = game.get('currentEbe')
        pls = game.get('players', {})
        elim = game.get('eliminated', {})
        for buid in list(gid_bots):
            if buid not in pls:
                continue
            if pls[buid].get('eliminated') or pls[buid].get('lives', 0) <= 0:
                continue
            if phase == 'ebe_turn' and buid == ebe:
                alive = [u for u in pls if u != buid and not elim.get(u) and pls[u].get('lives', 0) > 0]
                if alive:
                    target = random.choice(alive)
                    game['phase'] = 'catching'
                    game['targetPlayer'] = target
                    game['catchDeadline'] = time.time() * 1000 + 6000
                    game['message'] = f'⚡ {pls[target]["nickname"]} yakalamaya çalışıyor!'
                    game['phaseVersion'] += 1
                    broadcast_state(gid)
                    t = threading.Timer(6.5, check_catch_timeout, args=[gid])
                    t.daemon = True
                    t.start()
            elif phase == 'catching' and buid == game.get('targetPlayer'):
                if game['players'][buid].get('eliminated'):
                    continue
                if time.time() * 1000 > game.get('catchDeadline', 0):
                    continue
                game['phase'] = 'ebe_turn'
                game['currentEbe'] = buid
                game['targetPlayer'] = None
                game['catchDeadline'] = 0
                for u in pls:
                    pls[u]['isEbe'] = (u == buid)
                game['message'] = f'🎯 {pls[buid]["nickname"]} topu yakaladı! Yeni ebe!'
                game['phaseVersion'] += 1
                broadcast_state(gid)
            elif phase == 'istop' and buid == ebe:
                color = random.choice(['green', 'red'])
                game['phase'] = 'color_battle'
                game['selectedColor'] = color
                game['colorDeadline'] = time.time() * 1000 + 5000
                game['colorHitOrder'] = {}
                renk_adi = 'YEŞİL' if color == 'green' else 'KIRMIZI'
                game['message'] = f'🔍 {renk_adi} rengine dokun!'
                game['phaseVersion'] += 1
                broadcast_state(gid)
                t = threading.Timer(5.5, check_color_timeout, args=[gid])
                t.daemon = True
                t.start()
            elif phase == 'color_battle' and buid != ebe:
                if game['players'][buid].get('eliminated'):
                    continue
                if buid in game.get('colorHitOrder', {}):
                    continue
                game['colorHitOrder'][buid] = time.time() * 1000
                broadcast_state(gid)

def bot_loop():
    while True:
        time.sleep(0.5)
        try:
            bot_tick()
        except Exception:
            pass

def new_uid(): return uuid.uuid4().hex[:12]

def touch(game):
    """Oda son hareket zamanini guncelle (bos oda temizligi icin)."""
    if game:
        game['updatedAt'] = time.time()

PLAYER_TIMEOUT_PLAYING = 90   # oyun icinde baglanti kopma toleransi (sn)
PLAYER_TIMEOUT_LOBBY = 180    # lobide bekleme toleransi (sn)

def remove_player(game, uid):
    """Oyuncuyu odadan cikarir; oyun ortasindaysa akisi onarir."""
    players = game.get('players', {})
    if uid not in players:
        return False
    nick = players[uid].get('nickname', '?')
    players.pop(uid, None)
    game.get('eliminated', {}).pop(uid, None)
    game.get('colorHitOrder', {}).pop(uid, None)
    touch(game)
    game['phaseVersion'] = game.get('phaseVersion', 0) + 1

    if not players:
        game['status'] = 'deleted'
        return True

    if game.get('host') == uid:
        game['host'] = next(iter(players))
        game['message'] = f"{players[game['host']]['nickname']} yeni ev sahibi"
    else:
        game['message'] = f'{nick} ayrıldı'

    if game.get('status') != 'playing':
        return True

    alive = [u for u, p in players.items() if not p.get('eliminated') and p.get('lives', 0) > 0]
    if len(alive) <= 1:
        winner = alive[0] if alive else None
        game['status'] = 'finished'
        game['phase'] = 'result'
        game['winner'] = winner
        game['currentEbe'] = None
        game['targetPlayer'] = None
        game['selectedColor'] = None
        game['catchDeadline'] = 0
        game['colorDeadline'] = 0
        game['message'] = (f'🏆 {players[winner]["nickname"]} oyunu KAZANDI!' if winner else 'Oyun bitti')
        return True

    # Ebe kactiysa yeni ebe secilir
    if game.get('currentEbe') == uid:
        game['currentEbe'] = random.choice(alive)
        game['targetPlayer'] = None
        game['selectedColor'] = None
        game['catchDeadline'] = 0
        game['colorDeadline'] = 0
        game['colorHitOrder'] = {}
        game['phase'] = 'ebe_turn'
        for u in players:
            players[u]['isEbe'] = (u == game['currentEbe'])
        game['message'] = f"👑 {players[game['currentEbe']]['nickname']} yeni ebe ({nick} ayrıldı)"
        return True

    # Hedef kactiysa tur iptal edilir
    if game.get('targetPlayer') == uid:
        game['targetPlayer'] = None
        if game.get('phase') in ('catching', 'istop', 'color_battle'):
            game['phase'] = 'ebe_turn'
            game['selectedColor'] = None
            game['catchDeadline'] = 0
            game['colorDeadline'] = 0
            game['colorHitOrder'] = {}
    return True

def reap_disconnected():
    """Uzun suredir istek atmayan (sekmeyi kapatmis) oyunculari odadan atar."""
    now = time.time()
    for gid, g in list(games.items()):
        if g.get('status') == 'deleted':
            continue
        limit = PLAYER_TIMEOUT_PLAYING if g.get('status') == 'playing' else PLAYER_TIMEOUT_LOBBY
        for uid, p in list(g.get('players', {}).items()):
            last = p.get('lastSeen', g.get('createdAt', now))
            if now - last > limit:
                remove_player(g, uid)

def reaper():
    """5 sn'de bir baglantisi kopan oyunculari temizler."""
    while True:
        time.sleep(5)
        try:
            reap_disconnected()
        except Exception:
            pass

def cleanup_once():
    """Bos / hareketsiz odalari siler. Kac oda silindigini dondurur."""
    now = time.time()
    removed = 0
    for gid, g in list(games.items()):
        idle = now - g.get('updatedAt', g.get('createdAt', now))
        if g.get('status') == 'deleted' or not g.get('players') or idle > 1800:
            games.pop(gid, None)
            removed += 1
    return removed

def janitor():
    """Her 60 sn'de bir cleanup_once calistirir."""
    while True:
        time.sleep(60)
        try:
            cleanup_once()
        except Exception:
            pass

# ============================================================
# SUNUCU
# ============================================================
class ThreadedServer(ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

class Handler(http.server.BaseHTTPRequestHandler):
    # Sessiz log
    def log_message(self, fmt, *args):
        pass  # terminali kirletme

    def _send(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _html(self, content, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(content.encode('utf-8'))

    def _error(self, msg, status=400):
        self._send({'ok': False, 'error': msg}, status)

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0: return {}
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        params = parse_qs(parsed.query)

        if path == '/' or path == '':
            self._html(get_index_html())
            return

        # Surum bilgisi (deploy dogrulamasi)
        if path == '/api/version':
            self._send({'ok': True, 'version': APP_VERSION})
            return

        # Keep-alive ping
        if path == '/api/ping':
            self._send({'ok': True, 'pong': True, 'time': time.time()})
            return

        # Oda listesi
        if path == '/api/rooms':
            room_list = []
            for gid, g in games.items():
                if g.get('status') != 'deleted' and len(g.get('players', {})) > 0:
                    pc = len(g.get('players', {}))
                    room_list.append({
                        'id': gid[:8],
                        '_id': gid,
                        'playerCount': pc,
                        'maxPlayers': 4,
                        'full': pc >= 4,
                        'hasHost': g.get('host') is not None,
                        'message': g.get('message', ''),
                        'phase': g.get('phase', 'waiting')
                    })
            room_list.sort(key=lambda r: r.get('_id', ''), reverse=True)
            self._send({'ok': True, 'rooms': room_list})
            return

        # Oyun durumu
        if path.startswith('/api/game/') and path.endswith('/state'):
            parts = path.split('/')
            if len(parts) < 4:
                self._error('Bad path')
                return
            gid = parts[3]
            uid = params.get('uid', [None])[0]
            since = int(params.get('since', ['0'])[0])
            game = games.get(gid)
            if not game:
                self._error('Game not found', 404)
                return
            touch(game)

            player = game.get('players', {}).get(uid)
            if not player:
                # Lobby modunda bile state verelim
                if game.get('status') in ('lobby', 'waiting'):
                    player = game.get('players', {}).get(list(game.get('players', {}).keys())[0]) if game.get('players') else None
                if not player:
                    self._error('Not in game', 403)
                    return
            else:
                # Oyuncu hala bagli: lastSeen'i guncelle
                player['lastSeen'] = time.time()

            # Oyuncunun kendi görüşü
            me = {
                'uid': uid,
                'nickname': player.get('nickname', '?'),
                'lives': player.get('lives', 0),
                'isEbe': player.get('isEbe', False),
                'eliminated': player.get('eliminated', False),
            }

            include_players = uid == game.get('host') or uid in game.get('players', {})
            state = {
                'phase': game.get('phase', 'waiting'),
                'phaseVersion': game.get('phaseVersion', 0),
                'currentEbe': game.get('currentEbe'),
                'targetPlayer': game.get('targetPlayer'),
                'selectedColor': game.get('selectedColor'),
                'catchDeadline': game.get('catchDeadline', 0),
                'colorDeadline': game.get('colorDeadline', 0),
                'message': game.get('message', ''),
                'players': {},
                'eliminated': game.get('eliminated', {}) if include_players else {},
                'winner': game.get('winner'),
                'status': game.get('status', 'lobby'),
                'host': game.get('host'),
            }

            for puid, p in game.get('players', {}).items():
                state['players'][puid] = {
                    'nickname': p.get('nickname', '?'),
                    'lives': p.get('lives', 0),
                    'isEbe': p.get('isEbe', False),
                    'isBot': puid in bots.get(id(game), set()),
                }

            state['colorHitOrder'] = dict(game.get('colorHitOrder', {}))
            state['readyPlayers'] = dict(game.get('readyPlayers', {}))
            self._send({'ok': True, 'state': state, 'me': me, 'version': game.get('phaseVersion', 0)})
            return

        self._error('Not found', 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        body = self._read_body()

        # --- NİCK ---
        if path == '/api/nick':
            nick = (body.get('nickname', '') or '').strip()
            if not nick or len(nick) > 14:
                self._error('Nick 1-14 karakter')
                return
            uid = new_uid()
            nick_uids[nick] = uid
            self._send({'ok': True, 'uid': uid, 'nickname': nick})
            return

        # --- ODA OLUŞTUR ---
        if path == '/api/rooms':
            uid = body.get('uid', '')
            nick = body.get('nickname', '')
            if not uid or not nick:
                self._error('uid ve nickname gerekli')
                return
            # Zaten odada mı?
            for gid, g in list(games.items()):
                if uid in g.get('players', {}):
                    self._send({'ok': True, 'gameId': gid, 'alreadyIn': True})
                    return

            gid = new_uid()
            games[gid] = {
                'host': uid,
                'phase': 'waiting',
                'status': 'lobby',
                'phaseVersion': 1,
                'currentEbe': None,
                'targetPlayer': None,
                'selectedColor': None,
                'catchDeadline': 0,
                'colorDeadline': 0,
                'message': 'Oyuncular bekleniyor...',
                'eliminated': {},
                'winner': None,
                'colorHitOrder': {},
                'readyPlayers': {},
                'players': {
                    uid: {
                        'nickname': nick,
                        'lives': 3,
                        'isEbe': False,
                        'eliminated': False,
                        'joinedAt': time.time(),
                        'lastSeen': time.time(),
                    }
                },
                'createdAt': time.time(),
                'updatedAt': time.time(),
            }
            self._send({'ok': True, 'gameId': gid})
            return

        # --- ODAYA KATIL ---
        if path.startswith('/api/rooms/') and path.endswith('/join'):
            gid = path.split('/')[3]
            uid = body.get('uid', '')
            nick = body.get('nickname', '')
            game = games.get(gid)
            if not game:
                self._error('Oda bulunamadı', 404)
                return
            if len(game['players']) >= 4:
                self._error('Oda dolu (4/4)')
                return
            if uid in game['players']:
                # Zaten içinde
                self._send({'ok': True})
                return
            # Zaten başka odada mı?
            for ogid, og in list(games.items()):
                if uid in og.get('players', {}):
                    self._error('Zaten başka odadasın')
                    return

            game['players'][uid] = {
                'nickname': nick,
                'lives': 3,
                'isEbe': False,
                'eliminated': False,
                'joinedAt': time.time(),
                'lastSeen': time.time(),
            }
            game['readyPlayers'][uid] = False
            touch(game)
            game['phaseVersion'] += 1
            game['message'] = f'{nick} katıldı! ({len(game["players"])}/4)'
            self._send({'ok': True})
            broadcast_state(gid)
            return

        # --- ODA AYRIL ---
        if path.startswith('/api/rooms/') and path.endswith('/leave'):
            gid = path.split('/')[3]
            uid = body.get('uid', '')
            game = games.get(gid)
            if not game:
                self._send({'ok': True})  # zaten yok
                return
            game.get('readyPlayers', {}).pop(uid, None)
            remove_player(game, uid)
            self._send({'ok': True})
            broadcast_state(gid)
            return

        # --- READY TOGGLE ---
        if path.startswith('/api/rooms/') and path.endswith('/ready'):
            gid = path.split('/')[3]
            uid = body.get('uid', '')
            ready = bool(body.get('ready'))
            game = games.get(gid)
            if not game:
                self._error('Oda bulunamadı')
                return
            if game.get('status') != 'lobby':
                self._error('Sadece lobide hazır olunabilir')
                return
            player = game.get('players', {}).get(uid)
            if not player:
                self._error('Oyunda değilsin')
                return
            game.setdefault('readyPlayers', {})[uid] = ready
            game['message'] = f'{player["nickname"]} {"hazır" if ready else "hazır değil"}'
            game['phaseVersion'] += 1
            self._send({'ok': True, 'ready': ready})
            broadcast_state(gid)
            return

        # --- BOT EKLE / CIKAR ---
        if path.startswith('/api/rooms/') and path.endswith('/bots'):
            gid = path.split('/')[3]
            uid = body.get('uid', '')
            action = body.get('action', 'add')
            game = games.get(gid)
            if not game:
                self._error('Oda bulunamadı')
                return
            if game.get('host') != uid:
                self._error('Sadece ev sahibi bot ekleyebilir')
                return
            if action == 'add':
                buid, err = bot_add(game, uid)
                if err:
                    self._error(err)
                    return
                self._send({'ok': True, 'botUid': buid})
            else:
                # remove first bot
                gid_bots = bots.get(id(game), set())
                if not gid_bots:
                    self._error('Bot yok')
                    return
                bot_remove(game, next(iter(gid_bots)))
                self._send({'ok': True})
            broadcast_state(gid)
            return

        # --- OYUN BAŞLAT ---
        if path.startswith('/api/rooms/') and path.endswith('/start'):
            gid = path.split('/')[3]
            uid = body.get('uid', '')
            game = games.get(gid)
            if not game:
                self._error('Oda bulunamadı')
                return
            if game.get('host') != uid:
                self._error('Sadece ev sahibi başlatabilir')
                return
            pcount = len(game['players'])
            if pcount < 2:
                self._error('En az 2 oyuncu gerekli')
                return
            ready = game.get('readyPlayers', {})
            not_ready = [u for u in game['players'] if not ready.get(u)]
            if not_ready:
                names = ', '.join(game['players'][u]['nickname'] for u in not_ready)
                self._error(f'Hazır olmayan oyuncular: {names}')
                return

            uids = list(game['players'].keys())
            ebe_uid = random.choice(uids)
            touch(game)
            game['status'] = 'playing'
            game['phase'] = 'ebe_turn'
            game['currentEbe'] = ebe_uid
            game['targetPlayer'] = None
            game['selectedColor'] = None
            game['catchDeadline'] = 0
            game['colorDeadline'] = 0
            game['eliminated'] = {}
            game['winner'] = None
            game['colorHitOrder'] = {}
            for u in uids:
                game['players'][u]['lives'] = 3
                game['players'][u]['isEbe'] = (u == ebe_uid)
                game['players'][u]['eliminated'] = False
            game['message'] = f'👑 {game["players"][ebe_uid]["nickname"]} ebe oldu!'
            game['phaseVersion'] += 1
            self._send({'ok': True})
            broadcast_state(gid)
            return

        # --- TOP FIRLAT ---
        if path.startswith('/api/game/') and path.endswith('/throw'):
            parts = path.split('/')
            if len(parts) < 4:
                self._error('Bad path')
                return
            gid = parts[3]
            uid = body.get('uid', '')
            target = body.get('target', '')
            game = games.get(gid)
            if not game:
                self._error('Oyun bulunamadı')
                return
            if game.get('currentEbe') != uid:
                self._error('Sen ebe değilsin')
                return
            if target not in game['players'] or game['players'][target].get('eliminated'):
                self._error('Geçersiz hedef')
                return

            game['phase'] = 'catching'
            game['targetPlayer'] = target
            game['catchDeadline'] = time.time() * 1000 + 6000
            game['message'] = f'⚡ {game["players"][target]["nickname"]} yakalamaya çalışıyor!'
            game['phaseVersion'] += 1
            broadcast_state(gid)

            # 6 sn sonra timeout kontrol
            t = threading.Timer(6.5, check_catch_timeout, args=[gid])
            t.daemon = True
            t.start()
            self._send({'ok': True})
            return

        # --- TOP YAKALA ---
        if path.startswith('/api/game/') and path.endswith('/catch'):
            parts = path.split('/')
            if len(parts) < 4:
                self._error('Bad path')
                return
            gid = parts[3]
            uid = body.get('uid', '')
            game = games.get(gid)
            if not game:
                self._error('Oyun bulunamadı')
                return
            if game.get('phase') != 'catching':
                self._error('Yakalama fazı değil')
                return
            if game.get('targetPlayer') != uid:
                self._error('Sana top gelmedi')
                return
            if game['players'][uid].get('eliminated'):
                self._error('Elendin')
                return

            # Deadline geçmiş mi?
            if time.time() * 1000 > game.get('catchDeadline', 0):
                self._error('Süre doldu')
                return

            game['phase'] = 'ebe_turn'
            game['currentEbe'] = uid
            game['targetPlayer'] = None
            game['catchDeadline'] = 0
            # Diğerlerini ebe değil yap
            for u in game['players']:
                game['players'][u]['isEbe'] = (u == uid)
            game['message'] = f'🎯 {game["players"][uid]["nickname"]} topu yakaladı! Yeni ebe!'
            game['phaseVersion'] += 1
            self._send({'ok': True})
            broadcast_state(gid)
            return

        # --- İSTOP RENK SEÇ ---
        if path.startswith('/api/game/') and path.endswith('/color-pick'):
            parts = path.split('/')
            if len(parts) < 4:
                self._error('Bad path')
                return
            gid = parts[3]
            uid = body.get('uid', '')
            color = body.get('color', '')
            game = games.get(gid)
            if not game:
                self._error('Oyun bulunamadı')
                return
            if game.get('currentEbe') != uid:
                self._error('Sen ebe değilsin')
                return
            if color not in ('green', 'red'):
                self._error('Renk yeşil veya kırmızı olmalı')
                return
            if game.get('phase') != 'istop':
                self._error('İstop fazı bekleniyor')
                return

            game['phase'] = 'color_battle'
            game['selectedColor'] = color
            game['colorDeadline'] = time.time() * 1000 + 5000
            game['colorHitOrder'] = {}
            renk_adi = 'YEŞİL' if color == 'green' else 'KIRMIZI'
            game['message'] = f'🔍 {renk_adi} rengine dokun!'
            game['phaseVersion'] += 1
            broadcast_state(gid)

            t = threading.Timer(5.5, check_color_timeout, args=[gid])
            t.daemon = True
            t.start()
            self._send({'ok': True})
            return

        # --- RENK DOKUNMA ---
        if path.startswith('/api/game/') and path.endswith('/tap'):
            parts = path.split('/')
            if len(parts) < 4:
                self._error('Bad path')
                return
            gid = parts[3]
            uid = body.get('uid', '')
            game = games.get(gid)
            if not game:
                self._error('Oyun bulunamadı')
                return
            if game.get('phase') != 'color_battle':
                self._error('Renk savaşı fazı değil')
                return
            if game['players'][uid].get('eliminated'):
                self._error('Elendin')
                return
            if uid == game.get('currentEbe'):
                self._error('Ebe renge dokunamaz')
                return
            if uid in game['colorHitOrder']:
                self._error('Zaten dokundun')
                return

            game['colorHitOrder'][uid] = time.time() * 1000
            self._send({'ok': True, 'order': len(game['colorHitOrder'])})
            broadcast_state(gid)
            return

        # --- TEKRAR ---
        if path.startswith('/api/game/') and path.endswith('/rematch'):
            parts = path.split('/')
            if len(parts) < 4:
                self._error('Bad path')
                return
            gid = parts[3]
            uid = body.get('uid', '')
            game = games.get(gid)
            if not game:
                self._error('Oyun bulunamadı')
                return
            if game.get('host') != uid:
                self._error('Sadece ev sahibi')
                return

            game['status'] = 'lobby'
            game['phase'] = 'waiting'
            game['currentEbe'] = None
            game['targetPlayer'] = None
            game['selectedColor'] = None
            game['catchDeadline'] = 0
            game['colorDeadline'] = 0
            game['eliminated'] = {}
            game['winner'] = None
            game['colorHitOrder'] = {}
            game['message'] = 'Tekrar oyunu!'
            game['phaseVersion'] += 1
            broadcast_state(gid)
            for u in game['players']:
                game['players'][u]['lives'] = 3
                game['players'][u]['isEbe'] = False
                game['players'][u]['eliminated'] = False
            self._send({'ok': True})
            return

        self._error('Bilinmeyen yol', 404)


# ============================================================
# ZAMAN AŞIMI KONTROLLERİ
# ============================================================
def check_catch_timeout(gid):
    game = games.get(gid)
    if not game: return
    if game.get('phase') != 'catching': return
    if time.time() * 1000 >= game.get('catchDeadline', 0):
        game['phase'] = 'istop'
        game['catchDeadline'] = 0
        game['message'] = '💥 Top kaçtı! İstop!'
        game['phaseVersion'] += 1
        broadcast_state(gid)

def check_color_timeout(gid):
    game = games.get(gid)
    if not game: return
    if game.get('phase') != 'color_battle': return
    if time.time() * 1000 >= game.get('colorDeadline', 0):
        resolve_color_battle(gid)

def resolve_color_battle(gid):
    game = games.get(gid)
    if not game: return
    pls = game['players']
    elim = game.get('eliminated', {})
    ebe_id = game.get('currentEbe')
    hits = game.get('colorHitOrder', {})

    alive = [u for u in pls if u != ebe_id and not elim.get(u) and pls[u].get('lives', 0) > 0]
    if not alive:
        reset_after_battle(game, gid)
        return

    touched = list(hits.keys())
    untouched = [u for u in alive if u not in touched]

    loser = None
    if untouched:
        loser = untouched[0]
    elif len(touched) > 0:
        # En son dokunan
        last = max(touched, key=lambda u: hits.get(u, 0))
        loser = last
    else:
        # Hiç kimse dokunmadı - ebe seçer
        loser = random.choice(alive)

    if loser is None:
        reset_after_battle(game, gid)
        return

    new_lives = max(0, pls[loser].get('lives', 1) - 1)
    pls[loser]['lives'] = new_lives
    nick_loser = pls[loser]['nickname']

    if new_lives <= 0:
        pls[loser]['eliminated'] = True
        elim[loser] = True
        game['message'] = f'☠️ {nick_loser} elendi!'
    else:
        game['message'] = f'💔 {nick_loser} vuruldu! ({"❤️" * new_lives})'

    # Kalan canlı oyuncu sayısı (ebe hariç)
    remaining = [u for u in alive if u != loser and not elim.get(u)]
    if len(remaining) <= 0:
        # Ebe + varsa kalanlar
        all_alive = [u for u in pls if not elim.get(u) and pls[u].get('lives', 0) > 0]
        if len(all_alive) <= 1:
            # Tek kalan kazandı
            winner = all_alive[0] if all_alive else ebe_id
            game['phase'] = 'result'
            game['status'] = 'finished'
            game['winner'] = winner
            game['message'] = f'🏆 {pls[winner]["nickname"]} oyunu KAZANDI!'
            game['phaseVersion'] += 1
            broadcast_state(gid)
            return

    # Yeni ebe seç
    candidates = [u for u in alive if u != loser and not elim.get(u)]
    if not candidates:
        candidates = [u for u in pls if not elim.get(u) and pls[u].get('lives', 0) > 0]
    if not candidates:
        candidates = [list(pls.keys())[0]]

    new_ebe = random.choice(candidates)
    game['currentEbe'] = new_ebe
    game['phase'] = 'ebe_turn'
    game['targetPlayer'] = None
    game['selectedColor'] = None
    game['catchDeadline'] = 0
    game['colorDeadline'] = 0
    game['colorHitOrder'] = {}
    for u in pls:
        pls[u]['isEbe'] = (u == new_ebe)
    if new_ebe in pls:
        game['message'] = f'👑 {pls[new_ebe]["nickname"]} yeni ebe!'
    game['phaseVersion'] += 1
    broadcast_state(gid)

def reset_after_battle(game, gid):
    candidates = [u for u in game['players'] if not game['players'][u].get('eliminated') and game['players'][u].get('lives', 0) > 0]
    if not candidates:
        candidates = list(game['players'].keys())
    ne = random.choice(candidates)
    game['currentEbe'] = ne
    game['phase'] = 'ebe_turn'
    game['targetPlayer'] = None
    game['selectedColor'] = None
    game['catchDeadline'] = 0
    game['colorDeadline'] = 0
    game['colorHitOrder'] = {}
    for u in game['players']:
        game['players'][u]['isEbe'] = (u == ne)
    game['message'] = f'👑 {game["players"][ne]["nickname"]} yeni ebe!'
    game['phaseVersion'] += 1
    broadcast_state(gid)


# ============================================================
# İNDEX.HTML (gömülü)
# ============================================================
def get_index_html():
    return INDEX_HTML.replace('__BUILD__', APP_VERSION)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<meta name="theme-color" content="#0a0a14">
<title>İstop</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@700&family=Fredoka:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--felt:#3b0d16;--felt2:#571622;--gold:#d4af37;--gold2:#e6c15c;--gold3:#a8842a;--cream:#f7efe0;--text:#f3ead8;--mute:rgba(247,239,224,0.6);--green:#2fbf71;--red:#e0433f}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;width:100%;overflow:hidden;font-family:'Fredoka','Segoe UI',system-ui,sans-serif;background:#20060b;color:var(--text);touch-action:manipulation;-webkit-tap-highlight-color:transparent;user-select:none}
button{font:inherit;border:none;cursor:pointer;outline:none;-webkit-tap-highlight-color:transparent;touch-action:manipulation}
input{font:inherit;outline:none}
#app{display:flex;flex-direction:column;height:100vh;max-width:480px;margin:0 auto;position:relative;overflow:hidden;background-color:var(--felt);background-image:radial-gradient(130% 95% at 50% 0%,rgba(255,255,255,0.07) 0%,rgba(0,0,0,0) 42%),radial-gradient(150% 120% at 50% 108%,rgba(0,0,0,0.6) 0%,rgba(0,0,0,0) 55%),repeating-linear-gradient(45deg,rgba(0,0,0,0.05) 0 2px,rgba(0,0,0,0) 2px 6px),repeating-linear-gradient(-45deg,rgba(255,255,255,0.015) 0 2px,rgba(255,255,255,0) 2px 6px);box-shadow:inset 0 0 120px rgba(0,0,0,0.5)}
#player-bar{display:flex;flex-wrap:wrap;gap:4px;padding:6px;background:rgba(20,4,8,0.55);backdrop-filter:blur(4px);flex-shrink:0;justify-content:space-around;z-index:2;border-bottom:1px solid rgba(212,175,55,0.25)}
.p-card{flex:1 1 22%;display:flex;flex-direction:column;align-items:center;gap:2px;background:linear-gradient(180deg,rgba(255,255,255,0.09),rgba(255,255,255,0.02));border:1px solid rgba(212,175,55,0.4);border-radius:10px;padding:5px 2px;font-size:0.7rem;line-height:1.1;position:relative;transition:all 0.3s;box-shadow:0 2px 8px rgba(0,0,0,0.35)}
.p-card.you{border-color:var(--gold2);box-shadow:0 0 14px rgba(230,193,92,0.45)}
.p-card.eliminated{opacity:0.28;filter:grayscale(1);border-color:rgba(255,255,255,0.12);box-shadow:none}
.p-card .av{font-size:1.7rem}
.p-card .nm{font-size:0.58rem;color:#e8dcc4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.p-card .lv{font-size:0.6rem;letter-spacing:-1px;filter:drop-shadow(0 0 4px rgba(224,67,63,0.6))}
.p-card .badge{position:absolute;top:-5px;right:-5px;background:linear-gradient(135deg,#e6c15c,#a8842a);color:#2a1a05;font-size:0.6rem;padding:2px 6px;border-radius:7px;font-weight:800;box-shadow:0 0 12px rgba(212,175,55,0.7);animation:crownGlow 1.2s ease-in-out infinite alternate}
@keyframes crownGlow{from{box-shadow:0 0 7px rgba(212,175,55,0.5)}to{box-shadow:0 0 20px rgba(212,175,55,0.95)}}
#action{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:10px 16px;gap:12px;min-height:150px;z-index:2}
#msg{font-size:1.15rem;font-weight:600;text-align:center;padding:8px 14px;border-radius:12px;background:rgba(0,0,0,0.28);border:1px solid rgba(212,175,55,0.25);max-width:100%}
.btn{display:block;width:100%;padding:15px;border-radius:10px;font-size:1.18rem;font-weight:700;letter-spacing:0.5px;background:linear-gradient(180deg,rgba(0,0,0,0.25),rgba(0,0,0,0.35));color:var(--text);border:1px solid rgba(212,175,55,0.35);transition:transform 0.1s,box-shadow 0.1s;box-shadow:0 4px 12px rgba(0,0,0,0.4)}
.btn:active{transform:scale(0.96)}
.btn-green{background:linear-gradient(135deg,#3fdc88,#1f9d57);color:#04160c;border:none;box-shadow:0 4px 14px rgba(31,157,87,0.5)}
.btn-red{background:linear-gradient(135deg,#f06a5f,#c92c26);color:#fff;border:none;box-shadow:0 4px 14px rgba(201,44,38,0.5)}
.btn-gold{background:linear-gradient(180deg,#f0d489 0%,#d4af37 45%,#b8922b 100%);color:#2a1a05;border:1px solid #f7e9b8;box-shadow:0 4px 14px rgba(212,175,55,0.45),inset 0 1px 0 rgba(255,255,255,0.5)}
.btn-invite{background:rgba(0,0,0,0.3);color:#f3ead8;border:1px solid rgba(212,175,55,0.4)}
.btn-small{font-size:0.85rem;padding:10px 18px}
.btn:disabled{opacity:0.4;pointer-events:none;box-shadow:none}
.btn-circle{width:130px;height:130px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:1.2rem}
#status-bar{flex-shrink:0;padding:5px 8px;font-size:0.7rem;color:var(--mute);text-align:center;background:rgba(20,4,8,0.5);min-height:18px;z-index:2;border-top:1px solid rgba(212,175,55,0.18)}
#game-exit-btn{position:absolute;right:10px;bottom:46px;min-width:68px;height:32px;padding:0 14px;border-radius:10px;border:1px solid rgba(212,175,55,0.45);background:rgba(0,0,0,0.5);color:#f3ead8;font-size:0.72rem;line-height:1;z-index:20;cursor:pointer;font-family:inherit}
#game-exit-btn:active{background:rgba(201,44,38,0.85)}
.screen{position:absolute;inset:0;z-index:5;display:none;flex-direction:column;align-items:center;justify-content:center;gap:14px;padding:20px 16px}
#screen-nick{display:flex}
#screen-nick h1{font-family:'Cinzel','Fredoka',serif;font-size:2.7rem;font-weight:700;letter-spacing:4px;background:linear-gradient(180deg,#f9eec4 0%,#e6c15c 35%,#a8842a 58%,#e6c15c 78%,#f9eec4 100%);-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 2px 6px rgba(0,0,0,0.6))}
#screen-nick .sub{opacity:0.6;font-size:0.85rem;letter-spacing:1px}
#nick-input{width:80%;max-width:270px;padding:14px 16px;border-radius:10px;border:1px solid rgba(212,175,55,0.45);font-size:1.1rem;text-align:center;background:rgba(0,0,0,0.3);color:var(--cream);box-shadow:inset 0 2px 6px rgba(0,0,0,0.35)}
#nick-input:focus{border-color:var(--gold2);box-shadow:0 0 0 2px rgba(212,175,55,0.25),inset 0 2px 6px rgba(0,0,0,0.35)}
.room-list{display:flex;flex-direction:column;gap:6px;max-height:180px;overflow-y:auto;width:100%}
.room-item{display:flex;align-items:center;gap:8px;padding:9px 12px;border-radius:10px;background:rgba(0,0,0,0.28);border:1px solid rgba(212,175,55,0.22)}
.room-item .ri{flex:1;font-size:0.9rem;font-weight:600;color:var(--cream)}
.room-item .ri small{opacity:0.55;font-size:0.72rem}
.room-item .join-btn{background:linear-gradient(180deg,#f0d489,#d4af37);padding:8px 18px;border-radius:999px;font-size:0.82rem;font-weight:700;color:#2a1a05;box-shadow:0 2px 8px rgba(212,175,55,0.35)}
.room-item .join-btn:disabled{background:#3a2a2a;color:#998;opacity:0.5;box-shadow:none}
#room-header{font-size:1.25rem;font-weight:700;text-align:center;color:var(--cream)}
#room-hint{font-size:0.72rem;opacity:0.72}
.player-list{display:flex;flex-direction:column;gap:5px;width:100%;max-width:320px}
.pl-item{display:flex;align-items:center;gap:8px;padding:7px 12px;border-radius:10px;background:rgba(0,0,0,0.28);border:1px solid rgba(212,175,55,0.2)}
.pl-item .pln{flex:1;font-weight:600;font-size:0.9rem;color:var(--cream)}
#invite-msg{font-size:0.7rem;opacity:0.85;text-align:center}
#catch-btn{width:150px;height:150px;border-radius:50%;font-size:1.3rem;font-weight:800;background:radial-gradient(circle at 40% 36%,#f0d489,#b8922b);color:#2a1a05;display:flex;align-items:center;justify-content:center;border:2px solid #f7e9b8;box-shadow:0 0 30px rgba(212,175,55,0.55);animation:pulse-catch 0.7s infinite}
@keyframes pulse-catch{0%,100%{box-shadow:0 0 16px rgba(212,175,55,0.3);transform:scale(1)}50%{box-shadow:0 0 42px rgba(212,175,55,0.65);transform:scale(1.06)}}
#cd{font-size:2.4rem;font-weight:800;color:var(--gold2);text-shadow:0 0 14px rgba(212,175,55,0.7);font-family:'Cinzel','Fredoka',serif}
#istop-btn{font-size:1.9rem;font-weight:900;padding:18px 44px;border-radius:12px;background:linear-gradient(135deg,#f06a5f,#c92c26);color:#fff;box-shadow:0 0 30px rgba(201,44,38,0.55);animation:pulse-istop 0.6s infinite}
@keyframes pulse-istop{0%,100%{box-shadow:0 0 16px rgba(201,44,38,0.3);transform:scale(1)}50%{box-shadow:0 0 46px rgba(201,44,38,0.7);transform:scale(1.04)}}
#color-choice{display:flex;gap:16px;justify-content:center}
#color-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;width:100%;max-width:300px;margin:0 auto}
.color-cell{aspect-ratio:1;border-radius:50%;transition:all 0.08s;box-shadow:0 2px 8px rgba(0,0,0,0.45),inset 0 -2px 6px rgba(0,0,0,0.25)}
.color-cell:active{transform:scale(0.85)}
.color-cell.hit{opacity:0.3;transform:scale(0.4);pointer-events:none}
.color-cell.wrong{animation:sh 0.25s}
@keyframes sh{0%,100%{transform:translateX(0)}20%{transform:translateX(-6px)}60%{transform:translateX(6px)}}
#screen-result{z-index:20;position:absolute;inset:0;display:none;flex-direction:column;align-items:center;justify-content:center;gap:14px;padding:20px;background:rgba(20,4,8,0.78);backdrop-filter:blur(5px)}
#screen-result .title{font-family:'Cinzel','Fredoka',serif;font-size:2.2rem;font-weight:700;background:linear-gradient(180deg,#f9eec4,#e6c15c,#a8842a);-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 2px 8px rgba(0,0,0,0.7))}
#screen-result .sub{font-size:1rem;opacity:0.88;font-weight:600;color:var(--cream)}
.hidden{display:none!important}
.lbl{font-size:0.85rem;opacity:0.75;margin-bottom:4px;font-weight:500;color:var(--cream)}
.confetti{position:absolute;top:-20px;width:10px;height:16px;border-radius:2px;pointer-events:none;z-index:40;animation:confFall linear forwards}
@keyframes confFall{0%{transform:translateY(0) rotate(0deg);opacity:1}100%{transform:translateY(105vh) rotate(720deg);opacity:0}}
.flash{animation:screenFlash 0.5s ease-out}
@keyframes screenFlash{0%{box-shadow:inset 0 0 0 9999px rgba(224,67,63,0.35)}100%{box-shadow:inset 0 0 0 9999px rgba(224,67,63,0)}}
</style>
</head>
<body>
<div id="app">

<div id="screen-nick" class="screen">
<h1>İSTOP</h1>
<div class="sub">Çok oyunculu - internet</div>
<input id="nick-input" type="text" maxlength="14" placeholder="Takma adın" autocomplete="off" enterkeyhint="done">
<button class="btn btn-gold" id="nick-btn" style="width:80%;max-width:240px;">GİR</button>
<div style="font-size:0.6rem;opacity:0.45;margin-top:4px;">Arkadaşlarınla her yerden oyna</div>
</div>

<div id="screen-lobby" class="screen">
<div class="lbl" style="margin-top:-8px;">── ODALAR ──</div>
<div class="room-list" id="room-list"><div style="opacity:0.5;text-align:center;padding:10px;">Yükleniyor...</div></div>
<button class="btn btn-gold" id="create-btn">➕ Yeni Oda</button>
<button class="btn btn-small" id="logout-btn" style="background:rgba(0,0,0,0.35);border:1px solid rgba(212,175,55,0.4);color:#f3ead8;">Çıkış</button>
</div>

<div id="screen-room" class="screen">
<div id="room-header">Oda</div>
<div id="room-hint" style="font-size:0.72rem;opacity:0.65;margin:-4px 0 6px 0;text-align:center;"></div>
<div class="player-list" id="player-list"></div>
<button class="btn btn-invite" id="invite-btn" style="font-size:0.7rem;padding:8px 14px;">🔗 Davet linkini kopyala</button>
<div id="invite-msg" style="display:none;font-size:0.68rem;opacity:0.7;margin-top:4px;text-align:center;"></div>
<button class="btn btn-gold" id="start-btn" style="display:none;">🎮 Oyunu Başlat</button>
<button class="btn btn-small" id="bot-btn" style="display:none;background:rgba(0,0,0,0.35);border:1px solid rgba(212,175,55,0.4);color:#f3ead8;margin-top:6px;">🤖 Bot Ekle</button>
<button class="btn btn-small" id="leave-btn" style="background:rgba(0,0,0,0.35);border:1px solid rgba(212,175,55,0.4);color:#f3ead8;margin-top:6px;">Odadan Ayrıl</button>
</div>

<div id="screen-game" style="display:none;flex-direction:column;height:100%;width:100%;position:relative;">
<div id="player-bar"></div>
<div id="action"><div id="msg">Oyun başlıyor...</div><div id="ac"></div></div>
<div id="status-bar">📍</div>
<button id="game-exit-btn" title="Oyundan çık" aria-label="Oyundan çık">Çıkış</button>
</div>

<div id="screen-result">
<div class="title">🏆</div>
<div class="sub"></div>
<button class="btn btn-gold" id="rematch-btn" style="width:80%;max-width:240px;">Tekrar Oyna</button>
<button class="btn btn-small" id="result-leave-btn" style="background:rgba(0,0,0,0.35);border:1px solid rgba(212,175,55,0.4);color:#f3ead8;max-width:240px;">Odadan Ayrıl</button>
</div>

</div>
<script>
// ========== DURUM ==========
const BUILD='__BUILD__';
console.log('ISTOP build',BUILD);
let uid=null,nick='',gameId=null,isHost=false,ebem=false,eliminated=false,lastVer=0;
let curPhase=null,timers=[],myHit=false,stateCache={},pollTimer=null,pendingInvite=null,prevLives={};
function clrT(){timers.forEach(t=>clearTimeout(t));timers=[]}
function sc(id){['screen-nick','screen-lobby','screen-room','screen-game','screen-result'].forEach(s=>{const e=document.getElementById(s);if(e)e.style.display=s===id?'flex':'none'})}
function msg(t){const e=document.getElementById('msg');if(e)e.textContent=t}
function st(t){const e=document.getElementById('status-bar');if(e)e.textContent='📍 '+t}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
const AV='😊😎🤩😇🥳🤗😺🦊🐶🐱🐼🦁🐸🐧🦄🐲'.split('');
function av(u){let h=0;for(let i=0;i<(u||'x').length;i++)h=(h*31+u.charCodeAt(i))&65535;return AV[h%AV.length]}
function vib(){try{if(navigator.vibrate)navigator.vibrate([180,90,180])}catch(e){}}
function ntf(t,b){try{if('Notification'in window&&Notification.permission==='granted')new Notification(t,{body:b});else if('Notification'in window&&Notification.permission!=='denied')Notification.requestPermission().then(p=>{if(p==='granted')new Notification(t,{body:b})})}catch(e){}}

// ========== SES ==========
const Snd={ctx:null,oscs:[],
i(){try{if(!this.ctx||this.ctx.state==='closed')this.ctx=new(window.AudioContext||window.webkitAudioContext)();if(this.ctx.state==='suspended')this.ctx.resume()}catch(e){}},
stopAll(){try{this.oscs.forEach(o=>{try{o.stop()}catch(e){}})}catch(e){}this.oscs=[]},
p(f,d,t,v=0.3){if(!this.ctx)return;try{const o=this.ctx.createOscillator(),a=this.ctx.createGain();o.type=t||'sine';o.frequency.value=f;a.gain.value=v;o.connect(a);a.connect(this.ctx.destination);o.start();this.oscs.push(o);setTimeout(()=>{try{o.stop();a.disconnect()}catch(e){};const k=this.oscs.indexOf(o);if(k>=0)this.oscs.splice(k,1)},d)}catch(e){}},bip(){this.p(880,100,'sine',0.32)},alert(){this.p(660,70,'sine',0.28);setTimeout(()=>this.p(990,100,'sine',0.3),80)},send(){this.p(440,40,'square',0.16);setTimeout(()=>this.p(660,60,'sine',0.2),40)},caught(){this.p(880,50,'sine',0.26);setTimeout(()=>this.p(1100,120,'sine',0.2),50)},istop(){this.p(300,160,'sawtooth',0.2);setTimeout(()=>this.p(200,260,'sawtooth',0.16),160)},hit(){this.p(200,220,'square',0.16)},win(){[660,880,1100,1320].forEach((f,i)=>setTimeout(()=>this.p(f,110,'sine',0.26),i*80))},lose(){this.p(300,250,'sine',0.2);setTimeout(()=>this.p(200,320,'sine',0.16),230)}};

// ========== API ==========
const API=window.location.origin;
function api(method,path,body){
  const opts={method,headers:{'Content-Type':'application/json'}};
  if(body)opts.body=JSON.stringify(body);
  return fetch(API+path,opts).then(r=>r.json());
}

// ========== OTURUM HAFIZASI (yenileme / kilit sonrasi geri donus) ==========
const LS_KEY='istop_session_v1';
let roomsTimer=null;
function saveSession(){try{localStorage.setItem(LS_KEY,JSON.stringify({uid,nick,gameId,isHost:!!isHost}))}catch(e){}}
function clearSession(){try{localStorage.removeItem(LS_KEY)}catch(e){}}
function loadSession(){try{const s=JSON.parse(localStorage.getItem(LS_KEY)||'null');if(s&&s.uid&&s.nick)return s}catch(e){}return null}
function startLobbyPoll(){
  if(roomsTimer)clearInterval(roomsTimer);
  pollRooms();
  roomsTimer=setInterval(pollRooms,2000);
}
function stopLobbyPoll(){if(roomsTimer){clearInterval(roomsTimer);roomsTimer=null}}
function goLobby(){
  stopLobbyPoll();
  gameId=null;isHost=false;
  sc('screen-lobby');
  startLobbyPoll();
}

// ========== OYUNCU KARTLARI ==========
function cards(data){
  if(!data||!data.players)return;
  const bar=document.getElementById('player-bar'),pls=data.players,ebe=data.currentEbe,elim=data.eliminated||{};
  let h='';
  Object.keys(pls).forEach(u=>{
    const p=pls[u];if(!p)return;
    const me=u===uid,isE=u===ebe,el=!!elim[u]||(p.lives||0)<=0;
    const ht=el?'💀':'❤️'.repeat(Math.max(0,p.lives||0));
    const badge=isE?'<div class="badge">👑</div>':'';
    h+=`<div class="p-card ${el?'eliminated':''} ${me?'you':''}">${badge}<div class="av">${av(u)}</div><div class="nm">${esc(p.nickname)}</div><div class="lv">${ht}</div></div>`;
  });
  bar.innerHTML=h;
}

// ========== LOBI POLL ==========
function pollRooms(){
  api('GET','/api/rooms').then(r=>{
    if(!r.ok)return;
    const list=document.getElementById('room-list');
    if(!r.rooms||r.rooms.length===0){
      list.innerHTML='<div style="opacity:0.5;text-align:center;padding:10px;">Henüz oda yok</div>';
      return;
    }
    let h='';
    r.rooms.forEach(e=>{
      const ok=!e.full&&e.playerCount<4;
      h+=`<div class="room-item"><div class="ri">Oda #${e.id.substring(0,6)} <small>(${e.playerCount}/4)</small></div><button class="join-btn" data-id="${e._id}" ${ok?'':'disabled'}>${ok?'Katıl':'DOLU'}</button></div>`;
    });
    list.innerHTML=h;
    Array.from(document.querySelectorAll('.join-btn')).forEach(b=>b.addEventListener('click',()=>joinRoom(b.dataset.id)));
  }).catch(()=>{});
}

// ========== ODA İŞLEMLERİ ==========
function createRoom(){
  api('POST','/api/rooms',{uid,nickname:nick}).then(r=>{
    if(!r.ok){alert(r.error||'Oda olusturulamadi');return}
    if(r.alreadyIn){
      // Eski bir odada kaydimiz kalmis: cikip temiz oda kur
      console.log('Eski odada kalinti var, cikiliyor:',r.gameId);
      api('POST','/api/rooms/'+r.gameId+'/leave',{uid}).then(()=>{
        api('POST','/api/rooms',{uid,nickname:nick}).then(r2=>{
          if(r2.ok&&r2.gameId){gameId=r2.gameId;isHost=true;enterRoom();}
          else alert(r2.error||'Oda olusturulamadi');
        }).catch(e=>alert('Baglanti hatasi'));
      }).catch(()=>{gameId=r.gameId;enterRoom();});
      return;
    }
    gameId=r.gameId;
    isHost=true;
    console.log('Oda olusturuldu, isHost=true, gameId='+gameId+' build='+BUILD);
    enterRoom();
  }).catch(e=>{alert('Baglanti hatasi');console.error(e)});
}
function joinRoom(gid){
  api('POST','/api/rooms/'+gid+'/join',{uid,nickname:nick}).then(r=>{
    if(r.ok){gameId=gid;isHost=false;enterRoom()}
    else alert(r.error);
  });
}
// Davet linki ile otomatik katilim
function joinViaInvite(){
  if(!pendingInvite||!uid)return;
  const gid=pendingInvite;pendingInvite=null;
  const doJoin=()=>api('POST','/api/rooms/'+gid+'/join',{uid,nickname:nick}).then(r=>{
    if(r.ok){gameId=gid;isHost=false;enterRoom()}
    else alert(r.error||'Odaya katılınamadı');
  }).catch(()=>alert('Bağlantı hatası'));
  if(gameId){
    api('POST','/api/rooms/'+gameId+'/leave',{uid}).then(doJoin).catch(doJoin);
  }else doJoin();
}
function enterRoom(){
  sc('screen-room');
  stopLobbyPoll();
  document.getElementById('room-header').textContent='Oda #'+gameId.substring(0,8);
  saveSession();
  loadPlayers();
  connectWs(); // WS dene
  pollGame(); // oda içinde state poll
}

// ========== DAVET LINKI ==========
function inviteLink(){return API+'/#oda='+gameId}
function copyInvite(){
  if(!gameId)return;
  const link=inviteLink();
  const done=()=>{const m=document.getElementById('invite-msg');if(m){m.style.display='block';m.textContent='Link kopyalandı! Yapıştırıp arkadaşına gönder ✔'}};
  try{
    if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(link).then(done).catch(fallback)}else fallback();
  }catch(e){fallback()}
  function fallback(){
    try{const ta=document.createElement('textarea');ta.value=link;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);done()}catch(e){prompt('Linki kopyala:',link)}
  }
}
// ========== READY ==========
function toggleReady(){
  if(!gameId||!uid)return;
  const cur=!!((stateCache||{}).readyPlayers||{})[uid];
  api('POST','/api/rooms/'+gameId+'/ready',{uid,ready:!cur}).then(r=>{
    if(!r.ok)return;
  }).catch(()=>{});
}
// Lobide ekrani state'ten ciz (her poll'da cagrilir)
function renderRoom(g){
  if(!g||!g.players)return;
  isHost = (g.host === uid);
  const list=document.getElementById('player-list');
  const cnt=Object.keys(g.players).length;
  const readyMap=g.readyPlayers||{};
  let readyCount=0;
  let h='';
  Object.keys(g.players).forEach(u=>{
    const p=g.players[u],me=u===uid,host=u===g.host,isBot=!!p.isBot;
    const isReady=!!readyMap[u];
    if(isReady)readyCount++;
    const badge=host?'<span>👑</span>':'';
    const botBadge=isBot?'<span>🤖</span>':'';
    const readyBadge=isReady?'<span style="color:#2fbf71;font-weight:700;">✔ Hazır</span>':'<span style="opacity:0.55;">⏳</span>';
    const myBtn=me&&!host?' <button class="btn btn-small" style="padding:6px 12px;margin-left:6px;" onclick="toggleReady()">'+(isReady?'Hazır Değil':'Hazır')+'</button>':'';
    h+=`<div class="pl-item"><span>${badge}${botBadge}</span><span class="pln">${esc(p.nickname)}${me?' (Sen)':''}${host?' - kurucu':''}</span>${readyBadge}${myBtn}</div>`;
  });
  list.innerHTML=h;
  const sb=document.getElementById('start-btn');
  const bb=document.getElementById('bot-btn');
  if(isHost){
    const allReady = cnt>=2 && readyCount===cnt;
    sb.style.display='block';
    sb.disabled = !allReady;
    sb.textContent = allReady ? '🎮 Oyunu Başlat' : `Hazırlanıyor... (${readyCount}/${cnt})`;
    const botCount = Object.values(g.players||{}).filter(p=>p.isBot).length;
    bb.style.display='block';
    bb.textContent = botCount >= 2 ? '🤖 Botlar Dolu' : '🤖 Bot Ekle';
    bb.disabled = botCount >= 2;
  }else{
    sb.style.display='none';
    bb.style.display='none';
  }
  const hd=document.getElementById('room-hint');
  if(hd){
    const botCount = Object.values(g.players||{}).filter(p=>p.isBot).length;
    hd.textContent = isHost
      ? (cnt>=2 ? 'Hazırlık kontrolü 👑' : 'Sen kurucusun 👑 Arkadaşını bekle (2 kişi gerekli)') + (botCount ? ` (${botCount} bot)` : '')
      : 'Kurucunun başlatması bekleniyor...';
  }
  st('Oyuncu: '+cnt+'/4');
}
function loadPlayers(){
  api('GET','/api/game/'+gameId+'/state?uid='+uid+'&since='+lastVer).then(r=>{
    if(!r.ok||!r.state)return;
    renderRoom(r.state);
  }).catch(e=>console.warn('loadPlayers err',e));
}

// ========== OYUN POLL ==========
function applyGameState(g){
  if(!g)return;
  if(g.host){isHost = (g.host === uid);}
  stateCache=g;
  if(g.status==='deleted'){leaveRoom();return}
  if(!g.players||!g.players[uid]){leaveRoom();return}
  if(g.phaseVersion>lastVer){lastVer=g.phaseVersion;clrT();myHit=false;}
  const pl=g.players||{},prev=prevLives;
  let lost=false;
  Object.keys(pl).forEach(u=>{const nl=pl[u]&&pl[u].lives||0;if(!prev.hasOwnProperty(u)){prev[u]=nl} else if(nl<prev[u]){lost=true;prev[u]=nl}});
  if(lost)flashRed();
  cards(g);
  if(g.status==='playing'&&!curPhase){curPhase='live';sc('screen-game');Snd.i();enterGame(g);}
  else if(g.status==='playing'&&curPhase){gameTick(g);}
  else if(g.status==='finished'){showResult(g);}
  else if(g.status==='lobby'){curPhase=null;renderRoom(g);}
  if(g.message)msg(g.message);
}
function pollGame(){
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(()=>{
    if(!gameId||!uid)return;
    api('GET','/api/game/'+gameId+'/state?uid='+uid+'&since='+lastVer).then(r=>{
      if(!r.ok||!r.state)return;
      applyGameState(r.state);
    }).catch(e=>{console.warn('poll err',e)});
  },250);
}

// ========== WEBSOCKET ==========
let ws=null,wsMode=false;
const WS_PORT_OFFSET=1;
function getWsUrl(){
  const loc=window.location;
  const host=loc.hostname;
  const port=parseInt(loc.port||(loc.protocol==='https:'?'443':'80'));
  const wsPort=port+WS_PORT_OFFSET;
  const proto=loc.protocol==='https:'?'wss':'ws';
  return `${proto}://${host}:${wsPort}/`;
}
function connectWs(){
  if(wsMode)return;
  const url=getWsUrl();
  try{
    ws=new WebSocket(url);
    const timer=setTimeout(()=>{if(ws&&ws.readyState!==WebSocket.OPEN){ws.close();ws=null;}},2000);
    ws.onopen=()=>{
      clearTimeout(timer);
      wsMode=true;
      console.log('WS baglandi');
      if(pollTimer){clearInterval(pollTimer);pollTimer=null}
      stopLobbyPoll();
      if(gameId&&uid)ws.send(JSON.stringify({type:'subscribe',gameId:gameId,uid:uid}));
    };
    ws.onmessage=(e)=>{
      try{
        const msg=JSON.parse(e.data);
        if(msg.type==='state'&&msg.data&&msg.data.state)applyGameState(msg.data.state);
      }catch(err){}
    };
    ws.onclose=()=>{ws=null;wsMode=false;if(gameId)pollGame();else startLobbyPoll();};
    ws.onerror=()=>{if(ws)ws.close();};
  }catch(e){}
}
function disconnectWs(){if(ws){try{ws.close();}catch(e){}ws=null;}wsMode=false;}

let lastPhaseSig='';
function enterGame(g){prevLives={};cards(g);gameTick(g);}
function gameTick(g){
  const pls=g.players,eli=g.eliminated||{},me=pls[uid];
  eliminated=!!eli[uid]||!me||(me.lives||0)<=0;
  ebem=g.currentEbe===uid;
  const ac=document.getElementById('ac');
  const sig=(g.phase||'')+'#'+(g.phaseVersion||0)+'#'+(g.selectedColor||'')+'#'+(g.currentEbe||'');
  const fresh=(sig!==lastPhaseSig);
  if(fresh)lastPhaseSig=sig;
  if(eliminated){
    if(fresh)ac.innerHTML='<div style="opacity:0.5;font-size:1rem;">☠️ Elendin. İzliyorsun...</div>';
    return;
  }
  switch(g.phase){
    case'ebe_turn':phEbe(g,ac,fresh);break;
    case'catching':phCatch(g,ac,fresh);break;
    case'istop':phIstop(g,ac,fresh);break;
    case'color_battle':phColor(g,ac,fresh);break;
  }
}
function phEbe(g,ac,fresh){
  if(!fresh)return;
  if(ebem){
    const others=Object.keys(g.players).filter(u=>u!==uid&&!g.eliminated[u]&&(g.players[u].lives||0)>0);
    if(others.length===0){api('POST','/api/game/'+gameId+'/rematch',{uid});return}
    let h='<div class="lbl">Kime atıyorsun?</div><div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:center;">';
    others.forEach(u=>{h+=`<button class="btn btn-small" style="min-width:80px;padding:10px 16px;border-radius:10px;background:linear-gradient(180deg,#f0d489,#d4af37);color:#2a1a05;border:none;" onclick="throwTo('${u}')">${esc(g.players[u].nickname)}</button>`});
    h+='</div>';ac.innerHTML=h;
  }else{ac.innerHTML='<div style="opacity:0.5;font-size:1rem;">🔄 Ebe bekleniyor...</div>'}
}
function throwTo(tu){
  Snd.send();
  api('POST','/api/game/'+gameId+'/throw',{uid,target:tu}).catch(()=>{});
}
function phCatch(g,ac,fresh){
  if(g.targetPlayer===uid&&!eliminated){
    if(!fresh)return;
    const rem=Math.max(0,Math.floor((g.catchDeadline-Date.now())/1000));
    ac.innerHTML=`<div id="cd">${rem}</div><button id="catch-btn" onclick="doCatch()">🏀 YAKALA!</button>`;
    Snd.alert();vib();ntf('İstop','Sana top geldi!');
    if(g.catchDeadline>Date.now()){
      const iv=setInterval(()=>{const r=Math.max(0,Math.floor((g.catchDeadline-Date.now())/1000));const el=document.getElementById('cd');if(el)el.textContent=String(r);if(r<=0)clearInterval(iv);},150);
      timers.push(iv);timers.push(setTimeout(()=>clearInterval(iv),5000));
    }
  }else if(fresh){ac.innerHTML='<div style="opacity:0.5;font-size:1rem;">⏳ Top yolda...</div>'}
}
function doCatch(){if(!gameId)return;Snd.caught();vib();api('POST','/api/game/'+gameId+'/catch',{uid}).catch(()=>{});}
function phIstop(g,ac,fresh){
  if(!fresh)return;
  ac.innerHTML=ebem?`<button id="istop-btn" onclick="pickColor()">🛑 İSTOP!</button>`:'<div style="opacity:0.5;font-size:1.2rem;">🛑 İSTOP!</div>';
  Snd.istop();vib();ntf('İstop','Top kaçtı!');
}
function pickColor(){
  document.getElementById('ac').innerHTML=`<div class="lbl">Rengi seç:</div><div id="color-choice"><button class="btn btn-green btn-circle" onclick="pickC('green')">YEŞİL</button><button class="btn btn-red btn-circle" onclick="pickC('red')">KIRMIZI</button></div>`;
  Snd.bip();
}
function pickC(c){Snd.send();api('POST','/api/game/'+gameId+'/color-pick',{uid,color:c}).catch(()=>{});}
function phColor(g,ac,fresh){
  if(!fresh)return;
  myHit=false;
  if(ebem||eliminated){ac.innerHTML='<div style="opacity:0.5;font-size:1rem;">🔍 Aranıyor...</div>';return}
  const tc=g.selectedColor||'green',wc=tc==='green'?'red':'green';
  let grd=[];
  for(let i=0;i<12;i++)grd.push(i<5?tc:wc);
  for(let i=grd.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[grd[i],grd[j]]=[grd[j],grd[i]]}
  let h=`<div class="lbl">🔍 <strong>${tc==='green'?'YEŞİL':'KIRMIZI'}</strong> renge dokun!</div><div id="color-grid">`;
  grd.forEach((c,i)=>{h+=`<div class="color-cell" data-c="${c}" style="background:${c==='green'?'#2fbf71':'#e0433f'}" onclick="tapCell(event,'${c}')"></div>`});
  h+='</div>';ac.innerHTML=h;
  Snd.alert();
}
function tapCell(e,c){
  if(myHit||!gameId)return;
  api('POST','/api/game/'+gameId+'/tap',{uid}).then(r=>{
    if(!r.ok){e.target.classList.add('wrong');setTimeout(()=>e.target.classList.remove('wrong'),250);Snd.bip();return}
    myHit=true;e.target.classList.add('hit');Snd.caught();vib();
  }).catch(()=>{});
}
function showResult(g){
  if(resultShown)return;
  resultShown=true;
  const w=g.winner,p=g.players||{};
  document.querySelector('#screen-result .title').textContent='🏆 '+esc((p[w]||{}).nickname||'?');
  document.querySelector('#screen-result .sub').textContent='Oyunu kazandı!';
  sc('screen-result');
  Snd.win();
  confettiBurst();
}
// Kazanan ekranina konfeti
function confettiBurst(){
  const host=document.getElementById('app');
  if(!host)return;
  const colors=['#d4af37','#f7efe0','#e6c15c','#e0433f','#2fbf71','#a8842a'];
  for(let i=0;i<60;i++){
    const c=document.createElement('div');
    c.className='confetti';
    c.style.left=Math.random()*100+'%';
    c.style.background=colors[i%colors.length];
    c.style.animationDuration=(2+Math.random()*2)+'s';
    c.style.animationDelay=(Math.random()*0.8)+'s';
    c.style.transform='scale('+(0.6+Math.random())+')';
    host.appendChild(c);
    setTimeout(()=>{if(c.parentNode)c.parentNode.removeChild(c)},4500);
  }
}
// Hasar alinca kirmizi flas
function flashRed(){
  const app=document.getElementById('app');
  if(!app)return;
  app.classList.remove('flash');
  void app.offsetWidth;
  app.classList.add('flash');
}
let resultShown=false;

// ========== ODA AYRIL ==========
function leaveRoom(){
  clrT();curPhase=null;lastVer=0;ebem=false;eliminated=false;myHit=false;resultShown=false;lastPhaseSig='';
  Snd.stopAll();
  disconnectWs();
  if(pollTimer){clearInterval(pollTimer);pollTimer=null}
  if(gameId&&uid)api('POST','/api/rooms/'+gameId+'/leave',{uid}).catch(()=>{});
  clearSession();
  uid=null;gameId=null;nick='';isHost=false;ebem=false;eliminated=false;myHit=false;
  goLobby();
}
function doLogout(){
  if(gameId&&uid)api('POST','/api/rooms/'+gameId+'/leave',{uid}).catch(()=>{});
  if(pollTimer){clearInterval(pollTimer);pollTimer=null}
  Snd.stopAll();clrT();lastPhaseSig='';
  stopLobbyPoll();clearSession();
  uid=null;nick='';gameId=null;isHost=false;curPhase=null;lastVer=0;
  document.getElementById('nick-input').value='';
  document.getElementById('nick-input').disabled=false;
  document.getElementById('nick-btn').disabled=false;
  sc('screen-nick');
}

// ========== DOM ==========
document.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('nick-btn').addEventListener('click',doNick);
  document.getElementById('nick-input').addEventListener('keydown',e=>{if(e.key==='Enter')doNick()});
  function doNick(){
    const inp=document.getElementById('nick-input'),v=inp.value.trim();
    if(v.length<1||v.length>14){inp.style.borderColor='#e74c3c';setTimeout(()=>inp.style.borderColor='',1000);return}
    inp.disabled=true;document.getElementById('nick-btn').disabled=true;nick=v;
    api('POST','/api/nick',{nickname:v}).then(r=>{
      if(!r.ok){alert(r.error);inp.disabled=false;document.getElementById('nick-btn').disabled=false;return}
      uid=r.uid;saveSession();sc('screen-lobby');st('Hoş geldin, '+v+'!');
      startLobbyPoll();
      if(pendingInvite)joinViaInvite();
    }).catch(e=>{alert('Bağlantı hatası: '+e.message);inp.disabled=false;document.getElementById('nick-btn').disabled=false;});
  }
  document.getElementById('create-btn').addEventListener('click',createRoom);
  document.getElementById('logout-btn').addEventListener('click',doLogout);
  document.getElementById('start-btn').addEventListener('click',()=>{
    if(!isHost||!gameId)return;
    api('POST','/api/rooms/'+gameId+'/start',{uid}).then(r=>{
      if(!r.ok){msg(r.error||'Baslatilamadi');alert(r.error||'Oyun baslatilamadi');}
    }).catch(e=>{alert('Baglanti hatasi');console.warn(e)});
  });
  document.getElementById('leave-btn').addEventListener('click',leaveRoom);
  document.getElementById('invite-btn').addEventListener('click',copyInvite);
  document.getElementById('bot-btn').addEventListener('click',()=>{
    if(!isHost||!gameId)return;
    api('POST','/api/rooms/'+gameId+'/bots',{uid,action:'add'}).then(r=>{
      if(!r.ok)return;
      loadPlayers();
    }).catch(()=>{});
  });
  document.getElementById('game-exit-btn').addEventListener('click',()=>{
    if(!gameId)return;
    if(confirm('Oyundan ayrılmak istediğine emin misin? Lobiye döneceksin.'))leaveRoom();
  });
  document.getElementById('rematch-btn').addEventListener('click',()=>{
    if(!gameId)return;
    if(isHost){api('POST','/api/game/'+gameId+'/rematch',{uid}).then(()=>{curPhase=null;resultShown=false;sc('screen-room');pollGame();}).catch(()=>{});}
    else{curPhase=null;resultShown=false;sc('screen-room');pollGame();}
  });
  document.getElementById('result-leave-btn').addEventListener('click',leaveRoom);

  // --- Davet linki (#oda=XXX) algila ---
  try{
    const h=(window.location.hash||'').replace(/^#/,'');
    if(h.startsWith('oda=')&&h.length>4)pendingInvite=h.slice(4);
  }catch(e){}

  // --- Oturum geri yukleme (sayfa yenileme / telefon kilidi sonrasi) ---
  const saved=loadSession();
  if(!saved){sc('screen-nick');return}
  uid=saved.uid;nick=saved.nick;
  document.getElementById('nick-input').value=nick;
  if(!saved.gameId){st('Tekrar hoş geldin, '+nick+'!');goLobby();if(pendingInvite)joinViaInvite();return}
  if(pendingInvite&&pendingInvite!==saved.gameId){goLobby();joinViaInvite();return}
  gameId=saved.gameId;
  document.getElementById('room-header').textContent='Oda #'+gameId.substring(0,8);
  sc('screen-room');
  connectWs();
  api('GET','/api/game/'+gameId+'/state?uid='+uid+'&since=0').then(r=>{
    if(!r.ok||!r.state||!r.state.players||!r.state.players[uid]){
      clearSession();goLobby();return;
    }
    const g=r.state;
    lastVer=g.phaseVersion||0;
    if(g.status==='playing'){curPhase='live';lastPhaseSig='';sc('screen-game');Snd.i();enterGame(g);pollGame();}
    else if(g.status==='finished'){showResult(g);pollGame();}
    else{enterRoom();}
  }).catch(()=>{clearSession();goLobby()});
});
</script>
</body>
</html>"""

# ============================================================
# ANA
# ============================================================
if __name__ == '__main__':
    import socket
    hostname = socket.gethostbyname(socket.gethostname())
    # UTF-8 ortaminda calismiyorsa ASCII ile bas
    import io, sys
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    except:
        pass
    print('=' * 40)
    print('  I S T O P - OYUN SUNUCUSU')
    print('=' * 40)
    print(f'  Bilgisayar: http://localhost:{PORT}')
    print(f'  Telefon   : http://{hostname}:{PORT}')
    print('  (Ayni WiFi\'daki herkes girebilir)')
    print('=' * 40)
    server = ThreadedServer(('0.0.0.0', PORT), Handler)
    threading.Thread(target=janitor, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=start_ws_server, daemon=True).start()
    threading.Thread(target=bot_loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nSunucu durduruldu.')
        server.server_close()