@echo off
setlocal
pushd "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -B -X utf8 -s -m app.workflow.server --open-browser
) else (
  echo Create the local .venv and install requirements.txt first. See README.md.
)
popd
pause
