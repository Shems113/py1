<#
.SYNOPSIS
    Récupère l'IP et le port SSH d'un pod Vast.ai actif via l'API REST (mode Simple),
    ou décrit le pod et contrôle sa capacité à encoder en AV1 (mode Audit).

.DESCRIPTION
    Deux modes d'appel (comportement identique à get_run_pod_info.ps1) :

      -Mode Simple (défaut) : aucune connexion SSH. Interroge l'API et renvoie PodId, PodIP, PodPort.
                              C'est le mode à utiliser depuis d'autres scripts. linux_streaming.ps1,
                              qui passe -SkipFfmpegCheck, obtient ce mode sans modification.

      -Mode Audit           : en plus, se connecte au pod et affiche un rapport complet :
                              offre Vast.ai (GPU, CPU, débits annoncés, fiabilité, prix, coût du trafic),
                              CPU, RAM, disques, GPU (VRAM, driver, PCIe…), réseau Internet du pod,
                              débit réel PC <-> pod, et contrôle de la chaîne AV1 NVENC (UVM, cuInit,
                              libcuda compat, test réel, 10 bits, benchmark 4K 10 bits NVDEC -> NVENC)
                              avec un verdict et les actions à mener.
                              Nécessite pod_audit_common.ps1 dans le même dossier.

    La clé API est lue dans la variable d'environnement VAST_API_KEY, ou à défaut dans le
    fichier du CLI vastai (~\.config\vastai\vast_api_key). Elle ne doit JAMAIS être écrite
    dans ce script (ni dans un commentaire).

    Définition unique de la variable (sans laisser la clé dans l'historique) :
        $k = Read-Host "Clé API Vast.ai" -MaskInput
        [Environment]::SetEnvironmentVariable("VAST_API_KEY", $k, "User")
    puis rouvrir le terminal.

    Adresse SSH : IP publique + port direct mappé sur 22/tcp ; à défaut, proxy Vast (sshN.vast.ai),
    qui accepte lui aussi les commandes et SCP.

.EXAMPLE
    .\get_vast_pod_info.ps1
    .\get_vast_pod_info.ps1 -Mode Audit
    .\get_vast_pod_info.ps1 -Mode Audit -Index 2     # 2e pod de la liste (n°1 = le plus récent)
    .\get_vast_pod_info.ps1 -Index -2                # 2e plus ancien (-1 = le plus ancien = défaut)
    .\get_vast_pod_info.ps1 -Mode Audit -FixCompat
    .\get_vast_pod_info.ps1 -Mode Audit -All -SkipBenchmark
#>

[CmdletBinding()]
param (
    [ValidateSet("Simple", "Audit")]
    [string]$Mode = "Simple",

    # [string]$VastApiKey = $env:VAST_API_KEY,
	[string]$VastApiKey = "e6703638ee5f4817205f17fa934a1fa2e85e107ada7de344401d89ddbac76e81",
    [string]$InstanceId = "",
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

    # Compatibilité avec linux_streaming.ps1 : force le mode Simple
    [switch]$SkipFfmpegCheck
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8
if ($SkipFfmpegCheck) { $Mode = "Simple" }

# ---------------------------------------------------------------- Clé API
# Repli : clé déjà enregistrée par `vastai set api-key` (fichier du CLI, hors de ce script)
if ([string]::IsNullOrWhiteSpace($VastApiKey)) {
    $cliKeyFile = Join-Path $env:USERPROFILE ".config\vastai\vast_api_key"
    if (Test-Path -LiteralPath $cliKeyFile) {
        $VastApiKey = (Get-Content -LiteralPath $cliKeyFile -Raw).Trim()
    }
}
if ([string]::IsNullOrWhiteSpace($VastApiKey)) {
    Write-Host "[ERREUR] Clé API Vast.ai absente : définissez VAST_API_KEY ou lancez 'vastai set api-key' (voir l'aide du script)." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- API
# La clé passe dans l'en-tête Authorization (et non dans l'URL, où elle finit dans les journaux).
# La barre finale est volontaire : sans elle, une redirection peut faire perdre l'en-tête Authorization (HTTP 403).
$ApiUrl  = "https://console.vast.ai/api/v1/instances/"
$Headers = @{ Authorization = "Bearer $VastApiKey"; Accept = "application/json" }

Write-Host "[1/3] Interrogation de l'API Vast.ai..." -ForegroundColor Yellow
try {
    $Response = Invoke-WebRequest -Uri $ApiUrl -Headers $Headers -Method Get -TimeoutSec 20 -PreserveAuthorizationOnRedirect -ErrorAction Stop
    # -AsHashtable : gère la clé vide ("") de label_counts, qui casse un objet classique
    $Data = $Response.Content | ConvertFrom-Json -AsHashtable
}
catch {
    $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { $null }
    if ($code -in 401, 403) {
        Write-Host "[ERREUR] Clé API refusée par Vast.ai (HTTP ${code}). Vérifiez la variable VAST_API_KEY." -ForegroundColor Red
    } else {
        Write-Host "[ERREUR] Échec de la communication avec l'API Vast.ai : $($_.Exception.Message)" -ForegroundColor Red
    }
    exit 1
}

if ($Data['success'] -eq $false) {
    Write-Host "[ERREUR] L'API Vast.ai a retourné une erreur : $($Data['msg'])" -ForegroundColor Red
    exit 1
}

# @() garantit un tableau même s'il n'y a qu'une instance (ou aucune)
$Running = @(@($Data['instances']) | Where-Object {
    $_ -is [hashtable] -and ($_['cur_state'] -eq 'running' -or $_['actual_status'] -eq 'running')
})
Write-Host "      Pods actifs : $($Running.Count)" -ForegroundColor Cyan
if ($Running.Count -eq 0) {
    Write-Host "[ERREUR] Aucun pod en cours d'exécution n'a été trouvé." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- Sélection
Write-Host "[2/3] Sélection du pod..." -ForegroundColor Yellow

function Resolve-VastAddress {
    param ($Pod)
    $portMap = $Pod['ports']
    if ($Pod['public_ipaddr'] -and $portMap -and $portMap['22/tcp']) {
        return [pscustomobject]@{
            PodIP   = ([string]$Pod['public_ipaddr']).Trim()
            PodPort = [string]$portMap['22/tcp'][0]['HostPort']
            Mode    = "direct"
        }
    }
    elseif ($Pod['ssh_host'] -and $Pod['ssh_port']) {
        return [pscustomobject]@{ PodIP = [string]$Pod['ssh_host']; PodPort = [string]$Pod['ssh_port']; Mode = "proxy" }
    }
    return $null
}

# Liste numérotée par ordre de création : n°1 = le plus récent, n°-1 = le plus ancien.
# L'ID Vast.ai croît à chaque création : l'ordre reste stable même si un pod est arrêté puis
# relancé (start_date, lui, change à chaque redémarrage).
$Ordered = @($Running | Sort-Object { [long]$_['id'] } -Descending)

# Pod retenu : -InstanceId, sinon -Index (0 = défaut = -1 : le plus ancien), sinon tous (-Mode Audit -All).
# Le plus ancien par défaut : un lot en cours reste sur son pod même si un autre pod est créé ensuite.
$Pick        = if ($Index -ne 0) { $Index } elseif ($All -and $Mode -eq 'Audit') { 0 } else { -1 }
$SelectedPos = if ($Pick -gt 0) { $Pick - 1 } elseif ($Pick -lt 0) { $Ordered.Count + $Pick } else { -1 }
if (-not $InstanceId -and $Pick -ne 0 -and ($SelectedPos -lt 0 -or $SelectedPos -ge $Ordered.Count)) {
    Write-Host "[ERREUR] Vast_${Pick} demandé, mais seulement $($Ordered.Count) pod(s) actif(s)." -ForegroundColor Red
    exit 1
}

if ($Index -ne 0 -or $Ordered.Count -gt 1) {
    Write-Host "      Pods actifs (n°1 = le plus récent, n°-1 = le plus ancien) :" -ForegroundColor Cyan
    for ($i = 0; $i -lt $Ordered.Count; $i++) {
        $p        = $Ordered[$i]
        $addr     = Resolve-VastAddress $p
        $addr     = if ($addr) { "$($addr.PodIP):$($addr.PodPort) ($($addr.Mode))" } else { "SSH indisponible" }
        $selected = if ($InstanceId) { [string]$p['id'] -eq $InstanceId } else { $i -eq $SelectedPos }
        $mark     = if ($selected) { "►" } else { " " }
        $label    = "Vast_{0} / Vast_{1}" -f ($i + 1), ($i - $Ordered.Count)
        Write-Host ("      {0} {1,-22} {2,-16} {3,-26} {4}" -f $mark, $label, $p['id'], "$($p['gpu_name']) x$($p['num_gpus'])", $addr) -ForegroundColor Gray
    }
}

if ($InstanceId) {
    $Candidates = @($Running | Where-Object { [string]$_['id'] -eq $InstanceId })
    if ($Candidates.Count -eq 0) {
        Write-Host "[ERREUR] Le pod ${InstanceId} n'existe pas ou n'est pas en cours d'exécution." -ForegroundColor Red
        exit 1
    }
} elseif ($Pick -ne 0) {
    $Candidates = @($Ordered[$SelectedPos])
} else {
    $Candidates = $Ordered
}

$Usable = [System.Collections.Generic.List[object]]::new()
foreach ($p in $Candidates) {
    $addr = Resolve-VastAddress $p
    if ($addr) { $Usable.Add([pscustomobject]@{ Pod = $p; Address = $addr }) }
    else { Write-Host "[AVERTISSEMENT] Pod $($p['id']) ignoré : IP ou port SSH introuvable (pod encore en démarrage ?)." -ForegroundColor Yellow }
}
if ($Usable.Count -eq 0) {
    if ($Pick -ne 0 -and -not $InstanceId) { Write-Host "[ERREUR] Vast_${Pick} (ID $($Ordered[$SelectedPos]['id'])) n'est pas joignable en SSH (voir la raison ci-dessus) : choisis un autre n° avec -Index." -ForegroundColor Red; exit 1 }
    Write-Host "[ERREUR] Aucun pod actif n'a une adresse SSH exploitable." -ForegroundColor Red
    exit 1
}
if ($Ordered.Count -gt 1 -and -not $InstanceId -and $Index -eq 0 -and $Pick -ne 0) {
    Write-Host "[INFO] Plusieurs pods actifs : le plus ancien (Vast_-1) est retenu par défaut. -Index 1 pour le plus récent, -InstanceId pour un pod précis$(if ($Mode -eq 'Audit') { ', -All pour tous les auditer' })." -ForegroundColor Cyan
}

$Targets = if ($Mode -eq 'Audit' -and $All) { @($Usable) } else { @($Usable[0]) }

# ---------------------------------------------------------------- Mode Simple
if ($Mode -eq 'Simple') {
    $t = $Targets[0]
    Write-Host "[3/3] Extraction réussie !" -ForegroundColor Yellow
    Write-Host "[OK] Pod détecté (ID: $($t.Pod['id']), connexion $($t.Address.Mode))-> IP: $($t.Address.PodIP) | Port: $($t.Address.PodPort)" -ForegroundColor Green
    return [pscustomobject]@{
        Provider = "Vast.ai"
        PodId    = [string]$t.Pod['id']
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

function Get-VastApiInfo {
    param ($Pod, $Address)
    $v = { param ($k) $x = $Pod[$k]; if ($null -ne $x -and "$x" -ne '') { $x } else { $null } }
    $num = { param ($k, $fmt, $div = 1) $x = & $v $k; if ($null -ne $x) { $fmt -f ([double]$x / $div) } else { $null } }

    $uptime = $null; $spent = $null
    if (& $v 'start_date') {
        $started = [DateTimeOffset]::FromUnixTimeSeconds([long][double]$Pod['start_date']).LocalDateTime
        $h = ((Get-Date) - $started).TotalHours
        $uptime = "{0:0.0} h (depuis {1:dd/MM HH:mm})" -f $h, $started
        if (& $v 'dph_total') { $spent = "{0:0.00} $" -f ($h * [double]$Pod['dph_total']) }
    }
    $cc = & $v 'compute_cap'
    if ($cc -and "$cc" -match '^\d{3}$') { $cc = "{0}.{1}" -f "$cc".Substring(0, 1), "$cc".Substring(1, 1) }
    $traffic = if ((& $v 'inet_up_cost') -or (& $v 'inet_down_cost')) {
        "montant {0:0.000} $/Go, descendant {1:0.000} $/Go" -f [double]$Pod['inet_up_cost'], [double]$Pod['inet_down_cost']
    }
    $ssh = if ($Address.Mode -eq 'direct') { "ssh $SSHUser@$($Address.PodIP) -p $($Address.PodPort)" } else { "ssh $SSHUser@$($Address.PodIP) -p $($Address.PodPort)  (proxy Vast)" }

    return [ordered]@{
        "Nom / ID"              = "$(& $v 'label') / $($Pod['id'])"
        "SSH"                   = $ssh
        "Machine / hôte"        = "$(& $v 'machine_id') / $(& $v 'host_id')"
        "Localisation"          = & $v 'geolocation'
        "Fiabilité hôte"        = & $num 'reliability2' '{0:P1}'
        "GPU (offre)"           = "$(& $v 'gpu_name') x$(& $v 'num_gpus')"
        "VRAM / bande passante" = "$(& $num 'gpu_ram' '{0:0.0} Go' 1024) / $(& $num 'gpu_mem_bw' '{0:0} Go/s')"
        "Compute capability"    = $cc
        "Driver / CUDA max"     = "$(& $v 'driver_version') / $(& $v 'cuda_max_good')"
        "PCIe"                  = "Gen$(& $v 'pci_gen') x$(& $v 'gpu_lanes') ($(& $num 'pcie_bw' '{0:0.0} Go/s'))"
        "CPU"                   = "$(& $v 'cpu_name') ($(& $num 'cpu_cores_effective' '{0:0.#}') cœurs effectifs sur $(& $v 'cpu_cores'))"
        "RAM (offre)"           = & $num 'cpu_ram' '{0:0} Go' 1024
        "Disque"                = "$(& $num 'disk_space' '{0:0} Go') $(& $v 'disk_name') ($(& $num 'disk_bw' '{0:0} Mo/s'))"
        "Débit annoncé"         = "$(& $num 'inet_down' '{0:0}') Mbit/s desc. / $(& $num 'inet_up' '{0:0}') Mbit/s mont."
        "Image"                 = & $v 'image_uuid'
        "Prix"                  = & $num 'dph_total' '{0:0.000} $/h'
        "Coût du trafic"        = $traffic
        "En marche depuis"      = $uptime
        "Coût depuis démarrage" = $spent
    }
}

$Results = foreach ($t in $Targets) {
    $pod = $t.Pod; $addr = $t.Address
    Write-Host "[3/3] Audit du pod $($pod['id']) ($($addr.PodIP):$($addr.PodPort)) : 1 à 3 minutes…" -ForegroundColor Yellow

    $audit = Invoke-PodAudit -PodIP $addr.PodIP -PodPort $addr.PodPort -SSHUser $SSHUser -KeyPath $KeyPath -SSHExe $SSHExe `
        -NetTest (-not $SkipNetworkTest) -Bench (-not $SkipBenchmark) -Install (-not $NoFfmpegInstall) -FixCompat $FixCompat

    $link = $null
    if ($audit['SSH'] -eq 'OK' -and -not $SkipNetworkTest) {
        Write-Host "      Mesure du débit PC <-> pod ($LinkTestSizeMB Mo)…" -ForegroundColor Cyan
        $link = Measure-PodLink -PodIP $addr.PodIP -PodPort $addr.PodPort -SSHUser $SSHUser -KeyPath $KeyPath -SSHExe $SSHExe -SizeMB $LinkTestSizeMB
    }

    $verdict = Get-PodVerdict $audit
    Write-PodReport -Title "Vast.ai — $($pod['label']) ($($pod['id']))" -ApiInfo (Get-VastApiInfo $pod $addr) -Audit $audit -Link $link -Verdict $verdict

    $fps = 0; [void][int]::TryParse("$($audit['BENCH_4K_FPS'])", [ref]$fps)
    Set-PodResultDisplay ([pscustomobject]@{
        Provider        = "Vast.ai"
        PodId           = [string]$pod['id']
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