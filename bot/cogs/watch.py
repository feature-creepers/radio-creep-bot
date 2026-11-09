"""Surveillance : prévient le propriétaire (MP) si RADIO CREEP décroche du salon vocal.

Le son de la radio passe par le compte RADIO CREEP connecté dans Chrome. Si l'onglet est
fermé, le réseau coupe ou le PC redémarre, il sort du salon (ou a le micro coupé) : la
radio est muette. Le bot le voit via les états vocaux et envoie un MP après un délai de
grâce (les micro-coupures se reconnectent seules), puis un autre quand il revient.
Inutile quand le bot diffuse lui-même dans le salon (/diffusion)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from ..client import RadioBot

log = logging.getLogger(__name__)

GRACE_SECONDS = 45.0


class Watch(commands.Cog):
    def __init__(self, bot: RadioBot):
        self.bot = bot
        self.down_since: float | None = None
        self.alerted = False
        self._pending: asyncio.Task | None = None
        self.periodic.start()

    def cog_unload(self) -> None:
        self.periodic.cancel()

    def radio_problem(self) -> str:
        """Vide si la radio diffuse normalement, sinon la raison."""
        cfg = self.bot.config
        rid = cfg.radio_account_id
        if not rid:
            return ""
        st = self.bot.streamer
        if st.active and st.channel_id == cfg.voice_channel_id:
            return ""  # le bot diffuse lui-même dans le salon
        guild, vc = self.bot.guild, self.bot.voice_channel()
        if guild is None or vc is None:
            return ""
        m = guild.get_member(rid)
        if m is None or m.voice is None or m.voice.channel is None or m.voice.channel.id != vc.id:
            return f"n'est plus dans {vc.mention}"
        if m.voice.self_mute or m.voice.mute:
            return f"a le micro coupé dans {vc.mention}"
        return ""

    def schedule(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        self._pending = asyncio.create_task(self._check())

    async def _check(self) -> None:
        why = self.radio_problem()
        if not why:
            if self.alerted and self.down_since is not None:
                mins = max(1, round((time.time() - self.down_since) / 60))
                await self.bot.alert_owner(f"✅ RADIO CREEP diffuse à nouveau (coupure d'environ {mins} min).")
                log.info("RADIO CREEP de retour")
            self.alerted = False
            self.down_since = None
            return
        if self.down_since is None:
            self.down_since = time.time()
        if self.alerted:
            return
        await asyncio.sleep(GRACE_SECONDS)
        why = self.radio_problem()
        if why and not self.alerted:
            self.alerted = True
            log.warning("RADIO CREEP %s", why)
            await self.bot.alert_owner(
                f"📻 **La radio est muette ?** RADIO CREEP {why} depuis plus de {GRACE_SECONDS:.0f} s.\n"
                "Rouvre l'onglet Discord du compte RADIO CREEP dans Chrome et reconnecte-le au salon "
                "(onglet fermé, coupure réseau ou PC redémarré ?). Je te préviens quand il revient."
            )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.schedule()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.id == self.bot.config.radio_account_id:
            self.schedule()

    @tasks.loop(minutes=2)
    async def periodic(self) -> None:
        # filet de sécurité (événement manqué, démarrage) : ne relance pas un délai en cours
        if self._pending is None or self._pending.done():
            self.schedule()

    @periodic.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()
