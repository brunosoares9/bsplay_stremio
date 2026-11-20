import os
import re
import time
import socket
import random
import ipaddress
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, quote, urlparse, unquote

import requests
import urllib3
from flask import Flask, request, jsonify, Response, stream_with_context, g

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ========== CONFIG (tudo por variavel de ambiente) ==========
# Ex: XC_HOST=http://meuservidor:8880 XC_USERNAME=... XC_PASSWORD=... TMDB_API_KEY=...
XC_HOST = os.environ.get("XC_HOST", "").rstrip("/")
XC_USERNAME = os.environ.get("XC_USERNAME", "")
XC_PASSWORD = os.environ.get("XC_PASSWORD", "")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
BASE_URL_OVERRIDE = os.environ.get("BASE_URL", "").rstrip("/")  # ex: https://meuapp.koyeb.app
PROXY_VOD = os.environ.get("PROXY_VOD", "0") == "1"  # 1 = filmes/series passam pelo seu servidor (esconde credenciais, gasta banda)

ADDON_NAME = "bsPlay"
ADDON_ID = "org.bsplay.addon"
ADDON_VERSION = "1.0.1"
ADDON_DESCRIPTION = "bsPlay - Xtream Codes Addon para Stremio com F4M Proxy Embutido"

ICON_URL = "https://raw.githubusercontent.com/brunosoares9/bsplay_stremio/refs/heads/main/Icon.svg"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
}

PROXY_BUFFER_SIZE = 32768
PROXY_MAX_RETRIES = 3
PROXY_RETRY_DELAY = 0.5
M3U8_MAX_BYTES = 2 * 1024 * 1024

# ========== CACHES (todos com limite) ==========
IMG_CACHE = OrderedDict()
IMG_CACHE_BYTES = 0
IMG_CACHE_MAX_BYTES = 20 * 1024 * 1024
IMG_MAX_SINGLE = 2 * 1024 * 1024
IMG_CACHE_LOCK = threading.Lock()

CATEGORY_MAP = {}
CATEGORY_LOCK = threading.Lock()

TMDB_BASE_URL = 'https://api.themoviedb.org/3'

STREAM_MAX_RESULTS = 5
STREAM_MAX_CANDIDATES = 8
STREAM_MIN_MATCH_SCORE = 0.85
STREAM_CACHE_TTL = 600
TMDB_CACHE_TTL = 86400
XTREAM_CATALOG_TTL = 600
DETAIL_CONCURRENCY = 3
CACHE_MAX_ENTRIES = 300

STREAM_CACHE = {}
TMDB_CACHE = {}
XTREAM_CATALOG_CACHE = {}
CACHE_LOCK = threading.Lock()

# so os campos que o addon realmente usa (corta MUITA memoria)
SLIM_KEYS = ('name', 'title', 'stream_id', 'series_id', 'id', 'stream_icon',
             'cover', 'cover_big', 'movie_image', 'rating', 'year')

QUALITY_EMOJIS = {'4K': '👑', 'FHD': '💫', 'HD': '✨', 'SD': '📺', 'CAM': '🎥'}
QUALITY_LABELS = {
    '4K': '4K Ultra HD',
    'FHD': 'Full HD 1080p',
    'HD': 'HD 720p',
    'SD': '1080p',
    'CAM': 'CAM'
}
QUALITY_ORDER = {'4K': 4, 'FHD': 3, 'HD': 2, 'SD': 1, 'CAM': 0}


# ========== HELPERS GERAIS ==========
def cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
        'Access-Control-Allow-Headers': 'Range, Origin, Content-Type, Accept',
    }


def get_random_user_agent():
    v = f"{random.randint(118, 131)}.0.{random.randint(0, 6999)}.{random.randint(0, 199)}"
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36"


