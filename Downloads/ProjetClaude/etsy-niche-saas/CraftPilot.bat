@echo off
REM Lance CraftPilot SANS fenetre terminal qui reste ouverte.
REM Double-clique ce fichier. Le serveur tourne en arriere-plan (pythonw = pas de console),
REM le navigateur s'ouvre sur l'app, et le Chrome debug (detection dropship) se lance tout seul.
REM 1re fois: connecte-toi a Google dans la fenetre Chrome qui s'ouvre (une seule fois).

cd /d "%~dp0"

REM pythonw = Python sans console. Repli sur python si pythonw absent.
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw server.py
) else (
    start "" /min python server.py
)

REM laisse le serveur demarrer puis ouvre l'app
timeout /t 3 /nobreak >nul
start "" http://localhost:8000

REM cette fenetre se ferme immediatement (le serveur continue en arriere-plan)
exit
