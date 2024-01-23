"""Contains a runnable song loader."""

from __future__ import annotations

import base64
import copy
import tempfile
import traceback
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator

import attrs
import mutagen.mp4
import mutagen.oggvorbis
import send2trash
from mutagen import id3
from mutagen.flac import Picture
from PIL import Image
from PySide6.QtCore import QRunnable, QThreadPool

from usdb_syncer import (
    SongId,
    SyncMetaId,
    db,
    errors,
    events,
    resource_dl,
    usdb_scraper,
)
from usdb_syncer.constants import ISO_639_2B_LANGUAGE_CODES
from usdb_syncer.download_options import Options, download_options
from usdb_syncer.logger import Log, get_logger
from usdb_syncer.resource_dl import ImageKind, download_and_process_image
from usdb_syncer.song_txt import Headers, SongTxt
from usdb_syncer.sync_meta import ResourceFile, SyncMeta
from usdb_syncer.usdb_scraper import SongDetails
from usdb_syncer.usdb_song import DownloadStatus, UsdbSong
from usdb_syncer.utils import (
    is_name_maybe_with_suffix,
    next_unique_directory,
    resource_file_ending,
    sanitize_filename,
)


@attrs.define(kw_only=True)
class Locations:
    """Paths for downloading a song."""

    folder: Path
    filename_stem: str
    tempdir: Path

    @classmethod
    def new(
        cls, song: UsdbSong, song_dir: Path, headers: Headers, tempdir: Path
    ) -> Locations:
        filename_stem = sanitize_filename(headers.artist_title_str())
        if song.sync_meta:
            folder = song.sync_meta.path.parent
        else:
            folder = next_unique_directory(song_dir.joinpath(filename_stem))
        return cls(folder=folder, filename_stem=filename_stem, tempdir=tempdir)

    def filename_with_ending(self, filename: str) -> str:
        """Path to file in the final song folder with the ending of the given file."""
        return self.filename_stem + resource_file_ending(filename)

    def file_path(self, file: str = "", ext: str = "") -> Path:
        """Path to file in the final download directory. The final path component is
        the generic name or the provided file, optionally with the provided extension.
        """
        name = file or self.filename_stem
        if ext:
            name = f"{name}.{ext}"
        return self.folder.joinpath(name)

    def temp_path(self, file: str = "", ext: str = "") -> Path:
        """Path to file in the temporary download directory. The final path component is
        the generic name or the provided file, optionally with the provided extension.
        """
        name = file or self.filename_stem
        if ext:
            name = f"{name}.{ext}"
        return self.tempdir.joinpath(name)


@attrs.define
class TempResourceFile:
    """Interim resource file in the temporary folder, or in the old folder if the
    resource is potentially kept.
    """

    old_path: Path | None = None
    new_path: Path | None = None
    resource: str | None = None

    @classmethod
    def from_existing(cls, old: ResourceFile, folder: Path) -> TempResourceFile:
        return cls(resource=old.resource, old_path=folder.joinpath(old.fname))

    def path_and_resource(self) -> tuple[Path, str] | None:
        if (path := self.new_path or self.old_path) and self.resource:
            return (path, self.resource)
        return None

    def path(self) -> Path | None:
        return self.new_path or self.old_path

    def to_resource_file(self) -> ResourceFile | None:
        if path_resource := self.path_and_resource():
            return ResourceFile.new(*path_resource)
        return None


@attrs.define
class TempResourceFiles:
    """Collection of all temporary resource files."""

    txt: TempResourceFile = attrs.field(factory=TempResourceFile)
    audio: TempResourceFile = attrs.field(factory=TempResourceFile)
    video: TempResourceFile = attrs.field(factory=TempResourceFile)
    cover: TempResourceFile = attrs.field(factory=TempResourceFile)
    background: TempResourceFile = attrs.field(factory=TempResourceFile)

    def __iter__(self) -> Iterator[TempResourceFile]:
        return iter((self.txt, self.audio, self.video, self.cover, self.background))


