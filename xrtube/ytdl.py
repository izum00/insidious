from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import auto
from functools import partial
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    TypeAlias,
    TypeVar,
    overload,
)
from urllib.parse import parse_qs, quote

import backoff
import yt_dlp
from fastapi.datastructures import URL
from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    field_validator,
)
from typing_extensions import override
from yt_dlp import YoutubeDL

from .utils import AutoStrEnum

T = TypeVar("T")


class NoDataReceived(yt_dlp.utils.ExtractorError):
    def __init__(self) -> None:
        super().__init__("Failed to gather any data from origin site")


class LiveStatus(AutoStrEnum):
    is_upcoming = auto()
    is_live = auto()
    post_live = auto()  # was live, but VOD is not yet processed
    was_live = auto()
    not_live = auto()


class Thumbnail(BaseModel):
    url: str
    id: str | None = None
    width: int | None = None
    height: int | None = None
    preference: int = 0

    @property
    def fixed_url(self) -> str:
        if self.url == "/404":
            return self.url
        url = f"https:{self.url}" if self.url.startswith("//") else self.url
        return f"/proxy/get?url={quote(url)}"

    @property
    def suffix(self) -> str | None:
        if "." not in (path := URL(self.url).path):
            return None
        return path.split(".")[-1]

    @property
    def srcset(self) -> str:
        return f"{self.fixed_url} {self.width or 0}w"


class HasThumbnails(BaseModel):
    has_banner: ClassVar[bool] = False
    thumbnails: list[Thumbnail] = Field(default_factory=list)

    @property
    def best_thumbnail(self) -> Thumbnail:
        return next(iter(self._best_thumbnails()), Thumbnail(url="/404"))

    @property
    def thumbnails_srcset(self) -> str:
        return ", ".join(reversed([t.srcset for t in self._best_thumbnails()]))

    @property
    def banners_srcset(self) -> str:
        return ", ".join([t.srcset for t in self._best_thumbnails(True)][::-1])

    def _best_thumbnails(self, banners: bool = False) -> list[Thumbnail]:
        thumbs = self.thumbnails

        if self.has_banner:
            thumbs = [t for t in thumbs if banners == (t.preference < 0)]

        thumbs = (
            [t for t in thumbs if t.suffix == "webp" and t.width] or
            [t for t in thumbs if t.width] or
            [t for t in thumbs if t.suffix == "webp"] or
            thumbs
        )
        thumbs.sort(key=lambda t: t.width or 0, reverse=True)
        return thumbs


class HasHoverThumbnails(BaseModel):
    id: str

    @property
    def hover_srcsets(self) -> list[str]:
        return [
            ", ".join(quality.srcset for quality in nth)
            for nth in reversed(self._hover_thumbnails())
        ]

    def _hover_thumbnails(self) -> list[list[Thumbnail]]:
        def url(q: str, id: int) -> str:
            return f"https://i.ytimg.com/vi/{self.id}/{q}{id}.jpg"

        return [[
            Thumbnail(url=url("hq", id), width=480, height=360),
            Thumbnail(url=url("mq", id), width=320, height=180),
            Thumbnail(url=url("", id), width=120, height=90),
        ] for id in (1, 2, 3)]


class HasChannel(BaseModel):
    channel_id: str | None = None
    channel_name: str | None = Field(None, alias="channel")
    channel_url: str | None = None
    channel_followers: int | None = \
        Field(default=None, alias="channel_follower_count")
    uploader_id: str | None = None
    uploader_name: str | None = Field(None, alias="uploader")
    uploader_url: str | None = None

    @property
    def shortest_channel_url(self) -> str | None:
        if not self.uploader_url:
            return self.channel_url
        if not self.channel_url:
            return None
        return min((self.channel_url, self.uploader_url), key=len)


class Fragments(BaseModel):
    url: str | None = None
    path: str | None = None
    duration: float | None = None