def is_safe_url(url):
    """Bloqueia SSRF: so http/https e nada que resolva pra IP interno (exceto o proprio XC_HOST)."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https') or not p.hostname:
            return False
        if XC_HOST and p.hostname == urlparse(XC_HOST).hostname:
            return True
        port = p.port or (443 if p.scheme == 'https' else 80)
        for info in socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def get_base_url():
    if BASE_URL_OVERRIDE:
        return BASE_URL_OVERRIDE
    if request.headers.get('X-Forwarded-Host'):
        final = f"https://{request.headers.get('X-Forwarded-Host')}"
    else:
        final = request.url_root.rstrip('/')
        final = re.sub(r'^http://', 'https://', final)
    host = urlparse(final).hostname or ''
    if host and 'localhost' not in host and host != '127.0.0.1' and '.' not in host:
        final = final + '.baby-beamup.club'
    return final


def cache_get(cache_obj, key, ttl):
    entry = cache_obj.get(key)
    if entry and (time.time() - entry[1]) < ttl:
        return entry[0]
    return None


def cache_set(cache_obj, key, data):
    with CACHE_LOCK:
        if len(cache_obj) >= CACHE_MAX_ENTRIES:
            # remove o mais antigo
            oldest = min(cache_obj, key=lambda k: cache_obj[k][1])
            cache_obj.pop(oldest, None)
        cache_obj[key] = (data, time.time())


# ========== XTREAM CODES ==========
def xc_request(action, params=None):
    params = dict(params or {})
    params.update({'username': XC_USERNAME, 'password': XC_PASSWORD, 'action': action})
    try:
        response = requests.get(f"{XC_HOST}/player_api.php", params=params,
                                headers=HEADERS, timeout=15, verify=False)
        return response.json() if response.status_code == 200 else []
    except Exception:
        return []


def slim_item(item):
    out = {k: item[k] for k in SLIM_KEYS if k in item}
    plot = item.get('plot')
    if plot:
        out['plot'] = str(plot)[:200]
    return out


def get_xcatalog(action):
    """Catalogo completo, enxuto e com cache (ttl + limite de entradas)."""
    cache_key = action
    cached = cache_get(XTREAM_CATALOG_CACHE, cache_key, XTREAM_CATALOG_TTL)
    if cached is not None:
        return cached

    data = xc_request(action)
    if isinstance(data, list) and data:
        data = [slim_item(i) for i in data if isinstance(i, dict)]
        cache_set(XTREAM_CATALOG_CACHE, cache_key, data)
        return data
    return []


def clean_text(text):
    if not text:
        return ''
    text = text.lower()
    accents = {
        'à': 'a', 'á': 'a', 'â': 'a', 'ã': 'a', 'ä': 'a', 'å': 'a', 'æ': 'ae', 'ç': 'c',
        'è': 'e', 'é': 'e', 'ê': 'e', 'ë': 'e', 'ì': 'i', 'í': 'i', 'î': 'i', 'ï': 'i',
        'ð': 'd', 'ñ': 'n', 'ò': 'o', 'ó': 'o', 'ô': 'o', 'õ': 'o', 'ö': 'o', 'ø': 'o',
        'ù': 'u', 'ú': 'u', 'û': 'u', 'ü': 'u', 'ý': 'y', 'þ': 'th', 'ÿ': 'y'
    }
    for k, v in accents.items():
        text = text.replace(k, v)
    return re.sub(r'[^a-z0-9]', '', text)


def slugify_category(name):
    if not name:
        return ''
    text = name.lower()
    accents = {
        'à': 'a', 'á': 'a', 'â': 'a', 'ã': 'a', 'ä': 'a', 'å': 'a', 'æ': 'ae', 'ç': 'c',
        'è': 'e', 'é': 'e', 'ê': 'e', 'ë': 'e', 'ì': 'i', 'í': 'i', 'î': 'i', 'ï': 'i',
        'ð': 'd', 'ñ': 'n', 'ò': 'o', 'ó': 'o', 'ô': 'o', 'õ': 'o', 'ö': 'o', 'ø': 'o',
        'ù': 'u', 'ú': 'u', 'û': 'u', 'ü': 'u', 'ý': 'y', 'þ': 'th', 'ÿ': 'y'
    }
    for k, v in accents.items():
        text = text.replace(k, v)
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text or 'cat'


def build_category_map(cats):
    mapping = {}
    if not isinstance(cats, list):
        return mapping
    for cat in cats:
        cat_name = cat.get('category_name') or cat.get('name') or ''
        if cat_name:
            mapping[slugify_category(cat_name)] = cat_name
    return mapping


def resolve_category(genre, content_type):
    if not genre:
        return None

    with CATEGORY_LOCK:
        mapping = CATEGORY_MAP.get(content_type)

    if not mapping:
        if content_type == 'movie':
            cat_action = 'get_vod_categories'
        elif content_type == 'series':
            cat_action = 'get_series_categories'
        else:
            cat_action = 'get_live_categories'
        mapping = build_category_map(xc_request(cat_action))
        with CATEGORY_LOCK:
            CATEGORY_MAP[content_type] = mapping

    if genre in mapping:
        return mapping[genre]
    for slug, original in mapping.items():
        if slug.lower() == genre.lower() or original.lower() == genre.lower():
            return original
    return genre


def build_stream_url(content_type, stream_id, episode_id=None, base=None):
    """URLs sempre opacas quando passam pelo proxy: o cliente nunca ve usuario/senha."""
    if content_type == 'tv':
        return f"{base or get_base_url()}/live/{stream_id}.m3u8"

    if content_type == 'movie':
        kind, item_id = 'movie', stream_id
    elif content_type == 'series' and episode_id:
        kind, item_id = 'series', episode_id
    else:
        kind, item_id = 'series', stream_id

    if PROXY_VOD:
        return f"{base or get_base_url()}/vod/{kind}/{item_id}.mp4"
    return f"{XC_HOST}/{kind}/{XC_USERNAME}/{XC_PASSWORD}/{item_id}.mp4"


def proxify_image_url(poster):
    if not poster:
        return poster
    if not poster.startswith('http'):
        poster = urljoin(XC_HOST + '/', poster.lstrip('/'))
    try:
        return f"{get_base_url()}/img?url={quote(poster, safe='')}"
    except RuntimeError:
        return poster


def first_or_str(value, fallback=''):
    if isinstance(value, list):
        return value[0] if value else fallback
    return value or fallback


def map_item(item, content_type):
    if content_type == 'series':
        item_id = item.get('series_id') or item.get('id', '')
        poster = item.get('cover') or item.get('cover_big') or item.get('stream_icon') or item.get('movie_image')
    elif content_type == 'tv':
        item_id = item.get('stream_id') or item.get('id', '')
        poster = item.get('stream_icon') or item.get('cover')
    else:
        item_id = item.get('stream_id') or item.get('id', '')
        poster = (item.get('stream_icon') or item.get('movie_image')
                  or item.get('cover_big') or item.get('cover'))

    poster = proxify_image_url(poster)
    name = item.get('name') or item.get('title', 'Sem título')

    rating = None
    try:
        if item.get('rating'):
            rating = float(item['rating'])
    except (ValueError, TypeError):
        pass

    year = None
    try:
        if item.get('year'):
            year = int(str(item['year'])[:4])
    except (ValueError, TypeError):
        pass

    return {
        'id': f"bsplay:{content_type}:{item_id}",
        'type': content_type,
        'name': name,
        'poster': poster or None,
        'posterShape': 'square' if content_type == 'tv' else 'poster',
        'imdbRating': rating,
        'year': year,
        'description': item.get('plot') or f"bsPlay {content_type.capitalize()}"
    }


# ========== HOOKS ==========
@app.before_request
def log_request():
    g.t0 = time.time()
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    ua = request.headers.get('User-Agent', '')[:50]
    print(f">>> {request.method} {request.path} | IP: {client_ip} | UA: {ua}")


@app.after_request
def add_cors_headers(response):
    if request.path.startswith(('/proxy', '/tsdownloader', '/img', '/live', '/vod',
                                '/manifest', '/catalog', '/meta', '/stream')):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Range, Origin, Content-Type, Accept'

    t0 = getattr(g, 't0', None)
    if t0:
        print(f"<<< {request.method} {request.path} -> {response.status_code} ({(time.time() - t0) * 1000:.0f}ms)")
    return response


# ========== IMAGENS (cache LRU limitado por BYTES) ==========
@app.route('/img')
def proxy_image():
    global IMG_CACHE_BYTES
    url = request.args.get('url')
    if not url:
        return Response('Missing url parameter', status=400)
    if not is_safe_url(url):
        return Response('Forbidden url', status=403)

    with IMG_CACHE_LOCK:
        if url in IMG_CACHE:
            IMG_CACHE.move_to_end(url)
            content_type, content = IMG_CACHE[url]
            return Response(content, status=200, headers={
                'Content-Type': content_type,
                'Cache-Control': 'public, max-age=86400',
            })

    try:
        parsed = urlparse(url)
        resp = requests.get(
            url,
            headers={
                'User-Agent': HEADERS['User-Agent'],
                'Accept': 'image/*,*/*;q=0.8',
                'Accept-Encoding': 'identity',
                'Referer': f"{parsed.scheme}://{parsed.netloc}/",
            },
            timeout=15,
            verify=False,
        )
        if resp.status_code not in (200, 206):
            return Response('Image fetch failed', status=resp.status_code or 502)

        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        content = resp.content
        resp.close()

        if len(content) <= IMG_MAX_SINGLE:
            with IMG_CACHE_LOCK:
                if url not in IMG_CACHE:
                    IMG_CACHE[url] = (content_type, content)
                    IMG_CACHE_BYTES += len(content)
                    while IMG_CACHE_BYTES > IMG_CACHE_MAX_BYTES and IMG_CACHE:
                        _, (_, old) = IMG_CACHE.popitem(last=False)
                        IMG_CACHE_BYTES -= len(old)

        return Response(content, status=200, headers={
            'Content-Type': content_type,
            'Cache-Control': 'public, max-age=86400',
        })
    except Exception:
        return Response('Image proxy error', status=502)


# ========== PAGINA INICIAL ==========
@app.route('/')
def index():
    base_url = get_base_url()
    manifest_url = f"{base_url}/manifest.json"

    html = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>bsPlay - Stremio Addon</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; color: #fff; }
        .container { background: rgba(255,255,255,0.05); border-radius: 20px; padding: 40px; max-width: 500px; width: 100%; border: 1px solid rgba(255,255,255,0.1); backdrop-filter: blur(10px); }
        .logo { text-align: center; margin-bottom: 30px; }
        .logo img { width: 80px; height: 80px; border-radius: 16px; box-shadow: 0 8px 32px rgba(0,0,0,0.3); }
        h1 { text-align: center; font-size: 28px; margin-bottom: 10px; background: linear-gradient(135deg, #00d4ff, #ff006e); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .subtitle { text-align: center; color: rgba(255,255,255,0.7); margin-bottom: 30px; }
        .btn { display: block; width: 100%; padding: 16px; margin: 12px 0; border: none; border-radius: 12px; font-size: 16px; font-weight: 600; cursor: pointer; text-decoration: none; text-align: center; transition: all 0.3s; }
        .btn-stremio { background: linear-gradient(135deg, #00d4ff, #0099cc); color: white; }
        .btn-stremio:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,212,255,0.4); }
        .btn-web { background: linear-gradient(135deg, #ff006e, #cc0055); color: white; }
        .btn-web:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(255,0,110,0.4); }
        .btn-copy { background: rgba(255,255,255,0.1); color: #fff; border: 1px solid rgba(255,255,255,0.2); }
        .btn-copy:hover { background: rgba(255,255,255,0.2); }
        .manifest-box { background: rgba(0,0,0,0.3); border-radius: 8px; padding: 16px; margin-top: 20px; font-family: monospace; font-size: 12px; word-break: break-all; color: #00ff88; }
        .info { text-align: center; color: rgba(255,255,255,0.5); font-size: 13px; margin-top: 20px; }
        .credits { text-align: center; margin-top: 30px; padding-top: 24px; border-top: 1px solid rgba(255,255,255,0.1); color: rgba(255,255,255,0.6); line-height: 1.7; }
        .credits strong { background: linear-gradient(135deg, #00d4ff, #ff006e); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 800; }
        .credits .maintainer { font-size: 15px; color: rgba(255,255,255,0.75); margin-bottom: 10px; letter-spacing: 0.3px; }
        .credits .thanks { font-size: 12.5px; color: rgba(255,255,255,0.5); line-height: 1.6; }
        .credits .thanks strong { font-size: 13.5px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">
            <img src=""" + ICON_URL + """ alt="bsPlay">
        </div>
        <h1>bsPlay</h1>
        <p class="subtitle">Addon Stremio para Xtream Codes com F4M Proxy</p>

        <a href="stremio://addon?url=""" + quote(manifest_url, safe='') + """\" class="btn btn-stremio">📱 Instalar no App Stremio</a>
        <a href="https://web.stremio.com/#!/addons/""" + quote(manifest_url, safe='') + """\" target="_blank" class="btn btn-web">🌐 Instalar no Stremio Web</a>
        <button onclick="copyManifest()" class="btn btn-copy">📋 Copiar URL do Manifest</button>

        <div class="manifest-box" id="manifestUrl">""" + manifest_url + """</div>

        <p class="info">Funciona com servidores atrás de proxy reverso. O host base é detectado automaticamente.</p>

        <div class="credits">
            <div class="maintainer">Mantido por <strong>bsPlay</strong></div>
            <div class="thanks">
                Agradecimento especial à <strong>Equipe OnePlay</strong><br>
                pelo projeto original que tornou tudo isso possível
            </div>
        </div>
    </div>
    <script>
        function copyManifest() {
            const text = document.getElementById('manifestUrl').textContent;
            navigator.clipboard.writeText(text).then(() => {
                const btn = document.querySelector('.btn-copy');
                const original = btn.textContent;
                btn.textContent = '✅ Copiado!';
                setTimeout(() => btn.textContent = original, 2000);
            });
        }
    </script>
</body>
</html>
    """
    return html


