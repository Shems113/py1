<#
.SYNOPSIS
    Déploie et exécute un projet Python sur un pod distant (Vast.ai ou RunPod) via SSH,
    après un audit complet du pod choisi.

.DESCRIPTION
    Le pod est désigné par -Pod "<Plateforme>[_<N>]" :
        Vast       -> 1er pod actif Vast.ai (valeur par défaut)
        Vast_2     -> 2e pod actif Vast.ai
        RunPod     -> 1er pod actif RunPod
        RunPod_3   -> 3e pod actif RunPod
    La numérotation commence à 1 et suit l'ordre de démarrage : n°1 = pod démarré le plus
    récemment (c'est aussi le pod retenu par défaut par linux_streaming.ps1). La liste
    numérotée est affichée à chaque lancement, le pod choisi y est marqué par ►.
    Casse indifférente ; "VastAi", "Vast.ai" et "Vast-2" sont acceptés.

    Étapes :
      0. Audit complet du pod (get_vast_pod_info.ps1 / get_run_pod_info.ps1 -Mode Audit) :
         matériel, réseau, débit PC <-> pod, chaîne AV1 NVENC, benchmark 4K, verdict.
         -SkipAudit le remplace par une simple récupération de l'IP et du port.
      1. Détection du dossier de travail distant.
      2. Clonage du dépôt.
      3. Installation de uv si besoin.
      4. Création du venv et installation des dépendances.
      5. Lancement du script Python principal.

    Scripts requis dans le même dossier que celui-ci :
        get_vast_pod_info.ps1, get_run_pod_info.ps1, pod_audit_common.ps1

.NOTES
    ATTENTION : l'étape de clonage supprime avec "rm -rf" tout ce qui correspond à
    /workspace/root* et /root/root* sur le pod avant de cloner. C'est un nettoyage de
    dossiers mal nommés issus d'anciens essais — mais c'est un glob "rm -rf" sur un
    répertoire potentiellement partagé : vérifie qu'aucun dossier légitime ne matche
    ce motif sur le pod avant de lancer ce script sur un pod que tu réutilises.

    L'URL du dépôt (-RepoUrl) contient ton token GitHub en clair si tu utilises le format
    https://<TOKEN>@github.com/.... Ce token apparaît alors dans la ligne de commande
    passée à git sur le pod distant, donc visible via `ps aux` par quiconque a accès au
    pod (pods partagés/loués : à éviter). Une clé de déploiement SSH (repo en
    git@github.com:...) ou un credential helper évite ce risque.

.EXAMPLE
    .\linux_deploy_python.ps1 -RepoUrl $url                        # 1er pod Vast.ai
    .\linux_deploy_python.ps1 -RepoUrl $url -Pod RunPod            # 1er pod RunPod
    .\linux_deploy_python.ps1 -RepoUrl $url -Pod Vast_2 -FixCompat
    .\linux_deploy_python.ps1 -RepoUrl $url -Pod RunPod_1 -RequireAv1 -SkipBenchmark
#>

param (
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl, # Ex: "https://<TOKEN>@github.com/utilisateur/repo.git"

    # Plateforme et n° du pod : "Vast", "Vast_2", "RunPod", "RunPod_3"...
    [Parameter(Mandatory = $false)]
    [string]$Pod = "Vast",

    [Parameter(Mandatory = $false)]
    [string]$MainScript = "Main.py",

    [Parameter(Mandatory = $false)]
    [string]$RequirementsFile = "requirements.txt",

    [Parameter(Mandatory = $false)]
    [string]$JsonPath = "Files/LinuxSettings.json",

    [Parameter(Mandatory = $false)]
    [string]$MediaType = "File",

    [Parameter(Mandatory = $false)]
    [string]$ActionType = "Read",

    [Parameter(Mandatory = $false)]
    [string]$SSHUser = "root",

    # --- Audit ---
    [switch]$SkipAudit,        # pas d'audit : récupération simple de l'IP et du port
    [switch]$SkipBenchmark,    # audit sans le benchmark 4K
    [switch]$SkipNetworkTest,  # audit sans les tests de débit
    [switch]$FixCompat,        # neutralise la libcuda "compat" de l'image sur le pod (utile sur GeForce)
    [switch]$RequireAv1,       # arrête le déploiement si l'encodage AV1 NVENC ne fonctionne pas

    # Ancien nom de -SkipAudit, gardé pour compatibilité
    [switch]$SkipFfmpegCheck
)

# Configuration de l'encodage console UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

if ($SkipFfmpegCheck) { $SkipAudit = $true }

Write-Host "`n=== DEPLOIEMENT ET EXECUTIONS DISTANTES ===" -ForegroundColor Cyan

# --- SÉLECTEUR DE POD : "Vast", "Vast_2", "RunPod", "RunPod_3" ---
$m = [regex]::Match($Pod.Trim(), '^(?<plat>vast(?:[\.\s]?ai)?|runpod)(?:[_\-\s]?(?<n>\d+))?$', 'IgnoreCase')
if (-not $m.Success) {
    Write-Host "[ERREUR] -Pod '$Pod' invalide. Formats acceptés : Vast, Vast_2, RunPod, RunPod_3..." -ForegroundColor Red
    exit 1
}
$Platform = if ($m.Groups['plat'].Value -match '^runpod$') { "RunPod" } else { "Vast" }
$PodIndex = if ($m.Groups['n'].Success) { [int]$m.Groups['n'].Value } else { 1 }
if ($PodIndex -lt 1) {
    Write-Host "[ERREUR] -Pod '$Pod' : la numérotation commence à 1 (${Platform}_1 = ${Platform})." -ForegroundColor Red
    exit 1
}
$PlatformLabel = if ($Platform -eq "RunPod") { "RunPod" } else { "Vast.ai" }
Write-Host "[INFO] Pod ciblé : ${Platform}_${PodIndex} ($PlatformLabel, n°$PodIndex de la liste)" -ForegroundColor Cyan

# --- CONFIGURATION SSH ---
$KeyPath = "$env:USERPROFILE\.ssh\id_ed25519"
if (-not (Test-Path -LiteralPath $KeyPath)) {
    $KeyPath = "$env:USERPROFILE\.ssh\id_rsa"
}
if (-not (Test-Path -LiteralPath $KeyPath)) {
    Write-Host "[ERREUR] Aucune clé SSH (id_ed25519 ou id_rsa) trouvée dans $env:USERPROFILE\.ssh\" -ForegroundColor Red
    exit 1
}

$SSHExe = "C:\Windows\System32\OpenSSH\ssh.exe"
if (-not (Test-Path -LiteralPath $SSHExe)) {
    Write-Host "[ERREUR] ssh.exe introuvable ($SSHExe). Installe le client OpenSSH de Windows." -ForegroundColor Red
    exit 1
}

# $PSScriptRoot : dossier de CE script, quel que soit le dossier courant du terminal
# (un chemin relatif se résoudrait depuis C:\Windows\System32 et serait introuvable).
$GetPodScriptName = if ($Platform -eq "RunPod") { "get_run_pod_info.ps1" } else { "get_vast_pod_info.ps1" }
$GetPodScriptPath = Join-Path $PSScriptRoot $GetPodScriptName

# --- ÉTAPE 0 : SÉLECTION ET AUDIT DU POD ---
if (-not (Test-Path -LiteralPath $GetPodScriptPath)) {
    Write-Host "[ERREUR] Le script de détection est introuvable : $GetPodScriptPath" -ForegroundColor Red
    exit 1
}
if (-not $SkipAudit -and -not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "pod_audit_common.ps1"))) {
    Write-Host "[ERREUR] pod_audit_common.ps1 est introuvable dans $PSScriptRoot (requis pour l'audit)." -ForegroundColor Red
    exit 1
}

