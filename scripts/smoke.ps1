#Requires -Version 5.1
<#
.SYNOPSIS
    M1 端到端冒烟测试

.DESCRIPTION
    走一遍完整链路：健康检查 → 创建任务 → 规划 → 启动 → 抢租约 → 执行 → 完成
    → 提交评审 → 交付，并在每一步校验预期状态。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -BaseUrl http://localhost:8000
#>

param(
    [string]$BaseUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Stop"
$script:checks = @()
$script:failed = 0

function Assert-That {
    param([string]$Name, [bool]$Condition, [string]$Detail = "")
    $status = if ($Condition) { "PASS" } else { "FAIL"; $script:failed++ }
    $script:checks += [pscustomobject]@{ Check = $Name; Result = $status; Detail = $Detail }
    $color = if ($Condition) { "Green" } else { "Red" }
    Write-Host ("  [{0}] {1} {2}" -f $status, $Name, $Detail) -ForegroundColor $color
}

function Invoke-Json {
    param([string]$Method, [string]$Path, $Body = $null)
    $params = @{
        Uri         = "$BaseUrl$Path"
        Method      = $Method
        TimeoutSec  = 30
        ContentType = "application/json; charset=utf-8"
    }
    if ($null -ne $Body) {
        $params.Body = ($Body | ConvertTo-Json -Depth 8 -Compress)
    }
    return Invoke-RestMethod @params
}

Write-Host ""
Write-Host "=== M1 端到端冒烟测试 ===" -ForegroundColor Cyan
Write-Host "Target: $BaseUrl"
Write-Host ""

# ---------------------------------------------------------------- 1. health
Write-Host "[1] 健康检查"
$h = $null
try { $h = Invoke-RestMethod -Uri "$BaseUrl/healthz" -TimeoutSec 20 } catch { $h = $null }
Assert-That "healthz 返回 ok" ($null -ne $h -and $h.status -eq "ok") $(if ($h) { $h.status } else { "无响应" })

if ($script:failed -gt 0) {
    Write-Host ""
    Write-Host "服务未就绪，中止。" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 2. create
Write-Host ""
Write-Host "[2] 创建任务"
$task = Invoke-Json -Method Post -Path "/api/v1/tasks" -Body @{ title = "冒烟测试：支付超时故障复盘" }
$taskId = $task.task_id
Assert-That "返回 task_id" ($taskId -match "^[0-9a-f-]{36}$") $taskId

# ---------------------------------------------------------------- 3. plan
Write-Host ""
Write-Host "[3] 规划 DAG"
$plan = Invoke-Json -Method Post -Path "/api/v1/tasks/$taskId/plan" -Body @{
    steps = @(
        @{ step_id = "s1"; role = "telemetry"; capability_tag = "telemetry" }
        @{ step_id = "s2"; role = "log";       capability_tag = "log" }
        @{ step_id = "s3"; role = "change";    capability_tag = "change" }
    )
}
Assert-That "plan_version = 1" ($plan.plan_version -eq 1) "v$($plan.plan_version)"
Assert-That "步骤数 = 3" ($plan.steps -eq 3) "$($plan.steps) 步"

# ---------------------------------------------------------------- 4. start
Write-Host ""
Write-Host "[4] 启动任务"
$started = Invoke-Json -Method Post -Path "/api/v1/tasks/$taskId/start"
Assert-That "状态 = RUNNING" ($started.status -eq "RUNNING") $started.status

# ---------------------------------------------------------------- 5. 非法迁移
Write-Host ""
Write-Host "[5] 非法迁移应被拒绝（有未完成步骤时不能交付）"
$rejected = $false
try {
    Invoke-Json -Method Post -Path "/api/v1/tasks/$taskId/approval" -Body @{
        decision = "approve"; approver = "smoke"
    } | Out-Null
} catch {
    $rejected = $true
}
Assert-That "提前交付被拒绝" $rejected "返回非 2xx"

# ---------------------------------------------------------------- 6. 执行步骤
Write-Host ""
Write-Host "[6] 抢租约并执行 3 个步骤"
foreach ($step in @("s1", "s2", "s3")) {
    $lease = Invoke-Json -Method Post -Path "/api/v1/tasks/$taskId/steps/$step/lease" -Body @{
        owner = "smoke-worker"; ttl_ms = 30000
    }
    Assert-That "$step 获得租约 (fence=$($lease.fence))" ($lease.fence -ge 1) ""

    Invoke-Json -Method Post -Path "/api/v1/tasks/$taskId/steps/$step/begin" -Body @{
        owner = "smoke-worker"; fence = $lease.fence
    } | Out-Null

    $done = Invoke-Json -Method Post -Path "/api/v1/tasks/$taskId/steps/$step/succeed" -Body @{
        owner = "smoke-worker"; fence = $lease.fence
    }
    Assert-That "$step 完成" ($done.status -eq "SUCCEEDED") ""
}

# ---------------------------------------------------------------- 7. 状态校验
Write-Host ""
Write-Host "[7] 校验任务状态"
$view = Invoke-Json -Method Get -Path "/api/v1/tasks/$taskId"
$allDone = $true
foreach ($k in $view.steps.PSObject.Properties.Name) {
    if ($view.steps.$k.status -ne "SUCCEEDED") { $allDone = $false }
}
Assert-That "全部步骤 SUCCEEDED" $allDone ""
Assert-That "任务仍为 RUNNING" ($view.status -eq "RUNNING") $view.status

# ---------------------------------------------------------------- 8. 评审交付
Write-Host ""
Write-Host "[8] 提交评审并交付"
$final = Invoke-Json -Method Post -Path "/api/v1/tasks/$taskId/approval" -Body @{
    decision = "approve"; approver = "sre-lead"; comment = "通过"
}
Assert-That "任务 DELIVERED" ($final.status -eq "DELIVERED") $final.status

# ---------------------------------------------------------------- 9. 事件溯源
Write-Host ""
Write-Host "[9] 校验事件日志"
$events = Invoke-RestMethod -Uri "$BaseUrl/api/v1/tasks/$taskId/events" -TimeoutSec 30
$seqs = @($events | ForEach-Object { $_.seq })
$expected = 1..$seqs.Count
$monotonic = -not (Compare-Object $seqs $expected)
Assert-That "事件 seq 连续无空洞" $monotonic "共 $($seqs.Count) 条事件"

$types = @($events | ForEach-Object { $_.type })
Assert-That "包含 TASK_DELIVERED 事件" ($types -contains "TASK_DELIVERED") ""
Assert-That "包含 3 条 STEP_SUCCEEDED" (($types | Where-Object { $_ -eq "STEP_SUCCEEDED" }).Count -eq 3) ""

# ---------------------------------------------------------------- report
Write-Host ""
Write-Host "=== 结果 ===" -ForegroundColor Cyan
$script:checks | Format-Table -AutoSize

if ($script:failed -eq 0) {
    Write-Host "冒烟测试: 全部通过 ($($script:checks.Count) 项)" -ForegroundColor Green
    Write-Host "task_id = $taskId"
    exit 0
} else {
    Write-Host "冒烟测试: $($script:failed) 项失败" -ForegroundColor Red
    exit 1
}
