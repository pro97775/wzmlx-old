#!/usr/bin/env python3
from bot.helper.ext_utils.bot_utils import EngineStatus, MirrorStatus, get_readable_file_size


class FfmpegStatus:
    def __init__(self, name, listener, gid):
        self.__name = name
        self.__listener = listener
        self.__gid = gid
        self.upload_details = listener.upload_details
        self.message = listener.message
        self.done_files = 0
        self.current_file = ''

    def gid(self):
        return self.__gid

    def name(self):
        return self.__name

    def size(self):
        return 'N/A'

    def progress(self):
        return f'{self.done_files} file(s)'

    def processed_bytes(self):
        return get_readable_file_size(0)

    def speed(self):
        return '0B/s'

    def eta(self):
        return '-'

    def status(self):
        return MirrorStatus.STATUS_PROCESSING

    def download(self):
        return self

    def listener(self):
        return self.__listener

    async def cancel_download(self):
        await self.__listener.onUploadError('ffmpeg processing stopped!')

    def eng(self):
        return EngineStatus().STATUS_FFMPEG
