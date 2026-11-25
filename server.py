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

import os
PORT = int(os.environ.get('PORT', '8080'))
HOST = '0.0.0.0'

# ============================================================
# OYUN DURUMU (bellekte)
# ============================================================
games = {}
nick_uids = {}  # nickname -> uid (çakışma önleme)

def new_uid(): return uuid.uuid4().hex[:12]

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

        # Oda listesi
        if path == '/api/rooms':
            room_list = []
            for gid, g in games.items():
                if g.get('status') != 'deleted':
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

            player = game.get('players', {}).get(uid)
            if not player:
                # Lobby modunda bile state verelim
                if game.get('status') in ('lobby', 'waiting'):
                    player = game.get('players', {}).get(list(game.get('players', {}).keys())[0]) if game.get('players') else None
                if not player:
                    self._error('Not in game', 403)
                    return

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
            }

            for puid, p in game.get('players', {}).items():
                state['players'][puid] = {
                    'nickname': p.get('nickname', '?'),
                    'lives': p.get('lives', 0),
                    'isEbe': p.get('isEbe', False),
                }

            state['colorHitOrder'] = dict(game.get('colorHitOrder', {}))
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
                'players': {
                    uid: {
                        'nickname': nick,
                        'lives': 3,
                        'isEbe': False,
                        'eliminated': False,
                        'joinedAt': time.time(),
                    }
                },
                'createdAt': time.time(),
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
            }
            game['phaseVersion'] += 1
            game['message'] = f'{nick} katıldı! ({len(game["players"])}/4)'
            self._send({'ok': True})
            return

        # --- ODA AYRIL ---
        if path.startswith('/api/rooms/') and path.endswith('/leave'):
            gid = path.split('/')[3]
            uid = body.get('uid', '')
            game = games.get(gid)
            if not game:
                self._send({'ok': True})  # zaten yok
                return
            game['players'].pop(uid, None)
            game['eliminated'].pop(uid, None)
            game['phaseVersion'] += 1
            if not game['players']:
                games[gid]['status'] = 'deleted'
            elif game.get('host') == uid:
                # yeni host seç
                if game['players']:
                    new_host = next(iter(game['players']))
                    game['host'] = new_host
                    game['message'] = f'{game["players"][new_host]["nickname"]} yeni ev sahibi'
            self._send({'ok': True})
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

            uids = list(game['players'].keys())
            ebe_uid = random.choice(uids)
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
            game['catchDeadline'] = time.time() * 1000 + 4000
            game['message'] = f'⚡ {game["players"][target]["nickname"]} yakalamaya çalışıyor!'
            game['phaseVersion'] += 1

            # 4 sn sonra timeout kontrol
            t = threading.Timer(4.5, check_catch_timeout, args=[gid])
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
        reset_after_battle(game)
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
        reset_after_battle(game)
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

def reset_after_battle(game):
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


