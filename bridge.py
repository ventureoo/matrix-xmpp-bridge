#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2023 Vasiliy Stelmachenok <ventureo@yandex.ru>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
import aioxmpp
import yaml
import asyncio
import aiohttp
import aiofiles
import aiofiles.os
import magic
import tempfile
import sys
import re
import logging

from collections import OrderedDict
from asyncio import AbstractEventLoop
from aioxmpp.muc.xso import History
from aioxmpp.muc import Room, Occupant
from aioxmpp.stanza import Message
from aioxmpp.utils import namespaces
from aioxmpp.misc import Replace
from aioxmpp.misc.oob import OOBExtension
from nio import (
    AsyncClient, RoomMessageText, RoomMessageMedia,
    MatrixRoom, UploadResponse, RoomSendResponse,
    RedactionEvent, LoginResponse, StickerEvent
)
from PIL import Image
from datetime import datetime


# Auxiliary class which as LRU Cache used to store message IDs/message bodies
# (for Matrix replies)
class Cache():

    def __init__(self, size):
        self.size = size
        self.cache = OrderedDict()
        self.size = size

    def get(self, key: str):
        if key in self.cache.keys():
            self.cache.move_to_end(key)
            return self.cache[key]
        else:
            return None

    def add(self, key: str, value: str):
        self.cache[key] = value
        self.cache.move_to_end(key)
        if len(self.cache) > self.size:
            self.cache.popitem(last=False)


class BaseClient:
    def __init__(self, server: str, user: str, password: str, room: str, cache: Cache,
                 logger: logging.Logger):
        self.user = user
        self.password = password
        self.room = room
        self.cache = cache
        self.logger = logger

    async def download_attachment(self, url) -> str:
        path = tempfile.mktemp("-bridge")
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    self.logger.info(f"Successfully downloaded attachment: {url}")
                else:
                    self.logger.error(f"Download {url} failed: {resp.reason} {resp.status}")
                    return

        async with aiofiles.open(path, "wb") as out:
            await out.write(data)

        mime = magic.from_file(path, mime=True)
        fstat = await aiofiles.os.stat(path)
        fsize = fstat.st_size

        return path, mime, fsize

    async def _file_sender(self, fpath):
        async with aiofiles.open(fpath, 'rb') as f:
            chunk = await f.read(64 * 1024)
            while chunk:
                yield chunk
                chunk = await f.read(64 * 1024)

    async def upload_attachment(self, fpath, url, hrds, fsize, ftype):
        hrds["Content-Type"] = ftype
        hrds["Content-Length"] = str(fsize)
        hrds["User-Agent"] = "XMPP-Matrix Bridge"

        async with aiohttp.ClientSession() as session:
            async with session.put(url, data=self._file_sender(fpath),
                                   headers=hrds) as resp:
                if resp.status not in (200, 201):
                    self.logger.error("Upload failed: {resp.reason} {resp.status}")


