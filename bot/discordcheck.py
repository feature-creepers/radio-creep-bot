"""Diagnostic Discord : vérifie le token, le serveur, les salons, le rôle et les permissions.

    python -m bot.discordcheck

N'affiche jamais le token. Ne poste rien, ne modifie rien.
"""
from __future__ import annotations

import asyncio
import os
import sys

import discord
from dotenv import load_dotenv

from .config import ROOT


def _id(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() else None


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    load_dotenv(ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        print("[!!] DISCORD_TOKEN vide dans .env")
        return 1

    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.voice_states = True
    client = discord.Client(intents=intents)
    problems: list[str] = []

    def ok(m: str) -> None:
        print(f"  [OK]  {m}")

    def ko(m: str) -> None:
        print(f"  [!!]  {m}")
        problems.append(m)

    @client.event
    async def on_ready() -> None:
        try:
            me = client.user
            print(f"Connecté en tant que {me} (application {client.application_id})\n")
            print("Serveurs où se trouve le bot :")
            for g in client.guilds:
                print(f"  - {g.name}  (id {g.id})")
            if not client.guilds:
                ko("le bot n'est sur AUCUN serveur : refais l'invitation (README étape 2)")

            gid = _id("GUILD_ID")
            print(f"\nGUILD_ID du .env : {gid}")
            guild = client.get_guild(gid) if gid else None
            if guild is None:
                ko("GUILD_ID ne correspond à aucun serveur ci-dessus (mauvais ID, ou bot pas invité)")
                return
            ok(f"serveur trouvé : {guild.name}")
            bot_member = guild.me

            def check_channel(var: str, kinds: tuple, label: str, perms: list[str], optional: bool = False):
                cid = _id(var)
                if cid is None:
                    if optional:
                        print(f"  [--]  {var} vide (optionnel)")
                    else:
                        ko(f"{var} vide ou invalide")
                    return
                ch = guild.get_channel(cid)
                if ch is None:
                    ko(f"{var}={cid} : aucun salon de ce serveur avec cet ID (ou invisible pour le bot)")
                    return
                if not isinstance(ch, kinds):
                    ko(f"{var} : « {ch.name} » n'est pas un {label}")
                    return
                p = ch.permissions_for(bot_member)
                missing = [x for x in perms if not getattr(p, x, False)]
                if missing:
                    ko(f"{var} : « {ch.name} » OK mais permissions manquantes pour le bot : {', '.join(missing)}")
                else:
                    ok(f"{var} : « {ch.name} » ({label}), permissions OK")

            text_perms = ["view_channel", "send_messages", "embed_links", "attach_files", "read_message_history", "pin_messages"]
            check_channel("TEXT_CHANNEL_ID", (discord.TextChannel,), "salon texte", text_perms)
            check_channel("VOICE_CHANNEL_ID", (discord.VoiceChannel, discord.StageChannel), "salon vocal", ["view_channel"])
            check_channel("DJ_CHANNEL_ID", (discord.TextChannel,), "salon texte", ["view_channel", "send_messages", "embed_links"], optional=True)

            # Diffusion directe par le bot (/diffusion) : facultatif
            print("  Salons vocaux où le bot peut diffuser (/diffusion) :")
            for vch in guild.voice_channels:
                p = vch.permissions_for(bot_member)
                can = p.view_channel and p.connect and p.speak
                who = ", ".join(m.display_name for m in vch.members) or "vide"
                print(f"    {'[OK]' if can else '[--]'} {vch.name} ({vch.id}) · {vch.bitrate // 1000} kbps · {who}")

            rid = _id("DJ_ROLE_ID")
            role = guild.get_role(rid) if rid else None
            if role is None:
                ko(f"DJ_ROLE_ID={rid} : aucun rôle avec cet ID sur le serveur")
            else:
                ok(f"DJ_ROLE_ID : rôle « {role.name} » ({len(role.members)} membre(s))")

            for var, label in (("OWNER_ID", "toi"), ("RADIO_ACCOUNT_ID", "compte qui diffuse")):
                uid = _id(var)
                m = guild.get_member(uid) if uid else None
                if m is None:
                    ko(f"{var}={uid} : aucun membre du serveur avec cet ID")
                elif var == "RADIO_ACCOUNT_ID" and m.bot:
                    ko(f"{var} : « {m.display_name} » est le BOT, il faut le compte utilisateur qui diffuse")
                else:
                    extra = ""
                    if var == "RADIO_ACCOUNT_ID":
                        extra = f", en vocal dans « {m.voice.channel.name} »" if m.voice and m.voice.channel else ", pas en vocal actuellement"
                    ok(f"{var} : {m.display_name} ({label}){extra}")

            if len(guild.members) <= 1:
                ko("le bot ne voit pas les membres : active « Server Members Intent » (Developer Portal ▸ Bot)")
        finally:
            await client.close()

    try:
        await client.start(token)
    except discord.LoginFailure:
        print("[!!] Token refusé par Discord : recopie-le (Developer Portal ▸ Bot ▸ Reset Token)")
        return 1
    except discord.PrivilegedIntentsRequired:
        print("[!!] Active « Server Members Intent » dans Developer Portal ▸ Bot, puis enregistre")
        return 1
    print("\nTout est bon." if not problems else f"\n{len(problems)} problème(s) à corriger.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