$PodArgs = @{
    Mode    = $(if ($SkipAudit) { "Simple" } else { "Audit" })
    Index   = $PodIndex
    SSHUser = $SSHUser
    KeyPath = $KeyPath
    SSHExe  = $SSHExe
}
if (-not $SkipAudit) {
    if ($SkipBenchmark)   { $PodArgs["SkipBenchmark"]   = $true }
    if ($SkipNetworkTest) { $PodArgs["SkipNetworkTest"] = $true }
    if ($FixCompat)       { $PodArgs["FixCompat"]       = $true }
}

$StepLabel = if ($SkipAudit) { "récupération de l'IP et du port" } else { "audit complet du pod" }
Write-Host "[0/5] $GetPodScriptName : $StepLabel..." -ForegroundColor Yellow

$PodInfo = @(& $GetPodScriptPath @PodArgs) | Where-Object { $_ -and $_.PSObject.Properties['PodIP'] } | Select-Object -First 1

if (-not $PodInfo -or -not $PodInfo.PodIP -or -not $PodInfo.PodPort) {
    Write-Host "[ERREUR] Impossible de récupérer l'IP ou le port du pod ${Platform}_${PodIndex}." -ForegroundColor Red
    exit 1
}

$PodIP   = $PodInfo.PodIP
$PodPort = $PodInfo.PodPort
Write-Host "[OK] Pod ${Platform}_${PodIndex} (ID: $($PodInfo.PodId)) -> IP: $PodIP | Port: $PodPort" -ForegroundColor Green

