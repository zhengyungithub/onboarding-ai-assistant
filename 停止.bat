@echo off
title 停止 - 新员工入职AI业务问答助手

echo ========================================
echo   新员工入职 AI 业务问答助手 - 停止
echo ========================================
echo.

REM ================= 停止守护脚本（必须先停，否则服务会被立刻拉起）=================
echo 正在停止守护脚本...
taskkill /FI "WINDOWTITLE eq Watchdog*" /T /F >nul 2>&1
if %errorlevel% equ 0 (
    echo   已停止守护脚本
) else (
    echo   守护脚本未在运行
)

REM ================= 停止 Qdrant 向量库 (6333) =================
echo 正在停止 Qdrant 向量库...
set "KILLED=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":6333 " ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>&1 && (
        echo   已停止 Qdrant 进程 PID: %%a
        set "KILLED=1"
    )
)
if "%KILLED%"=="0" echo   Qdrant 未在运行

REM ================= 停止后端 (8000) =================
echo 正在停止后端服务...
set "KILLED=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":8000 " ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>&1 && (
        echo   已停止后端进程 PID: %%a
        set "KILLED=1"
    )
)
if "%KILLED%"=="0" echo   后端未在运行

REM ================= 停止前端 (5173) =================
echo 正在停止前端服务...
set "KILLED=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":5173 " ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>&1 && (
        echo   已停止前端进程 PID: %%a
        set "KILLED=1"
    )
)
if "%KILLED%"=="0" echo   前端未在运行

REM 等待 2 秒后复查一遍，防止 uvicorn --reload 的父进程重新拉起子进程
ping -n 3 127.0.0.1 >nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":8000 " ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":5173 " ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

REM 关闭残留的【后端】【前端】【Qdrant】命令行窗口
taskkill /FI "WINDOWTITLE eq 后端*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq 前端*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Qdrant*" /F >nul 2>&1

echo.
echo ========================================
echo   已停止所有服务
echo ========================================
echo.
pause
