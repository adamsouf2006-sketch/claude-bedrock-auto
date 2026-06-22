"""Lance TON Chrome reel en mode debug (--remote-debugging-port) sur ton profil habituel,
pour que la detection dropship s'y connecte via CDP (ALI_CDP_URL). Ta session Google reste
connectee et DBSC reste valide (meme machine) => Google Lens repond sans captcha ni mur login.

Usage:
    python ali_chrome.py            # lance Chrome debug + affiche la commande a exporter
    python ali_chrome.py --check    # verifie juste si le port debug repond

Ensuite, dans le terminal qui lance la detection:
    set ALI_CDP_URL=http://localhost:9222    (cmd)
    $env:ALI_CDP_URL="http://localhost:9222" (PowerShell)
    python test_dropship_live.py

IMPORTANT: ferme d'abord toutes tes fenetres Chrome (Chrome est mono-instance: sans fermeture,
le flag debug est ignore). Ce script tente de fermer Chrome proprement si besoin.
"""
import os, sys, time, json, subprocess, urllib.request, glob

PORT = int(os.environ.get("ALI_CDP_PORT", "9222"))

def _chrome_exe():
    cands = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    hits = glob.glob(os.path.expandvars(r"%ProgramFiles%\Google\Chrome*\Application\chrome.exe"))
    return hits[0] if hits else None

def _user_data_dir():
    # profil Chrome par defaut (ta session Google connectee y vit)
    return os.path.expandvars(r"%LocalAppData%\Google\Chrome\User Data")

def _debug_ok():
    try:
        with urllib.request.urlopen(f"http://localhost:{PORT}/json/version", timeout=3) as r:
            d = json.loads(r.read())
            return d.get("Browser", "chrome")
    except Exception:
        return None

def main():
    if "--check" in sys.argv:
        b = _debug_ok()
        print(f"debug port {PORT}:", b or "INJOIGNABLE")
        return
    if _debug_ok():
        print(f"Chrome debug deja actif sur :{PORT}. Rien a faire.")
        print(f'Exporte: $env:ALI_CDP_URL="http://localhost:{PORT}"')
        return
    exe = _chrome_exe()
    if not exe:
        print("chrome.exe introuvable. Installe Chrome ou ajuste _chrome_exe().")
        return
    udd = _user_data_dir()
    print(f"Chrome : {exe}")
    print(f"Profil : {udd}")
    print("\n>>> FERME toutes tes fenetres Chrome maintenant (sinon le flag debug est ignore).")
    input(">>> Quand Chrome est ferme, appuie sur ENTREE... ")
    # relance ton Chrome avec le port debug sur ton vrai profil
    args = [exe, f"--remote-debugging-port={PORT}", f'--user-data-dir={udd}',
            "--restore-last-session", "--no-first-run"]
    subprocess.Popen(args, close_fds=True)
    # attend que le port reponde
    for _ in range(20):
        time.sleep(0.7)
        b = _debug_ok()
        if b:
            print(f"\nOK Chrome debug actif: {b} sur :{PORT}")
            print("Verifie dans Chrome que tu es bien connecte a Google + lens.google.com sans captcha.")
            print(f'\nDans ton terminal detection:\n  $env:ALI_CDP_URL="http://localhost:{PORT}"')
            print("  python test_dropship_live.py")
            return
    print("Port debug ne repond pas. Chrome etait peut-etre deja ouvert sans le flag => referme tout et reessaie.")

if __name__ == "__main__":
    main()