# ========== MANIFEST ==========
@app.route('/manifest.json')
def manifest_root():
    with CATEGORY_LOCK:
        has_all = all(CATEGORY_MAP.get(k) for k in ('movie', 'series', 'tv'))

    if not has_all:
        movie_cats = build_category_map(xc_request('get_vod_categories'))
        series_cats = build_category_map(xc_request('get_series_categories'))
        live_cats = build_category_map(xc_request('get_live_categories'))
        with CATEGORY_LOCK:
            CATEGORY_MAP['movie'] = movie_cats
            CATEGORY_MAP['series'] = series_cats
            CATEGORY_MAP['tv'] = live_cats

    with CATEGORY_LOCK:
        movie_genres = list(CATEGORY_MAP.get('movie', {}).keys())
        series_genres = list(CATEGORY_MAP.get('series', {}).keys())
        live_genres = list(CATEGORY_MAP.get('tv', {}).keys())

    types = ['movie', 'series', 'tv']
    return jsonify({
        'id': ADDON_ID,
        'name': ADDON_NAME,
        'version': ADDON_VERSION,
        'description': ADDON_DESCRIPTION,
        'logo': ICON_URL,
        'types': types,
        'resources': [
            'catalog',
            # meta so para ids do proprio addon (tt* continua com o Cinemeta)
            {'name': 'meta', 'types': types, 'idPrefixes': ['bsplay']},
            {'name': 'stream', 'types': types, 'idPrefixes': ['bsplay', 'tt', 'tmdb']},
        ],
        'behaviorHints': {'configurable': False, 'configurationRequired': False},
        'catalogs': [
            {
                'id': 'bsplay_movie', 'type': 'movie', 'name': 'bsPlay - Filmes',
                'extra': [
                    {'name': 'genre', 'isRequired': False, 'options': movie_genres},
                    {'name': 'search', 'isRequired': False},
                    {'name': 'skip', 'isRequired': False}
                ]
            },
            {
                'id': 'bsplay_series', 'type': 'series', 'name': 'bsPlay - Séries',
                'extra': [
                    {'name': 'genre', 'isRequired': False, 'options': series_genres},
                    {'name': 'search', 'isRequired': False},
                    {'name': 'skip', 'isRequired': False}
                ]
            },
            {
                'id': 'bsplay_tv', 'type': 'tv', 'name': 'bsPlay - Canais de TV',
                'extra': [
                    {'name': 'genre', 'isRequired': False, 'options': live_genres},
                    {'name': 'search', 'isRequired': False},
                    {'name': 'skip', 'isRequired': False}
                ]
            }
        ]
    })


