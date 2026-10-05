@echo off
chcp 65001 >nul
title Cai dat Phong_DHue - Video Story Publisher V23.6 (may moi)
setlocal

echo ============================================================
echo   CAI DAT PHONG_DHUE  (may moi)
echo ------------------------------------------------------------
echo   Chi chay file nay MOT LAN. Sau do moi lan cap nhat
echo   chi can bam nut "Cap nhat" trong tool.
echo ============================================================
echo.

set "DEST=%~dp0Phong_DHue"
set "ZIP=%TEMP%\phong_dhue_install.zip"
set "URL=https://api.github.com/repos/NaupUuh/Phong_DHue/zipball/main"

echo [1/4] Dang tai tool tu GitHub...
powershell -NoProfile -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "Invoke-WebRequest -Uri '%URL%' -OutFile '%ZIP%' -Headers @{ 'Accept'='application/vnd.github+json' } -UseBasicParsing"
if not exist "%ZIP%" (
  echo   LOI: khong tai duoc. Kiem tra mang roi chay lai.
  pause & exit /b 1
)

echo [2/4] Dang giai nen...
if exist "%TEMP%\phong_dhue_x" rmdir /s /q "%TEMP%\phong_dhue_x"
powershell -NoProfile -Command ^
  "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%TEMP%\phong_dhue_x' -Force"
if not exist "%TEMP%\phong_dhue_x" (
  echo   LOI: giai nen that bai.
  pause & exit /b 1
)

echo [3/4] Dang chep vao: %DEST%
if not exist "%DEST%" mkdir "%DEST%"
for /d %%D in ("%TEMP%\phong_dhue_x\*") do (
  xcopy "%%D\*" "%DEST%\" /E /I /Y /Q >nul
)
del "%ZIP%" >nul 2>&1
rmdir /s /q "%TEMP%\phong_dhue_x" >nul 2>&1

if not exist "%DEST%\viet_drama_V23.6_dashboard_thumbnail.py" (
  echo   LOI: thieu file chinh - cai dat that bai.
  pause & exit /b 1
)

echo [4/4] Xong.
echo.
echo ============================================================
echo   DA CAI DAT vao:  %DEST%
echo.
echo   Mo tool bang:  CHAY_Phong_DHue.bat
echo   Lan sau co ban moi: mo tool roi bam nut "Cap nhat"
echo ============================================================
echo.
choice /C YN /N /M "Mo tool ngay bay gio? [Y/N] "
if errorlevel 2 goto :end
start "" "%DEST%\CHAY_Phong_DHue.bat"

:end
endlocal
