"""
Validation dropship par IMAGE: pour les images produit d'une boutique Etsy, on fait
une recherche PAR IMAGE sur AliExpress (upload via l'icone camera), on recupere les
vignettes des resultats et on compare perceptuellement (average hash) a l'image Etsy.
Un produit est "trouve sur AliExpress" si une vignette resultat est visuellement
quasi-identique (distance de Hamming faible). Une boutique est validee si >= min_match
de ses produits sont trouves.

Sync API utilisable depuis etsy_core:
  validate_shop_images(image_urls, min_match=3, hash_thresh=12) -> dict
"""
import asyncio, io, re, threading, urllib.parse, urllib.request
from PIL import Image

# ---- similarite titre (fallback texte, fiable) -------------------------------
_TSTOP = set("de la le les un une des et ou en pour avec sur au aux du d l the a an of for "
             "with in to and or handmade fait main decoration deco cadeau gift set new custom "
             "personalized your".split())
def _kw(title, n=6):
    t = re.sub(r"[^a-zA-Z0-9 ]", " ", (title or "").lower())
    return " ".join([w for w in t.split() if len(w) > 2 and w not in _TSTOP][:n])
def _tok(s):
    s = re.sub(r"[^a-zA-Z0-9 ]", " ", (s or "").lower())
    return set(w for w in s.split() if len(w) > 2 and w not in _TSTOP)
def _sim(a, b):
    x, y = _tok(a), _tok(b)
    return len(x & y) / len(x | y) if x and y else 0.0

try:
    from patchright.async_api import async_playwright
    PATCHRIGHT_OK = True
except Exception:
    PATCHRIGHT_OK = False

# SCRAPLING (camoufox furtif) = MEME moteur anti-bot que scraper.py, bien plus resistant que
# patchright chromium aux captchas Google Lens / Datadome. Si dispo, on l'utilise comme moteur
# par defaut pour les fetches Lens (override ALI_ENGINE=patchright pour forcer l'ancien moteur).
try:
    from scrapling.fetchers import AsyncStealthySession
    SCRAPLING_OK = True
except Exception:
    SCRAPLING_OK = False

# Au moins un moteur navigateur dispo (scrapling/camoufox OU patchright/chromium).
ENGINE_OK = PATCHRIGHT_OK or SCRAPLING_OK

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# ROTATION User-Agent: Google Lens flag les sessions a UA fixe/repete. On cycle un pool de
# UA Chrome/Firefox recents (desktop) => chaque contexte parait un appareil different.
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]
import random as _rnd
def _pick_ua():
    return _rnd.choice(_UA_POOL)

# PROXIES: on REUTILISE le pool valide de scraper.py (Webshare/residentiel via config, ou
# proxies gratuits recoltes). Rotation d'IP => aucune IP flaggee, captcha Lens reparti.
_proxy_idx = 0
def _proxy_pool():
    try:
        import scraper
        return list(getattr(scraper, "_PROXIES", []) or [])
    except Exception:
        return []
def _next_proxy_raw():
    """Retourne le prochain proxy du pool sous forme STRING 'http://user:pass@ip:port'
    (format scrapling/camoufox), ou None. Cycle le pool a chaque appel (rotation par relance)."""
    global _proxy_idx
    pool = _proxy_pool()
    if not pool:
        return None
    raw = pool[_proxy_idx % len(pool)]; _proxy_idx += 1
    return raw
def _to_pw_proxy(raw):
    """STRING proxy -> dict playwright {server,username,password}, ou None."""
    if not raw:
        return None
    try:
        from urllib.parse import urlsplit
        u = urlsplit(raw)
        d = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username: d["username"] = u.username
        if u.password: d["password"] = u.password
        return d
    except Exception:
        return None

# STEALTH: masque les signaux d'automation que Google lit (navigator.webdriver, plugins vides,
# languages absentes, chrome runtime manquant). patchright est deja stealth mais ce script
# couvre les fuites residuelles => moins de challenges "trafic inhabituel".
_STEALTH_JS = r"""
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  Object.defineProperty(navigator, 'languages', {get: () => ['fr-FR','fr','en-US','en']});
  Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
  window.chrome = window.chrome || {runtime: {}};
  const _q = navigator.permissions && navigator.permissions.query;
  if (_q) navigator.permissions.query = (p) => p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission}) : _q(p);
"""

