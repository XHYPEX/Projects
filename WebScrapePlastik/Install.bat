@echo off
setlocal EnableDelayedExpansion
title Pemasangan Aplikasi Toko Plastik

REM ===========================================================================
REM  PEMASANGAN AWAL - jalankan SEKALI saja di komputer baru.
REM
REM  Script ini memasang Python + Git, mengunduh aplikasi, lalu membuat ikon di
REM  Desktop. Setelah ini, cukup pakai ikon "Aplikasi Toko Plastik" di Desktop.
REM
REM  Aman dijalankan berulang kali: kalau sesuatu sudah ada, dilewati.
REM ---------------------------------------------------------------------------
REM  UNTUK DEVELOPER: kalau WebScrapePlastik sudah dipisah jadi repo sendiri,
REM  ubah REPO_URL dan KOSONGKAN APP_SUBDIR (set "APP_SUBDIR=").
REM ===========================================================================

set "REPO_URL=https://github.com/XHYPEX/Projects.git"
set "APP_SUBDIR=WebScrapePlastik"
set "TARGET=%USERPROFILE%\TokoPlastik"
set "SHORTCUT_NAME=Aplikasi Toko Plastik"

echo.
echo   ==================================================
echo     PEMASANGAN APLIKASI TOKO PLASTIK
echo   ==================================================
echo.
echo   Proses ini butuh koneksi internet dan sekitar
echo   5-10 menit. Cukup dijalankan sekali.
echo.
echo   Kalau muncul jendela biru yang menanyakan izin
echo   ^(Do you want to allow this app to make changes^), klik YES.
echo.
pause
echo.

REM --- 1. Pastikan winget tersedia --------------------------------------------
REM winget adalah pemasang bawaan Windows 10 versi baru dan Windows 11.
winget --version >nul 2>&1
if errorlevel 1 (
  echo   [!] Fitur "App Installer" Windows belum ada di komputer ini.
  echo.
  echo   Halaman Microsoft Store akan terbuka. Klik "Get" / "Install",
  echo   tunggu sampai selesai, lalu jalankan lagi file Install.bat ini.
  echo.
  start "" "ms-windows-store://pdp/?productid=9NBLGGH4NNS1"
  echo   Tekan tombol apa saja untuk menutup...
  pause >nul
  exit /b 1
)

REM --- 2. Pasang Python --------------------------------------------------------
call :has_python
if "%HAS_PYTHON%"=="1" (
  echo   [OK] Python sudah terpasang.
) else (
  echo   [..] Memasang Python, mohon tunggu...
  winget install --id Python.Python.3.12 --source winget ^
    --accept-package-agreements --accept-source-agreements --silent
  call :refresh_path
  call :has_python
  if "!HAS_PYTHON!"=="1" (
    echo   [OK] Python terpasang.
  ) else (
    echo   [!] Python terpasang tapi belum terdeteksi.
    echo       TUTUP jendela ini, lalu jalankan Install.bat sekali lagi.
    pause
    exit /b 1
  )
)

REM --- 3. Pasang Git -----------------------------------------------------------
REM Git dipakai untuk update otomatis tiap kali aplikasi dibuka.
call :has_git
if "%HAS_GIT%"=="1" (
  echo   [OK] Git sudah terpasang.
) else (
  echo   [..] Memasang Git, mohon tunggu...
  winget install --id Git.Git --source winget ^
    --accept-package-agreements --accept-source-agreements --silent
  call :refresh_path
  call :has_git
  if "!HAS_GIT!"=="1" (
    echo   [OK] Git terpasang.
  ) else (
    echo   [!] Git terpasang tapi belum terdeteksi.
    echo       TUTUP jendela ini, lalu jalankan Install.bat sekali lagi.
    pause
    exit /b 1
  )
)

REM --- 4. Unduh aplikasi -------------------------------------------------------
if exist "%TARGET%\.git" (
  echo   [..] Aplikasi sudah ada, mengambil versi terbaru...
  git -C "%TARGET%" pull --ff-only
  if errorlevel 1 echo   ^(gagal update - lanjut pakai versi yang ada^)
) else (
  if exist "%TARGET%\" (
    dir /b "%TARGET%" 2>nul | findstr "." >nul
    if not errorlevel 1 (
      echo   [!] Folder "%TARGET%" sudah ada tapi bukan hasil pemasangan ini.
      echo       Ganti nama atau hapus folder tersebut, lalu coba lagi.
      pause
      exit /b 1
    )
  )
  echo   [..] Mengunduh aplikasi...
  git clone "%REPO_URL%" "%TARGET%"
  if errorlevel 1 (
    echo   [!] Gagal mengunduh. Periksa koneksi internet lalu coba lagi.
    pause
    exit /b 1
  )
)

