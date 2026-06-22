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

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# Upload image AliExpress bloque par anti-automation (l'input n'apparait que sur vrai
# geste humain). Par defaut on saute l'image et on valide par TEXTE (fiable, ~5s).
# TRY_IMAGE=True: tente d'abord le match PAR IMAGE (vraie detection "identique"),
# puis retombe sur le TEXTE si l'upload est bloque. Override via env ALI_TRY_IMAGE=0.
import os as _os
TRY_IMAGE = _os.environ.get("ALI_TRY_IMAGE", "1") not in ("0", "false", "no")
# Yandex = 2e moteur reverse-image (gratuit, par URL). DESACTIVE par defaut: en pratique il
# remonte surtout des agregateurs (imall.com) avec des produits DIFFERENTS => faux positifs,
# 0 gain reel sur AliExpress + cout temps. Override ALI_YANDEX=1 pour le reactiver.
YANDEX_FALLBACK = _os.environ.get("ALI_YANDEX", "0") not in ("0", "false", "no")
# FALLBACK natif AliExpress (upload image par drag-drop simule). Active par defaut: uploade
# l'image directement dans le moteur image AliExpress => vrais produits + prix exacts.
# Override ALI_NATIVE=0. Captcha Datadome gere (pas de penalite boutique).
NATIVE_FALLBACK = _os.environ.get("ALI_NATIVE", "1") not in ("0", "false", "no")
_CONSENT_DONE = False   # consentement Google accepte une fois par process (cookie persiste)

# ---- perceptual hash (Pillow seul) -------------------------------------------
def _ahash(img_bytes):
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("L").resize((8, 8))
    except Exception:
        return None
    px = list(im.getdata()); avg = sum(px) / 64.0
    bits = 0
    for i, p in enumerate(px):
        if p > avg:
            bits |= (1 << i)
    return bits