class Format(BaseModel):
    id: str = Field(alias="format_id")
    name: str | None = Field(None, alias="format_note")
    protocol: str
    url: str
    manifest_url: str | None = None
    dash_fragments_base_url: str | None = \
        Field(None, alias="fragment_base_url")
    fragments: list[Fragments] = Field(alias="fragments", default_factory=list)
    rows: int | None = None
    columns: int | None = None
    filesize: int | None = None
    container: str | None = None
    video_codec: str | None = Field(None, alias="vcodec")
    audio_codec: str | None = Field(None, alias="acodec")
    average_bitrate: float | None = Field(None, alias="tbr")  # in KB/s
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    dynamic_range: str | None = None
    audio_channels: int | None = None
    language: str | None = None

    @property
    def has_dash(self) -> bool:
        return self.protocol == "http_dash_segments"

    @property
    def vcodec(self) -> str | None:
        if self.video_codec == "none":
            return None
        return self.video_codec or None

    @property
    def acodec(self) -> str | None:
        if self.audio_codec == "none":
            return None
        return self.audio_codec or None


class Chapter(BaseModel):
    start_sec: float = Field(alias="start_time")
    end_sec: float = Field(alias="end_time")
    title: str


class Entry(HasThumbnails):
    id: str
    url: str
    title: str
    index: int | None = None


class ShortEntry(Entry, HasHoverThumbnails):
    entry_type: Literal["ShortEntry"]
    views: int = Field(alias="view_count")


class VideoEntry(Entry, HasHoverThumbnails, HasChannel):
    entry_type: Literal["VideoEntry"]
    views: int | None = Field(None, alias="view_count")
    description: str | None = None
    duration: int | None = None
    upload_date: datetime | None = \
        Field(None, validation_alias=AliasChoices("timestamp", "upload_date"))
    live_status: LiveStatus | None = None
    live_release_date: datetime | None = Field(None, alias="release_timestamp")

    @field_validator("upload_date", mode="before")
    @classmethod
    def parse_upload_date(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, int):
            return datetime.fromtimestamp(value)
        return datetime.strptime(value, "%Y%m%d")

    @property
    def release_date(self) -> datetime | None:
        return self.live_release_date or self.upload_date

    @property
    def dislikes_url(self) -> str:
        return "/dislikes?video_id=%s" % self.id


class PartialEntry(VideoEntry):
    entry_type: Literal["PartialEntry"]  # type: ignore
    duration: int | None = None
    views: int | None = Field(None, alias="concurrent_view_count")


class PlaylistEntry(Entry):
    entry_type: Literal["PlaylistEntry"]

    @property
    def load_url(self) -> str | None:
        return "/load_playlist_entry?url=%s" % quote(self.url)


class ChannelEntry(Entry):
    entry_type: Literal["ChannelEntry"]
    uploader: str
    uploader_id: str
    uploader_url: str
    followers: int | None = Field(None, alias="channel_follower_count")

    @property
    def shortest_url(self) -> str | None:
        return min((self.url, self.uploader_url), key=len)


# NOTE: Inherit Sequence first to avoid BaseModel overriding __iter__
class Entries(Sequence[T], BaseModel):
    title: str = ""
    entries: list[T] = Field(default_factory=list)

    @overload
    def __getitem__(self, index: int) -> T: ...
    @overload
    def __getitem__(self, index: slice) -> list[T]: ...
    @override
    def __getitem__(self, index: int | slice) -> T | list[T]:
        return self.entries[index]

    @override
    def __len__(self) -> int:
        return len(self.entries)


class SearchLink(BaseModel):
    entry_type: Literal["SearchLink"]
    url: str
    title: str

    @property
    def load_url(self) -> str:
        params = (quote(self.url), quote(self.title))
        return "/load_search_link?url=%s&title=%s" % params


InSearch: TypeAlias = (
    ShortEntry | VideoEntry | PartialEntry | ChannelEntry | PlaylistEntry |
    SearchLink
)


class Search(Entries[InSearch]):
    url: str = Field(alias="original_url")
    entries: list[Annotated[InSearch, Field(discriminator="entry_type")]] = \
        Field(default_factory=list)