# ========== CATALOGO ==========
@app.route('/catalog/<content_type>/<catalog_id>.json')
@app.route('/catalog/<content_type>/<catalog_id>/<path:extra>.json')
def catalog(content_type, catalog_id, extra=None):
    genre = ''
    search = ''
    skip = 0

    if extra:
        for part in extra.split('&'):
            if '=' in part:
                key, value = part.split('=', 1)
                key = key.strip().lower()
                value = unquote(value)
                if key == 'genre':
                    genre = value
                elif key == 'search':
                    search = value
                elif key == 'skip':
                    try:
                        skip = int(value)
                    except ValueError:
                        skip = 0

    if not genre:
        genre = request.args.get('genre', '')
    if not search:
        search = request.args.get('search', '')

    real_genre = resolve_category(genre, content_type) if genre else ''

    if content_type == 'movie':
        action, cat_action = 'get_vod_streams', 'get_vod_categories'
    elif content_type == 'series':
        action, cat_action = 'get_series', 'get_series_categories'
    else:
        action, cat_action = 'get_live_streams', 'get_live_categories'

    metas = []

    if search:
        items = get_xcatalog(action)  # cacheado e enxuto
        cleaned = clean_text(search)
        for item in items:
            if len(metas) >= 65:
                break
            name = item.get('name') or item.get('title', '')
            if cleaned in clean_text(name):
                metas.append(map_item(item, content_type))
    elif real_genre:
        cats = xc_request(cat_action)
        cat_id = None
        if isinstance(cats, list):
            for cat in cats:
                cat_name = cat.get('category_name', '')
                if (cat_name == real_genre
                        or cat_name.lower() == real_genre.lower()
                        or clean_text(cat_name) == clean_text(real_genre)):
                    cat_id = cat.get('category_id')
                    break
        if cat_id:
            items = xc_request(action, {'category_id': cat_id})
            if isinstance(items, list):
                for item in items[skip:skip + 100]:
                    metas.append(map_item(item, content_type))
            return jsonify({'metas': metas})
    else:
        cats = xc_request(cat_action)
        if isinstance(cats, list) and cats:
            first_cat = cats[0].get('category_id')
            if first_cat:
                items = xc_request(action, {'category_id': first_cat})
                if isinstance(items, list):
                    for item in items[skip:skip + 100]:
                        metas.append(map_item(item, content_type))
                return jsonify({'metas': metas})

    if skip > 0:
        metas = metas[skip:]
    return jsonify({'metas': metas[:100]})


