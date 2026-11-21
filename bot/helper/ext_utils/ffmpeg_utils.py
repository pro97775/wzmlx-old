#!/usr/bin/env python3
from asyncio import create_subprocess_exec
from asyncio.subprocess import DEVNULL
from os import path as ospath, remove, replace, walk
from shlex import split as shlex_split

from bot import LOGGER, bot_cache, download_dict, download_dict_lock
from bot.helper.mirror_utils.status_utils.ffmpeg_status import FfmpegStatus

_VIDEO = {'mkv', 'mp4', 'avi', 'mov', 'webm', 'flv', 'ts', 'm4v', 'wmv', 'mpg', 'mpeg', '3gp', 'm2ts'}
_AUDIO = {'mp3', 'm4a', 'aac', 'flac', 'ogg', 'opus', 'wav', 'wma', 'mka'}


def _files(path):
    if ospath.isfile(path):
        return [path]
    result = []
    for root, _, names in walk(path):
        result.extend(ospath.join(root, name) for name in names)
    return sorted(result)


def _wanted_ext(command):
    for token in shlex_split(command):
        if token.startswith('mltb.'):
            ext = token.split('.', 1)[1].lower()
            if ext == 'video':
                return _VIDEO
            if ext == 'audio':
                return _AUDIO
            return {ext}
    return None


async def run_ffmpeg_commands(path, commands, listener, gid):
    """Run ffmpeg command templates over the downloaded files.

    ``mltb`` in a template is replaced with the file path without extension,
    so ``-i mltb.mkv -c copy mltb.mp4`` converts mkv files only. ``-del``
    removes the original file once the command succeeds.
    """
    files = _files(path)
    if not files:
        return

    status = FfmpegStatus(ospath.basename(path.rstrip('/')), listener, gid)
    async with download_dict_lock:
        download_dict[listener.uid] = status

    for command in commands:
        wanted = _wanted_ext(command)
        parts = shlex_split(command)
        delete_original = '-del' in parts
        parts = [part for part in parts if part != '-del']
        for file_path in _files(path):
            ext = ospath.splitext(file_path)[1].lower().lstrip('.')
            if wanted and ext not in wanted:
                continue
            base = ospath.splitext(file_path)[0]
            args = [part.replace('mltb', base) for part in parts]
            target = args[-1]
            replace_in_place = target == file_path
            if replace_in_place:
                stem, output_ext = ospath.splitext(file_path)
                args[-1] = f'{stem}.ffmpeg{output_ext or "." + ext}'
                target = args[-1]
            status.current_file = ospath.basename(file_path)
            proc = await create_subprocess_exec(bot_cache['pkgs'][2], '-y', *args,
                                                stdout=DEVNULL, stderr=DEVNULL)
            code = await proc.wait()
            if code != 0:
                LOGGER.error(f'ffmpeg command failed for {file_path}: {" ".join(args)}')
                continue
            status.done_files += 1
            if replace_in_place and ospath.exists(target):
                replace(target, file_path)
                continue
            if delete_original and ospath.exists(target) and target != file_path:
                try:
                    remove(file_path)
                except OSError as e:
                    LOGGER.error(f'Could not remove {file_path}: {e}')
