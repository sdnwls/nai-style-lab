@echo off
rem Installs the embedded Python + _dev\requirements.txt into %1 (default: python\ in the app folder).
rem Needs uv and Windows 10+ (curl, tar). Rerun it after changing requirements.txt or PYVER; it replaces the folder.
setlocal
cd /d "%~dp0.."
set "PYVER=3.14.6"
set "PY=%~1"
if "%PY%"=="" set "PY=python"

if exist "%PY%" rmdir /s /q "%PY%"
mkdir "%PY%" || exit /b 1
curl -fsSL -o "%PY%\embed.zip" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip" || exit /b 1
rem Windows' own tar (bsdtar) reads zip; a Unix tar earlier on PATH (Git's) does not.
"%SystemRoot%\System32\tar.exe" -xf "%PY%\embed.zip" -C "%PY%" || exit /b 1
del "%PY%\embed.zip"

uv pip install -r _dev\requirements.txt --target "%PY%\Lib\site-packages" --python-version 3.14 ^
    --python-platform x86_64-pc-windows-msvc || exit /b 1
rem The embedded Python only searches the paths listed in its ._pth file.
>> "%PY%\python314._pth" echo Lib\site-packages
rem Unzipped with Windows' own extractor, every file keeps the zip's "from the internet" mark, and .NET then refuses
rem the window's DLLs (pythonnet, WebView2): the app would close at once. This lets .NET load them anyway.
for %%e in (python pythonw) do > "%PY%\%%e.exe.config" echo ^<configuration^>^<runtime^>^<loadFromRemoteSources enabled="true"/^>^</runtime^>^</configuration^>
echo Python %PYVER% ready in %PY%\
