<#
.SYNOPSIS
    Convertit en Opus tout type de fichier audio ou video.

    - Fichier VIDEO (mp4, mkv...) : video en stream-copy, toutes les pistes audio
      reencodees en Opus. Sortie : <nom>_Opus.<ext d'origine>
    - Fichier AUDIO (mp3, m4a, flac, wav...) : premiere piste audio en Opus,
      tags conserves, pochette conservee et recompressee. Sortie : <nom>.opus

    Le type est detecte par ffprobe (presence d'une vraie piste video, une
    pochette integree ne compte pas comme video).

.PARAMETER Path
    Fichiers et/ou dossiers a convertir (-Recurse pour les sous-dossiers).
.PARAMETER CsvPath
    CSV (separateur ';', colonne "path"). Si ni -Path ni -CsvPath ne sont
    donnes, .\videoResult.csv est utilise s'il existe.
.PARAMETER AudioBitrate
    "Auto" (defaut) : debit choisi piste par piste selon la source.
    "96k", "128k"...: debit fixe pour toutes les pistes.
.PARAMETER Force
    Ecrase les sorties deja presentes (sinon elles sont ignorees).

.EXAMPLE
    .\Convert-ToOpus.ps1                                   # videoResult.csv
    .\Convert-ToOpus.ps1 -Path D:\Musique -Recurse
    .\Convert-ToOpus.ps1 -Path .\titre.mp3 -AudioBitrate 128k
    .\Convert-ToOpus.ps1 -CsvPath .\liste.csv -OutDir E:\Opus

.NOTES
    POCHETTE (fichiers audio) - jamais redimensionnee, seulement recompressee :
      - JPEG : optimisation SANS PERTE via jpegtran (Huffman optimise +
               progressif) si jpegtran est dans le PATH, sinon gardee telle quelle.
      - PNG/autre sans transparence : converti en JPEG haute qualite (4:4:4).
      - Avec transparence : PNG recompresse.
      - On garde toujours la version la plus petite.

    ATTENTION - bug connu ffmpeg 9.0 (gyan.dev full_build) :
    Le muxer MP4 de cette build droppe silencieusement le flux video AV1 en
    stream-copy. Les sorties video sont verifiees apres coup via ffprobe ;
    les fichiers suspects sont listes dans le rapport final.

    Prerequis : ffmpeg/ffprobe avec libopus et libsoxr (full_build gyan.dev).
    Optionnel : jpegtran (libjpeg-turbo) pour optimiser les pochettes JPEG.
#>

param(
    [string[]]$Path,
    [string]$CsvPath,
    [string]$OutDir,
    [ValidatePattern('^(Auto|\d+k)$')]
    [string]$AudioBitrate = "Auto",
    [switch]$Recurse,
    [switch]$Force
)

$ErrorActionPreference = "Continue"

$MediaExt = @(
    '.mp3', '.m4a', '.aac', '.flac', '.wav', '.ogg', '.oga', '.wma', '.ape', '.wv',
    '.mka', '.ac3', '.dts', '.aiff', '.aif',
    '.mp4', '.mkv', '.avi', '.mov', '.m4v', '.webm', '.ts', '.m2ts', '.wmv'
)
$LosslessCodecs = '^(flac|alac|pcm_.*|wavpack|ape|truehd|mlp|tta)$'

$Jpegtran = (Get-Command jpegtran -ErrorAction SilentlyContinue).Path
$Journal  = New-Object System.Collections.Generic.List[object]

# =====================================================================
#  Analyse ffprobe
# =====================================================================

function Get-MediaInfo {
    param([string]$File)

    $json = & ffprobe -v error `
        -show_entries "stream=index,codec_type,codec_name,profile,channels,channel_layout,sample_rate,bit_rate,width,height,pix_fmt:stream_disposition=attached_pic:stream_tags=BPS,BPS-eng,comment" `
        -of json -- $File 2>$null | Out-String

    if (-not $json.Trim()) { return $null }
    try   { $streams = @(($json | ConvertFrom-Json).streams) }
    catch { return $null }

    $isCover = { $_.codec_type -eq 'video' -and $_.disposition -and $_.disposition.attached_pic -eq 1 }

    [PSCustomObject]@{
        Audio  = @($streams | Where-Object { $_.codec_type -eq 'audio' })
        Video  = @($streams | Where-Object { $_.codec_type -eq 'video' -and -not (& $isCover) })
        Covers = @($streams | Where-Object $isCover)
    }
}

