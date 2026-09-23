<#
.SYNOPSIS
    Streaming Windows -> pod Linux (Vast.ai, RunPod à venir) : transcription, encodage AV1, contrôle des frames.

.DESCRIPTION
    Un seul schéma pour tous les modes :
      1) PRÉ-TRAITEMENT local          -> New-StreamPlan         (switch sur le mode)
      2) FLUX de streaming             -> Invoke-StreamEngine    (commun à tous les modes)
      3) JOURNAUX d'audit              -> Write-Log              (console + fichier optionnel)
      4) POST-TRAITEMENT de la sortie  -> Complete-StreamOutput  (switch sur le mode)
    Une vérification préalable du pod (Test-PodPreflight) évite d'envoyer des Go vers un pod inutilisable.

    Modes :
      Transcribe   : audio PCM 16 kHz -> Main.py (Transcribe) -> CSV, puis SRT
      EncodeAV1    : vidéo (Matroska) -> ffmpeg NVENC AV1     -> MKV, puis MP4 (faststart)
      CheckFrames  : vidéo (Matroska) -> Main.py (ReadAndCheck) -> rapport CSV
    Sans -Mode : .mp4/.mkv en sortie = EncodeAV1, sinon Transcribe (comportement de l'ancien script).

    AJOUTER UN MODE :
      - l'ajouter au ValidateSet de -Mode ;
      - ajouter un case dans New-StreamPlan, Test-PodPreflight et Complete-StreamOutput ;
      - écrire New-<Mode>Plan et Complete-<Mode> en s'inspirant de ceux des modes existants.
    AJOUTER UN FOURNISSEUR (RunPod...) : ajouter un case dans Resolve-Pod.

.EXAMPLE
    .\Invoke-RemoteStream.ps1 "E:\Series\ep1.mp4" "D:\Out\ep1_AV1.mp4"
.EXAMPLE
    .\Invoke-RemoteStream.ps1 "E:\Series\ep1.mp4" "D:\Out\ep1.csv" -Mode Transcribe
.EXAMPLE
    .\Invoke-RemoteStream.ps1 "E:\Series\ep1.mp4" "D:\Out\ep1_frames.csv" -Mode CheckFrames -LogFile "D:\Out\audit.log"
.EXAMPLE
    # Lot : détection du pod faite une fois, pas de pré-vérification répétée
    .\Invoke-RemoteStream.ps1 "a.mp4" "a_AV1.mp4" -PodHost 1.2.3.4 -PodPort 50367 -SkipPreflight -Preset p5
#>

[CmdletBinding()]
param (
    [Parameter(Mandatory = $true, Position = 0)]
    [Alias("InputFile", "FilePath", "Input")]
    [string]$LocalPath,

    [Parameter(Mandatory = $true, Position = 1)]
    [Alias("OutputPath", "Output", "Target")]
    [string]$OutputFile,

    [ValidateSet("Auto", "Transcribe", "EncodeAV1", "CheckFrames")]
    [string]$Mode = "Auto",

    # Fournisseur du pod : détermine comment on trouve IP et port SSH
    [ValidateSet("Vast", "RunPod", "Manual")]
    [string]$Provider = "Vast",

    # Contournent la détection (utile en lot : détecter une fois, réutiliser)
    [string]$PodHost = "",
    [string]$PodPort = "",
    [string]$SSHUser = "root",

    # Paramètres du projet Python distant
    [ValidatePattern('^[A-Za-z0-9_\-]+$')]
    [string]$MediaType = "Video",
    [ValidatePattern('^[A-Za-z0-9_\-]*$')]
    [string]$ActionType = "",

    # Encodage NVENC AV1 : CQ plus haut = fichier plus petit ; preset plus bas = plus rapide
    [ValidateRange(18, 51)]
    [int]$CQ = 34,
    [ValidateSet("p1", "p2", "p3", "p4", "p5", "p6", "p7")]
    [string]$Preset = "p7",

    # Ignore la vérification du pod (à réserver aux lots sur un pod déjà validé)
    [switch]$SkipPreflight,

    # Mode CheckFrames : par défaut l'audio n'est pas envoyé (inutile pour un contrôle d'images, ça réduit le volume transféré)
    [switch]$IncludeAudio,

    # Journal d'audit (en plus de la console)
    [string]$LogFile = ""
)

# ============================================================================
# CONFIGURATION
# ============================================================================

$Config = @{
    GetVastPodScript = Join-Path $PSScriptRoot "get_vast_pod_info.ps1"
    NvencFixSource   = Join-Path $PSScriptRoot "nvenc_fix.c"
    NvencFixRemote   = "/opt/libnvenc_fix.so"
    KeyPath          = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
    SSHExe           = "C:\Windows\System32\OpenSSH\ssh.exe"
    SCPExe           = "C:\Windows\System32\OpenSSH\scp.exe"
    RemoteProjectDir = "/workspace/git_project_python"
    RemotePython     = ".venv/bin/python"
    MaxLogLines      = 25     # messages d'erreur affichés (source locale et pod) avant de ne garder que le total
}

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

$script:LogPath    = $LogFile
$script:Pod        = $null
$script:SshOptions = @()
$script:SshTarget  = ""
# Déterminé par Test-PodPreflight (mode EncodeAV1) : la plupart des pods (hôte mono-GPU,
# ou déjà à jour) n'ont pas besoin du contournement LD_PRELOAD - il n'est activé que si
# av1_nvenc échoue réellement sans lui. Reste à $false si -SkipPreflight (on fait confiance
# à l'appelant, pas de LD_PRELOAD vers un .so qui n'existe peut-être pas sur ce pod).
$script:NvencFixNeeded = $false

# ============================================================================
# JOURNAUX
# ============================================================================

function Write-Log {
    param (
        [string]$Message,
        [string]$Tag = "STREAM",
        [ConsoleColor]$Color = [ConsoleColor]::Cyan
    )
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "HH:mm:ss.fff"), $Tag, $Message
    Write-Host $line -ForegroundColor $Color
    if ($script:LogPath) {
        try { Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8 } catch { }
    }
}