@attrs.define
class Context:
    """Context for downloading media and creating a song folder."""

    # deep copy of the passed in song
    song: UsdbSong
    details: SongDetails
    options: Options
    txt: SongTxt
    locations: Locations
    logger: Log
    out: TempResourceFiles = attrs.field(factory=TempResourceFiles)

    def __attrs_post_init__(self) -> None:
        # reuse old resource files unless we acquire new ones later on
        # txt is always rewritten
        if self.song.sync_meta:
            for old, out in (
                (self.song.sync_meta.audio, self.out.audio),
                (self.song.sync_meta.video, self.out.video),
                (self.song.sync_meta.cover, self.out.cover),
                (self.song.sync_meta.background, self.out.background),
            ):
                if old and old.is_in_sync(self.locations.folder):
                    out.resource = old.resource
                    out.old_path = self.locations.file_path(old.fname)

    @classmethod
    def new(
        cls, song: UsdbSong, options: Options, tempdir: Path, logger: Log
    ) -> Context:
        song = copy.deepcopy(song)
        details, txt = _get_usdb_data(song.song_id, logger)
        _update_song_with_usdb_data(song, details, txt)
        paths = Locations.new(song, options.song_dir, txt.headers, tempdir)
        if not song.sync_meta:
            song.sync_meta = SyncMeta.new(song.song_id, paths.folder, txt.meta_tags)
        return cls(song, details, options, txt, paths, logger)

    def all_audio_resources(self) -> Iterator[str]:
        if self.txt.meta_tags.audio:
            yield self.txt.meta_tags.audio
        if not self.txt.meta_tags.video:
            self.logger.debug("No valid audio/video meta tag. Looking in comments.")
        yield from self.all_video_resources()

    def all_video_resources(self) -> Iterator[str]:
        if self.txt.meta_tags.video:
            yield self.txt.meta_tags.video
        yield from self.details.all_comment_videos()

    def cover_url(self) -> str | None:
        url = None
        if self.txt.meta_tags.cover:
            url = self.txt.meta_tags.cover.source_url(self.logger)
            self.logger.debug(f"downloading cover from #VIDEO params: {url}")
        elif self.details.cover_url:
            url = self.details.cover_url
            self.logger.warning(
                "no cover resource in #VIDEO tag, so fallback to small usdb cover!"
            )
        return url

    def background_url(self) -> str | None:
        url = None
        if self.txt.meta_tags.background:
            url = self.txt.meta_tags.background.source_url(self.logger)
            self.logger.debug(f"downloading background from #VIDEO params: {url}")
        return url


def _get_usdb_data(song_id: SongId, logger: Log) -> tuple[SongDetails, SongTxt]:
    details = usdb_scraper.get_usdb_details(song_id)
    logger.info(f"Found '{details.artist} - {details.title}' on USDB.")
    txt_str = usdb_scraper.get_notes(details.song_id, logger)
    txt = SongTxt.parse(txt_str, logger)
    txt.sanitize()
    txt.headers.creator = txt.headers.creator or details.uploader or None
    return details, txt


def _update_song_with_usdb_data(
    song: UsdbSong, details: SongDetails, txt: SongTxt
) -> None:
    song.artist = details.artist
    song.title = details.title
    song.language = txt.headers.language or ""
    song.edition = txt.headers.edition or ""
    song.golden_notes = details.golden_notes
    song.rating = details.rating
    song.views = details.views


class SongLoader(QRunnable):
    """Runnable to create a complete song folder."""

    def __init__(self, song: UsdbSong, options: Options) -> None:
        super().__init__()
        self.song = song
        self.song_id = song.song_id
        self.options = options
        self.logger = get_logger(__file__, self.song_id)

    def run(self) -> None:
        change_event: events.SubscriptableEvent = events.SongChanged(self.song_id)
        try:
            updated_song = self._run_inner()
        except errors.UsdbLoginError:
            self.logger.error("Aborted; download requires login.")
            self.song.status = DownloadStatus.FAILED
        except errors.UsdbNotFoundError:
            self.logger.error("Song has been deleted from USDB.")
            with db.transaction():
                self.song.delete()
            change_event = events.SongDeleted(self.song_id)
        except Exception:  # pylint: disable=broad-except
            self.logger.debug(traceback.format_exc())
            self.logger.error(
                "Failed to finish download due to an unexpected error. "
                "See debug log for more information."
            )
            self.song.status = DownloadStatus.FAILED
        else:
            updated_song.status = DownloadStatus.NONE
            with db.transaction():
                updated_song.upsert()
            self.logger.info("All done!")
        change_event.post()
        events.DownloadFinished(self.song_id).post()

    def _run_inner(self) -> UsdbSong:
        self.song.status = DownloadStatus.DOWNLOADING
        events.SongChanged(self.song_id).post()
        with tempfile.TemporaryDirectory() as tempdir:
            ctx = Context.new(self.song, self.options, Path(tempdir), self.logger)

            _maybe_download_audio(ctx)
            _maybe_download_video(ctx)
            _maybe_download_cover(ctx)
            _maybe_download_background(ctx)
            _maybe_write_audio_tags(ctx)

            _cleanup_exisiting_resource(ctx)
            _ensure_correct_folder_name(ctx.locations)
            # only here so filenames in header are up-to-date
            _maybe_write_txt(ctx)
            _persist_tempfiles(ctx)

        _write_sync_meta(ctx)

        return ctx.song


