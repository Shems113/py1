[CmdletBinding()]
param (
    [Parameter(Mandatory = $true, HelpMessage = "Chemin du fichier multimédia en entrée")]
    [ValidateNotNullOrEmpty()]
    [string]$InputFile,

    [Parameter(Mandatory = $true, HelpMessage = "Chemin du fichier de sortie")]
    [ValidateNotNullOrEmpty()]
    [string]$OutputFile,

    [Parameter(Mandatory = $true, HelpMessage = "Exécutable cible (ex: python.exe, uv ou ssh.exe)")]
    [ValidateNotNullOrEmpty()]
    [string]$ExecutorExe,

    [Parameter(Mandatory = $false, HelpMessage = "Flags d'optimisation ou options de l'exécutable (ex: -OO)")]
    [string[]]$ExecutorFlags = @(),

    [Parameter(Mandatory = $false, HelpMessage = "Chemin du script ou binaire cible (ex: Main.py)")]
    [string]$ScriptPath = "",

    [Parameter(Mandatory = $false, HelpMessage = "Arguments transmis au script ou à la commande distante")]
    [string[]]$AppArgs = @(),

    [Parameter(Mandatory = $false, HelpMessage = "IP cible pour mesure de latence ICMP")]
    [string]$PodIP = ""
)

# Encodage console UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8
$utf8NoBom                = [System.Text.UTF8Encoding]::new($false)

$sw = [Diagnostics.Stopwatch]::StartNew()

function Write-StreamLog {
    param (
        [string]$Message,
        [ConsoleColor]$Color = [ConsoleColor]::Cyan
    )
    $timestamp = Get-Date -Format "HH:mm:ss.fff"
    Write-Host "[$timestamp] [AUDIO-STREAMING] $Message" -ForegroundColor $Color
}

function Get-StreamBufferSize {
    param ([string]$TargetHost)

    $default = 524288   # 512 Ko
    if ([string]::IsNullOrWhiteSpace($TargetHost)) { return $default }

    try {
        $replies = @(Test-Connection -TargetName $TargetHost -Count 3 -ErrorAction SilentlyContinue |
                     Where-Object { $_.Status -eq 'Success' })

        if ($replies.Count -eq 0) {
            Write-StreamLog "ICMP sans réponse : buffer par défaut à 512 Ko." -Color Gray
            return $default
        }

        # PowerShell 7 : la propriété s'appelle Latency (ResponseTime n'existe plus)
        $avgRtt = ($replies | Measure-Object -Property Latency -Average).Average

        if ($avgRtt -gt 100) {
            Write-StreamLog "Latence élevée ($([Math]::Round($avgRtt)) ms) : buffer à 2 Mo." -Color Yellow
            return 2097152
        }
        elseif ($avgRtt -gt 40) {
            Write-StreamLog "Latence moyenne ($([Math]::Round($avgRtt)) ms) : buffer à 1 Mo." -Color Gray
            return 1048576
        }
        else {
            Write-StreamLog "Latence faible ($([Math]::Round($avgRtt)) ms) : buffer à 512 Ko." -Color Gray
            return 524288
        }
    }
    catch {
        Write-StreamLog "Mesure de latence impossible : buffer par défaut à 512 Ko." -Color Gray
        return $default
    }
}

Write-StreamLog "Démarrage du pipeline de transcription..." -Color DarkCyan
Write-StreamLog "Fichier source : $InputFile"
Write-StreamLog "Fichier cible  : $OutputFile"
Write-StreamLog "Exécutable     : $ExecutorExe"

# --- 1. Validation des accès ---
if (-not (Test-Path -LiteralPath $InputFile)) { throw "Fichier d'entrée introuvable : $InputFile" }
if (-not (Get-Command -Name $ExecutorExe -ErrorAction SilentlyContinue)) { throw "Commande/exécutable introuvable : $ExecutorExe" }

