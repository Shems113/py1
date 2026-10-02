"""
CMusicAutotag — identifie un fichier audio et renvoie ses tags, sans jamais
modifier le fichier.

1. Empreinte acoustique (fpcalc) -> AcoustID -> MusicBrainz (+ genre et année)
2. Sinon, recherche par nom sur Deezer (tags existants ou chemin
   …/ARTISTE/ALBUM/Titre.mp3), validée par la durée du morceau
3. Liens de contrôle (MusicBrainz, Deezer, Apple Music, YouTube) et pochettes HD

    tagger = CMusicAutotag()  # clé AcoustID et URL : Files/ExternalApiSettings.json
    result, is_ok = tagger.get_tags(r"D:\\Musique\\piste.mp3")

Prérequis : fpcalc (variable FPCALC, constante _FPCALC ou PATH) et, dans la section
« AcoustId » de Files/ExternalApiSettings.json, une clé d'APPLICATION AcoustID
(https://acoustid.org/new-application).
Thread-safe : une même instance peut être partagée par un pool de threads.
"""
from __future__ import annotations

import difflib
import functools
import json
import os
import re
import shutil
import subprocess
import unicodedata
import urllib.parse
from typing import Any, Callable

from Common.Helpers.ApiSettings import get_api_settings
from Common.Helpers.Audio import extract_mono_16khz_pcm_stream, read_common_tags
from Common.Helpers.Cache import SingleFlightCache
from Common.Helpers.Web import RateLimiter, fetch_json

TAG_KEYS = (
    "title", "artist", "album", "album_artist", "track_number", "track_total",
    "disc_number", "disc_total", "year", "release_date", "genre", "release_type",
    "media_format", "country", "acoustid_id", "musicbrainz_recording_id",
    "musicbrainz_release_id", "musicbrainz_releasegroup_id", "isrc",
    "deezer_track_id", "deezer_album_id", "links", "covers",
)


@functools.lru_cache(maxsize=16384)
def _normalize(s: str) -> str:
    """Forme comparable d'un nom : sans accents, casse, (Remastered), [Deluxe], feat…"""
    s = s.lower().replace("œ", "oe").replace("æ", "ae")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"\(.*?\)|\[.*?\]|\b(the|feat|ft)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _similar(a: str | None, b: str | None) -> bool:
    """Noms équivalents : identiques, l'un contenu dans l'autre, ou orthographe proche
    (Cheb Khaled / Khaled, El Atlal / Al Atlal)."""
    na, nb = _normalize(a or ""), _normalize(b or "")
    if not (na and nb):
        return False
    return na == nb or na in nb or nb in na or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


def _deezer_quota_exceeded(payload: Any) -> bool:
    return isinstance(payload, dict) and (payload.get("error") or {}).get("code") == 4


def _https(url: str | None) -> str | None:
    return url and url.replace("http://", "https://", 1)


def _artists(items: list[dict] | None) -> str | None:
    return "".join(a.get("name", "") + a.get("joinphrase", "") for a in items or []) or None


