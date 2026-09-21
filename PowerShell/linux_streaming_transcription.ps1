[CmdletBinding()]
param (
    [Parameter(Mandatory = $true, Position = 0)]
    [Alias("InputFile", "FilePath", "Input")]
    [string]$LocalPath,

    [Parameter(Mandatory = $true, Position = 1)]
    [Alias("OutputPath", "Output", "Target")]
    [string]$OutputFile,

    [Parameter(Mandatory = $false)]
    [string]$MediaType = "Video",

    [Parameter(Mandatory = $false)]
    [string]$ActionType = "Transcribe",

    [Parameter(Mandatory = $false)]
    [string]$SSHUser = "root",

    # Qualité NVENC AV1 : plus la valeur est haute, plus le fichier est petit
    [Parameter(Mandatory = $false)]
    [ValidateRange(18, 51)]
    [int]$CQ = 34
)

$GetPodScriptPath = "${env:USERPROFILE}\Desktop\PowerShell\get_vast_pod_info.ps1"
$NvencFixSource   = "${env:USERPROFILE}\Desktop\PowerShell\nvenc_fix.c"
$KeyPath          = "${env:USERPROFILE}\.ssh\id_ed25519"
$SSHExe           = "C:\Windows\System32\OpenSSH\ssh.exe"
$SCPExe           = "C:\Windows\System32\OpenSSH\scp.exe"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

# ============================================================================
# FONCTIONS
# ============================================================================

function Get-MediaDuration {
    param ([string]$Path)

    $raw   = & ffprobe -v error -show_entries format=duration -of "default=noprint_wrappers=1:nokey=1" $Path 2>$null
    $text  = [string]($raw | Select-Object -First 1)
    $value = 0.0
    if ([double]::TryParse($text, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$value)) {
        return $value
    }
    return $null
}

function Invoke-PodSsh {
    param ([string]$Command)

    # Le pipe depuis $null empêche SSH d'attendre une touche sur l'entrée standard
    $output = $null | & $SSHExe @SSHOptions $SSHTarget $Command 2>&1
    $code   = $LASTEXITCODE
    return [pscustomobject]@{
        ExitCode = $code
        Output   = (($output | Out-String).Trim())
    }
}

# Vérifie que le pod est utilisable AVANT d'envoyer le moindre octet de vidéo
function Initialize-Pod {
    Write-Host "[INFO] Vérification de la connexion SSH..." -ForegroundColor Cyan
    $probe = Invoke-PodSsh -Command "echo POD_OK"
    if ($probe.ExitCode -ne 0 -or $probe.Output -notmatch "POD_OK") {
        throw "Connexion SSH impossible :`n$($probe.Output)"
    }

    # Contournement du bug NVENC en conteneur multi-GPU (bibliothèque LD_PRELOAD)
    $lib = Invoke-PodSsh -Command "test -f /opt/libnvenc_fix.so && echo PRESENT || echo ABSENT"
    if ($lib.Output -notmatch "PRESENT") {
        Write-Host "[INFO] Installation du contournement NVENC (compilation sur le pod)..." -ForegroundColor Cyan

        if (-not (Test-Path -LiteralPath $NvencFixSource)) {
            throw "Source du contournement introuvable : ${NvencFixSource}"
        }

        & $SCPExe -P $PodPort -i $KeyPath -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes `
            $NvencFixSource "${SSHUser}@${PodIP}:/tmp/nvenc_fix.c" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Envoi de nvenc_fix.c vers le pod impossible (scp)." }

        $build = Invoke-PodSsh -Command "gcc -shared -fPIC -O2 -o /opt/libnvenc_fix.so /tmp/nvenc_fix.c -ldl"
        if ($build.ExitCode -ne 0) { throw "Compilation du contournement impossible :`n$($build.Output)" }
    }

    # Test réel de l'encodeur : détecte un mauvais hôte en quelques secondes
    Write-Host "[INFO] Test de l'encodeur NVENC sur le pod..." -ForegroundColor Cyan
    $test = Invoke-PodSsh -Command "LD_PRELOAD=/opt/libnvenc_fix.so ffmpeg -nostdin -hide_banner -loglevel error -f lavfi -i testsrc2=size=1280x720:rate=24 -t 1 -c:v av1_nvenc -preset p7 -pix_fmt p010le -f null -"
    if ($test.ExitCode -ne 0) {
        throw "L'encodeur NVENC ne fonctionne pas sur ce pod (changer d'hôte ?) :`n$($test.Output)"
    }
    Write-Host "[OK] NVENC opérationnel." -ForegroundColor Green
}

# ============================================================================
# 1. PRÉREQUIS & VALIDATION
# ============================================================================

