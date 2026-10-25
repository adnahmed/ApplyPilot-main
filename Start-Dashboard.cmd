@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo ApplyPilot needs uv to use this source-folder launcher.
  echo Use your existing ApplyPilot Python environment, or install uv first.
  pause
  exit /b 1
)
echo Starting ApplyPilot without opening your default browser.
echo Copy the private dashboard link printed below into BrowserOS.
echo Use the dashboard's Start, Pause and Resume button.
echo Leave this launcher window open while using the dashboard.
uv run applypilot dashboard --no-open
if errorlevel 1 (
  echo.
  echo The dashboard could not start. Review the message above.
  pause
  exit /b 1
)
