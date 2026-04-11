import asyncio
import base64
import json
import os
import random
import re
import wave

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


class LoopingPCMAudioSource(discord.AudioSource):
    """Loops a pre-computed PCM buffer indefinitely, serving 20ms frames. Used for ringtone playback."""

    FRAME_SIZE = 3840  # 20 ms at 48 kHz stereo 16-bit

    def __init__(self, pcm_data: bytes):
        self._data = pcm_data
        self._pos = 0

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        needed = self.FRAME_SIZE
        result = bytearray()
        while len(result) < needed:
            chunk = self._data[self._pos:self._pos + (needed - len(result))]
            result.extend(chunk)
            self._pos += len(chunk)
            if self._pos >= len(self._data):
                self._pos = 0
        return bytes(result)


class TwilioAudioSink(voice_recv.AudioSink):
    """Captures Discord VC audio, mixes all speakers, and enqueues for Twilio."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._q = queue
        self._loop = loop
        self._mix_buf: np.ndarray | None = None  # accumulator for current frame

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data) -> None:
        if not data.pcm:
            return
        incoming = np.frombuffer(data.pcm, dtype=np.int16).astype(np.int32)
        if self._mix_buf is None:
            self._mix_buf = incoming
        else:
            # Pad/trim so lengths match, then sum
            length = max(len(self._mix_buf), len(incoming))
            a = np.pad(self._mix_buf, (0, length - len(self._mix_buf)))
            b = np.pad(incoming,       (0, length - len(incoming)))
            self._mix_buf = a + b

        # Flush: clip to int16 range and enqueue the mixed frame
        mixed = np.clip(self._mix_buf, -32768, 32767).astype(np.int16)
        self._mix_buf = None

        # Thread-safe: schedule on the event loop so asyncio.Queue is touched only
        # from the loop thread (prevents lost wakeup when a waiter is registered).
        self._loop.call_soon_threadsafe(self._enqueue, mixed.tobytes())

    def _enqueue(self, data: bytes) -> None:
        while self._q.qsize() > 5:
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self._q.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def cleanup(self) -> None:
        self._mix_buf = None
        

class IncomingCallView(discord.ui.View):
    """Buttons for accepting or declining an inbound call."""

    def __init__(self, accepted: asyncio.Event, declined: asyncio.Event):
        super().__init__(timeout=None)  # timeout managed externally by _ring_task
        self._accepted = accepted
        self._declined = declined

    @discord.ui.button(label='Pick Up', style=discord.ButtonStyle.success, emoji='📞')
    async def pick_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        await interaction.response.edit_message(view=self)
        self._accepted.set()

    @discord.ui.button(label='Hang Up', style=discord.ButtonStyle.danger, emoji='📵')
    async def hang_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        await interaction.response.edit_message(view=self)
        self._declined.set()


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
        self.twilio_to_discord: asyncio.Queue[bytes] = asyncio.Queue(maxsize=20)
        self.discord_to_twilio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=20)
        self._bridge_task: asyncio.Task | None = None
        self._runner: web.AppRunner | None = None

        # Inbound call state
        self._is_inbound: bool = False
        self._caller_number: str | None = None
        self._ring_task: asyncio.Task | None = None
        self._ring_accepted: asyncio.Event = asyncio.Event()
        self._ring_declined: asyncio.Event = asyncio.Event()
        self._ring_message: discord.Message | None = None
        self._ring_channel: discord.VoiceChannel | None = None
        self._ringtone_pcm: bytes = b''

    async def cog_load(self) -> None:
        self._ringtone_pcm = self._load_ringtone_pcm()
        asyncio.create_task(self._start_web_server())

    async def cog_unload(self) -> None:
        await self._cleanup(cancel_call=False)
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------
    # Audio helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_ringtone_pcm() -> bytes:
        """Load assets/ringtone.wav and convert to 48 kHz stereo 16-bit PCM.

        Falls back to a generated 440 Hz tone if the file is not found.
        Handles any sample rate or channel count in the source WAV.
        """
        wav_path = os.path.join(os.path.dirname(__file__), '..', 'assets', 'ringtone.wav')
        wav_path = os.path.normpath(wav_path)
        if os.path.isfile(wav_path):
            with wave.open(wav_path, 'rb') as wf:
                src_rate = wf.getframerate()
                src_channels = wf.getnchannels()
                src_width = wf.getsampwidth()
                raw = wf.readframes(wf.getnframes())

            # Normalise to int16
            if src_width == 1:
                audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) << 8
            elif src_width == 2:
                audio = np.frombuffer(raw, dtype=np.int16).copy()
            elif src_width == 4:
                audio = (np.frombuffer(raw, dtype=np.int32) >> 16).astype(np.int16)
            else:
                audio = np.frombuffer(raw, dtype=np.int16).copy()

            # Reshape to (frames, channels)
            audio = audio.reshape(-1, src_channels)

            # Mix down to mono if needed
            if src_channels > 1:
                audio = audio.mean(axis=1).astype(np.int16)
            else:
                audio = audio[:, 0]

            # Resample to 48 kHz if needed
            if src_rate != 48000:
                audio = soxr.resample(audio.astype(np.float32), src_rate, 48000).astype(np.int16)

            # Duplicate mono channel to stereo
            stereo = np.repeat(audio, 2)
            return stereo.tobytes()

        # Fallback: generated 440 Hz beep (0.4s on / 0.6s off)
        sample_rate = 48000
        on_samples = int(sample_rate * 0.4)
        off_samples = int(sample_rate * 0.6)
        t = np.linspace(0, 0.4, on_samples, endpoint=False)
        tone = (np.sin(2 * np.pi * 440.0 * t) * 32767 * 0.5).astype(np.int16)
        silence = np.zeros(off_samples, dtype=np.int16)
        cycle_stereo = np.repeat(np.concatenate([tone, silence]), 2)
        return cycle_stereo.tobytes()

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
        """Return TwiML based on call direction. Handles inbound rejection paths."""
        base_url = os.getenv('WEBHOOK_BASE_URL', '')
        post_data = await request.post()
        direction = post_data.get('Direction', '')
        caller = post_data.get('From', '')
        call_sid = post_data.get('CallSid', '')

        # Reject if already in a call
        if self.active_call_sid:
            xml = '<?xml version="1.0" encoding="UTF-8"?>\n<Response><Reject reason="busy"/></Response>'
            return web.Response(text=xml, content_type='text/xml')

        if direction == 'inbound':
            target_vc = self._find_best_voice_channel()
            if target_vc is None:
                xml = (
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<Response>'
                    '<Say voice="alice">You have reached Object land. No one is available right now. Please try again later.</Say>'
                    '<Hangup/>'
                    '</Response>'
                )
                return web.Response(text=xml, content_type='text/xml')
            self._is_inbound = True
            self._caller_number = caller
            self.active_call_sid = call_sid

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
                if self._is_inbound:
                    self._ring_task = asyncio.create_task(self._handle_inbound_ring())
                else:
                    # Outbound: voice_client already connected, start bridge immediately
                    if self.voice_client and not self.voice_client.is_playing():
                        source = QueueAudioSource(self.twilio_to_discord)
                        self.voice_client.play(source)
                    self._bridge_task = asyncio.create_task(self._forward_discord_to_twilio())

            elif event == 'media':
                # Discard caller audio during inbound ringing phase (before bridge starts)
                if self._is_inbound and self._bridge_task is None:
                    continue
                mulaw = base64.b64decode(data['media']['payload'])
                pcm = await loop.run_in_executor(None, _mulaw_to_discord_pcm, mulaw)
                # Evict stale frames to stay near the live edge (~100 ms cap)
                while self.twilio_to_discord.qsize() > 5:
                    try:
                        self.twilio_to_discord.get_nowait()
                    except asyncio.QueueEmpty:
                        break
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
    # Inbound call helpers
    # ------------------------------------------------------------------

    def _find_best_voice_channel(self) -> discord.VoiceChannel | None:
        """Return the voice channel with the most non-bot members, random tiebreak."""
        if not self.bot.guilds:
            return None
        guild = self.bot.guilds[0]
        best: list[discord.VoiceChannel] = []
        best_count = 0
        for vc in guild.voice_channels:
            count = sum(1 for m in vc.members if not m.bot)
            if count > best_count:
                best_count = count
                best = [vc]
            elif count == best_count and count > 0:
                best.append(vc)
        return random.choice(best) if best else None

    async def _handle_inbound_ring(self) -> None:
        """Join a VC, play a ringtone, and wait for a Discord user to accept or decline."""
        target_vc = self._find_best_voice_channel()
        if target_vc is None:
            # Race condition: everyone left between POST and WebSocket start
            await self._cleanup(cancel_call=True)
            return

        try:
            self.voice_client = await target_vc.connect(cls=voice_recv.VoiceRecvClient)
        except Exception:
            await self._cleanup(cancel_call=True)
            return

        self.voice_client.play(LoopingPCMAudioSource(self._ringtone_pcm))

        self._ring_accepted.clear()
        self._ring_declined.clear()
        view = IncomingCallView(self._ring_accepted, self._ring_declined)
        self._ring_channel = target_vc

        caller_display = self._caller_number or 'Unknown'
        embed = discord.Embed(
            title='Incoming Call',
            description=f'📞 Incoming call from **{caller_display}**',
            color=0x57f287,
        )
        embed.set_footer(text='You have 30 seconds to answer.')

        if self._ring_channel:
            try:
                self._ring_message = await self._ring_channel.send(embed=embed, view=view)
            except Exception:
                self._ring_message = None

        # Wait up to 30s for Pick Up or Hang Up
        t_accepted = asyncio.create_task(self._ring_accepted.wait())
        t_declined = asyncio.create_task(self._ring_declined.wait())
        done, pending = await asyncio.wait(
            [t_accepted, t_declined],
            timeout=30.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        accepted = self._ring_accepted.is_set() and t_accepted in done

        if accepted:
            await self._start_inbound_bridge()
        else:
            await self._reject_inbound_call()

    async def _start_inbound_bridge(self) -> None:
        """Transition from ringtone to full bidirectional audio bridge."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

        if self.voice_client:
            self.voice_client.play(QueueAudioSource(self.twilio_to_discord))
            self.voice_client.listen(TwilioAudioSink(self.discord_to_twilio, asyncio.get_running_loop()))

        self._bridge_task = asyncio.create_task(self._forward_discord_to_twilio())

        if self._ring_message:
            caller_display = self._caller_number or 'Unknown'
            embed = discord.Embed(
                title='Call Connected',
                description=f'📞 In call with **{caller_display}**',
                color=0x4ecdc4,
            )
            embed.set_footer(text='Use /hangup to end the call.')
            try:
                await self._ring_message.edit(embed=embed, view=None)
            except Exception:
                pass
        self._ring_message = None

    async def _reject_inbound_call(self) -> None:
        """Update the notification embed and clean up after a declined or timed-out call."""
        if self._ring_message:
            embed = discord.Embed(
                title='Missed Call',
                description=f'📵 Missed call from **{self._caller_number or "Unknown"}**',
                color=0xff6b6b,
            )
            try:
                await self._ring_message.edit(embed=embed, view=None)
            except Exception:
                pass
        self._ring_message = None
        await self._cleanup(cancel_call=True)

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

        # Cancel ring task if still running
        if self._ring_task and not self._ring_task.done():
            self._ring_task.cancel()
        self._ring_task = None

        # Reset inbound state
        self._is_inbound = False
        self._caller_number = None
        self._ring_accepted.clear()
        self._ring_declined.clear()

        # Best-effort update of any pending ring message
        if self._ring_message:
            embed = discord.Embed(
                title='Call Ended',
                description='The call was disconnected.',
                color=0xff6b6b,
            )
            try:
                asyncio.get_event_loop().create_task(self._ring_message.edit(embed=embed, view=None))
            except Exception:
                pass
        self._ring_message = None
        self._ring_channel = None

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

        sink = TwilioAudioSink(self.discord_to_twilio, asyncio.get_running_loop())
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
