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
import re, time, sys, threading, asyncio, os

try:
    from scrapling.fetchers import AsyncStealthySession
    SCRAPLING_OK = True
except Exception:
    SCRAPLING_OK = False

# VITESSE: concurrence + attentes reglables par env (gros volume => monter SCRAPE_PAGES, baisser
# les waits). Defauts releves pour scraper 1000 boutiques en quelques minutes. ATTENTION: trop
# haut SANS proxies => Etsy/Datadome bloque (403). Avec proxies valides on peut pousser fort.
SESSION_PAGES = int(os.environ.get("SCRAPE_PAGES", "16"))   # pages paralleles (concurrence)
SEARCH_WAIT = int(os.environ.get("SCRAPE_SEARCH_WAIT", "2600"))  # attente JS page recherche
SHOP_WAIT = int(os.environ.get("SCRAPE_SHOP_WAIT", "1100"))      # attente JS page boutique

# ---- anti-blocage Datadome: rotation de session + proxies optionnels --------------
# Datadome bloque par IP + cookie de session. En recreant periodiquement le navigateur
# (nouveau fingerprint + nouveau cookie) on REMET A ZERO le compteur de blocage => on
# scrape beaucoup plus loin avant un vrai blocage. Avec des proxies, on tourne aussi l'IP
# (seule vraie solution pour de gros volumes type 1000 boutiques).
ROTATE_EVERY = 120        # fetches reussis avant rotation proactive (ajuste plus bas si proxies)
_fetch_count = 0
_block_streak = 0
_rotating = False
_proxy_idx = 0

def _norm_proxy(x):
    """Normalise un proxy vers 'http://user:pass@ip:port' (scheme OBLIGATOIRE pour
    camoufox/playwright, sinon il l'ignore => 0 resultat). Accepte les formats:
      ip:port:user:pass   (export Webshare)
      user:pass@ip:port
      ip:port
      http(s)://...        (laisse tel quel)"""
    x = (x or "").strip()
    if not x:
        return ""
    if x.startswith("http://") or x.startswith("https://"):
        return x
    if "@" in x:                                   # user:pass@ip:port
        return "http://" + x
    parts = x.split(":")
    if len(parts) == 4:                            # ip:port:user:pass
        ip, port, u, pw = parts
        return f"http://{u}:{pw}@{ip}:{port}"
    return "http://" + x                            # ip:port

def _load_proxies():
    """Proxies optionnels: env SCRAPE_PROXIES (virgules) ou config.local.json
    {\"proxies\": [...]}. Formats divers acceptes (voir _norm_proxy). Vide => pas de
    proxy (rotation de session seule)."""
    import os, json as _j
    from pathlib import Path
    raw = [x.strip() for x in os.environ.get("SCRAPE_PROXIES", "").split(",") if x.strip()]
    p = Path(__file__).parent / "config.local.json"
    if p.exists():
        try:
            d = _j.loads(p.read_text(encoding="utf-8-sig"))
            raw += [x for x in (d.get("proxies") or []) if x]
        except Exception:
            pass
    return [n for x in raw if (n := _norm_proxy(x))]
_PROXIES = _load_proxies()
# AVEC proxies: rotation plus frequente => cycle les IP, repartit la charge, aucune IP
# flaggee. Sans proxy: rotation ne change que le cookie => moins utile, on espace.
if _PROXIES:
    ROTATE_EVERY = 50

# ---- proxies GRATUITS (auto-recolte + validation) ---------------------------------
# Listes publiques de proxies gratuits. ATTENTION: ce sont des IP DATACENTER => Datadome
# (Etsy) en bloque la grande majorite. Utile en complement (volume) mais peu fiable. Les
# proxies RESIDENTIELS payants restent la seule voie vraiment fiable.
_FREE_PROXY_SRC = [
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=8000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
]
def _fetch_free_proxy_list():
    import urllib.request
    got = set()
    for u in _FREE_PROXY_SRC:
        try:
            d = urllib.request.urlopen(u, timeout=15).read().decode("utf-8", "replace")
            for line in d.splitlines():
                line = line.strip().split()[0] if line.strip() else ""
                if ":" in line and line.count(".") == 3:
                    got.add(line)
        except Exception:
            pass
    return list(got)