# Upload image AliExpress bloque par anti-automation (l'input n'apparait que sur vrai
# geste humain). Par defaut on saute l'image et on valide par TEXTE (fiable, ~5s).
# TRY_IMAGE=True: tente d'abord le match PAR IMAGE (vraie detection "identique"),
# puis retombe sur le TEXTE si l'upload est bloque. Override via env ALI_TRY_IMAGE=0.
import os as _os
TRY_IMAGE = _os.environ.get("ALI_TRY_IMAGE", "1") not in ("0", "false", "no")
# PROFIL PERSISTANT (anti-captcha gratuit): une session Google CONNECTEE se fait challenger
# bien moins qu'une session anonyme. On stocke les cookies/login dans un user_data_dir reutilise
# d'un run a l'autre. Login manuel une seule fois via ali_login.py. Defaut: cache/ali_profile.
# Vide ("") => profil temporaire (comportement anonyme d'avant). Override ALI_PROFILE_DIR.
_PROFILE_DIR = _os.environ.get(
    "ALI_PROFILE_DIR",
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "cache", "ali_profile"))
# COOKIES IMPORTES: Google bloque le login DANS un navigateur automatise ("navigateur pas
# securise"). Contournement: exporter les cookies google.com depuis ton navigateur normal
# (extension Cookie-Editor -> Export JSON) vers cache/ali_cookies.json. On les injecte dans
# le contexte => session deja connectee, pas de page login => Lens challenge moins.
_COOKIES_FILE = _os.environ.get(
    "ALI_COOKIES",
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "cache", "ali_cookies.json"))
_cookies_loaded = False
def _load_cookies_list():
    """Lit cache/ali_cookies.json (format Cookie-Editor) -> liste cookies playwright, ou []."""
    import json
    try:
        with open(_COOKIES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []
    _smap = {"no_restriction": "None", "unspecified": "Lax", "lax": "Lax",
             "strict": "Strict", "none": "None"}
    out = []
    for c in (raw if isinstance(raw, list) else raw.get("cookies", [])):
        try:
            ck = {"name": c["name"], "value": c["value"],
                  "domain": c.get("domain") or ".google.com", "path": c.get("path", "/")}
            if c.get("expirationDate"): ck["expires"] = int(c["expirationDate"])
            if "httpOnly" in c: ck["httpOnly"] = bool(c["httpOnly"])
            if "secure" in c: ck["secure"] = bool(c["secure"])
            ss = (c.get("sameSite") or "").lower()
            if ss in _smap: ck["sameSite"] = _smap[ss]
            out.append(ck)
        except Exception:
            pass
    return out
async def _inject_cookies_once(page):
    """Injecte les cookies importes dans le contexte (1x par process). No-op si pas de fichier."""
    global _cookies_loaded
    if _cookies_loaded:
        return
    _cookies_loaded = True
    cks = _load_cookies_list()
    if not cks:
        return
    try:
        await page.context.add_cookies(cks)
    except Exception:
        pass
# Yandex = 2e moteur reverse-image (gratuit, par URL). DESACTIVE par defaut: en pratique il
# remonte surtout des agregateurs (imall.com) avec des produits DIFFERENTS => faux positifs,
# 0 gain reel sur AliExpress + cout temps. Override ALI_YANDEX=1 pour le reactiver.
YANDEX_FALLBACK = _os.environ.get("ALI_YANDEX", "0") not in ("0", "false", "no")
# FALLBACK natif AliExpress (upload image par drag-drop simule). Active par defaut: uploade
# l'image directement dans le moteur image AliExpress => vrais produits + prix exacts.
# Override ALI_NATIVE=0. Captcha Datadome gere (pas de penalite boutique).
# DESACTIVE par defaut: diagnostic a montre que l'upload image native ne marche PAS (drag-drop
# non pris) => AliExpress renvoie des produits TENDANCE sans rapport (gloss, trottinettes, coques)
# quelle que soit la photo => BRUIT PUR + source des faux positifs (artisans flagges dropship).
# Le vrai signal vient de Google Lens (matching visuel deep-feature). Reactiver: ALI_NATIVE=1.
NATIVE_FALLBACK = _os.environ.get("ALI_NATIVE", "0") not in ("0", "false", "no")
_CONSENT_DONE = False   # consentement Google accepte une fois par process (cookie persiste)

# ---- perceptual hash (Pillow seul) -------------------------------------------
# On combine DEUX hash: aHash (luminance moyenne) + dHash (gradient horizontal). dHash capte
# la STRUCTURE de l'image (contours), il est peu sensible au fond uni => discrimine bien deux
# produits differents sur fond blanc (la ou aHash sature et donne des faux positifs). Un match
# n'est confirme que si les DEUX hash sont proches (cf _hash_dist).
def _phash_pair(img_bytes):
    """Retourne (ahash, dhash) ou None. dhash sur 9x8 (8 comparaisons/ligne = 64 bits)."""
    try:
        base = Image.open(io.BytesIO(img_bytes)).convert("L")
    except Exception:
        return None
    a = base.resize((8, 8))
    px = list(a.getdata()); avg = sum(px) / 64.0
    ah = 0
    for i, p in enumerate(px):
        if p > avg:
            ah |= (1 << i)
    d = base.resize((9, 8))
    dp = list(d.getdata()); dh = 0; bit = 0
    for row in range(8):
        for col in range(8):
            if dp[row * 9 + col] > dp[row * 9 + col + 1]:
                dh |= (1 << bit)
            bit += 1
    return (ah, dh)

def _ahash(img_bytes):
    """Compat: retourne le couple (ahash, dhash). None si decodage echoue."""
    return _phash_pair(img_bytes)

def _ham(a, b):
    return bin(a ^ b).count("1") if (a is not None and b is not None) else 64

def _hash_dist(p, q):
    """Distance entre deux couples (ahash,dhash) = MAX des deux distances de Hamming. Le MAX
    (et non la moyenne) impose que les DEUX hash concordent => un faux positif fond-blanc sur
    aHash est rejete si dHash (structure) diverge. 64 si l'un des couples est absent."""
    if not p or not q:
        return 64
    return max(_ham(p[0], q[0]), _ham(p[1], q[1]))

def _hamming(a, b):
    """Compat: si on recoit des couples -> _hash_dist; sinon Hamming brut sur entiers."""
    if isinstance(a, tuple) or isinstance(b, tuple):
        return _hash_dist(a, b)
    return _ham(a, b)

# ---- parse prix AliExpress (texte carte Lens / page produit) -------------------
# Lens (resultats shopping/visual match) affiche souvent le prix de l'item AliExpress
# dans la carte. On extrait ce texte ("$5.99", "US $12.34", "8,50 €", "£3.20") et on
# le normalise en float. Sert au signal dropship: marge Etsy/AliExpress = preuve forte.
_PRICE_RE = re.compile(
    r"(?:US\s*)?[\$£€]\s*([0-9]{1,5}(?:[.,][0-9]{1,2})?)"
    r"|([0-9]{1,5}(?:[.,][0-9]{1,2})?)\s*[\$£€]")
def _parse_price(txt):
    """Texte -> float (USD-approx, on ne convertit pas la devise). None si rien."""
    if not txt:
        return None
    m = _PRICE_RE.search(txt)
    if not m:
        return None
    raw = m.group(1) or m.group(2) or ""
    raw = raw.replace(",", ".")
    try:
        v = float(raw)
        return v if 0 < v < 100000 else None
    except Exception:
        return None

def _download(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception:
        return None

# ---- boucle asyncio dediee ---------------------------------------------------
_loop = None
_lock = threading.Lock()
def _ensure_loop():
    global _loop
    if _loop is None:
        with _lock:
            if _loop is None:
                _loop = asyncio.new_event_loop()
                threading.Thread(target=_loop.run_forever, daemon=True).start()
def _run(coro):
    _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()

# ---- recherche image AliExpress ----------------------------------------------
_PIC_BTN = ('[class*="picture-search"], [class*="picSearch"], [class*="searchByImage"], '
            '[class*="image-search"], [class*="pic-search"], #searchByImage, '
            '[aria-label*="image" i], [aria-label*="photo" i]')
_CONSENT = ['button:has-text("Accept All")', 'button:has-text("Accept")',
            'button:has-text("Accepter")', 'button:has-text("Tout accepter")',
            '[data-role="gdpr-accept"]', '.btn-accept', '[class*="gdpr"] button',
            '[aria-label="Close"]', 'div[class*="close-icon"]']

async def _dismiss_overlays(pg):
    for sel in _CONSENT:
        try:
            el = await pg.query_selector(sel)
            if el:
                await el.click(timeout=1200); await pg.wait_for_timeout(300)
        except Exception:
            pass

async def _lens_ali_search(pg, image_url):
    """Recherche par IMAGE via Google Lens en passant l'URL publique de l'image Etsy
    (lens.google.com/uploadbyurl) => AUCUN upload de fichier (donc aucun blocage
    anti-bot AliExpress/Datadome). Lens renvoie les correspondances visuelles ; on
    extrait les liens produits AliExpress (= produit trouve a l'identique a la source).
    Retourne [urls_aliexpress]."""
    url = "https://lens.google.com/uploadbyurl?url=" + urllib.parse.quote(image_url, safe="")
    try:
        await pg.goto(url, wait_until="domcontentloaded", timeout=40000)
    except Exception:
        return []
    # Consentement Google: n'apparait qu'a la 1re visite (le cookie persiste ensuite dans
    # le contexte) => on ne le gere QU'UNE fois, pas a chaque produit (gain de temps).
    global _CONSENT_DONE
    if not _CONSENT_DONE:
        await pg.wait_for_timeout(2500)
        for sel in ('button:has-text("Accept all")', 'button:has-text("Tout accepter")',
                    'button:has-text("I agree")', 'button:has-text("Accepter")',
                    'button[aria-label*="Accept" i]'):
            try:
                el = await pg.query_selector(sel)
                if el:
                    await el.click(timeout=1500); await pg.wait_for_timeout(2000)
                    _CONSENT_DONE = True; break
            except Exception:
                pass
        _CONSENT_DONE = True
    # POLLING: au lieu d'attentes fixes longues, on sonde toutes les 0.6s et on RETOURNE
    # des qu'un lien produit AliExpress apparait (hit rapide ~2-3s). Un miss est borne a
    # ~8s. On scrolle un peu a chaque tour pour declencher le lazy-load des resultats.
    # On collecte aussi le TEXTE du resultat (titre/contexte) pour pouvoir verifier la
    # pertinence (le produit AliExpress doit ressembler au produit Etsy, pas juste un lien
    # quelconque que Google a remonte). Retourne [{url, txt}].
    JS = """() => {
        const PR = /(?:US\\s*)?[\\$£€]\\s*[0-9]{1,5}(?:[.,][0-9]{1,2})?|[0-9]{1,5}(?:[.,][0-9]{1,2})?\\s*[\\$£€]/;
        const priceNear = (a) => {
            let n = a;
            for (let i = 0; i < 5 && n; i++) {
                const m = (n.textContent || '').match(PR);
                if (m) return m[0].trim();
                n = n.parentElement;
            }
            return '';
        };
        // Choisit la VRAIE vignette produit (la PLUS GRANDE image du conteneur), pas le
        // favicon du site (petit, src contenant 'favicon') => sinon le hash perceptuel
        // compare des favicons et ne matche jamais.
        const pickThumb = (a) => {
            let cand = [], n = a;
            for (let i=0;i<4&&n;i++){ if(n.querySelectorAll){ cand.push(...n.querySelectorAll('img')); } n=n.parentElement; }
            cand = cand.filter(im => { const s=im.src||im.getAttribute('data-src')||''; return s && !/favicon|faviconV2|sprite/i.test(s); });
            cand.sort((p,q)=>((q.naturalWidth*q.naturalHeight)||(q.width*q.height)||0)-((p.naturalWidth*p.naturalHeight)||(p.width*p.height)||0));
            const im = cand[0];
            const area = im ? ((im.naturalWidth*im.naturalHeight)||(im.width*im.height)||0) : 0;
            return { src: im ? (im.src || im.getAttribute('data-src') || '') : '', area: area };
        };
        // Google emballe souvent les liens resultats: /url?q=<vraie_url>, ?url=, ?imgurl=.
        // On deballe pour retrouver le vrai domaine marchand (sinon host = google.com => rate).
        const unwrap = (href) => {
            try {
                const u = new URL(href, location.href);
                for (const k of ['q','url','imgurl','adurl','continue']) {
                    const v = u.searchParams.get(k);
                    if (v && /^https?:/i.test(v)) return v;
                }
                return u.href;
            } catch(e) { return href; }
        };
        // UNIQUEMENT AliExpress (tous TLD/sous-domaines: www/fr/de/m/...). Un lien produit
        // AliExpress a un id numerique (item/i/p/_p) => detection permissive pour ne rien rater.
        const ALI = /(^|\\.)aliexpress\\.(com|us|ru|[a-z]{2})$/i;
        // vraie page produit: item/i/p OU id numerique long, mais PAS pages video/boutique/recherche
        const ALI_ITEM = /aliexpress\\.[a-z.]+\\/(item|i|p|_p|_item)\\/|aliexpress\\.[a-z.]+\\/.*[0-9]{8,}/i;
        const ALI_SKIP = /\\/(video-ssr|store|w|wholesale|category|af|gcp|ssr\\/search|gateway|account)\\b/i;
        const out = [];
        document.querySelectorAll('a[href]').forEach(a => {
            try {
                const real = unwrap(a.href);
                const h = new URL(real).hostname;
                if (!(ALI.test(h) && ALI_ITEM.test(real)) || ALI_SKIP.test(real)) return;
                let t = (a.getAttribute('aria-label') || a.title || a.textContent || '').trim();
                const tb = pickThumb(a);
                const thumb = tb.src;
                if (!t && thumb) { const im0=a.querySelector('img'); t = im0 ? (im0.alt||'') : ''; }
                const pr = priceNear(a);
                // vm = lien dans la ZONE de correspondances visuelles Lens (vraie carte produit):
                // vignette produit de taille reelle (area>=2500 ~ >=50x50) ET prix proche. Les liens
                // hors-sujet ("people also search", recos bas de page) n'ont ni vraie vignette ni prix
                // => vm=false => ignores quand des vm existent (coupe faux positifs page-wide).
                const vm = !!(thumb && tb.area >= 2500 && pr);
                out.push({url: real, host: h, ali: true, txt: t.slice(0, 120), price: pr, img: thumb, vm: vm});
            } catch(e) {}
        });
        const seen = new Set(), res = [];
        for (const o of out) { if (!seen.has(o.url)) { seen.add(o.url); res.push(o); } }
        return res.slice(0, 30);
    }"""
    # RECALL: les liens AliExpress sont dispatches sur toute la page (section "visuellement
    # similaire" EN HAUT, mais les vendeurs AliExpress qui reuploadent la photo apparaissent
    # surtout dans la section "correspondances exactes" PLUS BAS, chargee en lazy-load au
    # scroll). On NE retourne donc PAS au 1er lien: on scrolle toute la page et on ACCUMULE
    # l'union des liens AliExpress sur plusieurs passes (gros gain de recall vs early-return).
    acc = {}                            # url -> record (dedup, garde le 1er prix vu)
    stable = 0
    for i in range(14):                 # ~8s max
        if "/sorry/" in (pg.url or ""): # Google captcha (surtout en headless)
            if acc:
                break                   # on a deja des liens => on les garde
            return None                 # 0 lien + captcha => signal CAPTCHA (=> rotation proxy)
        try:
            links = await pg.evaluate(JS)
        except Exception:
            links = []
        before = len(acc)
        for o in links:
            if o["url"] not in acc:
                acc[o["url"]] = o
        # arret anticipe: on a des liens ET 3 passes de suite sans nouveau lien (page epuisee)
        stable = stable + 1 if len(acc) == before else 0
        if acc and stable >= 2:
            break
        try:
            await pg.evaluate("window.scrollBy(0, 1600)")
        except Exception:
            pass
        await pg.wait_for_timeout(550)
    vals = list(acc.values())
    # PRECISION: si Lens a remonte >=1 vraie carte de correspondance visuelle (vm=True:
    # vignette produit + prix), on IGNORE les liens AliExpress page-wide (sections "people
    # also search"/recos) => coupe les faux positifs ou Lens lie un AliExpress sans rapport.
    vm = [o for o in vals if o.get("vm")]
    return vm if vm else vals

# ---- recherche image YANDEX (2e moteur gratuit, par URL => pas d'upload/Datadome) -------
# Yandex est le meilleur reverse-image pour retrouver le produit EXACT sur AliExpress
# (il indexe massivement les marketplaces). On passe l'URL image Etsy => page "sites
# contenant cette image" => on extrait les liens AliExpress. Gratuit, sans cle.
_YANDEX_JS = r"""() => {
    const PR = /(?:US\s*)?[\$£€]\s*[0-9]{1,5}(?:[.,][0-9]{1,2})?|[0-9]{1,5}(?:[.,][0-9]{1,2})?\s*[\$£€]/;
    const priceNear = (a) => { let n=a; for(let i=0;i<6&&n;i++){const m=(n.textContent||'').match(PR); if(m) return m[0].trim(); n=n.parentElement;} return ''; };
    // Yandex emballe les liens resultats dans /clck/redir?...&url=<encode>. Le vrai lien
    // AliExpress est quelque part dans le href, parfois encode 1-2 fois => on decode et on
    // extrait directement une URL produit AliExpress du texte du href.
    const ALI_URL = /https?:\/\/[a-z0-9.\-]*aliexpress\.[a-z.]+\/[^\s"'&]*?(?:\/(?:item|i)\/[0-9]+|[0-9]{8,}\.html)[^\s"'&]*/i;
    const extract = (href) => {
        let s = href;
        for (let i=0;i<3;i++){ let m=s.match(ALI_URL); if(m) return m[0]; try{ s=decodeURIComponent(s);}catch(e){break;} }
        return '';
    };
    const out=[], seen=new Set();
    document.querySelectorAll('a[href]').forEach(a => {
        try {
            if (!/aliexpress/i.test(a.href)) return;
            let real = extract(a.href); if (!real) return;
            real = real.split('?')[0].split('%3F')[0];
            if (seen.has(real)) return; seen.add(real);
            let t=(a.getAttribute('title')||a.textContent||'').trim();
            let cand=[],n=a;
            for(let i=0;i<4&&n;i++){if(n.querySelectorAll){cand.push(...n.querySelectorAll('img'));}n=n.parentElement;}
            cand=cand.filter(im=>{const s=im.src||im.getAttribute('data-src')||'';return s&&!/favicon|faviconV2|sprite/i.test(s);});
            cand.sort((p,q)=>((q.naturalWidth*q.naturalHeight)||(q.width*q.height)||0)-((p.naturalWidth*p.naturalHeight)||(p.width*p.height)||0));
            const thumb=cand[0]?(cand[0].src||cand[0].getAttribute('data-src')||''):'';
            out.push({url: real, txt: t.slice(0,120), price: priceNear(a), ali: true, img: thumb});
        } catch(e) {}
    });
    return out.slice(0, 20);
}"""

async def _yandex_ali_search(pg, image_url):
    """Recherche par IMAGE via Yandex (URL image). Retourne [{url,txt,price,ali}] AliExpress.
    [] si rien / captcha. Aucun upload => pas de Datadome AliExpress."""
    url = ("https://yandex.com/images/search?rpt=imageview&format=json&request="
           "&url=" + urllib.parse.quote(image_url, safe=""))
    try:
        await pg.goto(url, wait_until="domcontentloaded", timeout=35000)
    except Exception:
        return []
    acc = {}; stable = 0
    for i in range(12):                 # ~8s max
        u = (pg.url or "").lower()
        if "showcaptcha" in u or "/checkcaptcha" in u:
            return []                   # Yandex captcha => on abandonne (pas de penalite)
        try:
            links = await pg.evaluate(_YANDEX_JS)
        except Exception:
            links = []
        before = len(acc)
        for o in links:
            acc.setdefault(o["url"], o)
        stable = stable + 1 if len(acc) == before else 0
        if acc and stable >= 2:
            break
        try: await pg.evaluate("window.scrollBy(0, 1600)")
        except Exception: pass
        await pg.wait_for_timeout(550)
    return list(acc.values())

async def _ali_text_search(pg, query):
    """Recherche texte AliExpress -> [titres resultats]. Fallback fiable.
    Slug AliExpress = mots separes par des TIRETS (pas %20), sinon 0 resultat."""
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-") or "gift"
    url = ("https://www.aliexpress.com/w/wholesale-" + slug + ".html"
           "?SearchText=" + urllib.parse.quote(query))
    try:
        await pg.goto(url, wait_until="domcontentloaded", timeout=35000)
        await pg.wait_for_timeout(2500)
        await _dismiss_overlays(pg)
        await pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await pg.wait_for_timeout(1200)
        return await pg.evaluate("""()=>{const o=[];document.querySelectorAll('a[href*="/item/"]').forEach(a=>{
            const t=(a.textContent||'').trim();if(t&&t.length>15)o.push(t.slice(0,90));});return [...new Set(o)].slice(0,10);}""")
    except Exception:
        return []

async def _ali_native_search(pg, image_url):
    """FALLBACK recall: recherche par IMAGE NATIVE sur AliExpress (upload de la photo
    produit dans le moteur image d'AliExpress). Utilisee SEULEMENT quand Lens n'a remonte
    aucun lien AliExpress => recupere les vrais produits AliExpress + prix exacts.
    Retourne (links|None, blocked). links=[{url,txt,price,ali}] ; blocked=True si captcha
    Datadome (on n'insiste pas, la boutique n'est PAS penalisee). None = rien trouve."""
    data = _download(image_url)
    if not data:
        return None, False
    import base64
    b64 = base64.b64encode(data).decode()
    try:
        try:
            await pg.goto("https://www.aliexpress.com/", wait_until="domcontentloaded", timeout=35000)
        except Exception:
            return None, False
        await pg.wait_for_timeout(1500)
        await _dismiss_overlays(pg)
        if _ali_blocked(pg):
            return None, True
        # Ouvre le modal de recherche par image (clic camera). Erreurs avalees (le modal
        # peut deja intercepter le clic => peu importe, on injecte ensuite).
        for sel in _PIC_BTN.split(", "):
            try:
                el = await pg.query_selector(sel)
                if el:
                    await el.click(timeout=1200); await pg.wait_for_timeout(500); break
            except Exception:
                pass
        # UPLOAD ROBUSTE: AliExpress n'expose pas toujours d'input[type=file] (zone drag-drop).
        # On injecte donc l'image (base64 -> File -> DataTransfer) de DEUX facons:
        #  1) si un input[type=file] existe => on lui assigne le fichier + event 'change';
        #  2) sinon on simule un DRAG-DROP humain (dragenter/dragover/drop) sur la zone du modal.
        # Aucun fichier disque, aucun CORS (l'image vient des bytes en base64).
        try:
            ok = await pg.evaluate(_ALI_UPLOAD_JS, b64)
        except Exception:
            ok = False
        if not ok:
            return None, False
        # AliExpress navigue vers une page resultats image (scene=image_search). Les cartes
        # se chargent en lazy-load => on attend le passage en mode image_search, on re-ferme
        # les overlays (popup login/region apparait souvent APRES navigation), puis on scrolle
        # et parse en accumulant (recall max). ~15s borne.
        dismissed_again = False
        for k in range(14):                 # ~10s max
            await pg.wait_for_timeout(700)
            if _ali_blocked(pg):
                return None, True
            if not dismissed_again and "image_search" in (pg.url or "").lower():
                await _dismiss_overlays(pg); dismissed_again = True
            try:
                links = await pg.evaluate(_ALI_PARSE_JS)
            except Exception:
                links = []
            if links:
                return links, False
            # alterne scroll bas / remontee pour declencher tous les lots lazy-load
            try: await pg.evaluate("window.scrollBy(0, %d)" % (1400 if k % 4 != 3 else -800))
            except Exception: pass
        return None, False
    except Exception:
        return None, False

# Injection image -> recherche AliExpress: base64 -> File -> set input OU drag-drop simule.
_ALI_UPLOAD_JS = r"""(b64) => {
    try {
        const bin = atob(b64); const arr = new Uint8Array(bin.length);
        for (let i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i);
        const file = new File([arr], 'search.jpg', {type:'image/jpeg'});
        const dt = new DataTransfer(); dt.items.add(file);
        // 1) input[type=file] present (variante UI avec input) => assignation directe
        const inp = document.querySelector('input[type="file"], input[accept*="image"]');
        if (inp) {
            inp.files = dt.files;
            inp.dispatchEvent(new Event('input', {bubbles:true}));
            inp.dispatchEvent(new Event('change', {bubbles:true}));
            return true;
        }
        // 2) sinon DRAG-DROP simule sur la zone du modal de recherche image
        const zone = document.querySelector(
            '[class*="image-poplayer"], [class*="image-search"], [class*="searchByImage"], '
            + '[class*="upload"], [class*="drop"], [class*="dragger"]') || document.body;
        for (const type of ['dragenter','dragover','drop']) {
            const ev = new DragEvent(type, {bubbles:true, cancelable:true});
            // certains handlers lisent ev.dataTransfer (read-only) => on le force
            try { Object.defineProperty(ev, 'dataTransfer', {value: dt}); } catch(e) {}
            zone.dispatchEvent(ev);
        }
        return true;
    } catch(e) { return false; }
}"""

def _ali_blocked(pg):
    """True si AliExpress nous sert une page anti-bot (punish/captcha Datadome)."""
    u = (pg.url or "").lower()
    return ("punish" in u) or ("captcha" in u) or ("/sorry/" in u) or ("_____tmd_____" in u)

# Parse des cartes produit AliExpress (resultats image natifs): {url, txt, price, ali}.
_ALI_PARSE_JS = r"""() => {
    const PR = /(?:US\s*)?[\$£€]\s*[0-9]{1,5}(?:[.,][0-9]{1,2})?|[0-9]{1,5}(?:[.,][0-9]{1,2})?\s*[\$£€]/;
    const priceNear = (a) => { let n=a; for(let i=0;i<5&&n;i++){const m=(n.textContent||'').match(PR); if(m) return m[0].trim(); n=n.parentElement;} return ''; };
    const ITEM = /\/(item|i)\/[0-9]+|[0-9]{8,}\.html/i;
    const out=[], seen=new Set();
    document.querySelectorAll('a[href]').forEach(a => {
        try {
            let real = a.href.split('?')[0];   // retire les params tracking (url propre)
            if (!/aliexpress\./i.test(new URL(real).hostname) || !ITEM.test(real)) return;
            if (seen.has(real)) return; seen.add(real);
            let t=(a.getAttribute('title')||a.textContent||'').trim();
            let cand=[],n=a;
            for(let i=0;i<4&&n;i++){if(n.querySelectorAll){cand.push(...n.querySelectorAll('img'));}n=n.parentElement;}
            cand=cand.filter(im=>{const s=im.src||im.getAttribute('data-src')||'';return s&&!/favicon|faviconV2|sprite/i.test(s);});
            cand.sort((p,q)=>((q.naturalWidth*q.naturalHeight)||(q.width*q.height)||0)-((p.naturalWidth*p.naturalHeight)||(p.width*p.height)||0));
            const thumb=cand[0]?(cand[0].src||cand[0].getAttribute('data-src')||''):'';
            out.push({url: real, txt: t.slice(0,120), price: priceNear(a), ali: true, img: thumb});
        } catch(e) {}
    });
    return out.slice(0, 20);
}"""

# ---- VERIFICATION PRECISION: hash perceptuel vignette resultat vs image Etsy ----------
# Un "match" n'est valide que si la vignette du resultat AliExpress est VISUELLEMENT quasi
# identique a l'image produit Etsy (distance de Hamming faible sur average-hash). Elimine
# les faux positifs (le moteur image remonte un produit ressemblant mais different).
# DESACTIVE par defaut: la verif par hash perceptuel (aHash) compare des PIXELS, mais la
# photo AliExpress et la photo Etsy du MEME produit different (angle, fond, restyling du
# dropshipper) => aHash donne des FAUX NEGATIFS systematiques. La precision vient deja du
# matching VISUEL des moteurs (Google Lens / image-search AliExpress) qui utilisent des
# deep-features robustes (crop/rotation) + filtrage AliExpress-only. Reactiver: ALI_VERIFY=1.
VERIFY = _os.environ.get("ALI_VERIFY", "1") not in ("0", "false", "no")
# GATING: si actif, un produit n'est "trouve" QUE si >=1 vignette passe le hash (precision
# max, mais recall plus bas car les vignettes Lens sont des crops Google != photo Etsy).
# Par defaut ADVISORY: on garde le match et on annote `verified` (les 2 signaux remontent
# au score sans sacrifier le recall). Active le gating strict via ALI_VERIFY_GATE=1.
VERIFY_GATE = _os.environ.get("ALI_VERIFY_GATE", "0") not in ("0", "false", "no")
try: VERIFY_THRESH = int(_os.environ.get("ALI_VERIFY_THRESH", "18"))
except Exception: VERIFY_THRESH = 18
# CONFIRMATION PAGE PRODUIT: la vignette Lens est un CROP Google (recadre/recompresse) =>
# hash bruite. Source de verite = la VRAIE photo produit AliExpress (meta og:image de la page
# item). On ouvre les TOPN meilleurs candidats, on telecharge l'og:image et on hash-compare a
# la photo Etsy. Si une page confirme (dist <= STRONG_MAX), c'est la PREUVE forte "meme image".
# Active par defaut (precision max). ALI_CONFIRM_PAGE=0 pour couper. Borne TOPN pour le temps.
CONFIRM_PAGE = _os.environ.get("ALI_CONFIRM_PAGE", "1") not in ("0", "false", "no")
try: CONFIRM_TOPN = max(1, int(_os.environ.get("ALI_CONFIRM_TOPN", "2")))
except Exception: CONFIRM_TOPN = 2

def _thumb_bytes(src):
    if not src:
        return None
    if src.startswith("data:"):
        try:
            import base64
            return base64.b64decode(src.split(",", 1)[1])
        except Exception:
            return None
    return _download(src)

def _verify_results(etsy_hash, results, thresh=VERIFY_THRESH, topn=5):
    """Garde les resultats dont la vignette est ~identique a l'image Etsy. Si on ne peut pas
    verifier (pas de hash Etsy, ou aucune vignette telechargeable), on NE filtre PAS (on ne
    sacrifie pas le recall a cause d'une vignette manquante). Retourne (resultats_gardes,
    verifie?, distance_hamming_min). dmin = meilleure (plus petite) distance vignette vs photo
    Etsy => sert a grader la force du match (exact / fort / faible)."""
    if etsy_hash is None:
        return results, False, None
    kept, any_thumb, dmin = [], False, None
    for r in results[:topn]:
        b = _thumb_bytes(r.get("img"))
        if not b:
            continue
        any_thumb = True
        h = _ahash(b)
        if h is None:
            continue
        d = _hamming(etsy_hash, h)
        dmin = d if dmin is None else min(dmin, d)
        if d <= thresh:
            kept.append(r)
    if not any_thumb:
        return results, False, None     # aucune vignette comparable => pas de filtrage
    return kept, True, dmin

# Seuils de force du match. PRECISION D'ABORD: la seule preuve fiable de dropship est l'IMAGE
# IDENTIQUE (hash perceptuel). La similarite de TITRE seule NE compte PAS (un artisan vend un
# "olive wood bowl" et AliExpress aussi => titre identique mais objet different). Seuils STRICTS
# pour eviter les faux positifs (aHash sature sur les photos produit fond blanc).
#   exact  : vignette AliExpress quasi pixel-identique (hash <= EXACT)        -> 70 pts, HIT
#   fort   : image tres proche (hash <= STRONG)                              -> 40 pts, HIT
#   faible : trouve par le moteur mais image NON confirmee (titre/proximite) -> 15 pts, PAS hit
_HASH_EXACT = 8
_HASH_STRONG = 14
# Borne sup pour "strong": un match Lens ne compte comme hit QUE si la vignette AliExpress
# est PROCHE de la photo Etsy (dmin <= _HASH_STRONG_MAX). Au-dela, c'est un produit DIFFERENT
# de la meme categorie (ex: deux bols en bois d'olivier, l'artisan et l'usine) => faux positif.
# Diag: le MEME produit avec une photo differente donne d<=18 => 22 laisse une marge de securite.
# Tunable via env ALI_HASH_STRONG_MAX (baisser pour + de precision, monter pour + de recall).
try: _HASH_STRONG_MAX = int(_os.environ.get("ALI_HASH_STRONG_MAX", "22"))
except Exception: _HASH_STRONG_MAX = 22
_POINTS = {"exact": 70, "strong": 40, "weak": 15, "none": 0}
# Un produit ne compte comme "trouve sur AliExpress" (hit dropship) QUE si l'image est
# confirmee (exact|strong). weak = moteur a remonte un lien mais image pas identique => PAS dropship.
_HIT_STRENGTHS = ("exact", "strong")
def _is_hit(strength):
    return strength in _HIT_STRENGTHS

def _dedup_unique(results):
    """Normalisation (point 3): AliExpress reposte le MEME produit (angles differents,
    vendeurs multiples). On regroupe les cartes a titre quasi-identique (sim >= 0.7) pour
    ne compter qu'UN produit unique par groupe => evite de sur-estimer le dropship.
    Garde le 1er representant de chaque groupe. Retourne (representants, n_groupes)."""
    reps = []
    for r in results:
        t = r.get("txt") or ""
        if any(_sim(t, (rep.get("txt") or "")) >= 0.7 for rep in reps):
            continue
        reps.append(r)
    return reps, len(reps)

def _grade(best_sim, verified, dmin):
    """Force du match -> (label, points). Appele UNIQUEMENT quand Google Lens a deja renvoye un
    lien produit AliExpress (= match visuel deep-feature confirme par Google).
    PRECISION: un match ne compte comme hit (exact|strong) QUE si l'image AliExpress est PROCHE
    de la photo Etsy (hash perceptuel). Sans cette confirmation, c'est "weak" (Lens a trouve un
    lien mais on ne peut pas prouver que c'est le MEME produit => pas de hit dropship).
      exact  : vignette quasi pixel-identique (dmin <= EXACT)                    -> 70 pts, HIT
      strong : vignette proche, meme produit angle/eclairage differents (<=MAX)  -> 40 pts, HIT
      weak   : vignette trop differente (>MAX) OU non verifiable (dmin=None)      -> 15 pts, PAS hit
    Le diag: le meme produit avec photo differente donne d<=18 => _HASH_STRONG_MAX=22 laisse
    une marge tout en rejetant les produits DIFFERENTS d'une meme categorie (ex: deux bols en
    bois d'olivier artisan vs usine -> d>25). Elimine les faux positifs ou des artisans font
    des produits SIMILAIRES a des produits AliExpress sans les revendre.
    Note: le vol de photo (vendeur AliExpress qui copie la photo d'un artisan) donne un "exact"
    faux positif — non detectable au niveau image seul (meme image = meme hash). Le signal
    vient alors de l'age/ventes de la boutique (cf etsy_core.py dropship_score)."""
    if verified and dmin is not None and dmin <= _HASH_EXACT:
        return "exact", _POINTS["exact"]
    if dmin is not None and dmin <= _HASH_STRONG_MAX:
        return "strong", _POINTS["strong"]
    return "weak", _POINTS["weak"]

def _build_detail(title, results, src, verified=False, dmin=None):
    """[{url,txt,price}] -> detail {ali,n,n_unique,sim,strength,points,src,ali_price?,verified}.
    Classe par similarite titre. Normalise les doublons produit (n_unique). Grade la force du
    match (exact/strong/weak -> points) a partir du hash perceptuel + similarite titre.
    Prix = MEDIANE de TOUTES les cartes (le moteur image melange le produit et des accessoires
    cheap; la mediane sur l'ensemble est le cout d'achat le + representatif). Le prix reste
    indicatif: la marge dropship sature de toute facon a 5x => le verdict est robuste au bruit."""
    scored = sorted(((_sim(title, (r.get("txt") or "")), r) for r in results), key=lambda x: -x[0])
    best_sim, best = scored[0]
    _, n_unique = _dedup_unique(results)
    ali_prices = sorted(p for p in (_parse_price(r.get("price")) for r in results) if p is not None)
    price = ali_prices[len(ali_prices)//2] if ali_prices else None
    strength, points = _grade(best_sim, verified, dmin)
    detail = {"ali": best["url"], "n": len(results), "n_unique": n_unique,
              "sim": round(best_sim, 2), "strength": strength, "points": points,
              "verified": bool(verified), "src": src}
    if dmin is not None:
        detail["hash_dist"] = dmin
    if price is not None:
        detail["ali_price"] = round(price, 2)
    return detail

async def _check_lens(pg, prod):
    """PHASE parallelisable: 2 moteurs reverse-image GRATUITS par URL (zero upload => pas de
    Datadome). Google Lens d'abord (rapide) sur chaque image; si aucun lien AliExpress, Yandex
    (meilleur pour retrouver le produit exact sur AliExpress). Retourne (hit, via, detail).
    PRECISION: un match ne compte comme hit QUE si l'image est confirmee (exact|strong via
    _grade). Si Lens trouve un lien mais l'AliExpress vignette est trop differente (weak),
    on essaie la prochaine image avant de declarer un miss."""
    title = prod.get("title", "")
    imgs = [u for u in (prod.get("image_urls") or [prod.get("image_url")]) if u][:3]
    if not (TRY_IMAGE and imgs):
        return (False, "no_image", {})
    # 1) Google Lens sur chaque image. PRECISION: arret au 1er hit dont l'image est CONFIRMEE
    # (exact|strong). Un resultat "weak" (lien trouve mais image differente) ne suffit pas =>
    # on essaie une autre vue du produit (peut-etre que la 2e photo matchera mieux).
    captcha = False
    for img in imgs:
        try:
            results = await _lens_ali_search(pg, img)
        except Exception:
            results = []
        if results is None:             # Lens a servi un captcha => IP a rotater, on n'insiste pas
            captcha = True
            break
        if results:
            ok, vr, dmin = await _verified(img, results)
            # CONFIRMATION page produit = SOURCE DE VERITE: la vraie photo AliExpress (og:image)
            # remplace le crop Lens bruite. Si une page confirme => hit fort; si une page lue
            # montre une image DIFFERENTE (dpage > STRONG_MAX) => on rejette (precision: Lens a
            # lie un lien mais ce n'est pas le meme produit). Pages bloquees (Datadome, dpage=None)
            # => on garde le grading Lens (recall preserve).
            conf = False
            if CONFIRM_PAGE:
                import asyncio as _a
                eh = await _a.to_thread(lambda: _ahash(_download(img)))
                conf, dpage = await _confirm_via_page(pg, eh, title, results)
                if dpage is not None:
                    dmin = dpage; vr = True
            d = _build_detail(title, results, "aliexpress", verified=vr, dmin=dmin)
            d["page_confirmed"] = bool(conf)
            if _is_hit(d.get("strength")):
                return (True, "image", d)
    # 2) Yandex sur chaque image (2e moteur, recall AliExpress superieur)
    if YANDEX_FALLBACK:
        for img in imgs:
            try:
                ry = await _yandex_ali_search(pg, img)
            except Exception:
                ry = []
            if ry:
                ok, vr, dmin = await _verified(img, ry)
                d = _build_detail(title, ry, "aliexpress", verified=vr, dmin=dmin)
                if _is_hit(d.get("strength")):
                    return (True, "yandex", d)
    if captcha:                         # aucun hit ET captcha => via=captcha (=> retry proxy rotate)
        return (False, "captcha", {"n": 0})
    return (False, "image", {"n": 0})

async def _verified(img, results):
    """Confronte les vignettes resultats a l'image Etsy `img` par hash perceptuel.
    Retourne (resultats_a_utiliser, verified_bool, distance_hamming_min).
    - ADVISORY (defaut): garde TOUS les resultats, verified=True si >=1 vignette ~identique.
    - GATING (ALI_VERIFY_GATE=1): ne garde QUE les vignettes ~identiques; [] si aucune
      (=> pas de match) MAIS seulement quand des vignettes etaient comparables (sinon recall)."""
    if not VERIFY:
        return results, False, None
    import asyncio as _a
    eh = await _a.to_thread(lambda: _ahash(_download(img)))
    kept, comparable, dmin = await _a.to_thread(_verify_results, eh, results)
    verified = bool(comparable and kept)
    if VERIFY_GATE and comparable:
        return kept, verified, dmin     # strict: vignettes comparables => exige un match hash
    return results, verified, dmin      # advisory: garde le recall, annote la confiance

async def _confirm_via_page(pg, etsy_hash, title, results, topn=CONFIRM_TOPN):
    """PREUVE FORTE: ouvre les TOPN candidats AliExpress (tries par sim titre) et compare la
    VRAIE photo produit (meta og:image de la page item) a la photo Etsy par hash perceptuel.
    La page produit donne l'image SOURCE (pas le crop Google de Lens) => hash fiable.
    Retourne (confirmed_bool, dmin_page) ou (False, None). dmin_page = meilleure distance
    obtenue sur une page reellement ouverte (None si aucune page n'a pu etre lue/hashee)."""
    if etsy_hash is None or not results:
        return False, None
    ranked = sorted(results, key=lambda r: -_sim(title, (r.get("txt") or "")))
    dmin = None
    for r in ranked[:topn]:
        url = r.get("url")
        if not url:
            continue
        try:
            await pg.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception:
            continue
        if _ali_blocked(pg):            # Datadome => on n'insiste pas (pas de penalite)
            continue
        try:
            og = await pg.evaluate(
                """() => { const m=document.querySelector('meta[property="og:image"],meta[name="og:image"]');
                           let s=m?m.content:''; if(!s){const im=document.querySelector('img[src*="alicdn"]'); s=im?im.src:'';}
                           return s||''; }""")
        except Exception:
            og = ""
        if not og:
            continue
        import asyncio as _a
        h = await _a.to_thread(lambda: _ahash(_download(og)))
        if h is None:
            continue
        d = _hamming(etsy_hash, h)
        dmin = d if dmin is None else min(dmin, d)
        if d <= _HASH_STRONG_MAX:       # confirme: meme image a la source AliExpress
            return True, dmin
    return False, dmin

async def _check_native(pg, prod):
    """PHASE 2 (SERIE, pas en parallele sinon Datadome): recherche image NATIVE AliExpress
    sur la 1re image. Retourne (hit, via, detail). Etape lente (~10s) mais precise + prix
    exacts. Captcha => pas de hit, boutique NON penalisee."""
    title = prod.get("title", "")
    imgs = [u for u in (prod.get("image_urls") or [prod.get("image_url")]) if u][:2]
    if not (NATIVE_FALLBACK and imgs):
        return (False, "image", {"n": 0})
    # essaie jusqu'a 2 images: si la 1re est captcha Datadome (variable), la 2e passe souvent
    for img in imgs:
        try:
            nat, blocked = await _ali_native_search(pg, img)
        except Exception:
            nat, blocked = None, False
        if nat:
            ok, vr, dmin = await _verified(img, nat)
            if ok:
                d = _build_detail(title, ok, "aliexpress_native", verified=vr, dmin=dmin)
                if _is_hit(d.get("strength")):       # HIT seulement si IMAGE confirmee
                    return (True, "native", d)
        if not blocked:
            break                       # pas de captcha mais 0 resultat => 2e image inutile
    return (False, "image", {"n": 0})

async def _validate(products, min_match, hash_thresh, sim_thresh, headless, test_all=False):
    res = {"checked": 0, "hits": 0, "total": len(products), "via": {}, "matches": []}
    if not ENGINE_OK or not products:
        res["error"] = "moteur navigateur indisponible" if not ENGINE_OK else "pas de produit"
        res["validated"] = False
        return res
    # Google Lens captcha SYSTEMATIQUEMENT les navigateurs headless (page /sorry/ =
    # "unusual traffic"). En mode visible il ne challenge pas. On FORCE donc le mode
    # visible et on pousse la fenetre hors-ecran (comme scraper.py) pour ne pas gener.
    headless = False
    import os as _os3
    try: conc = max(1, int(_os3.environ.get("ALI_CONC", "3")))
    except Exception: conc = 3
    # ROUNDS anti-captcha: si Lens challenge (via=captcha), on RELANCE le navigateur avec une
    # nouvelle IP (proxy suivant) + nouveau UA + stealth, et on RE-teste UNIQUEMENT les produits
    # captcha'd. Borne par ALI_PROXY_ROUNDS (def 2) ET par la dispo de proxies (sans pool, 1 seul
    # round: rotater l'UA sans changer d'IP ne leve pas un captcha deja servi).
    try: max_rounds = max(1, int(_os3.environ.get("ALI_PROXY_ROUNDS", "2")))
    except Exception: max_rounds = 2
    pool = _proxy_pool()
    # MOTEUR: scrapling (camoufox furtif, MEME anti-bot que scraper.py) par defaut s'il est
    # installe => bien plus resistant aux captchas Lens que patchright chromium. Override
    # ALI_ENGINE=patchright pour forcer l'ancien moteur.
    # MOTEUR CDP: si ALI_CDP_URL est defini (ton vrai Chrome lance en mode debug via
    # ali_chrome.py), on s'y CONNECTE => session Google deja connectee (DBSC valide car MEME
    # appareil) => Lens repond sans captcha ni mur login. Prioritaire s'il est configure.
    cdp_url = _os3.environ.get("ALI_CDP_URL", "").strip()
    # MOTEUR PAR DEFAUT = cdp (voie prouvee: vrai Chrome connecte, pas de captcha/Datadome).
    # Override ALI_ENGINE=scrapling|patchright. ALI_CDP_AUTO=0 desactive l'auto-lancement Chrome.
    engine = _os3.environ.get("ALI_ENGINE", "cdp").lower()
    if engine == "cdp":
        if not cdp_url:
            cdp_url = "http://localhost:9222"
        # AUTO-LANCEMENT: le serveur n'a aucune manip a faire. On garantit un Chrome debug
        # joignable (le lance si besoin, reutilise le profil dedie deja connecte a Google).
        if _os3.environ.get("ALI_CDP_AUTO", "1") not in ("0", "false", "no"):
            try:
                import ali_chrome
                url = await asyncio.to_thread(ali_chrome.ensure_chrome)
                if url:
                    cdp_url = url
                elif SCRAPLING_OK:           # Chrome indispo => repli automatique
                    engine = "scrapling"
            except Exception:
                if SCRAPLING_OK:
                    engine = "scrapling"
    if engine == "scrapling" and not SCRAPLING_OK:
        engine = "patchright"

    # --- moteur SCRAPLING: 1 AsyncStealthySession (camoufox), pages via page_action. Comme
    # une page camoufox EST une page Playwright standard, _check_lens tourne dessus sans
    # modification (goto/evaluate/scroll identiques). ---
    async def _round_scrapling(proxy_raw, prods):
        # PAS d'init_script ici: camoufox (scrapling stealth) est deja furtif, et passer notre
        # init_script casse la resolution DNS du contexte (ERR_NAME_NOT_RESOLVED sur tout goto).
        # Le stealth JS ne sert qu'au moteur patchright/chromium (cf _round_patchright).
        kw = dict(headless=headless, max_pages=max(1, min(conc, len(prods))), network_idle=False,
                  block_webrtc=True, hide_canvas=True,
                  useragent=_pick_ua(), disable_resources=False)
        # PROFIL PERSISTANT: reutilise les cookies (login Google) => moins de captcha Lens.
        if _PROFILE_DIR:
            try: _os.makedirs(_PROFILE_DIR, exist_ok=True)
            except Exception: pass
            kw["user_data_dir"] = _PROFILE_DIR
            # un profil connecte ne doit PAS changer d'UA a chaque run (incoherence detectee
            # par Google) => UA fixe quand on a un profil persistant.
            kw["useragent"] = _UA_POOL[0]
        if proxy_raw: kw["proxy"] = proxy_raw
        sess = AsyncStealthySession(**kw)
        await sess.start()
        try:
            import scraper; scraper.start_window_hider()
        except Exception: pass
        sem = asyncio.Semaphore(conc)
        async def _one(prod):
            async with sem:
                holder = {"r": (False, "erreur", {})}
                async def act(page):
                    try:
                        await _inject_cookies_once(page)
                        holder["r"] = await _check_lens(page, prod)
                    except Exception: holder["r"] = (False, "erreur", {})
                    return page
                try:
                    await sess.fetch("https://lens.google.com/", page_action=act,
                                     network_idle=False, load_dom=False, timeout=70000)
                except Exception:
                    pass
                return prod, holder["r"]
        try:
            out = await asyncio.gather(*[_one(p) for p in prods])
        finally:
            try: await sess.close()
            except Exception: pass
        return {id(prod): r for prod, r in out}

    # --- moteur PATCHRIGHT (fallback): contexte chromium + UA/stealth + pages paralleles. ---
    async def _round_patchright(p, proxy_raw, prods):
        kw = {"headless": headless}
        pw = _to_pw_proxy(proxy_raw)
        if pw: kw["proxy"] = pw
        br = await p.chromium.launch(**kw)
        ctx = await br.new_context(locale="fr-FR", viewport={"width":1440,"height":900},
                                   user_agent=_pick_ua())
        try: await ctx.add_init_script(_STEALTH_JS)
        except Exception: pass
        try:
            import scraper; scraper.start_window_hider()
        except Exception: pass
        sem = asyncio.Semaphore(conc)
        async def _one(prod):
            async with sem:
                pg = await ctx.new_page()
                try:
                    await _inject_cookies_once(pg)
                    return prod, await _check_lens(pg, prod)
                except Exception:
                    return prod, (False, "erreur", {})
                finally:
                    try: await pg.close()
                    except Exception: pass
        try:
            out = await asyncio.gather(*[_one(p) for p in prods])
        finally:
            try: await br.close()
            except Exception: pass
        return {id(prod): r for prod, r in out}

    # --- moteur CDP: connexion a TON Chrome reel (lance par ali_chrome.py avec
    # --remote-debugging-port). On REUTILISE son contexte (deja connecte a Google) =>
    # DBSC valide (meme machine) => Lens repond. On NE ferme PAS le navigateur (c'est le
    # tien): on ouvre/ferme seulement des onglets. ---
    async def _round_cdp(p, prods):
        try:
            br = await p.chromium.connect_over_cdp(cdp_url, timeout=15000)
        except Exception as ex:
            # Chrome debug injoignable => on signale clairement (pas un captcha boutique)
            res["error"] = f"CDP injoignable ({cdp_url}): lance d'abord ali_chrome.py"
            return {id(pr): (False, "erreur", {}) for pr in prods}
        # contexte existant (ta session connectee); fallback: en creer un
        ctx = br.contexts[0] if br.contexts else await br.new_context()
        sem = asyncio.Semaphore(conc)
        async def _one(prod):
            async with sem:
                pg = await ctx.new_page()
                try:
                    return prod, await _check_lens(pg, prod)
                except Exception:
                    return prod, (False, "erreur", {})
                finally:
                    try: await pg.close()
                    except Exception: pass
        try:
            out = await asyncio.gather(*[_one(p) for p in prods])
        finally:
            try: await br.close()          # detache la connexion CDP, ne tue PAS ton Chrome
            except Exception: pass
        return {id(prod): r for prod, r in out}

    async def _drive(p):
        """Boucle de rounds anti-captcha commune aux moteurs. p = playwright ctx (patchright/cdp)
        ou None (scrapling). Remplit `outcome` et flag res['blocked'] si captcha persistant."""
        outcome = {}
        todo = list(products)
        proxy_raw = _next_proxy_raw() if pool else None
        for rnd in range(max_rounds):
            if engine == "scrapling":
                res_round = await _round_scrapling(proxy_raw, todo)
            elif engine == "cdp":
                res_round = await _round_cdp(p, todo)
            else:
                res_round = await _round_patchright(p, proxy_raw, todo)
            outcome.update(res_round)
            todo = [pr for pr in todo if outcome.get(id(pr), (0, "", 0))[1] == "captcha"]
            if not todo or not pool:
                break
            proxy_raw = _next_proxy_raw()         # IP suivante au round suivant
        if todo:
            res["blocked"] = True
        return outcome

    if engine == "scrapling":
        outcome = await _drive(None)
    else:                                   # patchright ET cdp ont besoin du contexte playwright
        async with async_playwright() as p:
            outcome = await _drive(p)
    for prod in products:
        hit, via, detail = outcome[id(prod)]
        res["checked"] += 1
        res["via"][via] = res["via"].get(via, 0) + 1
        if hit:
            res["hits"] += 1
            res["matches"].append({"title": prod.get("title","")[:60], "via": via, **detail})
    if res.get("blocked"):
        res["validated"] = None   # inconnu (AliExpress a bloque)
    else:
        res["validated"] = res["hits"] >= min_match
    # Agrege les prix AliExpress trouves (signal dropship + estimation cout d'achat).
    prices = [m["ali_price"] for m in res["matches"] if m.get("ali_price") is not None]
    if prices:
        sp = sorted(prices)
        res["ali_price_min"] = round(sp[0], 2)
        res["ali_price_avg"] = round(sum(sp) / len(sp), 2)
        res["ali_price_med"] = round(sp[len(sp)//2], 2)   # robuste au bruit (cartes parasites)
        res["ali_prices"] = sp
    # COUVERTURE: part des produits TESTES trouves identiques sur AliExpress. Fiable
    # seulement si test_all (sinon on s'arrete tot => denominateur partiel). 0..1.
    tested = res["checked"] or 0
    res["coverage"] = round(res["hits"] / tested, 3) if tested else 0.0
    return res

def validate_shop(products, min_match=3, hash_thresh=12, sim_thresh=0.30, headless=None, test_all=False):
    """Sync: valide une boutique. products = [{title, image_url}]. Tente image/produit,
    fallback texte si l'upload image est bloque. Boutique validee si >= min_match trouves.
    headless=None => non-headless par defaut (patchright stealth passe mieux l'anti-bot
    AliExpress/Datadome). Override via env ALI_HEADLESS=1."""
    if not ENGINE_OK:
        return {"validated": False, "hits": 0, "total": len(products or []), "error": "moteur navigateur indisponible"}
    if headless is None:
        import os as _os
        headless = _os.environ.get("ALI_HEADLESS", "0") in ("1", "true", "yes")
    return _run(_validate(products or [], min_match, hash_thresh, sim_thresh, headless, test_all))
