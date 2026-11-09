"""Favoris : /favorite, /favorites et bouton ⭐ sous chaque annonce de titre."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..applemusic_ui import UIError
from ..favorites import track_key
from ..library import Track
from ..textutil import clip, same_title

if TYPE_CHECKING:
    from ..client import RadioBot

log = logging.getLogger(__name__)


def _label(title: str, artist: str) -> str:
    return f"{title} — {artist}" if artist else title


class FavButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rcb:fav:(?P<key>[0-9a-f]{10})"):
    """Bouton ⭐ persistant sous les annonces (marche aussi après un redémarrage du bot)."""

    def __init__(self, key: str):
        super().__init__(
            discord.ui.Button(label="Favori", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id=f"rcb:fav:{key}")
        )
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: RadioBot = interaction.client  # type: ignore[assignment]
        info = bot.favorites.lookup(self.key)
        if info is None:
            await interaction.response.send_message("Ce morceau est trop ancien, je ne m'en souviens plus.", ephemeral=True)
            return
        cog: Favoris = bot.get_cog("Favoris")  # type: ignore[assignment]
        await cog.add_for(interaction, *info)


def fav_view(key: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(FavButton(key))
    return view


class _FavSelect(discord.ui.Select):
    def __init__(self, items: list[dict], placeholder: str):
        options = [
            discord.SelectOption(
                label=clip(f["t"], 100), description=clip(f["a"] or "?", 100), value=track_key(f["t"], f["a"])
            )
            for f in items[:25]
        ]
        super().__init__(placeholder=placeholder, options=options)
        self.items = {track_key(f["t"], f["a"]): f for f in items[:25]}


class FavRequestSelect(_FavSelect):
    def __init__(self, items: list[dict]):
        super().__init__(items, "🙋 Demander un de ces favoris…")

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: RadioBot = interaction.client  # type: ignore[assignment]
        f = self.items[self.values[0]]
        if self.view is not None:
            self.view.stop()
        await interaction.response.edit_message(content=f"🙋 Choisi : **{clip(_label(f['t'], f['a']), 150)}**", embed=None, view=None)
        # fichier de la radio si on l'a, sinon le catalogue Apple Music (recherche titre + artiste)
        track = bot.library.find_local(f["t"], f["a"])
        if track is None:
            track = Track(id=f"ct:fav:{self.values[0]}", title=f["t"], artist=f["a"], album=f.get("al", ""), duration=0)
            bot.catalog.tracks[track.id] = track
        radio = bot.get_cog("Radio")
        await radio.do_request(interaction, track.id)  # type: ignore[union-attr]


class FavRemoveSelect(_FavSelect):
    def __init__(self, items: list[dict]):
        super().__init__(items, "🗑️ Retirer un favori…")

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: RadioBot = interaction.client  # type: ignore[assignment]
        gone = bot.favorites.remove(interaction.user.id, self.values[0])
        if self.view is not None:
            self.view.stop()
        text = f"🗑️ **{clip(_label(gone['t'], gone['a']), 150)}** retiré de tes favoris." if gone else "Déjà retiré."
        await interaction.response.edit_message(content=text, embed=None, view=None)


class FavListView(discord.ui.View):
    def __init__(self, items: list[dict], mine: bool, source: discord.Interaction):
        super().__init__(timeout=300)
        self.source = source
        self.add_item(FavRequestSelect(items))
        if mine:
            self.add_item(FavRemoveSelect(items))

    async def on_timeout(self) -> None:
        try:
            await self.source.edit_original_response(view=None)
        except discord.HTTPException:
            pass


class Favoris(commands.Cog):
    def __init__(self, bot: RadioBot):
        self.bot = bot

    async def apple_favorite(self, title: str) -> str:
        """Marque le morceau en cours « Favorite » dans Apple Music (compte du propriétaire)."""
        np = self.bot.now
        if np is None or not same_title(np.title, title):
            return "🍎 Favoris Apple Music : possible seulement pour le morceau en cours."
        try:
            res = await self.bot.ui_call(self.bot.ui.favorite_current, np.title, idle_wait=0)
        except (UIError, asyncio.TimeoutError) as e:
            return f"🍎 Favoris Apple Music : impossible ({e or 'pas de réponse'})."
        except Exception as e:
            log.exception("favori Apple Music")
            return f"🍎 Favoris Apple Music : erreur ({e})."
        return "🍎 Ajouté aussi à tes Favoris Apple Music." if res == "added" else "🍎 Déjà dans tes Favoris Apple Music."

    async def add_for(self, interaction: discord.Interaction, title: str, artist: str, album: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = interaction.user.id
        added = self.bot.favorites.add(uid, title, artist, album)
        n = len(self.bot.favorites.list(uid))
        label = clip(_label(title, artist), 150)
        lines = [f"⭐ **{label}** ajouté à tes favoris ({n})." if added else f"⭐ **{label}** est déjà dans tes favoris."]
        if uid == self.bot.config.owner_id:
            lines.append(await self.apple_favorite(title))
        lines.append("Ta liste : /favorites")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="favorite", description="Ajouter le morceau en cours à tes favoris")
    async def favorite(self, interaction: discord.Interaction) -> None:
        np = self.bot.now
        if np is None:
            await interaction.response.send_message("🤷 Rien ne joue pour l'instant.", ephemeral=True)
            return
        await self.add_for(interaction, np.title, np.artist, np.album)

    @app_commands.command(name="favorites", description="Voir tes favoris (ou ceux d'un membre) et les demander")
    @app_commands.describe(membre="Voir les favoris de quelqu'un d'autre")
    async def favorites(self, interaction: discord.Interaction, membre: discord.Member | None = None) -> None:
        target = membre or interaction.user
        mine = target.id == interaction.user.id
        items = self.bot.favorites.list(target.id)
        if not items:
            who = "Tu n'as" if mine else f"{target.display_name} n'a"
            await interaction.response.send_message(
                f"{who} encore aucun favori. ⭐ sous une annonce de titre, ou /favorite pendant un morceau.", ephemeral=True
            )
            return
        lines, size = [], 0
        for i, f in enumerate(items, 1):
            line = f"`{i:>2}.` **{clip(f['t'], 60)}** · {clip(f['a'] or '?', 40)}"
            if size + len(line) > 3800:
                lines.append(f"… et {len(items) - i + 1} autre(s)")
                break
            lines.append(line)
            size += len(line) + 1
        title = "⭐ Tes favoris" if mine else f"⭐ Favoris de {target.display_name}"
        e = discord.Embed(title=f"{title} ({len(items)})", description="\n".join(lines), color=0xFA243C)
        e.set_footer(text="Menus : les 25 plus récents. Choisis-en un pour le demander à la radio.")
        await interaction.response.send_message(embed=e, view=FavListView(items, mine, interaction), ephemeral=True)
