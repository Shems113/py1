[CmdletBinding()]
param (
    [Parameter(Mandatory = $true, HelpMessage = "Chemin du fichier vidéo d'entrée")]
    [ValidateNotNullOrEmpty()]
    [string]$InputFile,

    [Parameter(Mandatory = $true, HelpMessage = "Chemin du fichier vidéo réencodé de sortie")]
    [ValidateNotNullOrEmpty()]
    [string]$OutputFile,

    [Parameter(Mandatory = $true, HelpMessage = "Exécutable cible (ex: ssh.exe)")]
    [ValidateNotNullOrEmpty()]
    [string]$ExecutorExe,

    [Parameter(Mandatory = $false, HelpMessage = "Flags d'optimisation de l'exécutable")]
    [string[]]$ExecutorFlags = @(),

    [Parameter(Mandatory = $false, HelpMessage = "Chemin du script cible")]
    [string]$ScriptPath = "",

    [Parameter(Mandatory = $false, HelpMessage = "Arguments transmis à la commande distante")]
    [string[]]$AppArgs = @(),

    [Parameter(Mandatory = $false, HelpMessage = "IP cible pour mesure de latence ICMP")]
    [string]$PodIP = ""
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

$sw = [Diagnostics.Stopwatch]::StartNew()

# Nombre maximal de messages d'erreur affichés (source locale et pod) : au-delà, on ne garde que le total
$MaxLogLines = 25

function Write-StreamLog {
    param (
        [string]$Message,
        [ConsoleColor]$Color = [ConsoleColor]::Cyan
    )
    $timestamp = Get-Date -Format "HH:mm:ss.fff"
    Write-Host "[$timestamp] [VIDEO-STREAMING] $Message" -ForegroundColor $Color
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

Write-StreamLog "Démarrage du pipeline de streaming vidéo..." -Color DarkCyan
Write-StreamLog "Fichier source : $InputFile"
Write-StreamLog "Fichier cible  : $OutputFile"
Write-StreamLog "Exécutable     : $ExecutorExe"

if (-not (Test-Path -LiteralPath $InputFile)) { throw "Fichier d'entrée introuvable : $InputFile" }
if (-not (Get-Command -Name $ExecutorExe -ErrorAction SilentlyContinue)) { throw "Exécutable introuvable : $ExecutorExe" }

# --- 1. Assemblage de la commande ---
$FullArgs = @()
if ($ExecutorFlags) { $FullArgs += $ExecutorFlags }
if ($ScriptPath)    { $FullArgs += $ScriptPath }
if ($AppArgs)       { $FullArgs += $AppArgs }

Write-StreamLog "Commande exécutée : $ExecutorExe $($FullArgs -join ' ')" -Color Gray

# --- 2. Configuration du processus distant ---
$executorPsi = [Diagnostics.ProcessStartInfo]@{
    FileName               = $ExecutorExe
    UseShellExecute        = $false
    RedirectStandardInput  = $true
    RedirectStandardOutput = $true
    RedirectStandardError  = $true
    StandardErrorEncoding  = [System.Text.Encoding]::UTF8
}
foreach ($arg in $FullArgs) { $executorPsi.ArgumentList.Add($arg) }

$executorProc = [System.Diagnostics.Process]::new()
$executorProc.StartInfo = $executorPsi

# État partagé avec le gestionnaire d'événements : tampon complet + compteur, affichage limité
$remoteLog = [pscustomobject]@{
    Buffer = [System.Text.StringBuilder]::new()
    Lines  = 0
    Limit  = $MaxLogLines
}

$stderrEvent = Register-ObjectEvent -InputObject $executorProc -EventName "ErrorDataReceived" -Action {
    $line = $Event.SourceEventArgs.Data
    if ($line) {
        $state = $Event.MessageData
        [void]$state.Buffer.AppendLine($line)
        $state.Lines++
        if ($state.Lines -le $state.Limit) {
            Write-Host "  [REMOTE LOG] $line" -ForegroundColor DarkGray
        }
        elseif ($state.Lines -eq ($state.Limit + 1)) {
            Write-Host "  [REMOTE LOG] (messages suivants masqués, total affiché en fin de traitement)" -ForegroundColor DarkGray
        }
    }
} -MessageData $remoteLog

# --- 3. Réglage de la taille de buffer ---
$bufferSize = Get-StreamBufferSize -TargetHost $PodIP

# --- 4. Préparation du dossier de sortie ---
$outputDir = [System.IO.Path]::GetDirectoryName($OutputFile)
if ($outputDir -and -not (Test-Path -LiteralPath $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
}

# --- 5. Source locale : conversion en Matroska (stderr lu en continu, jamais laissé se remplir) ---
Write-StreamLog "Démarrage de la conversion Matroska source en local..." -Color DarkCyan
$ffmpegLocalPsi = [Diagnostics.ProcessStartInfo]@{
    FileName               = "ffmpeg"
    Arguments              = "-nostdin -loglevel error -i `"$InputFile`" -c copy -f matroska pipe:1"
    UseShellExecute        = $false
    RedirectStandardOutput = $true
    RedirectStandardError  = $true
    StandardErrorEncoding  = [System.Text.Encoding]::UTF8
}
$ffmpegLocalProc = [Diagnostics.Process]::Start($ffmpegLocalPsi)
$ffmpegErrTask   = $ffmpegLocalProc.StandardError.ReadToEndAsync()
$fileStream      = $ffmpegLocalProc.StandardOutput.BaseStream

# --- 6. Démarrage du processus récepteur ---
$null = $executorProc.Start()
$executorProc.BeginErrorReadLine()

$outStream         = [System.IO.File]::Create($OutputFile)
$executorInStream  = $executorProc.StandardInput.BaseStream
$executorOutStream = $executorProc.StandardOutput.BaseStream

$inputLength        = (Get-Item -LiteralPath $InputFile).Length
$sendBuffer         = [byte[]]::new($bufferSize)
$totalBytesSent     = 0L
$totalBytesReceived = 0L
$nextLog            = 50MB
$sendFailed         = $false
$completed          = $false

try {
    # --- 7. Réception en tâche de fond : vide le stdout de ssh en continu ---
    $receiveTask = $executorOutStream.CopyToAsync($outStream, $bufferSize)

    # Envoi (boucle principale)
    try {
        while (($bytesRead = $fileStream.Read($sendBuffer, 0, $sendBuffer.Length)) -gt 0) {
            $executorInStream.Write($sendBuffer, 0, $bytesRead)
            $totalBytesSent += $bytesRead

            if ($totalBytesSent -ge $nextLog) {
                $pct     = if ($inputLength -gt 0) { [Math]::Min(100, [Math]::Round(100 * $totalBytesSent / $inputLength)) } else { 0 }
                $speed   = ($totalBytesSent / 1MB) / [Math]::Max(1, $sw.Elapsed.TotalSeconds)
                $recvMB  = [Math]::Round(([System.IO.FileInfo]::new($OutputFile)).Length / 1MB)
                Write-StreamLog ("Envoi : {0} Mo ({1} %) | reçu : {2} Mo | {3:N1} Mo/s" -f [Math]::Round($totalBytesSent / 1MB), $pct, $recvMB, $speed) -Color Gray
                $nextLog += 50MB
            }
        }
        $executorInStream.Flush()
    }
    catch [System.IO.IOException] {
        # Le récepteur a fermé son entrée (crash distant, connexion coupée...)
        $sendFailed = $true
        Write-StreamLog "Le récepteur a fermé son entrée prématurément : $($_.Exception.Message)" -Color Red
    }
    finally {
        if ($fileStream)       { $fileStream.Close() }
        if ($executorInStream) { try { $executorInStream.Close() } catch { } }   # signale la fin de l'entrée
    }

    # Échec d'envoi : on arrête ffmpeg local (à la fin normale, il se termine seul)
    if ($sendFailed -and -not $ffmpegLocalProc.HasExited) {
        try { $ffmpegLocalProc.Kill() } catch { }
    }

    # --- 8. Attendre la fin de la réception ---
    try {
        $null = $receiveTask.GetAwaiter().GetResult()
    }
    catch {
        $sendFailed = $true
        Write-StreamLog "Erreur pendant la réception : $($_.Exception.Message)" -Color Red
    }

    $totalBytesReceived = $outStream.Length
    $completed = $true
}
finally {
    # Libère toujours le fichier de sortie (sinon le script appelant ne peut pas supprimer un fichier partiel)
    $outStream.Dispose()
    if (-not $completed -and $ffmpegLocalProc -and -not $ffmpegLocalProc.HasExited) {
        try { $ffmpegLocalProc.Kill() } catch { }
    }
}

$ffmpegLocalProc.WaitForExit()
$executorProc.WaitForExit()   # sans argument : garantit que tous les événements stderr sont traités
$ffmpegErr = $ffmpegErrTask.GetAwaiter().GetResult()

# Nettoyage des abonnements d'événements
Get-EventSubscriber | Where-Object { $_.SourceObject -eq $executorProc } | Unregister-Event -Force -ErrorAction SilentlyContinue
Remove-Job -Job $stderrEvent -Force -ErrorAction SilentlyContinue

# --- 9. Résumé des messages d'erreur (source locale et pod) ---
if ($ffmpegErr) {
    $localLines = @($ffmpegErr -split "`r?`n" | Where-Object { $_ })
    if ($localLines.Count -gt 0) {
        Write-StreamLog "Messages de FFmpeg local : $($localLines.Count) ligne(s), source probablement abîmée (premières lignes) :" -Color Yellow
        $localLines | Select-Object -First 5 | ForEach-Object { Write-Host "  [LOCAL] $_" -ForegroundColor DarkGray }
    }
}
if ($remoteLog.Lines -gt 0) {
    Write-StreamLog "Messages du pod : $($remoteLog.Lines) ligne(s) au total ($([Math]::Min($remoteLog.Lines, $MaxLogLines)) affichées)." -Color Yellow
}

# --- 10. Contrôles de fin de traitement ---
if ($executorProc.ExitCode -ne 0 -or $sendFailed) {
    Write-StreamLog "Échec du processus récepteur (ExitCode: $($executorProc.ExitCode))" -Color Red
    $tail = (($remoteLog.Buffer.ToString() -split "`r?`n") | Select-Object -Last 30) -join "`n"
    throw "L'exécutable s'est terminé avec une erreur (30 dernières lignes) :`n$tail"
}

if ($ffmpegLocalProc.ExitCode -ne 0) {
    Write-StreamLog "FFmpeg local a échoué (ExitCode: $($ffmpegLocalProc.ExitCode))" -Color Red
    throw "FFmpeg local a échoué (ExitCode: $($ffmpegLocalProc.ExitCode)) :`n$ffmpegErr"
}

if ($totalBytesReceived -eq 0) {
    Write-StreamLog "Aucune donnée reçue du pod." -Color Red
    throw "Le pod n'a renvoyé aucune donnée vidéo."
}

$sw.Stop()
$elapsed    = [Math]::Round($sw.Elapsed.TotalSeconds, 2)
$totalMBRec = [Math]::Round($totalBytesReceived / 1MB, 2)

Write-StreamLog "Traitement binaire réussi en ${elapsed}s." -Color Green
Write-StreamLog "Fichier vidéo final enregistré ($totalMBRec Mo) : $OutputFile" -Color Green