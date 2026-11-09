"""Embeds et boutons partagés."""
from __future__ import annotations

import io
import re
from typing import TYPE_CHECKING

import discord

from .smtc import NowPlaying
from .textutil import clip, fmt_duration

if TYPE_CHECKING:
    from .client import RadioBot
    from .jukebox import Request

COLOR = discord.Color.from_rgb(250, 45, 72)  # rouge « Apple Music »


# ------------------------------------------------------------ now playing
def progress_bar(pos: float, dur: float, width: int = 16) -> str:
    if dur <= 0:
        return ""
    filled = max(0, min(width, round(width * pos / dur)))
    return "▬" * filled + "🔘" + "▬" * (width - filled)


def now_embed(np: NowPlaying, req: Request | None = None, *, with_progress: bool = True) -> discord.Embed:
    state = "▶️ En lecture" if np.playing else "⏸️ En pause"
    e = discord.Embed(title=clip(np.title, 250), color=COLOR)
    e.set_author(name=f"📻 Radio Creep · {state}")
    desc = [f"**{np.artist}**" if np.artist else ""]
    if np.album:
        desc.append(f"*{np.album}*")
    if with_progress and np.duration > 0:
        desc.append(f"`{fmt_duration(np.position)}` {progress_bar(np.position, np.duration)} `{fmt_duration(np.duration)}`")
    if req is not None:
        desc.append(f"🙋 Demandé par <@{req.requester_id}>")
    e.description = "\n".join(d for d in desc if d)
    return e


def cover_file(data: bytes | None) -> discord.File | None:
    if not data:
        return None
    ext = "png" if data[:4] == b"\x89PNG" else "jpg"
    return discord.File(io.BytesIO(data), filename=f"cover.{ext}")


def attach_cover(embed: discord.Embed, file: discord.File | None) -> None:
    if file is not None:
        embed.set_thumbnail(url=f"attachment://{file.filename}")


# ------------------------------------------------------------ aide épinglée
HELP_LINES = [
    ("/now", "Morceau en cours (titre, artiste, pochette)"),
    ("/search `texte ou lien`", "Chercher dans les fichiers de la radio et tout Apple Music"),
    ("/request `morceau ou lien`", "Proposer un morceau (avec autocomplétion) → file d'attente"),
    ("/queue", "Voir la file d'attente"),
    ("/skip", "Voter pour passer le morceau (majorité des auditeurs de -radio)"),
    ("/favorite · ⭐ sous une annonce", "Ajouter le morceau à tes favoris"),
    ("/favorites `[membre]`", "Voir tes favoris (ou ceux d'un membre) et les redemander"),
]
DJ_LINES = [
    ("/pause · /play · /next", "Contrôle de la lecture"),
    ("/playlist `nom` `[aleatoire]`", "Basculer sur une playlist"),
    ("/mode `auto|approve|manual`", "Mode de traitement des demandes"),
    ("/retirer `id`", "Retirer une demande de la file"),
    ("/reload", "Recharger bibliothèque et playlists"),
    ("/diffusion `démarrer|arrêter|état`", "Le bot diffuse lui-même la radio (sans Chrome)"),
    ("/station `maintenant|auto`", "Station dans le style du morceau (auto quand la file est vide)"),
    ("/volume `0-100`", "Volume de l'appli Apple Music (sans valeur : l'affiche)"),
]


def help_embed(voice_channel: str) -> discord.Embed:
    e = discord.Embed(
        title="📻 Radio Creep : le jukebox",
        description=f"La radio tourne 24/7 dans {voice_channel}. Pilote-la avec ces commandes :",
        color=COLOR,
    )
    e.add_field(name="🎧 Pour tout le monde", value="\n".join(f"**{c}** : {d}" for c, d in HELP_LINES), inline=False)
    e.add_field(name="🎛️ Rôle DJ", value="\n".join(f"**{c}** : {d}" for c, d in DJ_LINES), inline=False)
    e.set_footer(text="Le titre en cours est annoncé ici à chaque changement.")
    return e


# ------------------------------------------------------------ demandes (notifications DJ)
STATUS_LABEL = {
    "pending": "⏳ En attente d'un DJ",
    "approved": "🕒 Acceptée, attend son tour",
    "queued": "✅ Dans Apple Music",
    "played": "🎶 Jouée",
    "rejected": "❌ Refusée",
    "removed": "🗑️ Retirée",
}


