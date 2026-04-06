@echo off
echo Starting Salad Monitor Server...
cd /d "%~dp0"

if not exist config.json (
    copy config.example.json config.json
    echo [WARN] config.json created. Edit it before running again.
    notepad config.json
    pause
    exit /b 1
)

python -c "import flask, apscheduler, requests" 2>nul || pip install -r scripts\python_dependencies.txt
python scripts\server.py
pause
