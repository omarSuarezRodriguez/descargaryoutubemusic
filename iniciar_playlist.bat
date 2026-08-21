@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt -q
start "" pythonw descargar_playlist.py
exit
