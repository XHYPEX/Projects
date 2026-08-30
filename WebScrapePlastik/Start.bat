@echo off
setlocal EnableDelayedExpansion
title Aplikasi Toko Plastik
cd /d "%~dp0"

REM ===========================================================================
REM  Aplikasi Toko Plastik - klik dua kali file ini untuk menjalankan aplikasi.
REM
REM  Script ini otomatis: cek update -> pasang yang dibutuhkan -> buka aplikasi.
REM  Database TIDAK disimpan di folder ini, tapi di %LOCALAPPDATA%\TokoPlastik,
REM  supaya update tidak pernah bisa menimpa data penjualan/stok.
REM ===========================================================================

set "APPDATA_DIR=%LOCALAPPDATA%\TokoPlastik"
set "DB_PATH=%APPDATA_DIR%\scraper.db"
set "VENV=%~dp0.venv"
set "HOST=127.0.0.1"
set "PORT=8000"
set "URL=http://127.0.0.1:8000"

echo.
echo   ============================================
echo     APLIKASI TOKO PLASTIK
echo   ============================================
echo.

REM --- 1. Cari Python ---------------------------------------------------------
REM py.exe (Python Launcher) ikut terpasang dari installer python.org dan paling
REM bisa diandalkan; python.exe dipakai sebagai cadangan.
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo   [!] Python belum terpasang di komputer ini.
  echo.
  echo   Cara memasang ^(sekali saja^):
  echo     1. Halaman download Python akan terbuka otomatis.
  echo     2. Klik tombol kuning "Download Python".
  echo     3. Jalankan file yang terunduh.
  echo     4. PENTING: centang "Add python.exe to PATH" di layar pertama,
  echo        baru klik "Install Now".
  echo     5. Setelah selesai, klik dua kali lagi file Start.bat ini.
  echo.
  start "" "https://www.python.org/downloads/windows/"
  echo   Tekan tombol apa saja untuk menutup jendela ini...
  pause >nul
  exit /b 1
)

REM --- 2. Siapkan folder data + pindahkan database lama kalau ada --------------
if not exist "%APPDATA_DIR%" mkdir "%APPDATA_DIR%"

REM Instalasi lama menyimpan database di dalam folder aplikasi, tempat yang bisa
REM tertimpa saat update. Pindahkan sekali ke folder data yang aman.
if exist "data\scraper.db" (
  if not exist "%DB_PATH%" (
    echo   - Memindahkan database lama ke folder yang aman...
    copy /y "data\scraper.db" "%DB_PATH%" >nul
    if errorlevel 1 goto :fail_setup
    move /y "data\scraper.db" "data\scraper.db.dipindahkan" >nul
  )
)

REM --- 3. Cek update ----------------------------------------------------------
REM Gagal update bukan alasan untuk tidak bisa jualan: kalau internet mati atau
REM ada perubahan lokal, lewati saja dan tetap jalankan versi yang sekarang ada.
git --version >nul 2>&1
if errorlevel 1 (
  echo   - Git tidak ada, lewati cek update.
) else (
  git rev-parse --is-inside-work-tree >nul 2>&1
  if errorlevel 1 (
    echo   - Bukan folder git, lewati cek update.
  ) else (
    echo   - Mengecek pembaruan aplikasi...
    git pull --ff-only >nul 2>&1
    if errorlevel 1 (
      echo     ^(tidak bisa update sekarang - lanjut pakai versi saat ini^)
    ) else (
      echo     Aplikasi sudah versi terbaru.
    )
  )
)

REM --- 4. Siapkan lingkungan Python -------------------------------------------
if not exist "%VENV%\Scripts\python.exe" (
  echo   - Menyiapkan aplikasi untuk pertama kali, mohon tunggu...
  %PY% -m venv "%VENV%"
  if errorlevel 1 goto :fail_setup
)

REM Pasang ulang dependensi HANYA kalau requirements.txt berubah. Tanpa ini setiap
REM start harus menunggu pip, padahal biasanya tidak ada yang berubah.
set "REQ_HASH="
for /f "skip=1 tokens=* delims=" %%H in ('certutil -hashfile "requirements.txt" SHA256 2^>nul') do (
  if not defined REQ_HASH set "REQ_HASH=%%H"
)
set "STAMP=%VENV%\.requirements-hash"
set "OLD_HASH="
if exist "%STAMP%" set /p OLD_HASH=<"%STAMP%"

if not "%REQ_HASH%"=="%OLD_HASH%" (
  echo   - Memasang komponen yang dibutuhkan...
  "%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet
  "%VENV%\Scripts\python.exe" -m pip install -r requirements.txt --quiet
  if errorlevel 1 goto :fail_setup
  >"%STAMP%" echo %REQ_HASH%
)

REM --- 5. Jalankan ------------------------------------------------------------
echo   - Menjalankan aplikasi...
echo.
echo   ============================================
echo     Aplikasi berjalan di: %URL%
echo     Browser akan terbuka otomatis.
echo.
echo     JANGAN TUTUP JENDELA INI selama memakai
echo     aplikasi. Tutup jendela ini untuk berhenti.
echo   ============================================
echo.

REM Buka browser beberapa detik setelah server siap, tanpa memblokir server.
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process '%URL%'" >nul 2>&1

"%VENV%\Scripts\python.exe" run.py
if errorlevel 1 goto :fail_run

endlocal
exit /b 0

REM --- Penanganan error -------------------------------------------------------
:fail_setup
echo.
echo   [!] Gagal menyiapkan aplikasi.
echo   Pastikan komputer terhubung internet, lalu coba lagi.
echo   Kalau masih gagal, foto layar ini dan kirim ke admin.
echo.
pause
exit /b 1

:fail_run
echo.
echo   [!] Aplikasi berhenti karena ada masalah.
echo   Foto layar ini ^(termasuk tulisan di atas^) dan kirim ke admin.
echo.
pause
exit /b 1
