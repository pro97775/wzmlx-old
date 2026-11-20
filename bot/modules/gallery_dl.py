#!/usr/bin/env python3
from pyrogram.handlers import MessageHandler
from pyrogram.filters import command

from bot import bot
from bot.helper.telegram_helper.bot_commands import BotCommands
from bot.helper.telegram_helper.filters import CustomFilters
from bot.modules.mirror_leech import _mirror_leech


async def gallery_dl(client, message):
    _mirror_leech(client, message, isGalleryDl=True)


async def gallery_dl_leech(client, message):
    _mirror_leech(client, message, isGalleryDl=True, isLeech=True)


bot.add_handler(MessageHandler(gallery_dl, filters=command(
    BotCommands.GalleryDlCommand) & CustomFilters.authorized & ~CustomFilters.blacklisted))
bot.add_handler(MessageHandler(gallery_dl_leech, filters=command(
    BotCommands.GalleryDlLeechCommand) & CustomFilters.authorized & ~CustomFilters.blacklisted))
