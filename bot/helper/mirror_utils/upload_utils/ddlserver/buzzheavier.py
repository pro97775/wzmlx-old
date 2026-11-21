#!/usr/bin/env python3
from asyncio import CancelledError
from os import path as ospath, walk
from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from aiohttp import ClientSession, ClientTimeout

from bot.helper.ext_utils.bot_utils import sync_to_async

_API_BASE = "https://buzzheavier.com/api/fs"
_UPLOAD_BASE = "https://w.buzzheavier.com"
_CHUNK_SIZE = 1024 * 1024


class Buzzheavier:
    def __init__(self, dluploader=None, account_id=None):
        self.dluploader = dluploader
        self.account_id = (account_id or '').strip()
        self.total_files = 0
        self.total_folders = 0
        self.__session = None
        self.__root_id = None

    @property
    def __headers(self):
        return {"Authorization": f"Bearer {self.account_id}"} if self.account_id else {}

    async def __fetch(self, method, url, **kwargs):
        headers = {**self.__headers, **kwargs.pop("headers", {})}
        async with self.__session.request(method, url, headers=headers, **kwargs) as resp:
            text = await resp.text()
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}
            if resp.status not in (200, 201, 409):
                raise Exception(f"Buzzheavier {method} {url} failed ({resp.status}): {text[:200]}")
            return resp.status, data

    async def __root(self):
        if self.__root_id is None:
            _, data = await self.__fetch("GET", _API_BASE)
            self.__root_id = (data.get("data") or {}).get("id")
            if not self.__root_id:
                raise Exception("Buzzheavier: root folder id not found")
        return self.__root_id

    async def __create_dir(self, name, parent_id=None):
        parent_id = parent_id or await self.__root()
        status, data = await self.__fetch("POST", f"{_API_BASE}/{parent_id}", json={"name": name})
        if status == 409:
            _, listing = await self.__fetch("GET", f"{_API_BASE}/{parent_id}")
            for item in (listing.get("data") or {}).get("children", []):
                if item.get("name") == name and item.get("isDirectory"):
                    return item.get("id")
            raise Exception(f"Buzzheavier: folder '{name}' exists but was not listed")
        folder_id = (data.get("data") or {}).get("id")
        if not folder_id:
            raise Exception(f"Buzzheavier: could not create folder '{name}'")
        return folder_id

    async def __stream(self, file_path):
        async with aiopen(file_path, "rb") as fh:
            while True:
                if self.dluploader.is_cancelled:
                    raise CancelledError()
                chunk = await fh.read(_CHUNK_SIZE)
                if not chunk:
                    return
                self.dluploader.add_uploaded_bytes(len(chunk))
                yield chunk

    async def __upload_file(self, file_path, parent_id=None):
        parent_id = parent_id or await self.__root()
        file_name = ospath.basename(file_path)
        size = await aiopath.getsize(file_path)
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            **self.__headers,
        }
        async with self.__session.put(f"{_UPLOAD_BASE}/{parent_id}/{file_name}",
                                      data=self.__stream(file_path), headers=headers) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                raise Exception(f"Buzzheavier upload failed ({resp.status}): {text[:200]}")
            try:
                payload = await resp.json(content_type=None)
            except Exception:
                payload = {}
        file_id = (payload.get("data") or {}).get("id")
        if not file_id:
            raise Exception("Buzzheavier: upload response has no file id")
        self.total_files += 1
        return f"https://buzzheavier.com/{file_id}"

    async def __upload_tree(self, directory, parent_id):
        folder_ids = {'.': parent_id}
        for root, _, files in await sync_to_async(lambda: list(walk(directory))):
            rel_path = ospath.relpath(root, directory)
            if rel_path == '.':
                current_id = parent_id
            else:
                current_id = await self.__create_dir(ospath.basename(root),
                                                     folder_ids.get(ospath.dirname(rel_path), parent_id))
                folder_ids[rel_path] = current_id
                self.total_folders += 1
            for file_name in sorted(files):
                await self.__upload_file(ospath.join(root, file_name), current_id)

    async def upload(self, file_path):
        if not self.account_id:
            raise Exception("Buzzheavier Account ID is not set in User Settings!")
        timeout = ClientTimeout(total=None, connect=30, sock_read=600)
        async with ClientSession(timeout=timeout) as session:
            self.__session = session
            try:
                if await aiopath.isfile(file_path):
                    link = await self.__upload_file(file_path)
                else:
                    root_id = await self.__create_dir(ospath.basename(ospath.abspath(file_path)))
                    await self.__upload_tree(file_path, root_id)
                    link = f"https://buzzheavier.com/{root_id}"
                if self.dluploader.is_cancelled:
                    return None
                return link
            finally:
                self.__session = None