def download_songs(songs: Iterable[UsdbSong]) -> None:
    options = download_options()
    threadpool = QThreadPool.globalInstance()
    for song in songs:
        worker = SongLoader(song, options)
        threadpool.start(worker)


def _maybe_download_audio(ctx: Context) -> None:
    if not (options := ctx.options.audio_options):
        return
    for resource in islice(ctx.all_audio_resources(), 10):
        if ctx.out.audio.resource == resource:
            ctx.logger.info("Audio resource is unchanged.")
            return
        if ext := resource_dl.download_audio(
            resource,
            options,
            ctx.options.browser,
            ctx.locations.temp_path(),
            ctx.logger,
        ):
            ctx.out.audio.resource = resource
            ctx.out.audio.new_path = ctx.locations.temp_path(ext=ext)
            ctx.logger.info("Success! Downloaded audio.")
            return
    keep = " Keeping last resource." if ctx.out.audio.resource else ""
    song_len = ctx.txt.minimum_song_length()
    ctx.logger.error(f"Failed to download audio (song duration > {song_len})!{keep}")


def _maybe_download_video(ctx: Context) -> None:
    if not (options := ctx.options.video_options) or ctx.txt.meta_tags.is_audio_only():
        return
    for resource in islice(ctx.all_video_resources(), 10):
        if ctx.out.video.resource == resource:
            ctx.logger.info("Video resource is unchanged.")
            return
        if ext := resource_dl.download_video(
            resource,
            options,
            ctx.options.browser,
            ctx.locations.temp_path(),
            ctx.logger,
        ):
            ctx.out.video.resource = resource
            ctx.out.video.new_path = ctx.locations.temp_path(ext=ext)
            ctx.logger.info("Success! Downloaded video.")
            return
    keep = " Keeping last resource." if ctx.out.video.resource else ""
    ctx.logger.error(f"Failed to download video!{keep}")


def _maybe_download_cover(ctx: Context) -> None:
    if not ctx.options.cover:
        return
    if not (url := ctx.cover_url()):
        ctx.logger.warning("No cover resource found.")
        return
    if ctx.out.cover.resource == url:
        ctx.logger.info("Cover resource is unchanged.")
        return
    if path := download_and_process_image(
        url,
        ctx.locations.temp_path(),
        ctx.txt.meta_tags.cover,
        ctx.details,
        ImageKind.COVER,
        max_width=ctx.options.cover.max_size,
    ):
        ctx.out.cover.resource = url
        ctx.out.cover.new_path = path
        ctx.logger.info("Success! Downloaded cover.")
    else:
        keep = " Keeping last resource." if ctx.out.cover.resource else ""
        ctx.logger.error(f"Failed to download cover!{keep}")


def _maybe_download_background(ctx: Context) -> None:
    if not (options := ctx.options.background_options):
        return
    if not options.download_background(bool(ctx.txt.headers.video)):
        return
    if not (url := ctx.background_url()):
        ctx.logger.warning("No background resource found.")
        return
    if ctx.out.background.resource == url:
        ctx.logger.info("Background resource is unchanged.")
        return
    if path := download_and_process_image(
        url,
        ctx.locations.temp_path(),
        ctx.txt.meta_tags.background,
        ctx.details,
        ImageKind.BACKGROUND,
        max_width=None,
    ):
        ctx.out.background.resource = url
        ctx.out.background.new_path = path
        ctx.logger.info("Success! Downloaded background.")
    else:
        keep = " Keeping last resource." if ctx.out.cover.resource else ""
        ctx.logger.error(f"Failed to download background!{keep}")


