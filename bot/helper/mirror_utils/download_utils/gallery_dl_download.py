#!/usr/bin/env python3
from os import path as ospath, walk, listdir
from re import sub as re_sub
from secrets import token_hex
from time import time
from logging import getLogger, Handler, ERROR

from gallery_dl import config as gdl_config
from gallery_dl import extractor
from gallery_dl.job import DownloadJob
from gallery_dl.output import NullOutput

from bot import download_dict, download_dict_lock, non_queued_dl, queue_dict_lock
from bot.helper.telegram_helper.message_utils import sendStatusMessage
from bot.helper.mirror_utils.status_utils.gallery_dl_status import GalleryDlStatus
from bot.helper.mirror_utils.status_utils.queue_status import QueueStatus
from bot.helper.ext_utils.bot_utils import sync_to_async, async_to_sync
from bot.helper.ext_utils.task_manager import is_queued

LOGGER = getLogger(__name__)


def is_gallery_dl_link(link):
    try:
        return extractor.find(link) is not None
    except Exception:
        return False


class GalleryDlLogHandler(Handler):
    def __init__(self):
        super().__init__(level=ERROR)
        self.errors = []

    def emit(self, record):
        self.errors.append(record.getMessage())


class GalleryDlOutput(NullOutput):
    def __init__(self, obj):
        self.__obj = obj

    def start(self, path):
        self.__obj.current_file = path

    def skip(self):
        self.__obj.skipped_count += 1

    def success(self, path):
        try:
            if ospath.exists(path):
                self.__obj.downloaded_bytes += ospath.getsize(path)
        except OSError:
            pass
        self.__obj.completed_count += 1
        self.__obj.current_file = ''


class GalleryDlHelper:
    def __init__(self, listener):
        self.__listener = listener
        self.__gid = ''
        self.__path = ''
        self.__size = 0
        self.__downloaded_bytes = 0
        self.__download_speed = 0
        self.__last_check_time = 0
        self.__last_check_bytes = 0
        self.__is_cancelled = False
        self.completed_count = 0
        self.skipped_count = 0
        self.current_file = ''
        self.name = ''

    @property
    def downloaded_bytes(self):
        total = self.__downloaded_bytes
        if self.current_file:
            try:
                part = f'{self.current_file}.part'
                if ospath.exists(part):
                    total += ospath.getsize(part)
            except OSError:
                pass
        return total

    @downloaded_bytes.setter
    def downloaded_bytes(self, value):
        self.__downloaded_bytes = value

    @property
    def size(self):
        return max(self.__scan_size(self.__path), self.__downloaded_bytes)

    @property
    def progress(self):
        size = self.size
        return (self.downloaded_bytes / size) * 100 if size else 0

    @property
    def download_speed(self):
        now = time()
        elapsed = now - self.__last_check_time
        if elapsed > 0.5:
            current = self.downloaded_bytes
            self.__download_speed = (current - self.__last_check_bytes) / elapsed
            self.__last_check_time = now
            self.__last_check_bytes = current
        return self.__download_speed

    @staticmethod
    def __scan_size(path):
        total = 0
        if path and ospath.isdir(path):
            for dirpath, _, filenames in walk(path):
                for file_name in filenames:
                    try:
                        total += ospath.getsize(ospath.join(dirpath, file_name))
                    except OSError:
                        pass
        return total

    def __on_download_error(self, error):
        self.__is_cancelled = True
        async_to_sync(self.__listener.onDownloadError, error)

    async def __on_download_start(self, from_queue=False):
        async with download_dict_lock:
            download_dict[self.__listener.uid] = GalleryDlStatus(self, self.__listener, self.__gid)
        if not from_queue:
            await self.__listener.onDownloadStart()
            await sendStatusMessage(self.__listener.message)

    def __download(self, link, path):
        error_handler = GalleryDlLogHandler()
        gdl_logger = getLogger('gallery-dl')
        gdl_logger.addHandler(error_handler)
        try:
            gdl_config.clear()
            gdl_config.set((), 'base-directory', path)
            gdl_config.set((), 'directory', [])
            gdl_config.set(('output',), 'mode', 'null')
            gdl_config.set(('output',), 'skip', False)
            gdl_config.set(('downloader',), 'retries', 10)
            gdl_config.set(('downloader', 'http'), 'timeout', 30)
            gdl_config.set(('downloader',), 'part', True)
            gdl_config.set(('extractor',), 'retries', 10)
            gdl_config.set(('extractor',), 'timeout', 30)
            if ospath.exists('cookies.txt'):
                gdl_config.set(('extractor',), 'cookies', 'cookies.txt')

            if self.__is_cancelled:
                return

            job = DownloadJob(link)
            job.out = GalleryDlOutput(self)
            original_handle_url = job.handle_url

            def patched_handle_url(url, kwdict):
                if self.__is_cancelled:
                    raise ValueError('Cancelled by user')
                return original_handle_url(url, kwdict)

            job.handle_url = patched_handle_url
            self.__last_check_time = time()
            self.__last_check_bytes = 0
            status = job.run()

            if self.__is_cancelled:
                return

            if not ospath.exists(path) or not listdir(path):
                self.__on_download_error('No files downloaded. Check if the link is valid.')
                return

            if status != 0 and self.completed_count == 0:
                error_msg = '\n'.join(error_handler.errors) or f'gallery-dl failed with status code: {status}'
                self.__on_download_error(error_msg)
                return

            LOGGER.info(f'Gallery-dl Download Completed: {self.name}')
            async_to_sync(self.__listener.onDownloadComplete)
        except Exception as e:
            if not self.__is_cancelled:
                self.__on_download_error(str(e))
        finally:
            gdl_logger.removeHandler(error_handler)

    async def add_download(self, link, path, name):
        self.__gid = token_hex(5)
        self.name = re_sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name or link.rstrip('/').split('/')[-1])[:120] or 'gallery-dl'
        self.__path = ospath.join(path, self.name)

        await self.__on_download_start()

        added_to_queue, event = await is_queued(self.__listener.uid)
        if added_to_queue:
            LOGGER.info(f'Added to Queue/Download: {self.name}')
            async with download_dict_lock:
                download_dict[self.__listener.uid] = QueueStatus(self.name, 0, self.__gid, self.__listener, 'dl')
            await event.wait()
            async with download_dict_lock:
                if self.__listener.uid not in download_dict:
                    return
            LOGGER.info(f'Start Queued Download from gallery-dl: {self.name}')
            await self.__on_download_start(True)
        else:
            LOGGER.info(f'Download with gallery-dl: {self.name}')

        async with queue_dict_lock:
            non_queued_dl.add(self.__listener.uid)

        await sync_to_async(self.__download, link, self.__path)

    async def cancel_download(self):
        self.__is_cancelled = True
        LOGGER.info(f'Cancelling Download: {self.name}')
        await self.__listener.onDownloadError('Download Cancelled by User!')


async def add_gallery_dl_download(link, path, listener, name):
    await GalleryDlHelper(listener).add_download(link, path, name)
