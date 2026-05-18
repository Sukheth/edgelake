@echo off
setlocal enabledelayedexpansion

echo.
echo  edgelake setup
echo  ==============

:: 1. Create venv if it doesn't exist
if not exist ".venv\Scripts\activate.bat" (
    echo [1/4] Creating virtual environment...
    python -m venv .venv
) else (
    echo [1/4] Virtual environment already exists, skipping.
)

:: 2. Install dependencies
echo [2/4] Installing dependencies...
.venv\Scripts\pip install -e . --quiet

:: 3. Install Playwright browser
echo [3/4] Installing Playwright Chromium...
.venv\Scripts\playwright install chromium

:: 4. Set up .env
echo [4/4] Configuring .env...
if not exist ".env" (
    copy .env.example .env >nul
    echo      Created .env from .env.example.
) else (
    echo      .env already exists, skipping copy.
)

echo.
echo  Enter your API keys and settings.
echo  Press Enter to keep the current value shown in brackets.
echo.

:: Read existing values from .env
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if "%%A"=="TELEGRAM_BOT_TOKEN"  set _TG=%%B
    if "%%A"=="GEMINI_API_KEY"      set _GEM=%%B
    if "%%A"=="DEFAULT_LOCATION"    set _LOC=%%B
    if "%%A"=="DEFAULT_PROJECT_CODE" set _PROJ=%%B
)

:: Prompt for each required value
set /p "NEW_TG=  Telegram bot token [!_TG!]: "
if "!NEW_TG!"=="" set NEW_TG=!_TG!

set /p "NEW_GEM=  Gemini API key [!_GEM!]: "
if "!NEW_GEM!"=="" set NEW_GEM=!_GEM!

set /p "NEW_LOC=  Default location (e.g. India) [!_LOC!]: "
if "!NEW_LOC!"=="" set NEW_LOC=!_LOC!

set /p "NEW_PROJ=  Default project code [!_PROJ!]: "
if "!NEW_PROJ!"=="" set NEW_PROJ=!_PROJ!

:: Write updated .env
(
    echo TELEGRAM_BOT_TOKEN=!NEW_TG!
    echo GEMINI_API_KEY=!NEW_GEM!
    echo GEMINI_MODEL=gemini-2.0-flash
    echo CHROMERIVER_URL=https://app.eu1.chromeriver.com/
    echo DEFAULT_CATEGORY=Meals - Chocolate/Dessert/Snacks
    echo DEFAULT_CURRENCY=INR
    echo DEFAULT_LOCATION=!NEW_LOC!
    echo DEFAULT_PROJECT_CODE=!NEW_PROJ!
    echo BLINKIT_URL=https://blinkit.com/account/orders
) > .env

echo.
echo  Done! Run:
echo    .venv\Scripts\activate
echo    edgelake telegram        ^<-- start the Telegram bot
echo    edgelake run             ^<-- full Blinkit fetch + upload pipeline
echo.
