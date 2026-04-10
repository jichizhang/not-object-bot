import asyncio
import base64
import json
import os
import re

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import voice_recv
from aiohttp import web, WSMsgType
import numpy as np
import soxr
from twilio.rest import Client as TwilioClient

try:
    import audioop
except ImportError:
    import audioop_lts as audioop  # type: ignore[no-redef]


E164_RE = re.compile(r'^\+[1-9]\d{1,14}$')


def _mulaw_to_discord_pcm(mulaw_bytes: bytes) -> bytes:
    """Convert µ-law 8 kHz mono to PCM 16-bit 48 kHz stereo."""
    pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
    audio = np.frombuffer(pcm_8k, dtype=np.int16)
    audio_48k = soxr.resample(audio.astype(np.float32), 8000, 48000).astype(np.int16)
    stereo = np.repeat(audio_48k, 2)  # mono → stereo (duplicate channel)
    return stereo.tobytes()


def _discord_pcm_to_mulaw(pcm_bytes: bytes) -> bytes:
    """Convert PCM 16-bit 48 kHz stereo to µ-law 8 kHz mono."""
    audio = np.frombuffer(pcm_bytes, dtype=np.int16)
    if len(audio) < 2:
        return b''
    mono = ((audio[::2].astype(np.int32) + audio[1::2].astype(np.int32)) // 2).astype(np.int16)
    audio_8k = soxr.resample(mono.astype(np.float32), 48000, 8000).astype(np.int16)
    return audioop.lin2ulaw(audio_8k.tobytes(), 2)


class QueueAudioSource(discord.AudioSource):
    """Feeds Twilio phone audio (from a queue) into a Discord voice channel."""

    FRAME_SIZE = 3840  # 20 ms at 48 kHz stereo 16-bit

    def __init__(self, queue: asyncio.Queue):
        self._q = queue
        self._buf = bytearray()

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        while len(self._buf) < self.FRAME_SIZE:
            try:
                self._buf.extend(self._q.get_nowait())
            except asyncio.QueueEmpty:
                break
        if len(self._buf) >= self.FRAME_SIZE:
            frame = bytes(self._buf[:self.FRAME_SIZE])
            self._buf = self._buf[self.FRAME_SIZE:]
            return frame
        return bytes(self.FRAME_SIZE)  # silence if queue is dry


class TwilioAudioSink(voice_recv.AudioSink):
    """Captures Discord VC audio and enqueues it for forwarding to Twilio."""

    def __init__(self, queue: asyncio.Queue):
        super().__init__()
        self._q = queue

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data) -> None:
        if data.pcm:
            try:
                self._q.put_nowait(data.pcm)
            except asyncio.QueueFull:
                pass  # drop frame rather than block the audio thread

    def cleanup(self) -> None:
        pass


