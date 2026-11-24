#Requires -Version 5.1
<#
.SYNOPSIS
    Postmortem Copilot / AdGuard 环境自检脚本

.DESCRIPTION
    检查 M0 阶段所需的全部环境条件，输出 PASS / WARN / FAIL 三态报告。
    只读检测，不修改任何系统配置。

.EXAMPLE
    pwsh -File scripts\check-env.ps1
#>

$ErrorActionPreference = "SilentlyContinue"
$script:pass = 0
$script:warn = 0
$script:fail = 0
$results = @()

function Add-Result {
    param([string]$Name, [string]$Status, [string]$Detail, [string]$Fix)
    $script:results += [pscustomobject]@{
        Item   = $Name
        Status = $Status
        Detail = $Detail
        Fix    = $Fix
    }
    switch ($Status) {
        "PASS" { $script:pass++ }
        "WARN" { $script:warn++ }
        "FAIL" { $script:fail++ }
    }
}

function Test-CommandExists($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

Write-Host ""
Write-Host "=== Postmortem Copilot / AdGuard 环境自检 ===" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------- OS
$os = Get-CimInstance Win32_OperatingSystem
if ($os.Caption -match "Windows 1[01]") {
    Add-Result "操作系统" "PASS" "$($os.Caption) build $($os.BuildNumber)" ""
} else {
    Add-Result "操作系统" "WARN" $os.Caption "本项目目标平台为 Windows 11 + WSL2"
}

# ---------------------------------------------------------------- RAM
$cs = Get-CimInstance Win32_ComputerSystem
$ramGB = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
if ($ramGB -ge 30) {
    Add-Result "内存" "PASS" "$ramGB GB" ""
} elseif ($ramGB -ge 15) {
    Add-Result "内存" "WARN" "$ramGB GB（够用但偏紧）" "需创建 .wslconfig 限制 WSL 内存至 6GB"
} else {
    Add-Result "内存" "FAIL" "$ramGB GB" "全栈容器至少需要 15GB"
}

# ---------------------------------------------------------------- Disk
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
$freeGB = [math]::Round($disk.FreeSpace / 1GB, 1)
if ($freeGB -ge 60) {
    Add-Result "磁盘 C:" "PASS" "$freeGB GB 可用" ""
} elseif ($freeGB -ge 30) {
    Add-Result "磁盘 C:" "WARN" "$freeGB GB 可用" "模型权重与镜像预计需要 60GB"
} else {
    Add-Result "磁盘 C:" "FAIL" "$freeGB GB 可用" "清理磁盘或迁移 Docker 数据目录"
}

# ---------------------------------------------------------------- Docker
if (Test-CommandExists "docker") {
    $dv = (docker --version) -replace "Docker version ", "" -replace ",.*", ""
    $server = docker info --format "{{.ServerVersion}}" 2>$null
    if ($LASTEXITCODE -eq 0 -and $server) {
        Add-Result "Docker 引擎" "PASS" "client $dv / server $server" ""
    } else {
        Add-Result "Docker 引擎" "FAIL" "client $dv 已装，但守护进程未运行" "启动 Docker Desktop，等待托盘图标变绿"
    }
} else {
    Add-Result "Docker 引擎" "FAIL" "未安装" "安装 Docker Desktop 并启用 WSL2 后端"
}

if (Test-CommandExists "docker") {
    $cv = (docker compose version 2>$null) -replace ".*version ", "" -replace " .*", ""
    if ($cv) { Add-Result "Compose" "PASS" $cv "" }
    else { Add-Result "Compose" "FAIL" "docker compose 不可用" "升级 Docker Desktop" }
}

# ---------------------------------------------------------------- WSL
$wsl = wsl -l -v 2>$null
if ($LASTEXITCODE -eq 0 -and $wsl) {
    Add-Result "WSL2" "PASS" "已安装发行版" ""
} else {
    Add-Result "WSL2" "WARN" "未检测到发行版" "Docker Desktop 通常会自动安装 docker-desktop 发行版"
}

$wslcfg = Join-Path $env:USERPROFILE ".wslconfig"
if (Test-Path $wslcfg) {
    $mem = (Get-Content $wslcfg | Select-String "memory=") -replace ".*memory=", ""
    Add-Result ".wslconfig" "PASS" "已存在，memory=$mem" ""
} else {
    Add-Result ".wslconfig" "WARN" "不存在，WSL 将可占用约 50% 物理内存" "按环境指南创建并限制为 6GB"
}

# ---------------------------------------------------------------- Python
if (Test-CommandExists "python") {
    $pv = (python --version) -replace "Python ", ""
    $parts = $pv.Split(".")
    $major = 0; $minor = 0
    [void][int]::TryParse(($parts[0] -replace "[^0-9].*", ""), [ref]$major)
    if ($parts.Count -gt 1) { [void][int]::TryParse(($parts[1] -replace "[^0-9].*", ""), [ref]$minor) }
    if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
        Add-Result "Python" "PASS" $pv ""
    } else {
        Add-Result "Python" "FAIL" $pv "需要 3.11+"
    }
} else {
    Add-Result "Python" "FAIL" "未安装" "安装 Python 3.12"
}

# ---------------------------------------------------------------- Go
if (Test-CommandExists "go") {
    $gv = (go version) -replace "go version ", ""
    Add-Result "Go" "PASS" $gv ""
} else {
    Add-Result "Go" "WARN" "未安装（M2 才需要）" "winget install GoLang.Go"
}