class CMusicAutotag:
    _FPCALC = r"C:\Users\SEDOUD\Downloads\fpcalc\fpcalc.exe"
    _TIMEOUT = 20
    _DURATION_TOLERANCE = 3  # secondes (ou 1,5 % pour les morceaux longs)

    # Intervalle minimal entre deux appels d'un même service (limites des API)
    _INTERVALS = {"acoustid": 1 / 3, "musicbrainz": 1.0, "caa": 0.2, "deezer": 0.2, "itunes": 3.0}

    # Choix de l'édition : album studio > EP > single, compilations et lives pénalisés,
    # numérotation CD préférée à celle des vinyles / cassettes
    _PRIMARY = {"Album": 3, "EP": 1, "Single": 0}
    _SECONDARY = {"Compilation": -3, "Live": -2, "DJ-mix": -2, "Soundtrack": -1, "Remix": -1}
    _FORMATS = {"CD": 3, "Enhanced CD": 2, "HDCD": 2, "Digital Media": 2, "SACD": 1, "Hybrid SACD": 1}

    def __init__(self, api_key: str | None = None, contact: str | None = None, min_score: float = 0.80,
                 fetch_links: bool = True, match_by_name: bool = True):
        """api_key / contact : par défaut, ceux de la section « AcoustId » des paramètres des API."""
        candidates = (os.environ.get("FPCALC"), self._FPCALC, shutil.which("fpcalc"))
        self._fpcalc = next((p for p in candidates if p and os.path.isfile(p)), None)
        if not self._fpcalc:
            raise FileNotFoundError("fpcalc introuvable (variable FPCALC, _FPCALC ou PATH)")
        acoustid = get_api_settings("AcoustId")
        apple = get_api_settings("AppleMusic")
        deezer = get_api_settings("Deezer")
        self._api_key = api_key or acoustid["ApiKey"]
        contact = contact or acoustid.get("Contact", "")
        self._acoustid_url = acoustid["LookupUrl"]
        self._acoustid_track_url = acoustid["TrackUrl"]
        self._mb_url = get_api_settings("MusicBrainz")["BaseUrl"]
        self._caa_url = get_api_settings("CoverArtArchive")["BaseUrl"]
        self._deezer_api_url = deezer["ApiUrl"]
        self._deezer_web_url = deezer["WebUrl"]
        self._itunes_url = f"{apple['SearchUrl']}?entity=album&limit=25&country={apple.get('Country', 'FR')}&term="
        self._youtube_search_url = get_api_settings("YouTube")["SearchUrl"]
        self._min_score = min_score
        self._fetch_links = fetch_links
        self._match_by_name = match_by_name
        self._headers = {"User-Agent": f"CMusicAutotag/1.0 ( {contact or 'no-contact'} )",
                         "Accept": "application/json"}
        self._limiters = {service: RateLimiter(i) for service, i in self._INTERVALS.items()}
        self._cache = SingleFlightCache()

    # ------------------------------------------------------------------ API
    def get_tags(self, path: str, release_id: str | None = None) -> tuple[dict[str, Any], bool]:
        """Renvoie (résultat, is_ok) ; ne lève jamais d'exception.

        résultat : path, status (ok / ok_by_name / not_found / error), score, tags,
        match, error, warnings. is_ok vaut False seulement en cas d'erreur.
        release_id : édition MusicBrainz à privilégier si le morceau y figure."""
        result: dict[str, Any] = {"path": str(path), "status": "not_found", "score": None,
                                  "tags": None, "match": None, "error": None, "warnings": []}
        warnings = result["warnings"]
        try:
            duration, fingerprint = self._fingerprint(str(path), warnings)
            best = self._pick(self._lookup(fingerprint, duration), release_id)
            deezer = None
            if best:
                result["score"], tags = best
                result["status"] = "ok"
                result["match"] = {"method": "fingerprint", "duration_file": duration}
                if release_id and tags["musicbrainz_release_id"] != release_id:
                    warnings.append(f"morceau absent de l'édition {release_id}, meilleure édition retenue")
                self._add_release_group_info(tags, warnings)
            elif self._match_by_name:
                found = self._match_name(str(path), duration, warnings)
                if not found:
                    return result, True
                tags, deezer, result["match"] = found
                result["status"] = "ok_by_name"
            else:
                return result, True
            result["tags"] = tags
            if self._fetch_links:
                self._add_links_and_covers(tags, warnings, deezer)
            return result, True
        except Exception as e:  # noqa: BLE001
            result["status"] = "error"
            result["error"] = f"{type(e).__name__}: {e}"
            return result, False

    # ------------------------------------------------------ Empreinte
    def _fingerprint(self, path: str, warnings: list[str]) -> tuple[int, str]:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        try:
            return self._run_fpcalc([path])
        except RuntimeError:
            # fpcalc abandonne à la première trame illisible ; ffmpeg (helper Audio) la
            # saute : on lui fait décoder l'audio en PCM mono 16 kHz, passé à fpcalc sur stdin
            pcm = extract_mono_16khz_pcm_stream(path)
            if not pcm:
                raise
            _, fingerprint = self._run_fpcalc(
                ["-format", "s16le", "-rate", "16000", "-channels", "1", "-"], stdin=pcm)
            warnings.append("audio illisible par fpcalc : empreinte calculée sur l'audio décodé par ffmpeg")
            return round(len(pcm) / 32_000), fingerprint  # 16 000 échantillons/s x 2 octets

    def _run_fpcalc(self, args: list[str], stdin: bytes | None = None) -> tuple[int, str]:
        proc = subprocess.run([self._fpcalc, "-json", *args], input=stdin,
                              capture_output=True, timeout=180, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"fpcalc : {proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
        data = json.loads(proc.stdout)
        return int(data["duration"]), data["fingerprint"]

    # ------------------------------------------- AcoustID / MusicBrainz
    def _lookup(self, fingerprint: str, duration: int) -> list[dict]:
        body = {
            "client": self._api_key, "format": "json", "duration": duration,
            "fingerprint": fingerprint, "meta": "recordings releasegroups releases tracks",
        }
        resp = self._http(self._acoustid_url, "acoustid", body)
        if resp.get("status") != "ok":
            raise RuntimeError(f"AcoustID : {resp.get('error')}")
        return resp.get("results", [])

    def _pick(self, results: list[dict], release_id: str | None) -> tuple[float, dict] | None:
        """Meilleure édition parmi toutes celles proposées par AcoustID."""
        best_key, best = None, None
        for res in results:
            score = float(res.get("score", 0))
            if score < self._min_score:
                continue
            for rec in res.get("recordings") or []:
                if not rec.get("title"):
                    continue
                for rg in rec.get("releasegroups") or [{}]:
                    type_bonus = self._PRIMARY.get(rg.get("type"), -1) + sum(
                        self._SECONDARY.get(t, 0) for t in rg.get("secondarytypes", []))
                    for rel in rg.get("releases") or [{}]:
                        formats = [m.get("format") for m in rel.get("mediums") or [] if m.get("format")]
                        key = (
                            bool(release_id) and rel.get("id") == release_id,  # édition imposée
                            round(score, 1),
                            type_bonus if rel else -99,                        # sans album : en dernier
                            min((self._FORMATS.get(f, -2) for f in formats), default=0),
                            -((rel.get("date") or {}).get("year") or 9999),    # la plus ancienne
                            -(rel.get("medium_count") or 99),                  # le moins de disques
                        )
                        if best_key is None or key > best_key:
                            best_key, best = key, (score, self._tags_from_acoustid(res, rec, rg, rel))
        return best

    @staticmethod
    def _tags_from_acoustid(res: dict, rec: dict, rg: dict, rel: dict) -> dict:
        medium = next((m for m in rel.get("mediums") or [] if m.get("tracks")), {})
        track = (medium.get("tracks") or [{}])[0]
        date = rel.get("date") or {}
        return dict.fromkeys(TAG_KEYS) | {
            "title": track.get("title") or rec.get("title"),
            "artist": _artists(track.get("artists")) or _artists(rec.get("artists")),
            "album": rel.get("title") or rg.get("title"),
            "album_artist": _artists(rg.get("artists")),
            "track_number": track.get("position"),
            "track_total": medium.get("track_count"),
            "disc_number": medium.get("position"),
            "disc_total": rel.get("medium_count"),
            "year": date.get("year"),  # remplacée ensuite par l'année originale de l'album
            "release_date": "-".join(f"{date[k]:02d}" for k in ("year", "month", "day") if date.get(k)) or None,
            "release_type": " + ".join([rg["type"], *rg.get("secondarytypes", [])]) if rg.get("type") else None,
            "media_format": " + ".join(m.get("format") or "?" for m in rel.get("mediums") or []) or None,
            "country": rel.get("country"),
            "acoustid_id": res.get("id"),
            "musicbrainz_recording_id": rec.get("id"),
            "musicbrainz_release_id": rel.get("id"),
            "musicbrainz_releasegroup_id": rg.get("id"),
        }

    def _add_release_group_info(self, tags: dict, warnings: list[str]) -> None:
        """Année de sortie originale de l'album et genre (1 requête MusicBrainz par album)."""
        rg_id = tags["musicbrainz_releasegroup_id"]
        if not rg_id:
            return
        try:
            rg = self._cache.get(("rg", rg_id), lambda: self._http(
                f"{self._mb_url}/ws/2/release-group/{rg_id}?inc=genres&fmt=json", "musicbrainz"))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"musicbrainz : {e}")
            return
        year = (rg.get("first-release-date") or "")[:4]
        if year.isdigit():
            tags["year"] = int(year)
        if genres := rg.get("genres"):
            tags["genre"] = max(genres, key=lambda g: g.get("count", 0))["name"].title()

    # ---------------------------------------- Secours : recherche par nom
    def _match_name(self, path: str, duration: int, warnings: list[str]) -> tuple[dict, dict, dict] | None:
        """Morceau Deezer dont le titre et l'artiste correspondent ET dont la durée
        est quasi identique à celle du fichier."""
        tolerance = max(self._DURATION_TOLERANCE, 0.015 * duration)
        best, best_key = None, None
        for artist, title, album in self._hints(path):
            try:
                items = self._deezer_search(artist, title, album)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"recherche par nom (deezer) : {e}")
                return None
            for item in items:
                diff = abs(int(item.get("duration") or 0) - duration)
                key = (diff <= tolerance, _similar(item.get("album", {}).get("title"), album), -diff)
                if best_key is None or key > best_key:
                    best_key, best = key, (item, artist, title, album, diff)
            if best_key and best_key[0]:
                break
        if not best or not best_key[0]:
            detail = f"durée différente ({best[4]} s d'écart) : {best[0].get('link')}" if best else "aucun titre correspondant"
            warnings.append(f"recherche par nom : {detail}")
            return None

        item, artist, title, album, diff = best
        track = self._deezer(f"track/{item['id']}")
        album_id = (track.get("album") or {}).get("id")
        alb = self._cache.get(("dz_album", album_id), lambda: self._deezer_album(album_id)) if album_id else {}
        disc = int(track.get("disk_number") or 1)
        release_date = alb.get("release_date") or track.get("release_date") or ""
        tags = dict.fromkeys(TAG_KEYS) | {
            "title": track.get("title"),
            "artist": (track.get("artist") or {}).get("name"),
            "album": alb.get("title"),
            "album_artist": (alb.get("artist") or {}).get("name"),
            "track_number": track.get("track_position"),
            "track_total": alb.get("discs", {}).get(disc) or alb.get("nb_tracks"),
            "disc_number": disc,
            "disc_total": len(alb.get("discs") or {}) or 1,
            "year": int(release_date[:4]) if release_date[:4].isdigit() and release_date[:4] != "0000" else None,
            "release_date": release_date or None,
            "genre": alb.get("genre"),
            "release_type": {"album": "Album", "ep": "EP", "single": "Single",
                             "compile": "Album + Compilation"}.get(alb.get("record_type")),
            "media_format": "Digital Media",
            "isrc": track.get("isrc"),
            "deezer_track_id": track.get("id"),
            "deezer_album_id": album_id,
        }
        match = {"method": "name", "source": "deezer",
                 "query": {"artist": artist, "title": title, "album": album},
                 "duration_file": duration, "duration_match": item.get("duration"), "duration_diff": diff}
        return tags, self._deezer_links(track, alb), match

    @staticmethod
    def _hints(path: str) -> list[tuple[str, str, str | None]]:
        """(artiste, titre, album) à essayer : tags du fichier, puis chemin ARTISTE/ALBUM/Titre."""
        hints = []
        tags = read_common_tags(path)
        if (artist := tags.get("artist") or tags.get("albumartist")) and tags.get("title"):
            hints.append((artist, tags["title"], tags.get("album")))
        parts = os.path.normpath(path).split(os.sep)
        if len(parts) >= 3:
            title = re.sub(r"^\s*(\d{1,2}[-.]\s*)?\d{1,3}\s*[-._)]*\s*", "", os.path.splitext(parts[-1])[0])
            title = " ".join(title.replace("_", " ").split())                 # « 01 - Titre » -> « Titre »
            album = re.sub(r"\s*\[(disc|cd)\s*\d+\]\s*$", "", parts[-2], flags=re.IGNORECASE)
            if title and (parts[-3], title, album) not in hints:
                hints.append((parts[-3], title, album))
        return hints

    # --------------------------------------------------- Liens et pochettes
    def _add_links_and_covers(self, t: dict, warnings: list[str], deezer: dict | None) -> None:
        artist, title, album = t["artist"] or "", t["title"] or "", t["album"]
        album_artist = t["album_artist"] or artist
        rec, rel, rg = t["musicbrainz_recording_id"], t["musicbrainz_release_id"], t["musicbrainz_releasegroup_id"]

        def safe(service: str, fetch: Callable[[], dict | None]) -> dict:
            try:
                value = fetch()
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{service} : {e}")
                return {}
            if not value:
                warnings.append(f"{service} : aucun résultat correspondant")
            return value or {}

        if deezer is None:
            deezer = safe("deezer", lambda: self._deezer_links_for(artist, title, album))
        apple = safe("apple_music", lambda: self._cache.get(
            ("itunes", _normalize(album_artist), _normalize(album)), lambda: self._itunes(album_artist, album))
        ) if album else {}
        caa = safe("coverartarchive", lambda: self._cache.get(
            ("caa", rel, rg), lambda: self._cover_art_archive(rel, rg))) if rel or rg else {}

        t["links"] = {
            "musicbrainz_recording": rec and f"{self._mb_url}/recording/{rec}",
            "musicbrainz_release": rel and f"{self._mb_url}/release/{rel}",
            "musicbrainz_release_group": rg and f"{self._mb_url}/release-group/{rg}",
            "acoustid_track": t["acoustid_id"] and f"{self._acoustid_track_url}/{t['acoustid_id']}",
            "deezer_track": deezer.get("track"),
            "deezer_preview_30s": deezer.get("preview"),
            "deezer_album": deezer.get("album"),
            "apple_music_album": apple.get("url"),
            "youtube_search": f"{self._youtube_search_url}?search_query="
                              + urllib.parse.quote_plus(f"{artist} {title}"),
        }
        covers = {
            "apple_music_3000px": apple.get("cover"),
            "coverartarchive_original": caa.get("original"),
            "coverartarchive_1200px": caa.get("1200"),
            "deezer_1000px": deezer.get("cover"),
        }
        # Priorité : visuel officiel Apple (3000 px) > scan original MusicBrainz > Deezer
        covers["best"] = covers["apple_music_3000px"] or covers["coverartarchive_original"] or covers["deezer_1000px"]
        t["covers"] = covers

    def _deezer_links_for(self, artist: str, title: str, album: str | None) -> dict | None:
        items = self._deezer_search(artist, title, None)
        if not items:
            return None
        item = next((i for i in items if album and _similar(i.get("album", {}).get("title"), album)), items[0])
        return self._deezer_links(item, item.get("album") or {})

    def _deezer_links(self, track: dict, album: dict) -> dict:
        album_id = album.get("id") or (track.get("album") or {}).get("id")
        return {"track": track.get("link"), "preview": track.get("preview") or None,
                "album": album_id and f"{self._deezer_web_url}/album/{album_id}",
                "cover": album.get("cover_xl") or (track.get("album") or {}).get("cover_xl")}

    def _itunes(self, artist: str, album: str) -> dict | None:
        for term in (f"{artist} {album}", album):  # l'album seul si l'artiste est écrit autrement
            for item in self._http(self._itunes_url + urllib.parse.quote_plus(term), "itunes").get("results", []):
                if _similar(item.get("artistName"), artist) and _similar(item.get("collectionName"), album):
                    cover = re.sub(r"/\d+x\d+bb\.\w+$", "/3000x3000bb.jpg", item.get("artworkUrl100") or "")
                    return {"url": item.get("collectionViewUrl"), "cover": cover or None}
        return None

    def _cover_art_archive(self, release_id: str | None, releasegroup_id: str | None) -> dict | None:
        for kind, mbid in (("release", release_id), ("release-group", releasegroup_id)):
            data = mbid and self._http(f"{self._caa_url}/{kind}/{mbid}", "caa", allow_404=True)
            front = next((i for i in (data or {}).get("images", []) if i.get("front")), None)
            if front:
                thumbs = front.get("thumbnails", {})
                return {"original": _https(front.get("image")),
                        "1200": _https(thumbs.get("1200") or thumbs.get("large"))}
        return None

    # -------------------------------------------------------------- Deezer
    def _deezer(self, endpoint: str) -> dict:
        data = self._http(f"{self._deezer_api_url}/{endpoint}", "deezer")
        if data.get("error"):  # Deezer renvoie ses erreurs en HTTP 200
            raise RuntimeError(f"API Deezer : {data['error']}")
        return data

    def _deezer_search(self, artist: str, title: str, album: str | None) -> list[dict]:
        """Résultats (dédoublonnés) des recherches stricte puis libre."""
        queries = [f'artist:"{artist}" album:"{album}" track:"{title}"'] if album else []
        queries += [f'artist:"{artist}" track:"{title}"', f"{artist} {title}"]
        found: dict[int, dict] = {}
        for q in queries:
            for item in self._deezer(f"search/track?limit=25&q={urllib.parse.quote(q)}").get("data") or []:
                if _similar(item.get("artist", {}).get("name"), artist) and _similar(item.get("title"), title):
                    found.setdefault(item["id"], item)
        return list(found.values())

    def _deezer_album(self, album_id: int) -> dict:
        album = self._deezer(f"album/{album_id}")
        album["genre"] = next((g["name"] for g in (album.get("genres") or {}).get("data") or []), None)
        album["discs"] = {}
        for track in self._deezer(f"album/{album_id}/tracks?limit=500").get("data") or []:
            disc = int(track.get("disk_number") or 1)
            album["discs"][disc] = album["discs"].get(disc, 0) + 1
        return album

    # ------------------------------------------------------------ Outils
    def _http(self, url: str, service: str, data: dict | None = None, allow_404: bool = False) -> Any:
        """Appel JSON via le helper Web, avec la limite de débit du service."""
        return fetch_json(url, data=data, headers=self._headers, limiter=self._limiters[service],
                          timeout=self._TIMEOUT, allow_404=allow_404,
                          # Deezer signale son quota dépassé (50 req / 5 s) en HTTP 200, code 4
                          retry_if=_deezer_quota_exceeded if service == "deezer" else None)
