import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import auto
from functools import lru_cache, partial
from typing import Annotated, Any, ClassVar, Literal, TypeVar
from urllib.parse import parse_qs, quote

import backoff
from fastapi.datastructures import URL
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL

from .utils import AutoStrEnum

T = TypeVar("T")


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


class Format(BaseModel):
    name: str | None = Field(alias="format_note")
    url: str
    filesize: int | None
    manifest_url: str | None
    width: int | None
    height: int | None
    fps: float | None


class Entry(HasThumbnails):
    id: str
    url: str
    title: str


class ShortEntry(Entry):
    entry_type: Literal["ShortEntry"]
    views: int = Field(alias="view_count")


class VideoEntry(Entry):
    entry_type: Literal["VideoEntry"]
    views: int = Field(alias="view_count")
    description: str | None
    duration: int | None
    upload_date: datetime | None = Field(alias="timestamp")
    channel_id: str | None
    channel_name: str | None = Field(alias="channel")
    channel_url: str | None
    uploader_id: str | None
    uploader_name: str | None = Field(alias="uploader")
    uploader_url: str | None
    live_status: LiveStatus | None
    live_release_date: datetime | None = Field(alias="release_timestamp")

    @property
    def release_date(self) -> datetime | None:
        return self.live_release_date or self.upload_date

    @property
    def shortest_channel_url(self) -> str | None:
        if not self.uploader_url:
            return self.channel_url
        if not self.channel_url:
            return None
        return min((self.channel_url, self.uploader_url), key=len)


class PartialEntry(VideoEntry):
    entry_type: Literal["PartialEntry"]
    duration: int | None
    views: int | None = Field(alias="concurrent_view_count")


class PlaylistEntry(Entry):
    entry_type: Literal["PlaylistEntry"]


class ChannelEntry(Entry):
    entry_type: Literal["ChannelEntry"]
    uploader: str
    uploader_id: str
    uploader_url: str
    followers: int = Field(alias="channel_follower_count")

    @property
    def shortest_url(self) -> str | None:
        return min((self.url, self.uploader_url), key=len)


class Entries(BaseModel, Sequence[T]):
    title: str = ""
    entries: list[T] = Field(default_factory=list)

    def __getitem__(self, index: int) -> T:
        return self.entries[index]

    def __len__(self) -> int:
        return len(self.entries)


class SearchLink(BaseModel):
    entry_type: Literal["SearchLink"]
    url: str
    title: str


class Search(Entries[Entry | SearchLink]):
    entries: list[Annotated[
        ShortEntry | VideoEntry | PartialEntry | ChannelEntry | PlaylistEntry |
        SearchLink,
        Field(discriminator="entry_type")
    ]] = Field(default_factory=list)


class Video(VideoEntry):
    entry_type: Literal["Video"] = "Video"
    url: str = Field(alias="original_url")
    width: int
    height: int
    aspect_ratio: float
    upload_date: str
    formats: list[Format]

    @property
    def manifest_url(self) -> str | None:
        gen = (f.manifest_url for f in self.formats)
        return next((f for f in gen if f), None)


class Playlist(Entries[ShortEntry | VideoEntry | PartialEntry]):
    id: str
    entries: list[Annotated[
        ShortEntry | VideoEntry | PartialEntry,
        Field(discriminator="entry_type")
    ]] = Field(default_factory=list)


class Channel(Search, HasThumbnails):
    has_banner: ClassVar[bool] = True

    title: str = Field(alias="channel")
    description: str
    tab: str = Field(alias="webpage_url_basename", default="featured")
    followers: int = Field(alias="channel_follower_count")


class NoDataReceived(Exception):
    """ytdlp failed to return any data after retrying"""


@lru_cache(64)
class YoutubeClient:
    _executor = ThreadPoolExecutor(max_workers=16)

    def __init__(
        self,
        page: int = 1,
        per_page: int = 12,
    ) -> None:
        offset = per_page * (page - 1)
        self._ytdl = YoutubeDL({
            "quiet": True,
            "write_pages": True,
            "load_pages": True,
            "playliststart": offset + 1,
            "playlistend": offset + per_page,
            "extract_flat": "in_playlist",
            "compat_opts": ["no-youtube-unavailable-videos"],
            "extractor_args": {
                # This client has the HLS manifests, no need for others
                "youtube": {"player_client": ["ios"]},
                # Retrieve upload dates in flat playlists
                "youtubetab": {"approximate_date": ["timestamp"]},
            },
        })

    def convert_url(self, url: URL) -> URL:
        return url.replace(scheme="https", hostname="youtube.com", port=None)

    async def search(self, url: URL | str) -> Search:
        return Search.parse_obj(await self._get(url))

    async def channel(self, url: URL | str) -> Channel:
        return Channel.parse_obj(await self._get(url))

    async def playlist(self, url: URL | str) -> Playlist:
        return Playlist.parse_obj(await self._get(url))

    async def video(self, url: URL | str) -> Video:
        return Video.parse_obj(await self._get(url))

    @backoff.on_exception(backoff.expo, NoDataReceived, max_tries=10)
    async def _get(self, url: URL | str) -> dict[str, Any]:
        func = partial(self._ytdl.extract_info, str(url), download=False)
        if (data := await self._thread(func)) is None:
            raise NoDataReceived
        return self._extend_entries(data)

    def _extend_entries(self, data: dict[str, Any]) -> dict[str, Any]:
        def extend(entry: dict[str, Any]) -> dict[str, Any]:
            data = {}
            if "/shorts/" in entry["url"]:
                etype = ShortEntry.__name__
            elif "/channel/" in entry["url"]:
                etype = ChannelEntry.__name__
            elif "/playlist?" in entry["url"]:
                etype = PlaylistEntry.__name__
                data["id"] = entry.get("id") or \
                    parse_qs(URL(entry["url"]).query)["list"][-1]
            elif "/videos?" in entry["url"]:
                etype = SearchLink.__name__
            elif "concurrent_view_count" in entry:
                etype = PartialEntry.__name__
            else:
                etype = VideoEntry.__name__
            return entry | data | {"entry_type": etype}

        return data | {"entries": [extend(e) for e in data.get("entries", [])]}

    @classmethod
    def _thread(cls, *args, **kwargs) -> asyncio.Future:
        exe = cls._executor
        return asyncio.get_event_loop().run_in_executor(exe, *args, **kwargs)