def _maybe_write_txt(ctx: Context) -> None:
    if not (options := ctx.options.txt_options):
        return
    _write_headers(ctx)
    ctx.out.txt.new_path = path = ctx.locations.temp_path(ext="txt")
    ctx.txt.write_to_file(path, options.encoding.value, options.newline.value)
    ctx.out.txt.resource = ctx.song.song_id.usdb_url()
    ctx.logger.info("Success! Created song txt.")


def _write_headers(ctx: Context) -> None:
    if path := ctx.out.audio.path():
        ctx.txt.headers.mp3 = path.name
    if path := ctx.out.video.path():
        ctx.txt.headers.video = path.name
    if path := ctx.out.cover.path():
        ctx.txt.headers.cover = path.name
    if path := ctx.out.background.path():
        ctx.txt.headers.background = path.name


def _maybe_write_audio_tags(ctx: Context) -> None:
    if not (options := ctx.options.audio_options):
        return
    if not (path_resource := ctx.out.audio.path_and_resource()):
        return
    path, resource = path_resource
    try:
        match path_resource[1]:
            case ".m4a":
                _write_m4a_tags(path, resource, ctx, options.embed_artwork)
            case ".mp3":
                _write_mp3_tags(path, resource, ctx, options.embed_artwork)
            case ".ogg":
                _write_ogg_tags(path, ctx, options.embed_artwork)
    except Exception:  # pylint: disable=broad-exception-caught
        ctx.logger.debug(traceback.format_exc())
        ctx.logger.error(f"Failed to write audio tags to file '{path}'!")
    else:
        ctx.logger.debug(f"Audio tags written to file '{path}'.")


def _write_m4a_tags(
    path: Path, resource: str, ctx: Context, embed_artwork: bool
) -> None:
    tags = mutagen.mp4.MP4Tags()

    tags["\xa9ART"] = ctx.txt.headers.artist
    tags["\xa9nam"] = ctx.txt.headers.title
    if ctx.txt.headers.genre:
        tags["\xa9gen"] = ctx.txt.headers.genre
    if ctx.txt.headers.year:
        tags["\xa9day"] = ctx.txt.headers.year
    tags["\xa9lyr"] = ctx.txt.unsynchronized_lyrics()
    tags["\xa9cmt"] = resource

    if embed_artwork:
        tags["covr"] = [
            mutagen.mp4.MP4Cover(
                image.read_bytes(), imageformat=mutagen.mp4.MP4Cover.FORMAT_JPEG
            )
            for image in (ctx.out.cover.path(), ctx.out.background.path())
            if image
        ]

    tags.save(path)


def _write_mp3_tags(
    path: Path, resource: str, ctx: Context, embed_artwork: bool
) -> None:
    tags = id3.ID3()

    lang = ISO_639_2B_LANGUAGE_CODES.get(ctx.txt.headers.main_language(), "und")
    tags["TPE1"] = id3.TPE1(encoding=id3.Encoding.UTF8, text=ctx.txt.headers.artist)
    tags["TIT2"] = id3.TIT2(encoding=id3.Encoding.UTF8, text=ctx.txt.headers.title)
    tags["TLAN"] = id3.TLAN(encoding=id3.Encoding.UTF8, text=lang)
    if genre := ctx.txt.headers.genre:
        tags["TCON"] = id3.TCON(encoding=id3.Encoding.UTF8, text=genre)
    if year := ctx.txt.headers.year:
        tags["TDRC"] = id3.TDRC(encoding=id3.Encoding.UTF8, text=year)
    tags[f"USLT::'{lang}'"] = id3.USLT(
        encoding=id3.Encoding.UTF8,
        lang=lang,
        desc="Lyrics",
        text=ctx.txt.unsynchronized_lyrics(),
    )
    tags["SYLT"] = id3.SYLT(
        encoding=id3.Encoding.UTF8,
        lang=lang,
        format=2,  # milliseconds as units
        type=1,  # lyrics
        text=ctx.txt.synchronized_lyrics(),
    )
    tags["COMM"] = id3.COMM(
        encoding=id3.Encoding.UTF8, lang="eng", desc="Audio Source", text=resource
    )

    if embed_artwork and (path_resource := ctx.out.cover.path_and_resource()):
        tags.add(
            id3.APIC(
                encoding=id3.Encoding.UTF8,
                mime="image/jpeg",
                type=id3.PictureType.COVER_FRONT,
                desc=f"Source: {path_resource[1]}",
                data=path_resource[0].read_bytes(),
            )
        )

    tags.save(path)


