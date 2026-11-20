#!/usr/bin/env python3
from asyncio import Semaphore, gather, sleep, create_subprocess_exec
from asyncio.subprocess import DEVNULL
from os import path as ospath
from re import match as re_match, sub as re_sub
from secrets import token_hex
from shutil import rmtree
from time import time

from aiofiles import open as aiopen
from aiofiles.os import makedirs, path as aiopath, remove as aioremove
from aiohttp import ClientSession, TCPConnector

from bot import config_dict, download_dict, download_dict_lock, non_queued_dl, queue_dict_lock, bot_cache
from bot.helper.ext_utils.bot_utils import sync_to_async
from bot.helper.ext_utils.task_manager import is_queued
from bot.helper.mirror_utils.status_utils.queue_status import QueueStatus
from bot.helper.mirror_utils.status_utils.tldv_status import TldvStatus
from bot.helper.telegram_helper.message_utils import sendStatusMessage

_TLDV_LINK = r'https?://(?:app\.)?tldv\.io/(?:app/meetings|watch)/([a-zA-Z0-9]+)'
_CONF_PREFIX = '#TLDVCONF:'
_SEGMENT_CONCURRENCY = 6
TLDV_GW = 'https://gw.tldv.io'
TLDV_GAIA = 'https://gaia.tldv.io'


def is_tldv_link(link):
    return bool(re_match(_TLDV_LINK, link or ''))


def caesar_decipher(text, offset):
    shift = ((offset % 26) + 26) % 26
    out = []
    for char in text:
        code = ord(char)
        if 65 <= code <= 90:
            out.append(chr(((code - 65 + shift) % 26) + 65))
        elif 97 <= code <= 122:
            out.append(chr(((code - 97 + shift) % 26) + 97))
        else:
            out.append(char)
    return ''.join(out)


def parse_tldv_conf(line):
    rest = line[len(_CONF_PREFIX):]
    first = rest.find(',')
    second = rest.find(',', first + 1) if first != -1 else -1
    if first == -1 or second == -1:
        raise ValueError(f'Malformed TLDVCONF: {line}')
    return rest[:first], int(rest[first + 1:second]), rest[second + 1:]


def parse_playlist(content):
    offset = base_url = None
    segments = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(_CONF_PREFIX):
            _, offset, base_url = parse_tldv_conf(line)
            continue
        if line.startswith('#'):
            continue
        if offset is None or base_url is None:
            raise ValueError('Segment line found before TLDVCONF header')
        segments.append(base_url + caesar_decipher(line, offset))
    return segments


