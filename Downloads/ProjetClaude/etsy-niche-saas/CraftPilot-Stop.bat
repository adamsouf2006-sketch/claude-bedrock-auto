@echo off
REM Arrete le serveur CraftPilot lance en arriere-plan par CraftPilot.bat.
REM Tue uniquement le(s) process python qui executent server.py (pas tes autres python).

for /f "tokens=2 delims==; " %%P in ('wmic process where "name='pythonw.exe' and CommandLine like '%%server.py%%'" get ProcessId /value 2^>nul ^| find "="') do taskkill /F /PID %%P >nul 2>nul
for /f "tokens=2 delims==; " %%P in ('wmic process where "name='python.exe' and CommandLine like '%%server.py%%'" get ProcessId /value 2^>nul ^| find "="') do taskkill /F /PID %%P >nul 2>nul

echo CraftPilot arrete.
timeout /t 2 /nobreak >nul
exit
