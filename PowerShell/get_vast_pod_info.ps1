<#
.SYNOPSIS
    Récupère l'IP, le port SSH et l'ID d'un pod actif sur Vast.ai via l'API REST,
    et vérifie que ffmpeg/ffprobe y sont utilisables (avec l'encodeur AV1 NVENC listé).

.DESCRIPTION
    La clé API est lue dans la variable d'environnement VAST_API_KEY, ou à défaut dans
    le fichier du CLI vastai (~\.config\vastai\vast_api_key). Elle ne doit JAMAIS être
    écrite dans ce script (ni dans un commentaire).

    Définition unique de la variable (utilisateur courant, sans laisser la clé dans l'historique) :
        $k = Read-Host "Clé API Vast.ai" -MaskInput
        [Environment]::SetEnvironmentVariable("VAST_API_KEY", $k, "User")
    puis rouvrir le terminal.

    Sans -InstanceId, le pod actif le plus récent est choisi. S'il y en a plusieurs et que
    la vérification ffmpeg n'est pas ignorée, les pods sont essayés du plus récent au plus
    ancien et le premier qui passe le contrôle (ffmpeg + ffprobe présents, encodeur av1_nvenc
    listé) est retenu. Si aucun ne passe, le plus récent est renvoyé quand même, avec un
    avertissement — la propriété FfmpegOk du résultat permet à l'appelant de le savoir.

    QUI FAIT LE CONTRÔLE FFMPEG, ET QUAND :
      - Lancé directement par toi (à la main, pour inspecter un pod avant de s'en servir) :
        le contrôle ffmpeg/av1_nvenc est fait PAR DÉFAUT (tu veux savoir si le pod est prêt).
      - Lancé par Invoke-RemoteStream.ps1 (Resolve-Pod) : ce script appelle celui-ci avec
        -SkipFfmpegCheck, car il fait ensuite son propre contrôle, plus complet, dans
        Test-PodPreflight (qui teste aussi le contournement LD_PRELOAD multi-GPU, ce que
        ce script-ci ne fait pas). Pas de doublon de connexion SSH, démarrage plus rapide ;
        get_vast_pod_info.ps1 se contente alors de renvoyer les infos du pod.
      Rien à faire pour obtenir ce comportement : c'est la valeur par défaut de -SkipFfmpegCheck
      (absent = $false) côté script, et Invoke-RemoteStream.ps1 passe le switch explicitement.

    Ce contrôle ne teste PAS un encodage réel : sur un pod multi-GPU, av1_nvenc peut être
    listé et pourtant échouer à l'exécution tant que le contournement LD_PRELOAD n'est pas
    installé (voir Invoke-RemoteStream.ps1 / Test-PodPreflight, qui fait ce test complet).
#>

[CmdletBinding()]
param (
    # [string]$VastApiKey = $env:VAST_API_KEY,
	[string]$VastApiKey = "e6703638ee5f4817205f17fa934a1fa2e85e107ada7de344401d89ddbac76e81",
	# Run Pod : rpa_RGWYOV61Y09S04MI01YPI0XUNXG1KOLU24F9GZHQ1afrf5
    [string]$InstanceId = "",

    # Connexion SSH pour le contrôle ffmpeg côté pod
    [string]$SSHUser  = "root",
    [string]$KeyPath  = (Join-Path $env:USERPROFILE ".ssh\id_ed25519"),
    [string]$SSHExe   = "C:\Windows\System32\OpenSSH\ssh.exe",

    # Ignore le contrôle ffmpeg (retour immédiat du pod le plus récent). C'est ce que passe
    # Invoke-RemoteStream.ps1 quand il appelle ce script : il refait son propre contrôle après.
    [switch]$SkipFfmpegCheck
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

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

function Test-PodFfmpeg {
    param ([string]$PodIP, [string]$PodPort)

    $result = [pscustomobject]@{
        SshOk          = $false
        FfmpegOk       = $false
        FfprobeOk      = $false
        Av1NvencListed = $false
        Output         = ""
    }

    if (-not (Test-Path -LiteralPath $SSHExe)) {
        $result.Output = "ssh.exe introuvable ($SSHExe) : contrôle ffmpeg ignoré."
        return $result
    }

    $sshOptions = @(
        "-T", "-p", $PodPort, "-i", $KeyPath,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10"
    )
    $remoteCheck = 'echo SSH_OK; ' +
        '(command -v ffmpeg >/dev/null 2>&1 && echo FFMPEG_OK || echo FFMPEG_MISSING); ' +
        '(command -v ffprobe >/dev/null 2>&1 && echo FFPROBE_OK || echo FFPROBE_MISSING); ' +
        '(ffmpeg -hide_banner -encoders 2>/dev/null | grep -q av1_nvenc && echo AV1_NVENC_OK || echo AV1_NVENC_MISSING)'

    $raw  = & $SSHExe @sshOptions "${SSHUser}@${PodIP}" $remoteCheck 2>&1
    $text = ($raw | Out-String).Trim()

    $result.Output         = $text
    $result.SshOk          = $LASTEXITCODE -eq 0 -and $text -match "SSH_OK"
    $result.FfmpegOk       = $text -match "FFMPEG_OK"
    $result.FfprobeOk      = $text -match "FFPROBE_OK"
    $result.Av1NvencListed = $text -match "AV1_NVENC_OK"
    return $result
}

# La clé passe dans l'en-tête Authorization (et non dans l'URL, où elle finit dans les journaux).
# La barre finale est volontaire : sans elle, une redirection peut faire perdre l'en-tête Authorization (HTTP 403).
$ApiUrl  = "https://console.vast.ai/api/v1/instances/"
$Headers = @{
    Authorization = "Bearer $VastApiKey"
    Accept        = "application/json"
}

Write-Host "[1/4] Interrogation de l'API REST Vast.ai..." -ForegroundColor Yellow

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

Write-Host "[2/4] Analyse de la réponse API..." -ForegroundColor Yellow

if ($Data['success'] -eq $false) {
    Write-Host "[ERREUR] L'API Vast.ai a retourné une erreur : $($Data['msg'])" -ForegroundColor Red
    exit 1
}

Write-Host "      Instances trouvées : $($Data['instances_found'])" -ForegroundColor Cyan

# @() garantit un tableau même s'il n'y a qu'une instance (ou aucune)
$Instances = @($Data['instances'])
$RunningPods = @($Instances | Where-Object {
    $_ -is [hashtable] -and ($_['cur_state'] -eq 'running' -or $_['actual_status'] -eq 'running')
})

if ($RunningPods.Count -eq 0) {
    Write-Host "[ERREUR] Aucun pod en cours d'exécution n'a été trouvé." -ForegroundColor Red
    exit 1
}

Write-Host "[3/4] Sélection du pod..." -ForegroundColor Yellow

if ($InstanceId) {
    $Candidates = @($RunningPods | Where-Object { [string]$_['id'] -eq $InstanceId })
    if ($Candidates.Count -eq 0) {
        Write-Host "[ERREUR] Le pod ${InstanceId} n'existe pas ou n'est pas en cours d'exécution." -ForegroundColor Red
        exit 1
    }
}
else {
    $Candidates = @($RunningPods | Sort-Object { [double]$_['start_date'] } -Descending)
    if ($Candidates.Count -gt 1) {
        $ids = ($Candidates | ForEach-Object { $_['id'] }) -join ", "
        Write-Host "[INFO] Plusieurs pods actifs ($ids) : essai du plus récent au plus ancien. Utilisez -InstanceId pour en forcer un." -ForegroundColor Cyan
    }
}

function Resolve-PodAddress {
    param ($TargetPod)

    # Adresse SSH : IP publique + port direct mappé sur 22/tcp ; à défaut, proxy Vast (hôte ET port du proxy)
    $portMap = $TargetPod['ports']
    if ($TargetPod['public_ipaddr'] -and $portMap -and $portMap['22/tcp']) {
        return [pscustomobject]@{
            PodIP   = ([string]$TargetPod['public_ipaddr']).Trim()
            PodPort = $portMap['22/tcp'][0]['HostPort']
            Mode    = "direct"
        }
    }
    elseif ($TargetPod['ssh_host'] -and $TargetPod['ssh_port']) {
        return [pscustomobject]@{
            PodIP   = $TargetPod['ssh_host']
            PodPort = $TargetPod['ssh_port']
            Mode    = "proxy"
        }
    }
    return $null
}

$TargetPod  = $null
$Address    = $null
$FfmpegInfo = $null

foreach ($candidate in $Candidates) {
    $addr = Resolve-PodAddress -TargetPod $candidate
    if (-not $addr) {
        Write-Host "[AVERTISSEMENT] Pod $($candidate['id']) : IP ou port SSH introuvable, ignoré." -ForegroundColor Yellow
        continue
    }

    if ($SkipFfmpegCheck) {
        $TargetPod = $candidate; $Address = $addr
        break
    }

    Write-Host "      Contrôle ffmpeg sur le pod $($candidate['id']) ($($addr.PodIP):$($addr.PodPort))..." -ForegroundColor Cyan
    $check = Test-PodFfmpeg -PodIP $addr.PodIP -PodPort $addr.PodPort

    if (-not $check.SshOk) {
        Write-Host "[AVERTISSEMENT] Pod $($candidate['id']) : connexion SSH impossible, ignoré." -ForegroundColor Yellow
        continue
    }
    if ($check.FfmpegOk -and $check.FfprobeOk -and $check.Av1NvencListed) {
        Write-Host "[OK] Pod $($candidate['id']) : ffmpeg, ffprobe et av1_nvenc présents." -ForegroundColor Green
        $TargetPod = $candidate; $Address = $addr; $FfmpegInfo = $check
        break
    }

    if (-not $TargetPod) { $TargetPod = $candidate; $Address = $addr; $FfmpegInfo = $check }
    $missing = @()
    if (-not $check.FfmpegOk)       { $missing += "ffmpeg" }
    if (-not $check.FfprobeOk)      { $missing += "ffprobe" }
    if (-not $check.Av1NvencListed) { $missing += "av1_nvenc" }
    Write-Host "[AVERTISSEMENT] Pod $($candidate['id']) : manque $($missing -join ', ') — essai du pod suivant s'il y en a un." -ForegroundColor Yellow
}

if (-not $TargetPod) {
    Write-Host "[ERREUR] Aucun pod actif n'a une adresse SSH exploitable." -ForegroundColor Red
    exit 1
}

$PodId   = $TargetPod['id']
$PodIP   = $Address.PodIP
$PodPort = $Address.PodPort
$Mode    = $Address.Mode

$FfmpegOk = if ($SkipFfmpegCheck) { $null } else { [bool]($FfmpegInfo.FfmpegOk -and $FfmpegInfo.FfprobeOk -and $FfmpegInfo.Av1NvencListed) }

if ($FfmpegOk -eq $false) {
    $indented = $FfmpegInfo.Output -replace "`n", "`n  "
    Write-Host "[AVERTISSEMENT] Le pod retenu (${PodId}) n'a pas passé le contrôle ffmpeg/av1_nvenc. Détail :" -ForegroundColor Yellow
    Write-Host "  $indented" -ForegroundColor DarkGray
}

Write-Host "[4/4] Extraction réussie !" -ForegroundColor Yellow
Write-Host "[OK] Pod détecté (ID: ${PodId}, connexion ${Mode}) -> IP: ${PodIP} | Port: ${PodPort}" -ForegroundColor Green

return [PSCustomObject]@{
    PodId    = [string]$PodId
    PodIP    = [string]$PodIP
    PodPort  = [string]$PodPort
    FfmpegOk = $FfmpegOk
}
