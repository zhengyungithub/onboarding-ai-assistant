<#
  守护脚本 — 新员工入职 AI 业务问答助手
  ------------------------------------------------------------------
  作用：常驻监控 Qdrant(6333) / 后端(8000) / 前端(5173) 三个服务，
        发现掉线自动拉起；服务由本脚本以「独立进程」方式启动，
        所以关掉本窗口不会带走服务（但也就没人守护了）。

  用法：
    双击  守护服务.bat           # 推荐：按默认参数常驻
    powershell -ExecutionPolicy Bypass -File watchdog.ps1
    powershell -ExecutionPolicy Bypass -File watchdog.ps1 -Once            # 只巡检一次（可当健康检查用）
    powershell -ExecutionPolicy Bypass -File watchdog.ps1 -SkipFrontend    # 不管前端
    powershell -ExecutionPolicy Bypass -File watchdog.ps1 -IntervalSeconds 30

  停止：双击 停止.bat（会先杀掉本守护脚本，再停服务）
        或直接关掉本窗口，然后双击 停止.bat

  日志：logs\watchdog.log（守护动作）、logs\*.out.log / *.err.log（各服务自身输出）
#>

[CmdletBinding()]
param(
    # 巡检间隔（秒）
    [int]$IntervalSeconds = 15,
    # 只巡检一轮就退出（适合做健康检查 / 手动拉起）
    [switch]$Once,
    # 不守护前端
    [switch]$SkipFrontend,
    # 传给子进程的 NO_PROXY 值；默认 '*' = 彻底绕过系统全局代理
    # （沿用《部署指南》〇 第 3 条的既有约定：系统代理会劫持 localhost / DashScope 请求）
    [string]$NoProxy = '*'
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# ------------------------------------------------------------------
# 代理隔离：本机服务（127.0.0.1）与外部 API 调用都不经过系统全局代理
#   本机实测：代理对 Qdrant 的耗时影响可忽略（均值 9.9ms / 7.8ms / 5.5ms，属噪声），
#   真正的风险是「代理客户端（本机为 63859 端口）挂掉时把请求一起拖死」，
#   以及《部署指南》〇 第 3 条记录的 localhost / DashScope 被劫持问题。
#   后端与前端进程都会继承这里的 NO_PROXY。
#   如需保留外部走代理，可传 -NoProxy "127.0.0.1,localhost,::1"
# ------------------------------------------------------------------
$env:NO_PROXY = $NoProxy
$env:no_proxy = $NoProxy

# ------------------------------------------------------------------
# 路径
# ------------------------------------------------------------------
$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Root) { $Root = (Get-Location).Path }

$LogDir  = Join-Path $Root 'logs'
$LogFile = Join-Path $LogDir 'watchdog.log'

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

# 日志超过 10MB 时轮转一次，避免无限增长
if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 10MB)) {
    Move-Item -Path $LogFile -Destination ($LogFile + '.1') -Force -ErrorAction SilentlyContinue
}

# 说明：不写 PID 文件，避免上次运行留下的过期 PID 被误用。
# 结束守护请直接关闭本窗口（Ctrl+C），或用 停止.bat（按窗口标题精确结束）

# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------
function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch { }
    switch ($Level) {
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        'ERROR' { Write-Host $line -ForegroundColor Red }
        'OK'    { Write-Host $line -ForegroundColor Green }
        'DIM'   { Write-Host $line -ForegroundColor DarkGray }
        default { Write-Host $line }
    }
}

# 探活：显式把 Proxy 设为 $null，避免走系统代理导致本机探测变慢或误判
function Invoke-Probe {
    param([string]$Url, [string]$Expect)
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = 'GET'
        $req.Proxy = $null
        $req.Timeout = 3000
        $req.ReadWriteTimeout = 3000
        $req.KeepAlive = $false
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $body = $reader.ReadToEnd()
        $reader.Close()
        $resp.Close()
        if ($code -ne 200) { return @{ Ok = $false; Code = $code } }
        if ($Expect -and ($body -notmatch [regex]::Escape($Expect))) { return @{ Ok = $false; Code = $code } }
        return @{ Ok = $true; Code = $code }
    } catch {
        return @{ Ok = $false; Code = 0 }
    }
}

