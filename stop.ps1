# 一键停止脚本
Write-Host "正在停止服务..." -ForegroundColor Yellow

# 停止 Qdrant 向量库
$qdrantConn = Get-NetTCPConnection -LocalPort 6333 -State Listen -ErrorAction SilentlyContinue
if ($qdrantConn) {
    Stop-Process -Id $qdrantConn.OwningProcess -Force -ErrorAction SilentlyContinue
    Write-Host "[Qdrant] 已停止" -ForegroundColor Green
} else {
    Write-Host "[Qdrant] 未运行" -ForegroundColor Gray
}

# 停止后端
$backendConn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($backendConn) {
    Stop-Process -Id $backendConn.OwningProcess -Force -ErrorAction SilentlyContinue
    Write-Host "[后端] 已停止" -ForegroundColor Green
} else {
    Write-Host "[后端] 未运行" -ForegroundColor Gray
}

# 停止前端
$frontendConn = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
if ($frontendConn) {
    Stop-Process -Id $frontendConn.OwningProcess -Force -ErrorAction SilentlyContinue
    Write-Host "[前端] 已停止" -ForegroundColor Green
} else {
    Write-Host "[前端] 未运行" -ForegroundColor Gray
}

Write-Host "全部服务已停止" -ForegroundColor Cyan
