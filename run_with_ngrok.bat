@echo off
rem Change to the project directory
cd /d "C:\Users\Admin\Desktop\AI automation\Automated_Voice_Agent"

rem Start the FastAPI Voice Agent in a new window
start "AI Voice Agent" cmd /k "python main.py"

rem Give the server a moment to start
timeout /t 5 /nobreak >nul

rem Open an ngrok tunnel to expose port 8000
ngrok http 8000 --domain stimulate-nifty-snitch.ngrok-free.dev