if (-not (Test-Path -LiteralPath $LocalPath)) {
    Write-Host "[ERREUR] Fichier source introuvable : ${LocalPath}" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath $GetPodScriptPath)) {
    Write-Host "[ERREUR] Script de détection du pod introuvable : ${GetPodScriptPath}" -ForegroundColor Red
    exit 1
}

$targetExtension = [System.IO.Path]::GetExtension($OutputFile).ToLower()

if ($targetExtension -in @('.webm', '.avi')) {
    Write-Host "[ERREUR] Extension ${targetExtension} non prise en charge pour l'encodage (utiliser .mp4 ou .mkv)." -ForegroundColor Red
    exit 1
}

# Une extension de conteneur vidéo déclenche le réencodage, sinon c'est le pipeline de transcription
$isEncodingTask = $targetExtension -in @('.mp4', '.mkv')

if ($isEncodingTask) {
    $StreamingScript = "${env:USERPROFILE}\Desktop\PowerShell\streaming_video.ps1"
} else {
    $StreamingScript = "${env:USERPROFILE}\Desktop\PowerShell\streaming_audio.ps1"
}

if (-not (Test-Path -LiteralPath $StreamingScript)) {
    Write-Host "[ERREUR] Script de streaming introuvable : ${StreamingScript}" -ForegroundColor Red
    exit 1
}

$Host.UI.RawUI.WindowTitle = "Traitement en cours : ${LocalPath}"

# ============================================================================
# 2. DÉTECTION DU POD
# ============================================================================

Write-Host "[0/3] Récupération des accès SSH du pod..." -ForegroundColor Yellow
$PodInfo = & $GetPodScriptPath

if (-not $PodInfo -or -not $PodInfo.PodIP -or -not $PodInfo.PodPort) {
    Write-Host "[ERREUR] Impossible de récupérer l'IP ou le port du pod actif." -ForegroundColor Red
    exit 1
}

$PodIP   = $PodInfo.PodIP
$PodPort = $PodInfo.PodPort

Write-Host "[OK] Connexion ciblée sur -> IP: ${PodIP} | Port: ${PodPort}" -ForegroundColor Green

# Options SSH communes (toujours AVANT l'hôte : après, elles seraient prises pour la commande distante)
$SSHOptions = @(
    "-T",
    "-p", $PodPort,
    "-i", $KeyPath,
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=yes",       # échoue au lieu de demander un mot de passe
    "-o", "ConnectTimeout=15"
)
$SSHTarget = "${SSHUser}@${PodIP}"

# ============================================================================
# 3. PRÉ-VÉRIFICATION DU POD (encodage uniquement)
# ============================================================================

