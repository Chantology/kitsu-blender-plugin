@echo off
setlocal enabledelayedexpansion

rem Installs the Kitsu Blender Plugin into Blender's user "scripts\addons"
rem folder(s).
rem
rem Usage: install_windows.cmd [custom scripts folder]
rem   Run with no arguments to auto-detect every
rem   "%%APPDATA%%\Blender Foundation\Blender\<major>.<minor>" folder and
rem   install into all of them (so every installed Blender version gets the
rem   plugin in one pass). If you use a custom Blender user config
rem   directory, pass that version folder as an argument instead, e.g.
rem   install_windows.cmd "D:\BlenderConfig\5.2".

set "SCRIPT_DIR=%~dp0"
set "SOURCE=%SCRIPT_DIR%kitsu_blender_plugin"

if not exist "%SOURCE%\__init__.py" (
    echo ERROR: %SOURCE%\__init__.py not found.
    echo Run this script from the repository root, or check your checkout.
    exit /b 1
)

set "LISTFILE=%TEMP%\kitsu_blender_plugin_install_targets.txt"
if exist "%LISTFILE%" del "%LISTFILE%"

if not "%~1"=="" (
    echo %~1>>"%LISTFILE%"
) else (
    for /d %%D in ("%APPDATA%\Blender Foundation\Blender\*") do echo %%D>>"%LISTFILE%"
)

if not exist "%LISTFILE%" (
    echo ERROR: No "%%APPDATA%%\Blender Foundation\Blender\<version>" folder found.
    echo Launch Blender at least once to create it, then re-run this script -
    echo or pass your Blender version config folder as an argument.
    exit /b 1
)

for /f "usebackq delims=" %%T in ("%LISTFILE%") do (
    set "TARGET=%%T\scripts\addons"
    echo Installing Kitsu Blender Plugin
    echo   From: %SOURCE%
    echo   To:   !TARGET!\kitsu_blender_plugin
    echo.

    if not exist "!TARGET!" mkdir "!TARGET!"

    rem Remove any previous install of this plugin's own folder first, for
    rem a clean, idempotent install that never leaves stale files from an
    rem older version behind.
    if exist "!TARGET!\kitsu_blender_plugin" rmdir /s /q "!TARGET!\kitsu_blender_plugin"

    robocopy "%SOURCE%" "!TARGET!\kitsu_blender_plugin" /E /NFL /NDL /NJH /NJS >nul
    if errorlevel 8 (
        echo ERROR: Could not copy the plugin folder to !TARGET!.
    )
)

del "%LISTFILE%"

echo.
echo Done. Restart Blender, then enable "Kitsu" in
echo Edit ^> Preferences ^> Add-ons (search "Kitsu"). The panel appears in
echo the 3D Viewport's sidebar (press N), under the "Kitsu" tab.
pause
endlocal
