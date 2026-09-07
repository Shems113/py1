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
    Write=4
    Bookmark=5
    Transcribe=6

class ETorrentSource(str, Enum):
    ALL = "All"
    LIMETORRENTS = "LimeTorrents"
    THEPIRATEBAY = "ThePirateBay"
    TORZNAB = "Torznab"
    YTS = "YTS"