# ========== META ==========
@app.route('/meta/<content_type>/<id>.json')
def meta(content_type, id):
    match = re.search(r'bsplay:(?:movie|series|tv):(\d+)', id)
    if not match:
        return jsonify({'meta': None})

    numeric_id = match.group(1)

    if content_type == 'movie':
        detail = xc_request('get_vod_info', {'vod_id': numeric_id})
        if not isinstance(detail, dict) or not detail.get('info'):
            return jsonify({'meta': None})
        info = detail['info']

        poster = info.get('cover_big') or info.get('cover') or info.get('movie_image', '')
        background = first_or_str(info.get('backdrop_path'), poster)

        poster = proxify_image_url(poster)
        background = proxify_image_url(background)

        year = None
        for field in ['year', 'releasedate', 'releaseDate']:
            if info.get(field):
                try:
                    year = int(str(info[field])[:4])
                    break
                except Exception:
                    pass

        rating = None
        for field in ['rating', 'rating_5based']:
            if info.get(field):
                try:
                    rating = float(info[field])
                    break
                except Exception:
                    pass

        genres = [g.strip() for g in info['genre'].split(',')] if info.get('genre') else []

        meta_data = {
            'id': f"bsplay:movie:{numeric_id}",
            'type': 'movie',
            'name': info.get('name') or info.get('title', 'Filme'),
            'description': info.get('plot') or info.get('description', ''),
            'poster': poster,
            'background': background,
            'posterShape': 'poster',
            'releaseInfo': str(year) if year else '',
            'year': year,
            'imdbRating': rating,
            'genres': genres
        }

    elif content_type == 'series':
        detail = xc_request('get_series_info', {'series_id': numeric_id})
        if not isinstance(detail, dict) or not detail.get('info'):
            return jsonify({'meta': None})
        info = detail['info']

        poster = info.get('cover') or info.get('cover_big', '')
        background = first_or_str(info.get('backdrop_path'), poster)

        poster = proxify_image_url(poster)
        background = proxify_image_url(background)

        year = None
        for field in ['year', 'releaseDate', 'release_date']:
            if info.get(field):
                try:
                    year = int(str(info[field])[:4])
                    break
                except Exception:
                    pass

        try:
            rating = float(info['rating']) if info.get('rating') else None
        except (ValueError, TypeError):
            rating = None
        genres = [g.strip() for g in info['genre'].split(',')] if info.get('genre') else []

        videos = []
        episodes = detail.get('episodes', {})
        if isinstance(episodes, dict):
            for season_num, eps in episodes.items():
                if not isinstance(eps, list):
                    continue
                try:
                    season = int(season_num)
                except (ValueError, TypeError):
                    continue
                for ep in eps:
                    try:
                        ep_num = int(ep.get('episode_num', 0))
                    except (ValueError, TypeError):
                        ep_num = 0
                    ep_info = ep.get('info') or {}
                    thumb = ep_info.get('movie_image') or ep_info.get('cover_big') or ep_info.get('cover', '')
                    thumb = proxify_image_url(thumb)
                    videos.append({
                        'id': f"bsplay:series:{numeric_id}:{season}:{ep_num}",
                        'title': ep.get('title', f"Episode {ep_num}"),
                        'season': season,
                        'episode': ep_num,
                        'thumbnail': thumb,
                        'overview': ep_info.get('plot', '')
                    })

        videos.sort(key=lambda x: (x['season'], x['episode']))

        meta_data = {
            'id': f"bsplay:series:{numeric_id}",
            'type': 'series',
            'name': info.get('name') or info.get('title', 'Série'),
            'description': info.get('plot') or info.get('description', ''),
            'poster': poster,
            'background': background,
            'posterShape': 'poster',
            'releaseInfo': str(year) if year else '',
            'year': year,
            'imdbRating': rating,
            'genres': genres,
            'videos': videos
        }

    else:
        channel = None
        for s in get_xcatalog('get_live_streams'):  # cacheado, nao baixa tudo a cada request
            if str(s.get('stream_id')) == numeric_id:
                channel = s
                break
        if not channel:
            return jsonify({'meta': None})

        meta_data = {
            'id': f"bsplay:tv:{numeric_id}",
            'type': 'tv',
            'name': channel.get('name', 'Canal'),
            'description': 'bsPlay Canal ao Vivo',
            'poster': proxify_image_url(channel.get('stream_icon', '')),
            'posterShape': 'square'
        }

    meta_data = {k: v for k, v in meta_data.items() if v not in [None, '', []]}
    return jsonify({'meta': meta_data})


# ========== BUSCA DE STREAMS POR IMDB ID ==========
def normalize_text(text):
    if not text:
        return ''
    text = text.lower()
    accents = {
        'á': 'a', 'à': 'a', 'â': 'a', 'ã': 'a', 'ä': 'a',
        'é': 'e', 'è': 'e', 'ê': 'e', 'ë': 'e',
        'í': 'i', 'ì': 'i', 'î': 'i', 'ï': 'i',
        'ó': 'o', 'ò': 'o', 'ô': 'o', 'õ': 'o', 'ö': 'o',
        'ú': 'u', 'ù': 'u', 'û': 'u', 'ü': 'u',
        'ç': 'c', 'ñ': 'n'
    }
    for k, v in accents.items():
        text = text.replace(k, v)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def calculate_similarity(text1, text2):
    norm1 = normalize_text(text1)
    norm2 = normalize_text(text2)
    set1 = set(norm1.split(' '))
    set2 = set(norm2.split(' '))
    union = set1 | set2
    if not union:
        return 0
    jaccard = len(set1 & set2) / len(union)
    matches = 0
    min_len = min(len(norm1), len(norm2))
    for i in range(min_len):
        if norm1[i] == norm2[i]:
            matches += 1
    max_len = max(len(norm1), len(norm2))
    sequence_sim = matches / max_len if max_len else 0
    return (jaccard * 0.7) + (sequence_sim * 0.3)


def extract_title_from_name(name):
    title = re.sub(r'\s*(?:HD|FHD|4K|CAM|HDCAM|TS|WEB|WEB-DL|BluRay|DVD|HDRip|BDRip|480p|720p|1080p|2160p)\s*',
                   ' ', name, flags=re.IGNORECASE)
    title = re.sub(r'\s*[\[\(]\d{4}[\]\)]\s*', ' ', title)
    title = re.sub(r'\s*S\d{1,2}E\d{1,2}\s*', ' ', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*Temporada\s*\d+\s*', ' ', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*Epis[oó]dio\s*\d+\s*', ' ', title, flags=re.IGNORECASE)
    # corta so " - algo" (hifen com espaco dos dois lados), preserva "Spider-Man"
    title = re.sub(r'\s+-\s+.*$', ' ', title)
    title = re.sub(r'\s*\[.*?\]\s*', ' ', title)
    title = re.sub(r'\s*\(L\)\s*', ' ', title, flags=re.IGNORECASE)
    return title.strip()


def extract_year_from_name(name):
    match = re.search(r'[\[\(](\d{4})[\]\)]', name)
    return match.group(1) if match else ''


def extract_quality(name):
    name_lower = name.lower()
    if re.search(r'\b4k\b|2160', name_lower):
        return '4K'
    if re.search(r'\bfhd\b|full hd|1080', name_lower):
        return 'FHD'
    if re.search(r'\bhd\b|720', name_lower):
        return 'HD'
    if re.search(r'\b(cam|hdcam|cinema|ts)\b', name_lower):
        return 'CAM'
    return 'SD'


def is_subtitled(name):
    name_lower = name.lower()
    return bool(re.search(r'\[l\]|-\s*l\b|\(l\)|legendado', name_lower))


def get_video_quality_from_info(info):
    if not info:
        return 'SD'
    video = info.get('video') or {}
    width = video.get('width') or 0
    height = video.get('height') or 0
    if width >= 3840 or height >= 2160:
        return '4K'
    if width >= 1920 or height >= 1080:
        return 'FHD'
    if width >= 1280 or height >= 720:
        return 'HD'
    return 'SD'


