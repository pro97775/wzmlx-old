#!/usr/bin/env python3
from pyrogram.handlers import MessageHandler
from pyrogram.filters import command

from bot import bot, download_dict, queued_dl, queued_up, queue_dict_lock, OWNER_ID, user_data
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.ext_utils.task_manager import start_dl_from_queued, start_up_from_queued
from bot.helper.telegram_helper.bot_commands import BotCommands
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.message_utils import sendMessage


def _parse_args(text):
    args = text.split()[1:]
    status = ''
    if args and args[0] in ('fd', 'fu'):
        status = args[0]
        args = args[1:]
    return status, (args[0] if args else '')


def _find_download(message, gid):
    if gid:
        for uid, download in list(download_dict.items()):
            try:
                if download.gid() == gid:
                    return uid, download
            except Exception:
                continue
        return None, None
    if message.reply_to_message:
        uid = message.reply_to_message.id
        return uid, download_dict.get(uid)
    return None, None


def _is_allowed(user_id, download):
    if user_id == OWNER_ID or user_data.get(user_id, {}).get('is_sudo'):
        return True
    from_user = getattr(getattr(download, 'message', None), 'from_user', None)
    return bool(from_user and from_user.id == user_id)


@new_task
async def force_start(client, message):
    status, gid = _parse_args(message.text)
    uid, download = _find_download(message, gid)
    if download is None:
        await sendMessage(message, 'Task not found. Reply to the task command or pass its GID.')
        return
    if not _is_allowed(message.from_user.id, download):
        await sendMessage(message, 'This task is not for you!')
        return
    started = []
    async with queue_dict_lock:
        if status != 'fu' and uid in queued_dl:
            start_dl_from_queued(uid)
            started.append('download')
        if status != 'fd' and uid in queued_up:
            start_up_from_queued(uid)
            started.append('upload')
    if started:
        await sendMessage(message, f"Task force started from the {' and '.join(started)} queue.")
    else:
        await sendMessage(message, 'This task is not in the selected queue.')


bot.add_handler(MessageHandler(force_start, filters=command(
    BotCommands.ForceStartCommand) & CustomFilters.authorized & ~CustomFilters.blacklisted))