# ============================================================================
# OUTILS MÉDIA
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

function New-PythonCommand {
    param (
        [string]$Action,
        [switch]$Optimize
    )
    $flags = if ($Optimize) { "-OO " } else { "" }
    return "cd $($Config.RemoteProjectDir) && $($Config.RemotePython) ${flags}Main.py --mediaType `"$MediaType`" --actionType `"$Action`""
}

# ============================================================================
# POD : DÉTECTION PAR FOURNISSEUR, SSH, COPIE
# ============================================================================

function Resolve-Pod {
    if ($PodHost -and $PodPort) {
        return [pscustomobject]@{ PodId = "manual"; PodIP = $PodHost; PodPort = $PodPort }
    }

    switch ($Provider) {
        "Vast" {
            if (-not (Test-Path -LiteralPath $Config.GetVastPodScript)) {
                throw "Script de détection du pod introuvable : $($Config.GetVastPodScript)"
            }
            # -SkipFfmpegCheck : ce script-ci refait de toute façon son propre contrôle, plus complet
            # (Test-PodPreflight, plus bas), qui teste aussi le contournement LD_PRELOAD multi-GPU.
            # Évite une connexion SSH en double et accélère le démarrage.
            return (& $Config.GetVastPodScript -SkipFfmpegCheck)
        }
        "RunPod" {
            # À compléter : appel à l'API RunPod. En attendant, IP/port fournis par variables d'environnement.
            if (-not $env:RUNPOD_SSH_HOST -or -not $env:RUNPOD_SSH_PORT) {
                throw "Fournisseur RunPod : définissez RUNPOD_SSH_HOST et RUNPOD_SSH_PORT (détection par API non implémentée)."
            }
            return [pscustomobject]@{ PodId = "runpod"; PodIP = $env:RUNPOD_SSH_HOST; PodPort = $env:RUNPOD_SSH_PORT }
        }
        "Manual" {
            throw "Fournisseur Manual : -PodHost et -PodPort sont obligatoires."
        }
    }
}

function Initialize-SshContext {
    # Options communes, toujours AVANT l'hôte : après, elles seraient prises pour la commande distante
    $script:SshOptions = @(
        "-T",
        "-p", [string]$script:Pod.PodPort,
        "-i", $Config.KeyPath,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",        # échoue au lieu de demander un mot de passe
        "-o", "ConnectTimeout=15"
    )
    $script:SshTarget = "$SSHUser@$($script:Pod.PodIP)"
}

# Commande courte (pré-vérifications). NE PAS utiliser pour le flux : celui-ci a besoin de stdin.
function Invoke-PodSsh {
    param ([string]$Command)

    # Le pipe depuis $null empêche ssh.exe d'attendre une touche sur l'entrée standard
    $output = $null | & $Config.SSHExe @($script:SshOptions) $script:SshTarget $Command 2>&1
    $code   = $LASTEXITCODE
    return [pscustomobject]@{
        ExitCode = $code
        Output   = (($output | Out-String).Trim())
    }
}

# ============================================================================
# 2) + 3) MOTEUR DE STREAMING (commun à tous les modes)
# ============================================================================