if ($isEncodingTask) {
    try {
        Initialize-Pod
    }
    catch {
        Write-Host "[ERREUR] Pod non prêt : $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

# ============================================================================
# 4. CONSTRUCTION DE LA COMMANDE DISTANTE
# ============================================================================

if ($isEncodingTask) {
    $tempMkvPath      = [System.IO.Path]::ChangeExtension($OutputFile, ".mkv")
    $targetStreamPath = $tempMkvPath

    # Commande FFmpeg GPU distante (NVENC AV1), sortie Matroska pour le pipe
    $RemoteCmd = "LD_PRELOAD=/opt/libnvenc_fix.so ffmpeg -nostdin -hide_banner -loglevel error -probesize 32M -analyzeduration 0 -y -i pipe:0 -map 0:v:0 -map 0:a? -c:v av1_nvenc -preset p7 -tune hq -cq $CQ -b:v 0 -pix_fmt p010le -c:a copy -f matroska -live 1 -flush_packets 1 pipe:1"
} else {
    $targetStreamPath = $OutputFile

    # Commande Python distante (Transcription / Extraction via Main.py)
    $RemoteCmd = "cd /workspace/git_project_python && .venv/bin/python -OO Main.py --mediaType `"${MediaType}`" --actionType `"${ActionType}`""
}

$SSHArgs = @(
    "-q",
    "-o", "Compression=no",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3"
) + $SSHOptions + @($SSHTarget, $RemoteCmd)

# ============================================================================
# 5. EXÉCUTION DU STREAMING
# ============================================================================

Write-Host "[1/3] Lancement du streaming..." -ForegroundColor Yellow

$streamingParams = @{
    InputFile   = $LocalPath
    OutputFile  = $targetStreamPath
    ExecutorExe = $SSHExe
    AppArgs     = $SSHArgs
    PodIP       = $PodIP
}

try {
    & $StreamingScript @streamingParams
}
catch {
    Write-Host "[ERREUR] Échec du streaming : $($_.Exception.Message)" -ForegroundColor Red
    # Le script de streaming crée le fichier de sortie dès le début : on supprime le résidu partiel
    if (Test-Path -LiteralPath $targetStreamPath) {
        Remove-Item -LiteralPath $targetStreamPath -Force -ErrorAction SilentlyContinue
        Write-Host "[INFO] Fichier de sortie partiel supprimé." -ForegroundColor Yellow
    }
    exit 1
}

# ============================================================================
# 6. POST-TRAITEMENT ET VALIDATION
# ============================================================================

Write-Host "[2/3] Traitement et vérification de la sortie..." -ForegroundColor Yellow

if (-not (Test-Path -LiteralPath $targetStreamPath)) {
    Write-Host "[ERREUR] Le fichier attendu (${targetStreamPath}) n'a pas été généré." -ForegroundColor Red
    exit 1
}

if ($isEncodingTask) {
    $finalPath = $OutputFile

    if ($targetExtension -eq ".mp4") {
        Write-Host "[INFO] Remux local vers MP4 (faststart)..." -ForegroundColor Cyan

        $remuxArgs = "-y -loglevel error -i `"${tempMkvPath}`" -c copy -movflags +faststart `"${OutputFile}`""
        $remuxProc = Start-Process -FilePath "ffmpeg" -ArgumentList $remuxArgs -NoNewWindow -Wait -PassThru

        if ($remuxProc.ExitCode -eq 0 -and (Test-Path -LiteralPath $OutputFile)) {
            Remove-Item -LiteralPath $tempMkvPath -Force
        } else {
            Write-Host "[AVERTISSEMENT] Échec du remux MP4, le fichier MKV est conservé." -ForegroundColor Yellow
            $finalPath = $tempMkvPath
        }
    }

    # --- Contrôles de cohérence : la durée doit correspondre à la source ---
    $srcDur = Get-MediaDuration -Path $LocalPath
    $outDur = Get-MediaDuration -Path $finalPath

    if ($null -ne $srcDur -and $null -ne $outDur) {
        $delta     = [Math]::Abs($srcDur - $outDur)
        $tolerance = [Math]::Max(2.0, $srcDur * 0.001)
        if ($delta -gt $tolerance) {
            Write-Host ("[AVERTISSEMENT] Durée différente de la source : source {0:N1} s, sortie {1:N1} s. Ne supprimez pas l'original avant vérification." -f $srcDur, $outDur) -ForegroundColor Yellow
        } else {
            Write-Host ("[OK] Durée conforme ({0:N1} s)." -f $outDur) -ForegroundColor Green
        }
    } else {
        Write-Host "[AVERTISSEMENT] Durée non vérifiable (ffprobe indisponible ou fichier illisible)." -ForegroundColor Yellow
    }

    # --- Taille et gain ---
    $srcSize = (Get-Item -LiteralPath $LocalPath).Length
    $outSize = (Get-Item -LiteralPath $finalPath).Length
    $srcMB   = [Math]::Round($srcSize / 1MB, 2)
    $outMB   = [Math]::Round($outSize / 1MB, 2)
    $gain    = [Math]::Round((1 - ($outSize / $srcSize)) * 100, 1)

    Write-Host "[3/3] Encodage AV1 réussi : ${srcMB} Mo -> ${outMB} Mo (gain ${gain} %) : ${finalPath}" -ForegroundColor Green

    if ($outSize -ge $srcSize) {
        Write-Host "[AVERTISSEMENT] Le fichier AV1 n'est pas plus petit que la source : conservez l'original (essayez -CQ plus élevé)." -ForegroundColor Yellow
    }
}
elseif ($targetExtension -in @('.csv', '.srt')) {
    try {
        $SrtPath = [System.IO.Path]::ChangeExtension($OutputFile, ".srt")

        if ($targetExtension -eq ".csv") {
            $csvData = Import-Csv -LiteralPath $OutputFile -Delimiter ';'

            if ($csvData -and @($csvData)[0].PSObject.Properties['srt']) {
                $SrtLines = @(foreach ($row in $csvData) {
                    if (-not [string]::IsNullOrEmpty($row.srt)) {
                        $row.srt -replace '\\n', "`r`n"
                    }
                })

                if ($SrtLines.Count -eq 0) {
                    Write-Warning "La colonne 'srt' du CSV est vide : aucun sous-titre généré."
                } else {
                    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
                    [System.IO.File]::WriteAllLines($SrtPath, [string[]]$SrtLines, $utf8NoBom)
                    Write-Host "[3/3] Fichier SRT généré avec succès : ${SrtPath}" -ForegroundColor Green
                }
            } else {
                Write-Warning "Le fichier CSV ne contient pas de colonne 'srt'."
            }
        } else {
            Write-Host "[3/3] Fichier de sous-titres SRT reçu : ${OutputFile}" -ForegroundColor Green
        }
    }
    catch {
        Write-Warning "Erreur lors de la génération/traitement du fichier SRT : $($_.Exception.Message)"
    }
}
else {
    Write-Host "[3/3] Fichier de sortie reçu : ${OutputFile}" -ForegroundColor Green
}

exit 0