class MatrixClient(BaseClient):
    reply_pattern: str = re.compile(r"^> ([^>\n]+)", re.MULTILINE)
    body_pattern: str = re.compile(r"^(?!>).*", re.MULTILINE)

    def __init__(self, server: str, user: str, password: str, room_id: str,
                 cache: Cache):
        super().__init__(server, user, password, room_id, cache, logging.getLogger("MatrixClient"))
        self.matrix = AsyncClient(server, user.split(":")[0][1:])
        self.ids_cache = Cache(150)
        self.pending_id = None
        self.logger.info("Initialising MatrixClient...")

    async def run(self, xmpp):
        self.xmpp = xmpp
        resp = await self.matrix.login(self.password)

        if not isinstance(resp, LoginResponse):
            self.logger.error("Login failed", resp)
            return

        self.matrix.add_event_callback(self.message_callback, RoomMessageText)
        self.matrix.add_event_callback(self.attachment_callback, RoomMessageMedia)
        self.matrix.add_event_callback(self.redact_callback, RedactionEvent)
        self.matrix.add_event_callback(self.sticker_callback, StickerEvent)

        await self.matrix.sync_forever(timeout=30000, since=self.room, full_state=True)

    async def message_callback(self, room: MatrixRoom, event: RoomMessageText):
        if room.room_id != self.room:
            return

        if event.sender == self.user:
            if self.pending_id is not None:
                self.ids_cache.add(event.event_id, self.pending_id)
                self.xmpp.ids_cache.add(self.pending_id, event.event_id)
            return

        edited = event.source["content"].get("m.new_content")

        if edited is not None:
            body = edited.get("body")
            matrix_id = event.source["content"].get("m.relates_to").get("event_id")
            xmpp_id = self.ids_cache.get(matrix_id)

            if xmpp_id is not None:
                await self.xmpp.edit_message(xmpp_id, body)
                return

        self.cache.add(event.body, event.event_id)
        self.xmpp.pending_id = event.event_id
        await self.xmpp.send_message(event.body, room.user_name(event.sender))
        await self.matrix.room_read_markers(room.room_id, event.event_id, event.event_id)

    async def attachment_callback(self, room: MatrixRoom, event: RoomMessageMedia):
        if event.sender == self.user:
            return

        url = await self.matrix.mxc_to_http(event.url)
        self.cache.add(event.body, event.event_id)
        self.xmpp.pending_id = event.event_id
        await self.xmpp.send_attachment(url, event.body, room.user_name(event.sender))

    async def redact_callback(self, room: MatrixRoom, event: RedactionEvent):
        if room.room_id != self.room:
            return

        xmpp_id = self.ids_cache.get(event.redacts)
        if xmpp_id is not None:
            await self.xmpp.edit_message(xmpp_id, "This message was deleted by user which sent it")

    async def sticker_callback(self, room: MatrixRoom, event: StickerEvent):
        if room.room_id != self.room:
            return

        url = await self.matrix.mxc_to_http(event.url)
        self.cache.add(event.body, event.event_id)
        self.xmpp.pending_id = event.event_id
        await self.xmpp.send_attachment(url, event.body, room.user_name(event.sender))

    def _make_message_content(self, text: str, sender: str):
        content = {"msgtype": "m.text", "body": f"{sender}: {text}"}
        reply = re.findall(self.reply_pattern, text)
        body = "\n".join(re.findall(self.body_pattern, text))

        if reply:
            key = "\n".join(reply)

            # Attempt to detect a replies format of some clients that add time of
            # the message sent on second line
            try:
                datetime.strptime(reply[1], '%Y-%m-%d  %H:%M (GMT%z)')
                key = "\n".join(reply[2:])
            except (ValueError, IndexError):
                pass

            matrix_id = self.cache.get(key)
            if matrix_id is not None:
                content = {
                    "msgtype": "m.text",
                    "body": f"{sender}: {body}",
                    "m.relates_to": {
                        "m.in_reply_to": {
                            "event_id": matrix_id
                        }
                    }
                }
            else:
                content = {"msgtype": "m.text", "body": f"{sender}:\n{text}"}

        return content, body

    async def send_message(self, text: str, sender: str):
        content, body = self._make_message_content(text, sender)
        resp = await self.matrix.room_send(
            room_id=self.room,
            message_type="m.room.message",
            content=content
        )

        if isinstance(resp, RoomSendResponse):
            self.cache.add(body, resp.event_id)
        else:
            self.logger.error("Error when sending a message", resp)

    async def send_attachment(self, url: str, fname: str, sender: str):
        fpath, mime, fsize = await self.download_attachment(url)

        if fpath is None:
            self.logger.error("Error while sending an attachment")

        async with aiofiles.open(fpath, "rb+") as f:
            resp, _ = await self.matrix.upload(
                f,
                content_type=mime,
                filename=fname,
                filesize=fsize
            )

        if not isinstance(resp, UploadResponse):
            self.logger.error("Failed to upload attachment", resp)
            return

        content = {
            "body": fname,
            "info": {
                "size": fsize,
                "mimetype": mime,
            },
            "url": resp.content_uri,
        }

        ftype = mime.split("/")[0]
        if ftype == "image":
            content["msgtype"] = "m.image"
            img = Image.open(fpath)
            content["info"]["w"] = img.size[0]
            content["info"]["h"] = img.size[1]
        elif ftype == "video":
            content["msgtype"] = "m.video"
        elif ftype == "audio":
            content["msgtype"] = "m.audio"
        else:
            content["msgtype"] = "m.file"

        await self.send_message("", sender)
        resp = await self.matrix.room_send(self.room, message_type="m.room.message",
                                           content=content)

        if not isinstance(resp, RoomSendResponse):
            self.logger.error("Error when sending a message", resp)

        await aiofiles.os.remove(fpath)

    async def edit_message(self, matrix_id: str, text: str, sender: str):
        content, _ = self._make_message_content(text, sender)
        content["m.relates_to"] = {
            "rel_type": "m.replace",
            "event_id": matrix_id
        }
        content["m.new_content"] = content.copy()

        resp = await self.matrix.room_send(
            room_id=self.room,
            message_type="m.room.message",
            content=content
        )

        if not isinstance(resp, RoomSendResponse):
            self.logger.error("Error when sending a message", resp)


