import argparse
from pathlib import Path
import platform
from typing import Dict, Type, Union

from Common.EEnums import EActionType, EMediaType
from Media.CFile import CFile as File
from Media.CGmail import CGmail as Gmail
from Media.CMusic import CMusic as Music
from Media.CSubtitle import CSubtitle as Subtitle
from Media.CVideo import CVideo as Video
from Media.CWord import CWord as Word

# Valeurs par défaut
DEFAULT_MEDIA = EMediaType.Subtitle
DEFAULT_ACTION = EActionType.Read

# Mappings des classes de médias et des actions associées
MEDIA_MAPPING: Dict[EMediaType, Type] = {
    EMediaType.File: File,
    EMediaType.Gmail: Gmail,
    EMediaType.Music: Music,
    EMediaType.Subtitle: Subtitle,
    EMediaType.Video: Video,
    EMediaType.Word: Word,
}

ACTION_MAPPING: Dict[EActionType, str] = {
    EActionType.Read: "read",
    EActionType.ReadAndMove: "read",
    EActionType.ReadAndCopy: "read",
    EActionType.Write: "write",
    EActionType.Bookmark: "bookmark",
    EActionType.Transcribe: "transcribe",
    EActionType.TranscribeAndCheck: "transcribeAndCheck",
}

def process(
    media: Union[File, Gmail, Music, Video, Word],
    action_name: str
    ) -> None:
    """Initialise les logs et exécute la méthode d'action sur l'instance de média."""
    if hasattr(media, action_name) and callable(getattr(media, action_name)):
        media.init_log()
        getattr(media, action_name)()


def main() -> None:
    """Point d'entrée principal analysant les arguments de la ligne de commande."""
    parser = argparse.ArgumentParser(description="Process some media files.")
    parser.add_argument(
        "--jsonPath",
        type=str,
        help="Path to the JSON settings file",
        default=str(Path("Files") / f"{('Windows' if platform.system() == 'Windows' else 'Linux')}Settings.json"),
    )
    parser.add_argument(
        "--mediaType",
        type=str,
        help="Type of media",
        default=DEFAULT_MEDIA.name,
    )
    parser.add_argument(
        "--actionType",
        type=str,
        help="Type of action",
        default=DEFAULT_ACTION.name,
    )
    parser.add_argument(
        "--isStreaming",
        type=bool,
        help="Flag indicating if the data source is streamed",
        default=False
    )

    args = parser.parse_args()

    # Récupération de l'action et du type de média depuis les énumérations
    action_type = getattr(EActionType, args.actionType)
    media_type = getattr(EMediaType, args.mediaType)

    # Instanciation et exécution
    media_class = MEDIA_MAPPING[media_type]
    media_instance = media_class(Path(args.jsonPath), action_type, bool(args.isStreaming))
    action_name = ACTION_MAPPING[action_type]
    process(media_instance, action_name)


if __name__ == "__main__":
    main()