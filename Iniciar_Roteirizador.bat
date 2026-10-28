@echo off
cd /d C:\ROTEIRIZADOR

call .venv\Scripts\activate.bat

start "" cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8501"

python -m streamlit run app.py --server.headless=true

pause