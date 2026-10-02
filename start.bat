@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "RUN_TESTS=false"
set "TESTS_ONLY=false"
set "RESET=false"
set "PORT=8501"

:parse_args
if "%~1"=="" goto args_parsed
if /i "%~1"=="--run-tests" (
    set "RUN_TESTS=true"
    shift
    goto parse_args
)
if /i "%~1"=="--tests-only" (
    set "TESTS_ONLY=true"
    set "RUN_TESTS=true"
    shift
    goto parse_args
)
if /i "%~1"=="--skip-tests" (
    set "RUN_TESTS=false"
    shift
    goto parse_args
)
if /i "%~1"=="--reset" (
    set "RESET=true"
    shift
    goto parse_args
)
if /i "%~1"=="--port" (
    if "%~2"=="" goto missing_port
    set "PORT=%~2"
    shift
    shift
    goto parse_args
)
if /i "%~1"=="-h" goto show_help
if /i "%~1"=="--help" goto show_help
echo Unknown option: %~1 1>&2
exit /b 1

:args_parsed
set "PYTHON="
where py >nul 2>&1
if not errorlevel 1 (
    for %%V in (3.14 3.13 3.12 3.11 3.10) do (
        py -%%V -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON=py -%%V"
            goto python_found
        )
    )
)

python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 set "PYTHON=python"
if not defined PYTHON goto python_not_found

:python_found
echo.
echo ===============================================
echo   Negotium - Investment Tracker
echo ===============================================

if "%RESET%"=="true" (
    echo.
    echo --reset: removing all data files...
    if exist "data" rmdir /s /q "data"
    if errorlevel 1 (
        echo Failed to remove data directory. Close the app and try again. 1>&2
        exit /b 1
    )
    echo Data cleared.
)

if not exist "data" mkdir "data"

set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo.
    echo Creating virtual environment at .venv...
    %PYTHON% -m venv "%SCRIPT_DIR%.venv"
    if errorlevel 1 goto venv_failed
    echo Installing dependencies from requirements.txt...
    "%VENV_PY%" -m pip install --quiet --upgrade pip
    if errorlevel 1 goto install_failed
    "%VENV_PY%" -m pip install --quiet -r "%SCRIPT_DIR%requirements.txt"
    if errorlevel 1 goto install_failed
) else (
    "%VENV_PY%" -c "import streamlit, yfinance, plotly, pandas, orjson, openpyxl, python_calamine, pytest" >nul 2>&1
    if errorlevel 1 (
        echo Installing missing dependencies from requirements.txt...
        "%VENV_PY%" -m pip install --quiet -r "%SCRIPT_DIR%requirements.txt"
        if errorlevel 1 goto install_failed
    )
)

echo.
"%VENV_PY%" --version
echo Project: %SCRIPT_DIR%
echo.

if "%RUN_TESTS%"=="true" (
    echo Running tests...
    "%VENV_PY%" -m pytest tests/
    if errorlevel 1 (
        echo Tests failed. 1>&2
        exit /b 1
    )
    echo.
)

if "%TESTS_ONLY%"=="true" (
    echo Tests complete.
    exit /b 0
)

echo Starting app at http://localhost:%PORT%
echo Press Ctrl+C to stop.
echo.
"%VENV_PY%" -m streamlit run src/app.py --server.port %PORT% --server.headless true --server.runOnSave true --browser.gatherUsageStats false
exit /b %ERRORLEVEL%

:missing_port
echo Missing value for --port. 1>&2
exit /b 1

:python_not_found
echo Python 3.10 or newer was not found. Install Python and try again. 1>&2
exit /b 1

:venv_failed
echo Could not create the virtual environment. 1>&2
exit /b 1

:install_failed
echo Dependency installation failed. Check your internet connection and try again. 1>&2
exit /b 1

:show_help
echo Usage: start.bat [options]
echo   --run-tests   Run tests, then start the app
echo   --tests-only  Run tests and exit
echo   --skip-tests  Skip tests on launch (default)
echo   --port N      Use port N (default 8501)
echo   --reset       Delete data/ and start fresh
echo   -h, --help    Show this help
exit /b 0