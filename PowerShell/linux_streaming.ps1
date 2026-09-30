<#
.SYNOPSIS
    Streaming Windows -> pod Linux (Vast.ai ou RunPod) : transcription, encodage AV1, contrôle des frames.

.DESCRIPTION
    Un seul schéma pour tous les modes :
      1) PRÉ-TRAITEMENT local          -> New-StreamPlan         (switch sur le mode)
      2) FLUX de streaming             -> Invoke-StreamEngine    (commun à tous les modes)
      3) JOURNAUX d'audit              -> Write-Log              (console + fichier optionnel)
      4) POST-TRAITEMENT de la sortie  -> Complete-StreamOutput  (switch sur le mode)

    SAUT SI DÉJÀ FAIT : si le fichier de sortie (-OutputFile) existe déjà, le traitement est
    sauté immédiatement (code de sortie 0, aucun contact avec le pod). -Force pour le refaire.
    Pratique pour relancer un lot interrompu : seuls les fichiers manquants sont traités.

    CHOIX DU POD : -Pod "<Plateforme>[_<N>]"
        Vast (défaut) = Vast_-1 = le pod Vast.ai le plus ancien ; RunPod = RunPod_-1.
        N° positif = depuis le plus récent (Vast_1, Vast_2...), négatif = depuis le plus ancien (Vast_-2...).
        Le plus ancien par défaut : un lot en cours reste sur son pod même si un autre pod est créé.
        La liste numérotée est affichée à chaque lancement.
        -PodHost / -PodPort court-circuitent la recherche (usage en lot).

    VÉRIFICATION DU POD (Test-PodPreflight), avant d'envoyer la moindre donnée. Volontairement
    légère (quelques secondes) : l'audit complet se fait en amont, au déploiement
    (linux_deploy_python.ps1, qui installe aussi ffmpeg et peut corriger la libcuda « compat »).
      - connexion SSH ;
      - EncodeAV1 : ffmpeg présent, test AV1 NVENC 10 bits natif puis, en cas d'échec, avec la
        libcuda de l'hôte forcée (LD_PRELOAD) : parade à une libcuda « compat » non corrigée ;
        puis test du décodage sur GPU (voir ci-dessous) ;
      - Transcribe / CheckFrames : environnement Python du projet présent.
      -SkipPreflight supprime cette vérification (lot sur un pod déjà validé).

    DÉCODAGE SUR GPU (EncodeAV1, -Decode Auto par défaut) : la source est décodée par le décodeur
    matériel de la carte (NVDEC) au lieu du CPU du pod, et les images restent dans la mémoire du GPU
    jusqu'à l'encodeur AV1 (NVENC). Gain net quand le CPU du pod saturait (source 4K, peu de vCPU).
    Choisi seulement si le codec de la source est décodable par NVDEC (H.264 8 bits, HEVC 8/10 bits,
    AV1, VP9, MPEG-2, VC-1) ET si un test réel sur le pod réussit ; sinon, décodage CPU comme avant.
    -Decode Cpu force l'ancien fonctionnement, -Decode Gpu force le GPU.

    Scripts requis dans le même dossier : get_vast_pod_info.ps1, get_run_pod_info.ps1.

    Modes :
      Transcribe   : audio PCM 16 kHz -> Main.py (Transcribe)-> CSV, puis SRT
      EncodeAV1    : vidéo (Matroska)-> ffmpeg NVENC AV1     -> MKV, puis MP4 (faststart)
      CheckFrames  : vidéo (Matroska)-> Main.py (ReadAndCheck)-> rapport CSV
    Sans -Mode : .mp4/.mkv en sortie = EncodeAV1, sinon Transcribe (comportement de l'ancien script).

    AJOUTER UN MODE :
      - l'ajouter au ValidateSet de -Mode ;
      - ajouter un case dans New-StreamPlan, Test-PodPreflight et Complete-StreamOutput ;
      - écrire New-<Mode>Plan et Complete-<Mode> en s'inspirant de ceux des modes existants.
    AJOUTER UN FOURNISSEUR : un script get_<x>_pod_info.ps1 (mêmes paramètres et même objet
    renvoyé que les deux existants) et une entrée dans $Config.GetPodScripts.

.EXAMPLE
    .\linux_streaming.ps1 "E:\Series\ep1.mp4" "D:\Out\ep1_AV1.mp4"                 # pod Vast.ai le plus ancien
.EXAMPLE
    .\linux_streaming.ps1 "E:\Series\ep1.mp4" "D:\Out\ep1_AV1.mp4" -Pod RunPod_2
.EXAMPLE
    .\linux_streaming.ps1 "E:\Series\ep1.mp4" "D:\Out\ep1.csv" -Mode Transcribe -Pod Vast_2
.EXAMPLE
    .\linux_streaming.ps1 "E:\Series\ep1.mp4" "D:\Out\ep1_frames.csv" -Mode CheckFrames -LogFile "D:\Out\audit.log"
.EXAMPLE
    # Lot : IP/port connus ; les fichiers déjà produits sont sautés
    .\linux_streaming.ps1 "a.mp4" "a_AV1.mp4" -PodHost 1.2.3.4 -PodPort 50367 -Preset p5
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

    # Plateforme et n° du pod : "Vast" (= Vast_-1, le plus ancien), "Vast_1" (le plus récent), "RunPod_-2"...
    [string]$Pod = "Vast",

    # Contournent la détection (utile en lot : détecter une fois, réutiliser)
    [string]$PodHost = "",
    [string]$PodPort = "",
    [string]$SSHUser = "root",


    # Refait le traitement même si le fichier de sortie existe déjà
    [switch]$Force,

    # Ancien paramètre, gardé pour compatibilité : équivaut à -Pod <Provider>
    [ValidateSet("", "Vast", "RunPod", "Manual")]
    [string]$Provider = "",

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

    # Décodage de la source sur le pod (mode EncodeAV1) :
    #   Auto : sur le GPU (NVDEC) si le codec de la source le permet ET si le test du pod réussit, sinon CPU
    #   Gpu  : force le GPU (sans test si -SkipPreflight)      Cpu : force le CPU (comportement d'avant)
    [ValidateSet("Auto", "Gpu", "Cpu")]
    [string]$Decode = "Auto",

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
    GetPodScripts    = @{
        Vast   = Join-Path $PSScriptRoot "get_vast_pod_info.ps1"
        RunPod = Join-Path $PSScriptRoot "get_run_pod_info.ps1"
    }
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
$script:PodInfo        = $null
$script:SshOptions = @()
$script:SshTarget  = ""
# Chemin de la libcuda de l'HÔTE à forcer via LD_PRELOAD pour ffmpeg (mode EncodeAV1).
# Nécessaire quand l'image embarque une libcuda « compat » (ex. 595 dans /usr/local/cuda-*/compat)
# plus récente que le driver de l'hôte : non supportée sur GeForce, elle fait échouer cuInit.
# Déterminé par Test-PodPreflight ; reste vide si av1_nvenc marche nativement
# ou avec -SkipPreflight.
$script:NvencPreload = ""
# Décodage de la source sur le GPU (NVDEC) : la vidéo reste dans la mémoire de la carte du décodage
# à l'encodage, le CPU du pod n'a presque plus rien à faire. Déterminé par Resolve-DecodePath.
$script:GpuDecode    = $false
$script:SourceVideo  = $null
$script:PodPlatform  = ""
$script:PodIndex     = -1

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

# "Vast" (= Vast_-1), "Vast_2", "Vast_-2", "RunPod", "RunPod_3" (casse indifférente ; "VastAi", "Vast.ai-2" acceptés)
# N° positif : depuis le plus récent (1 = le plus récent) ; négatif : depuis le plus ancien (-1 = le plus ancien).
function Resolve-PodSelector {
    param ([string]$Selector)

    $m = [regex]::Match($Selector.Trim(), '^(?<plat>vast(?:[\.\s]?ai)?|runpod)(?:[_\-\s]?(?<n>-?\d+))?$', 'IgnoreCase')
    if (-not $m.Success) {
        throw "-Pod '$Selector' invalide. Formats acceptés : Vast, Vast_2, Vast_-1, RunPod, RunPod_3..."
    }
    # Sans n° : le plus ancien (-1), pour qu'un lot en cours ne change pas de pod quand un autre est créé
    $index = if ($m.Groups['n'].Success) { [int]$m.Groups['n'].Value } else { -1 }
    if ($index -eq 0) { throw "-Pod '$Selector' : n° 0 invalide (1 = le plus récent, -1 = le plus ancien)." }

    return [pscustomobject]@{
        Platform = $(if ($m.Groups['plat'].Value -match '^runpod$') { "RunPod" } else { "Vast" })
        Index    = $index
    }
}

function Resolve-Pod {
    if ($PodHost -and $PodPort) {
        return [pscustomobject]@{ PodId = "manual"; PodIP = $PodHost; PodPort = $PodPort }
    }
    if ($Provider -eq "Manual") { throw "Fournisseur Manual : -PodHost et -PodPort sont obligatoires." }

    $getScript = $Config.GetPodScripts[$script:PodPlatform]
    if (-not (Test-Path -LiteralPath $getScript)) { throw "Script de détection du pod introuvable : $getScript" }

    # Mode Simple : IP et port seulement, pas d'audit (Test-PodPreflight fait la vérification utile)
    $podArgs = @{ Mode = "Simple"; Index = $script:PodIndex }
    return (@(& $getScript @podArgs) | Where-Object { $_ -and $_.PSObject.Properties['PodIP'] } | Select-Object -First 1)
}

function Initialize-SshContext {
    # Options communes, toujours AVANT l'hôte : après, elles seraient prises pour la commande distante
    $script:SshOptions = @(
        "-T",
        "-p", [string]$script:PodInfo.PodPort,
        "-i", $Config.KeyPath,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",        # échoue au lieu de demander un mot de passe
        "-o", "ConnectTimeout=15"
    )
    $script:SshTarget = "$SSHUser@$($script:PodInfo.PodIP)"
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

# Latence mesurée par ouverture de connexion TCP sur le port SSH du pod (Vast et RunPod bloquent
# souvent le ping ICMP : l'ancien Test-Connection attendait alors ~16 s pour rien à chaque fichier).
function Measure-PodLatency {
    param ([string]$TargetHost, [string]$Port, [int]$Samples = 3, [int]$TimeoutMs = 1000)

    $times = foreach ($i in 1..$Samples) {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $sw = [Diagnostics.Stopwatch]::StartNew()
            if ($client.ConnectAsync($TargetHost, [int]$Port).Wait($TimeoutMs)) { $sw.Elapsed.TotalMilliseconds }
        } catch { } finally { $client.Dispose() }
    }
    if (-not $times) { return $null }
    return ($times | Measure-Object -Minimum).Minimum
}

function Get-StreamBufferSize {
    param ([string]$TargetHost, [string]$Port)

    $default = 524288   # 512 Ko
    if ([string]::IsNullOrWhiteSpace($TargetHost) -or [string]::IsNullOrWhiteSpace($Port)) { return $default }

    $rtt = Measure-PodLatency -TargetHost $TargetHost -Port $Port
    if ($null -eq $rtt) {
        Write-Log "Latence TCP non mesurable : buffer par défaut à 512 Ko." -Color Gray
        return $default
    }
    if ($rtt -gt 100) {
        Write-Log "Latence TCP élevée ($([Math]::Round($rtt)) ms) : buffer à 2 Mo." -Color Yellow
        return 2097152
    }
    elseif ($rtt -gt 40) {
        Write-Log "Latence TCP moyenne ($([Math]::Round($rtt)) ms) : buffer à 1 Mo." -Color Gray
        return 1048576
    }
    Write-Log "Latence TCP faible ($([Math]::Round($rtt)) ms) : buffer à 512 Ko." -Color Gray
    return $default
}

function Invoke-StreamEngine {
    param ([Parameter(Mandatory = $true)] $Plan)

    $sw       = [Diagnostics.Stopwatch]::StartNew()
    $maxLines = $Config.MaxLogLines
    $utf8     = [System.Text.Encoding]::UTF8

    # --- Récepteur : ssh.exe qui exécute la commande distante du plan ---
    $sshArgs = @("-q", "-o", "Compression=no", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                 "-c", "aes128-gcm@openssh.com,chacha20-poly1305@openssh.com") +
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

    $bufferSize = Get-StreamBufferSize -TargetHost $script:PodInfo.PodIP -Port $script:PodInfo.PodPort

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
    $lastLogBytes  = 0L                               # pour le débit instantané (depuis le dernier affichage)
    $lastLogTime   = 0.0

    try {
        # Réception en tâche de fond : vide en continu le stdout de ssh (évite tout inter-blocage)
        $receiveTask = $executorOutStream.CopyToAsync($outStream, $bufferSize)

        # Envoi (boucle principale)
        try {
            while (($bytesRead = $srcStream.Read($sendBuffer, 0, $sendBuffer.Length)) -gt 0) {
                $executorInStream.Write($sendBuffer, 0, $bytesRead)
                $totalSent += $bytesRead

                if ($totalSent -ge $nextLog) {
                    $now     = $sw.Elapsed.TotalSeconds
                    $pct     = if ($expected -gt 0) { " ({0} %)" -f [Math]::Min(100, [Math]::Round(100 * $totalSent / $expected)) } else { "" }
                    # Débit instantané : octets envoyés depuis le dernier affichage / temps écoulé depuis
                    $instant = (($totalSent - $lastLogBytes) / 1MB) / [Math]::Max(0.1, $now - $lastLogTime)
                    $average = ($totalSent / 1MB) / [Math]::Max(0.1, $now)
                    $recvMB  = [Math]::Round(([System.IO.FileInfo]::new($Plan.RawOutputPath)).Length / 1MB, 1)
                    $eta     = ""
                    if ($expected -gt $totalSent -and $instant -gt 0) {
                        $eta = " | reste ~" + [TimeSpan]::FromSeconds([Math]::Round((($expected - $totalSent) / 1MB) / $instant)).ToString()
                    }
                    Write-Log ("Envoi : {0} Mo{1} | reçu : {2} Mo | {3:N1} Mo/s (moy. {4:N1}){5}" -f [Math]::Round($totalSent / 1MB), $pct, $recvMB, $instant, $average, $eta) -Color Gray
                    $lastLogBytes = $totalSent
                    $lastLogTime  = $now
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
    # $script:NvencPreload n'est connu qu'après l'audit / Test-PodPreflight ; tant qu'ils n'ont pas
    # tourné (plan construit avant le contact du pod, ou -SkipPreflight), on part sans LD_PRELOAD.
    $prefix = if ($script:NvencPreload) { "LD_PRELOAD=$($script:NvencPreload) " } else { "" }

    # GPU : décodage NVDEC, images gardées en mémoire GPU, conversion 10 bits par scale_cuda.
    # CPU : décodage logiciel, conversion 10 bits par -pix_fmt, puis envoi des images brutes au GPU.
    if ($script:GpuDecode) {
        $decode  = "-hwaccel cuda -hwaccel_output_format cuda "
        $convert = "-vf scale_cuda=format=p010le"
    } else {
        $decode  = ""
        $convert = "-pix_fmt p010le"
    }
    return "${prefix}ffmpeg -nostdin -hide_banner -loglevel error -probesize 32M -analyzeduration 0 -y ${decode}-i pipe:0 " +
           "-map 0:v:0 -map 0:a? -c:v av1_nvenc -preset $Preset -tune hq -cq $CQ -b:v 0 $convert -c:a copy " +
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
    # Métadonnées et chapitres retirés : inutiles au contrôle des images, et certains vieux fichiers
    # les ont en Latin-1, que PyAV refuse d'ouvrir (UnicodeDecodeError sur « é »).
    $sourceArgs += @("-map_metadata", "-1", "-map_chapters", "-1", "-c", "copy", "-f", "matroska", "pipe:1")

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
            $check = "echo POD_OK; command -v ffmpeg >/dev/null 2>&1 && echo FF_OK || echo FF_MISSING"
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
            if ($r.Output -match "FF_MISSING") {
                throw "ffmpeg absent du pod : lance d'abord le déploiement (linux_deploy_python.ps1), dont l'audit l'installe."
            }
            if (Test-NvencEncoder) {
                $script:NvencPreload = ""
                Write-Log "NVENC opérationnel nativement." -Tag POD -Color Green
                Resolve-DecodePath -Preflight
                return
            }

            # Échec natif : cause la plus fréquente = libcuda « compat » de l'image chargée à la place
            # de celle du driver de l'hôte. On force celle de l'hôte (même version que le driver).
            Write-Log "av1_nvenc échoue nativement, essai avec la libcuda de l'hôte forcée..." -Tag POD -Color Yellow
            $find = Invoke-PodSsh -Command ('d=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d " "); ' +
                'for f in /usr/lib/x86_64-linux-gnu/libcuda.so.$d /lib/x86_64-linux-gnu/libcuda.so.$d /usr/lib64/libcuda.so.$d; do ' +
                '[ -n "$d" ] && [ -f "$f" ] && { echo "HOSTLIB=$f"; break; }; done')
            $m = [regex]::Match($find.Output, 'HOSTLIB=(\S+)')
            if (-not $m.Success) {
                throw "L'encodeur AV1 NVENC ne fonctionne pas sur ce pod et la libcuda de l'hôte est introuvable. " +
                      "Lance l'audit (get_*_pod_info.ps1 -Mode Audit) pour le diagnostic, ou change de pod."
            }
            if (-not (Test-NvencEncoder -Preload $m.Groups[1].Value)) {
                throw "L'encodeur AV1 NVENC ne fonctionne pas sur ce pod, même avec la libcuda de l'hôte " +
                      "(nvidia-uvm défectueux ?). Lance l'audit (get_*_pod_info.ps1 -Mode Audit) ou change de pod."
            }
            $script:NvencPreload = $m.Groups[1].Value
            Write-Log "NVENC opérationnel avec LD_PRELOAD=$($script:NvencPreload) (redéploie avec -FixCompat pour corriger le pod)." -Tag POD -Color Yellow
            Resolve-DecodePath -Preflight
        }
        default {
            if ($r.Output -match "PY_MISSING") {
                throw "Python du projet introuvable sur le pod : $($Config.RemoteProjectDir)/$($Config.RemotePython)"
            }
            Write-Log "Environnement Python du projet présent." -Tag POD -Color Green
        }
    }
}

function Test-NvencEncoder {
    param ([string]$Preload = "")

    $prefix = if ($Preload) { "LD_PRELOAD=$Preload " } else { "" }
    Write-Log "Test de l'encodeur NVENC$(if ($Preload) { " (libcuda de l'hôte forcée)" } else { ' (natif)' })..." -Tag POD
    $test = Invoke-PodSsh -Command "${prefix}ffmpeg -nostdin -hide_banner -loglevel error -f lavfi -i testsrc2=size=1280x720:rate=24 -t 1 -c:v av1_nvenc -preset $Preset -pix_fmt p010le -f null -"
    return ($test.ExitCode -eq 0)
}

# ----------------------------------------------------------------------------
# DÉCODAGE GPU (NVDEC) OU CPU
# ----------------------------------------------------------------------------

# Codec et format de pixels de la 1re piste vidéo de la source (ffprobe local, instantané)
function Get-SourceVideoInfo {
    param ([string]$Path)
    $raw  = & ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,pix_fmt,width,height -of "default=noprint_wrappers=1" $Path 2>$null
    $info = @{}
    foreach ($line in $raw) { if ("$line" -match '^(\w+)=(.*)$') { $info[$Matches[1]] = $Matches[2].Trim() } }
    return [pscustomobject]@{
        Codec  = $info['codec_name']
        PixFmt = $info['pix_fmt']
        Size   = $(if ($info['width']) { "$($info['width'])x$($info['height'])" } else { "?" })
    }
}

# Combinaisons que NVDEC décode sur les GPU compatibles AV1 NVENC (Ada et plus récents).
# Hors liste (H.264 10 bits, 4:2:2, 4:4:4, codecs anciens...) : ffmpeg repasserait en décodage
# logiciel, incompatible avec scale_cuda -> on choisit directement le CPU.
function Test-NvdecCompatible {
    param ($Video)
    if (-not $Video -or -not $Video.Codec) { return $false }
    $supported = @{
        h264       = @("yuv420p", "yuvj420p")
        hevc       = @("yuv420p", "yuvj420p", "yuv420p10le")
        av1        = @("yuv420p", "yuv420p10le")
        vp9        = @("yuv420p", "yuv420p10le")
        mpeg2video = @("yuv420p")
        vc1        = @("yuv420p")
    }
    return ($supported.ContainsKey($Video.Codec) -and $Video.PixFmt -in $supported[$Video.Codec])
}

# Teste sur le pod la chaîne complète NVDEC -> scale_cuda -> NVENC, sur un court extrait
# encodé au même codec que la source (vérifie aussi que ce ffmpeg a bien le filtre scale_cuda).
function Test-GpuDecodePipeline {
    $prefix = if ($script:NvencPreload) { "LD_PRELOAD=$($script:NvencPreload) " } else { "" }
    $v      = $script:SourceVideo
    $sample = switch ($v.Codec) {
        "h264"  { "-c:v h264_nvenc -pix_fmt yuv420p" }
        "av1"   { if ($v.PixFmt -like "*10le") { "-c:v av1_nvenc -pix_fmt p010le" } else { "-c:v av1_nvenc -pix_fmt yuv420p" } }
        default { if ($v.PixFmt -like "*10le") { "-c:v hevc_nvenc -profile:v main10 -pix_fmt p010le" } else { "-c:v hevc_nvenc -pix_fmt yuv420p" } }
    }
    Write-Log "Test du décodage GPU (NVDEC -> scale_cuda -> NVENC)..." -Tag POD
    $cmd = "${prefix}ffmpeg -nostdin -hide_banner -loglevel error -f lavfi -i testsrc2=size=1280x720:rate=24 -t 1 $sample -f matroska - | " +
           "${prefix}ffmpeg -nostdin -hide_banner -loglevel error -hwaccel cuda -hwaccel_output_format cuda -i pipe:0 " +
           "-vf scale_cuda=format=p010le -c:v av1_nvenc -preset $Preset -f null -"
    $test = Invoke-PodSsh -Command "set -o pipefail; $cmd"
    return [pscustomobject]@{ Ok = ($test.ExitCode -eq 0); Output = $test.Output }
}

# Fixe $script:GpuDecode selon -Decode, le codec de la source et (si -Preflight) le test sur le pod
function Resolve-DecodePath {
    param ([switch]$Preflight)

    $v     = $script:SourceVideo
    $label = if ($v) { "$($v.Codec) $($v.PixFmt) $($v.Size)" } else { "inconnue" }

    switch ($Decode) {
        "Cpu" {
            $script:GpuDecode = $false
            Write-Log "Décodage : CPU (forcé par -Decode Cpu)." -Tag POD -Color Gray
            return
        }
        "Gpu" {
            if (-not $Preflight) {
                $script:GpuDecode = $true
                Write-Log "Décodage : GPU (forcé par -Decode Gpu, non testé)." -Tag POD -Color Yellow
                return
            }
        }
        default {
            if (-not $Preflight) {
                $script:GpuDecode = $false
                Write-Log "Décodage : CPU (-SkipPreflight : GPU non testé ; -Decode Gpu pour le forcer)." -Tag POD -Color Gray
                return
            }
            if (-not (Test-NvdecCompatible $v)) {
                $script:GpuDecode = $false
                Write-Log "Décodage : CPU (source $label non décodable par NVDEC)." -Tag POD -Color Yellow
                return
            }
        }
    }

    $t = Test-GpuDecodePipeline
    if ($t.Ok) {
        $script:GpuDecode = $true
        Write-Log "Décodage : GPU (NVDEC), source $label : toute la chaîne reste sur la carte graphique." -Tag POD -Color Green
    } else {
        $script:GpuDecode = $false
        $why = (($t.Output -split "`r?`n") | Where-Object { $_ } | Select-Object -First 2) -join " | "
        Write-Log "Décodage : CPU (le test du décodage GPU a échoué sur ce pod : $why)." -Tag POD -Color Yellow
    }
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

    # --- Saut si la sortie existe déjà (avant tout travail, sans contacter le pod) ---
    $OutputFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputFile)
    if (Test-Path -LiteralPath $OutputFile -PathType Leaf) {
        if (-not $Force) {
            Write-Log "Sortie déjà présente, traitement sauté : $OutputFile (-Force pour refaire)." -Tag SKIP -Color DarkYellow
            exit 0
        }
        Write-Log "Sortie déjà présente, elle sera remplacée (-Force) : $OutputFile" -Tag PRE -Color Yellow
    }

    # --- Validation ---
    if (-not (Test-Path -LiteralPath $LocalPath)) { throw "Fichier source introuvable : $LocalPath" }
    foreach ($tool in @("ffmpeg", "ffprobe")) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Outil local introuvable dans le PATH : $tool" }
    }
    if (-not (Test-Path -LiteralPath $Config.SSHExe)) { throw "ssh.exe introuvable : $($Config.SSHExe)" }

    # Chemins absolus : les API .NET utilisent le dossier du processus, pas l'emplacement PowerShell courant
    $LocalPath  = (Resolve-Path -LiteralPath $LocalPath).ProviderPath

    # --- Pod ciblé : -Pod "Vast_2" (l'ancien -Provider reste accepté) ---
    if ($Provider -and $Provider -ne "Manual" -and -not $PSBoundParameters.ContainsKey('Pod')) { $Pod = $Provider }
    if (-not ($PodHost -and $PodPort) -and $Provider -ne "Manual") {
        $sel = Resolve-PodSelector -Selector $Pod
        $script:PodPlatform = $sel.Platform
        $script:PodIndex    = $sel.Index
    }

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
    if ($ResolvedMode -eq "EncodeAV1") {
        $script:SourceVideo = Get-SourceVideoInfo -Path $LocalPath
        Write-Log "Vidéo source  : $($script:SourceVideo.Codec) $($script:SourceVideo.PixFmt) $($script:SourceVideo.Size)" -Tag PRE
    }

    $outDir = [IO.Path]::GetDirectoryName($OutputFile)
    if ($outDir -and -not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

    # --- Étape 1 : pré-traitement local (plan du flux) ---
    $plan = New-StreamPlan -ResolvedMode $ResolvedMode

    # --- Pod ---
    $podLabel = if ($PodHost -and $PodPort) { "manuel" } else { "$($script:PodPlatform)_$($script:PodIndex)" }
    Write-Log "Recherche du pod ($podLabel)..." -Tag POD -Color Yellow
    $script:PodInfo = Resolve-Pod
    if (-not $script:PodInfo -or -not $script:PodInfo.PodIP -or -not $script:PodInfo.PodPort) {
        throw "Impossible de récupérer l'IP ou le port du pod."
    }
    Write-Log "Pod ciblé : $podLabel (ID $($script:PodInfo.PodId))-> $($script:PodInfo.PodIP):$($script:PodInfo.PodPort)" -Tag POD -Color Green
    Initialize-SshContext

    if ($SkipPreflight) {
        Write-Log "Pré-vérification du pod ignorée (-SkipPreflight)." -Tag POD -Color Yellow
        if ($ResolvedMode -eq "EncodeAV1") { Resolve-DecodePath }
    } else {
        Test-PodPreflight -ResolvedMode $ResolvedMode
    }
    # $plan a été construit avant de connaître LD_PRELOAD et le mode de décodage (étape 1 avant
    # l'étape POD) ; on met à jour sa commande distante avec ce qui a été déterminé.
    if ($ResolvedMode -eq "EncodeAV1") { $plan.RemoteCommand = Get-EncodeRemoteCommand }

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