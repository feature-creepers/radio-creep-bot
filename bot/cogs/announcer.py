"""Message d'aide épinglé + annonce du titre à chaque changement + statut du bot."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..smtc import NowPlaying
from ..textutil import clip
from ..views import attach_cover, cover_file, help_embed, now_embed

if TYPE_CHECKING:
    from ..client import RadioBot

log = logging.getLogger(__name__)

MIN_SECONDS_BETWEEN_ANNOUNCES = 3.0  # anti-rafale si on enchaîne les « suivant »


class Announcer(commands.Cog):
    def __init__(self, bot: RadioBot):
        self.bot = bot
        self._pinned_done = False
        self._last_announce = 0.0
        self._pending: asyncio.Task | None = None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._pinned_done:
            self._pinned_done = True
            await self.ensure_pinned_help()

    async def ensure_pinned_help(self) -> None:
        ch = self.bot.text_channel()
        if ch is None:
            log.error("TEXT_CHANNEL_ID introuvable ou pas un salon texte")
            return
        vc = self.bot.voice_channel()
        embed = help_embed(vc.mention if vc else "le salon vocal")
        mid = self.bot.store.get("help_message_id")
        if mid:
            try:
                msg = await ch.fetch_message(mid)
                await msg.edit(content=None, embed=embed)
                if not msg.pinned:
                    await msg.pin(reason="Aide de la radio")
                return
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                log.warning("mise à jour du message épinglé impossible : %s", e)
                return
        try:
            msg = await ch.send(embed=embed)
            self.bot.store.set("help_message_id", msg.id)
            await msg.pin(reason="Aide de la radio")
            log.info("message d'aide créé et épinglé")
        except discord.Forbidden:
            log.warning("pas la permission d'envoyer/épingler dans #%s (voir README : permissions)", ch.name)

    # ------------------------------------------------------------ changement de titre
    @commands.Cog.listener()
    async def on_track_change(self, np: NowPlaying, req) -> None:
        await self._set_presence(np)
        # Regroupe les changements rapprochés : on n'annonce que le titre qui « reste »
        if self._pending and not self._pending.done():
            self._pending.cancel()
        self._pending = asyncio.create_task(self._announce_later(np, req))

    @commands.Cog.listener()
    async def on_playback_state(self, np: NowPlaying) -> None:
        await self._set_presence(np)

    async def _announce_later(self, np: NowPlaying, req) -> None:
        wait = max(0.0, MIN_SECONDS_BETWEEN_ANNOUNCES - (time.monotonic() - self._last_announce))
        await asyncio.sleep(max(wait, 1.5))
        cur = self.bot.now
        if cur is None or cur.key != np.key:
            return  # déjà changé entre-temps
        self._last_announce = time.monotonic()
        ch = self.bot.text_channel()
        if ch is None:
            return
        embed = now_embed(cur, req, with_progress=False)
        file = cover_file(await self.bot.media.thumbnail())
        attach_cover(embed, file)
        from .favorites import fav_view

        view = fav_view(self.bot.favorites.remember(cur.title, cur.artist, cur.album))
        try:
            msg = await ch.send(embed=embed, view=view, silent=True, **({"file": file} if file else {}))
        except discord.HTTPException as e:
            log.warning("annonce impossible : %s", e)
            return
        if self.bot.config.announce_delete_previous:
            prev = self.bot.store.get("last_announce_id")
            if prev:
                try:
                    await ch.get_partial_message(prev).delete()
                except discord.HTTPException:
                    pass
        self.bot.store.set("last_announce_id", msg.id)

    async def _set_presence(self, np: NowPlaying) -> None:
        name = clip(f"{np.title} — {np.artist}" if np.artist else np.title, 120)
        if not np.playing:
            name = f"⏸ {name}"
        try:
            await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=name))
        except Exception:
            pass