function Get-SourceBitrate {
    # bit_rate vaut souvent "N/A" en MKV : repli sur les tags BPS de mkvmerge.
    param($Stream)

    $candidates = @($Stream.bit_rate)
    if ($Stream.tags) { $candidates += $Stream.tags.BPS, $Stream.tags.'BPS-eng' }

    foreach ($v in $candidates) {
        $n = [int64]0
        if ($v -and [int64]::TryParse([string]$v, [ref]$n) -and $n -gt 0) { return $n }
    }
    return 0
}

# =====================================================================
#  Choix de la configuration Opus pour une piste
# =====================================================================

function Get-OpusConfig {
    param($Stream, [string]$Override = "Auto")

    $ch       = [int]$Stream.channels
    $layout   = [string]$Stream.channel_layout
    $src      = Get-SourceBitrate $Stream
    $isHE     = ([string]$Stream.profile) -like "HE-AAC*"
    $lossless = ([string]$Stream.codec_name) -match $LosslessCodecs

    # Filtres : remap des layouts refuses par libopus + resample 48 kHz
    $filters = @()
    if     ($ch -eq 6 -and $layout -ne "5.1") { $filters += "channelmap=channel_layout=5.1" }
    elseif ($ch -eq 8 -and $layout -ne "7.1") { $filters += "channelmap=channel_layout=7.1" }
    $filters += "aresample=48000:resampler=soxr"

    if ($Override -ne "Auto") {
        $bitrate = $Override
        $reason  = "fixe"
    }
    else {
        if ($ch -le 1) {
            if ($lossless) { $kbps = 64; $reason = "mono lossless" }
            else           { $kbps = 48; $reason = "mono" }
        }
        elseif ($ch -eq 2) {
            if     ($lossless)                       { $kbps = 128; $reason = "stereo lossless" }
            elseif ($isHE)                           { $kbps = 64;  $reason = "stereo HE-AAC" }
            elseif ($src -gt 0 -and $src -lt 100000) { $kbps = 80;  $reason = "stereo < 100k" }
            else                                     { $kbps = 96;  $reason = "stereo >= 100k" }
        }
        elseif ($ch -le 6) {
            if ($src -gt 0 -and $src -le 224001) { $kbps = 192; $reason = "5.1 <= 224k" }
            else                                 { $kbps = 256; $reason = "5.1 > 224k" }
        }
        else {
            $kbps = 256; $reason = "7.1"
        }

        # Plafond : inutile de depasser le debit source (arrondi au multiple de 8k)
        if ($src -gt 0 -and -not $lossless) {
            $cap = [int]([Math]::Ceiling($src / 8000.0) * 8)
            if ($cap -lt $kbps) {
                $kbps    = [Math]::Max(32, $cap)
                $reason += ", plafonne a la source"
            }
        }
        if ($src -eq 0 -and -not $lossless) { $reason += ", debit source inconnu" }

        $bitrate = "$($kbps)k"
    }

    [PSCustomObject]@{
        Bitrate   = $bitrate
        Filter    = ($filters -join ",")
        Reason    = $reason
        SourceBps = $src
    }
}