function Get-StreamBufferSize {
    param ([string]$TargetHost)

    $default = 524288   # 512 Ko
    if ([string]::IsNullOrWhiteSpace($TargetHost)) { return $default }

    try {
        $replies = @(Test-Connection -TargetName $TargetHost -Count 3 -ErrorAction SilentlyContinue |
                     Where-Object { $_.Status -eq 'Success' })
        if ($replies.Count -eq 0) {
            Write-Log "ICMP sans réponse : buffer par défaut à 512 Ko." -Color Gray
            return $default
        }

        $avgRtt = ($replies | Measure-Object -Property Latency -Average).Average   # PowerShell 7 : Latency
        if ($avgRtt -gt 100) {
            Write-Log "Latence élevée ($([Math]::Round($avgRtt)) ms) : buffer à 2 Mo." -Color Yellow
            return 2097152
        }
        elseif ($avgRtt -gt 40) {
            Write-Log "Latence moyenne ($([Math]::Round($avgRtt)) ms) : buffer à 1 Mo." -Color Gray
            return 1048576
        }
        Write-Log "Latence faible ($([Math]::Round($avgRtt)) ms) : buffer à 512 Ko." -Color Gray
        return 524288
    }
    catch {
        Write-Log "Mesure de latence impossible : buffer par défaut à 512 Ko." -Color Gray
        return $default
    }
}

