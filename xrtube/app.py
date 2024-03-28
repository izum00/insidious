from __future__ import annotations

import asyncio
import logging as log
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeAlias
from urllib.parse import quote

import appdirs
import httpx
import jinja2
import sass
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket
from fastapi.datastructures import URL
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from watchfiles import awatch

from xrtube.streaming import (
    HLS_ALT_MIME,
    HLS_MIME,
    dash_variant_playlist,
    filter_master_playlist,
    master_playlist,
    sort_master_playlist,
    variant_playlist,
)

from . import NAME
from .markup import yt_to_html
from .pagination import Pagination, RelatedPagination, T
from .utils import httpx_to_fastapi_errors, report
from .ytdl import (
    Channel,
    Format,
    LiveStatus,
    Playlist,
    Video,
    YoutubeClient,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log.basicConfig(level=log.INFO)
log.getLogger("httpx").setLevel(log.WARNING)

CallNext: TypeAlias = Callable[[Request], Awaitable[Response]]

LOADER = jinja2.PackageLoader(NAME, "templates")
ENV = jinja2.Environment(loader=LOADER, autoescape=jinja2.select_autoescape())
TEMPLATES = Jinja2Templates(env=ENV)
scss = resources.read_text(f"{NAME}.style", "main.scss")
css = sass.compile(string=scss, indented=False)
app = FastAPI(default_response_class=HTMLResponse)

app.mount("/static", StaticFiles(packages=[(NAME, "static")]), name="static")
app.mount("/npm", StaticFiles(packages=[(NAME, "npm")]), name="npm")
if os.getenv("UVICORN_RELOAD"):
    # Fix browser reusing cached files at reload despite disk modifications
    StaticFiles.is_not_modified = lambda *_, **_kws: False  # type: ignore

HTTPX = httpx.AsyncClient(follow_redirects=True, headers={
    **YoutubeClient().headers,
    "Referer": "https://www.youtube.com/",
})
MANIFEST_URL = re.compile(r'(^|")(https?://[^"]+?)($|")', re.MULTILINE)
dying = False
RELOAD_PAGE = asyncio.Event()
RELOAD_STYLE = asyncio.Event()

CACHE_DIR = Path(appdirs.user_cache_dir(NAME))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(CACHE_DIR)  # for ytdlp's write/load_pages mechanism


@dataclass(slots=True)
class Index(Generic[T]):
    request: Request
    title: str | None = None
    pagination: Pagination[T] | None = None
    group: Channel | Playlist | None = None
    video: Video | None = None
    get_related: URL | None = None

    @property
    def full_title(self) -> str:
        return f"{self.title or ''} | {NAME}".removeprefix(" | ")

    @property
    def response(self) -> Response:
        enums = {LiveStatus}
        return TEMPLATES.TemplateResponse("index.html.jinja", {
            a: getattr(self, a) for a in dir(self)
            if not a.startswith("_") and a != "response"
        } | {
            e.__name__: e for e in enums
        } | {
            "UVICORN_RELOAD": os.getenv("UVICORN_RELOAD"),
            "no_emoji": "&#xFE0E;",
        })

    @staticmethod
    def local_url(url: str) -> str:
        return str(URL(url).replace(scheme="", netloc=""))

    @staticmethod
    def proxy(url: str, method: str = "get") -> str:
        return f"/proxy/{method}?url={quote(url)}"

    @staticmethod
    def youtube_format(text: str, allow_markup: bool = True) -> str:
        return yt_to_html(text, allow_markup)

    @staticmethod
    def format_duration(seconds: float) -> str:
        wild = "" if seconds < 60 else "*"  # noqa: PLR2004
        text = re.sub(rf"^0:0{wild}", "", str(timedelta(seconds=seconds)))
        return re.sub(r", 0:00:00", "", text)  # e.g. 1 day, 0:00:00


@app.middleware("http")
async def fix_esm_mime(request: Request, call_next: CallNext) -> Response:
    # Chrome refuses to load ESM modules with the text/plain MIME that
    # FastAPI infers from "+esm" filenames
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/npm/") and path.endswith("/+esm"):
        response.headers["content-type"] = "application/javascript"
    return response


@app.get("/")
async def home(request: Request) -> Response:
    return Index(request).response


@app.get("/style.css")
async def style() -> Response:
    return Response(content=css, media_type="text/css")


@app.get("/results")
async def results(request: Request, search_query: str = "") -> Response:
    if (pg := Pagination.get(request).advance()).needs_more_data:
        pg.add(await pg.extender.search(pg.extender.convert_url(request.url)))

    return Index(request, search_query, pg).response


@app.get("/hashtag/{tag}")
async def hashtag(request: Request, tag: str) -> Response:
    if (pg := Pagination.get(request).advance()).needs_more_data:
        url = pg.extender.convert_url(request.url)
        pg.add(await pg.extender.playlist(url))

    return Index(request, f"#{tag}", pg).response


@app.get("/@{id}")
@app.get("/@{id}/{tab}")
@app.get("/c/{id}")
@app.get("/c/{id}/{tab}")
@app.get("/channel/{id}")
@app.get("/channel/{id}/{tab}")
@app.get("/user/{id}")
@app.get("/user/{id}/{tab}")
async def channel(request: Request, tab: str = "featured") -> Response:
    group = None

    if (pg := Pagination.get(request).advance()).needs_more_data:
        url = pg.extender.convert_url(request.url)

        if tab == "featured" and not request.url.path.endswith("/featured"):
            url = url.replace(path=url.path + "/featured")

        try:
            pg.add(group := await pg.extender.channel(url))
        except yt_dlp.DownloadError as e:
            log.warning("%s", e)
            path = url.path.removesuffix(f"/{tab}") + "/featured"
            group = await pg.extender_with(0).channel(url.replace(path=path))
            pg.done = True

    return Index(request, group.title if group else "", pg, group).response


@app.get("/watch")
@app.get("/v/{v}")
@app.get("/shorts/{v}")
async def watch(request: Request) -> Response:
    client = YoutubeClient()
    video = await client.video(client.convert_url(request.url))
    rel = request.url_for("related").include_query_params(
        video_id = video.id,
        video_name = video.title,
        channel_name = video.channel_name,
        channel_url = video.channel_url,
    )
    return Index(request, video.title, video=video, get_related=rel).response


@app.get("/storyboard")
async def storyboard(video_url: str) -> Response:
    # WARN: relying on the implicit caching mechanism here
    text = (await YoutubeClient().video(video_url)).webvtt_storyboard
    return Response(text, media_type="text/vtt")


@app.get("/chapters")
async def chapters(video_url: str) -> Response:
    # WARN: relying on the implicit caching mechanism here
    text = (await YoutubeClient().video(video_url)).webvtt_chapters
    return Response(text, media_type="text/vtt")


@app.get("/related")
async def related(request: Request) -> Response:
    if (pg := RelatedPagination.get(request).advance()).needs_more_data:
        await pg.find()
    return Index(request, pagination=pg).response


@app.get("/generate_hls/master")
async def make_master_m3u8(request: Request, video_url: str) -> Response:
    api = f"{request.base_url}generate_hls/variant?format_json=%s"
    # WARN: relying on the implicit caching mechanism here
    text = master_playlist(api, await YoutubeClient().video(video_url))
    return Response(text, media_type="application/x-mpegURL")


@app.get("/generate_hls/variant")
async def make_variant_m3u8(request: Request, format_json: str) -> Response:
    format = Format.parse_raw(format_json)
    api = f"{request.base_url}proxy/get?url=%s"

    if format.has_dash:
        text = dash_variant_playlist(api, format)
        return Response(text, media_type=HLS_MIME)

    with httpx_to_fastapi_errors():
        async with HTTPX.stream("GET", format.url) as reply:
            mp4_data = reply.aiter_bytes()
            text = await variant_playlist(api % quote(format.url), mp4_data)
            return Response(text, media_type=HLS_MIME)


@app.get("/filter_hls/master")
async def filter_master(content: str, height: int, fps: float) -> Response:
    modified = filter_master_playlist(content, height, fps)
    return Response(modified, media_type=HLS_MIME)


@app.get("/proxy/get", response_class=Response)
async def proxy(
    request: Request, url: str, background_tasks: BackgroundTasks,
) -> Response:
    """GET request runner, fix some content and bypass Same-Origin Policy."""

    def patch_hls_manifest(data: str) -> str:
        """Sort variant streams and proxy all googlevideo URLs to bypass SOP"""
        return sort_master_playlist(MANIFEST_URL.sub(
            lambda m: m[1] + request.url.path + "?url=" + quote(m[2]) + m[3],
            data,
        ))

    headers = {}
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    req = HTTPX.build_request("GET", url, headers=headers)
    with httpx_to_fastapi_errors():
        reply = await HTTPX.send(req, stream=True)
        reply.raise_for_status()

    mime = reply.headers.get("content-type")
    reply_headers = {k: v for k, v in reply.headers.items() if k in {
        "accept-ranges", "content-length", "x-content-type-options",
        # FIXME: breaks video delivery
        # "date", "expires", "cache-control", "age", "etag",
    }}
    if mime in {HLS_MIME, HLS_ALT_MIME}:
        data = await reply.aread()
        data = patch_hls_manifest(data.decode())
        return Response(content=data, media_type=mime)
    if URL(url).path.endswith(".ts"):
        mime = "video/mp2t"

    async def iter() -> AsyncIterator[bytes]:
        with httpx_to_fastapi_errors():
            async for chunk in reply.aiter_bytes():
                yield chunk

    background_tasks.add_task(reply.aclose)
    return StreamingResponse(iter(), 200, reply_headers, mime)


@app.get("/{v}", response_class=RedirectResponse)
async def short_url_watch(request: Request, v: str) -> Response:
    if v == "favicon.ico":
        return Response(status_code=404)

    url = request.url.replace(path="/watch").include_query_params(v=v)
    return RedirectResponse(url)


@app.websocket("/wait_reload")
async def wait_reload(ws: WebSocket) -> None:
    global dying  # noqa: PLW0603
    if dying:
        return

    async def wait_page() -> None:
        while True:
            await RELOAD_PAGE.wait()
            RELOAD_PAGE.clear()
            await ws.send_text("page")

    async def wait_style() -> None:
        global scss  # noqa: PLW0603
        global css  # noqa: PLW0603
        while True:
            await RELOAD_STYLE.wait()
            RELOAD_STYLE.clear()
            scss = resources.read_text(f"{NAME}.style", "main.scss")
            css = sass.compile(string=scss, indented=False)
            await ws.send_text("style")

    await ws.accept()
    with suppress(asyncio.CancelledError):
        await asyncio.gather(wait_page(), wait_style())
    dying = True


@app.websocket("/wait_alive")
async def wait_alive(ws: WebSocket) -> None:
    if not dying:
        await ws.accept()


def create_background_job(coro: Awaitable[None]) -> asyncio.Task[None]:
    async def task() -> None:
        with report(Exception), suppress(asyncio.CancelledError):
            await coro
    return asyncio.create_task(task())


@app.on_event("startup")
async def prune_cache() -> None:
    async def job() -> None:
        while True:
            freed = 0
            for file in CACHE_DIR.glob("*.dump"):
                stats = file.stat()
                if stats.st_mtime < time.time() - 600:
                    file.unlink()
                    freed += stats.st_size

            significant_size = 0.1
            if (mib := freed / 1024 / 1024) >= significant_size:
                log.info("Cache: freed %d MiB", round(mib, 1))

            await asyncio.sleep(300)

    create_background_job(job())


@app.on_event("startup")
async def watch_files() -> None:
    async def job() -> None:
        if not (dir := os.getenv("UVICORN_RELOAD")):
            return

        async for changes in awatch(dir):
            exts = {Path(p).suffix for _, p in changes}
            if ".jinja" in exts or ".js" in exts:
                RELOAD_PAGE.set()
            elif ".scss" in exts or ".css" in exts:
                RELOAD_STYLE.set()

    create_background_job(job())


@app.on_event("shutdown")
async def separate_log() -> None:
    print("─" * shutil.get_terminal_size()[0])