# ============================================================
# İNDEX.HTML (gömülü)
# ============================================================
def get_index_html():
    return INDEX_HTML


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<meta name="theme-color" content="#1a1a2e">
<title>İstop</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;width:100%;overflow:hidden;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#1a1a2e;color:#fff;touch-action:manipulation;-webkit-tap-highlight-color:transparent;user-select:none}
button{font:inherit;border:none;cursor:pointer;outline:none;-webkit-tap-highlight-color:transparent;touch-action:manipulation}
input{font:inherit;outline:none}
#app{display:flex;flex-direction:column;height:100vh;max-width:480px;margin:0 auto;background:linear-gradient(180deg,#1a1a2e,#232340);position:relative;overflow:hidden}
#player-bar{display:flex;flex-wrap:wrap;gap:3px;padding:5px 6px;background:rgba(0,0,0,0.2);flex-shrink:0;justify-content:space-around}
.p-card{flex:1 1 22%;display:flex;flex-direction:column;align-items:center;gap:1px;background:rgba(255,255,255,0.05);border-radius:8px;padding:3px 1px;font-size:0.68rem;line-height:1.1;position:relative;transition:all 0.3s}
.p-card.eliminated{opacity:0.3;filter:grayscale(1)}
.p-card .av{font-size:1.5rem}
.p-card .nm{font-size:0.55rem;color:#ccc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.p-card .lv{font-size:0.55rem;letter-spacing:-1px}
.p-card .badge{position:absolute;top:-3px;right:-3px;background:#ffd700;color:#000;font-size:0.55rem;padding:1px 4px;border-radius:5px;font-weight:bold}
.p-card.you{outline:1px solid rgba(255,255,255,0.12)}
#action{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:10px 14px;gap:10px;min-height:150px}
#msg{font-size:1.1rem;font-weight:600;text-align:center;padding:6px 12px;border-radius:10px;background:rgba(255,255,255,0.05);max-width:100%}
.btn{display:block;width:100%;padding:14px;border-radius:12px;font-size:1.15rem;font-weight:700;background:#3a3a5a;color:#fff;transition:all 0.1s}
.btn:active{transform:scale(0.95)}
.btn-green{background:#2ecc71;color:#fff}
.btn-red{background:#e74c3c;color:#fff}
.btn-gold{background:#f39c12;color:#fff}
.btn-small{font-size:0.85rem;padding:10px 16px}
.btn:disabled{opacity:0.35;pointer-events:none}
.btn-circle{width:120px;height:120px;border-radius:50%;display:flex;align-items:center;justify-content:center}
#status-bar{flex-shrink:0;padding:4px 8px;font-size:0.68rem;color:rgba(255,255,255,0.5);text-align:center;background:rgba(0,0,0,0.12);min-height:16px}
.screen{position:absolute;inset:0;z-index:5;display:none;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:16px;background:#1a1a2e}
#screen-nick{display:flex}
#screen-nick h1{font-size:2.2rem;font-weight:800}
#screen-nick .accent{color:#e74c3c}
#screen-nick .sub{opacity:0.5;font-size:0.8rem}
#nick-input{width:80%;max-width:260px;padding:12px 14px;border-radius:12px;border:none;font-size:1.05rem;text-align:center;background:rgba(255,255,255,0.07);color:#fff;border:1px solid rgba(255,255,255,0.1)}
.room-list{display:flex;flex-direction:column;gap:5px;max-height:170px;overflow-y:auto}
.room-item{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:8px;background:rgba(255,255,255,0.04)}
.room-item .ri{flex:1;font-size:0.85rem}
.room-item .ri small{opacity:0.45;font-size:0.7rem}
.room-item .join-btn{background:#2ecc71;padding:7px 16px;border-radius:7px;font-size:0.8rem;font-weight:600;color:#fff}
.room-item .join-btn:disabled{background:#555;opacity:0.5}
#room-header{font-size:1.15rem;font-weight:700;text-align:center;margin-bottom:3px}
.player-list{display:flex;flex-direction:column;gap:4px}
.pl-item{display:flex;align-items:center;gap:5px;padding:5px 8px;border-radius:5px;background:rgba(255,255,255,0.03)}
.pl-item .pln{flex:1;font-weight:600;font-size:0.85rem}
#catch-btn{width:140px;height:140px;border-radius:50%;font-size:1.2rem;font-weight:800;background:radial-gradient(circle at 40% 36%,#f1c40f,#e67e22);color:#fff;display:flex;align-items:center;justify-content:center;box-shadow:0 0 25px rgba(241,196,15,0.3);animation:pulse-catch 0.7s infinite}
@keyframes pulse-catch{0%,100%{box-shadow:0 0 15px rgba(241,196,15,0.15);transform:scale(1)}50%{box-shadow:0 0 40px rgba(241,196,15,0.45);transform:scale(1.05)}}
#cd{font-size:2.2rem;font-weight:800;color:#f1c40f}
#istop-btn{font-size:1.8rem;font-weight:900;padding:16px 40px;border-radius:16px;background:linear-gradient(135deg,#e74c3c,#c0392b);color:#fff;box-shadow:0 0 30px rgba(231,76,60,0.3);animation:pulse-istop 0.6s infinite}
@keyframes pulse-istop{0%,100%{box-shadow:0 0 15px rgba(231,76,60,0.15);transform:scale(1)}50%{box-shadow:0 0 40px rgba(231,76,60,0.4);transform:scale(1.03)}}
#color-choice{display:flex;gap:12px;justify-content:center}
#color-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;width:100%;max-width:280px;margin:0 auto}
.color-cell{aspect-ratio:1;border-radius:50%;transition:all 0.08s}
.color-cell:active{transform:scale(0.85)}
.color-cell.hit{opacity:0.3;transform:scale(0.5);pointer-events:none}
.color-cell.wrong{animation:sh 0.25s}
@keyframes sh{0%,100%{transform:translateX(0)}20%{transform:translateX(-5px)}60%{transform:translateX(5px)}}
#screen-result{z-index:20;position:absolute;inset:0;display:none;flex-direction:column;align-items:center;justify-content:center;gap:12px;background:rgba(0,0,0,0.5);backdrop-filter:blur(3px)}
#screen-result .title{font-size:2rem;font-weight:800}
#screen-result .sub{font-size:0.9rem;opacity:0.65}
.hidden{display:none!important}
.lbl{font-size:0.8rem;opacity:0.55;margin-bottom:4px}
</style>
</head>
<body>
<div id="app">

<div id="screen-nick" class="screen">
<h1>İ<span class="accent">STOP</span></h1>
<div class="sub">Çok oyunculu - yerel ağ</div>
<input id="nick-input" type="text" maxlength="14" placeholder="Takma adın" autocomplete="off" enterkeyhint="done">
<button class="btn btn-green" id="nick-btn" style="width:80%;max-width:240px;">GİR</button>
<div style="font-size:0.55rem;opacity:0.3;margin-top:6px;">Aynı WiFi'daki oyuncularla oyna</div>
</div>

<div id="screen-lobby" class="screen">
<div class="lbl" style="margin-top:-8px;">── ODALAR ──</div>
<div class="room-list" id="room-list"><div style="opacity:0.5;text-align:center;padding:10px;">Yükleniyor...</div></div>
<button class="btn btn-gold" id="create-btn">➕ Yeni Oda</button>
<button class="btn btn-small" id="logout-btn" style="background:#555;">Çıkış</button>
</div>

<div id="screen-room" class="screen">
<div id="room-header">Oda</div>
<div class="player-list" id="player-list"></div>
<button class="btn btn-green" id="start-btn" style="display:none;">🎮 Oyunu Başlat</button>
<button class="btn btn-small" id="leave-btn" style="background:#555;">Odadan Ayrıl</button>
</div>

<div id="screen-game" style="display:none;flex-direction:column;height:100%;width:100%;position:relative;">
<div id="player-bar"></div>
<div id="action"><div id="msg">Oyun başlıyor...</div><div id="ac"></div></div>
<div id="status-bar">📍</div>
</div>

<div id="screen-result">
<div class="title">🏆</div>
<div class="sub"></div>
<button class="btn btn-gold" id="rematch-btn" style="width:80%;max-width:240px;">Tekrar Oyna</button>
<button class="btn btn-small" id="result-leave-btn" style="background:#555;max-width:240px;">Odadan Ayrıl</button>
</div>

</div>
<script>
// ========== DURUM ==========
let uid=null,nick='',gameId=null,isHost=false,ebem=false,eliminated=false,lastVer=0;
let curPhase=null,timers=[],myHit=false,stateCache={},pollTimer=null;
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
const Snd={ctx:null,i(){try{this.ctx=new(window.AudioContext||window.webkitAudioContext)()}catch(e){}},p(f,d,t,v=0.3){if(!this.ctx)return;try{const o=this.ctx.createOscillator(),a=this.ctx.createGain();o.type=t||'sine';o.frequency.value=f;a.gain.value=v;o.connect(a);a.connect(this.ctx.destination);o.start();setTimeout(()=>{o.stop();a.disconnect()},d)}catch(e){}},bip(){this.p(880,100,'sine',0.32)},alert(){this.p(660,70,'sine',0.28);setTimeout(()=>this.p(990,100,'sine',0.3),80)},send(){this.p(440,40,'square',0.16);setTimeout(()=>this.p(660,60,'sine',0.2),40)},caught(){this.p(880,50,'sine',0.26);setTimeout(()=>this.p(1100,120,'sine',0.2),50)},istop(){this.p(300,160,'sawtooth',0.2);setTimeout(()=>this.p(200,260,'sawtooth',0.16),160)},hit(){this.p(200,220,'square',0.16)},win(){[660,880,1100,1320].forEach((f,i)=>setTimeout(()=>this.p(f,110,'sine',0.26),i*80))},lose(){this.p(300,250,'sine',0.2);setTimeout(()=>this.p(200,320,'sine',0.16),230)}};

// ========== API ==========
const API=window.location.origin;
function api(method,path,body){
  const opts={method,headers:{'Content-Type':'application/json'}};
  if(body)opts.body=JSON.stringify(body);
  return fetch(API+path,opts).then(r=>r.json());
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
    if(r.ok){gameId=r.gameId;isHost=true;enterRoom()}
    else alert(r.error);
  });
}
function joinRoom(gid){
  api('POST','/api/rooms/'+gid+'/join',{uid,nickname:nick}).then(r=>{
    if(r.ok){gameId=gid;isHost=false;enterRoom()}
    else alert(r.error);
  });
}
function enterRoom(){
  sc('screen-room');
  document.getElementById('room-header').textContent='Oda #'+gameId.substring(0,8);
  pollGame(); // oda içinde de state poll
  loadPlayers();
}
function loadPlayers(){
  api('GET','/api/game/'+gameId+'/state?uid='+uid+'&since='+lastVer).then(r=>{
    if(!r.ok)return;
    const g=r.state;if(!g||!g.players)return;
    const list=document.getElementById('player-list');
    let h='';const cnt=Object.keys(g.players).length;
    Object.keys(g.players).forEach(u=>{
      const p=g.players[u],me=u===uid;const s=(p.lives||0)>0&&!g.eliminated[u]?'✅':'☠️';
      h+=`<div class="pl-item"><span>${s}</span><span class="pln">${esc(p.nickname)}${me?' (Sen)':''}</span></div>`;
    });
    list.innerHTML=h;
    const sb=document.getElementById('start-btn');
    if(isHost&&cnt>=2&&cnt<=4){sb.style.display='block';sb.disabled=false;sb.textContent='🎮 Oyunu Başlat ('+cnt+'/4)';}
    else if(isHost){sb.style.display='block';sb.disabled=true;sb.textContent='En az 2 oyuncu ('+cnt+'/4)';}
    else sb.style.display='none';
    st('Oyuncu: '+cnt+'/4');
    if(g.phase!=='waiting'&&g.status==='playing'){curPhase='live';enterGame(g);}
  }).catch(()=>{});
}

// ========== OYUN POLL ==========
function pollGame(){
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(()=>{
    if(!gameId||!uid)return;
    api('GET','/api/game/'+gameId+'/state?uid='+uid+'&since='+lastVer).then(r=>{
      if(!r.ok){if(r.error.includes('Not in game'))leaveRoom();return;}
      const g=r.state;if(!g)return;
      stateCache=g;
      if(g.status==='deleted'||!g.players[uid]){leaveRoom();return}
      if(g.phaseVersion>lastVer){lastVer=g.phaseVersion;clrT();myHit=false;}
      cards(g);
      if(g.status==='playing'&&!curPhase){curPhase='live';sc('screen-game');Snd.i();enterGame(g);}
      else if(g.status==='playing'&&curPhase){gameTick(g);}
      else if(g.status==='finished'){showResult(g);}
      else if(g.status==='lobby'&&curPhase){curPhase=null;loadPlayers();}
      if(g.message)msg(g.message);
    }).catch(()=>{});
  },250);
}

function enterGame(g){cards(g);gameTick(g);}
function gameTick(g){
  const pls=g.players,eli=g.eliminated||{},me=pls[uid];
  eliminated=!!eli[uid]||!me||(me.lives||0)<=0;
  ebem=g.currentEbe===uid;
  if(eliminated){document.getElementById('ac').innerHTML='<div style="opacity:0.5;font-size:1rem;">☠️ Elendin. İzliyorsun...</div>';return}
  const ac=document.getElementById('ac');
  switch(g.phase){
    case'ebe_turn':phEbe(g,ac);break;
    case'catching':phCatch(g,ac);break;
    case'istop':phIstop(g,ac);break;
    case'color_battle':phColor(g,ac);break;
  }
}
function phEbe(g,ac){
  if(ebem){
    const others=Object.keys(g.players).filter(u=>u!==uid&&!g.eliminated[u]&&(g.players[u].lives||0)>0);
    if(others.length===0){api('POST','/api/game/'+gameId+'/rematch',{uid});return}
    let h='<div class="lbl">Kime atıyorsun?</div><div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:center;">';
    others.forEach(u=>{h+=`<button class="btn btn-small" style="min-width:80px;padding:10px 16px;border-radius:10px;background:#5a5a8a;color:#fff;" onclick="throwTo('${u}')">${esc(g.players[u].nickname)}</button>`});
    h+='</div>';ac.innerHTML=h;
  }else{ac.innerHTML='<div style="opacity:0.5;font-size:1rem;">🔄 Ebe bekleniyor...</div>'}
}
function throwTo(tu){
  Snd.send();
  api('POST','/api/game/'+gameId+'/throw',{uid,target:tu}).catch(()=>{});
}
function phCatch(g,ac){
  if(g.targetPlayer===uid&&!eliminated){
    const rem=Math.max(0,Math.floor((g.catchDeadline-Date.now())/1000));
    ac.innerHTML=`<div id="cd">${rem}</div><button id="catch-btn" onclick="doCatch()">🏀 YAKALA!</button>`;
    Snd.alert();vib();ntf('İstop','Sana top geldi!');
    if(g.catchDeadline>Date.now()){
      const iv=setInterval(()=>{const r=Math.max(0,Math.floor((g.catchDeadline-Date.now())/1000));const el=document.getElementById('cd');if(el)el.textContent=String(r);if(r<=0)clearInterval(iv);},150);
      timers.push(iv);timers.push(setTimeout(()=>clearInterval(iv),5000));
    }
  }else{ac.innerHTML='<div style="opacity:0.5;font-size:1rem;">⏳ Top yolda...</div>'}
}
function doCatch(){if(!gameId)return;Snd.caught();vib();api('POST','/api/game/'+gameId+'/catch',{uid}).catch(()=>{});}
function phIstop(g,ac){
  ac.innerHTML=ebem?`<button id="istop-btn" onclick="pickColor()">🛑 İSTOP!</button>`:'<div style="opacity:0.5;font-size:1.2rem;">🛑 İSTOP!</div>';
  Snd.istop();vib();ntf('İstop','Top kaçtı!');
}
function pickColor(){
  document.getElementById('ac').innerHTML=`<div class="lbl">Rengi seç:</div><div id="color-choice"><button class="btn btn-green btn-circle" onclick="pickC('green')">YEŞİL</button><button class="btn btn-red btn-circle" onclick="pickC('red')">KIRMIZI</button></div>`;
  Snd.bip();
}
function pickC(c){Snd.send();api('POST','/api/game/'+gameId+'/color-pick',{uid,color:c}).catch(()=>{});}
function phColor(g,ac){
  myHit=false;
  if(ebem||eliminated){ac.innerHTML='<div style="opacity:0.5;font-size:1rem;">🔍 Aranıyor...</div>';return}
  const tc=g.selectedColor||'green',wc=tc==='green'?'red':'green';
  let grd=[];
  for(let i=0;i<12;i++)grd.push(i<5?tc:wc);
  for(let i=grd.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[grd[i],grd[j]]=[grd[j],grd[i]]}
  let h=`<div class="lbl">🔍 <strong>${tc==='green'?'YEŞİL':'KIRMIZI'}</strong> renge dokun!</div><div id="color-grid">`;
  grd.forEach((c,i)=>{h+=`<div class="color-cell" data-c="${c}" style="background:${c==='green'?'#2ecc71':'#e74c3c'}" onclick="tapCell(event,'${c}')"></div>`});
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
}
let resultShown=false;

// ========== ODA AYRIL ==========
function leaveRoom(){
  clrT();curPhase=null;lastVer=0;ebem=false;eliminated=false;myHit=false;resultShown=false;
  if(pollTimer){clearInterval(pollTimer);pollTimer=null}
  if(gameId&&uid)api('POST','/api/rooms/'+gameId+'/leave',{uid}).catch(()=>{});
  gameId=null;isHost=false;
  setTimeout(()=>{sc('screen-lobby')},100);
}
function doLogout(){
  if(gameId&&uid)api('POST','/api/rooms/'+gameId+'/leave',{uid}).catch(()=>{});
  if(pollTimer){clearInterval(pollTimer);pollTimer=null}
  uid=null;gameId=null;isHost=false;curPhase=null;lastVer=0;
  document.getElementById('nick-input').value='';
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
      uid=r.uid;sc('screen-lobby');st('Hoş geldin, '+v+'!');
      pollRooms();setInterval(pollRooms,2000);
    }).catch(e=>{alert('Bağlantı hatası: '+e.message);inp.disabled=false;document.getElementById('nick-btn').disabled=false;});
  }
  document.getElementById('create-btn').addEventListener('click',createRoom);
  document.getElementById('logout-btn').addEventListener('click',doLogout);
  document.getElementById('start-btn').addEventListener('click',()=>{if(!isHost||!gameId)return;api('POST','/api/rooms/'+gameId+'/start',{uid}).catch(()=>{});});
  document.getElementById('leave-btn').addEventListener('click',leaveRoom);
  document.getElementById('rematch-btn').addEventListener('click',()=>{
    if(!gameId)return;
    if(isHost){api('POST','/api/game/'+gameId+'/rematch',{uid}).then(()=>{curPhase=null;resultShown=false;sc('screen-room');pollGame();}).catch(()=>{});}
    else{curPhase=null;resultShown=false;sc('screen-room');pollGame();}
  });
  document.getElementById('result-leave-btn').addEventListener('click',leaveRoom);
  sc('screen-nick');
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nSunucu durduruldu.')
        server.server_close()