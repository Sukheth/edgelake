@echo off
echo.
echo  edgelake bootstrap
echo  ==================

if not exist ".venv\Scripts\activate.bat" (
    echo [1/2] Creating virtual environment...
    python -m venv .venv
) else (
    echo [1/2] Virtual environment already exists, skipping.
)

echo [2/2] Installing dependencies...
.venv\Scripts\pip install -e . --quiet

echo.
echo  Bootstrap done. Running interactive setup...
echo.
.venv\Scripts\edgelake setup

echo.
echo  Activate the venv each session with:
echo    .venv\Scripts\activate
echo.
