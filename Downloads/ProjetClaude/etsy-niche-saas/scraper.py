"""
Scraping Etsy (0 credit API) — SESSION ASYNC PERSISTANTE.
Avant: chaque fetch relancait un navigateur (~9s/boutique => 1h30 pour 574).
Maintenant: UN seul navigateur (AsyncStealthySession) avec un pool de pages, et on
charge les boutiques EN PARALLELE via asyncio (~1.5s/boutique). Headless impossible
(403 Datadome) => fenetres deplacees hors ecran par start_window_hider().

API publique (sync, utilisable depuis etsy_core):
  scrape_search_shops(keyword, pages, page_start) -> [(shop_name, sample_title)]
  scrape_shops_batch(names, wait)                 -> {name: {sold, months, titles}|{error}}
"""
import re, time, sys, threading, asyncio

try:
    from scrapling.fetchers import AsyncStealthySession
    SCRAPLING_OK = True
except Exception:
    SCRAPLING_OK = False

SESSION_PAGES = 6          # pages paralleles dans le navigateur (concurrence)
SEARCH_WAIT = 4000         # attente JS page recherche (challenge anti-bot)
SHOP_WAIT = 2000           # attente JS page boutique

# ---- boucle asyncio dediee (thread daemon) -----------------------------------
_loop = None
_session = None
_loop_lock = threading.Lock()

def _ensure_loop():
    global _loop
    if _loop is not None:
        return
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()

def _run(coro):
    """Execute une coroutine dans la boucle dediee et rend le resultat (sync)."""
    _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()

async def _get_session():
    global _session
    if _session is None:
        _session = AsyncStealthySession(headless=False, network_idle=True, max_pages=SESSION_PAGES)
        await _session.start()
        start_window_hider()   # cache la/les fenetre(s) du navigateur
    return _session

async def _afetch(url, wait, retries=2, network_idle=True):
    """Fetch via la session; 1 retry si 403 transitoire (Datadome).
    Timeout DUR par essai: une page Etsy peut ne jamais atteindre network_idle
    (pub/spinner infini) => sans timeout, asyncio.gather gele tout le batch.
    network_idle=False (+load_dom) pour les pages boutique: rapide et sans hang."""
    s = await _get_session()
    hard = wait / 1000.0 + 18   # secondes: attente JS + marge chargement
    p = None
    for _ in range(retries):
        try:
            p = await asyncio.wait_for(
                s.fetch(url, wait=wait, timeout=int(hard * 1000),
                        network_idle=network_idle, load_dom=True),
                timeout=hard + 5)
        except asyncio.TimeoutError:
            p = None
        except Exception:
            p = None
        if p is not None and getattr(p, "status", 0) == 200:
            return p
        await asyncio.sleep(0.6)
    return p