function Write-StreamConfig {
    param([string]$File, [int]$Index, $Stream, $Cfg)

    $srcTxt = if ($Cfg.SourceBps -gt 0) { "$([Math]::Round($Cfg.SourceBps / 1000))k" } else { "?" }
    Write-Host ("  a:{0} {1} {2} {3}ch {4} {5}Hz @ {6} -> Opus {7} ({8})" -f `
        $Index, $Stream.codec_name, $Stream.profile, $Stream.channels, $Stream.channel_layout,
        $Stream.sample_rate, $srcTxt, $Cfg.Bitrate, $Cfg.Reason) -ForegroundColor DarkCyan

    $script:Journal.Add([PSCustomObject]@{
        Fichier    = $File
        Piste      = $Index
        Codec      = $Stream.codec_name
        Profil     = $Stream.profile
        Canaux     = $Stream.channels
        Layout     = $Stream.channel_layout
        SampleRate = $Stream.sample_rate
        DebitSrc   = $Cfg.SourceBps
        DebitOpus  = $Cfg.Bitrate
        Raison     = $Cfg.Reason
    })
}

function New-Result {
    param([string]$Source, [string]$Sortie, [string]$Statut, [string]$Detail = "")
    [PSCustomObject]@{ Source = $Source; Sortie = $Sortie; Statut = $Statut; Detail = $Detail }
}

# =====================================================================
#  Pochette : extraction, recompression, bloc FLAC picture
# =====================================================================

function Compress-Cover {
    param([string]$Src, $Cover, [string]$WorkDir)

    switch ($Cover.codec_name) {
        'mjpeg' { $ext = 'jpg'; $codecArgs = @('-c', 'copy') }
        'png'   { $ext = 'png'; $codecArgs = @('-c', 'copy') }
        default { $ext = 'png'; $codecArgs = @('-c:v', 'png') }   # bmp, webp... -> png
    }

    $orig = Join-Path $WorkDir "cover_orig.$ext"
    & ffmpeg -v error -y -i $Src -map "0:$($Cover.index)" @codecArgs -frames:v 1 -update 1 $orig 2>$null | Out-Null
    if (-not (Test-Path -LiteralPath $orig)) { return $null }

    $isJpeg   = $ext -eq 'jpg'
    $hasAlpha = ([string]$Cover.pix_fmt) -match '^(ya|yuva|pal8)|rgba|bgra|argb|abgr'
    $cand     = $null

    if ($isJpeg) {
        if ($Jpegtran) {
            $cand = Join-Path $WorkDir 'cover_opt.jpg'; $candMime = 'image/jpeg'
            & $Jpegtran -copy none -optimize -progressive -outfile $cand $orig 2>$null | Out-Null
        }
    }
    elseif ($hasAlpha) {
        $cand = Join-Path $WorkDir 'cover_opt.png'; $candMime = 'image/png'
        & ffmpeg -v error -y -i $orig -c:v png -pred mixed -compression_level 9 -frames:v 1 -update 1 $cand 2>$null | Out-Null
    }
    else {
        $cand = Join-Path $WorkDir 'cover_opt.jpg'; $candMime = 'image/jpeg'
        & ffmpeg -v error -y -i $orig -c:v mjpeg -q:v 2 -pix_fmt yuvj444p -frames:v 1 -update 1 $cand 2>$null | Out-Null
    }

    $origSize = (Get-Item -LiteralPath $orig).Length
    $best = $orig
    $mime = if ($isJpeg) { 'image/jpeg' } else { 'image/png' }

    if ($cand -and (Test-Path -LiteralPath $cand)) {
        $candSize = (Get-Item -LiteralPath $cand).Length
        if ($candSize -gt 0 -and $candSize -lt $origSize) { $best = $cand; $mime = $candMime }
    }

    [PSCustomObject]@{
        Path     = $best
        Mime     = $mime
        Width    = [int]$Cover.width
        Height   = [int]$Cover.height
        OrigSize = $origSize
        NewSize  = (Get-Item -LiteralPath $best).Length
    }
}

function New-FlacPictureB64 {
    # Bloc METADATA_BLOCK_PICTURE (format FLAC, entiers big-endian), en base64
    param([byte[]]$Data, [string]$Mime, [int]$Width, [int]$Height)

    $ms = New-Object System.IO.MemoryStream
    $be = { param([long]$v) $b = [BitConverter]::GetBytes([uint32]$v); [Array]::Reverse($b); $ms.Write($b, 0, 4) }

    $mimeB = [Text.Encoding]::ASCII.GetBytes($Mime)
    $descB = [Text.Encoding]::UTF8.GetBytes("Cover (front)")

    & $be 3                                   # type : couverture avant
    & $be $mimeB.Length; $ms.Write($mimeB, 0, $mimeB.Length)
    & $be $descB.Length; $ms.Write($descB, 0, $descB.Length)
    & $be $Width; & $be $Height
    & $be 24                                  # profondeur
    & $be 0                                   # couleurs (0 = non indexe)
    & $be $Data.Length; $ms.Write($Data, 0, $Data.Length)

    [Convert]::ToBase64String($ms.ToArray())
}

# =====================================================================
#  Conversion d'un fichier AUDIO -> .opus (tags + pochette)
# =====================================================================

function Convert-AudioFile {
    param([string]$In, [string]$Out, $Info)

    $a   = $Info.Audio[0]
    $cfg = Get-OpusConfig -Stream $a -Override $AudioBitrate
    Write-StreamConfig -File $In -Index 0 -Stream $a -Cfg $cfg

    $work = Join-Path ([IO.Path]::GetTempPath()) ("opus_" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null

    try {
        $extraIn  = @()
        $metaSrc  = '0'
        $coverMsg = ''

        $cover = $Info.Covers | Where-Object { $_.tags -and $_.tags.comment -match 'front' } | Select-Object -First 1
        if (-not $cover) { $cover = $Info.Covers | Select-Object -First 1 }

        if ($cover) {
            $c = Compress-Cover -Src $In -Cover $cover -WorkDir $work
            $meta = Join-Path $work 'meta.txt'
            if ($c) {
                & ffmpeg -v error -y -i $In -f ffmetadata $meta 2>$null | Out-Null
            }
            if ($c -and (Test-Path -LiteralPath $meta)) {
                $b64 = New-FlacPictureB64 -Data ([IO.File]::ReadAllBytes($c.Path)) `
                        -Mime $c.Mime -Width $c.Width -Height $c.Height

                # Insertion juste apres l'en-tete ;FFMETADATA1 (section globale,
                # avant d'eventuels [CHAPTER]). '=' doit etre echappe en ffmetadata.
                $txt = [IO.File]::ReadAllText($meta)
                $nl  = $txt.IndexOf("`n")
                $txt = $txt.Insert($nl + 1, "METADATA_BLOCK_PICTURE=" + $b64.Replace('=', '\=') + "`n")
                [IO.File]::WriteAllText($meta, $txt)   # UTF-8 sans BOM

                $extraIn  = @('-f', 'ffmetadata', '-i', $meta)
                $metaSrc  = '1'
                $coverMsg = "pochette $($c.Width)x$($c.Height) : $([int]($c.OrigSize/1KB)) Ko -> $([int]($c.NewSize/1KB)) Ko"
                Write-Host "  $coverMsg" -ForegroundColor DarkCyan
            }
            else {
                Write-Warning "Pochette non extraite, conversion sans pochette"
            }
        }

        $ffArgs = @('-hide_banner', '-v', 'error', '-y', '-i', $In) + $extraIn + @(
            '-map', '0:a:0', '-map_metadata', $metaSrc,
            '-c:a', 'libopus', '-vbr', 'on', '-b:a', $cfg.Bitrate, '-af', $cfg.Filter,
            $Out
        )
        & ffmpeg @ffArgs 2>&1 | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }

        if (-not (Test-Path -LiteralPath $Out)) {
            return New-Result $In $Out 'ERREUR' 'fichier de sortie absent'
        }
        $codec = & ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 -- $Out 2>$null
        if ($codec -notmatch 'opus') {
            return New-Result $In $Out 'ERREUR' 'piste Opus absente dans la sortie'
        }
        return New-Result $In $Out 'OK' $coverMsg
    }
    finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# =====================================================================
