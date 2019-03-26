# -*- coding: utf-8 -*-

# Copyright © 2018, 2019 Damir Jelić <poljar@termina.org.uk>
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

from typing import (
    Any,
    Dict,
    Optional,
    Tuple,
    Union,
)

from uuid import uuid4
from functools import wraps

from json.decoder import JSONDecodeError
from aiohttp import ClientSession, ContentTypeError

from ..api import Api
from ..responses import (
    LoginResponse,
    LoginError,
    SyncResponse,
    SyncError,
    KeysUploadResponse,
    KeysQueryResponse,
    RoomSendResponse,
    ShareGroupSessionResponse,
    ShareGroupSessionError,
)
from ..exceptions import LocalProtocolError, EncryptionError

from . import Client, ClientConfig, logged_in, store_loaded


def client_session(func):
    """Ensure that the Async client has a valid client session."""
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        if not self.client_session:
            self.client_session = ClientSession()
        return await func(self, *args, **kwargs)
    return wrapper


class AsyncClient(Client):
    """An async IO matrix client.

    Args:
        homeserver (str): The URL of the homeserver which we want to connect
            to.
        user (str, optional): The user which will be used when we log in to the
            homeserver.
        device_id (str, optional): An unique identifier that distinguishes
            this client instance. If not set the server will provide one after
            log in.
        store_path (str, optional): The directory that should be used for state
            storeage.
        config (ClientConfig, optional): Configuration for the client.
        ssl (bool/ssl.SSLContext, optional): SSL validation mode. None for
            default SSL check (ssl.create_default_context() is used), False
            for skip SSL certificate validation connection.
        proxy (str, optional): The proxy that should be used for the HTTP
            connection.

    Example:
            >>> client = AsyncClient("https://example.org", "example")
            >>> login_response = client.login("hunter1")

    """

    def __init__(
            self,
            homeserver,     # type: str
            user="",        # type: str
            device_id="",   # type: Optional[str]
            store_path="",  # type: Optional[str]
            config=None,    # type: Optional[ClientConfig]
            ssl=None,       # type: Optional[bool]
            proxy=None,     # type: Optional[str]
    ):
        # type: (...) -> None
        self.homeserver = homeserver
        self.client_session = None  # type: Optional[ClientSession]

        self.ssl = ssl
        self.proxy = proxy

        super().__init__(user, device_id, store_path, config)

    async def _create_response(
            self,
            response_class,
            transport_response,
            data=None
    ):
        try:
            parsed_dict = await transport_response.json()
        except (JSONDecodeError, ContentTypeError):
            parsed_dict = {}

        if data:
            response = response_class.from_dict(parsed_dict, *data)
        else:
            response = response_class.from_dict(parsed_dict)

        response.transport_response = transport_response
        return response

    @client_session
    async def login(self, password, device_name=""):
        # type: (str, str) -> Union[LoginResponse, LoginError]
        """Login to the homeserver.

        Args:
            password (str): The user's password.
            device_name (str): A display name to assign to a newly-created
                device. Ignored if the logged in device corresponds to a
                known device.

        Returns either a `LoginResponse` if the request was successful or
        a `LoginError` if there was an error with the request.
        """
        assert self.client_session
        method, path, data = Api.login(
            self.user,
            password,
            device_name=device_name,
            device_id=self.device_id
        )

        async with self.client_session.request(
                method,
                self.homeserver + path,
                data=data,
                ssl=self.ssl,
                proxy=self.proxy
        ) as resp:
            response = await self._create_response(LoginResponse, resp)
            self.receive_response(response)
            return response

    @logged_in
    @client_session
    async def sync(
            self,
            timeout=None,     # type: Optional[int]
            sync_filter=None  # type: Optional[Dict[Any, Any]]
    ):
        # type: (...) -> Tuple[SyncResponse, SyncError]
        """Synchronise the client's state with the latest state on the server.

        Args:
            timeout(int, optional): The maximum time that the server should
                wait for new events before it should return the request
                anyways, in milliseconds.
            filter (Dict[Any, Any], optional): A filter that should be used for
                this sync request.

        Returns either a `SyncResponse` if the request was successful or
        a `SyncError` if there was an error with the request.
        """
        assert self.client_session
        method, path = Api.sync(
            self.access_token,
            since=self.next_batch,
            timeout=timeout,
            filter=sync_filter
        )

        async with self.client_session.request(
                method,
                self.homeserver + path,
                data=None,
                ssl=self.ssl,
                proxy=self.proxy
        ) as resp:
            response = await self._create_response(SyncResponse, resp)
            self.receive_response(response)
            return response

    @logged_in
    @store_loaded
    @client_session
    async def keys_upload(self):
        """Upload the E2E encryption keys.

        This uploads the long lived session keys as well as the required amount
        of one-time keys.

        Raises LocalProtocolError if the client isn't logged in, if the session
        store isn't loaded or if no encryption keys need to be uploaded.
        """
        assert self.client_session
        if not self.should_upload_keys:
            raise LocalProtocolError("No key upload needed.")

        keys_dict = self.olm.share_keys()

        method, path, data = Api.keys_upload(
            self.access_token,
            keys_dict
        )

        async with self.client_session.request(
                method,
                self.homeserver + path,
                data=data,
                ssl=self.ssl,
                proxy=self.proxy
        ) as resp:
            response = await self._create_response(KeysUploadResponse, resp)
            self.receive_response(response)
            return response

    @logged_in
    @store_loaded
    @client_session
    async def keys_query(self):
        # type: () -> Union[KeysQueryResponse]
        """Query the server for user keys.

        This queries the server for device keys of users with which we share an
        encrypted room.

        Raises LocalProtocolError if the client isn't logged in, if the session
        store isn't loaded or if no key query needs to be performed.
        """
        # TODO refactor that out into the base client, and use our knowledge of
        # already queried users to limit the user list.
        assert self.client_session
        user_list = [
            user_id for room in self.rooms.values()
            if room.encrypted for user_id in room.users
        ]

        if not user_list:
            raise LocalProtocolError("No key query required.")

        # TODO pass the sync token here if it's a device update that triggered
        # our need for a key query.
        method, path, data = Api.keys_query(
            self.access_token,
            user_list
        )

        async with self.client_session.request(
                method,
                self.homeserver + path,
                data=data,
                ssl=self.ssl,
                proxy=self.proxy
        ) as resp:
            response = await self._create_response(KeysQueryResponse, resp)
            self.receive_response(response)
            return response

    @logged_in
    @client_session
    async def room_send(self, room_id, message_type, content, tx_id=None):
        """Send a message to a room.

        Args:
            room_id(str): The room id of the room where the message should be
                sent to.
            message_type(str): A string identifying the type of the message.
            content(Dict[Any, Any]): A dictionary containing the content of the
                message.
            tx_id(str, optional): The transaction ID of this event used to
                uniquely identify this message.

        If the room where the message should be sent is encrypted the message
        will be encrypted before sending.

        Raises GroupEncryptionError if the room is encrypted but the group
        session wasn't shared yet.

        Raises LocalProtocolError if the client isn't logged in.
        """
        if self.olm:
            try:
                room = self.rooms[room_id]
            except KeyError:
                raise LocalProtocolError(
                    "No such room with id {} found.".format(room_id)
                )

            if room.encrypted:
                print("ENCRYPTING")
                content = self.olm.group_encrypt(
                    room_id,
                    {
                        "content": content,
                        "type": message_type
                    },
                )
                print("HELLO")
                message_type = "m.room.encrypted"

        uuid = tx_id or uuid4()

        method, path, data = Api.room_send(
            self.access_token,
            room_id,
            message_type,
            content,
            uuid
        )

        async with self.client_session.request(
                method,
                self.homeserver + path,
                data=data,
                ssl=self.ssl,
                proxy=self.proxy
        ) as resp:
            response = await self._create_response(
                RoomSendResponse,
                resp,
                (room_id,)
            )
            self.receive_response(response)
            return response

    @logged_in
    @store_loaded
    @client_session
    async def share_group_session(
            self,
            room_id,                        # type: str
            tx_id=None                      # type: Optional[str]
    ):
        # type: (...) -> Union[ShareGroupSessionResponse]
        """Send a message to a room.

        Args:
            room_id(str): The room id of the room where the message should be
                sent to.
            message_type(str): A string identifying the type of the message.
            content(Dict[Any, Any]): A dictionary containing the content of the
                message.
            tx_id(str, optional): The transaction ID of this event used to
                uniquely identify this message.
        """
        assert self.olm
        try:
            room = self.rooms[room_id]
        except KeyError:
            raise LocalProtocolError("No such room with id {}".format(room_id))

        if not room.encrypted:
            raise LocalProtocolError("Room with id {} is not encrypted".format(
                room_id))

        shared_with = set()

        try:
            while True:
                # TODO claim keys and create olm sessions here if they are
                # missing
                user_set, to_device_dict = self.olm.share_group_session(
                    room_id,
                    list(room.users.keys()),
                    True
                )

                uuid = tx_id or uuid4()

                method, path, data = Api.to_device(
                    self.access_token,
                    "m.room.encrypted",
                    to_device_dict,
                    uuid
                )

                async with self.client_session.request(
                        method,
                        self.homeserver + path,
                        data=data,
                        ssl=self.ssl,
                        proxy=self.proxy
                ) as resp:
                    response = await self._create_response(
                        ShareGroupSessionResponse,
                        resp,
                        (room_id, user_set)
                    )
                    self.receive_response(response)

                    if isinstance(response, ShareGroupSessionResponse):
                        shared_with.update(response.users_shared_with)

        except LocalProtocolError:
            return ShareGroupSessionResponse(room_id, shared_with)

    async def close(self):
        """Close the underlying http session."""
        if self.client_session:
            await self.client_session.close()
            self.client_session = None