# --- 2. Assemblage des arguments du processus récepteur ---
$FullArgs = @()
if ($ExecutorFlags) { $FullArgs += $ExecutorFlags }
if ($ScriptPath)    { $FullArgs += $ScriptPath }
if ($AppArgs)       { $FullArgs += $AppArgs }

Write-StreamLog "Commande exécutée : $ExecutorExe $($FullArgs -join ' ')" -Color Gray

# --- 3. Configuration des processus : FFmpeg local (source) et récepteur (SSH / Python) ---
$ffmpegPsi = [Diagnostics.ProcessStartInfo]@{
    FileName               = "ffmpeg"
    UseShellExecute        = $false
    RedirectStandardOutput = $true
    RedirectStandardError  = $true
    StandardErrorEncoding  = $utf8NoBom
}
# -nostdin : ffmpeg ne doit pas lire le clavier de la console ; -vn : pas de décodage vidéo
$ffmpegArgs = @(
    "-nostdin", "-loglevel", "error",
    "-i", $InputFile,
    "-vn",
    "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
    "pipe:1"
)
foreach ($arg in $ffmpegArgs) { $ffmpegPsi.ArgumentList.Add($arg) }

$executorPsi = [Diagnostics.ProcessStartInfo]@{
    FileName               = $ExecutorExe
    UseShellExecute        = $false
    RedirectStandardInput  = $true
    RedirectStandardOutput = $true
    RedirectStandardError  = $true
    StandardOutputEncoding = $utf8NoBom
    StandardErrorEncoding  = $utf8NoBom
}
foreach ($arg in $FullArgs) { $executorPsi.ArgumentList.Add($arg) }

$executorProc = [System.Diagnostics.Process]::new()
$executorProc.StartInfo = $executorPsi
$executorErrBuffer = [System.Text.StringBuilder]::new()

$stderrEvent = Register-ObjectEvent -InputObject $executorProc -EventName "ErrorDataReceived" -Action {
    if ($Event.SourceEventArgs.Data) {
        $logLine = $Event.SourceEventArgs.Data
        [void]$Event.MessageData.AppendLine($logLine)
        Write-Host "  [REMOTE LOG] $logLine" -ForegroundColor DarkGray
    }
} -MessageData $executorErrBuffer

# --- 4. Buffer selon la latence ---
$bufferSize = Get-StreamBufferSize -TargetHost $PodIP

# --- 5. Démarrage des processus ---
Write-StreamLog "Lancement des processus FFmpeg et récepteur..."
$ffmpegProc = [Diagnostics.Process]::Start($ffmpegPsi)

# stderr de ffmpeg lu EN CONTINU : sinon le tampon se remplit et ffmpeg se bloque
$ffmpegErrTask = $ffmpegProc.StandardError.ReadToEndAsync()

$null = $executorProc.Start()
$executorProc.BeginErrorReadLine()

# stdout du récepteur lu EN CONTINU (au lieu d'un ReadToEnd après l'envoi)
$stdoutTask = $executorProc.StandardOutput.ReadToEndAsync()

Write-StreamLog "Processus démarrés [FFmpeg PID: $($ffmpegProc.Id)] -> [Récepteur PID: $($executorProc.Id)]" -Color DarkYellow

# --- 6. Envoi du flux audio PCM ---
$buffer           = [byte[]]::new($bufferSize)
$ffmpegStream     = $ffmpegProc.StandardOutput.BaseStream
$executorInStream = $executorProc.StandardInput.BaseStream

$totalBytesSent = 0L
$chunkCount     = 0
$logStep        = 50MB
$nextLog        = $logStep
$sendFailed     = $false

Write-StreamLog "Début du transfert audio PCM 16 kHz mono par blocs de $($bufferSize / 1KB) Ko..."

