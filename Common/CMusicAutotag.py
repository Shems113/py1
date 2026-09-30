"""
CMusicAutotag — identifie un fichier audio par empreinte acoustique
(AcoustID + MusicBrainz) et renvoie les tags trouvés, avec des liens de
contrôle (MusicBrainz, AcoustID, Deezer, Apple Music, YouTube) et des
pochettes haute définition. Aucune écriture dans le fichier.

    tagger = CMusicAutotag(api_key="...", contact="toi@mail.com")
    res = tagger.get_tags(r"D:\\Musique\\piste.flac")
    # forcer une édition précise (ex. pour aligner les bonus d'un album) :
    res = tagger.get_tags(r"D:\\Musique\\bonus.mp3", release_id="ca0501d3-...")

Prérequis : fpcalc (https://acoustid.org/chromaprint) dans le PATH ou
variable FPCALC, et une clé d'APPLICATION AcoustID
(https://acoustid.org/new-application). Aucune dépendance Python externe.

Thread-safe : une même instance peut être appelée depuis plusieurs threads.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


class CMusicAutotag:
    _ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
    _MB_RG_URL = "https://musicbrainz.org/ws/2/release-group/{}?inc=genres&fmt=json"
    _CAA_URL = "https://coverartarchive.org/{}/{}"
    _DEEZER_URL = "https://api.deezer.com/search/track?q={}&limit=25"
    _ITUNES_URL = "https://itunes.apple.com/search?term={}&entity=album&limit=25&country={}"
    _FPCALC = r"C:\Users\SEDOUD\Downloads\fpcalc\fpcalc.exe"
    # Intervalle minimal entre deux appels, par service (secondes)
    _INTERVALS = {"acoustid": 1 / 3, "musicbrainz": 1.0, "caa": 0.2,
                  "deezer": 0.2, "itunes": 3.0}

    # Préférence : album studio > EP > single ; compilations et lives pénalisés
    _PRIMARY = {"Album": 3, "EP": 1, "Single": 0}
    _SECONDARY = {"Compilation": -3, "Live": -2, "DJ-mix": -2, "Soundtrack": -1, "Remix": -1}
    # Préférence de support : la numérotation CD est celle attendue en numérique
    _FORMATS = {"CD": 3, "Digital Media": 2, "Enhanced CD": 2, "HDCD": 2, "SACD": 1,
                "Hybrid SACD": 1}

    def __init__(self, api_key: str, contact: str = "", min_score: float = 0.80,
                 fetch_genre: bool = True, fetch_links: bool = True,
                 itunes_country: str = "FR", timeout: float = 20.0):
        candidates = (os.environ.get("FPCALC"), self._FPCALC, shutil.which("fpcalc"))
        self._fpcalc = next((p for p in candidates if p and os.path.isfile(p)), None)
        if not self._fpcalc:
            raise FileNotFoundError("fpcalc introuvable (PATH ou variable FPCALC)")
        self._api_key = api_key
        self._min_score = min_score
        self._fetch_genre = fetch_genre
        self._fetch_links = fetch_links
        self._country = itunes_country
        self._timeout = timeout
        self._user_agent = f"CMusicAutotag/1.0 ( {contact or 'no-contact'} )"

        self._lock = threading.Lock()
        self._next_call = {s: 0.0 for s in self._INTERVALS}
        self._cache: dict[tuple, Any] = {}

    # ------------------------------------------------------------------ API
    def get_tags(
        self,
        path: str,
        release_id: str | None = None,
    ) -> dict[str, Any]:
        """Renvoie un dict : path, status (ok / not_found / error), score, tags,
        error, warnings. Ne lève jamais d'exception.

        release_id : identifiant MusicBrainz d'une édition à privilégier. Si le
        morceau y figure, c'est elle qui est retenue (numérotation, pochette...)."""
        result: dict[str, Any] = {"path": str(path), "status": "not_found", "score": None,
                                  "tags": None, "error": None, "warnings": []}
        warnings: list[str] = result["warnings"]
        try:
            duration, fingerprint = self._fingerprint(str(path))
            best = self._pick(self._lookup(fingerprint, duration), release_id)
            if best:
                result["score"], tags = best
                result["tags"] = tags
                result["status"] = "ok"
                if release_id and tags["musicbrainz_release_id"] != release_id:
                    warnings.append(f"morceau absent de l'édition {release_id}, "
                                    "meilleure édition retenue à la place")
                if tags["musicbrainz_releasegroup_id"]:
                    self._apply_release_group(tags, warnings)
                if self._fetch_links:
                    self._enrich(tags, warnings)
        except Exception as e:  # noqa: BLE001
            result["status"] = "error"
            result["error"] = f"{type(e).__name__}: {e}"
        return result

    # ------------------------------------------------------- Identification
    def _fingerprint(self, path: str) -> tuple[int, str]:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        proc = subprocess.run([self._fpcalc, "-json", path], capture_output=True,
                              text=True, encoding="utf-8", timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"fpcalc : {proc.stderr.strip()}")
        data = json.loads(proc.stdout)
        return int(data["duration"]), data["fingerprint"]

    def _lookup(self, fingerprint: str, duration: int) -> list[dict]:
        body = urllib.parse.urlencode({
            "client": self._api_key, "format": "json",
            "duration": duration, "fingerprint": fingerprint,
            "meta": "recordings releasegroups releases tracks",
        }).encode()
        resp = self._http(self._ACOUSTID_URL, "acoustid", body)
        if resp.get("status") != "ok":
            raise RuntimeError(f"AcoustID : {resp.get('error')}")
        return resp.get("results", [])

    def _apply_release_group(self, tags: dict, warnings: list[str]) -> None:
        """Année de sortie ORIGINALE de l'album et genre, depuis MusicBrainz
        (une seule requête par album, mise en cache)."""
        rg_id = tags["musicbrainz_releasegroup_id"]
        try:
            rg = self._cached(("rg", rg_id),
                              lambda: self._http(self._MB_RG_URL.format(rg_id), "musicbrainz"))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"musicbrainz : {type(e).__name__}: {e}")
            return
        first = (rg.get("first-release-date") or "")[:4]
        if first.isdigit():
            tags["year"] = int(first)
        if self._fetch_genre:
            genres = rg.get("genres") or []
            if genres:
                tags["genre"] = max(genres, key=lambda g: g.get("count", 0))["name"].title()

    # ------------------------------------------------ Liens et pochettes
    def _enrich(self, t: dict, warnings: list[str]) -> None:
        artist, title = t["artist"] or "", t["title"] or ""
        album, album_artist = t["album"], t["album_artist"] or artist
        rec, rel, rg = (t["musicbrainz_recording_id"], t["musicbrainz_release_id"],
                        t["musicbrainz_releasegroup_id"])

        def safe(service: str, fn: Callable[[], Any]) -> Any:
            try:
                value = fn()
                if value is None:
                    warnings.append(f"{service} : aucun résultat correspondant")
                return value
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{service} : {type(e).__name__}: {e}")
                return None

        deezer = safe("deezer", lambda: self._deezer(artist, title, album))
        apple = safe("apple_music", lambda: self._cached(
            ("itunes", self._norm(album_artist), self._norm(album)),
            lambda: self._itunes(album_artist, album))) if album else None
        caa = safe("coverartarchive", lambda: self._cached(
            ("caa", rel, rg), lambda: self._cover_art_archive(rel, rg))) if rel or rg else None

        mb = "https://musicbrainz.org"
        t["links"] = {
            "musicbrainz_recording": f"{mb}/recording/{rec}" if rec else None,
            "musicbrainz_release": f"{mb}/release/{rel}" if rel else None,
            "musicbrainz_release_group": f"{mb}/release-group/{rg}" if rg else None,
            "acoustid_track": (f"https://acoustid.org/track/{t['acoustid_id']}"
                               if t.get("acoustid_id") else None),
            "deezer_track": (deezer or {}).get("track"),
            "deezer_preview_30s": (deezer or {}).get("preview"),
            "deezer_album": (deezer or {}).get("album"),
            "apple_music_album": (apple or {}).get("url"),
            "youtube_search": "https://www.youtube.com/results?search_query="
                              + urllib.parse.quote_plus(f"{artist} {title}"),
        }
        covers = {
            "apple_music_3000px": (apple or {}).get("cover"),
            "coverartarchive_original": (caa or {}).get("original"),
            "coverartarchive_1200px": (caa or {}).get("1200"),
            "deezer_1000px": (deezer or {}).get("cover"),
        }
        # Priorité : visuel officiel Apple (3000 px) > scan CAA original > Deezer
        covers["best"] = next((covers[k] for k in ("apple_music_3000px",
                              "coverartarchive_original", "deezer_1000px") if covers[k]), None)
        t["covers"] = covers

    def _deezer(self, artist: str, title: str, album: str | None) -> dict | None:
        # 1) recherche avancée stricte, 2) recherche libre (tolère « Idéal J » / « Ideal J »)
        queries = [f'artist:"{artist}" track:"{title}"', f"{artist} {title}"]
        for q in queries:
            resp = self._http(self._DEEZER_URL.format(urllib.parse.quote(q)), "deezer")
            if resp.get("error"):  # Deezer renvoie ses erreurs en HTTP 200
                raise RuntimeError(f"API Deezer : {resp['error']}")
            items = [i for i in resp.get("data") or []
                     if self._same(i.get("artist", {}).get("name"), artist)
                     and self._same(i.get("title"), title)]
            if items:
                hit = next((i for i in items
                            if album and self._same(i.get("album", {}).get("title"), album)),
                           items[0])
                alb = hit.get("album") or {}
                return {"track": hit.get("link"), "preview": hit.get("preview") or None,
                        "album": f"https://www.deezer.com/album/{alb['id']}" if alb.get("id") else None,
                        "cover": alb.get("cover_xl")}
        return None

    def _itunes(self, artist: str, album: str) -> dict | None:
        # 1) artiste + album, 2) album seul (si l'artiste est orthographié autrement)
        for term in (f"{artist} {album}", album):
            url = self._ITUNES_URL.format(urllib.parse.quote_plus(term), self._country)
            for i in self._http(url, "itunes").get("results", []):
                if self._same(i.get("artistName"), artist) and self._same(i.get("collectionName"), album):
                    art = i.get("artworkUrl100") or ""
                    return {"url": i.get("collectionViewUrl"),
                            "cover": re.sub(r"/\d+x\d+bb\.\w+$", "/3000x3000bb.jpg", art) or None}
        return None

    def _cover_art_archive(self, rel_id: str | None, rg_id: str | None) -> dict | None:
        for kind, mbid in (("release", rel_id), ("release-group", rg_id)):
            if not mbid:
                continue
            data = self._http(self._CAA_URL.format(kind, mbid), "caa", allow_404=True)
            for img in (data or {}).get("images", []):
                if img.get("front"):
                    th = img.get("thumbnails", {})
                    return {"original": self._https(img.get("image")),
                            "1200": self._https(th.get("1200") or th.get("large"))}
        return None

    @staticmethod
    def _https(url: str | None) -> str | None:
        return url.replace("http://", "https://", 1) if url else None

    # ------------------------------------------------------------ Outils
    def _cached(self, key: tuple, fetch: Callable[[], Any]) -> Any:
        """Cache thread-safe. Une exception n'est pas mise en cache (retentée
        au prochain appel) et remonte à l'appelant."""
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        value = fetch()
        with self._lock:
            self._cache[key] = value
        return value

    @staticmethod
    def _norm(s: str | None) -> str:
        s = (s or "").lower().replace("œ", "oe").replace("æ", "ae")
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)          # (Remastered 2011), [Deluxe]...
        s = re.sub(r"\b(the|feat|ft)\b", " ", s)
        return re.sub(r"[^a-z0-9]+", " ", s).strip()

    def _same(self, a: str | None, b: str | None) -> bool:
        na, nb = self._norm(a), self._norm(b)
        return bool(na and nb) and (na == nb or na in nb or nb in na)

    def _http(self, url: str, service: str, data: bytes | None = None,
              allow_404: bool = False) -> dict | None:
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        for attempt in range(3):
            self._throttle(service)
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=self._timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 404 and allow_404:
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"HTTP {e.code} ({service}) : "
                                   f"{e.read().decode('utf-8', 'replace')[:200]}") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError(f"{service} : échec après 3 tentatives")

    def _throttle(self, service: str) -> None:
        """Réserve un créneau sous verrou, attend hors verrou (thread-safe)."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_call[service])
            self._next_call[service] = slot + self._INTERVALS[service]
        if slot > now:
            time.sleep(slot - now)

    # ------------------------------------------------ Choix du meilleur match
    def _pick(self, results: list[dict], release_id: str | None = None
              ) -> tuple[float, dict] | None:
        best_key, best = None, None
        for res in results:
            score = float(res.get("score", 0))
            if score < self._min_score:
                continue
            for rec in res.get("recordings") or []:
                if not rec.get("title"):
                    continue
                for rg in rec.get("releasegroups") or [None]:
                    if rg is None:  # titre et artiste connus, mais aucun album
                        key = (0, round(score, 1), -99, 0, 0, 0)
                        if best_key is None or key > best_key:
                            best_key, best = key, (score, self._build(res, rec))
                        continue
                    bonus = self._PRIMARY.get(rg.get("type"), -1) + sum(
                        self._SECONDARY.get(s, 0) for s in rg.get("secondarytypes", []))
                    for rel in rg.get("releases") or []:
                        key = (int(bool(release_id) and rel.get("id") == release_id),
                               round(score, 1), bonus, self._format_score(rel),
                               -(self._year(rel) or 9999),      # la plus ancienne en CD
                               -(rel.get("medium_count") or 99))
                        if best_key is None or key > best_key:
                            best_key = key
                            best = (score, self._build(res, rec, rg, rel))
        return best

    def _format_score(self, rel: dict) -> int:
        fmts = [m.get("format") for m in rel.get("mediums") or [] if m.get("format")]
        if not fmts:
            return 0  # format inconnu : neutre
        return min(self._FORMATS.get(f, -2) for f in fmts)  # vinyle, cassette... = -2

    @staticmethod
    def _year(rel: dict) -> int | None:
        return (rel.get("date") or {}).get("year")

    @staticmethod
    def _artists(items: list[dict] | None) -> str | None:
        return "".join(a.get("name", "") + a.get("joinphrase", "") for a in items or []) or None

    def _build(self, res: dict, rec: dict, rg: dict | None = None,
               rel: dict | None = None) -> dict:
        medium, track = {}, {}
        for m in (rel or {}).get("mediums") or []:
            if m.get("tracks"):
                medium, track = m, m["tracks"][0]
                break
        d = (rel or {}).get("date") or {}
        release_date = "-".join([f"{d['year']:04d}"] + [f"{d[k]:02d}" for k in ("month", "day")
                                                         if d.get(k)]) if d.get("year") else None
        return {
            "title": track.get("title") or rec.get("title"),
            "artist": self._artists(track.get("artists")) or self._artists(rec.get("artists")),
            "album": (rel or {}).get("title") or (rg or {}).get("title"),
            "album_artist": self._artists((rg or {}).get("artists")),
            "track_number": track.get("position"),
            "track_total": medium.get("track_count"),
            "disc_number": medium.get("position"),
            "disc_total": (rel or {}).get("medium_count"),
            "year": d.get("year"),          # remplacé par l'année originale de l'album
            "release_date": release_date,   # date de CETTE édition
            "genre": None,
            "release_type": " + ".join([rg.get("type") or "?"] + rg.get("secondarytypes", []))
                            if rg else None,
            "media_format": " + ".join(m.get("format") or "?" for m in (rel or {}).get("mediums") or [])
                            or None,
            "country": (rel or {}).get("country"),
            "acoustid_id": res.get("id"),
            "musicbrainz_recording_id": rec.get("id"),
            "musicbrainz_release_id": (rel or {}).get("id"),
            "musicbrainz_releasegroup_id": (rg or {}).get("id"),
            "links": None,
            "covers": None,
        }