function Invoke-StreamEngine {
    param ([Parameter(Mandatory = $true)] $Plan)

    $sw       = [Diagnostics.Stopwatch]::StartNew()
    $maxLines = $Config.MaxLogLines
    $utf8     = [System.Text.Encoding]::UTF8

    # --- Récepteur : ssh.exe qui exécute la commande distante du plan ---
    $sshArgs = @("-q", "-o", "Compression=no", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3") +
               $script:SshOptions + @($script:SshTarget, $Plan.RemoteCommand)

    Write-Log "Source locale  : $($Plan.SourceFile) $($Plan.SourceArgs -join ' ')" -Color Gray
    Write-Log "Commande SSH   : ssh $($sshArgs -join ' ')" -Color Gray

    $executorPsi = [Diagnostics.ProcessStartInfo]@{
        FileName               = $Config.SSHExe
        UseShellExecute        = $false
        RedirectStandardInput  = $true
        RedirectStandardOutput = $true
        RedirectStandardError  = $true
        StandardErrorEncoding  = $utf8
    }
    foreach ($a in $sshArgs) { $executorPsi.ArgumentList.Add([string]$a) }

    $executorProc = [System.Diagnostics.Process]::new()
    $executorProc.StartInfo = $executorPsi

    # État partagé avec le gestionnaire d'événements : tampon complet + compteur, affichage limité
    $remoteLog = [pscustomobject]@{
        Buffer  = [System.Text.StringBuilder]::new()
        Lines   = 0
        Limit   = $maxLines
        LogPath = $script:LogPath
    }

    $stderrEvent = Register-ObjectEvent -InputObject $executorProc -EventName "ErrorDataReceived" -Action {
        $line = $Event.SourceEventArgs.Data
        if ($line) {
            $state = $Event.MessageData
            [void]$state.Buffer.AppendLine($line)
            $state.Lines++
            if ($state.Lines -le $state.Limit) {
                Write-Host "  [REMOTE LOG] $line" -ForegroundColor DarkGray
                if ($state.LogPath) { try { Add-Content -LiteralPath $state.LogPath -Value "  [REMOTE LOG] $line" -Encoding UTF8 } catch { } }
            }
            elseif ($state.Lines -eq ($state.Limit + 1)) {
                Write-Host "  [REMOTE LOG] (messages suivants masqués, total affiché en fin de traitement)" -ForegroundColor DarkGray
            }
        }
    } -MessageData $remoteLog

    $bufferSize = Get-StreamBufferSize -TargetHost $script:Pod.PodIP

    # --- Source locale (stderr lu en continu : jamais laissé se remplir) ---
    $srcPsi = [Diagnostics.ProcessStartInfo]@{
        FileName               = $Plan.SourceFile
        UseShellExecute        = $false
        RedirectStandardOutput = $true
        RedirectStandardError  = $true
        StandardErrorEncoding  = $utf8
    }
    foreach ($a in $Plan.SourceArgs) { $srcPsi.ArgumentList.Add([string]$a) }

    $srcProc    = [Diagnostics.Process]::Start($srcPsi)
    $srcErrTask = $srcProc.StandardError.ReadToEndAsync()
    $srcStream  = $srcProc.StandardOutput.BaseStream

    $null = $executorProc.Start()
    $executorProc.BeginErrorReadLine()

    $outStream         = [System.IO.File]::Create($Plan.RawOutputPath)
    $executorInStream  = $executorProc.StandardInput.BaseStream
    $executorOutStream = $executorProc.StandardOutput.BaseStream

    $expected      = [long]$Plan.ExpectedBytes
    $logStep       = if ($expected -gt 0 -and $expected -lt 300MB) { 25MB } else { 50MB }
    $nextLog       = $logStep
    $sendBuffer    = [byte[]]::new($bufferSize)
    $totalSent     = 0L
    $totalReceived = 0L
    $sendFailed    = $false
    $completed     = $false

    try {
        # Réception en tâche de fond : vide en continu le stdout de ssh (évite tout inter-blocage)
        $receiveTask = $executorOutStream.CopyToAsync($outStream, $bufferSize)

        # Envoi (boucle principale)
        try {
            while (($bytesRead = $srcStream.Read($sendBuffer, 0, $sendBuffer.Length)) -gt 0) {
                $executorInStream.Write($sendBuffer, 0, $bytesRead)
                $totalSent += $bytesRead

                if ($totalSent -ge $nextLog) {
                    $pct    = if ($expected -gt 0) { " ({0} %)" -f [Math]::Min(100, [Math]::Round(100 * $totalSent / $expected)) } else { "" }
                    $speed  = ($totalSent / 1MB) / [Math]::Max(1, $sw.Elapsed.TotalSeconds)
                    $recvMB = [Math]::Round(([System.IO.FileInfo]::new($Plan.RawOutputPath)).Length / 1MB, 1)
                    Write-Log ("Envoi : {0} Mo{1} | reçu : {2} Mo | {3:N1} Mo/s" -f [Math]::Round($totalSent / 1MB), $pct, $recvMB, $speed) -Color Gray
                    $nextLog += $logStep
                }
            }
            $executorInStream.Flush()
        }
        catch [System.IO.IOException] {
            # Le récepteur a fermé son entrée (crash distant, connexion coupée...)
            $sendFailed = $true
            Write-Log "Le récepteur a fermé son entrée prématurément : $($_.Exception.Message)" -Color Red
        }
        finally {
            if ($srcStream)        { $srcStream.Close() }
            if ($executorInStream) { try { $executorInStream.Close() } catch { } }   # signale la fin de l'entrée (EOF)
        }

        # Échec d'envoi : on arrête la source (à la fin normale, elle se termine seule)
        if ($sendFailed -and -not $srcProc.HasExited) { try { $srcProc.Kill() } catch { } }

        Write-Log "Fin d'envoi ($([Math]::Round($totalSent / 1MB, 1)) Mo). En attente de la réponse du pod..." -Color DarkYellow

        try {
            $null = $receiveTask.GetAwaiter().GetResult()
        }
        catch {
            $sendFailed = $true
            Write-Log "Erreur pendant la réception : $($_.Exception.Message)" -Color Red
        }

        $totalReceived = $outStream.Length
        $completed     = $true
    }
    finally {
        # Libère toujours le fichier de sortie (sinon un fichier partiel ne pourrait pas être supprimé)
        $outStream.Dispose()
        if (-not $completed -and $srcProc -and -not $srcProc.HasExited) { try { $srcProc.Kill() } catch { } }
    }

    $srcProc.WaitForExit()
    $executorProc.WaitForExit()   # sans argument : garantit que tous les événements stderr sont traités
    $srcErr = $srcErrTask.GetAwaiter().GetResult()

    Get-EventSubscriber | Where-Object { $_.SourceObject -eq $executorProc } | Unregister-Event -Force -ErrorAction SilentlyContinue
    Remove-Job -Job $stderrEvent -Force -ErrorAction SilentlyContinue

    # --- Résumé des messages d'erreur (source locale et pod) ---
    $srcLines = @()
    if ($srcErr) { $srcLines = @($srcErr -split "`r?`n" | Where-Object { $_ }) }
    if ($srcLines.Count -gt 0) {
        Write-Log "Source locale : $($srcLines.Count) message(s), fichier probablement abîmé (premiers messages) :" -Color Yellow
        $srcLines | Select-Object -First 5 | ForEach-Object { Write-Log "  [LOCAL] $_" -Color DarkGray }
    }
    if ($remoteLog.Lines -gt 0) {
        Write-Log "Pod : $($remoteLog.Lines) message(s) au total ($([Math]::Min($remoteLog.Lines, $maxLines)) affichés)." -Color Yellow
    }

    # --- Contrôles de fin de traitement ---
    if ($executorProc.ExitCode -ne 0 -or $sendFailed) {
        $tail = (($remoteLog.Buffer.ToString() -split "`r?`n") | Select-Object -Last 30) -join "`n"
        throw "Échec du récepteur (ExitCode: $($executorProc.ExitCode)). Derniers messages du pod :`n$tail"
    }
    if ($srcProc.ExitCode -ne 0) {
        throw "La source locale ($($Plan.SourceFile)) a échoué (ExitCode: $($srcProc.ExitCode)) :`n$srcErr"
    }
    if ($totalReceived -eq 0) {
        throw "Le pod n'a renvoyé aucune donnée."
    }

    $sw.Stop()
    Write-Log ("Flux terminé en {0:N1} s : {1:N1} Mo envoyés, {2:N1} Mo reçus." -f $sw.Elapsed.TotalSeconds, ($totalSent / 1MB), ($totalReceived / 1MB)) -Color Green

    return [pscustomobject]@{
        BytesSent     = $totalSent
        BytesReceived = $totalReceived
        Seconds       = $sw.Elapsed.TotalSeconds
        RemoteLines   = $remoteLog.Lines
        LocalMessages = $srcLines.Count
    }
}

# ============================================================================
# 1) PRÉ-TRAITEMENT LOCAL : construit le "plan" du flux selon le mode
# ============================================================================

function New-StreamPlan {
    param ([string]$ResolvedMode)

    $rawPath = "$OutputFile.part"   # écrit en .part puis renommé : jamais de fichier final incomplet

    switch ($ResolvedMode) {
        "Transcribe"  { return New-TranscribePlan  -RawPath $rawPath }
        "EncodeAV1"   { return New-EncodePlan      -RawPath $rawPath }
        "CheckFrames" { return New-CheckFramesPlan -RawPath $rawPath }
    }
}

function New-TranscribePlan {
    param ([string]$RawPath)

    # Audio PCM 16 kHz mono : 32 000 octets par seconde -> taille attendue pour le pourcentage
    $duration = Get-MediaDuration -Path $LocalPath
    $expected = if ($duration) { [long]($duration * 16000 * 2) } else { 0 }
    $action   = if ($ActionType) { $ActionType } else { "Transcribe" }

    return [pscustomobject]@{
        Mode          = "Transcribe"
        SourceFile    = "ffmpeg"
        SourceArgs    = @("-nostdin", "-loglevel", "error", "-i", $LocalPath, "-vn",
                          "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "pipe:1")
        RemoteCommand = New-PythonCommand -Action $action -Optimize
        RawOutputPath = $RawPath
        ExpectedBytes = $expected
    }
}

function Get-EncodeRemoteCommand {
    # $script:NvencFixNeeded n'est connu qu'après Test-PodPreflight ; tant qu'il n'a pas tourné
    # (plan construit avant le contact du pod, ou -SkipPreflight), on part sans LD_PRELOAD.
    $prefix = if ($script:NvencFixNeeded) { "LD_PRELOAD=$($Config.NvencFixRemote) " } else { "" }
    return "${prefix}ffmpeg -nostdin -hide_banner -loglevel error -probesize 32M -analyzeduration 0 -y -i pipe:0 " +
           "-map 0:v:0 -map 0:a? -c:v av1_nvenc -preset $Preset -tune hq -cq $CQ -b:v 0 -pix_fmt p010le -c:a copy " +
           "-f matroska -live 1 -flush_packets 1 pipe:1"
}

function New-EncodePlan {
    param ([string]$RawPath)

    return [pscustomobject]@{
        Mode          = "EncodeAV1"
        SourceFile    = "ffmpeg"
        SourceArgs    = @("-nostdin", "-loglevel", "error", "-i", $LocalPath, "-c", "copy", "-f", "matroska", "pipe:1")
        RemoteCommand = Get-EncodeRemoteCommand
        RawOutputPath = $RawPath
        ExpectedBytes = (Get-Item -LiteralPath $LocalPath).Length
    }
}

function New-CheckFramesPlan {
    param ([string]$RawPath)

    # Le projet Python lit sur stdin un flux Matroska (remux sans réencodage) et écrit le rapport CSV sur stdout.
    # Sources toujours en .mp4 : -c copy suffit, pas de conversion nécessaire.
    # Audio exclu par défaut (-IncludeAudio pour le garder) : inutile pour un contrôle d'images, ça allège le transfert.
    $action = if ($ActionType) { $ActionType } else { "ReadAndCheck" }

    $sourceArgs = @("-nostdin", "-loglevel", "error", "-i", $LocalPath, "-map", "0:v:0")
    if ($IncludeAudio) { $sourceArgs += @("-map", "0:a?") }
    $sourceArgs += @("-c", "copy", "-f", "matroska", "pipe:1")

    return [pscustomobject]@{
        Mode          = "CheckFrames"
        SourceFile    = "ffmpeg"
        SourceArgs    = $sourceArgs
        RemoteCommand = New-PythonCommand -Action $action
        RawOutputPath = $RawPath
        ExpectedBytes = (Get-Item -LiteralPath $LocalPath).Length   # majore un peu si -IncludeAudio est absent (taille de la source complète)
    }
}

# ============================================================================
# VÉRIFICATION DU POD (avant d'envoyer la moindre donnée)
# ============================================================================

function Test-PodPreflight {
    param ([string]$ResolvedMode)

    # Un seul appel SSH : connexion + contrôle propre au mode
    switch ($ResolvedMode) {
        "EncodeAV1" {
            $check = "echo POD_OK"
        }
        default {
            $check = "echo POD_OK; test -x $($Config.RemoteProjectDir)/$($Config.RemotePython) && echo PY_OK || echo PY_MISSING"
        }
    }

    $r = Invoke-PodSsh -Command $check
    if ($r.ExitCode -ne 0 -or $r.Output -notmatch "POD_OK") {
        throw "Connexion SSH impossible :`n$($r.Output)"
    }
    Write-Log "Connexion SSH OK." -Tag POD -Color Green

    switch ($ResolvedMode) {
        "EncodeAV1" {
            # D'abord SANS contournement : la plupart des pods (hôte mono-GPU, ou déjà à jour)
            # n'en ont pas besoin. On n'installe/compile le contournement que si ce test échoue
            # réellement - évite de réclamer nvenc_fix.c pour rien.
            if (Test-NvencEncoder -Preload:$false) {
                $script:NvencFixNeeded = $false
                Write-Log "NVENC opérationnel nativement (pas de contournement nécessaire sur ce pod)." -Tag POD -Color Green
            }
            else {
                Write-Log "av1_nvenc échoue nativement, tentative avec le contournement LD_PRELOAD..." -Tag POD -Color Yellow
                $libCheck = Invoke-PodSsh -Command "test -f $($Config.NvencFixRemote) && echo LIB_OK || echo LIB_MISSING"
                if ($libCheck.Output -match "LIB_MISSING") { Install-NvencFix }
                if (-not (Test-NvencEncoder -Preload:$true)) {
                    throw "L'encodeur NVENC ne fonctionne pas sur ce pod, même avec le contournement (changer d'hôte ?)."
                }
                $script:NvencFixNeeded = $true
                Write-Log "NVENC opérationnel (avec contournement LD_PRELOAD)." -Tag POD -Color Green
            }
        }
        default {
            if ($r.Output -match "PY_MISSING") {
                throw "Python du projet introuvable sur le pod : $($Config.RemoteProjectDir)/$($Config.RemotePython)"
            }
            Write-Log "Environnement Python du projet présent." -Tag POD -Color Green
        }
    }
}

function Install-NvencFix {
    Write-Log "Installation du contournement NVENC (compilation sur le pod)..." -Tag POD

    if (-not (Test-Path -LiteralPath $Config.NvencFixSource)) {
        throw "Ce pod a besoin du contournement NVENC, mais sa source est introuvable en local : $($Config.NvencFixSource). " +
              "Change de pod (hôte mono-GPU de préférence) ou fournis nvenc_fix.c."
    }

    $port   = [string]$script:Pod.PodPort
    $remote = "$SSHUser@$($script:Pod.PodIP):/tmp/nvenc_fix.c"
    & $Config.SCPExe -P $port -i $Config.KeyPath -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes $Config.NvencFixSource $remote 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Envoi de nvenc_fix.c vers le pod impossible (scp)." }

    $build = Invoke-PodSsh -Command "gcc -shared -fPIC -O2 -o $($Config.NvencFixRemote) /tmp/nvenc_fix.c -ldl"
    if ($build.ExitCode -ne 0) { throw "Compilation du contournement impossible :`n$($build.Output)" }
}

function Test-NvencEncoder {
    param ([bool]$Preload = $true)

    $prefix = if ($Preload) { "LD_PRELOAD=$($Config.NvencFixRemote) " } else { "" }
    Write-Log "Test de l'encodeur NVENC$(if ($Preload) { ' (avec contournement)' } else { ' (sans contournement)' })..." -Tag POD
    $test = Invoke-PodSsh -Command "${prefix}ffmpeg -nostdin -hide_banner -loglevel error -f lavfi -i testsrc2=size=1280x720:rate=24 -t 1 -c:v av1_nvenc -preset $Preset -pix_fmt p010le -f null -"
    return ($test.ExitCode -eq 0)
}

# ============================================================================
# 4) POST-TRAITEMENT DE LA SORTIE : selon le mode
# ============================================================================

function Complete-StreamOutput {
    param (
        [string]$ResolvedMode,
        $Plan,
        $Stats
    )

    switch ($ResolvedMode) {
        "Transcribe"  { Complete-Transcribe  -Plan $Plan }
        "EncodeAV1"   { Complete-Encode      -Plan $Plan }
        "CheckFrames" { Complete-CheckFrames -Plan $Plan }
    }
}

function Complete-Transcribe {
    param ($Plan)

    Move-Item -LiteralPath $Plan.RawOutputPath -Destination $OutputFile -Force
    Write-Log "Fichier texte enregistré : $OutputFile" -Tag POST -Color Green

    if ([IO.Path]::GetExtension($OutputFile).ToLower() -eq ".csv") {
        Convert-CsvToSrt -CsvPath $OutputFile
    }
}

function Convert-CsvToSrt {
    param ([string]$CsvPath)

    try {
        $srtPath = [IO.Path]::ChangeExtension($CsvPath, ".srt")
        $rows    = @(Import-Csv -LiteralPath $CsvPath -Delimiter ';')

        if ($rows.Count -eq 0 -or -not $rows[0].PSObject.Properties['srt']) {
            Write-Log "Le CSV ne contient pas de colonne 'srt' : aucun SRT généré." -Tag POST -Color Yellow
            return
        }

        $srtLines = @(foreach ($row in $rows) {
            if (-not [string]::IsNullOrEmpty($row.srt)) { $row.srt -replace '\\n', "`r`n" }
        })
        if ($srtLines.Count -eq 0) {
            Write-Log "La colonne 'srt' est vide : aucun sous-titre généré." -Tag POST -Color Yellow
            return
        }

        [IO.File]::WriteAllLines($srtPath, [string[]]$srtLines, [Text.UTF8Encoding]::new($false))
        Write-Log "Fichier SRT généré : $srtPath" -Tag POST -Color Green
    }
    catch {
        Write-Log "Erreur lors de la génération du SRT : $($_.Exception.Message)" -Tag POST -Color Yellow
    }
}

function Complete-Encode {
    param ($Plan)

    $part      = $Plan.RawOutputPath
    $finalPath = $OutputFile

    if ([IO.Path]::GetExtension($OutputFile).ToLower() -eq ".mp4") {
        Write-Log "Remux local vers MP4 (faststart)..." -Tag POST
        $remuxOut  = & ffmpeg -y -loglevel error -f matroska -i $part -c copy -movflags +faststart $OutputFile 2>&1
        $remuxCode = $LASTEXITCODE

        if ($remuxCode -eq 0 -and (Test-Path -LiteralPath $OutputFile)) {
            Remove-Item -LiteralPath $part -Force
        }
        else {
            Remove-Item -LiteralPath $OutputFile -Force -ErrorAction SilentlyContinue
            $finalPath = [IO.Path]::ChangeExtension($OutputFile, ".mkv")
            Move-Item -LiteralPath $part -Destination $finalPath -Force
            Write-Log "Échec du remux MP4 ($(($remuxOut | Out-String).Trim())) : MKV conservé : $finalPath" -Tag POST -Color Yellow
        }
    }
    else {
        Move-Item -LiteralPath $part -Destination $OutputFile -Force
    }

    # Contrôle de cohérence : la durée doit correspondre à la source
    $srcDur = Get-MediaDuration -Path $LocalPath
    $outDur = Get-MediaDuration -Path $finalPath
    if ($null -ne $srcDur -and $null -ne $outDur) {
        $delta     = [Math]::Abs($srcDur - $outDur)
        $tolerance = [Math]::Max(2.0, $srcDur * 0.001)
        if ($delta -gt $tolerance) {
            Write-Log ("Durée différente de la source : source {0:N1} s, sortie {1:N1} s. Ne supprimez pas l'original avant vérification." -f $srcDur, $outDur) -Tag POST -Color Yellow
        } else {
            Write-Log ("Durée conforme ({0:N1} s)." -f $outDur) -Tag POST -Color Green
        }
    } else {
        Write-Log "Durée non vérifiable (ffprobe indisponible ou fichier illisible)." -Tag POST -Color Yellow
    }

    # Taille et gain
    $srcSize = (Get-Item -LiteralPath $LocalPath).Length
    $outSize = (Get-Item -LiteralPath $finalPath).Length
    $gain    = [Math]::Round((1 - ($outSize / $srcSize)) * 100, 1)
    Write-Log ("Encodage AV1 réussi : {0:N1} Mo -> {1:N1} Mo (gain {2} %) : {3}" -f ($srcSize / 1MB), ($outSize / 1MB), $gain, $finalPath) -Tag POST -Color Green

    if ($outSize -ge $srcSize) {
        Write-Log "Le fichier AV1 n'est pas plus petit que la source : conservez l'original (essayez un -CQ plus élevé)." -Tag POST -Color Yellow
    }
}

function Complete-CheckFrames {
    param ($Plan)

    Move-Item -LiteralPath $Plan.RawOutputPath -Destination $OutputFile -Force

    # $null ne peut pas servir de variable de boucle (c'est une constante en lecture seule en PowerShell)
    $lineCount = 0
    foreach ($ignoredLine in [IO.File]::ReadLines($OutputFile)) { $lineCount++ }
    $dataRows = [Math]::Max(0, $lineCount - 1)   # hors en-tête

    Write-Log "Rapport de contrôle enregistré : $OutputFile ($dataRows ligne(s) de données)." -Tag POST -Color Green
    if ($dataRows -eq 0) {
        Write-Log "Le rapport ne contient aucune ligne de données." -Tag POST -Color Yellow
    }

    # Point d'extension : synthèse du rapport (frames en erreur, score moyen...) selon les colonnes produites par le projet Python.
}

# ============================================================================
# PROGRAMME PRINCIPAL
# ============================================================================

try {
    if ($script:LogPath) {
        $logDir = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($script:LogPath))
        if ($logDir -and -not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    }

    # --- Validation ---
    if (-not (Test-Path -LiteralPath $LocalPath)) { throw "Fichier source introuvable : $LocalPath" }
    foreach ($tool in @("ffmpeg", "ffprobe")) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Outil local introuvable dans le PATH : $tool" }
    }
    if (-not (Test-Path -LiteralPath $Config.SSHExe)) { throw "ssh.exe introuvable : $($Config.SSHExe)" }

    # Chemins absolus : les API .NET utilisent le dossier du processus, pas l'emplacement PowerShell courant
    $LocalPath  = (Resolve-Path -LiteralPath $LocalPath).ProviderPath
    $OutputFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputFile)

    # --- Mode ---
    $ResolvedMode = $Mode
    if ($ResolvedMode -eq "Auto") {
        $ResolvedMode = if ([IO.Path]::GetExtension($OutputFile).ToLower() -in @(".mp4", ".mkv")) { "EncodeAV1" } else { "Transcribe" }
    }
    if ($ResolvedMode -eq "EncodeAV1" -and [IO.Path]::GetExtension($OutputFile).ToLower() -notin @(".mp4", ".mkv")) {
        throw "Mode EncodeAV1 : la sortie doit être en .mp4 ou .mkv."
    }

    $Host.UI.RawUI.WindowTitle = "[$ResolvedMode] $LocalPath"
    $swTotal = [Diagnostics.Stopwatch]::StartNew()

    Write-Log "Mode          : $ResolvedMode" -Tag PRE -Color DarkCyan
    Write-Log "Fichier source: $LocalPath" -Tag PRE
    Write-Log "Fichier cible : $OutputFile" -Tag PRE

    $outDir = [IO.Path]::GetDirectoryName($OutputFile)
    if ($outDir -and -not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

    # --- Étape 1 : pré-traitement local (plan du flux) ---
    $plan = New-StreamPlan -ResolvedMode $ResolvedMode

    # --- Pod ---
    Write-Log "Recherche du pod ($Provider)..." -Tag POD -Color Yellow
    $script:Pod = Resolve-Pod
    if (-not $script:Pod -or -not $script:Pod.PodIP -or -not $script:Pod.PodPort) {
        throw "Impossible de récupérer l'IP ou le port du pod."
    }
    Write-Log "Pod ciblé : $($script:Pod.PodIP):$($script:Pod.PodPort)" -Tag POD -Color Green
    Initialize-SshContext

    if ($SkipPreflight) {
        Write-Log "Pré-vérification du pod ignorée (-SkipPreflight)." -Tag POD -Color Yellow
    } else {
        Test-PodPreflight -ResolvedMode $ResolvedMode
        # $plan a été construit avant de savoir si le contournement NVENC est nécessaire (étape 1
        # avant l'étape POD) ; on met à jour sa commande distante avec ce qu'a déterminé le preflight.
        if ($ResolvedMode -eq "EncodeAV1") { $plan.RemoteCommand = Get-EncodeRemoteCommand }
    }

    # --- Étapes 2 et 3 : flux et journaux ---
    Write-Log "Lancement du streaming..." -Color Yellow
    $stats = Invoke-StreamEngine -Plan $plan

    # --- Étape 4 : post-traitement de la sortie ---
    Write-Log "Post-traitement de la sortie..." -Tag POST -Color Yellow
    Complete-StreamOutput -ResolvedMode $ResolvedMode -Plan $plan -Stats $stats

    $swTotal.Stop()
    Write-Log ("Terminé en {0:N1} s." -f $swTotal.Elapsed.TotalSeconds) -Tag DONE -Color Green
    exit 0
}
catch {
    Write-Log "Échec : $($_.Exception.Message)" -Tag ERROR -Color Red
    # Les fichiers partiels sont toujours écrits en .part : on les supprime
    Remove-Item -LiteralPath "$OutputFile.part" -Force -ErrorAction SilentlyContinue
    exit 1
}