# --- Exploitation du verdict d'audit ---
if (-not $SkipAudit) {
    if ($PodInfo.Audit -and $PodInfo.Audit['SSH'] -ne 'OK') {
        Write-Host "[ERREUR] L'audit n'a pas pu se connecter en SSH au pod : déploiement annulé." -ForegroundColor Red
        exit 1
    }

    if ($PodInfo.Av1EncodeOk -and -not $PodInfo.Av1NeedsPreload) {
        $fps = if ($PodInfo.Bench4kFps) { " ($($PodInfo.Bench4kFps) i/s en 4K 10 bits)" } else { "" }
        Write-Host "[OK] Audit : encodage AV1 NVENC opérationnel$fps." -ForegroundColor Green
    }
    elseif ($PodInfo.Av1EncodeOk -and $PodInfo.Av1NeedsPreload) {
        Write-Host "[AVERTISSEMENT] Audit : AV1 NVENC ne marche qu'en forçant la libcuda de l'hôte." -ForegroundColor Yellow
        Write-Host "                Relance avec -FixCompat pour corriger le pod avant que ton programme ne l'utilise." -ForegroundColor Yellow
        if ($RequireAv1) {
            Write-Host "[ERREUR] -RequireAv1 : déploiement annulé (pod non corrigé)." -ForegroundColor Red
            exit 1
        }
    }
    else {
        $msg = "Audit : encodage AV1 NVENC indisponible sur ce pod (verdict $($PodInfo.Verdict), voir le rapport ci-dessus)."
        if ($RequireAv1) {
            Write-Host "[ERREUR] $msg -RequireAv1 : déploiement annulé. Choisis un autre pod." -ForegroundColor Red
            exit 1
        }
        Write-Host "[AVERTISSEMENT] $msg Déploiement poursuivi (ajoute -RequireAv1 pour l'interdire)." -ForegroundColor Yellow
    }
}

$CommonSSHArgs = @(
    "-p", $PodPort,
    "-i", $KeyPath,
    "-L", "8080:localhost:8080",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "Compression=no",
    "${SSHUser}@${PodIP}"
)

# Étape 1 : Détection dynamique du dossier de travail (isolée du MOTD/Bannière SSH)
Write-Host "[1/5] Détection du dossier de travail distant..." -ForegroundColor Yellow
$DetectWorkDirCmd = "if [ -d '/workspace' ]; then echo '/workspace'; else echo '/root'; fi"

