@echo off
setlocal
title Hapus Data Dummy
cd /d "%~dp0"

REM ===========================================================================
REM  Menghapus SEMUA data dummy sebelum toko mulai memakai aplikasi sungguhan.
REM  Yang tetap ada: Master Brand, Master Kategori, akun login, Pengaturan
REM  Perusahaan. Master Barang, supplier, customer, dan semua transaksi terhapus.
REM
REM  Memakai database yang sama dengan Start.bat. Database dicadangkan dulu.
REM  Hanya bisa dijalankan SEKALI per database, supaya data asli tetap aman.
REM ===========================================================================

set "DB_PATH=%LOCALAPPDATA%\TokoPlastik\scraper.db"
set "PYEXE=%~dp0.venv\Scripts\python.exe"

echo.
echo   ============================================
echo     HAPUS DATA DUMMY
echo   ============================================
echo.

if not exist "%PYEXE%" (
  echo   [!] Aplikasi belum pernah dijalankan di komputer ini.
  echo       Buka aplikasi sekali lewat ikon di Desktop, tutup, lalu coba lagi.
  echo.
  pause
  exit /b 1
)

"%PYEXE%" scripts\hapus_data_dummy.py
echo.
pause
endlocal
