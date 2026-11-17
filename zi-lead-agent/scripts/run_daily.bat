@echo off
REM Daily run for Windows Task Scheduler. Add --live once the live setup is verified.
cd /d "%~dp0.."
if not exist logs mkdir logs
python run_agent.py run-daily %* >> logs\scheduler_daily.log 2>&1
