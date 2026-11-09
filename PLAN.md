# Radio Creep Bot : plan et faisabilité

Constats faits sur le PC le 2026-11-04, en lecture seule, sans toucher à la lecture en cours.

## 1. État de la machine

| Élément | Constat |
|---|---|
| Apple Music | Appli Store `AppleInc.AppleMusicWin` 1.1792, interface WinUI 3, langue anglaise |
| iTunes | 12.13.9 installé, COM `iTunes.Application` enregistré |
| Bibliothèque active | `OneDrive\Music\Apple Music\Apple Music Library.musiclibrary\Library.musicdb` (format propriétaire, chiffré) |
| Ancien export XML | `OneDrive\Music\iTunes\iTunes Music Library.xml`, daté du **14/09/2026** : 6 696 morceaux (6 660 fichiers locaux, 36 distants), 41 playlists, presque toutes intelligentes |
| Fichiers audio | En local sur `E:\Music\iTunes\iTunes Media\…` (439 des 500 premiers chemins existent encore) |
| Runtimes | Node 24, Python 3.12 |

## 2. Point 1 : « now playing » et contrôles via SMTC : FIABLE (testé)

`GlobalSystemMediaTransportControlsSessionManager` voit une session `AppleMusic.exe` avec titre, pochette, statut, position/durée, et les boutons play/pause/next/previous actifs.

Deux pièges relevés :
- Apple Music met « **Artiste — Album** » dans le champ `artist`, et laisse `album` vide. Il faut découper sur ` — `.
- La session « courante » peut être Chrome (YouTube dans le Chrome de RADIO CREEP, par exemple). Le bot doit donc cibler la session dont l'AUMID contient `AppleMusic`, pas la session courante.

## 3. Point 2 : lancer un morceau ou une playlist précis

| Piste | Verdict | Pourquoi |
|---|---|---|
| **iTunes COM** | ❌ À écarter | Quand l'appli Apple Music est installée, iTunes ne gère plus que les podcasts et les livres audio. Sa bibliothèque musicale est figée au 14/09. Le lancer créerait un second lecteur en concurrence sur l'audio. |
| **COM interne `AMPLibraryAgent`** | ❌ | Interfaces non documentées, cassées à chaque mise à jour |
| **URI `music://`, `itms://`** | ❌ pour la bibliothèque | Ces liens ouvrent des pages du catalogue en ligne, pas un morceau local. Aucune lecture automatique garantie. |
| **Lire `Library.musicdb`** | ❌ | Format propriétaire chiffré |
| **Export XML (Fichier ▸ Bibliothèque ▸ *Export Library*)** | ✅ pour **chercher** | Le menu existe (`MainMenuFlyoutItem_ExportLibrary`). Le XML donne titres, artistes, albums, chemins des fichiers et playlists. Limite : c'est un instantané, à ré-exporter quand la bibliothèque change. |
| **`AppleMusic.exe /play "<fichier>"`** | 🟡 Probable, **à tester** | C'est l'association de fichiers officielle déclarée dans le manifeste (`Parameters="/play %1"`), et l'alias `AppleMusic.exe` existe. Elle ne dépend pas de l'interface. Trois inconnues : est-ce qu'elle coupe le morceau en cours, ce qui se passe à la fin du morceau (arrêt ou reprise), et est-ce qu'elle crée un doublon dans la bibliothèque. Elle ne marche pas pour les 36 morceaux distants. |
| **UI Automation (« Play Next » / « Add to Queue »)** | 🟡 Possible, **à tester** | L'arbre UIA est complet et bien identifié : `TextBox` Search, `Sidebar_Header_Playlists`, `TransportControl_*`, et les menus contextuels « Play Next » et « Add to Queue » existent. C'est le comportement idéal d'un jukebox : le morceau s'insère dans la file d'Apple Music et la playlist reprend ensuite. Limites : c'est fragile aux mises à jour de l'appli, lié à la langue anglaise, et il faut que la fenêtre ne soit pas réduite (elle peut prendre le focus une seconde). |

### Verdict honnête

- **Chercher dans ta bibliothèque** : fiable, à partir de l'export XML.
- **Lire / pause / suivant / now playing** : fiable, via SMTC.
- **Mettre un morceau précis en file ou basculer de playlist** : **faisable mais pas garanti**. Aucune API officielle n'existe : soit l'interface (UIA), soit l'ouverture du fichier. Je ne m'engage pas avant les deux tests du §6.

D'où la conception en **deux modes**, choisis dans `.env` (et basculables à chaud par un DJ) :
- `REQUEST_MODE=manual` (repli garanti) : `/request` alimente la file. Le bot te notifie en MP et dans un salon DJ, avec des boutons ✅ Lancer / ❌ Refuser. C'est toi qui lances le morceau dans Apple Music. Le bot détecte via SMTC que le morceau demandé passe et le retire de la file.
- `REQUEST_MODE=auto` : quand une demande est validée (automatiquement ou par un DJ), le bot l'insère via UIA « Play Next », ou via `/play fichier` en fin de morceau selon le résultat des tests. En cas d'échec, il rebascule tout seul en notification manuelle.

`/playlist <nom>` : UIA (barre latérale ▸ Playlists ▸ nom ▸ Play), avec repli en notification au DJ si l'élément n'est pas trouvé.