def get_movie_search_terms(title, original_title):
    terms = {}
    if title:
        normalized_title = title.strip()
        terms[normalized_title.lower()] = {'term': normalized_title, 'weight': 1.0, 'source': 'title_pt'}
        no_accent = normalize_text(normalized_title)
        if no_accent != normalized_title.lower():
            terms[no_accent] = {'term': no_accent, 'weight': 0.98, 'source': 'title_pt_normalized'}
    if original_title and original_title != title:
        normalized_original = original_title.strip()
        terms[normalized_original.lower()] = {'term': normalized_original, 'weight': 0.95, 'source': 'title_en'}
    return sorted(terms.values(), key=lambda t: t['weight'], reverse=True)


def calculate_match_score(stream_title, stream_year, search_term, target_year):
    normalized_stream = normalize_text(stream_title)
    normalized_search = normalize_text(search_term['term'])

    if target_year and stream_year and str(target_year) != str(stream_year):
        return 0

    year_bonus = 0
    if target_year and not stream_year:
        year_bonus = -0.1
    elif target_year and stream_year and str(target_year) == str(stream_year):
        year_bonus = 0.05

    if len(normalized_search) < 3:
        return 0

    if normalized_stream == normalized_search:
        return min(1.0, 1.0 + year_bonus)

    stream_words = [w for w in normalized_stream.split(' ') if len(w) > 2]
    search_words = [w for w in normalized_search.split(' ') if len(w) > 2]

    if search_words and stream_words:
        if all(any(t == s for t in stream_words) for s in search_words):
            extra_words = len(stream_words) - len(search_words)
            if extra_words == 0:
                return min(0.97, 0.97 + year_bonus)
            if extra_words <= 1:
                return min(0.92, 0.92 + year_bonus)

    similarity = calculate_similarity(normalized_search, normalized_stream)
    if similarity > 0.7:
        return min(0.85, 0.7 + (similarity - 0.7) * 0.5 + year_bonus)

    return 0


def get_tmdb_info(imdb_id, media_type):
    is_series = media_type in ('tv', 'series')
    cache_key = f"tmdb_{imdb_id}_{media_type}"

    cached = cache_get(TMDB_CACHE, cache_key, TMDB_CACHE_TTL)
    if cached is not None:
        return cached

    final_id = None
    if str(imdb_id).startswith('tt'):
        try:
            find_resp = requests.get(
                f"{TMDB_BASE_URL}/find/{imdb_id}",
                params={'api_key': TMDB_API_KEY, 'external_source': 'imdb_id', 'language': 'pt-BR'},
                headers=HEADERS, timeout=8, verify=False
            )
            find_data = find_resp.json() if find_resp.status_code == 200 else None
        except Exception:
            find_data = None

        if find_data:
            if is_series:
                results = find_data.get('tv_results') or find_data.get('movie_results') or []
            else:
                results = find_data.get('movie_results') or find_data.get('tv_results') or []
            if results:
                final_id = results[0].get('id')

        if not final_id:
            return None
    else:
        final_id = imdb_id

    endpoint = 'tv' if is_series else 'movie'
    try:
        detail_resp = requests.get(
            f"{TMDB_BASE_URL}/{endpoint}/{final_id}",
            params={'api_key': TMDB_API_KEY, 'language': 'pt-BR'},
            headers=HEADERS, timeout=8, verify=False
        )
        data = detail_resp.json() if detail_resp.status_code == 200 else None
    except Exception:
        data = None

    if not data:
        return None

    if is_series:
        result = {
            'title': data.get('name') or '',
            'year': (data.get('first_air_date') or '')[:4],
            'type': 'series',
            'tmdb_id': data.get('id'),
            'original_title': data.get('original_name') or '',
        }
    else:
        result = {
            'title': data.get('title') or '',
            'year': (data.get('release_date') or '')[:4],
            'type': 'movie',
            'tmdb_id': data.get('id'),
            'original_title': data.get('original_title') or '',
        }

    cache_set(TMDB_CACHE, cache_key, result)
    return result


def _search_catalog(action, id_field, title, year, original_title):
    search_terms = get_movie_search_terms(title, original_title)
    data = get_xcatalog(action)

    if not data or not search_terms:
        return []

    matches = []
    for stream in data:
        name = stream.get('name') or ''
        if not name:
            continue
        stream_title = extract_title_from_name(name)
        stream_year = extract_year_from_name(name)
        best_score = 0
        for search_term in search_terms:
            score = calculate_match_score(stream_title, stream_year, search_term, year)
            if score > best_score:
                best_score = score
        if best_score >= STREAM_MIN_MATCH_SCORE:
            item_id = stream.get(id_field)
            if item_id:
                matches.append({
                    'name': name,
                    'title': stream_title,
                    'year': stream_year,
                    'quality': extract_quality(name),
                    'id': item_id,
                    'similarity': best_score,
                    'subtitled': is_subtitled(name)
                })

    matches.sort(key=lambda m: m['similarity'], reverse=True)
    return matches


def search_movie_streams(title, year, original_title):
    return _search_catalog('get_vod_streams', 'stream_id', title, year, original_title)


def search_series_streams(title, year, original_title):
    return _search_catalog('get_series', 'series_id', title, year, original_title)


def find_episode(series_info, season, episode):
    if not isinstance(series_info, dict):
        return None
    episodes = series_info.get('episodes') or {}
    if not isinstance(episodes, dict):
        return None
    season_eps = episodes.get(str(season)) or episodes.get(int(season)) or []
    for ep in season_eps:
        if str(ep.get('episode_num')) == str(episode):
            return ep
    return None


def create_formatted_title(title, year, quality, is_series, season, episode, subtitled):
    quality_emoji = QUALITY_EMOJIS.get(quality, '📺')
    quality_label = QUALITY_LABELS.get(quality, quality)
    sub_tag = ' [LEGENDADO]' if subtitled else ''

    display_title = title
    if is_series and season and episode:
        display_title = f"{title} S{int(season):02d}E{int(episode):02d}"

    return (f"{quality_emoji} {display_title}{sub_tag} ({year or '????'})\n"
            f"⚡ bsPlay\n🎯 {quality_label}")


