"""Commandes publiques : /now /search /request /queue /skip."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..catalog import FETCH_FULL, CatalogUnavailable, full_match, is_apple_link
from ..jukebox import RequestError
from ..library import Track
from ..smtc import NowPlaying
from ..textutil import clip, fmt_duration, norm
from ..views import COLOR, attach_cover, cover_file, now_embed

if TYPE_CHECKING:
    from ..client import RadioBot

log = logging.getLogger(__name__)

# Recherche complète (/search, texte libre de /request) : fichiers de la radio (instantané)
# + recherche « Apple Music » de l'appli (~3 s, ne vole pas le focus, trouve tout le catalogue)
# + API iTunes en parallèle (pochettes, résultats en plus). L'autocomplétion, elle, doit
# répondre en moins de 3 s à chaque frappe : fichiers + API seulement, et un dernier choix
# « Chercher dans tout Apple Music » qui lance la recherche complète.
# Avec CATALOG=false : recherche « Your Library » dans l'appli, quand les fichiers donnent peu.
APPLE_FALLBACK_BELOW = 5
AUTOCOMPLETE_DEBOUNCE = 0.45  # Discord envoie une requête par touche : on attend la fin de la frappe


def _describe(t: Track) -> str:
    if t.from_catalog:
        return clip(f"🍎 {t.artist} · {t.album}" if t.album else f"🍎 {t.artist}", 100)
    if t.from_apple:
        return f"🍎 {t.artist}" if t.artist else "🍎 Apple Music (streaming)"
    return f"📀 {t.artist} · {t.album}" if t.album else f"📀 {t.artist or '?'}"


def _choice_name(t: Track) -> str:
    base = f"{t.title} — {t.artist}" if t.artist else t.title
    if t.from_catalog:
        return clip(f"🍎 {base}" + (" 🅴" if t.explicit else ""), 100)
    return clip(f"📀 {base}", 100)


class SearchSelect(discord.ui.Select):
    def __init__(self, tracks: list[Track]):
        options = [
            discord.SelectOption(label=clip(t.title, 100), description=clip(_describe(t), 100), value=t.id)
            for t in tracks[:25]
        ]
        super().__init__(placeholder="🙋 Demander un de ces morceaux…", options=options)
        self.labels = {t.id: t.label for t in tracks[:25]}

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Radio = interaction.client.get_cog("Radio")  # type: ignore[union-attr,assignment]
        value = self.values[0]
        # Ferme le menu tout de suite (un seul choix par recherche) ; la suite part en followup.
        if self.view is not None:
            self.view.stop()
        await interaction.response.edit_message(
            content=f"🙋 Choisi : **{clip(self.labels.get(value, value), 150)}**", view=None
        )
        await cog.do_request(interaction, value)


class SearchView(discord.ui.View):
    """Menu de choix éphémère : retiré après le choix, ou au bout de 5 min sans choix."""

    def __init__(self, tracks: list[Track], source: discord.Interaction):
        super().__init__(timeout=300)
        self.source = source
        self.add_item(SearchSelect(tracks))

    async def on_timeout(self) -> None:
        try:
            await self.source.edit_original_response(view=None)
        except discord.HTTPException:
            pass


class Radio(commands.Cog):
    def __init__(self, bot: RadioBot):
        self.bot = bot
        self.skip_votes: set[int] = set()
        self.skip_key: tuple | None = None
        self._ac_seq: dict[int, int] = {}  # dernière frappe par membre (anti-rebond de l'autocomplétion)

    # ------------------------------------------------------------ événements
    @commands.Cog.listener()
    async def on_track_change(self, np: NowPlaying, req) -> None:
        self.skip_votes.clear()
        self.skip_key = np.key

    # ------------------------------------------------------------ /now
    @app_commands.command(name="now", description="Le morceau en cours sur la radio")
    async def now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        np = await self.bot.media.now_playing()
        if np is None:
            await interaction.followup.send("🤷 Rien ne joue pour l'instant (Apple Music est-il ouvert ?).")
            return
        req = next((r for r in self.bot.jukebox.requests if r.status == "played" and r.title == np.title), None)
        embed = now_embed(np, req)
        file = cover_file(await self.bot.media.thumbnail())
        attach_cover(embed, file)
        listeners = len(self.bot.listeners())
        embed.set_footer(text=f"🎧 {listeners} auditeur(s) dans le salon")
        await interaction.followup.send(embed=embed, **({"file": file} if file else {}))

    # ------------------------------------------------------------ recherche
    async def apple_search(self, query: str) -> list[Track] | None:
        """Recherche « Your Library » dans l'appli Apple Music (secours si le catalogue est
        injoignable). None si l'appli est indisponible."""
        try:
            songs, artists = await self.bot.ui_call(self.bot.ui.search_library_full, query, idle_wait=0)
        except Exception as e:
            log.info("recherche Apple Music impossible (%s) : %s", query, e)
            return None
        # Les lignes « Songs » ne donnent pas l'artiste. Si la recherche correspond
        # exactement à UN artiste (ex. « black sabbath »), on le lui attribue.
        artist = ""
        tokens = norm(query).split()
        if len(artists) == 1 and all(tok in norm(artists[0]).split() for tok in tokens):
            artist = artists[0]
        out: list[Track] = []
        seen: set[str] = set()
        for title in songs:
            k = norm(title)
            if k and k not in seen:
                seen.add(k)
                out.append(self.bot.library.remember_apple(title, artist, query))
        return out

    def _not_local(self, tracks: list[Track], local: list[Track]) -> list[Track]:
        """Retire du catalogue les morceaux déjà présents en fichier (on garde la version locale)."""
        known = {(norm(t.title), norm(t.artist)) for t in local}
        return [
            t
            for t in tracks
            if (norm(t.title), norm(t.artist)) not in known and not self.bot.library.has_local(t.title, t.artist)
        ]

    async def _app_rows(self, query: str) -> tuple[list[tuple[str, str, bool]] | None, str]:
        try:
            return await self.bot.ui_call(self.bot.ui.search_catalog, query, idle_wait=0), ""
        except asyncio.TimeoutError:
            return None, "l'appli Apple Music n'a pas répondu à temps"
        except Exception as e:
            log.info("recherche dans l'appli impossible (%s) : %s", query, e)
            return None, "l'appli Apple Music n'a pas pu être interrogée"

    async def _api_tracks(self, query: str) -> tuple[list[Track], str]:
        try:
            return await self.bot.catalog.search(query, limit=FETCH_FULL, timeout=5.0, fetch=FETCH_FULL), ""
        except CatalogUnavailable as e:
            log.info("catalogue : %s", e)
            return [], str(e)

    async def find_tracks(self, query: str, limit: int = 10) -> tuple[list[Track], list[Track], str]:
        """(fichiers de la radio, résultats Apple Music en plus, problème éventuel)."""
        catalog = self.bot.catalog
        try:
            linked = await catalog.from_link(query)
        except CatalogUnavailable as e:
            log.info("lien « %s » : %s", query, e)
            return [], [], str(e)
        if linked is not None:
            tracks = [self.bot.library.find_local(t.title, t.artist) or t for t in linked]
            local = [t for t in tracks if not t.from_catalog][:25]
            return local, [t for t in tracks if t.from_catalog][: 25 - len(local)], ""

        local = self.bot.library.search(query, limit=limit)
        if not catalog.enabled:
            return await self._library_fallback(query, local, "catalogue désactivé (CATALOG=false)")
        (rows, app_problem), (api, api_problem) = await asyncio.gather(self._app_rows(query), self._api_tracks(query))
        found = api if rows is None else catalog.from_app(query, rows, api)
        problem = app_problem
        if rows is None and api_problem:
            problem = f"{app_problem} ; {api_problem}"
        more = self._not_local(found, local)[: 25 - len(local)]
        log.info(
            "recherche « %s » : %d fichier(s), %d Apple Music (appli : %s, API : %d)",
            query, len(local), len(more), "indisponible" if rows is None else len(rows), len(api),
        )
        return local, more, problem

    async def _library_fallback(self, query: str, local: list[Track], problem: str) -> tuple[list[Track], list[Track], str]:
        """Sans catalogue : la bibliothèque de l'appli, si les fichiers donnent peu de résultats complets."""
        tokens = set(norm(query).split())
        strong = [t for t in local if tokens <= set(norm(f"{t.title} {t.artist} {t.album}").split())]
        if len(strong) >= APPLE_FALLBACK_BELOW:
            return local, [], problem
        apple = await self.apple_search(query)
        if apple is None:
            return local, [], problem + " ; l'appli Apple Music n'a pas pu être interrogée"
        known = {norm(t.title) for t in local}
        extra = [t for t in apple if norm(t.title) not in known and not self.bot.library.has_local(t.title, t.artist)]
        return local, extra[: 25 - len(local)], problem

    # ------------------------------------------------------------ /search
    @app_commands.command(name="search", description="Chercher un morceau : fichiers de la radio + tout Apple Music")
    @app_commands.describe(texte="Titre, artiste, album ou lien Apple Music")
    async def search(self, interaction: discord.Interaction, texte: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        local, more, problem = await self.find_tracks(texte)
        if not local and not more:
            extra = f" ({problem})" if problem else ""
            await interaction.followup.send(f"Aucun résultat pour « {texte} »{extra}.", ephemeral=True)
            return
        e = discord.Embed(title=f"🔎 « {clip(texte, 80)} »", color=COLOR)
        n = 0
        if local:
            lines = []
            for t in local:
                n += 1
                lines.append(f"`{n:>2}.` **{clip(t.title, 60)}** · {clip(t.artist, 40)} `{fmt_duration(t.duration)}`")
            e.add_field(name="📀 Fichiers de la radio", value=clip("\n".join(lines), 1024), inline=False)
        if more:
            lines = []
            shown = 0
            for t in more:
                who = f" · {clip(t.artist, 40)}" if t.artist else ""
                dur = f" `{fmt_duration(t.duration)}`" if t.duration else ""
                line = f"`{n + 1:>2}.` **{clip(t.title, 60)}**{who}{' 🅴' if t.explicit else ''}{dur}"
                if len("\n".join(lines + [line])) > 950:
                    break
                n += 1
                shown += 1
                lines.append(line)
            if shown < len(more):
                lines.append(f"… et {len(more) - shown} autre(s) dans le menu")
            title = "🍎 Catalogue Apple Music" if more[0].from_catalog else "🍎 Bibliothèque Apple Music (streaming)"
            e.add_field(name=title, value="\n".join(lines), inline=False)
        footer = "Choisis un morceau dans le menu pour le demander."
        if problem:
            footer += f" ⚠️ {problem}"
        e.set_footer(text=clip(footer, 2000))
        view = SearchView(local + more, interaction)
        await interaction.followup.send(embed=e, view=view, ephemeral=True)

    # ------------------------------------------------------------ /request
    @app_commands.command(name="request", description="Proposer un morceau pour la radio (fichiers + tout Apple Music)")
    @app_commands.describe(morceau="Commence à taper puis choisis dans la liste, ou colle un lien Apple Music")
    async def request(self, interaction: discord.Interaction, morceau: str) -> None:
        await self.do_request(interaction, morceau)

    @request.autocomplete("morceau")
    async def request_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        current = current.strip()
        if len(current) < 2:
            return []
        if is_apple_link(current):
            try:
                linked = await self.bot.catalog.from_link(current, timeout=2.0) or []
            except CatalogUnavailable:
                return []
            return [app_commands.Choice(name=_choice_name(t), value=t.id) for t in linked[:25]]
        local = self.bot.library.search(current, limit=25)
        catalog: list[Track] = []
        if len(current) >= 3 and self.bot.catalog.enabled:
            uid = interaction.user.id
            seq = self._ac_seq.get(uid, 0) + 1
            self._ac_seq[uid] = seq
            cached = self.bot.catalog.cached(current)
            if cached is not None:
                catalog = cached
            else:
                await asyncio.sleep(AUTOCOMPLETE_DEBOUNCE)
                if self._ac_seq.get(uid) != seq:
                    return []  # une frappe plus récente est arrivée : cette réponse ne sera pas affichée
                try:
                    catalog = await self.bot.catalog.search(current, limit=25, timeout=1.5)
                except CatalogUnavailable:
                    catalog = []
        catalog = self._not_local(catalog, local)
        # les fichiers de la radio d'abord (8 max s'il y a aussi du catalogue), puis le catalogue
        picked = local[: 8 if catalog else 25]
        picked += catalog[: 25 - len(picked)]
        choices = [app_commands.Choice(name=_choice_name(t), value=t.id) for t in picked]
        if len(current) < 3 or len(current) > 100 or not self.bot.catalog.enabled:
            return choices
        # L'API ne connaît pas tout le catalogue : ce choix (du texte libre) lance la recherche
        # complète dans l'appli. En tête si rien ne contient tous les mots, sinon en fin de liste.
        everything = app_commands.Choice(name=clip(f"🔎 Chercher « {current} » dans tout Apple Music", 100), value=current)
        if not local and not any(full_match(current, t) for t in catalog):
            return [everything] + choices[:24]
        return choices[:14] + [everything]

    async def do_request(self, interaction: discord.Interaction, value: str) -> None:
        track = self.bot.get_track(value)
        if track is None:  # texte libre (pas choisi dans l'autocomplétion)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            local, more, problem = await self.find_tracks(value)
            found = local + more
            if not found:
                why = f" ({problem})" if problem else ""
                await interaction.followup.send(f"❓ « {value} » introuvable{why}. Essaie /search.", ephemeral=True)
                return
            if len(found) > 1:
                view = SearchView(found, interaction)
                await interaction.followup.send(
                    f"🔎 {len(found)} morceaux trouvés pour « {clip(value, 80)} » : lequel ?", view=view, ephemeral=True
                )
                return
            track = found[0]
            await interaction.edit_original_response(content=f"🔎 Trouvé : **{track.label}**")
        member = interaction.user
        deferred = interaction.response.is_done()
        try:
            req = self.bot.jukebox.add(track, member.id, member.display_name, is_dj=self.bot.is_dj(member))
        except RequestError as e:
            if deferred:
                await interaction.followup.send(f"🚫 {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"🚫 {e}", ephemeral=True)
            return
        if not deferred:
            await interaction.response.defer(thinking=True)
        msg = await self.bot.submit_request(req)
        # (si on a déjà répondu en privé, ce message-ci est public : nouveau message)
        await interaction.followup.send(f"{member.mention} {msg}", allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ /queue
    @app_commands.command(name="queue", description="Voir la file d'attente")
    async def queue(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        e = discord.Embed(title="📜 File d'attente", color=COLOR)
        np = self.bot.now
        if np:
            e.add_field(name="▶️ En cours", value=f"**{clip(np.title, 80)}** · {clip(np.artist, 60)}", inline=False)
        icons = {"pending": "⏳", "approved": "🕒", "queued": "✅"}
        order = {"queued": 0, "approved": 1, "pending": 2}
        reqs = sorted(self.bot.jukebox.active(), key=lambda r: (order[r.status], r.id))
        if reqs:
            lines = [
                f"{icons[r.status]} `#{r.id}` **{clip(r.title, 60)}** · {clip(r.artist, 40)} (par {clip(r.requester_name, 30)})"
                for r in reqs
            ]
            e.add_field(name=f"🙋 Demandes ({len(reqs)})", value=clip("\n".join(lines), 1024), inline=False)
        else:
            e.add_field(name="🙋 Demandes", value="Aucune. Lance-toi avec /request !", inline=False)

        if self.bot.config.show_apple_queue:
            try:
                q = await self.bot.ui_call(self.bot.ui.read_queue, idle_wait=5)
                upcoming = q[:8]
                if upcoming:
                    lines = [f"`{i}.` {clip(x.title, 60)} · {clip(x.artist, 40)}" for i, x in enumerate(upcoming, 1)]
                    e.add_field(name="🍎 Ensuite dans Apple Music", value=clip("\n".join(lines), 1024), inline=False)
            except Exception as ex:  # l'interface peut être indisponible : on n'empêche pas /queue
                log.info("file Apple Music illisible : %s", ex)
        e.set_footer(text="✅ passe juste après le morceau en cours · 🕒 acceptée, attend son tour · ⏳ en attente d'un DJ")
        await interaction.followup.send(embed=e)

    # ------------------------------------------------------------ /skip
    @app_commands.command(name="skip", description="Voter pour passer le morceau en cours")
    async def skip(self, interaction: discord.Interaction) -> None:
        listeners = self.bot.listeners()
        ids = {m.id for m in listeners}
        vc = self.bot.voice_channel()
        where = vc.mention if vc else "le salon de la radio"
        if interaction.user.id not in ids:
            await interaction.response.send_message(f"🎧 Il faut être dans {where} (et pas en sourdine) pour voter.", ephemeral=True)
            return
        np = self.bot.now
        if np is None:
            await interaction.response.send_message("Rien à passer.", ephemeral=True)
            return
        if self.skip_key != np.key:
            self.skip_votes.clear()
            self.skip_key = np.key
        self.skip_votes &= ids  # oublie les votants partis du salon
        self.skip_votes.add(interaction.user.id)
        needed = len(ids) // 2 + 1
        have = len(self.skip_votes)
        if have >= needed:
            self.skip_votes.clear()
            ok = await self.bot.media.next()
            text = f"⏭️ Vote validé ({have}/{needed}) : on passe **{clip(np.title, 80)}** !" if ok else "⚠️ Vote validé mais Apple Music n'a pas répondu."
            await interaction.response.send_message(text)
        else:
            await interaction.response.send_message(
                f"🗳️ {interaction.user.display_name} vote pour passer **{clip(np.title, 80)}** ({have}/{needed}, encore {needed - have})."
            )
