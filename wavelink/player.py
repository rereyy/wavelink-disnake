"""
MIT License

Copyright (c) 2019-Current PythonistaGuild, EvieePy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Union

import async_timeout
import discord
from discord.abc import Connectable
from discord.utils import MISSING

from .exceptions import (
    ChannelTimeoutException,
    InvalidChannelStateException,
    LavalinkException,
)
from .node import Pool
from .queue import Queue
from .tracks import Playable

if TYPE_CHECKING:
    from discord.types.voice import GuildVoiceState as GuildVoiceStatePayload
    from discord.types.voice import VoiceServerUpdate as VoiceServerUpdatePayload
    from typing_extensions import Self

    from .node import Node
    from .types.request import Request as RequestPayload
    from .types.state import PlayerVoiceState, VoiceState

    VocalGuildChannel = Union[discord.VoiceChannel, discord.StageChannel]


logger: logging.Logger = logging.getLogger(__name__)


class Player(discord.VoiceProtocol):
    """

    Attributes
    ----------
    client: discord.Client
        The :class:`discord.Client` associated with this :class:`Player`.
    channel: discord.abc.Connectable | None
        The currently connected :class:`discord.VoiceChannel`.
        Could be None if this :class:`Player` has not been connected or has previously been disconnected.

    """

    channel: VocalGuildChannel

    def __call__(self, client: discord.Client, channel: VocalGuildChannel) -> Self:
        super().__init__(client, channel)

        self._guild = channel.guild

        return self

    def __init__(
        self, client: discord.Client = MISSING, channel: Connectable = MISSING, *, nodes: list[Node] | None = None
    ) -> None:
        super().__init__(client, channel)

        self.client: discord.Client = client
        self._guild: discord.Guild | None = None

        self._voice_state: PlayerVoiceState = {"voice": {}}

        self._node: Node
        if not nodes:
            self._node = Pool.get_node()
        else:
            self._node = sorted(nodes, key=lambda n: len(n.players))[0]

        if self.client is MISSING and self.node.client:
            self.client = self.node.client

        self._connected: bool = False
        self._connection_event: asyncio.Event = asyncio.Event()

        self._current: Playable | None = None
        self._original: Playable | None = None
        self._previous: Playable | None = None

        self.queue: Queue = Queue()

        self._volume: int = 100
        self._paused: bool = False

    @property
    def node(self) -> Node:
        """The :class:`Player`'s currently selected :class:`Node`.


        .. versionchanged:: 3.0.0
            This property was previously known as ``current_node``.
        """
        return self._node

    @property
    def guild(self) -> discord.Guild | None:
        """Returns the :class:`Player`'s associated :class:`discord.Guild`.

        Could be None if this :class:`Player` has not been connected.
        """
        return self._guild

    @property
    def connected(self) -> bool:
        """Returns a bool indicating if the player is currently connected to a voice channel.

        .. versionchanged:: 3.0.0
            This property was previously known as ``is_connected``.
        """
        return self.channel and self._connected

    @property
    def current(self) -> Playable | None:
        """Returns the currently playing :class:`~wavelink.Playable` or None if no track is playing."""
        return self._current

    @property
    def volume(self) -> int:
        """Returns an int representing the currently set volume, as a percentage.

        See: :meth:`set_volume` for setting the volume.
        """
        return self._volume

    @property
    def paused(self) -> bool:
        """Returns the paused status of the player. A currently paused player will return ``True``.

        See: :meth:`pause` and :meth:`play` for setting the paused status.
        """
        return self._paused

    @property
    def playing(self) -> bool:
        return self._connected and self._current is not None

    async def on_voice_state_update(self, data: GuildVoiceStatePayload, /) -> None:
        channel_id = data["channel_id"]

        if not channel_id:
            await self._destroy()
            return

        self._connected = True

        self._voice_state["voice"]["session_id"] = data["session_id"]
        self.channel = self.client.get_channel(int(channel_id))  # type: ignore

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload, /) -> None:
        self._voice_state["voice"]["token"] = data["token"]
        self._voice_state["voice"]["endpoint"] = data["endpoint"]

        await self._dispatch_voice_update()

    async def _dispatch_voice_update(self) -> None:
        assert self.guild is not None
        data: VoiceState = self._voice_state["voice"]

        try:
            session_id: str = data["session_id"]
            token: str = data["token"]
        except KeyError:
            return

        endpoint: str | None = data.get("endpoint", None)
        if not endpoint:
            return

        request: RequestPayload = {"voice": {"sessionId": session_id, "token": token, "endpoint": endpoint}}

        try:
            await self.node._update_player(self.guild.id, data=request)
        except LavalinkException:
            await self.disconnect()
        else:
            self._connection_event.set()

        logger.debug(f"Player {self.guild.id} is dispatching VOICE_UPDATE.")

    async def connect(
        self, *, timeout: float = 5.0, reconnect: bool, self_deaf: bool = False, self_mute: bool = False
    ) -> None:
        if self.channel is MISSING:
            msg: str = 'Please use "discord.VoiceChannel.connect(cls=...)" and pass this Player to cls.'
            raise InvalidChannelStateException(f"Player tried to connect without a valid channel: {msg}")

        if not self._guild:
            self._guild = self.channel.guild
            self.node._players[self._guild.id] = self

        assert self.guild is not None
        await self.guild.change_voice_state(channel=self.channel, self_mute=self_mute, self_deaf=self_deaf)

        try:
            async with async_timeout.timeout(timeout):
                await self._connection_event.wait()
        except (asyncio.TimeoutError, asyncio.CancelledError):
            msg = f"Unable to connect to {self.channel} as it exceeded the timeout of {timeout} seconds."
            raise ChannelTimeoutException(msg)

    async def play(
        self,
        track: Playable,
        *,
        replace: bool = True,
        start: int = 0,
        end: int | None = None,
        volume: int | None = None,
        paused: bool | None = None,
    ) -> Playable:
        """Play the provided :class:`~wavelink.Playable`.

        Parameters
        ----------
        track: :class:`~wavelink.Playable`
            The track to being playing.
        replace: bool
            Whether this track should replace the currently playing track, if there is one. Defaults to ``True``.
        start: int
            The position to start playing the track at in milliseconds.
            Defaults to ``0`` which will start the track from the beginning.
        end: Optional[int]
            The position to end the track at in milliseconds.
            Defaults to ``None`` which means this track will play until the very end.
        volume: Optional[int]
            Sets the volume of the player. Must be between ``0`` and ``1000``.
            Defaults to ``None`` which will not change the current volume.
            See Also: :meth:`set_volume`
        paused: bool | None
            Whether the player should be paused, resumed or retain current status when playing this track.
            Setting this parameter to ``True`` will pause the player. Setting this parameter to ``False`` will
            resume the player if it is currently paused. Setting this parameter to ``None`` will not change the status
            of the player. Defaults to ``None``.

        Returns
        -------
        :class:`~wavelink.Playable`
            The track that began playing.


        .. versionchanged:: 3.0.0
            Added the ``paused`` parameter. ``replace``, ``start``, ``end``, ``volume`` and ``paused`` are now all
            keyword-only arguments.
        """
        assert self.guild is not None

        original_vol: int = self._volume
        vol: int = volume or self._volume

        if vol != self._volume:
            self._volume = vol

        if replace or not self._current:
            self._current = track
            self._original = track

        old_previous = self._previous
        self._previous = self._current

        pause: bool
        if paused is not None:
            pause = paused
        else:
            pause = self._paused

        request: RequestPayload = {
            "encodedTrack": track.encoded,
            "volume": vol,
            "position": start,
            "endTime": end,
            "paused": pause,
        }

        try:
            await self.node._update_player(self.guild.id, data=request, replace=replace)
        except LavalinkException as e:
            self._current = None
            self._original = None
            self._previous = old_previous
            self._volume = original_vol
            raise e

        self._paused = pause

        return track

    async def pause(self, value: bool, /) -> None:
        """Set the paused or resume state of the player.

        Parameters
        ----------
        value: bool
            A bool indicating whether the player should be paused or resumed. True indicates that the player should be
            ``paused``. False will resume the player if it is currently paused.


        .. versionchanged:: 3.0.0
            This method now expects a positional-only bool value. The ``resume`` method has been removed.
        """
        assert self.guild is not None

        request: RequestPayload = {"paused": value}
        await self.node._update_player(self.guild.id, data=request)

        self._paused = value

    async def seek(self, position: int = 0, /) -> None:
        """Seek to the provided position in the currently playing track, in milliseconds.

        Parameters
        ----------
        position: int
            The position to seek to in milliseconds. To restart the song from the beginning,
            you can disregard this parameter or set position to 0.


        .. versionchanged:: 3.0.0
            The ``position`` parameter is now positional-only, and has a default of 0.
        """
        assert self.guild is not None

        if not self._current:
            return

        request: RequestPayload = {"position": position}
        await self.node._update_player(self.guild.id, data=request)

    async def set_filter(self) -> None:
        raise NotImplementedError

    async def set_volume(self, value: int = 100, /) -> None:
        """Set the :class:`Player` volume, as a percentage, between 0 and 1000.

        By default, every player is set to 100 on creation. If a value outside 0 to 1000 is provided it will be
        clamped.

        Parameters
        ----------
        value: int
            A volume value between 0 and 1000. To reset the player to 100, you can disregard this parameter.


        .. versionchanged:: 3.0.0
            The ``value`` parameter is now positional-only, and has a default of 100.
        """
        assert self.guild is not None
        vol: int = max(min(value, 1000), 0)

        request: RequestPayload = {"volume": vol}
        await self.node._update_player(self.guild.id, data=request)

        self._volume = vol

    async def disconnect(self, **kwargs: Any) -> None:
        """Disconnect the player from the current voice channel and remove it from the :class:`~wavelink.Node`.

        This method will cause any playing track to stop and potentially trigger the following events:

            - ``on_wavelink_track_end``
            - ``on_wavelink_websocket_closed``


        .. warning::

            Please do not re-use a :class:`Player` instance that has been disconnected, unwanted side effects are
            possible.
        """
        assert self.guild

        await self._destroy()
        await self.guild.change_voice_state(channel=None)

    async def stop(self, *, force: bool = True) -> Playable | None:
        """An alias to :meth:`skip`.

        See Also: :meth:`skip` for more information.

        .. versionchanged:: 3.0.0
            This method is now known as ``skip``, but the alias ``stop`` has been kept for backwards compatability.
        """
        return await self.skip(force=force)

    async def skip(self, *, force: bool = True) -> Playable | None:
        """Stop playing the currently playing track.

        Parameters
        ----------
        force: bool
            Whether the track should skip looping, if :class:`wavelink.Queue` has been set to loop.
            Defaults to ``True``.

        Returns
        -------
        :class:`~wavelink.Playable` | None
            The currently playing track that was skipped, or ``None`` if no track was playing.


        .. versionchanged:: 3.0.0
            This method was previously known as ``stop``. To avoid confusion this method is now known as ``skip``.
            This method now returns the :class:`~wavelink.Playable` that was skipped.
        """
        assert self.guild is not None
        old: Playable | None = self._current

        request: RequestPayload = {"encodedTrack": None}
        await self.node._update_player(self.guild.id, data=request, replace=True)

        return old

    def _invalidate(self) -> None:
        self._connected = False
        self._connection_event.clear()

        try:
            self.cleanup()
        except (AttributeError, KeyError):
            pass

    async def _destroy(self) -> None:
        assert self.guild

        self._invalidate()
        player: Self | None = self.node._players.pop(self.guild.id, None)

        if player:
            try:
                await self.node._destroy_player(self.guild.id)
            except LavalinkException:
                pass