class Video(VideoEntry):
    entry_type: Literal["Video"] = "Video"  # type: ignore
    url: str = Field(alias="original_url")
    width: int
    height: int
    aspect_ratio: float
    fps: float
    likes: int | None = Field(alias="like_count")
    formats: list[Format]
    chapters: list[Chapter] | None = None

    @property
    def manifest_url(self) -> str:
        for fmt in self.formats:
            if fmt.manifest_url and not fmt.has_dash:
                return "/proxy/get?url=%s" % quote(fmt.manifest_url)
        return "/generate_hls/master?video_url=%s" % quote(self.url)

    @property
    def storyboard_url(self) -> str:
        return "/storyboard?video_url=%s" % quote(self.url)

    @property
    def chapters_url(self) -> str:
        return "/chapters?video_url=%s" % quote(self.url)

    @property
    def webvtt_storyboard(self) -> str:
        return "\n".join(self._webvtt_storyboard())

    @property
    def webvtt_chapters(self) -> str:
        return "\n".join(self._webvtt_chapters())

    @staticmethod
    def _vtt_time(s: float) -> str:
        h = s // 3600
        s -= h * 3600
        m = s // 60
        s -= m * 60
        return f"{h:02.0f}:{m:02.0f}:{s:06.3f}"  # e.g. 00:03:22.067

    def _webvtt_chapters(self) -> Iterator[str]:
        yield "WEBVTT"

        for i, chapter in enumerate(self.chapters or [], 1):
            end = self._vtt_time(chapter.end_sec)
            yield ""
            yield str(i)
            yield self._vtt_time(chapter.start_sec) + " --> " + end
            yield chapter.title

    def _webvtt_storyboard(self) -> Iterator[str]:
        yield "WEBVTT"

        variants = [f for f in self.formats if f.name == "storyboard"]
        sb = max(variants, key=lambda f: f.height or 0, default=None)
        if not sb or not sb.fragments:
            return

        frag_duration = sb.fragments[0].duration or 0
        sec_per_thumb = frag_duration / (sb.columns or 1) / (sb.rows or 1)
        max_sec = sum(f.duration or 0 for f in sb.fragments)
        now = 0

        for frag in sb.fragments:
            for row in range(sb.rows or 0):
                for col in range(sb.columns or 0):
                    end = now + sec_per_thumb
                    yield self._vtt_time(now) + " --> " + self._vtt_time(end)

                    xywh = ",".join(map(str, (
                        (sb.width or 0) * col,
                        (sb.height or 0) * row,
                        sb.width or 0,
                        sb.height or 0,
                    )))
                    yield f"/proxy/get?url={quote(frag.url or '')}#xywh={xywh}"

                    if (now := end) >= max_sec:
                        return


InPlaylist: TypeAlias = ShortEntry | VideoEntry | PartialEntry


class Playlist(
    PlaylistEntry, HasHoverThumbnails, HasChannel, Entries[InPlaylist],
):
    entry_type: Literal["Playlist"] = "Playlist"  # type: ignore
    url: str = Field(alias="original_url")
    description: str | None = Field(None)
    last_change: datetime | None = Field(None, alias="modified_date")
    views: int | None = Field(None, alias="view_count")
    total_entries: int | None = Field(None, alias="playlist_count")
    entries: list[Annotated[InPlaylist, Field(discriminator="entry_type")]] = \
        Field(default_factory=list)

    @field_validator("last_change", mode="before")
    @classmethod
    def parse_last_change(cls, value: Any) -> datetime | None:
        return None if value is None else datetime.strptime(value, "%Y%m%d")

    @property
    @override
    def banners_srcset(self) -> str:
        return ""  # this is just gonna be the upscaled first vid's thumbnail

    @property
    @override
    def load_url(self) -> str | None:
        return None

    @property
    @override
    def hover_srcsets(self) -> list[str]:
        if len(self) < 3:  # noqa: PLR2004
            return self[0].hover_srcsets
        return [entry.thumbnails_srcset for entry in self[1:6]]