# ---- masquage fenetres navigateur (Windows) ----------------------------------
_HIDER_STARTED = False
def start_window_hider():
    global _HIDER_STARTED
    if _HIDER_STARTED or not sys.platform.startswith("win"):
        return
    _HIDER_STARTED = True
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
    # scrapling pilote "Google Chrome for Testing" (classe Chrome_WidgetWin_1).
    # On NE deplace QUE les fenetres dont le titre contient ce marqueur, pour ne
    # pas toucher les autres fenetres Chrome de l'utilisateur.
    MARK = "Google Chrome for Testing"
    moved = set()
    def _scan():
        def cb(hwnd, lparam):
            if hwnd in moved:
                return True
            ttl = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, ttl, 256)
            if MARK in (ttl.value or ""):
                moved.add(hwnd)
                user32.SetWindowPos(hwnd, 0, -32000, -32000, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    def worker():
        while True:
            try: _scan()
            except Exception: pass
            time.sleep(0.2)
    threading.Thread(target=worker, daemon=True).start()

# ---- extraction --------------------------------------------------------------
def _shop_from_href(href):
    m = re.search(r"/shop/([A-Za-z0-9_]+)", href or "")
    return m.group(1) if m else ""

def _parse_search(p):
    """[(shop_name, sample_title)] alignes par carte."""
    out, seen = [], set()
    if p is None or getattr(p, "status", 0) != 200:
        return out
    cards = p.css("div.v2-listing-card, li.wt-list-unstyled, div[data-listing-id]")
    for c in cards:
        title, name = "", ""
        for a in c.css('a[href*="/listing/"]'):
            title = " ".join((a.attrib.get("title") or a.text or "").split())
            if title: break
        for a in c.css('a[href*="/shop/"]'):
            name = _shop_from_href(a.attrib.get("href"))
            if name: break
        if not name:
            for e in c.css('p.v2-listing-card__shop, span[class*=shop]'):
                name = " ".join(e.text.split())
                if name: break
        if name and name not in seen:
            seen.add(name); out.append((name, title))
    return out

_SALES_RE = [
    re.compile(r"([\d,]+)\s+Sales?\s+([\d,]+)\s+Admirers", re.I),
    re.compile(r"([\d,]+)\s+Sales?\b", re.I),
    re.compile(r"([\d,]+)\s+ventes?\b", re.I),
]

def _parse_shop(p):
    if p is None:
        return {"error": "no response"}
    if getattr(p, "status", 0) != 200:
        return {"error": f"http {getattr(p,'status','?')}"}
    html = p.html_content
    txt = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    sold = None
    for rx in _SALES_RE:
        sm = rx.search(txt)
        if sm:
            sold = int(sm.group(1).replace(",", "")); break
    mo = re.search(r"(\d+)\s*months? on Etsy", txt, re.I)
    yo = re.search(r"(\d+)\s*years? on Etsy", txt, re.I)
    titles = []
    for a in p.css('a[href*="/listing/"]'):
        t = " ".join((a.attrib.get("title") or a.text or "").split())
        if t and len(t) > 5 and t not in titles:
            titles.append(t)
    months = mo.group(1) if mo else None
    years = yo.group(1) if yo else None
    return {"sold": sold,
            "months": int(months) if months else (int(years) * 12 if years else None),
            "is_under_1y": months is not None,
            "titles": titles[:30]}

# ---- API sync ----------------------------------------------------------------
def scrape_search_shops(keyword, pages=1, page_start=1):
    """Pages de recherche -> [(shop_name, sample_title)]. 0 API."""
    out, seen = [], set()
    async def go():
        res = []
        for pg in range(page_start, page_start + pages):
            kw = keyword.strip().replace(" ", "+") or "handmade"
            url = f"https://www.etsy.com/search?q={kw}&page={pg}&ref=search"
            try:
                p = await _afetch(url, SEARCH_WAIT)
            except Exception:
                continue
            res.extend(_parse_search(p))
        return res
    if not SCRAPLING_OK:
        return out
    for nm, sample in _run(go()):
        if nm and nm not in seen:
            seen.add(nm); out.append((nm, sample))
    return out

def scrape_shops_batch(names, wait=SHOP_WAIT):
    """Charge plusieurs boutiques EN PARALLELE (1 navigateur, pool de pages).
    Retourne {name: {sold, months, titles}|{error}}."""
    if not SCRAPLING_OK or not names:
        return {}
    async def go():
        async def one(n):
            try:
                p = await _afetch(f"https://www.etsy.com/shop/{n}", wait, network_idle=False)
                return n, _parse_shop(p)
            except Exception as e:
                return n, {"error": str(e)[:50]}
        return dict(await asyncio.gather(*[one(n) for n in names]))
    return _run(go())

def scrape_shop(name):
    """Compat: 1 boutique."""
    return scrape_shops_batch([name]).get(name, {"error": "no response"})

_ali_warm = {"done": False}
def scrape_ali_search(query):
    """Recherche texte AliExpress via la session scrapling (camoufox furtif).
    Rechauffe la session (visite accueil) pour limiter le captcha x5sec.
    Retourne ([titres], blocked: bool). blocked=True si AliExpress nous captcha."""
    if not SCRAPLING_OK:
        return [], False
    import re as _re, urllib.parse as _up
    slug = _re.sub(r"[^a-z0-9]+", "-", (query or "").lower()).strip("-") or "gift"
    url = "https://www.aliexpress.com/w/wholesale-" + slug + ".html?SearchText=" + _up.quote(query or "")
    async def go():
        if not _ali_warm["done"]:
            try: await _afetch("https://www.aliexpress.com", 4000, retries=1)
            except Exception: pass
            _ali_warm["done"] = True
        p = await _afetch(url, 6000, retries=1)
        if p is None:
            return [], False
        blocked = ("punish" in (getattr(p, "url", "") or "")) or ("_____tmd_____" in (getattr(p, "url", "") or ""))
        if blocked:
            return [], True
        titles = []
        for a in p.css('a[href*="/item/"]'):
            t = " ".join((a.text or "").split())
            if t and len(t) > 12 and t not in titles:
                titles.append(t)
        return titles[:10], False
    try:
        return _run(go())
    except Exception:
        return [], False

def close_session():
    global _session
    if _session is not None and _loop is not None:
        try: _run(_session.close())
        except Exception: pass
        _session = None
