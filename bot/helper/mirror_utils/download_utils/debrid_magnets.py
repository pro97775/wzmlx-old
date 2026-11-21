#!/usr/bin/env python3
"""Resolve magnet links through AllDebrid or TorBox into direct download links."""
from asyncio import gather, sleep
from os import path as ospath
from time import time

from httpx import AsyncClient, HTTPError

from bot.helper.ext_utils.exceptions import DirectDownloadLinkException

AD_AGENT = 'wzmlx'
AD_API = 'https://api.alldebrid.com/v4'
AD_API_V4_1 = 'https://api.alldebrid.com/v4.1'
AD_READY = 4

TB_API = 'https://api.torbox.app/v1/api'

_POLL_INTERVAL = 5
_MAX_WAIT = 7200


def is_magnet_link(link):
    return link.startswith('magnet:?') if link else False


def _title_from_magnet(magnet):
    for part in magnet.split('&'):
        if part.startswith('dn='):
            from urllib.parse import unquote_plus
            return unquote_plus(part[3:]).strip() or 'Debrid Download'
    return 'Debrid Download'


def _flatten_nodes(nodes, parent=''):
    for node in nodes or []:
        name = node.get('n') or node.get('name') or 'file'
        path = f'{parent}/{name}' if parent else name
        if node.get('e'):
            yield from _flatten_nodes(node['e'], path)
        elif node.get('l') or node.get('link'):
            yield {'name': name, 'path': path, 'size': int(node.get('s') or node.get('size') or 0),
                   'link': node.get('l') or node.get('link')}


async def _ad_post(client, path, api_key, base=None, **kwargs):
    base = base or AD_API
    params = {'agent': AD_AGENT, 'apikey': api_key}
    try:
        resp = await client.post(f'{base}{path}', params=params, **kwargs)
    except HTTPError as e:
        raise DirectDownloadLinkException(f'ERROR: AllDebrid request failed: {e}') from e
    try:
        payload = resp.json()
    except ValueError:
        raise DirectDownloadLinkException(f'ERROR: AllDebrid returned {resp.status_code}')
    if payload.get('status') != 'success':
        error = payload.get('error') or {}
        raise DirectDownloadLinkException(
            f"ERROR: AllDebrid {error.get('code') or resp.status_code}: "
            f"{error.get('message') or 'request failed'}")
    return payload.get('data') or {}


async def alldebrid_resolve_magnet(magnet, api_key, cancelled=None):
    if not api_key:
        raise DirectDownloadLinkException('ERROR: ALLDEBRID_API is not set')
    async with AsyncClient(timeout=60) as client:
        data = await _ad_post(client, '/magnet/upload', api_key, data={'magnets[]': magnet})
        entries = data.get('magnets') or []
        if not entries or 'error' in entries[0]:
            raise DirectDownloadLinkException('ERROR: AllDebrid rejected the magnet')
        magnet_id = entries[0].get('id')
        started = time()
        while True:
            status = await _ad_post(client, '/magnet/status', api_key, base=AD_API_V4_1,
                                    data={'id': str(magnet_id)})
            info = status.get('magnets')
            if isinstance(info, list):
                info = info[0] if info else {}
            info = info or {}
            code = info.get('statusCode')
            if code == AD_READY:
                break
            if code in range(5, 16):
                raise DirectDownloadLinkException(
                    f"ERROR: AllDebrid magnet failed: {info.get('status') or code}")
            if cancelled and cancelled():
                raise DirectDownloadLinkException('ERROR: magnet resolve cancelled')
            if time() - started > _MAX_WAIT:
                raise DirectDownloadLinkException('ERROR: AllDebrid magnet timed out')
            await sleep(_POLL_INTERVAL)

        files = await _ad_post(client, '/magnet/files', api_key, data={'id[]': str(magnet_id)})
        magnet_files = (files.get('magnets') or [{}])[0].get('files') or []
        nodes = list(_flatten_nodes(magnet_files))
        if not nodes:
            raise DirectDownloadLinkException('ERROR: AllDebrid returned no files')

        contents = []
        total_size = 0

        async def unlock(node):
            nonlocal total_size
            unlocked = await _ad_post(client, '/link/unlock', api_key, data={'link': node['link']})
            url = unlocked.get('link')
            if url:
                size = int(unlocked.get('filesize') or node.get('size') or 0)
                contents.append({
                    'path': ospath.dirname(node.get('path') or ''),
                    'filename': node['name'],
                    'url': url,
                })
                total_size += size

        await gather(*(unlock(node) for node in nodes))
        if not contents:
            raise DirectDownloadLinkException('ERROR: AllDebrid returned no download links')
        return {'title': _title_from_magnet(magnet), 'total_size': total_size, 'contents': contents}


async def torbox_resolve_magnet(magnet, token, cancelled=None):
    if not token:
        raise DirectDownloadLinkException('ERROR: TORBOX_API is not set')
    headers = {'Authorization': f'Bearer {token}'}
    async with AsyncClient(timeout=60, headers=headers) as client:
        try:
            resp = await client.post(f'{TB_API}/torrents/createtorrent', data={'magnet': magnet})
            payload = resp.json()
        except HTTPError as e:
            raise DirectDownloadLinkException(f'ERROR: TorBox request failed: {e}') from e
        except ValueError:
            raise DirectDownloadLinkException('ERROR: TorBox returned an invalid response')
        if not payload.get('success'):
            raise DirectDownloadLinkException(
                f"ERROR: TorBox: {payload.get('detail') or payload.get('error') or 'request failed'}")
        data = payload.get('data') or {}
        torrent_id = data.get('torrent_id') or data.get('torrentId')
        if not torrent_id:
            raise DirectDownloadLinkException('ERROR: TorBox returned no torrent id')

        started = time()
        item = {}
        while True:
            resp = await client.get(f'{TB_API}/torrents/mylist', params={'id': torrent_id})
            data = resp.json().get('data') or {}
            if isinstance(data, list):
                data = data[0] if data else {}
            item = data or {}
            if item.get('download_finished') and item.get('download_present'):
                break
            if str(item.get('download_state', '')).lower() in ('error', 'failed'):
                raise DirectDownloadLinkException('ERROR: TorBox torrent failed')
            if cancelled and cancelled():
                raise DirectDownloadLinkException('ERROR: magnet resolve cancelled')
            if time() - started > _MAX_WAIT:
                raise DirectDownloadLinkException('ERROR: TorBox magnet timed out')
            await sleep(_POLL_INTERVAL)

        contents = []
        total_size = 0

        async def request_link(file_item):
            nonlocal total_size
            params = {'token': token, 'torrent_id': torrent_id, 'file_id': file_item.get('id')}
            resp = await client.post(f'{TB_API}/torrents/requestdl', params=params)
            payload = resp.json()
            if not payload.get('success'):
                return
            data = payload.get('data')
            url = data.get('url') if isinstance(data, dict) else data
            if url:
                contents.append({
                    'path': '',
                    'filename': file_item.get('name') or f"file-{file_item.get('id')}",
                    'url': url,
                })
                total_size += int(file_item.get('size') or 0)

        await gather(*(request_link(file_item) for file_item in item.get('files') or []))
        if not contents:
            raise DirectDownloadLinkException('ERROR: TorBox returned no download links')
        return {'title': _title_from_magnet(magnet), 'total_size': total_size, 'contents': contents}