#  Conversion d'un fichier VIDEO (video copiee, audio -> Opus)
# =====================================================================

function Convert-VideoFile {
    param([string]$In, [string]$Out, $Info)

    $audioArgs = @()
    for ($i = 0; $i -lt $Info.Audio.Count; $i++) {
        $s   = $Info.Audio[$i]
        $cfg = Get-OpusConfig -Stream $s -Override $AudioBitrate
        # -map 0:a conserve l'ordre : piste audio i de la source = a:i de la sortie
        $audioArgs += "-b:a:$i", $cfg.Bitrate, "-filter:a:$i", $cfg.Filter
        Write-StreamConfig -File $In -Index $i -Stream $s -Cfg $cfg
    }

    $ffArgs = @(
        '-hide_banner', '-i', $In,
        '-map', '0:v', '-map', '0:a',
        '-c:v', 'copy',
        '-c:a', 'libopus', '-vbr', 'on'
    ) + $audioArgs + @(
        '-map_metadata', '0',
        '-y', $Out
    )
    & ffmpeg @ffArgs 2>&1 |
        Select-String -Pattern "error|Error" | ForEach-Object { Write-Host "  $($_.Line)" -ForegroundColor Red }

    if (-not (Test-Path -LiteralPath $Out)) {
        return New-Result $In $Out 'ERREUR' 'fichier de sortie absent'
    }

    # Verification anti-bug AV1/MP4 : piste video presente et non vide ?
    $probe = & ffprobe -v error -select_streams v:0 -show_entries stream=codec_type -of csv=p=0 -- $Out 2>&1
    if (-not $probe -or $probe -notmatch "video") {
        return New-Result $In $Out 'SUSPECT' 'piste video absente'
    }

    $ratio = (Get-Item -LiteralPath $Out).Length / [Math]::Max((Get-Item -LiteralPath $In).Length, 1)
    if ($ratio -lt 0.15) {
        return New-Result $In $Out 'SUSPECT' "taille anormalement faible (ratio $([Math]::Round($ratio,3)))"
    }

    return New-Result $In $Out 'OK' "$([Math]::Round((Get-Item -LiteralPath $Out).Length/1MB,1)) MB"
}