try {
    while (($bytesRead = $ffmpegStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $executorInStream.Write($buffer, 0, $bytesRead)
        $totalBytesSent += $bytesRead
        $chunkCount++

        if ($totalBytesSent -ge $nextLog) {
            Write-StreamLog ("Flux en cours : {0} Mo transmis ({1} blocs)..." -f [Math]::Round($totalBytesSent / 1MB, 2), $chunkCount) -Color Gray
            $nextLog += $logStep
        }
    }
    $executorInStream.Flush()
}
catch [System.IO.IOException] {
    # Le récepteur a fermé son entrée (crash, connexion coupée...)
    $sendFailed = $true
    Write-StreamLog "Le récepteur a fermé son entrée prématurément : $($_.Exception.Message)" -Color Red
}
finally {
    if ($executorInStream) {
        try { $executorInStream.Close() } catch { }   # signale la fin du flux (EOF)
    }
    $totalMB = [Math]::Round($totalBytesSent / 1MB, 2)
    Write-StreamLog "Fin d'extraction audio. Total transféré : $totalMB Mo ($chunkCount blocs)." -Color DarkYellow
}

# On ne tue ffmpeg qu'en cas d'échec de l'envoi (à la fin normale il se termine seul)
if ($sendFailed -and -not $ffmpegProc.HasExited) {
    try { $ffmpegProc.Kill() } catch { }
}

# --- 7. Attente de l'inférence et récupération de la réponse ---
Write-StreamLog "En attente de la réponse du récepteur (inférence Whisper en cours)..."

$dataResult = $stdoutTask.GetAwaiter().GetResult()
$ffmpegErr  = $ffmpegErrTask.GetAwaiter().GetResult()

$ffmpegProc.WaitForExit()
$executorProc.WaitForExit()   # sans argument : garantit que les événements stderr sont tous traités

# Nettoyage des abonnements d'événements
Get-EventSubscriber | Where-Object { $_.SourceObject -eq $executorProc } | Unregister-Event -Force -ErrorAction SilentlyContinue
Remove-Job -Job $stderrEvent -Force -ErrorAction SilentlyContinue

if ($ffmpegErr) {
    Write-StreamLog "Messages de FFmpeg (stderr) : $ffmpegErr" -Color DarkGray
}

# --- 8. Contrôles de fin de traitement ---
if ($executorProc.ExitCode -ne 0 -or $sendFailed) {
    Write-StreamLog "Échec du processus récepteur (ExitCode: $($executorProc.ExitCode))" -Color Red
    throw "L'exécutable $ExecutorExe s'est terminé avec une erreur :`n$($executorErrBuffer.ToString())"
}

if ($ffmpegProc.ExitCode -ne 0) {
    Write-StreamLog "FFmpeg local a échoué (ExitCode: $($ffmpegProc.ExitCode)) : l'audio extrait est peut-être incomplet." -Color Red
    throw "FFmpeg local a échoué (ExitCode: $($ffmpegProc.ExitCode)) :`n$ffmpegErr"
}

if ([string]::IsNullOrWhiteSpace($dataResult)) {
    Write-StreamLog "Aucune donnée reçue du récepteur." -Color Red
    throw "Le récepteur s'est terminé sans produire de résultat.`n$($executorErrBuffer.ToString())"
}

Write-StreamLog "Données reçues du récepteur ($([Math]::Round($dataResult.Length / 1KB, 2)) Ko)."

# --- 9. Écriture du fichier de sortie ---
$outputDir = [System.IO.Path]::GetDirectoryName($OutputFile)
if ($outputDir -and -not (Test-Path -LiteralPath $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
}

# Le récepteur renvoie du texte (CSV / SRT / JSON) : écriture en UTF-8 sans BOM
[System.IO.File]::WriteAllText($OutputFile, $dataResult, $utf8NoBom)
Write-Host "Fichier texte enregistré : $OutputFile" -ForegroundColor Green

$sw.Stop()
$elapsed = [Math]::Round($sw.Elapsed.TotalSeconds, 2)
Write-StreamLog "Traitement terminé avec succès en $elapsed s. Fichier enregistré dans : $OutputFile" -Color Green