# ---------------------------------------------------------------- Java
$pinnedJdk = "C:\Java\jdk-21\bin\java.exe"
if (Test-Path $pinnedJdk) {
    $pjv = (cmd /c "`"$pinnedJdk`" -version 2>&1" | Out-String)
    if ($pjv -match 'version\s+"?(\d+)') {
        Add-Result "JDK" "PASS" "项目专用 JDK $($Matches[1]) @ C:\Java\jdk-21" ""
    } else {
        Add-Result "JDK" "PASS" "项目专用 JDK @ C:\Java\jdk-21" ""
    }
} elseif (Test-CommandExists "java") {
    # java writes its version to stderr; route through cmd so the text is a
    # plain string even when $ErrorActionPreference is SilentlyContinue.
    $jraw = (cmd /c "java -version 2>&1" | Out-String)
    if ($jraw -match 'version\s+"?(\d+)') {
        $jmajor = [int]$Matches[1]
        if ($jmajor -eq 21 -or $jmajor -eq 17) {
            Add-Result "JDK" "PASS" "Java $jmajor" ""
        } else {
            Add-Result "JDK" "WARN" "系统 Java $jmajor（Spring Boot 3 推荐 17/21）" "安装 JDK 21 并设置 JAVA_HOME"
        }
    } else {
        Add-Result "JDK" "WARN" "已安装但无法解析版本" "执行 java -version 人工确认"
    }
} else {
    Add-Result "JDK" "WARN" "未安装（P2 控制面需要）" "安装 JDK 21"
}

# ---------------------------------------------------------------- Node
if (Test-CommandExists "node") {
    $nv = (node --version) -replace "v", ""
    Add-Result "Node.js" "PASS" "v$nv" ""
} else {
    Add-Result "Node.js" "WARN" "未安装（前端需要）" "安装 Node 20+"
}

# ---------------------------------------------------------------- GPU
$smi = Get-Command nvidia-smi
if ($smi) {
    $line = (nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null) -join ""
    if ($line -match "(\d+)\s*MiB") {
        $vram = [int]$Matches[1]
        if ($vram -ge 40000) {
            Add-Result "GPU" "PASS" "$line（可跑 7B DPO）" ""
        } elseif ($vram -ge 20000) {
            Add-Result "GPU" "PASS" "$line（可跑 7B LoRA SFT）" ""
        } else {
            Add-Result "GPU" "WARN" "$line（仅够 3B QLoRA）" "P2 的 7B 训练需租云 GPU"
        }
    } else {
        Add-Result "GPU" "WARN" "nvidia-smi 存在但未解析到显存" ""
    }
} else {
    Add-Result "GPU" "WARN" "无 NVIDIA GPU" "P2 训练与推理需云 GPU"
}

# ---------------------------------------------------------------- Ports
$portPlan = @{ 8000 = "core API"; 3307 = "MySQL(容器)"; 6379 = "Redis"; 9090 = "Prometheus"; 3000 = "Grafana" }
foreach ($p in $portPlan.Keys | Sort-Object) {
    $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        Add-Result "端口 $p ($($portPlan[$p]))" "WARN" "被 PID $((($conn | Select-Object -First 1).OwningProcess)) 占用" "修改 compose 端口映射或停止占用进程"
    } else {
        Add-Result "端口 $p ($($portPlan[$p]))" "PASS" "空闲" ""
    }
}

# 已知冲突：3306 被本机 MySQL57 占用
$c3306 = Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction SilentlyContinue
if ($c3306) {
    Add-Result "端口 3306" "WARN" "被本机 MySQL57 占用（已知）" "容器 MySQL 已规划映射 3307，无需处理"
}

# ---------------------------------------------------------------- Network
foreach ($u in @("https://pypi.org/simple/pip/", "https://registry.npmmirror.com", "https://docker.io")) {
    try {
        $r = Invoke-WebRequest -Uri $u -Method Get -TimeoutSec 10 -UseBasicParsing -MaximumRedirection 2
        Add-Result "网络 $(([uri]$u).Host)" "PASS" "HTTP $($r.StatusCode)" ""
    } catch {
        Add-Result "网络 $(([uri]$u).Host)" "WARN" "不可达" "配置镜像源"
    }
}

# ---------------------------------------------------------------- Report
Write-Host ""
$script:results | Format-Table -AutoSize -Wrap

Write-Host ""
$color = if ($script:fail -gt 0) { "Red" } elseif ($script:warn -gt 0) { "Yellow" } else { "Green" }
Write-Host ("汇总: PASS={0}  WARN={1}  FAIL={2}" -f $script:pass, $script:warn, $script:fail) -ForegroundColor $color

$todo = $script:results | Where-Object { $_.Status -ne "PASS" -and $_.Fix -ne "" }
if ($todo) {
    Write-Host ""
    Write-Host "需要处理:" -ForegroundColor Yellow
    foreach ($t in $todo) {
        Write-Host ("  [{0}] {1} -> {2}" -f $t.Status, $t.Item, $t.Fix)
    }
}

Write-Host ""
if ($script:fail -eq 0) {
    Write-Host "M0 门禁: 通过（可进入 M1 验证）" -ForegroundColor Green
    exit 0
} else {
    Write-Host "M0 门禁: 未通过，请先修复 FAIL 项" -ForegroundColor Red
    exit 1
}
