from enum import Enum


class EMediaType(Enum):
    File=1
    Gmail=2
    Music=3
    Subtitle=4
    Video=5
    Word=6

class EActionType(Enum):
    Read=1
    ReadAndMove=2
    ReadAndCopy=3
    ReadAndCheck=4
    Write=5
    Bookmark=6
    Transcribe=7
    TranscribeAndCheck=8

class ETorrentSource(str, Enum):
    ALL = "All"
    LIMETORRENTS = "LimeTorrents"
    THEPIRATEBAY = "ThePirateBay"
    TORZNAB = "Torznab"
    YTS = "YTS"