if defined APP_SUBDIR (
  set "APP_DIR=%TARGET%\%APP_SUBDIR%"
) else (
  set "APP_DIR=%TARGET%"
)

if not exist "!APP_DIR!\Start.bat" (
  echo   [!] File Start.bat tidak ditemukan di:
  echo       !APP_DIR!
  echo       Hubungi admin - kemungkinan alamat repo salah.
  pause
  exit /b 1
)
echo   [OK] Aplikasi terunduh.

REM --- 5. Buat ikon di Desktop -------------------------------------------------
REM Lokasi Desktop diambil lewat PowerShell, bukan %USERPROFILE%\Desktop, karena
REM di banyak komputer Desktop dialihkan ke OneDrive.
echo   [..] Membuat ikon di Desktop...
set "SC_APP_DIR=!APP_DIR!"
set "SC_NAME=%SHORTCUT_NAME%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d ($env:SC_NAME + '.lnk'))); $s.TargetPath=(Join-Path $env:SC_APP_DIR 'Start.bat'); $s.WorkingDirectory=$env:SC_APP_DIR; $s.Description='Aplikasi Toko Plastik'; $s.IconLocation=(Join-Path $env:SystemRoot 'System32\SHELL32.dll') + ',13'; $s.Save()"
if errorlevel 1 (
  echo   ^(ikon gagal dibuat - aplikasi tetap bisa dijalankan dari Start.bat^)
) else (
  echo   [OK] Ikon "%SHORTCUT_NAME%" dibuat di Desktop.
)

REM --- 6. Selesai --------------------------------------------------------------
echo.
echo   ==================================================
echo     PEMASANGAN SELESAI
echo   ==================================================
echo.
echo   Mulai sekarang, cukup klik dua kali ikon
echo   "%SHORTCUT_NAME%" di Desktop.
echo.
echo   Saat pertama dibuka, aplikasi butuh 2-3 menit
echo   untuk menyiapkan diri. Setelah itu jauh lebih cepat.
echo.
choice /c YN /n /m "  Buka aplikasi sekarang? (Y/N): "
if errorlevel 2 goto :done
start "" "!APP_DIR!\Start.bat"

:done
echo.
echo   Tekan tombol apa saja untuk menutup jendela ini...
pause >nul
endlocal
exit /b 0


REM ===========================================================================
REM  Subrutin
REM ===========================================================================

:has_python
set "HAS_PYTHON="
py -3 --version >nul 2>&1 && set "HAS_PYTHON=1"
if not defined HAS_PYTHON (
  python --version >nul 2>&1 && set "HAS_PYTHON=1"
)
if not defined HAS_PYTHON set "HAS_PYTHON=0"
exit /b 0

:has_git
set "HAS_GIT="
git --version >nul 2>&1 && set "HAS_GIT=1"
if not defined HAS_GIT set "HAS_GIT=0"
exit /b 0

:refresh_path
REM winget memasang program dan mengubah PATH di registry, tapi jendela cmd yang
REM sedang jalan masih memakai PATH lama. Baca ulang dari registry supaya program
REM yang baru dipasang langsung terdeteksi tanpa perlu menutup jendela.
set "SYSPATH="
set "USERPATH="
for /f "tokens=2,*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul ^| find "REG_"') do set "SYSPATH=%%B"
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul ^| find "REG_"') do set "USERPATH=%%B"
REM "call set" memaksa ekspansi kedua, supaya %SystemRoot% di dalam nilai registry
REM ikut diterjemahkan dan tidak tersimpan sebagai teks mentah.
call set "PATH=%SYSPATH%;%USERPATH%"

REM Cadangan: kalau PATH tetap belum memuatnya, tambahkan lokasi pemasangan umum.
if exist "%ProgramFiles%\Git\cmd\git.exe" set "PATH=%PATH%;%ProgramFiles%\Git\cmd"
if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Git\cmd"
if exist "%SystemRoot%\py.exe" set "PATH=%PATH%;%SystemRoot%"
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%~D\python.exe" set "PATH=!PATH!;%%~D;%%~D\Scripts"
)
exit /b 0
