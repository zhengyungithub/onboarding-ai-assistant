@echo off
title 启动 - 新员工入职AI业务问答助手
cd /d "%~dp0"

echo ========================================
echo   新员工入职 AI 业务问答助手 - 启动
echo ========================================
echo.

REM ---------- 前置检查 ----------
if not exist "%~dp0backend\venv\Scripts\python.exe" (
    echo [错误] 未找到 backend\venv\Scripts\python.exe
    echo        请确认本脚本位于项目根目录下
    pause
    exit /b 1
)

REM ================= 1. Qdrant 向量库 (6333) =================
curl -s -m 2 http://127.0.0.1:6333/healthz 2>nul | findstr /C:"passed" >nul
if %errorlevel% equ 0 (
    echo [Qdrant] 已在运行  http://localhost:6333
    goto backend
)

if not exist "%~dp0qdrant-native\qdrant.exe" (
    echo [Qdrant] 未找到 qdrant-native\qdrant.exe，向量检索将降级为关键词搜索
    goto backend
)

echo [Qdrant] 启动中... 新窗口标题为【Qdrant】，请勿手动关闭
start "Qdrant" /min cmd /k "pushd %~dp0qdrant-native && qdrant.exe"
ping -n 4 127.0.0.1 >nul
curl -s -m 2 http://127.0.0.1:6333/healthz 2>nul | findstr /C:"passed" >nul
if %errorlevel% equ 0 (
    echo [Qdrant] 已启动  http://localhost:6333
) else (
    echo [Qdrant] 启动较慢或失败，请查看【Qdrant】窗口中的报错信息
)

REM ================= 2. 后端 (8000) =================
:backend
curl -s -m 2 http://127.0.0.1:8000/api/health 2>nul | findstr /C:"ok" >nul
if %errorlevel% equ 0 (
    echo [后端] 已在运行  http://localhost:8000
    goto frontend
)

echo [后端] 启动中... 新窗口标题为【后端】，请勿手动关闭
start "后端" cmd /k "pushd %~dp0backend && venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

REM 等待后端就绪（首次导入依赖约 20 秒，最多等 45 秒）
set /a tries=0
:wait_backend
ping -n 3 127.0.0.1 >nul
curl -s -m 2 http://127.0.0.1:8000/api/health 2>nul | findstr /C:"ok" >nul
if %errorlevel% equ 0 goto backend_ok
set /a tries+=1
if %tries% lss 15 goto wait_backend
echo [后端] 启动较慢或失败，请查看【后端】窗口中的报错信息
goto frontend

:backend_ok
echo [后端] 已启动  http://localhost:8000

REM ================= 3. 前端 (5173) =================
:frontend
curl -s -m 2 -o nul http://127.0.0.1:5173 2>nul
if %errorlevel% equ 0 (
    echo [前端] 已在运行  http://localhost:5173
    goto done
)

REM 定位 node.exe（优先本机常见安装位置，最后回退 PATH 中的 node）
set "NODE_EXE="
if exist "D:\node.js\node.exe" set "NODE_EXE=D:\node.js\node.exe"
if not defined NODE_EXE if exist "%ProgramFiles%\nodejs\node.exe" set "NODE_EXE=%ProgramFiles%\nodejs\node.exe"
if not defined NODE_EXE set "NODE_EXE=node"

echo [前端] 启动中... 新窗口标题为【前端】，请勿手动关闭
start "前端" cmd /k "pushd %~dp0frontend && %NODE_EXE% node_modules\vite\bin\vite.js --host 0.0.0.0 --port 5173"

set /a tries=0
:wait_frontend
ping -n 3 127.0.0.1 >nul
curl -s -m 2 -o nul http://127.0.0.1:5173 2>nul
if %errorlevel% equ 0 goto frontend_ok
set /a tries+=1
if %tries% lss 10 goto wait_frontend
echo [前端] 启动较慢或失败，请查看【前端】窗口中的报错信息
goto done

:frontend_ok
echo [前端] 已启动  http://localhost:5173

:done
echo.
echo ========================================
echo   启动完成！
echo ========================================
echo.
echo   前端: http://localhost:5173
echo   后端: http://localhost:8000
echo   API文档: http://localhost:8000/docs
echo.
echo   管理员账号: admin / admin123
echo.
pause