## 4. Point 3 : audio

Le bot **n'entre jamais dans le vocal** et ne capte aucun son. Il lit seulement la liste des membres du salon `-radio` pour compter les auditeurs du vote de skip. Le routage Apple Music → AUX → B2 → RADIO CREEP n'est pas touché.

## 5. Choix de stack : **Python 3.12 + discord.py 2.x** (plutôt que Node/TS)

- Les API Windows dont on a besoin (SMTC en WinRT, UI Automation) ont des liaisons Python maintenues : `winrt-Windows.Media.Control` (déjà testé ci-dessus) et `uiautomation`. En Node, NodeRT est abandonné et demande une compilation native. Il faudrait donc un processus annexe en C# ou PowerShell, avec un protocole entre les deux : deux langages au lieu d'un.
- discord.py 2.x couvre tout le besoin : slash commands, boutons, embeds, état vocal.
- Un seul processus asyncio, lancé par le Planificateur de tâches Windows à l'ouverture de session. Il faut une session ouverte, car UIA a besoin du bureau, ce qui est de toute façon le cas pour Chrome et Apple Music.

## 6. Résultats des tests en direct (2026-11-04, radio en pause, file remise en l'état après chaque test)

| Test | Résultat |
|---|---|
| Recherche « Your Library » via UIA | ✅ Ça marche aussi fenêtre réduite. Les lignes de résultat ne donnent que le titre ; le bot cherche donc « titre artiste » pour cibler le bon morceau. |
| « Add to Queue » | ⚠️ Position variable : une fois avant la suite de l'album, une autre fois après tout l'AutoPlay. **Écarté.** |
| « Play Next » | ✅ Toujours en tête de file. Le bot ne pousse qu'une demande à la fois, ce qui garantit l'ordre. |
| Playlist : page ▸ bouton Play (`PlayButtonElement`) | ✅ Page ouverte, bouton trouvé. Non cliqué, pour ne pas écraser l'album en cours. Les playlists ont des identifiants persistants (PID), et 50 sont visibles, dossiers compris. |
| T1 `/play fichier` | ⏭️ Non testé : il aurait remplacé le contexte en cours, et UIA suffit. |
| Fenêtre sur un autre bureau virtuel | ❌ Interface invisible pour UIA. Le bot affiche un message clair ; solution : épingler la fenêtre sur tous les bureaux. |
| Fenêtre réduite | ⚠️ Les menus ne s'ouvrent pas. Le bot réaffiche la fenêtre sans l'activer, tout en dessous des autres, le temps de l'opération. |

### Focus clavier (relevé du premier plan toutes les 30 ms)

- Écrire dans la recherche donnait le premier plan à Apple Music : ce que tu tapais partait dans l'appli. **Corrigé** en posant `WS_EX_NOACTIVATE` sur sa fenêtre pendant l'opération.
- Le panneau « File », les menus flottants et `ShowContextMenu` laissent une activation **en attente**, qui se déclenche au réaffichage suivant. Purge immédiate, puis focus rendu.
- Résultat :
  - **fenêtre ouverte derrière les autres** : 0 vol de focus (ajout, lecture de la file, playlist) ;
  - **fenêtre réduite** : environ 0,7 s en fin d'ajout, puis focus rendu automatiquement ; en plus, le bot attend 5 s d'inactivité clavier/souris avant d'agir seul ;
  - retirer un morceau de la file d'Apple Music prendrait environ 3 s de focus : **non utilisé**, `/retirer` fait sauter le morceau via SMTC.
- La souris n'est jamais utilisée : aucun appel souris ou clavier dans le code.

## 7. Architecture

```
radio-creep-bot/
  .env.example
  requirements.txt
  bot/
    __main__.py        # point d'entrée : python -m bot
    config.py          # chargement du .env
    smtc.py            # session Apple Music : now playing, pochette, play/pause/next
    library.py         # index du XML exporté, recherche approximative, rechargement si le fichier change
    applemusic_ui.py   # UIA : Play Next, playlists (mode auto)
    jukebox.py         # file d'attente persistante (JSON), modes manual/auto, détection « demande jouée »
    cogs/
      radio.py         # /now /search /request /queue /skip
      dj.py            # /pause /play /next /playlist /mode /reload-library
      announcer.py     # message épinglé + annonce à chaque changement de titre
  scripts/
    install-task.ps1   # tâche planifiée au démarrage de la session
  README.md
```

Règles fonctionnelles :
- **Vote de skip** : on compte les humains présents dans `-radio`, hors bots et hors RADIO CREEP (`RADIO_ACCOUNT_ID`). Seuil = ⌊n/2⌋+1. Les votes repartent à zéro à chaque nouveau titre. S'il n'y a qu'un auditeur, il passe tout seul.
- **Annonce** : sondage SMTC toutes les 2 s. Quand le titre change, le bot poste un embed (titre, artiste, album, pochette, « demandé par @x » s'il s'agit d'une demande).
- **Message épinglé** : créé une fois, puis modifié aux redémarrages suivants (son ID est gardé dans `state.json`).
- **Garde-fous** : une demande en attente par membre à la fois (configurable), pas de doublon dans la file, taille de file plafonnée.