def imdb_get_streams(imdb_id, media_type, season=None, episode=None):
    is_series = media_type == 'series'
    try:
        season_num = int(season) if season else 1
        episode_num = int(episode) if episode else 1
    except ValueError:
        season_num, episode_num = 1, 1

    # base calculada aqui (thread da request); as threads abaixo nao tem request context
    base = get_base_url()

    cache_key = f"{base}_{imdb_id}_{media_type}_{season_num}_{episode_num}"
    cached = cache_get(STREAM_CACHE, cache_key, STREAM_CACHE_TTL)
    if cached is not None:
        return cached

    tmdb_info = get_tmdb_info(imdb_id, media_type)
    if not tmdb_info:
        cache_set(STREAM_CACHE, cache_key, [])
        return []

    title = tmdb_info.get('title') or ''
    year = tmdb_info.get('year') or ''
    original_title = tmdb_info.get('original_title') or ''

    if is_series:
        matches = search_series_streams(title, year, original_title)
    else:
        matches = search_movie_streams(title, year, original_title)

    if not matches:
        cache_set(STREAM_CACHE, cache_key, [])
        return []

    matches.sort(key=lambda m: (QUALITY_ORDER.get(m['quality'], 0), m['similarity']), reverse=True)

    seen_ids = set()
    unique_matches = []
    for match in matches[:STREAM_MAX_CANDIDATES]:
        if match['id'] in seen_ids:
            continue
        seen_ids.add(match['id'])
        unique_matches.append(match)

    def build_stream_entry(match):
        try:
            quality = match['quality']

            if is_series:
                detail = xc_request('get_series_info', {'series_id': match['id']})
                ep = find_episode(detail, season_num, episode_num)
                if not ep:
                    return None
                ep_quality = get_video_quality_from_info(ep.get('info'))
                if ep_quality != 'SD':
                    quality = ep_quality
                stream_url = build_stream_url('series', match['id'], ep.get('id'), base=base)
            else:
                stream_url = build_stream_url('movie', match['id'], base=base)

            formatted_title = create_formatted_title(
                title, year, quality, is_series,
                season_num, episode_num, match.get('subtitled', False)
            )
            return {
                'name': ADDON_NAME,
                'title': formatted_title,
                'url': stream_url,
                'behaviorHints': {
                    'notWebReady': True,
                    'bingeGroup': f"bsplay-{'series' if is_series else 'movie'}:{imdb_id}"
                }
            }
        except Exception:
            return None

    if is_series:
        with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as executor:
            built = list(executor.map(build_stream_entry, unique_matches))
    else:
        built = [build_stream_entry(m) for m in unique_matches]

    streams = [b for b in built if b][:STREAM_MAX_RESULTS]

    if streams:
        cache_set(STREAM_CACHE, cache_key, streams)

    return streams


@app.route('/stream/<content_type>/<id>.json')
def stream(content_type, id):
    raw_id = (id or '').strip()

    if raw_id.startswith('tt') or raw_id.lower().startswith('tmdb:'):
        season = request.args.get('season')
        episode = request.args.get('episode')

        parts = raw_id.split(':')
        if parts[0].lower() == 'tmdb':
            content_id = parts[1] if len(parts) > 1 else ''
            if len(parts) > 2:
                season = parts[2]
            if len(parts) > 3:
                episode = parts[3]
        else:
            content_id = parts[0]
            if len(parts) > 1:
                season = parts[1]
            if len(parts) > 2:
                episode = parts[2]

        if content_type in ('movie', 'series') and content_id:
            streams = imdb_get_streams(content_id, content_type, season, episode)
            return jsonify({'streams': streams, 'cacheMaxAge': STREAM_CACHE_TTL})
        return jsonify({'streams': []})

    parts = id.split(':')
    if len(parts) < 3:
        return jsonify({'streams': []})

    numeric_id = parts[2]

    if content_type == 'movie':
        stream_url = build_stream_url('movie', numeric_id)
        title = 'Filme (Direto)'
    elif content_type == 'tv':
        stream_url = build_stream_url('tv', numeric_id)
        title = 'Canais ao Vivo (Direto)'
    else:
        season = parts[3] if len(parts) > 3 else ''
        episode = parts[4] if len(parts) > 4 else ''

        detail = xc_request('get_series_info', {'series_id': numeric_id})
        episode_id = ''
        if season and episode:
            ep = find_episode(detail, season, episode)
            if ep:
                episode_id = ep.get('id', '')

        stream_url = build_stream_url('series', numeric_id, episode_id or numeric_id)
        title = 'Série (Direto)'

    return jsonify({'streams': [{'name': 'bsPlay', 'title': title, 'url': stream_url}]})


# ========== PROXY (sem cache em RAM: so repassa o stream) ==========
def detect_mp4(url):
    lower = url.lower()
    if any(ext in lower for ext in ['.mp4', '.mkv', '.webm', '.f4v', '.mov', '.avi']):
        return True
    if '/play/' in lower:
        return True
    if '/movie/' in lower or '/series/' in lower:
        return True
    return False


def fetch_with_fallback(url, range_header=None, max_retries=PROXY_MAX_RETRIES):
    for attempt in range(max_retries):
        user_agent = HEADERS['User-Agent'] if attempt == 0 else get_random_user_agent()
        req_headers = {
            'User-Agent': user_agent,
            'Accept': '*/*',
            'Accept-Language': 'pt-BR,pt;q=0.9',
            'Accept-Encoding': 'identity',
            'Connection': 'keep-alive'
        }
        if range_header:
            req_headers['Range'] = range_header

        try:
            resp = requests.get(url, headers=req_headers, timeout=15, stream=True, verify=False)
            if resp.status_code not in (200, 206):
                code = resp.status_code
                resp.close()
                if attempt < max_retries - 1:
                    time.sleep(PROXY_RETRY_DELAY * (attempt + 1))
                    continue
                return None, code
            return resp, resp.status_code
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(PROXY_RETRY_DELAY * (attempt + 1))
                continue
            return None, 0
    return None, 0