def request_embed(bot: RadioBot, req: Request, failure: str = "", footer: str = "") -> discord.Embed:
    e = discord.Embed(title=f"Demande #{req.id} : {clip(req.title, 200)}", color=COLOR)
    e.description = f"**{req.artist or '?'}**" + (f"\n*{req.album}*" if req.album else "")
    e.add_field(name="Par", value=f"<@{req.requester_id}> ({req.requester_name})")
    e.add_field(name="Durée", value=fmt_duration(req.duration))
    e.add_field(name="Statut", value=STATUS_LABEL.get(req.status, req.status))
    if req.from_catalog:
        e.add_field(name="Source", value="🍎 Catalogue Apple Music")
    if req.artwork:
        e.set_thumbnail(url=req.artwork)
    if failure:
        e.add_field(name="⚠️ Ajout automatique impossible", value=clip(failure, 1000), inline=False)
    if req.status == "pending":
        how = {
            "approve": "« Ajouter » : le bot le passe juste après le morceau en cours (ou après les demandes déjà acceptées).",
            "manual": "Ajoute-le toi-même dans Apple Music (clic droit ▸ Play Next), puis « C'est fait ».",
            "auto": "Réessaie « Ajouter », ou ajoute-le à la main puis « C'est fait ».",
        }[bot.request_mode]
        e.set_footer(text=how)
    elif footer:
        e.set_footer(text=footer)
    return e


class RequestButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rcb:(?P<action>add|done|reject):(?P<rid>\d+)"):
    """Boutons persistants (survivent aux redémarrages du bot)."""

    STYLES = {
        "add": ("Ajouter", "▶️", discord.ButtonStyle.success),
        "done": ("C'est fait", "✅", discord.ButtonStyle.primary),
        "reject": ("Refuser", "❌", discord.ButtonStyle.danger),
    }

    def __init__(self, action: str, rid: int):
        label, emoji, style = self.STYLES[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"rcb:{action}:{rid}"))
        self.action = action
        self.rid = rid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["action"], int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: RadioBot = interaction.client  # type: ignore[assignment]
        user = interaction.user
        if bot.guild and not isinstance(user, discord.Member):
            user = bot.guild.get_member(user.id) or user
        if not bot.is_dj(user):
            await interaction.response.send_message("🔒 Réservé au rôle DJ.", ephemeral=True)
            return
        req = bot.jukebox.get(self.rid)
        if req is None or req.status != "pending":
            status = STATUS_LABEL.get(req.status, "?") if req else "introuvable"
            await interaction.response.edit_message(view=None)
            await interaction.followup.send(f"Demande #{self.rid} déjà traitée ({status}).", ephemeral=True)
            return

        who = interaction.user.display_name
        if self.action == "add":
            await interaction.response.defer(thinking=True, ephemeral=True)
            msg = await bot.approve(req)
            if req.status == "pending":  # le push a échoué : une nouvelle notification est partie
                await interaction.followup.send(
                    f"⚠️ Échec : {req.note}\nAjoute-le à la main dans Apple Music puis clique « C'est fait ».",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(msg, ephemeral=True)
            await bot.refresh_notifications(req, f"Acceptée par {who}")
            await _tell_requester(bot, req, f"✅ Ta demande **{req.label}** est dans la file de la radio !")
        elif self.action == "done":
            req.pushed = False
            bot.jukebox.set_status(req, "queued")
            await interaction.response.send_message("👍 Noté.", ephemeral=True)
            await bot.refresh_notifications(req, f"Ajoutée à la main par {who}")
            await _tell_requester(bot, req, f"✅ Ta demande **{req.label}** est dans la file de la radio !")
        else:
            bot.jukebox.set_status(req, "rejected", note=f"par {who}")
            await interaction.response.send_message("Demande refusée.", ephemeral=True)
            await bot.refresh_notifications(req, f"Refusée par {who}")
            await _tell_requester(bot, req, f"❌ Ta demande **{req.label}** n'a pas été retenue cette fois.")


def request_view(bot: RadioBot, req: Request) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    actions = ("done", "reject") if bot.request_mode == "manual" else ("add", "done", "reject")
    for a in actions:
        view.add_item(RequestButton(a, req.id))
    return view


async def _tell_requester(bot: RadioBot, req: Request, text: str) -> None:
    """Prévient le demandeur dans le salon texte de la radio (mention, sans notification push)."""
    ch = bot.text_channel()
    if ch is None:
        return
    try:
        await ch.send(f"<@{req.requester_id}> {text}", allowed_mentions=discord.AllowedMentions(users=True), silent=True)
    except discord.HTTPException:
        pass