class VoipCog(commands.Cog):
    """Bridge a phone call into a Discord voice channel using Twilio Media Streams."""

    WEB_PORT = 8080

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.twilio = TwilioClient(
            os.getenv('TWILIO_ACCOUNT_SID'),
            os.getenv('TWILIO_AUTH_TOKEN'),
        )
        self.active_call_sid: str | None = None
        self.voice_client: voice_recv.VoiceRecvClient | None = None
        self.stream_ws: web.WebSocketResponse | None = None
        self.stream_sid: str | None = None
        self.twilio_to_discord: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self.discord_to_twilio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._bridge_task: asyncio.Task | None = None
        self._runner: web.AppRunner | None = None

    async def cog_load(self) -> None:
        asyncio.create_task(self._start_web_server())

    async def cog_unload(self) -> None:
        await self._cleanup(cancel_call=False)
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------
    # Embedded aiohttp server
    # ------------------------------------------------------------------

    async def _start_web_server(self) -> None:
        app = web.Application()
        app.router.add_post('/voice', self._twiml_handler)
        app.router.add_get('/stream', self._stream_handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, '0.0.0.0', self.WEB_PORT)
        await site.start()

    async def _twiml_handler(self, request: web.Request) -> web.Response:
        """Return TwiML that opens a bidirectional Media Stream WebSocket."""
        base_url = os.getenv('WEBHOOK_BASE_URL', '')
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Response>\n'
            '    <Connect>\n'
            f'        <Stream url="wss://{base_url}/stream" />\n'
            '    </Connect>\n'
            '</Response>'
        )
        return web.Response(text=xml, content_type='text/xml')

    async def _stream_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle the Twilio Media Stream WebSocket connection."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.stream_ws = ws

        loop = asyncio.get_running_loop()

        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            event = data.get('event')

            if event == 'start':
                self.stream_sid = data.get('streamSid')
                # Start playing phone audio into Discord VC
                if self.voice_client and not self.voice_client.is_playing():
                    source = QueueAudioSource(self.twilio_to_discord)
                    self.voice_client.play(source)
                # Start forwarding Discord audio back to Twilio
                self._bridge_task = asyncio.create_task(self._forward_discord_to_twilio())

            elif event == 'media':
                mulaw = base64.b64decode(data['media']['payload'])
                pcm = await loop.run_in_executor(None, _mulaw_to_discord_pcm, mulaw)
                try:
                    self.twilio_to_discord.put_nowait(pcm)
                except asyncio.QueueFull:
                    pass

            elif event == 'stop':
                await self._cleanup(cancel_call=False)
                break

        self.stream_ws = None
        return ws

    async def _forward_discord_to_twilio(self) -> None:
        """Background task: consume Discord audio frames and send them to Twilio."""
        loop = asyncio.get_running_loop()
        while self.stream_ws and not self.stream_ws.closed:
            try:
                pcm = await asyncio.wait_for(self.discord_to_twilio.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            mulaw = await loop.run_in_executor(None, _discord_pcm_to_mulaw, pcm)
            if not mulaw:
                continue
            payload = {
                'event': 'media',
                'streamSid': self.stream_sid,
                'media': {'payload': base64.b64encode(mulaw).decode()},
            }
            try:
                await self.stream_ws.send_str(json.dumps(payload))
            except Exception:
                break

    # ------------------------------------------------------------------
    # Shared teardown
    # ------------------------------------------------------------------

    async def _cleanup(self, cancel_call: bool = True) -> None:
        if cancel_call and self.active_call_sid:
            call_sid = self.active_call_sid
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self.twilio.calls(call_sid).update(status='completed'),
                )
            except Exception:
                pass
        self.active_call_sid = None

        if self._bridge_task:
            self._bridge_task.cancel()
            self._bridge_task = None

        if self.voice_client:
            try:
                if self.voice_client.is_listening():
                    self.voice_client.stop_listening()
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                await self.voice_client.disconnect()
            except Exception:
                pass
            self.voice_client = None

        # Drain queues so they're ready for the next call
        while not self.twilio_to_discord.empty():
            self.twilio_to_discord.get_nowait()
        while not self.discord_to_twilio.empty():
            self.discord_to_twilio.get_nowait()

        self.stream_sid = None

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    @app_commands.command(name='call', description='Call a phone number and bridge audio to your voice channel')
    @app_commands.describe(phone_number='Phone number in E.164 format, e.g. +15551234567')
    async def call_cmd(self, interaction: discord.Interaction, phone_number: str) -> None:
        if not E164_RE.match(phone_number):
            embed = discord.Embed(
                title='Invalid Phone Number',
                description='Please use E.164 format, e.g. `+15551234567`.',
                color=0xff6b6b,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not interaction.user.voice or not interaction.user.voice.channel:
            embed = discord.Embed(
                title='Not in a Voice Channel',
                description='You must be in a voice channel to make a call.',
                color=0xff6b6b,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.active_call_sid:
            embed = discord.Embed(
                title='Call Already Active',
                description='A call is already in progress. Use `/hangup` first.',
                color=0xff6b6b,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.defer()

        channel = interaction.user.voice.channel
        try:
            self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
        except Exception as e:
            embed = discord.Embed(
                title='Failed to Join Voice Channel',
                description=f'Could not connect to **{channel.name}**: {e}',
                color=0xff6b6b,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        sink = TwilioAudioSink(self.discord_to_twilio)
        self.voice_client.listen(sink)

        base_url = os.getenv('WEBHOOK_BASE_URL', '')
        loop = asyncio.get_running_loop()
        try:
            call = await loop.run_in_executor(
                None,
                lambda: self.twilio.calls.create(
                    to=phone_number,
                    from_=os.getenv('TWILIO_PHONE_NUMBER'),
                    url=f'https://{base_url}/voice',
                ),
            )
            self.active_call_sid = call.sid
        except Exception as e:
            await self._cleanup(cancel_call=False)
            embed = discord.Embed(
                title='Twilio Error',
                description=f'Failed to initiate call: {e}',
                color=0xff6b6b,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title='Calling...',
            description=f'Dialing **{phone_number}**.\nAudio will bridge once the call is answered.',
            color=0x4ecdc4,
        )
        embed.set_footer(text='Use /hangup to end the call.')
        await interaction.followup.send(embed=embed)

    @app_commands.command(name='hangup', description='End the current phone call')
    async def hangup_cmd(self, interaction: discord.Interaction) -> None:
        if not self.active_call_sid:
            embed = discord.Embed(
                title='No Active Call',
                description='There is no call in progress.',
                color=0xff6b6b,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.defer()
        await self._cleanup(cancel_call=True)

        embed = discord.Embed(
            title='Call Ended',
            description='The phone call has been disconnected.',
            color=0x4ecdc4,
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoipCog(bot))
