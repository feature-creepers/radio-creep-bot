"""Commandes réservées au rôle DJ."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..applemusic_ui import UIError
from ..client import dj_only
from ..config import REQUEST_MODES
from ..textutil import clip, norm

if TYPE_CHECKING:
    from ..client import RadioBot

log = logging.getLogger(__name__)


class DJ(commands.Cog):
    def __init__(self, bot: RadioBot):
        self.bot = bot

    async def _transport(self, interaction: discord.Interaction, action: str, done: str) -> None:
        ok = await getattr(self.bot.media, action)()
        if ok:
            await interaction.response.send_message(f"{done} (par {interaction.user.display_name})")
        else:
            await interaction.response.send_message("⚠️ Apple Music n'a pas répondu (ouvert ?).", ephemeral=True)

    @app_commands.command(name="pause", description="[DJ] Mettre la radio en pause")
    @dj_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        self.bot.station.dj_paused_at = time.monotonic()  # pas de station auto sur une pause voulue
        await self._transport(interaction, "pause", "⏸️ Pause")

    @app_commands.command(name="play", description="[DJ] Reprendre la lecture")
    @dj_only()
    async def play(self, interaction: discord.Interaction) -> None:
        await self._transport(interaction, "play", "▶️ Lecture")

    @app_commands.command(name="next", description="[DJ] Passer au morceau suivant (sans vote)")
    @dj_only()
    async def next(self, interaction: discord.Interaction) -> None:
        await self._transport(interaction, "next", "⏭️ Morceau suivant")

    # ------------------------------------------------------------ /playlist
    @app_commands.command(name="playlist", description="[DJ] Basculer la radio sur une de mes playlists")
    @app_commands.describe(nom="Nom de la playlist", aleatoire="Lecture aléatoire")
    @dj_only()
    async def playlist(self, interaction: discord.Interaction, nom: str, aleatoire: bool = False) -> None:
        await interaction.response.defer(thinking=True)
        if not self.bot.playlists:
            err = await self.bot.refresh_playlists()
            if err:
                await interaction.followup.send(f"⚠️ Playlists indisponibles : {err}")
                return
        target = next((p for p in self.bot.playlists if p.aid == nom), None)
        if target is None:  # texte libre
            n = norm(nom)
            target = next((p for p in self.bot.playlists if norm(p.name) == n), None) or next(
                (p for p in self.bot.playlists if n and n in norm(p.path)), None
            )
        if target is None:
            await interaction.followup.send(f"❓ Playlist « {nom} » introuvable. Essaie /reload si elle est récente.")
            return
        try:
            await self.bot.ui_call(self.bot.ui.play_playlist, target.aid, aleatoire)
        except (UIError, asyncio.TimeoutError) as e:
            await interaction.followup.send(f"⚠️ Impossible de lancer « {target.path} » : {e or 'délai dépassé'}")
            return
        mode = " en aléatoire 🔀" if aleatoire else ""
        await interaction.followup.send(f"💿 La radio passe sur la playlist **{target.path}**{mode} (par {interaction.user.display_name}).")

    @playlist.autocomplete("nom")
    async def playlist_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        n = norm(current)
        items = [p for p in self.bot.playlists if not n or n in norm(p.path)]
        return [
            app_commands.Choice(name=clip(("📁 " if p.is_folder else "") + p.path, 100), value=p.aid) for p in items[:25]
        ]

    # ------------------------------------------------------------ /mode
    @app_commands.command(name="mode", description="[DJ] Mode de traitement des /request")
    @app_commands.describe(mode="auto : direct dans la file · approve : un DJ valide · manual : un DJ ajoute à la main")
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in REQUEST_MODES])
    @dj_only()
    async def mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        self.bot.request_mode = mode.value
        self.bot.store.set("request_mode", mode.value)
        await interaction.response.send_message(f"⚙️ Mode des demandes : **{mode.value}**.", ephemeral=True)

    # ------------------------------------------------------------ /station
    @app_commands.command(name="station", description="[DJ] Station Apple Music dans le style d'un morceau")
    @app_commands.describe(action="maintenant : depuis le morceau en cours · auto : quand la file Apple Music est vide")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="maintenant", value="now"),
            app_commands.Choice(name="auto : activer", value="on"),
            app_commands.Choice(name="auto : désactiver", value="off"),
            app_commands.Choice(name="état", value="status"),
        ]
    )
    @dj_only()
    async def station(self, interaction: discord.Interaction, action: app_commands.Choice[str]) -> None:
        st = self.bot.station
        if action.value in ("on", "off"):
            self.bot.store.set("auto_station", action.value == "on")
            text = (
                "📻 Station automatique **activée** : quand la file Apple Music est vide, je lance une station "
                "dans le style du dernier morceau."
                if action.value == "on"
                else "📻 Station automatique **désactivée**."
            )
            await interaction.response.send_message(text, ephemeral=True)
            return
        if action.value == "status":
            last = f" · dernière : {st.last_seed}" if st.last_seed else ""
            await interaction.response.send_message(
                f"📻 Station automatique : {'activée' if st.enabled else 'désactivée'}{last}", ephemeral=True
            )
            return
        np = self.bot.now
        seed = (np.title, np.artist) if np else tuple(self.bot.store.get("last_track") or ())
        if not seed:
            await interaction.response.send_message("🤷 Aucun morceau de référence.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            await st.launch(seed[0], seed[1])
        except UIError as e:
            await interaction.followup.send(f"⚠️ Station impossible : {e}")
            return
        await interaction.followup.send(
            f"📻 Station lancée dans le style de **{st.last_seed}** (par {interaction.user.display_name})."
        )

    # ------------------------------------------------------------ /volume
    @app_commands.command(name="volume", description="[DJ] Volume de l'appli Apple Music (0 à 100)")
    @app_commands.describe(valeur="De 0 à 100 ; sans valeur : affiche le volume actuel")
    @dj_only()
    async def volume(self, interaction: discord.Interaction, valeur: app_commands.Range[int, 0, 100] | None = None) -> None:
        await interaction.response.defer(ephemeral=valeur is None, thinking=True)
        try:
            got = await self.bot.ui_call(self.bot.ui.volume, None if valeur is None else valeur / 100, idle_wait=3)
        except (UIError, asyncio.TimeoutError) as e:
            await interaction.followup.send(f"⚠️ Volume inaccessible : {e or 'Apple Music ne répond pas'}")
            return
        pct = round(got * 100)
        bar = "▰" * round(pct / 10) + "▱" * (10 - round(pct / 10))
        if valeur is None:
            await interaction.followup.send(f"🔊 Volume Apple Music : **{pct} %** {bar}")
        else:
            log.info("volume Apple Music réglé à %d %% par %s", pct, interaction.user)
            await interaction.followup.send(
                f"🔊 Volume Apple Music réglé à **{pct} %** {bar} (par {interaction.user.display_name})"
            )

    # ------------------------------------------------------------ /diffusion
    @app_commands.command(name="diffusion", description="[DJ] Le bot diffuse lui-même la radio dans un salon vocal")
    @app_commands.describe(action="démarrer, arrêter ou voir l'état", salon="Salon vocal (par défaut : celui de la radio)")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="démarrer", value="start"),
            app_commands.Choice(name="arrêter", value="stop"),
            app_commands.Choice(name="état", value="status"),
        ]
    )
    @dj_only()
    async def diffusion(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        salon: discord.VoiceChannel | None = None,
    ) -> None:
        st = self.bot.streamer
        if action.value == "status":
            await interaction.response.send_message(f"🔊 Diffusion : {st.status()}", ephemeral=True)
            return
        if action.value == "stop":
            await st.stop()
            self.bot.store.set("stream_channel_id", None)
            await interaction.response.send_message("🔇 Diffusion du bot arrêtée.", ephemeral=True)
            return
        target = salon or self.bot.voice_channel()
        if target is None:
            await interaction.response.send_message("Salon vocal introuvable.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await st.start(target.id)
        except Exception as e:
            await st.stop()
            await interaction.followup.send(f"⚠️ Impossible de diffuser dans {target.mention} : {e}", ephemeral=True)
            return
        self.bot.store.set("stream_channel_id", target.id)
        warn = ""
        rid = self.bot.config.radio_account_id
        radio = target.guild.get_member(rid) if rid else None
        if radio and radio.voice and radio.voice.channel and radio.voice.channel.id == target.id and not radio.voice.self_mute:
            warn = "\n⚠️ RADIO CREEP diffuse aussi dans ce salon : le son est **doublé**. Déconnecte-le (ou coupe son micro)."
        await interaction.followup.send(
            f"🔊 Le bot diffuse dans {target.mention} ({st.bitrate} kbps). Il s'y reconnectera tout seul, "
            f"même après un redémarrage. `/diffusion arrêter` pour couper.{warn}",
            ephemeral=True,
        )

    # ------------------------------------------------------------ /retirer
    @app_commands.command(name="retirer", description="[DJ] Retirer une demande de la file du bot")
    @app_commands.describe(id="Numéro de la demande (#)")
    @dj_only()
    async def retirer(self, interaction: discord.Interaction, id: int) -> None:
        req = self.bot.jukebox.get(id)
        if req is None or req.status not in ("pending", "approved", "queued"):
            await interaction.response.send_message(f"Demande #{id} introuvable ou déjà terminée.", ephemeral=True)
            return
        in_apple = req.status == "queued"
        # Déjà dans Apple Music : on ne touche pas à sa file (ça volerait le focus du PC),
        # le bot sautera le morceau s'il démarre.
        req.skip_if_played = in_apple
        self.bot.jukebox.set_status(req, "removed", note=f"par {interaction.user.display_name}")
        await self.bot.refresh_notifications(req, f"Retirée par {interaction.user.display_name}")
        extra = "\nElle était déjà dans Apple Music : je la sauterai si elle démarre." if in_apple else ""
        await interaction.response.send_message(f"🗑️ Demande #{id} (**{req.label}**) retirée.{extra}", ephemeral=True)
        asyncio.create_task(self.bot.pump())

    # ------------------------------------------------------------ /reload
    @app_commands.command(name="reload", description="[DJ] Recharger la bibliothèque et la liste des playlists")
    @dj_only()
    async def reload(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self.bot.reload_library()
        err = await self.bot.refresh_playlists()
        lib = self.bot.library
        pl = f"{len(self.bot.playlists)} playlists" if not err else f"playlists indisponibles ({err})"
        await interaction.followup.send(f"🔄 {len(lib.tracks)} morceaux ({lib.source}) · {pl}.", ephemeral=True)
