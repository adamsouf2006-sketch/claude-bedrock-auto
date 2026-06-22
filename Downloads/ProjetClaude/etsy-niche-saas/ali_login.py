"""Login Google UNE SEULE FOIS dans le profil persistant utilise par la detection dropship.
Une session Google connectee se fait beaucoup moins captcha par Google Lens => debloque la
detection sans proxy payant.

Usage:
    python ali_login.py

Ouvre une fenetre Chrome (profil = ali_image._PROFILE_DIR). Connecte-toi a ton compte Google,
puis reviens au terminal et appuie sur ENTREE. Les cookies sont sauves dans le profil et
reutilises a chaque run de validate_shop. A refaire seulement si Google te deconnecte.
"""
import asyncio, os
import ali_image as ali
from scrapling.fetchers import AsyncStealthySession

PROFILE = ali._PROFILE_DIR

async def main():
    if not PROFILE:
        print("ALI_PROFILE_DIR vide => pas de profil persistant. Abandon.")
        return
    os.makedirs(PROFILE, exist_ok=True)
    print(f"Profil: {PROFILE}")
    sess = AsyncStealthySession(headless=False, max_pages=1, network_idle=False,
                                block_webrtc=True, hide_canvas=True,
                                useragent=ali._UA_POOL[0], user_data_dir=PROFILE)
    await sess.start()
    async def act(page):
        # 1) login Google
        try:
            await page.goto("https://accounts.google.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception as ex:
            print("nav login:", repr(ex)[:80])
        print("\n>>> Connecte-toi a Google dans la fenetre.")
        print(">>> Va ensuite sur lens.google.com pour verifier qu'il n'y a PAS de captcha.")
        await asyncio.to_thread(input, ">>> Quand c'est fait, appuie sur ENTREE ici... ")
        # 2) verifie l'etat Lens
        try:
            await page.goto("https://lens.google.com/", wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(1500)
            u = page.url or ""
            print("Lens ->", u[:70])
            print("CAPTCHA detecte" if "/sorry/" in u else "OK pas de captcha")
        except Exception as ex:
            print("check lens:", repr(ex)[:80])
        return page
    try:
        await sess.fetch("https://accounts.google.com/", page_action=act,
                         load_dom=False, network_idle=False, timeout=600000)
    finally:
        await sess.close()
    print("Cookies sauves. La detection dropship reutilisera ce profil.")

if __name__ == "__main__":
    asyncio.run(main())
