@echo off
setlocal
title Impor Master Barang
cd /d "%~dp0"

REM ===========================================================================
REM  Memasukkan daftar barang dari file LAPORAN MASTER BARANG (.ttx) ke Master
REM  Barang. Seret file .ttx ke ikon file ini, atau klik dua kali lalu seret file
REM  ke jendela yang muncul.
REM
REM  Memakai database yang sama dengan Start.bat. Aman dijalankan ulang: barang
REM  yang sudah ada dilewati. Database dicadangkan dulu sebelum diubah.
REM ===========================================================================

set "DB_PATH=%LOCALAPPDATA%\TokoPlastik\scraper.db"
set "PYEXE=%~dp0.venv\Scripts\python.exe"

echo.
echo   ============================================
echo     IMPOR MASTER BARANG
echo   ============================================
echo.

if not exist "%PYEXE%" (
  echo   [!] Aplikasi belum pernah dijalankan di komputer ini.
  echo       Buka aplikasi sekali lewat ikon di Desktop, tutup, lalu coba lagi.
  echo.
  pause
  exit /b 1
)

set "SOURCE=%~1"
if not defined SOURCE (
  echo   Seret file barang.ttx ke jendela ini, lalu tekan Enter:
  set /p "SOURCE=  > "
)
REM File yang diseret ke jendela diberi tanda kutip kalau lokasinya mengandung spasi.
if defined SOURCE set "SOURCE=%SOURCE:"=%"
if not defined SOURCE (
  echo   Tidak ada file dipilih.
  pause
  exit /b 1
)

echo.
"%PYEXE%" import_barang.py "%SOURCE%"
echo.
echo   Selesai. Buka Master Barang di aplikasi untuk melihat hasilnya.
echo   ^(Kalau aplikasi sedang terbuka, muat ulang halamannya.^)
echo.
pause
endlocal