# -q masque la bannière de bienvenue lors de la capture de $BaseDir
$DetectSSHArgs = @("-q") + $CommonSSHArgs
$RawOutput = & $SSHExe @DetectSSHArgs $DetectWorkDirCmd 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERREUR] Échec de la connexion SSH (Code de sortie: $LASTEXITCODE)." -ForegroundColor Red
    Write-Host "Détails de l'erreur SSH :" -ForegroundColor Yellow
    $RawOutput | ForEach-Object { Write-Host $_ }
    exit 1
}

# Filtrage strict : uniquement les lignes valides qui commencent par /
$BaseDirMatch = $RawOutput | Select-String -Pattern "^/(workspace|root)" | Select-Object -First 1
$BaseDir = if ($BaseDirMatch) { $BaseDirMatch.ToString().Trim() } else { "/workspace" }

$RemoteRepoPath = "$BaseDir/git_project_python"
Write-Host "[OK] Chemin distant retenu : $RemoteRepoPath" -ForegroundColor Green

# Étape 2 : Nettoyage complet des anciens dossiers corrompus et clonage propre
Write-Host "[2/5] Clonage du dépôt sur le pod..." -ForegroundColor Yellow
$SafeRepoUrl = $RepoUrl.Replace("'", "'\''")

# ATTENTION : rm -rf sur un glob (voir .NOTES ci-dessus) - à limiter à un pod que tu contrôles seul.
$RemoteCmd1 = "rm -rf '$RemoteRepoPath' /workspace/root* /root/root* && git clone -q '$SafeRepoUrl' '$RemoteRepoPath'"

& $SSHExe @CommonSSHArgs $RemoteCmd1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERREUR] Échec du clonage." -ForegroundColor Red
    exit 1
}

# Étape 3 : Installer uv si besoin
Write-Host "[3/5] Vérification de l'outil 'uv'..." -ForegroundColor Yellow
$RemoteCmd2 = "export PATH=`"`$HOME/.local/bin:`$HOME/.cargo/bin:`$PATH`"; if ! command -v uv &> /dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi"
& $SSHExe @CommonSSHArgs $RemoteCmd2
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERREUR] Échec de l'installation de 'uv'." -ForegroundColor Red
    exit 1
}

# Étape 4 : Créer le venv et installer les dépendances avec uv
Write-Host "[4/5] Installation des paquets via uv..." -ForegroundColor Yellow
$RemoteCmd3 = "export PATH=`"`$HOME/.local/bin:`$HOME/.cargo/bin:`$PATH`"; export UV_LINK_MODE=copy; cd '$RemoteRepoPath' && uv venv .venv --allow-existing && uv pip install --python .venv/bin/python -r '$RequirementsFile'"
& $SSHExe @CommonSSHArgs $RemoteCmd3
if ($LASTEXITCODE -ne 0) { Write-Host "[ERREUR] Échec de l'installation des dépendances." -ForegroundColor Red; exit 1 }

# Étape 5 : Exécuter le script Python principal
Write-Host "[5/5] Lancement du programme Python..." -ForegroundColor Green
$RemoteCmd4 = "export PATH=`"`$HOME/.local/bin:`$HOME/.cargo/bin:`$PATH`"; cd '$RemoteRepoPath' && './.venv/bin/python' '$MainScript' --jsonPath '$JsonPath' --mediaType '$MediaType' --actionType '$ActionType'"
& $SSHExe @CommonSSHArgs $RemoteCmd4
if ($LASTEXITCODE -ne 0) { Write-Host "[ERREUR] Échec de l'exécution du script." -ForegroundColor Red; exit 1 }

Write-Host "=== TERMINE (${Platform}_${PodIndex}) ===" -ForegroundColor Green