class TldvDownloader:
    def __init__(self, listener):
        self.__listener = listener
        self.__gid = token_hex(5)
        self.name = ''
        self.__size = 0
        self.__downloaded = 0
        self.__speed = 0
        self.__start = 0
        self.__completed = 0
        self.__total = 0
        self.__is_cancelled = False

    @property
    def gid(self):
        return self.__gid

    @property
    def downloaded_bytes(self):
        return self.__downloaded

    @property
    def size(self):
        if self.__completed and self.__total:
            return int((self.__downloaded / self.__completed) * self.__total)
        return self.__size

    @property
    def speed(self):
        return self.__speed

    @property
    def progress(self):
        size = self.size
        return (self.__downloaded / size) * 100 if size else 0

    async def __on_download_start(self, from_queue=False):
        async with download_dict_lock:
            download_dict[self.__listener.uid] = TldvStatus(self, self.__listener, self.__gid)
        if not from_queue:
            await self.__listener.onDownloadStart()
            await sendStatusMessage(self.__listener.message)

    async def __segment(self, client, sem, url, index, temp_dir, token):
        async with sem:
            headers = {'Authorization': f'Bearer {token}'} if token else {}
            for attempt in range(3):
                if self.__is_cancelled:
                    return
                try:
                    async with client.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            raise Exception(f'HTTP {resp.status}')
                        target = ospath.join(temp_dir, f'seg_{index:05d}.ts')
                        async with aiopen(target, 'wb') as fh:
                            async for chunk in resp.content.iter_chunked(32768):
                                if self.__is_cancelled:
                                    return
                                await fh.write(chunk)
                                self.__downloaded += len(chunk)
                        self.__completed += 1
                        elapsed = time() - self.__start
                        if elapsed:
                            self.__speed = self.__downloaded / elapsed
                        return
                except Exception:
                    if attempt == 2:
                        raise
                    await sleep(1)

    async def download(self, link, path):
        self.__start = time()
        meeting = re_match(_TLDV_LINK, link)
        if not meeting:
            return await self.__listener.onDownloadError('Invalid tldv.io URL format')
        meeting_id = meeting.group(1)
        token = config_dict.get('TLDV_TOKEN', '')
        headers = {'Authorization': f'Bearer {token}'} if token else {}

        async with ClientSession(connector=TCPConnector(ssl=False), headers=headers) as client:
            try:
                async with client.get(f'{TLDV_GW}/v1/meetings/{meeting_id}/watch-page?noTranscript=true') as resp:
                    if resp.status in (401, 403):
                        return await self.__listener.onDownloadError('Authentication required. Set TLDV_TOKEN.')
                    if resp.status != 200:
                        return await self.__listener.onDownloadError(f'Failed to fetch metadata (HTTP {resp.status})')
                    meta = await resp.json()
                async with client.get(f'{TLDV_GAIA}/v1/meetings/{meeting_id}/playlist.m3u8') as resp:
                    if resp.status != 200:
                        return await self.__listener.onDownloadError(f'Failed to fetch playlist (HTTP {resp.status})')
                    playlist = await resp.text()
            except Exception as e:
                return await self.__listener.onDownloadError(f'tldv request failed: {e}')

            meeting_data = meta.get('meeting') or {}
            name = meeting_data.get('name') or 'Untitled Meeting'
            created = meeting_data.get('createdAt') or ''
            date_match = re_match(r'^(\d{4}-\d{2}-\d{2})', created)
            date_str = date_match.group(1) + '_' if date_match else ''
            safe_name = re_sub(r'[\\/*?:"<>|]', '', name).replace(' ', '_')
            self.name = f'{date_str}{safe_name}.mp4'

            try:
                segments = parse_playlist(playlist)
            except Exception as e:
                return await self.__listener.onDownloadError(f'Failed to parse playlist: {e}')
            if not segments:
                return await self.__listener.onDownloadError('No segments found in the playlist')
            self.__total = len(segments)

            temp_dir = ospath.join(path, '_tldv_temp')
            await makedirs(temp_dir, exist_ok=True)
            sem = Semaphore(_SEGMENT_CONCURRENCY)
            try:
                await gather(*(self.__segment(client, sem, url, i, temp_dir, token)
                               for i, url in enumerate(segments)))
            except Exception as e:
                await self.__listener.onDownloadError(f'Error downloading segments: {e}')
                await sync_to_async(rmtree, temp_dir, ignore_errors=True)
                return
            if self.__is_cancelled:
                await sync_to_async(rmtree, temp_dir, ignore_errors=True)
                return

            merged = ospath.join(path, 'output.ts')
            try:
                async with aiopen(merged, 'wb') as out:
                    for i in range(self.__total):
                        part = ospath.join(temp_dir, f'seg_{i:05d}.ts')
                        if not await aiopath.exists(part):
                            raise Exception(f'Missing segment {i}')
                        async with aiopen(part, 'rb') as src:
                            await out.write(await src.read())
            except Exception as e:
                await self.__listener.onDownloadError(f'Failed to merge segments: {e}')
                await sync_to_async(rmtree, temp_dir, ignore_errors=True)
                if await aiopath.exists(merged):
                    await aioremove(merged)
                return
            await sync_to_async(rmtree, temp_dir, ignore_errors=True)

            final = ospath.join(path, self.name)
            proc = await create_subprocess_exec(
                bot_cache['pkgs'][2], '-y', '-i', merged, '-c', 'copy', '-movflags', '+faststart', final,
                stdout=DEVNULL, stderr=DEVNULL)
            code = await proc.wait()
            await aioremove(merged)
            if code != 0:
                return await self.__listener.onDownloadError('ffmpeg failed to remux the recording')
            self.__size = await aiopath.getsize(final)
            await self.__listener.onDownloadComplete()

    async def add_download(self, link, path):
        await self.__on_download_start()
        added_to_queue, event = await is_queued(self.__listener.uid)
        if added_to_queue:
            async with download_dict_lock:
                download_dict[self.__listener.uid] = QueueStatus('tldv', 0, self.__gid, self.__listener, 'dl')
            await event.wait()
            async with download_dict_lock:
                if self.__listener.uid not in download_dict:
                    return
            await self.__on_download_start(True)
        async with queue_dict_lock:
            non_queued_dl.add(self.__listener.uid)
        await self.download(link, path)

    async def cancel_download(self):
        self.__is_cancelled = True
        await self.__listener.onDownloadError('Download Cancelled by User!')


async def add_tldv_download(link, path, listener):
    await TldvDownloader(listener).add_download(link, path)
