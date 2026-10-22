@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found. Create .venv and install requirements.txt first.
    popd
    exit /b 2
)
echo Stop the LawRAG server before collection. Completed versions will be reused.
".venv\Scripts\python.exe" -m app.preload_admrul %*
set "lawrag_admrul_exit=%ERRORLEVEL%"
popd
exit /b %lawrag_admrul_exit%
