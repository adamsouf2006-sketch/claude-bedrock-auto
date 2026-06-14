@echo off
REM Lance le SaaS niche Etsy avec le bon Python (venv).
cd /d "%~dp0"
taskkill /F /FI "WINDOWTITLE eq etsy-saas" >nul 2>&1
title etsy-saas
"C:\Users\Windows\Downloads\ProjetClaude\.venv\Scripts\python.exe" server.py
pause