def rewrite_m3u8_urls(playlist_content, base_url, proxy_base):
    """Reescreve cada linha de segmento/playlist pra passar pelo /tsdownloader (mantem o esquema https)."""
    def replace_url(match):
        segment = match.group(0).strip()
        if segment.startswith('#') or not segment:
            return segment
        try:
            absolute = urljoin(base_url + '/', segment)
            return f"{proxy_base}/tsdownloader?url={quote(absolute, safe='')}"
        except Exception:
            return segment

    return re.sub(r'^(?!#)\S+', replace_url, playlist_content, flags=re.MULTILINE)


def handle_channel_proxy(url, range_header=None):
    response, status_code = fetch_with_fallback(url, range_header)

    if response is None:
        return Response('Upstream unavailable', status=status_code or 502, headers=cors_headers())

    content_type = response.headers.get('content-type', '').lower()
    content_url = response.url

    it = response.iter_content(chunk_size=PROXY_BUFFER_SIZE)
    try:
        first = next(it, b'')
    except Exception:
        first = b''

    is_m3u8 = (
        'mpegurl' in content_type
        or urlparse(content_url).path.lower().endswith('.m3u8')
        or first.lstrip()[:7] == b'#EXTM3U'
    )

    if is_m3u8:
        parts = [first]
        size = len(first)
        try:
            for chunk in it:
                size += len(chunk)
                if size > M3U8_MAX_BYTES:
                    break
                parts.append(chunk)
        except Exception:
            pass
        finally:
            response.close()

        playlist_text = b''.join(parts).decode('utf-8', errors='ignore')
        base_url = content_url.rsplit('/', 1)[0]
        rewritten = rewrite_m3u8_urls(playlist_text, base_url, get_base_url()).encode('utf-8')

        return Response(rewritten, status=200, headers={
            **cors_headers(),
            'Content-Type': 'application/vnd.apple.mpegurl',
            'Content-Length': str(len(rewritten)),
            'Cache-Control': 'no-cache'
        })

    def generate_ts():
        try:
            if first:
                yield first
            for chunk in it:
                if chunk:
                    yield chunk
        except Exception:
            pass
        finally:
            try:
                response.close()
            except Exception:
                pass

    resp_headers = {
        **cors_headers(),
        'Content-Type': 'video/mp2t',
        'Cache-Control': 'no-cache',
    }
    if status_code == 206:
        for h in ('Content-Range', 'Content-Length'):
            if response.headers.get(h):
                resp_headers[h] = response.headers[h]

    return Response(stream_with_context(generate_ts()), status=status_code, headers=resp_headers)


def fetch_mp4(url, range_header=None, method='GET', max_retries=PROXY_MAX_RETRIES):
    parsed = urlparse(url)
    headers = {
        'User-Agent': HEADERS['User-Agent'],
        'Accept': 'video/mp4,video/*;q=0.9,*/*;q=0.8',
        'Accept-Language': 'pt-BR,pt;q=0.9',
        'Accept-Encoding': 'identity',
        'Connection': 'keep-alive',
        'Referer': f"{parsed.scheme}://{parsed.netloc}/",
        'Origin': f"{parsed.scheme}://{parsed.netloc}",
    }
    if range_header:
        headers['Range'] = range_header

    for attempt in range(max_retries):
        try:
            return requests.request(method, url, headers=headers, timeout=30, stream=True, verify=False)
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(PROXY_RETRY_DELAY)
    return None


def handle_mp4_proxy(url, range_header, method):
    upstream = fetch_mp4(url, range_header=range_header, method=method)
    if upstream is None:
        return Response('Failed to connect to origin', status=503, headers=cors_headers())

    status = upstream.status_code
    if status >= 400:
        upstream.close()
        return Response('Upstream error', status=status, headers=cors_headers())

    headers = {
        **cors_headers(),
        'Content-Type': upstream.headers.get('Content-Type', 'video/mp4'),
        'Accept-Ranges': upstream.headers.get('Accept-Ranges', 'bytes'),
    }
    for h in ('Content-Length', 'Content-Range', 'Last-Modified', 'ETag'):
        if upstream.headers.get(h):
            headers[h] = upstream.headers[h]

    if method == 'HEAD':
        upstream.close()
        return Response('', status=status, headers=headers)

    def generate_mp4():
        try:
            for chunk in upstream.iter_content(chunk_size=PROXY_BUFFER_SIZE):
                if chunk:
                    yield chunk
        except Exception:
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    return Response(stream_with_context(generate_mp4()), status=status, headers=headers)


# --- rotas opacas: o cliente nunca ve usuario/senha ---
@app.route('/live/<int:stream_id>.m3u8')
def live_channel(stream_id):
    upstream = f"{XC_HOST}/live/{XC_USERNAME}/{XC_PASSWORD}/{stream_id}.m3u8"
    return handle_channel_proxy(upstream, request.headers.get('Range'))


@app.route('/vod/<kind>/<int:item_id>.mp4')
def vod(kind, item_id):
    if kind not in ('movie', 'series'):
        return Response('Not found', status=404)
    upstream = f"{XC_HOST}/{kind}/{XC_USERNAME}/{XC_PASSWORD}/{item_id}.mp4"
    return handle_mp4_proxy(upstream, request.headers.get('Range'), request.method)


# --- rotas por ?url= (compatibilidade), agora com protecao SSRF ---
@app.route('/proxy/')
@app.route('/proxy/<path:path>')
def proxy_root(path=''):
    url = request.args.get('url')
    if not url:
        return 'Missing url parameter', 400
    if not is_safe_url(url):
        return Response('Forbidden url', status=403, headers=cors_headers())

    range_header = request.headers.get('Range')
    if detect_mp4(url):
        return handle_mp4_proxy(url, range_header, request.method)
    return handle_channel_proxy(url, range_header)


@app.route('/tsdownloader')
def proxy_tsdownloader():
    url = request.args.get('url')
    if not url:
        return Response('Missing url parameter', status=400, headers=cors_headers())
    if not is_safe_url(url):
        return Response('Forbidden url', status=403, headers=cors_headers())
    return handle_channel_proxy(url, request.headers.get('Range'))


@app.route('/<path:path>')
def catch_all(path):
    return jsonify({'error': 'Rota não encontrada'}), 404


if __name__ == '__main__':
    # producao: gunicorn -w 1 -k gthread --threads 8 -b 0.0.0.0:$PORT app:app
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 80)), threaded=True)
