@echo off
set USE_OLLAMA=true
set OLLAMA_MODEL=qwen3:4b
python -m pip install -r requirements.txt
python -m uvicorn main_v2:app --host 0.0.0.0 --port 8000 --reload
pause
