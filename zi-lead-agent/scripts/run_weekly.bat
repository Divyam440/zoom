@echo off
REM Weekly radar + report for Windows Task Scheduler. Add --live to the radar once verified.
cd /d "%~dp0.."
if not exist logs mkdir logs
python run_agent.py radar-weekly %* >> logs\scheduler_weekly.log 2>&1
python run_agent.py report-weekly >> logs\scheduler_weekly.log 2>&1
