<#
.SYNOPSIS
    Déploie et exécute un projet Python sur un pod distant (Vast.ai) via SSH.

.DESCRIPTION
    Récupère l'IP/le port du pod via get_vast_pod_info.ps1 (diagnostic complet par défaut :
    contrôle ffmpeg/ffprobe/av1_nvenc — utile même ici pour savoir dans quel état est le pod
    avant d'y déployer quoi que ce soit ; passe -SkipFfmpegCheck pour l'ignorer et gagner
    quelques secondes), clone le dépôt, installe les dépendances avec uv, puis lance le
    script Python principal.

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
#>

param (
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl, # Ex: "https://<TOKEN>@github.com/utilisateur/repo.git"

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

    # Passé tel quel à get_vast_pod_info.ps1 : ignore son diagnostic ffmpeg/av1_nvenc
    # (inutile pour un simple déploiement Python, mais gardé par défaut pour un
    # diagnostic complet du pod avant d'y déployer quoi que ce soit).
    [switch]$SkipFfmpegCheck
)

# Configuration de l'encodage console UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

Write-Host "`n=== DEPLOIEMENT ET EXECUTIONS DISTANTES ===" -ForegroundColor Cyan

# $PSScriptRoot : dossier où se trouve CE script (linux_deploy_python.ps1), quel que soit le
# dossier courant du terminal d'où tu le lances. Un chemin relatif comme "get_vast_pod_info.ps1"
# se résout par rapport au dossier courant du terminal, pas par rapport au script - c'est ce qui
# a provoqué "introuvable" quand le script est lancé depuis C:\Windows\System32.
$GetPodScriptPath = Join-Path $PSScriptRoot "get_vast_pod_info.ps1"

# --- ÉTAPE 0 : APPEL DU SCRIPT DÉDIÉ POUR RÉCUPÉRER L'IP ET LE PORT ---
if (-not (Test-Path -LiteralPath $GetPodScriptPath)) {
    Write-Host "[ERREUR] Le script de détection est introuvable : $GetPodScriptPath" -ForegroundColor Red
    exit 1
}

Write-Host "[0/4] Appel de 'get_vast_pod_info.ps1'..." -ForegroundColor Yellow

# Exécution du script externe (diagnostic complet par défaut, sauf -SkipFfmpegCheck)
$PodArgs = @{}
if ($SkipFfmpegCheck) { $PodArgs["SkipFfmpegCheck"] = $true }
$PodInfo = & $GetPodScriptPath @PodArgs

if (-not $PodInfo -or -not $PodInfo.PodIP -or -not $PodInfo.PodPort) {
    Write-Host "[ERREUR] Impossible de récupérer l'IP ou le port depuis le script externe." -ForegroundColor Red
    exit 1
}

$PodIP   = $PodInfo.PodIP
$PodPort = $PodInfo.PodPort

Write-Host "[OK] IP et Port récupérés avec succès -> IP: $PodIP | Port: $PodPort" -ForegroundColor Green

# FfmpegOk est $true/$false/$null (null = contrôle ignoré) ; purement informatif ici,
# ce script ne dépend pas de ffmpeg pour déployer du Python.
if ($null -ne $PodInfo.FfmpegOk -and -not $PodInfo.FfmpegOk) {
    Write-Host "[INFO] Ce pod n'a pas passé le contrôle ffmpeg/av1_nvenc (sans impact pour ce déploiement Python)." -ForegroundColor DarkYellow
}

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

$CommonSSHArgs = @(
    "-p", $PodPort,
    "-i", $KeyPath,
    "-L", "8080:localhost:8080",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "Compression=no",
    "${SSHUser}@${PodIP}"
)

# Étape 1 : Détection dynamique du dossier de travail (isolée du MOTD/Bannière SSH)
Write-Host "[1/4] Détection du dossier de travail distant..." -ForegroundColor Yellow
$DetectWorkDirCmd = "if [ -d '/workspace' ]; then echo '/workspace'; else echo '/root'; fi"

