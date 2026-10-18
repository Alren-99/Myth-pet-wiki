$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ($env:OS -ne 'Windows_NT') { throw 'Execute no Windows 11 x64.' }
py -3 -m venv .venv
if ($LASTEXITCODE) { throw 'Falha ao criar ambiente Python.' }
& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
if ($LASTEXITCODE) { throw 'Falha ao instalar compilador.' }
& .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --onefile --windowed --uac-admin --name 'AMD GPU Recovery Tool' app.py
if ($LASTEXITCODE) { throw 'Falha na compilação.' }
Write-Host 'Executável: dist\AMD GPU Recovery Tool.exe'