# =====================================================================
#  Collecte des fichiers a traiter
# =====================================================================

if (-not $Path -and -not $CsvPath) {
    if (Test-Path ".\videoResult.csv") { $CsvPath = ".\videoResult.csv" }
    else {
        Write-Error "Rien a traiter : utiliser -Path <fichiers|dossiers> ou -CsvPath <fichier.csv>"
        exit 1
    }
}

$inputs = New-Object System.Collections.Generic.List[string]

if ($CsvPath) {
    if (-not (Test-Path $CsvPath)) { Write-Error "CSV introuvable : $CsvPath"; exit 1 }
    # Le CSV a un BOM UTF-8 et des ';' comme separateur
    Import-Csv -Path $CsvPath -Delimiter ';' -Encoding UTF8 | ForEach-Object { $inputs.Add($_.path) }
}

foreach ($p in $Path) {
    if (Test-Path -LiteralPath $p -PathType Container) {
        Get-ChildItem -LiteralPath $p -File -Recurse:$Recurse |
            Where-Object { $MediaExt -contains $_.Extension.ToLower() -and $_.BaseName -notmatch '_Opus$' } |
            Sort-Object FullName |
            ForEach-Object { $inputs.Add($_.FullName) }
    }
    else {
        $inputs.Add($p)   # fichier (ou introuvable : signale dans la boucle)
    }
}

if ($OutDir -and -not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
}

# =====================================================================
#  Traitement
# =====================================================================

$total   = $inputs.Count
$results = New-Object System.Collections.Generic.List[object]
$n       = 0

Write-Host "=== $total fichiers a traiter (AudioBitrate = $AudioBitrate) ===" -ForegroundColor Cyan
if (-not $Jpegtran) {
    Write-Host "(jpegtran absent du PATH : les pochettes JPEG seront gardees telles quelles)" -ForegroundColor DarkGray
}