# On ajoute -q pour masquer la bannière de bienvenue lors de la capture de $BaseDir
$DetectSSHArgs = @("-q") + $CommonSSHArgs
$RawOutput = & $SSHExe @DetectSSHArgs $DetectWorkDirCmd 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERREUR] Échec de la connexion SSH (Code de sortie: $LASTEXITCODE)." -ForegroundColor Red
    Write-Host "Détails de l'erreur SSH :" -ForegroundColor Yellow
    $RawOutput | ForEach-Object { Write-Host $_ }
    exit 1
}

# Filtrage strict : extraction uniquement des lignes valides qui commencent par /
$BaseDir = ($RawOutput | Select-String -Pattern "^/(workspace|root)" | Select-Object -First 1).ToString().Trim()

if (-not $BaseDir) {
    # Fallback de sécurité vers /workspace si le parsing échoue
    $BaseDir = "/workspace"
}

$RemoteRepoPath = "$BaseDir/git_project_python"
Write-Host "[OK] Chemin distant retenu : $RemoteRepoPath" -ForegroundColor Green

# Étape 2 : Nettoyage complet des anciens dossiers corrompus et clonage propre
Write-Host "[2/4] Clonage du dépôt sur le pod..." -ForegroundColor Yellow
$SafeRepoUrl = $RepoUrl.Replace("'", "'\''")

# Nettoyage préventif des dossiers bizarres créés par erreur et clonage
# ATTENTION : rm -rf sur un glob (voir .NOTES ci-dessus) - à limiter à un pod que tu contrôles seul.
$RemoteCmd1 = "rm -rf '$RemoteRepoPath' /workspace/root* /root/root* && git clone '$SafeRepoUrl' '$RemoteRepoPath'"

& $SSHExe @CommonSSHArgs $RemoteCmd1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERREUR] Échec du clonage." -ForegroundColor Red
    exit 1
}

# Étape 3 : Installer uv si besoin
Write-Host "[3/4] Vérification de l'outil 'uv'..." -ForegroundColor Yellow
$RemoteCmd2 = "export PATH=`"`$HOME/.local/bin:`$HOME/.cargo/bin:`$PATH`"; if ! command -v uv &> /dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi"
& $SSHExe @CommonSSHArgs $RemoteCmd2
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERREUR] Échec de l'installation de 'uv'." -ForegroundColor Red
    exit 1
}

# Étape 4 : Créer le venv et installer les dépendances avec uv
Write-Host "[4/4] Installation des paquets via uv..." -ForegroundColor Yellow
$RemoteCmd3 = "export PATH=`"`$HOME/.local/bin:`$HOME/.cargo/bin:`$PATH`"; export UV_LINK_MODE=copy; cd '$RemoteRepoPath' && uv venv .venv --allow-existing && uv pip install --python .venv/bin/python -r '$RequirementsFile'"
& $SSHExe @CommonSSHArgs $RemoteCmd3
if ($LASTEXITCODE -ne 0) { Write-Host "[ERREUR] Échec de l'installation des dépendances." -ForegroundColor Red; exit 1 }

# Étape 5 : Exécuter le script Python principal
Write-Host "[OK] Lancement du programme Python..." -ForegroundColor Green
$RemoteCmd4 = "export PATH=`"`$HOME/.local/bin:`$HOME/.cargo/bin:`$PATH`"; cd '$RemoteRepoPath' && './.venv/bin/python' '$MainScript' --jsonPath '$JsonPath' --mediaType '$MediaType' --actionType '$ActionType'"
& $SSHExe @CommonSSHArgs $RemoteCmd4
if ($LASTEXITCODE -ne 0) { Write-Host "[ERREUR] Échec de l'exécution du script." -ForegroundColor Red; exit 1 }

Write-Host "=== TERMINE ===" -ForegroundColor Green