def _validate_proxy(p, test_url="https://www.etsy.com/", timeout=8):
    """Un proxy n'est garde QUE s'il atteint Etsy (pas juste httpbin): c'est le vrai test
    (Datadome). Retourne p si OK, sinon None."""
    import urllib.request
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": "http://" + p, "https": "http://" + p}))
        op.addheaders = [("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124 Safari/537.36")]
        r = op.open(test_url, timeout=timeout)
        return p if r.status == 200 else None
    except Exception:
        return None

def refresh_free_proxies(max_test=400, want=20, test_url="https://www.etsy.com/"):
    """Recolte + valide des proxies gratuits CONTRE ETSY et alimente la rotation (_PROXIES).
    Retourne la liste validee. Lent (~30-60s) et rendement faible (datacenter vs Datadome)."""
    import concurrent.futures as cf
    global _PROXIES
    cand = _fetch_free_proxy_list()[:max_test]
    ok = []
    with cf.ThreadPoolExecutor(max_workers=60) as ex:
        for r in ex.map(lambda p: _validate_proxy(p, test_url), cand):
            if r:
                ok.append(r)
                if len(ok) >= want:
                    break
    if ok:
        _PROXIES = ok
    return ok

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
    global _session, _proxy_idx
    if _session is None:
        kw = dict(headless=False, network_idle=True, max_pages=SESSION_PAGES)
        # MODE LEGER (SCRAPE_LIGHT=1): bloque images/fonts/media/CSS => ~8x MOINS de donnees
        # (economise le quota proxy: 2Go -> ~6000 boutiques au lieu de ~1000) ET + rapide. Garde
        # le HTML + JS (necessaires au parsing Etsy). A activer quand on paye la bande passante
        # proxy. Defaut OFF (certaines pages Etsy rendent moins bien sans CSS => a tester).
        if os.environ.get("SCRAPE_LIGHT", "0") in ("1", "true", "yes"):
            kw["disable_resources"] = True
        if _PROXIES:                       # rotation d'IP a chaque (re)creation de session
            kw["proxy"] = _PROXIES[_proxy_idx % len(_PROXIES)]
            _proxy_idx += 1
        _session = AsyncStealthySession(**kw)
        await _session.start()
        start_window_hider()   # cache la/les fenetre(s) du navigateur
    return _session

async def _reset_session():
    """Le navigateur est mort (crash camoufox: TargetClosedError / frame detached).
    Sans reset, _session reste en cache et TOUS les fetchs suivants echouent =>
    le scrape rend 0 boutique 'par moments'. On ferme proprement et on force la
    recreation au prochain _get_session."""
    global _session
    s, _session = _session, None
    if s is not None:
        try: await s.close()
        except Exception: pass

def _is_dead(exc):
    m = str(exc).lower()
    return ("targetclosed" in m or "browser has been closed" in m or "frame was detached"
            in m or "page, context or browser" in m or "session closed" in m
            or "connection closed" in m)

async def _afetch(url, wait, retries=2, network_idle=True):
    """Fetch via la session; retry si 403 transitoire (Datadome) OU si le navigateur
    a crashe (on le recree). Timeout DUR par essai COURT: quand les proxies sont
    epuises/bloques, chaque fetch echoue => on veut echouer VITE (pas 21s x3) pour rendre
    le bilan rapidement. 2 essais, marge reduite."""
    hard = wait / 1000.0 + 8    # secondes: attente JS + marge chargement (reduite)
    p = None
    for attempt in range(retries):
        try:
            s = await _get_session()
            p = await asyncio.wait_for(
                s.fetch(url, wait=wait, timeout=int(hard * 1000),
                        network_idle=network_idle, load_dom=True),
                timeout=hard + 5)
        except asyncio.TimeoutError:
            p = None
        except Exception as e:
            p = None
            if _is_dead(e):           # navigateur mort => on le recree avant de reessayer
                await _reset_session()
        if p is not None and getattr(p, "status", 0) == 200:
            await _rotate_tick(ok=True)
            return p
        await _rotate_tick(ok=False)               # 403/timeout => peut declencher rotation
        await asyncio.sleep(0.6 + attempt * 0.8)   # backoff croissant
    return p