function Get-NodeExe {
    $candidates = @(
        'D:\node.js\node.exe',
        (Join-Path $env:ProgramFiles 'nodejs\node.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'nodejs\node.exe'),
        'C:\Users\zheng-yun\.workbuddy\binaries\node\versions\22.22.2-3\node.exe'
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# 以「独立进程 + 隐藏窗口 + 输出落盘」方式拉起服务
function Start-Item {
    param([string]$Name)

    switch ($Name) {
        'Qdrant' {
            $work = Join-Path $Root 'qdrant-native'
            $exe  = Join-Path $work 'qdrant.exe'
            if (-not (Test-Path $exe)) {
                Write-Log "未找到 $exe，向量检索将降级为关键词搜索" 'ERROR'
                return $false
            }
            Start-Process -FilePath $exe -WorkingDirectory $work -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $LogDir 'qdrant.out.log') `
                -RedirectStandardError  (Join-Path $LogDir 'qdrant.err.log')
            return $true
        }

        'Backend' {
            $work = Join-Path $Root 'backend'
            $exe  = Join-Path $work 'venv\Scripts\python.exe'
            if (-not (Test-Path $exe)) {
                Write-Log "未找到 $exe，无法拉起后端" 'ERROR'
                return $false
            }
            Start-Process -FilePath $exe `
                -ArgumentList @('-m','uvicorn','app.main:app','--host','0.0.0.0','--port','8000') `
                -WorkingDirectory $work -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $LogDir 'backend.out.log') `
                -RedirectStandardError  (Join-Path $LogDir 'backend.err.log')
            return $true
        }

        'Frontend' {
            $work = Join-Path $Root 'frontend'
            $node = Get-NodeExe
            if (-not $node) {
                Write-Log "未找到 node.exe，无法拉起前端" 'ERROR'
                return $false
            }
            $vite = Join-Path $work 'node_modules\vite\bin\vite.js'
            if (-not (Test-Path $vite)) {
                Write-Log "未找到 $vite，请先在 frontend 执行 npm install" 'ERROR'
                return $false
            }
            Start-Process -FilePath $node `
                -ArgumentList @($vite,'--host','0.0.0.0','--port','5173') `
                -WorkingDirectory $work -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $LogDir 'frontend.out.log') `
                -RedirectStandardError  (Join-Path $LogDir 'frontend.err.log')
            return $true
        }
    }
    return $false
}

# ------------------------------------------------------------------
# 服务清单
#   Grace：拉起后给多久启动时间（这段时间内不重复探测）
# ------------------------------------------------------------------
$services = @(
    [pscustomobject]@{ Name = 'Qdrant';   Url = 'http://127.0.0.1:6333/healthz';   Expect = 'passed'; Grace = 20 },
    [pscustomobject]@{ Name = 'Backend';  Url = 'http://127.0.0.1:8000/api/health'; Expect = 'ok';     Grace = 30 },
    [pscustomobject]@{ Name = 'Frontend'; Url = 'http://127.0.0.1:5173';           Expect = '';       Grace = 20 }
)
if ($SkipFrontend) {
    $services = $services | Where-Object { $_.Name -ne 'Frontend' }
}

# ------------------------------------------------------------------
# 主循环
# ------------------------------------------------------------------
Write-Log ("守护启动  间隔={0}s  NO_PROXY={1}  项目={2}" -f $IntervalSeconds, $NoProxy, $Root)
Write-Log ("监控服务：{0}" -f (($services | ForEach-Object { $_.Name }) -join ' / ')) 'DIM'
Write-Log "关闭本窗口即停止守护（已启动的服务会继续运行）；彻底停止请用 停止.bat" 'DIM'
Write-Host ""

$failCount     = @{}
$restarts      = @{}
$cooldownUntil = @{}
$totalRestarts = 0

foreach ($s in $services) { $failCount[$s.Name] = 0; $restarts[$s.Name] = 0 }

while ($true) {
    $now   = Get-Date
    $parts = @()

    foreach ($s in $services) {
        $n = $s.Name

        # 冷却期内不重复探测（服务正在启动）
        if ($cooldownUntil.ContainsKey($n) -and $cooldownUntil[$n] -gt $now) {
            $parts += ("{0}=启动中" -f $n)
            continue
        }

        $probe = Invoke-Probe -Url $s.Url -Expect $s.Expect

        if ($probe.Ok) {
            if ($failCount[$n] -gt 0) {
                Write-Log ("{0} 已恢复（此前连续失败 {1} 次）" -f $n, $failCount[$n]) 'OK'
            }
            $failCount[$n] = 0
            $parts += ("{0}=正常" -f $n)
            continue
        }

        # 掉线 → 尝试拉起
        $failCount[$n] = $failCount[$n] + 1
        if (($failCount[$n] -eq 1) -or (($failCount[$n] % 5) -eq 0)) {
            Write-Log ("{0} 无响应（连续 {1} 次），尝试拉起" -f $n, $failCount[$n]) 'WARN'
        }

        if (Start-Item -Name $n) {
            $restarts[$n]     = $restarts[$n] + 1
            $totalRestarts    = $totalRestarts + 1
            $cooldownUntil[$n] = $now.AddSeconds($s.Grace)
            $parts += ("{0}=已拉起" -f $n)
        } else {
            # 拉不起来（缺文件等）：指数退避，最长 5 分钟一次
            $wait = [Math]::Min(60 * $failCount[$n], 300)
            $cooldownUntil[$n] = $now.AddSeconds($wait)
            $parts += ("{0}=拉起失败" -f $n)
        }
    }

    Write-Host ("[{0}] {1}   累计拉起 {2} 次" -f (Get-Date -Format 'HH:mm:ss'), ($parts -join '   '), $totalRestarts)

    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSeconds
}

if ($Once) {
    Write-Host ""
    Write-Log "单轮巡检结束" 'DIM'
}
