<#
.SYNOPSIS
    Récupère l'IP, le port SSH et l'ID d'un pod actif sur Vast.ai via l'API REST.

.DESCRIPTION
    La clé API est lue dans la variable d'environnement VAST_API_KEY, ou à défaut dans
    le fichier du CLI vastai (~\.config\vastai\vast_api_key). Elle ne doit JAMAIS être
    écrite dans ce script (ni dans un commentaire).

    Définition unique de la variable (utilisateur courant, sans laisser la clé dans l'historique) :
        $k = Read-Host "Clé API Vast.ai" -MaskInput
        [Environment]::SetEnvironmentVariable("VAST_API_KEY", $k, "User")
    puis rouvrir le terminal.

    Sans -InstanceId, le pod actif le plus récent est choisi (avec un avertissement
    s'il y en a plusieurs).
#>

[CmdletBinding()]
param (
    [Parameter(Mandatory = $false)]
    # [string]$VastApiKey = $env:VAST_API_KEY,
	[string]$VastApiKey = "e6703638ee5f4817205f17fa934a1fa2e85e107ada7de344401d89ddbac76e81",
	# Run Pod : rpa_RGWYOV61Y09S04MI01YPI0XUNXG1KOLU24F9GZHQ1afrf5

    [Parameter(Mandatory = $false)]
    [string]$InstanceId = ""
)

# Configuration de l'encodage console UTF-8
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
    $TargetPod = $RunningPods | Where-Object { [string]$_['id'] -eq $InstanceId } | Select-Object -First 1
    if (-not $TargetPod) {
        Write-Host "[ERREUR] Le pod ${InstanceId} n'existe pas ou n'est pas en cours d'exécution." -ForegroundColor Red
        exit 1
    }
}
else {
    if ($RunningPods.Count -gt 1) {
        $ids = ($RunningPods | ForEach-Object { $_['id'] }) -join ", "
        Write-Host "[AVERTISSEMENT] Plusieurs pods actifs ($ids) : sélection du plus récent. Utilisez -InstanceId pour forcer un pod." -ForegroundColor Yellow
    }
    $TargetPod = $RunningPods | Sort-Object { [double]$_['start_date'] } -Descending | Select-Object -First 1
}

$PodId = $TargetPod['id']

# Adresse SSH : IP publique + port direct mappé sur 22/tcp ; à défaut, proxy Vast (hôte ET port du proxy)
$PodIP   = $null
$PodPort = $null
$Mode    = ""
$PortMap = $TargetPod['ports']

if ($TargetPod['public_ipaddr'] -and $PortMap -and $PortMap['22/tcp']) {
    $PodIP   = ([string]$TargetPod['public_ipaddr']).Trim()
    $PodPort = $PortMap['22/tcp'][0]['HostPort']
    $Mode    = "direct"
}
elseif ($TargetPod['ssh_host'] -and $TargetPod['ssh_port']) {
    $PodIP   = $TargetPod['ssh_host']
    $PodPort = $TargetPod['ssh_port']
    $Mode    = "proxy"
}

if (-not $PodIP -or -not $PodPort) {
    Write-Host "[ERREUR] IP ou port SSH introuvable pour le pod ID ${PodId}." -ForegroundColor Red
    exit 1
}

Write-Host "[4/4] Extraction réussie !" -ForegroundColor Yellow
Write-Host "[OK] Pod détecté (ID: ${PodId}, connexion ${Mode}) -> IP: ${PodIP} | Port: ${PodPort}" -ForegroundColor Green

return [PSCustomObject]@{
    PodId   = [string]$PodId
    PodIP   = [string]$PodIP
    PodPort = [string]$PodPort
}