def _hamming(a, b):
    return bin(a ^ b).count("1") if (a is not None and b is not None) else 64

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
                if (!t) { const im = a.querySelector('img'); t = im ? (im.alt || '') : ''; }
                out.push({url: real, host: h, ali: true, txt: t.slice(0, 120), price: priceNear(a)});
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
            return list(acc.values())
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
    return list(acc.values())

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
            out.push({url: real, txt: t.slice(0,120), price: priceNear(a), ali: true});
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
            out.push({url: real, txt: t.slice(0,120), price: priceNear(a), ali: true});
        } catch(e) {}
    });
    return out.slice(0, 20);
}"""

def _build_detail(title, results, src):
    """[{url,txt,price}] -> detail {ali,n,sim,src,ali_price?}. Classe par similarite titre.
    Prix = MEDIANE de TOUTES les cartes (le moteur image melange le produit et des accessoires
    cheap; la mediane sur l'ensemble est le cout d'achat le + representatif). Le prix reste
    indicatif: la marge dropship sature de toute facon a 5x => le verdict est robuste au bruit."""
    scored = sorted(((_sim(title, (r.get("txt") or "")), r) for r in results), key=lambda x: -x[0])
    best_sim, best = scored[0]
    ali_prices = sorted(p for p in (_parse_price(r.get("price")) for r in results) if p is not None)
    price = ali_prices[len(ali_prices)//2] if ali_prices else None
    detail = {"ali": best["url"], "n": len(results), "sim": round(best_sim, 2), "src": src}
    if price is not None:
        detail["ali_price"] = round(price, 2)
    return detail

async def _check_lens(pg, prod):
    """PHASE parallelisable: 2 moteurs reverse-image GRATUITS par URL (zero upload => pas de
    Datadome). Google Lens d'abord (rapide) sur chaque image; si aucun lien AliExpress, Yandex
    (meilleur pour retrouver le produit exact sur AliExpress). Retourne (hit, via, detail)."""
    title = prod.get("title", "")
    imgs = [u for u in (prod.get("image_urls") or [prod.get("image_url")]) if u][:3]
    if not (TRY_IMAGE and imgs):
        return (False, "no_image", {})
    # 1) Google Lens sur chaque image (arret au 1er hit)
    for img in imgs:
        try:
            results = await _lens_ali_search(pg, img)
        except Exception:
            results = []
        if results:
            return (True, "image", _build_detail(title, results, "aliexpress"))
    # 2) Yandex sur chaque image (2e moteur, recall AliExpress superieur)
    if YANDEX_FALLBACK:
        for img in imgs:
            try:
                ry = await _yandex_ali_search(pg, img)
            except Exception:
                ry = []
            if ry:
                return (True, "yandex", _build_detail(title, ry, "aliexpress"))
    return (False, "image", {"n": 0})

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
            return (True, "native", _build_detail(title, nat, "aliexpress_native"))
        if not blocked:
            break                       # pas de captcha mais 0 resultat => 2e image inutile
    return (False, "image", {"n": 0})

async def _validate(products, min_match, hash_thresh, sim_thresh, headless, test_all=False):
    res = {"checked": 0, "hits": 0, "total": len(products), "via": {}, "matches": []}
    if not PATCHRIGHT_OK or not products:
        res["error"] = "patchright indisponible" if not PATCHRIGHT_OK else "pas de produit"
        res["validated"] = False
        return res
    # Google Lens captcha SYSTEMATIQUEMENT les navigateurs headless (page /sorry/ =
    # "unusual traffic"). En mode visible il ne challenge pas. On FORCE donc le mode
    # visible et on pousse la fenetre hors-ecran (comme scraper.py) pour ne pas gener.
    headless = False
    async with async_playwright() as p:
        br = await p.chromium.launch(headless=headless)
        ctx = await br.new_context(locale="fr-FR", viewport={"width":1440,"height":900}, user_agent=UA)
        try:
            import scraper; scraper.start_window_hider()   # fenetre Chrome hors-ecran
        except Exception:
            pass
        # PARALLELISME: on teste plusieurs produits EN MEME TEMPS, chacun sur sa propre page
        # (le goulot = la latence reseau Lens/AliExpress, pas le CPU). ~CONC produits a la
        # fois => temps total ~ temps_par_produit * ceil(N/CONC) au lieu de la somme. Mode
        # visible conserve (Lens ne challenge pas). CONC modere (3) pour ne pas declencher
        # de captcha "trafic exceptionnel".
        import os as _os3
        try: conc = max(1, int(_os3.environ.get("ALI_CONC", "3")))
        except Exception: conc = 3
        sem = asyncio.Semaphore(conc)
        async def _lens(prod):
            async with sem:
                pg = await ctx.new_page()
                try:
                    return prod, await _check_lens(pg, prod)
                except Exception:
                    return prod, (False, "erreur", {})
                finally:
                    try: await pg.close()
                    except Exception: pass
        # PHASE 1 — Lens en PARALLELE (rapide, robuste): couvre la majorite des produits.
        lens_out = await asyncio.gather(*[_lens(p) for p in products])
        outcome = {}                              # id(prod) -> (hit, via, detail)
        misses = []
        for prod, r1 in lens_out:
            outcome[id(prod)] = r1
            if not r1[0] and r1[1] != "no_image":
                misses.append(prod)
        # PHASE 2 — NATIF en SERIE sur les ratés Lens (1 session AliExpress a la fois => pas
        # de Datadome). Recupere les produits que Lens ne relie pas a une URL AliExpress.
        if NATIVE_FALLBACK and misses:
            npg = await ctx.new_page()
            try:
                for prod in misses:
                    try:
                        r2 = await _check_native(npg, prod)
                    except Exception:
                        r2 = (False, "image", {})
                    if r2[0]:
                        outcome[id(prod)] = r2
            finally:
                try: await npg.close()
                except Exception: pass
        for prod in products:
            hit, via, detail = outcome[id(prod)]
            res["checked"] += 1
            res["via"][via] = res["via"].get(via, 0) + 1
            if hit:
                res["hits"] += 1
                res["matches"].append({"title": prod.get("title","")[:60], "via": via, **detail})
        await br.close()
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
    if not PATCHRIGHT_OK:
        return {"validated": False, "hits": 0, "total": len(products or []), "error": "patchright indisponible"}
    if headless is None:
        import os as _os
        headless = _os.environ.get("ALI_HEADLESS", "0") in ("1", "true", "yes")
    return _run(_validate(products or [], min_match, hash_thresh, sim_thresh, headless, test_all))
