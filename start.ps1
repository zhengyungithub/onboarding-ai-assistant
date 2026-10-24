# 一键启动脚本
# 用法: powershell -ExecutionPolicy Bypass -File start.ps1

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  新员工入职 AI 业务问答助手 - 启动" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 检查 Qdrant 向量库是否已运行（引用质量依赖它：知识条目语义检索 + 流程节点补全）
$qdrantRunning = $false
try {
    $r = Invoke-RestMethod -Uri "http://localhost:6333/healthz" -Method GET -TimeoutSec 2
    $qdrantRunning = $true
} catch {}

if ($qdrantRunning) {
    Write-Host "[Qdrant] 已在运行 (http://localhost:6333)" -ForegroundColor Green
} elseif (Test-Path "qdrant-native\qdrant.exe") {
    Write-Host "[Qdrant] 启动中..." -ForegroundColor Yellow
    Start-Process -FilePath "qdrant-native\qdrant.exe" -WorkingDirectory "qdrant-native" -WindowStyle Minimized
    Start-Sleep -Seconds 3
    Write-Host "[Qdrant] 已启动 (http://localhost:6333)" -ForegroundColor Green
} else {
    Write-Host "[Qdrant] 未找到 qdrant-native\qdrant.exe，向量检索将降级为关键词搜索" -ForegroundColor Red
}

# 检查后端是否已运行
$backendRunning = $false
try {
    $r = Invoke-RestMethod -Uri "http://localhost:8000/api/health" -Method GET -TimeoutSec 2
    if ($r.status -eq "ok") { $backendRunning = $true }
} catch {}

if ($backendRunning) {
    Write-Host "[后端] 已在运行 (http://localhost:8000)" -ForegroundColor Green
} else {
    Write-Host "[后端] 启动中..." -ForegroundColor Yellow
    Start-Process -FilePath "powershell" -ArgumentList "-Command", "cd backend; venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload-dir app"
    Start-Sleep -Seconds 5
    Write-Host "[后端] 已启动 (http://localhost:8000)" -ForegroundColor Green
}

# 检查前端是否已运行
$frontendRunning = $false
try {
    $r = Invoke-WebRequest -Uri "http://localhost:5173" -Method GET -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $frontendRunning = $true }
} catch {}

if ($frontendRunning) {
    Write-Host "[前端] 已在运行 (http://localhost:5173)" -ForegroundColor Green
} else {
    Write-Host "[前端] 启动中..." -ForegroundColor Yellow
    Start-Process -FilePath "powershell" -ArgumentList "-Command", "cd frontend; npx vite --host 0.0.0.0 --port 5173"
    Start-Sleep -Seconds 4
    Write-Host "[前端] 已启动 (http://localhost:5173)" -ForegroundColor Green
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  启动完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  前端: http://localhost:5173" -ForegroundColor White
Write-Host "  后端: http://localhost:8000" -ForegroundColor White
Write-Host "  API 文档: http://localhost:8000/docs" -ForegroundColor White
Write-Host ""
Write-Host "  管理员账号: admin / admin123" -ForegroundColor Yellow
Write-Host ""
Write-Host "  按任意键退出此窗口（服务在后台继续运行）" -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
