@echo off
chcp 65001 >nul
wsl.exe --distribution Ubuntu --exec bash /home/kymar_x_ai/mira-memory-v1/launcher/run-mira.sh start-lan-attached
pause