def _write_ogg_tags(path: Path, ctx: Context, embed_artwork: bool) -> None:
    audio = mutagen.oggvorbis.OggVorbis(path)

    # Set basic tags
    audio["artist"] = ctx.txt.headers.artist
    audio["title"] = ctx.txt.headers.title
    lang = ISO_639_2B_LANGUAGE_CODES.get(ctx.txt.headers.main_language(), "und")
    audio["language"] = lang
    if genre := ctx.txt.headers.genre:
        audio["genre"] = genre
    if year := ctx.txt.headers.year:
        audio["date"] = year
    audio["lyrics"] = ctx.txt.unsynchronized_lyrics()

    if embed_artwork and (cover_path := ctx.out.cover.path()):
        picture = Picture()
        with cover_path.open("rb") as file:
            picture.data = file.read()
        with Image.open(cover_path) as image:
            picture.width, picture.height = image.size
        picture.type = 3  # "Cover (front)"
        picture.desc = "Cover art"
        picture.mime = "image/jpeg"
        picture.depth = 24

        picture_data = picture.write()
        encoded_data = base64.b64encode(picture_data)
        vcomment_value = encoded_data.decode("ascii")

        audio["metadata_block_picture"] = [vcomment_value]

    audio.save()


def _cleanup_exisiting_resource(ctx: Context) -> None:
    """Delete resources that are either out of sync or will be replaced with a new one,
    and ensure kept ones are correctly named.
    """
    if not ctx.song.sync_meta:
        return
    for (old, _), out in zip(ctx.song.sync_meta.all_resource_files(), ctx.out):
        if not old:
            continue
        if not out.old_path:
            # out of sync
            path = ctx.locations.file_path(file=old.fname)
            if path.exists():
                send2trash.send2trash(path)
                ctx.logger.debug(f"Trashed untracked file: '{path}'.")
        elif out.new_path:
            send2trash.send2trash(out.old_path)
            ctx.logger.debug(f"Trashed existing file: '{out.old_path}'.")
        elif out.old_path != (
            path := ctx.locations.file_path(ext=resource_file_ending(old.fname))
        ):
            # no new file; keep existing one, but ensure correct name
            out.old_path.rename(path)
            out.old_path = path


def _ensure_correct_folder_name(locations: Locations) -> None:
    """Ensure the song folder exists and has the correct name."""
    locations.folder.mkdir(parents=True, exist_ok=True)
    if is_name_maybe_with_suffix(locations.folder.name, locations.filename_stem):
        return
    new = next_unique_directory(locations.folder.with_name(locations.filename_stem))
    locations.folder.rename(new)
    locations.folder = new


def _persist_tempfiles(ctx: Context) -> None:
    for temp_file in ctx.out:
        if temp_file.new_path:
            target = ctx.locations.file_path(temp_file.new_path.name)
            if target.exists():
                send2trash.send2trash(target)
                ctx.logger.debug(f"Trashed existing file: '{target}'.")
            temp_file.new_path.rename(target)
            temp_file.new_path = target


def _write_sync_meta(ctx: Context) -> None:
    old = ctx.song.sync_meta
    sync_meta_id = old.sync_meta_id if old else SyncMetaId.new()
    ctx.song.sync_meta = SyncMeta(
        sync_meta_id=sync_meta_id,
        song_id=ctx.song.song_id,
        path=ctx.locations.file_path(file=sync_meta_id.to_filename()),
        mtime=0,
        meta_tags=ctx.txt.meta_tags,
        pinned=old.pinned if old else False,
    )
    ctx.song.sync_meta.txt = ctx.out.txt.to_resource_file()
    ctx.song.sync_meta.audio = ctx.out.audio.to_resource_file()
    ctx.song.sync_meta.video = ctx.out.video.to_resource_file()
    ctx.song.sync_meta.cover = ctx.out.cover.to_resource_file()
    ctx.song.sync_meta.background = ctx.out.background.to_resource_file()
    ctx.song.sync_meta.synchronize_to_file()
