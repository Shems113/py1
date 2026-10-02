import argparse
from pathlib import Path

from Common.EEnums import EActionType, EMediaType
from Common.Helpers.System import IS_LINUX, IS_WINDOWS
from Media.CFile import CFile as File
from Media.CGmail import CGmail as Gmail
from Media.CMusic import CMusic as Music
from Media.CSubtitle import CSubtitle as Subtitle
from Media.CVideo import CVideo as Video
from Media.CWord import CWord as Word

# Valeurs par défaut
DEFAULT_MEDIA = EMediaType.Music
DEFAULT_ACTION = EActionType.Read

# Mappings des classes de médias et des actions associées
MEDIA_MAPPING: dict[EMediaType, type] = {
    EMediaType.File: File,
    EMediaType.Gmail: Gmail,
    EMediaType.Music: Music,
    EMediaType.Subtitle: Subtitle,
    EMediaType.Video: Video,
    EMediaType.Word: Word,
}

ACTION_MAPPING: dict[EActionType, str] = {
    EActionType.Read: "read",
    EActionType.ReadAndMove: "read",
    EActionType.ReadAndCopy: "read",
    EActionType.ReadAndCheck: "read",
    EActionType.Write: "write",
    EActionType.Bookmark: "bookmark",
    EActionType.Transcribe: "transcribe",
    EActionType.TranscribeAndCheck: "transcribe_and_check",
}


def process(
    media: File | Gmail | Music | Subtitle | Video | Word,
    action_name: str
    )-> None:
    """Initialise les logs et exécute la méthode d'action sur l'instance de média."""
    action = getattr(media, action_name, None)
    if not callable(action):
        # Auparavant ignoré sans message (ex. Music + Bookmark)
        media.logger.log_error(
            f"Action '{media.action_type.name}' non prise en charge pour {media.media_type.name}.")
        return
    media.init_log()
    action()


def main()-> None:
    """Point d'entrée principal analysant les arguments de la ligne de commande."""
    parser = argparse.ArgumentParser(description="Process some media files.")
    parser.add_argument(
        "--jsonPath",
        type=str,
        help="Path to the JSON settings file",
        default=str(
            Path("Files")
            / f"{('Windows' if IS_WINDOWS else ('Linux' if IS_LINUX else ''))}Settings.json"
        ),
    )
    parser.add_argument(
        "--mediaType",
        type=str,
        nargs="+",
        help="One or more media types",
        choices=[m.name for m in EMediaType],
        default=[DEFAULT_MEDIA.name],
    )
    parser.add_argument(
        "--actionType",
        type=str,
        nargs="+",
        help="One or more action types",
        choices=[a.name for a in EActionType],
        default=[DEFAULT_ACTION.name],
    )
    args = parser.parse_args()

    # Chaque action est appliquée à chaque média
    json_path = Path(args.jsonPath)
    media_types = [EMediaType[name] for name in args.mediaType]
    action_types = [EActionType[name] for name in args.actionType]

    for media_type in media_types:
        media_class = MEDIA_MAPPING[media_type]

        for action_type in action_types:
            action_name = ACTION_MAPPING[action_type]

            # Instanciation et exécution
            media_instance = media_class(json_path, action_type)
            process(media_instance, action_name)

if __name__ == "__main__":
    main()