class XmppClient(BaseClient):
    xmpp: aioxmpp.PresenceClient = None
    t2bot_pattern = re.compile("@telegram_[0-9]+:t2bot.io")

    def __init__(self, loop: AbstractEventLoop, user: str, password: str, room: str,
                 cache: Cache):
        super().__init__(user.split("@")[-1], user, password, room, cache,
                         logging.getLogger("XmppClient"))
        self.loop = loop
        self.jid = aioxmpp.JID.fromstr(user)
        self.muc_jid = aioxmpp.JID.fromstr(room).bare()
        self.xmpp = aioxmpp.PresenceManagedClient(self.jid, aioxmpp.make_security_layer(password))
        self.ids_cache: Cache = Cache(150)
        self.service_addr: str = None
        self.room: Room = None
        self.pending_id = None
        self.logger.info("Initialising MatrixClient...")

    async def run(self, matrix: MatrixClient):
        self.matrix = matrix

        muc = self.xmpp.summon(aioxmpp.MUCClient)
        self.room, self.room_future = muc.join(self.muc_jid, "Matrix",
                                               history=History(maxstanzas=0))
        self.disco = self.xmpp.summon(aioxmpp.DiscoClient)
        self.room.on_message.connect(self.message_callback)

        async with self.xmpp.connected():
            while True:
                await asyncio.sleep(1)

    def message_callback(self, message: Message, member: Occupant, source, **kwargs):
        if member.direct_jid == self.jid.bare():
            if self.pending_id is not None:
                self.ids_cache.add(message.id_, self.pending_id)
                self.matrix.ids_cache.add(self.pending_id, message.id_)
            return

        if message.xep0066_oob is not None:
            self.attachment_callback(message, member)
        elif message.xep0308_replace is not None:
            xmpp_id = message.xep0308_replace.id_
            matrix_id = self.ids_cache.get(xmpp_id)
            if matrix_id is not None:
                self.loop.create_task(self.matrix.edit_message(matrix_id, message.body.any(),
                                                               member.nick))
        else:
            self.matrix.pending_id = message.id_
            self.loop.create_task(self.matrix.send_message(message.body.any(), member.nick))

    def attachment_callback(self, message: Message, member: Occupant):
        url = message.xep0066_oob.url
        fname = url.split("/")[-1]
        self.loop.create_task(self.matrix.send_attachment(url, fname, member.nick))

    async def send_message(self, text: str, sender: str):
        msg = Message(to=self.muc_jid, type_=aioxmpp.MessageType.GROUPCHAT)
        msg.body[None] = text

        try:
            await self._set_nick(sender)
            await self.xmpp.send(msg)
        except Exception as ex:
            self.logger.error("Error when sending message", ex)

    async def _set_nick(self, sender: str):
        nick = re.sub(self.t2bot_pattern, "Telegram", sender)

        if nick == sender:
            nick += " (Matrix)"

        try:
            # Wait until XMPP client is connected
            while not self.room_future.done():
                await asyncio.sleep(1)
            await self.room.set_nick(nick)
        except ValueError as ex:
            self.logger.error("An invalid user name has been passed", ex)

    async def _is_http_upload_supported(self):
        if self.service_addr is not None:
            return True

        items = await self.disco.query_items(
            self.xmpp.local_jid.replace(localpart=None, resource=None),
            timeout=10
        )

        # Check if server supports XEP-0363
        addrs = [item.jid for item in items.items if not item.node
                 if namespaces.xep0363_http_upload in
                 (await self.disco.query_info(item.jid)).features]

        if addrs:
            self.service_addr = addrs[0]
            return True

        return False

    async def send_attachment(self, url, fname, sender):
        if not await self._is_http_upload_supported():
            self.logger.warning("""Your XMPP server doesn't support HTTP Upload.
                    It is not possible to send attachments from matrix to XMPP.""")
            return

        fpath, mime, fsize = await self.download_attachment(url)

        if fpath is None:
            self.logger.error("Error while sending an attachment")

        slot = await self.xmpp.send(
            aioxmpp.IQ(
                to=self.service_addr,
                type_=aioxmpp.IQType.GET,
                payload=aioxmpp.httpupload.Request(fname, fsize, mime)
            )
        )

        await self.upload_attachment(fpath, slot.put.url, slot.put.headers, fsize, mime)

        # We should force the language tag because some clients ignore attachments without it
        # https://dev.gajim.org/gajim/gajim/-/issues/9178
        msg = Message(to=self.muc_jid, from_=self.jid, type_=aioxmpp.MessageType.GROUPCHAT)
        tag = aioxmpp.structs.LanguageTag.fromstr("en")
        msg.body[tag] = slot.get.url
        msg.xep0066_oob = OOBExtension()
        msg.xep0066_oob.url = slot.get.url

        try:
            await self._set_nick(sender)
            await self.xmpp.send(msg)
        except Exception as ex:
            self.logger.error("Error while editing a message", ex)

        await aiofiles.os.remove(fpath)

    async def edit_message(self, xmpp_id: str, text: str):
        msg = Message(to=self.muc_jid, from_=self.jid, type_=aioxmpp.MessageType.GROUPCHAT)
        msg.body[None] = text
        msg.xep0308_replace = Replace()
        msg.xep0308_replace.id_ = xmpp_id

        try:
            await self.xmpp.send(msg)
        except Exception as ex:
            self.logger.error("Error while editing a message", ex)


# Init
def main():
    config = {}
    with open("config.yml", "r") as f:
        try:
            config = yaml.safe_load(f)
        except yaml.YAMLError as ex:
            print("Config parsing error", ex)
            sys.exit(1)

    cache = Cache(150)
    loop = asyncio.get_event_loop()

    try:
        logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                            level=logging.INFO)
        xmpp = XmppClient(loop, config['xmpp']['jid'],
                          config['xmpp']['password'], config['xmpp']['muc'], cache)
        matrix = MatrixClient(config['matrix']['server'], config['matrix']['user'],
                              config['matrix']['password'], config['matrix']['room_id'],
                              cache)
        loop.create_task(matrix.run(xmpp))
        loop.create_task(xmpp.run(matrix))
        loop.run_forever()
    except KeyError as ex:
        print("Missing mandatory config parameter", ex)
        sys.exit(1)


if __name__ == "__main__":
    main()