InChannel: TypeAlias = InSearch


class Channel(Search, HasThumbnails):
    has_banner: ClassVar[bool] = True
    tabs: ClassVar[list[str]] = ["featured", "videos", "shorts", "playlists"]

    title: str = Field(alias="channel")
    description: str
    tab: str = Field(alias="webpage_url_basename", default="featured")
    followers: int | None = Field(None, alias="channel_follower_count")

    def tab_url(self, from_url: URL, to_tab: str) -> URL:
        path = from_url.path.rstrip("/")

        for tab in self.tabs:
            path = path.removesuffix(f"/{tab}")

        path = f"{path}/{to_tab}".removesuffix("/featured")
        return from_url.replace(path=path)


class YoutubeClient:
    _ytdl_instances: ClassVar[dict[tuple[int, int], YoutubeDL]] = {}
    _executor = ThreadPoolExecutor(max_workers=16)

    def __init__(self, page: int = 1, per_page: int = 12) -> None:
        super().__init__()
        self._offset = per_page * (page - 1)

        self._ytdl = self._ytdl_instances.get((page, per_page)) or YoutubeDL({
            "quiet": True,
            "write_pages": True,
            "load_pages": True,
            "playliststart": self._offset + 1,
            "playlistend": self._offset + per_page,
            "extract_flat": "in_playlist",
            "compat_opts": ["no-youtube-unavailable-videos"],
            "extractor_args": {
                # This client has the HLS manifests, no need for others
                "youtube": {"player_client": ["ios"]},
                # Retrieve upload dates in flat playlists
                "youtubetab": {"approximate_date": ["timestamp"]},
            },
        })

    @property
    def headers(self) -> dict[str, str]:
        return self._ytdl.params["http_headers"]

    async def search(self, url: URL | str) -> Search:
        return Search.model_validate(await self._get(url))

    async def channel(self, url: URL | str) -> Channel:
        return Channel.model_validate(await self._get(url))

    async def playlist(self, url: URL | str) -> Playlist:
        pl = Playlist.model_validate(await self._get(url))
        for i, entry in enumerate(pl, 1):
            entry.index = self._offset + i
            entry.url = str(URL(entry.url).include_query_params(
                list = pl.id,
                index = entry.index,
            ))
        return pl

    async def video(self, url: URL | str) -> Video:
        url = URL(str(url)).remove_query_params("list")
        return Video.model_validate(await self._get(url))

    @backoff.on_exception(backoff.expo, NoDataReceived, max_tries=10)
    async def _get(self, url: URL | str) -> dict[str, Any]:
        func = partial(self._ytdl.extract_info, str(url), download=False)
        if (data := await self._thread(func)) is None:
            raise NoDataReceived
        return self._extend_entries(data)

    @classmethod
    def _thread(cls, fn: Callable[[], T]) -> asyncio.Future[T]:
        return asyncio.get_event_loop().run_in_executor(cls._executor, fn)

    @staticmethod
    def convert_url(url: URL) -> URL:
        return url.replace(scheme="https", hostname="youtube.com", port=None)

    @staticmethod
    def _extend_entries(data: dict[str, Any]) -> dict[str, Any]:
        def extend(entry: dict[str, Any]) -> dict[str, Any]:
            data = {}
            tabs = [f"/{name}?" for name in Channel.tabs]
            if "/shorts/" in entry["url"]:
                etype = ShortEntry.__name__
            elif "/channel/" in entry["url"]:
                etype = ChannelEntry.__name__
            elif "/playlist?" in entry["url"]:
                etype = PlaylistEntry.__name__
                data["id"] = entry.get("id") or \
                    parse_qs(URL(entry["url"]).query)["list"][-1]
            elif any(name in entry["url"] for name in tabs):
                etype = SearchLink.__name__
            elif "concurrent_view_count" in entry:
                etype = PartialEntry.__name__
            else:
                etype = VideoEntry.__name__
            return entry | data | {"entry_type": etype}

        return data | {"entries": [extend(e) for e in data.get("entries", [])]}