async def _rotate_tick(ok):
    """Compte les fetches et ROTATE la session (nouveau navigateur/cookie/IP) soit
    periodiquement (anti-accumulation Datadome), soit des qu'on enchaine les blocages.
    => on repart 'propre' et on continue a scraper au lieu de rester bloque."""
    global _fetch_count, _block_streak, _rotating
    if _rotating:
        return
    if ok:
        _fetch_count += 1; _block_streak = 0
        if _fetch_count % ROTATE_EVERY == 0:       # rotation proactive
            _rotating = True
            try: await _reset_session()
            finally: _rotating = False
    else:
        _block_streak += 1
        if _block_streak >= 6:                     # blocages soutenus => identite grillee
            _rotating = True; _block_streak = 0
            try: await _reset_session()
            finally: _rotating = False

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
    # scrapling pilote "Google Chrome for Testing" ; patchright (Google Lens) pilote son
    # propre "Chromium" (titre different). On masque les DEUX. "Chrome for Testing" couvre
    # aussi la variante sans "Google". Une fenetre automation vierge = "about:blank - ...".
    # On NE deplace QUE les fenetres dont le titre contient un de ces marqueurs, pour ne
    # pas toucher les fenetres Chrome normales de l'utilisateur.
    MARKS = ("Chrome for Testing", "Chromium")
    SW_HIDE = 0
    def _scan():
        def cb(hwnd, lparam):
            # PAS de cache "deja vue": chromium RE-AFFICHE parfois sa fenetre apres qu'on l'a
            # cachee (nouvel onglet, navigation) => on re-cache a CHAQUE scan tant qu'elle est
            # visible. Si deja cachee (IsWindowVisible False), on passe (pas de travail inutile).
            if not user32.IsWindowVisible(hwnd):
                return True
            ttl = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, ttl, 256)
            t = ttl.value or ""
            if any(m in t for m in MARKS):
                # 1) HORS-ECRAN (au cas ou ShowWindow soit ignore) PUIS 2) ShowWindow(SW_HIDE):
                # retire la fenetre de l'ecran ET de la barre des taches (avant: deplacee hors-ecran
                # mais l'icone restait dans le taskbar => "pages google for testing" visibles).
                # Le rendu continue: --disable-renderer-backgrounding / --disable-backgrounding-
                # occluded-windows (args chromium) empechent le throttling d'une fenetre cachee.
                user32.SetWindowPos(hwnd, 0, -32000, -32000, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                user32.ShowWindow(hwnd, SW_HIDE)
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    def worker():
        while True:
            try: _scan()
            except Exception: pass
            time.sleep(0.02)   # scan tres frequent => fenetre cachee quasi instantanement
                               # (a peine un flash possible a la creation)
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

# Prix produit Etsy via JSON-LD (schema.org/Product). Etsy embarque un <script
# type="application/ld+json"> avec soit un Product direct, soit un @graph les contenant.
# offers.price = prix de vente. On prend la MEDIANE des produits de la page boutique
# (robuste aux soldes / cartes promo) = base de la marge dropship (etsy_core.py:2079).
_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
_PRICE_NUM = re.compile(r'([0-9]+(?:[.,][0-9]{1,2})?)')

def _parse_etsy_prices_ld(html):
    """Extrait les prix produits du JSON-LD Etsy. Retourne liste de float (USD), [] si rien.
    Gere @graph, offers direct, et priceSpecification (prix promo). Fallback <meta price>."""
    out = []
    for m in _JSONLD_RE.findall(html):
        try:
            import json
            data = json.loads(m)
        except Exception:
            continue
        # Recolte TOUS les dicts contenant une cle 'offers' (Product direct, Product dans
        # @graph, Product dans ItemList...). Pas de filtre sur @type: Etsy change le schema
        # et le @type peut etre absent ou typo differents => on se fie a la structure.
        prods = []
        def _collect(node):
            if isinstance(node, dict):
                if isinstance(node.get("offers"), (dict, list)):
                    prods.append(node)
                for v in node.values():
                    _collect(v)
            elif isinstance(node, list):
                for x in node:
                    _collect(x)
        _collect(data)
        for prod in prods:
            off = prod.get("offers")
            if isinstance(off, list):
                off = off[0] if off else {}
            if not isinstance(off, dict):
                continue
            # priceSpecification (promo) prioritaire sur price direct
            spec = off.get("priceSpecification")
            if isinstance(spec, dict):
                price = spec.get("price", spec.get("lowPrice"))
            else:
                price = off.get("price", off.get("lowPrice"))
            try:
                v = float(str(price).replace(",", "."))
                if 0 < v < 100000:
                    out.append(v)
            except Exception:
                continue
    # Fallback: meta itemprop="price" si le JSON-LD est vide/casse
    if not out:
        for mm in re.finditer(r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)["\']', html, re.I):
            pm = _PRICE_NUM.search(mm.group(1))
            if pm:
                try:
                    v = float(pm.group(1).replace(",", "."))
                    if 0 < v < 100000:
                        out.append(v)
                except Exception:
                    pass
    return out

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
    images = []
    for a in p.css('a[href*="/listing/"]'):
        t = " ".join((a.attrib.get("title") or a.text or "").split())
        if t and len(t) > 5 and t not in titles:
            titles.append(t)
            # image produit de la carte (pour la validation dropship par image, sans API).
            # Etsy lazy-load: l'URL est dans src OU data-src/srcset de l'<img> de la carte.
            iu = ""
            for im in a.css("img"):
                iu = (im.attrib.get("src") or im.attrib.get("data-src") or "").strip()
                if not iu:
                    ss = (im.attrib.get("srcset") or "").strip()
                    iu = ss.split()[0] if ss else ""
                if iu.startswith("http"):
                    break
            images.append(iu if iu.startswith("http") else "")
    months = mo.group(1) if mo else None
    years = yo.group(1) if yo else None
    # Prix de vente Etsy (mediane des produits de la page) via JSON-LD schema.org/Product.
    # Base de la marge dropship (prix vente / prix achat AliExpress). 0 credit API.
    prices = _parse_etsy_prices_ld(html)
    price = round(sorted(prices)[len(prices) // 2], 2) if prices else None
    return {"sold": sold,
            "months": int(months) if months else (int(years) * 12 if years else None),
            "is_under_1y": months is not None,
            "price": price,
            "titles": titles[:48], "images": images[:48]}

# ---- BACKEND CDP: scraping via TON VRAI Chrome (comme une extension navigateur) -------------
# Active par SCRAPE_VIA_CHROME=1. On se connecte (CDP) au Chrome debug lance par ali_chrome.py
# (vrai profil, vrais cookies, ta vraie empreinte/IP) et on ouvre des onglets Etsy DEDANS. Pour
# Datadome ca ressemble a TOI qui navigues, pas a un bot d'automation => beaucoup moins de
# blocage, pas besoin de proxy. Comme l'extension "Ultimate Web Scraper". Pagination = on ouvre
# ?page=N en parallele (onglets), borne par SCRAPE_CDP_CONC.
SCRAPE_VIA_CHROME = os.environ.get("SCRAPE_VIA_CHROME", "0") in ("1", "true", "yes")
CDP_CONC = int(os.environ.get("SCRAPE_CDP_CONC", "6"))   # onglets paralleles dans ton Chrome
_cdp = {"pw": None, "br": None, "ctx": None}

class _CDPPage:
    """Adapte le HTML recupere via CDP a l'interface attendue par _parse_search/_parse_shop
    (scrapling: .css(), .html_content, .status)."""
    def __init__(self, html, url):
        from scrapling.parser import Adaptor
        self.html_content = html or ""
        self.status = 200 if html else 0
        self._a = Adaptor(html, url=url) if html else None
    def css(self, sel):
        return self._a.css(sel) if self._a is not None else []

async def _cdp_get_ctx():
    from patchright.async_api import async_playwright
    if _cdp["ctx"] is not None:
        return _cdp["ctx"]
    import ali_chrome
    url = await asyncio.to_thread(ali_chrome.ensure_chrome)
    cdp_url = url if (url or "").startswith("http") else "http://localhost:9222"
    pw = await async_playwright().start()
    br = await pw.chromium.connect_over_cdp(cdp_url, timeout=20000)
    ctx = br.contexts[0] if br.contexts else await br.new_context()
    _cdp.update(pw=pw, br=br, ctx=ctx)
    return ctx

async def _cdp_close():
    for k in ("br", "pw"):
        o = _cdp.get(k)
        if o is not None:
            try: await (o.close() if k == "br" else o.stop())
            except Exception: pass
    _cdp.update(pw=None, br=None, ctx=None)

async def _cdp_fetch_one(ctx, url, wait):
    """Ouvre 1 onglet dans ton Chrome, charge l'URL, rend le HTML (str) ou None."""
    pg = None
    try:
        pg = await ctx.new_page()
        await pg.goto(url, wait_until="domcontentloaded", timeout=int(wait) + 20000)
        if wait:
            await pg.wait_for_timeout(int(wait))
        return await pg.content()
    except Exception:
        return None
    finally:
        if pg is not None:
            try: await pg.close()
            except Exception: pass

async def _cdp_fetch_many(urls, wait):
    """Charge plusieurs URLs EN PARALLELE (onglets), borne par CDP_CONC. [(url, html|None)]."""
    ctx = await _cdp_get_ctx()
    sem = asyncio.Semaphore(max(1, CDP_CONC))
    async def one(u):
        async with sem:
            return u, await _cdp_fetch_one(ctx, u, wait)
    return await asyncio.gather(*[one(u) for u in urls])

def _cdp_pages(urls, wait):
    """Sync: {url: _CDPPage}. Reset la connexion CDP si elle a lache (Chrome ferme/rouvert)."""
    try:
        pairs = _run(_cdp_fetch_many(urls, wait))
    except Exception:
        try: _run(_cdp_close())
        except Exception: pass
        try:
            pairs = _run(_cdp_fetch_many(urls, wait))
        except Exception:
            return {u: _CDPPage(None, u) for u in urls}
    return {u: _CDPPage(h, u) for u, h in pairs}

def etsy_login_window():
    """Ouvre une fenetre Etsy VISIBLE dans le Chrome debug (profil persistant) pour que
    l'utilisateur se CONNECTE a Etsy + passe le 1er challenge Datadome a la main, UNE fois.
    Apres ca, le profil garde la session => les scrapes CDP (SCRAPE_VIA_CHROME=1) passent comme
    une vraie navigation connectee (comme l'extension). Relance le Chrome debug si besoin."""
    try:
        import ali_chrome
        if not ali_chrome.chrome_exe():
            return {"ok": False, "error": "Chrome introuvable (installe Google Chrome)"}
        ali_chrome.ensure_chrome()                       # garantit le Chrome debug joignable
        # 2e launch sur le MEME profil/port: Chrome ouvre l'URL dans une fenetre VISIBLE de
        # l'instance existante (pas un 2e process) => l'utilisateur voit Etsy pour se connecter.
        ok = ali_chrome.launch(url="https://www.etsy.com/", hidden=False)
        return {"ok": bool(ok), "error": "" if ok else "launch a echoue"}
    except Exception as e:
        import traceback
        return {"ok": False, "error": (str(e) or repr(e))[:200], "trace": traceback.format_exc()[-400:]}

def etsy_session_ok():
    """True si le Chrome debug atteint Etsy SANS blocage Datadome (session connectee valide).
    Sert a dire a l'utilisateur si son login a marche avant de lancer un gros scrape."""
    if not SCRAPE_VIA_CHROME:
        return None
    try:
        m = _cdp_pages(["https://www.etsy.com/search?q=test&ref=search"], SEARCH_WAIT)
        p = list(m.values())[0]
        h = (p.html_content or "").lower()
        if len(h) < 5000 and ("datadome" in h or "captcha" in h):
            return False
        return p.css('a[href*="/shop/"]') and True or (len(h) > 20000)
    except Exception:
        return False

def _search_shops_cdp(keyword, pages, page_start):
    kw = keyword.strip().replace(" ", "+") or "handmade"
    urls = [f"https://www.etsy.com/search?q={kw}&page={pg}&ref=search"
            for pg in range(page_start, page_start + pages)]
    pages_map = _cdp_pages(urls, SEARCH_WAIT)
    out, seen = [], set()
    for u in urls:
        for nm, sample in _parse_search(pages_map.get(u)):
            if nm and nm not in seen:
                seen.add(nm); out.append((nm, sample))
    return out

def _shops_batch_cdp(names):
    urls = [f"https://www.etsy.com/shop/{n}" for n in names]
    pages_map = _cdp_pages(urls, SHOP_WAIT)
    return {n: _parse_shop(pages_map.get(f"https://www.etsy.com/shop/{n}")) for n in names}

# ---- API sync ----------------------------------------------------------------
def scrape_search_shops(keyword, pages=1, page_start=1):
    """Pages de recherche -> [(shop_name, sample_title)]. 0 API."""
    if SCRAPE_VIA_CHROME:
        return _search_shops_cdp(keyword, pages, page_start)
    out, seen = [], set()
    async def go():
        kw = keyword.strip().replace(" ", "+") or "handmade"
        # Fetch des pages de recherche EN PARALLELE (avant: sequentiel => chaque attente
        # ~4s bloquait la suivante). network_idle=False: une page recherche Etsy n'atteint
        # JAMAIS network_idle (pub/tracking en boucle); load_dom suffit pour parser.
        async def one(pg):
            url = f"https://www.etsy.com/search?q={kw}&page={pg}&ref=search"
            try:
                p = await _afetch(url, SEARCH_WAIT, network_idle=False)
            except Exception:
                return []
            return _parse_search(p)
        pages_res = await asyncio.gather(*[one(pg) for pg in range(page_start, page_start + pages)])
        res = []
        for r in pages_res:
            res.extend(r)
        return res
    if not SCRAPLING_OK:
        return out
    for nm, sample in _run(go()):
        if nm and nm not in seen:
            seen.add(nm); out.append((nm, sample))
    return out

def scrape_shops_batch(names, wait=SHOP_WAIT):
    """Charge plusieurs boutiques EN PARALLELE (1 navigateur, pool de pages).
    Retourne {name: {sold, months, titles}|{error}}.

    RESILIENCE CRASH: si le navigateur meurt en PLEIN batch (TargetClosedError: toutes les
    pages concurrentes tombent d'un coup), les boutiques touchees revenaient en {error} =>
    catalogue vide => le gate niche les jetait => 0 resultat. On RE-FETCH les boutiques tombees
    sur browser-mort apres avoir recree la session (jusqu'a 2 passes). Les vraies erreurs
    (404, boutique absente) ne sont PAS re-essayees (pas un crash)."""
    if not names:
        return {}
    if SCRAPE_VIA_CHROME:
        return _shops_batch_cdp(names)
    if not SCRAPLING_OK:
        return {}
    async def fetch_set(targets):
        async def one(n):
            try:
                p = await _afetch(f"https://www.etsy.com/shop/{n}", wait, network_idle=False)
                return n, _parse_shop(p)
            except Exception as e:
                return n, {"error": str(e)[:80]}
        return dict(await asyncio.gather(*[one(n) for n in targets]))
    async def go():
        out = await fetch_set(names)
        # boutiques tombees sur un navigateur mort (crash mid-batch) => re-fetch sur session neuve.
        for _ in range(2):
            dead = [n for n, d in out.items()
                    if d.get("error") and _is_dead(Exception(d["error"]))]
            if not dead:
                break
            await _reset_session()             # session neuve avant de reprendre les tombees
            out.update(await fetch_set(dead))
        return out
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

def kill_stray_browsers():
    """Tue les 'Google Chrome for Testing' fantomes lances par scrapling/playwright et restes
    ouverts (about:blank). _session.close() ne ferme QUE le dernier navigateur; chaque rotation
    (_reset_session) ou crash camoufox laisse une instance derriere elle (l'erreur de close est
    avalee) => empilement de fenetres. On les tue par le chemin ms-playwright present dans leur
    ligne de commande => on ne touche JAMAIS au Chrome perso de l'utilisateur (autre chemin)."""
    if not sys.platform.startswith("win"):
        return 0
    import subprocess, re
    n = 0
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='chrome.exe'", "get", "ProcessId,CommandLine"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0
    for line in out.splitlines():
        if "ms-playwright" in line:
            m = re.search(r"(\d+)\s*$", line.strip())
            if m:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", m.group(1)],
                                   capture_output=True, timeout=10); n += 1
                except Exception:
                    pass
    return n

def cleanup_temp_profiles():
    """Supprime les profils navigateur temporaires (playwright_chromiumdev_profile-*) laisses
    dans %TEMP%. Chaque (re)creation de session en cree un NOUVEAU et ne le nettoie pas =>
    accumulation sur le disque (cache, cookies, GPU cache => peut grossir a plusieurs Mo/session
    en run reel avec images). On les efface APRES avoir tue les navigateurs (sinon dir verrouille).
    Ne touche qu'aux dossiers du scraping (prefixe playwright_chromiumdev_profile), pas au profil
    perso de l'utilisateur."""
    import tempfile, glob, shutil
    n = 0
    base = tempfile.gettempdir()
    for d in glob.glob(os.path.join(base, "playwright_chromiumdev_profile-*")) \
           + glob.glob(os.path.join(base, "playwright-artifacts-*")):
        try:
            shutil.rmtree(d, ignore_errors=True); n += 1
        except Exception:
            pass
    return n

def close_session():
    global _session
    if _session is not None and _loop is not None:
        try: _run(_session.close())
        except Exception: pass
        _session = None
    if _cdp.get("ctx") is not None:   # detache la connexion CDP (ne tue PAS ton Chrome)
        try: _run(_cdp_close())
        except Exception: pass
    kill_stray_browsers()    # bute les fenetres fantomes laissees par les rotations/crashes
    cleanup_temp_profiles()  # efface les profils temp sur disque (anti-accumulation stockage)
