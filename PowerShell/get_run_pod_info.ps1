<#
.SYNOPSIS
    Récupère l'IP et le port SSH d'un pod RunPod actif via l'API REST (mode Simple),
    ou décrit le pod et contrôle sa capacité à encoder en AV1 (mode Audit).

.DESCRIPTION
    Deux modes d'appel :

      -Mode Simple (défaut) : aucune connexion SSH. Interroge l'API et renvoie PodId, PodIP, PodPort.
                              C'est le mode à utiliser depuis d'autres scripts (linux_streaming.ps1…).

      -Mode Audit           : en plus, se connecte au pod et affiche un rapport complet :
                              offre RunPod (cloud, datacenter, prix, débits max de l'hôte), CPU, RAM, disques,
                              GPU (VRAM, driver, PCIe…), réseau Internet du pod, débit réel PC <-> pod,
                              et contrôle de la chaîne AV1 NVENC (UVM, cuInit, libcuda compat, test réel,
                              10 bits, benchmark 4K 10 bits NVDEC -> NVENC) avec un verdict et les actions à mener.
                              Nécessite pod_audit_common.ps1 dans le même dossier.

    La clé API est lue dans la variable d'environnement RUNPOD_API_KEY, ou à défaut dans la
    configuration de runpodctl (~\.runpod\config.toml ou ~\.runpod.yaml). Elle ne doit JAMAIS
    être écrite dans ce script.

    Définition unique de la variable (sans laisser la clé dans l'historique) :
        $k = Read-Host "Clé API RunPod" -MaskInput
        [Environment]::SetEnvironmentVariable("RUNPOD_API_KEY", $k, "User")
    puis rouvrir le terminal.

    Accès SSH : seul l'accès direct (IP publique + port 22 exposé en TCP) permet de lancer des
    commandes et d'utiliser SCP. Le proxy ssh.runpod.io ne le permet pas : un pod sans IP publique
    est signalé et ignoré.

.EXAMPLE
    .\get_run_pod_info.ps1
    .\get_run_pod_info.ps1 -Mode Audit
    .\get_run_pod_info.ps1 -Mode Audit -Index 2      # 2e pod de la liste (n°1 = le plus récent)
    .\get_run_pod_info.ps1 -Index -2                 # 2e plus ancien (-1 = le plus ancien = défaut)
    .\get_run_pod_info.ps1 -Mode Audit -FixCompat
    .\get_run_pod_info.ps1 -Mode Audit -All -SkipBenchmark
#>

[CmdletBinding()]
param (
    [ValidateSet("Simple", "Audit")]
    [string]$Mode = "Simple",

    # [string]$RunPodApiKey = $env:RUNPOD_API_KEY,
	[string]$RunPodApiKey = "rpa_RGWYOV61Y09S04MI01YPI0XUNXG1KOLU24F9GZHQ1afrf5",
    [string]$PodId = "",
    # N° du pod dans la liste des pods actifs : 1 = le plus récent, -1 = le plus ancien.
    # 0 = défaut = -1 : le plus ancien, stable même si d'autres pods sont créés ensuite.
    [ValidateRange(-999, 999)]
    [int]$Index = 0,
    # Audit : tous les pods actifs au lieu du plus ancien
    [switch]$All,

    [string]$SSHUser = "root",
    [string]$KeyPath = (Join-Path $env:USERPROFILE ".ssh\id_ed25519"),
    [string]$SSHExe  = "C:\Windows\System32\OpenSSH\ssh.exe",

    # Options du mode Audit
    [switch]$SkipNetworkTest,     # pas de test de débit (Internet et PC <-> pod)
    [switch]$SkipBenchmark,       # pas de benchmark 4K
    [switch]$NoFfmpegInstall,     # n'installe pas ffmpeg s'il manque (ou s'il n'a pas av1_nvenc)
    [switch]$FixCompat,           # neutralise la libcuda "compat" de l'image (incompatible GeForce)
    [int]$LinkTestSizeMB = 64,

    # Compatibilité avec les appelants existants : force le mode Simple
    [switch]$SkipFfmpegCheck
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8
if ($SkipFfmpegCheck) { $Mode = "Simple" }

# ---------------------------------------------------------------- Clé API
if ([string]::IsNullOrWhiteSpace($RunPodApiKey)) {
    foreach ($f in @((Join-Path $env:USERPROFILE ".runpod\config.toml"), (Join-Path $env:USERPROFILE ".runpod.yaml"))) {
        if (Test-Path -LiteralPath $f) {
            $m = [regex]::Match((Get-Content -LiteralPath $f -Raw), '(?im)^\s*api_?key\s*[=:]\s*"?([A-Za-z0-9_\-]+)"?')
            if ($m.Success) { $RunPodApiKey = $m.Groups[1].Value; break }
        }
    }
}
if ([string]::IsNullOrWhiteSpace($RunPodApiKey)) {
    Write-Host "[ERREUR] Clé API RunPod absente : définissez RUNPOD_API_KEY (voir l'aide du script)." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- API
$ApiUrl  = "https://rest.runpod.io/v1/pods?desiredStatus=RUNNING&includeMachine=true"
$Headers = @{ Authorization = "Bearer $RunPodApiKey"; Accept = "application/json" }

Write-Host "[1/3] Interrogation de l'API RunPod..." -ForegroundColor Yellow
try {
    $Pods = @(Invoke-RestMethod -Uri $ApiUrl -Headers $Headers -Method Get -TimeoutSec 20 -ErrorAction Stop)
}
catch {
    $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { $null }
    if ($code -in 401, 403) {
        Write-Host "[ERREUR] Clé API refusée par RunPod (HTTP ${code}). Vérifiez RUNPOD_API_KEY." -ForegroundColor Red
    } else {
        Write-Host "[ERREUR] Échec de la communication avec l'API RunPod : $($_.Exception.Message)" -ForegroundColor Red
    }
    exit 1
}

$Running = @($Pods | Where-Object { $_ -and $_.desiredStatus -eq 'RUNNING' })
Write-Host "      Pods actifs : $($Running.Count)" -ForegroundColor Cyan
if ($Running.Count -eq 0) {
    Write-Host "[ERREUR] Aucun pod en cours d'exécution." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- Sélection
Write-Host "[2/3] Sélection du pod..." -ForegroundColor Yellow

function Resolve-RunPodAddress {
    param ($Pod)
    $port = $null
    if ($Pod.portMappings) { $port = $Pod.portMappings.'22' }
    if ($Pod.publicIp -and $port) {
        return [pscustomobject]@{ PodIP = ([string]$Pod.publicIp).Trim(); PodPort = [string]$port; Mode = "direct" }
    }
    return $null
}

function Get-RunPodReason {
    param ($Pod)
    if (-not $Pod.publicIp -and -not $Pod.portMappings) { return "pod en cours d'initialisation (IP et ports pas encore attribués) : réessayez dans un instant" }
    if (-not $Pod.publicIp) { return "pas d'IP publique (hôte Community sans IP) : redéployez avec le filtre « Public IP » ou en Secure Cloud" }
    return "port 22 non exposé en TCP : ajoutez 22 dans « Expose TCP ports » au déploiement"
}

# Dates RunPod : ISO ("2024-07-12T19:14:40.144Z") ou format Go ("2026-09-24 18:49:15.341 +0000 UTC")
function ConvertTo-RunPodDate {
    param ($Value)
    if (-not $Value) { return [datetime]::MinValue }
    $m = [regex]::Match([string]$Value, '(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?')
    if (-not $m.Success) { return [datetime]::MinValue }
    return [datetime]::SpecifyKind([datetime]::ParseExact("$($m.Groups[1].Value) $($m.Groups[2].Value)", 'yyyy-MM-dd HH:mm:ss',
        [Globalization.CultureInfo]::InvariantCulture), [DateTimeKind]::Utc)
}

# Nom du GPU : champ "gpu" s'il est renseigné, sinon infos de la machine
function Get-RunPodGpuLabel {
    param ($Pod)
    $count = if ($Pod.gpu -and $Pod.gpu.count) { $Pod.gpu.count } elseif ($Pod.gpuCount) { $Pod.gpuCount } else { 1 }
    $name  = @($Pod.gpu.displayName, $Pod.machine.gpuDisplayName, $Pod.machine.gpuType.displayName, $Pod.machine.gpuTypeId, $Pod.gpu.id) |
             Where-Object { $_ } | Select-Object -First 1
    if ($name) { return "$name x$count" }
    return "?"
}

# Liste numérotée par ordre de création : n°1 = le plus récent, n°-1 = le plus ancien.
# Date de création (createdAt) si l'API la fournit, sinon date du dernier démarrage :
# dans ce second cas, redémarrer un pod le fait passer en tête de liste.
$Ordered = @($Running | Sort-Object { ConvertTo-RunPodDate $(if ($_.createdAt) { $_.createdAt } else { $_.lastStartedAt }) } -Descending)

# Pod retenu : -PodId, sinon -Index (0 = défaut = -1 : le plus ancien), sinon tous (-Mode Audit -All).
# Le plus ancien par défaut : un lot en cours reste sur son pod même si un autre pod est créé ensuite.
$Pick        = if ($Index -ne 0) { $Index } elseif ($All -and $Mode -eq 'Audit') { 0 } else { -1 }
$SelectedPos = if ($Pick -gt 0) { $Pick - 1 } elseif ($Pick -lt 0) { $Ordered.Count + $Pick } else { -1 }
if (-not $PodId -and $Pick -ne 0 -and ($SelectedPos -lt 0 -or $SelectedPos -ge $Ordered.Count)) {
    Write-Host "[ERREUR] RunPod_${Pick} demandé, mais seulement $($Ordered.Count) pod(s) actif(s)." -ForegroundColor Red
    exit 1
}

if ($Index -ne 0 -or $Ordered.Count -gt 1) {
    Write-Host "      Pods actifs (n°1 = le plus récent, n°-1 = le plus ancien) :" -ForegroundColor Cyan
    for ($i = 0; $i -lt $Ordered.Count; $i++) {
        $p        = $Ordered[$i]
        $addr     = Resolve-RunPodAddress $p
        $addr     = if ($addr) { "$($addr.PodIP):$($addr.PodPort)" } else { "pas de SSH direct" }
        $selected = if ($PodId) { $p.id -eq $PodId } else { $i -eq $SelectedPos }
        $mark     = if ($selected) { "►" } else { " " }
        $label    = "RunPod_{0} / RunPod_{1}" -f ($i + 1), ($i - $Ordered.Count)
        Write-Host ("      {0} {1,-22} {2,-16} {3,-26} {4}" -f $mark, $label, $p.id, (Get-RunPodGpuLabel $p), $addr) -ForegroundColor Gray
    }
}

if ($PodId) {
    $Candidates = @($Running | Where-Object { $_.id -eq $PodId })
    if ($Candidates.Count -eq 0) {
        Write-Host "[ERREUR] Le pod ${PodId} n'existe pas ou n'est pas en cours d'exécution." -ForegroundColor Red
        exit 1
    }
} elseif ($Pick -ne 0) {
    $Candidates = @($Ordered[$SelectedPos])
} else {
    $Candidates = $Ordered
}

$Usable = [System.Collections.Generic.List[object]]::new()
foreach ($p in $Candidates) {
    $addr = Resolve-RunPodAddress $p
    if ($addr) { $Usable.Add([pscustomobject]@{ Pod = $p; Address = $addr }) }
    else { Write-Host "[AVERTISSEMENT] Pod $($p.id) ($($p.name)) ignoré : $(Get-RunPodReason $p)." -ForegroundColor Yellow }
}
if ($Usable.Count -eq 0) {
    if ($Pick -ne 0 -and -not $PodId) { Write-Host "[ERREUR] RunPod_${Pick} (ID $($Ordered[$SelectedPos].id)) n'est pas joignable en SSH (voir la raison ci-dessus) : choisis un autre n° avec -Index." -ForegroundColor Red; exit 1 }
    Write-Host "[ERREUR] Aucun pod actif n'a d'accès SSH direct exploitable." -ForegroundColor Red
    exit 1
}
if ($Ordered.Count -gt 1 -and -not $PodId -and $Index -eq 0 -and $Pick -ne 0) {
    Write-Host "[INFO] Plusieurs pods actifs : le plus ancien (RunPod_-1) est retenu par défaut. -Index 1 pour le plus récent, -PodId pour un pod précis$(if ($Mode -eq 'Audit') { ', -All pour tous les auditer' })." -ForegroundColor Cyan
}

$Targets = if ($Mode -eq 'Audit' -and $All) { @($Usable) } else { @($Usable[0]) }

# ---------------------------------------------------------------- Mode Simple
if ($Mode -eq 'Simple') {
    $t = $Targets[0]
    Write-Host "[3/3] Extraction réussie !" -ForegroundColor Yellow
    Write-Host "[OK] Pod détecté (ID: $($t.Pod.id), connexion $($t.Address.Mode))-> IP: $($t.Address.PodIP) | Port: $($t.Address.PodPort)" -ForegroundColor Green
    return [pscustomobject]@{
        Provider = "RunPod"
        PodId    = [string]$t.Pod.id
        PodIP    = [string]$t.Address.PodIP
        PodPort  = [string]$t.Address.PodPort
        FfmpegOk = $null
    }
}

# ---------------------------------------------------------------- Mode Audit
$common = Join-Path $PSScriptRoot "pod_audit_common.ps1"
if (-not (Test-Path -LiteralPath $common)) {
    Write-Host "[ERREUR] Mode Audit : fichier introuvable $common" -ForegroundColor Red
    exit 1
}
. $common

if (-not (Test-Path -LiteralPath $SSHExe)) {
    Write-Host "[ERREUR] ssh.exe introuvable ($SSHExe)." -ForegroundColor Red
    exit 1
}

function Get-RunPodApiInfo {
    param ($Pod, $Address)
    $m   = $Pod.machine
    $gpu = Get-RunPodGpuLabel $Pod
    $cpu = if ($m -and $m.cpuType) { "$($m.cpuType.displayName) ($($m.cpuType.cores) cœurs, $($m.cpuType.threadsPerCore) threads/cœur)" }
    $started = $null; $uptime = $null; $spent = $null
    if ($Pod.lastStartedAt) {
        $started = (ConvertTo-RunPodDate $Pod.lastStartedAt).ToLocalTime()
        $h = ((Get-Date) - $started).TotalHours
        $uptime = "{0:0.0} h (depuis {1:dd/MM HH:mm})" -f $h, $started
        if ($Pod.costPerHr) { $spent = "{0:0.00} $" -f ($h * [double]$Pod.costPerHr) }
    }
    $price = if ($Pod.costPerHr) {
        $p = "$($Pod.costPerHr) $/h"
        if ($Pod.adjustedCostPerHr -and $Pod.adjustedCostPerHr -ne $Pod.costPerHr) { $p += " (ajusté : $($Pod.adjustedCostPerHr) $/h)" }
        $p
    }
    $cloud = if ($m) { if ($m.secureCloud) { "Secure Cloud" } else { "Community Cloud" } }
    if ($Pod.interruptible) { $cloud += " (interruptible / spot)" }
    $volume = if ($Pod.volumeInGb) { "$($Pod.volumeInGb) Go sur $($Pod.volumeMountPath)" } else { "aucun (données perdues à l'arrêt)" }

    return [ordered]@{
        "Nom / ID"             = "$($Pod.name) / $($Pod.id)"
        "SSH direct"           = "ssh $SSHUser@$($Address.PodIP) -p $($Address.PodPort)"
        "Cloud"                = $cloud
        "Datacenter"           = $(if ($m) { "$($m.dataCenterId) $(if ($m.location) { "($($m.location))" })" })
        "GPU (offre)"          = $gpu
        "vCPU / RAM (offre)"   = "$($Pod.vcpuCount) vCPU / $($Pod.memoryInGb) Go"
        "CPU de l'hôte"        = $cpu
        "Disque conteneur"     = "$($Pod.containerDiskInGb) Go (effacé à l'arrêt)"
        "Volume"               = $volume
        "Débit max hôte"       = $(if ($m -and ($m.maxDownloadSpeedMbps -or $m.maxUploadSpeedMbps)) { "$($m.maxDownloadSpeedMbps) Mbit/s desc. / $($m.maxUploadSpeedMbps) Mbit/s mont." })
        "Débit disque hôte"    = $(if ($m -and $m.diskThroughputMBps) { "$($m.diskThroughputMBps) Mo/s" })
        "Image"                = $Pod.image
        "Prix"                 = $price
        "En marche depuis"     = $uptime
        "Coût depuis démarrage"= $spent
        "Maintenance prévue"   = $(if ($m -and $m.maintenanceStart) { "$($m.maintenanceStart) → $($m.maintenanceEnd) $($m.maintenanceNote)" })
    }
}

$Results = foreach ($t in $Targets) {
    $pod = $t.Pod; $addr = $t.Address
    Write-Host "[3/3] Audit du pod $($pod.id) ($($addr.PodIP):$($addr.PodPort)) : 1 à 3 minutes…" -ForegroundColor Yellow

    $audit = Invoke-PodAudit -PodIP $addr.PodIP -PodPort $addr.PodPort -SSHUser $SSHUser -KeyPath $KeyPath -SSHExe $SSHExe `
        -NetTest (-not $SkipNetworkTest) -Bench (-not $SkipBenchmark) -Install (-not $NoFfmpegInstall) -FixCompat $FixCompat

    $link = $null
    if ($audit['SSH'] -eq 'OK' -and -not $SkipNetworkTest) {
        Write-Host "      Mesure du débit PC <-> pod ($LinkTestSizeMB Mo)…" -ForegroundColor Cyan
        $link = Measure-PodLink -PodIP $addr.PodIP -PodPort $addr.PodPort -SSHUser $SSHUser -KeyPath $KeyPath -SSHExe $SSHExe -SizeMB $LinkTestSizeMB
    }

    $verdict = Get-PodVerdict $audit
    Write-PodReport -Title "RunPod — $($pod.name) ($($pod.id))" -ApiInfo (Get-RunPodApiInfo $pod $addr) -Audit $audit -Link $link -Verdict $verdict

    $fps = 0; [void][int]::TryParse("$($audit['BENCH_4K_FPS'])", [ref]$fps)
    Set-PodResultDisplay ([pscustomobject]@{
        Provider        = "RunPod"
        PodId           = [string]$pod.id
        PodIP           = [string]$addr.PodIP
        PodPort         = [string]$addr.PodPort
        FfmpegOk        = [bool]($audit['FFMPEG'] -and $audit['FFMPEG'] -ne 'absent' -and $audit['FFPROBE'] -ne 'absent' -and $audit['ENC_av1_nvenc'] -eq 'oui')
        Av1EncodeOk     = $verdict.Av1Ok
        Av1NeedsPreload = $verdict.NeedsPreload
        HostLibCuda     = $audit['LIBCUDA_HOST']
        Bench4kFps      = $(if ($fps -gt 0) { $fps } else { $null })
        Verdict         = $verdict.Level
        Audit           = $audit
    })
}
return $Results