foreach ($in in $inputs) {
    $n++
    Write-Host "`n--- [$n/$total] $([IO.Path]::GetFileName($in)) ---" -ForegroundColor Yellow

    if (-not (Test-Path -LiteralPath $in -PathType Leaf)) {
        Write-Warning "Introuvable, ignore : $in"
        $results.Add((New-Result $in '' 'IGNORE' 'introuvable')); continue
    }

    $info = Get-MediaInfo $in
    if (-not $info -or $info.Audio.Count -eq 0) {
        Write-Warning "Aucune piste audio, ignore"
        $results.Add((New-Result $in '' 'IGNORE' 'aucune piste audio')); continue
    }
    if (@($info.Audio | Where-Object { $_.codec_name -ne 'opus' }).Count -eq 0) {
        Write-Host "  Deja en Opus, ignore" -ForegroundColor DarkYellow
        $results.Add((New-Result $in '' 'IGNORE' 'deja en Opus')); continue
    }

    $isVideo   = $info.Video.Count -gt 0
    $dir       = [IO.Path]::GetDirectoryName($in)
    $name      = [IO.Path]::GetFileNameWithoutExtension($in)
    $ext       = [IO.Path]::GetExtension($in)
    $targetDir = if ($OutDir) { $OutDir } else { $dir }
    $out       = if ($isVideo) { Join-Path $targetDir "$name`_Opus$ext" } else { Join-Path $targetDir "$name.opus" }

    if ((Test-Path -LiteralPath $out) -and -not $Force) {
        Write-Host "  Sortie deja presente, ignore (-Force pour ecraser) : $out" -ForegroundColor DarkYellow
        $results.Add((New-Result $in $out 'IGNORE' 'sortie deja presente')); continue
    }

    Write-Host "  Mode : $(if ($isVideo) { 'video (audio -> Opus, video copiee)' } else { 'audio -> .opus' })" -ForegroundColor DarkGray

    try {
        $r = if ($isVideo) { Convert-VideoFile -In $in -Out $out -Info $info }
             else          { Convert-AudioFile -In $in -Out $out -Info $info }
    }
    catch {
        $r = New-Result $in $out 'ERREUR' $_.Exception.Message
    }
    $results.Add($r)

    $color = switch ($r.Statut) { 'OK' { 'Green' } 'SUSPECT' { 'Red' } default { 'Red' } }
    Write-Host "  $($r.Statut) : $out $(if ($r.Detail) { "($($r.Detail))" })" -ForegroundColor $color
}

# =====================================================================
#  Rapport
# =====================================================================

$count = { param($s) @($results | Where-Object { $_.Statut -eq $s }).Count }

Write-Host "`n=== Rapport final ===" -ForegroundColor Cyan
Write-Host "Reussis               : $(& $count 'OK') / $total" -ForegroundColor Green
Write-Host "Ignores               : $(& $count 'IGNORE')"       -ForegroundColor DarkYellow
Write-Host "Suspects (a verifier) : $(& $count 'SUSPECT')"      -ForegroundColor Red
Write-Host "Erreurs               : $(& $count 'ERREUR')"       -ForegroundColor Red

if ($Journal.Count -gt 0) {
    $Journal | Export-Csv -Path ".\opus_config.csv" -NoTypeInformation -Delimiter ';' -Encoding UTF8
    Write-Host "Configuration Opus par piste exportee dans : .\opus_config.csv" -ForegroundColor Cyan
}

$problems = @($results | Where-Object { $_.Statut -in 'SUSPECT', 'ERREUR' })
if ($problems.Count -gt 0) {
    Write-Host "`n--- Fichiers a verifier ---" -ForegroundColor Red
    $problems | Format-Table Statut, Source, Detail -AutoSize
    $problems | Export-Csv -Path ".\suspects_opus.csv" -NoTypeInformation -Delimiter ';' -Encoding UTF8
    Write-Host "Liste exportee dans : .\suspects_opus.csv" -ForegroundColor Red
    if (& $count 'SUSPECT') {
        Write-Host "Pour les SUSPECT (bug AV1/MP4), utiliser le pipeline mkvextract + MP4Box (voir conversation precedente)." -ForegroundColor Red
    }
}