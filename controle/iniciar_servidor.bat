@echo off
chcp 65001 >nul
title Servidor de Producao - Controle de TI
REM ============================================================
REM  Atalho para iniciar o servidor de PRODUCAO com Waitress.
REM  Mantenha este arquivo na MESMA pasta do manage.py.
REM ============================================================

REM --- AJUSTE AQUI (uma unica vez): caminho da pasta venv -------
REM  Caminho provavel (confirme no servidor). Se o venv estiver
REM  em outro lugar, basta corrigir a linha abaixo.
set "VENV_DIR=C:\Projeto Djngo v2\controle-ti\controle-ti\controle\venv"
REM -------------------------------------------------------------

cd /d "%~dp0"
set "PY=%VENV_DIR%\Scripts\python.exe"

if exist "%PY%" (
  echo Usando Python do venv.
) else (
  echo [AVISO] venv nao encontrado em:
  echo         %VENV_DIR%
  echo         Usando o Python do sistema ^(PATH^) -- ok para TESTE local.
  echo         Em PRODUCAO, ajuste o VENV_DIR no topo deste arquivo.
  echo.
  set "PY=python"
)

echo Coletando arquivos estaticos...
"%PY%" manage.py collectstatic --noinput
if errorlevel 1 (
  echo.
  echo [ERRO] Falha no collectstatic. Verifique as mensagens acima.
  pause
  exit /b 1
)

echo.
echo Iniciando servidor de producao...
"%PY%" servir_producao.py

echo.
echo Servidor encerrado.
pause
