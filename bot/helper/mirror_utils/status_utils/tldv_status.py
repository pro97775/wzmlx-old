#!/usr/bin/env python3
from bot.helper.ext_utils.bot_utils import EngineStatus, MirrorStatus, get_readable_file_size, get_readable_time


class TldvStatus:
    def __init__(self, obj, listener, gid):
        self.__obj = obj
        self.__listener = listener
        self.upload_details = listener.upload_details
        self.__gid = gid
        self.message = listener.message

    def gid(self):
        return self.__gid

    def processed_bytes(self):
        return get_readable_file_size(self.__obj.downloaded_bytes)

    def size(self):
        return get_readable_file_size(self.__obj.size)

    def status(self):
        return MirrorStatus.STATUS_DOWNLOADING

    def name(self):
        return self.__obj.name or 'tldv'

    def progress(self):
        return f'{round(self.__obj.progress, 2)}%'

    def speed(self):
        return f'{get_readable_file_size(self.__obj.speed)}/s'

    def eta(self):
        try:
            return get_readable_time((self.__obj.size - self.__obj.downloaded_bytes) / self.__obj.speed)
        except (ZeroDivisionError, TypeError):
            return '-'

    def download(self):
        return self.__obj

    def listener(self):
        return self.__listener

    async def cancel_download(self):
        await self.__obj.cancel_download()

    def eng(self):
        return EngineStatus().STATUS_TLDV
