# 📻 Radio Creep Bot

Bot Discord **officiel** (application du Developer Portal, pas un selfbot) qui transforme la radio 24/7 du serveur **Feature Creepers** en jukebox, piloté depuis le PC de la radio.

- Le son ne change pas : Apple Music → Voicemeeter AUX → B2 → compte **RADIO CREEP** dans Chrome. Le bot **n'entre jamais en vocal** et ne capte aucun son : il **pilote** Apple Music et **informe** le serveur.
- Python 3.12 + discord.py 2. Choisi parce que les deux API Windows nécessaires (SMTC et UI Automation) ont des bibliothèques Python maintenues ; en Node il faudrait un second programme en C#.

## Commandes

| Commande | Qui | Effet |
|---|---|---|
| `/now` | tous | Morceau en cours : titre, artiste, album, pochette, progression |
| `/search texte` | tous | Cherche dans les fichiers de la radio 📀 **et dans tout le catalogue Apple Music** 🍎 (~3 s), avec un menu pour demander directement. Accepte aussi un lien Apple Music |
| `/request morceau` | tous | Propose un morceau → file d'attente. L'autocomplétion suggère des morceaux 📀 🍎 ; si le tien n'y est pas, choisis « 🔎 Chercher dans tout Apple Music » ou valide ton texte tel quel. Accepte aussi un lien Apple Music |
| `/queue` | tous | File des demandes (et, en option, la suite dans Apple Music) |
| `/skip` | auditeurs | Vote : majorité des humains présents dans `-radio` (hors RADIO CREEP et membres en sourdine) |
| `/favorite` · bouton ⭐ sous chaque annonce | tous | Ajoute le morceau à ta liste de favoris. Pour toi (`OWNER_ID`), le marque aussi **Favorite** dans Apple Music, seulement pour le morceau en cours |
| `/favorites [membre]` | tous | Tes favoris (ou ceux d'un membre), avec un menu pour en redemander un et, pour ta liste, en retirer un |
| `/pause` `/play` `/next` | DJ | Contrôle de la lecture |
| `/playlist nom [aleatoire]` | DJ | Bascule sur une de tes playlists (avec autocomplétion, dossiers compris) |
| `/mode auto\|approve\|manual` | DJ | Mode de traitement des demandes (voir plus bas) |
| `/retirer id` | DJ | Retire une demande |
| `/reload` | DJ | Recharge la bibliothèque et la liste des playlists |
| `/diffusion démarrer [salon]` · `arrêter` · `état` | DJ | Le bot diffuse lui-même la radio dans un salon vocal, sans la fenêtre Chrome de RADIO CREEP (voir « Diffusion par le bot ») |
| `/volume [0-100]` | DJ | Règle le volume de l'appli Apple Music (son curseur intégré, sans ouvrir la fenêtre). Sans valeur : affiche le volume actuel |
| `/station maintenant` · `auto : activer/désactiver` · `état` | DJ | Lance une station Apple Music dans le style du morceau en cours. En automatique (par défaut), le bot le fait seul quand la file Apple Music se termine : la radio ne s'arrête jamais |

Le bot épingle aussi un message d'aide dans le salon texte, poste le titre à chaque changement et affiche « Écoute … » dans son statut.

---

## Installation pas à pas

### 1. Créer l'application Discord

1. Va sur <https://discord.com/developers/applications> ▸ **New Application** ▸ nom : `Radio Creep` (ou autre).
2. Onglet **Bot** :
   - **Reset Token** ▸ copie le token. Il va dans le `.env` ; **ne le partage jamais**.
   - **Privileged Gateway Intents** : active **Server Members Intent**. Il permet de savoir qui est dans le vocal dès le démarrage. Les deux autres intents ne servent pas.
   - Tu peux décocher **Public Bot** pour que toi seul puisses l'inviter.
3. Onglet **General Information** : copie l'**Application ID**.

### 2. Inviter le bot sur le serveur

Ouvre cette URL en remplaçant `APPLICATION_ID` :

```
https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&scope=bot+applications.commands&permissions=2251801964440576
```

Elle accorde : Voir les salons, Envoyer des messages, Gérer les messages, Intégrer des liens, Joindre des fichiers, Voir l'historique, Utiliser les commandes de l'application, **Épingler des messages**, et en vocal **Se connecter** et **Parler** (pour `/diffusion`). Depuis 2026, épingler est une permission distincte de « Gérer les messages ».

Vérifie ensuite les permissions **par salon**. Le bot doit avoir :
- **salon texte de la radio** : voir, écrire, intégrer des liens, joindre des fichiers, voir l'historique, épingler ;
- **salon vocal `-radio`** : *voir le salon* (pour compter les auditeurs), plus *se connecter* et *parler* si tu utilises `/diffusion` ;
- **salon DJ privé** (optionnel) : voir et écrire.

Crée aussi le rôle **DJ** si ce n'est pas fait. Les administrateurs et `OWNER_ID` sont toujours considérés comme DJ.

### 3. Installer

Dans PowerShell, dans le dossier du projet :

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. Configurer

```powershell
Copy-Item .env.example .env
notepad .env
```

Pour copier un identifiant : Discord ▸ Paramètres ▸ Avancés ▸ **Mode développeur**, puis clic droit sur le serveur, un salon, un rôle ou un membre ▸ **Copier l'identifiant**.

| Variable | Valeur |
|---|---|
| `DISCORD_TOKEN` | token de l'étape 1 |
| `GUILD_ID` | serveur Feature Creepers |
| `VOICE_CHANNEL_ID` | salon vocal `-radio` |
| `TEXT_CHANNEL_ID` | salon texte (message épinglé et annonces) |
| `DJ_ROLE_ID` | rôle DJ |
| `OWNER_ID` | ton compte perso : reçoit les demandes en MP |
| `RADIO_ACCOUNT_ID` | le compte RADIO CREEP (exclu du vote de skip) |
| `DJ_CHANNEL_ID` | optionnel : salon privé où poster aussi les demandes |
| `REQUEST_MODE` | `approve` conseillé pour commencer |
| `LIBRARY_DIRS` | `E:\Music\iTunes\iTunes Media\Music` (déjà rempli) |
| `STREAM_CHANNEL_ID` | facultatif : salon où le bot diffuse dès le démarrage. `/diffusion` fait la même chose et prime sur ce réglage |
| `CATALOG` / `CATALOG_COUNTRY` | `true` / `fr` : recherche dans tout le catalogue Apple Music du pays de ton compte. Facultatif (valeurs par défaut) |

### 5. Préparer Apple Music (important)

Le bot pilote l'appli comme le ferait un lecteur d'écran, **sans souris ni clavier**. Trois règles :

1. **Épingle la fenêtre sur tous les bureaux.** Si elle est sur un autre bureau virtuel, Windows masque son interface et le bot ne la voit plus. Pour l'épingler : **Win+Tab** ▸ clic droit sur Apple Music ▸ **Afficher cette fenêtre sur tous les bureaux**.
2. **Laisse-la ouverte, derrière tes autres fenêtres** (ou sur un autre écran), plutôt que réduite. C'est ce qui a été mesuré :
   - **ouverte derrière** : aucun vol de focus, ni à l'ajout, ni au changement de playlist ;
   - **réduite** : ça marche aussi, mais elle réapparaît derrière tes fenêtres quelques secondes et prend le focus clavier **environ 0,7 s** à la fin de l'opération, puis le bot te le rend. En plus, le bot attend que clavier et souris soient inactifs depuis 5 s avant d'agir tout seul (`UI_IDLE_SECONDS`).
3. Garde l'interface d'Apple Music **en anglais**. Le bot cherche les menus « Play Next », « Songs », etc. (constantes `UI_TEXT` dans `bot/applemusic_ui.py`).

Le bot ne ferme et ne lance jamais Apple Music.

### 6. Tester

```powershell
.\.venv\Scripts\python.exe -m bot.selfcheck                       # lecture seule
.\.venv\Scripts\python.exe -m bot.selfcheck --search "booba"
.\.venv\Scripts\python.exe -m bot.selfcheck --enqueue "A.C. Milan" "Booba"   # ⚠️ met VRAIMENT le morceau en Play Next
.\.venv\Scripts\python.exe -m bot                                 # lance le bot (Ctrl+C pour arrêter)
```

Au premier lancement, le scan de la bibliothèque prend environ une minute (6 000 fichiers). Ensuite il est en cache et ne relit que les fichiers modifiés.

### 7. Lancer au démarrage de Windows

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
Start-ScheduledTask -TaskName RadioCreepBot     # pour le lancer tout de suite
```

Une tâche planifiée (`RadioCreepBot`) démarre le bot sans fenêtre **45 s après l'ouverture de ta session**, et le relance au bout d'une minute s'il plante. Elle tourne dans ta session, car l'accès au bureau est nécessaire. Le PC doit donc avoir une session ouverte, ce qui est déjà le cas pour Chrome et Apple Music.

> ⚠️ Ne lance pas `python -m bot` à la main pendant que la tâche tourne : deux copies annonceraient chaque titre en double. Utilise `.\scripts\bot.ps1 console`, qui arrête la tâche d'abord.

---

## Commandes utiles (PC de la radio)

Toutes se lancent dans PowerShell, **depuis le dossier du projet**. Le script `scripts\bot.ps1` regroupe les plus courantes :

| Commande | Effet |
|---|---|
| `.\scripts\bot.ps1 status` | État de la tâche et du processus, nombre de copies, 10 dernières lignes du log |
| `.\scripts\bot.ps1 start` | Démarre le bot |
| `.\scripts\bot.ps1 stop` | Arrête le bot (tâche et processus) |
| `.\scripts\bot.ps1 restart` | Redémarre, **à faire après toute modification du `.env` ou du code** |
| `.\scripts\bot.ps1 logs` | 40 dernières lignes du log |
| `.\scripts\bot.ps1 follow` | Suit le log en direct (Ctrl+C pour quitter, le bot continue de tourner) |
| `.\scripts\bot.ps1 check` | Diagnostic Discord : token, serveur, salons, rôle, membres, permissions. N'envoie rien |
| `.\scripts\bot.ps1 selfcheck` | Diagnostic local : Apple Music, pochette, bibliothèque, playlists. Ne modifie rien |
| `.\scripts\bot.ps1 console` | Arrête la tâche et lance le bot dans la console, pour voir les erreurs en direct (Ctrl+C pour quitter, puis `start`) |
| `.\scripts\bot.ps1 install` | Installe le lancement automatique |
| `.\scripts\bot.ps1 uninstall` | Supprime le lancement automatique |

Si PowerShell refuse d'exécuter le script (« l'exécution de scripts est désactivée »), préfixe la commande, par exemple : `powershell -ExecutionPolicy Bypass -File scripts\bot.ps1 status`. Pour l'autoriser une fois pour toutes pour ton compte : `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### Équivalents sans le script

```powershell
Get-ScheduledTask RadioCreepBot | Select-Object State     # Running = il tourne
Start-ScheduledTask RadioCreepBot
Stop-ScheduledTask RadioCreepBot
Get-Content logs\bot.log -Tail 40 -Encoding UTF8
Get-Content logs\bot.log -Wait -Encoding UTF8             # suivi en direct
.\.venv\Scripts\python.exe -m bot.discordcheck
.\.venv\Scripts\python.exe -m bot.selfcheck
.\.venv\Scripts\python.exe -m bot.selfcheck --search "booba"            # tester la recherche
.\.venv\Scripts\python.exe -m bot.selfcheck --queue                      # lire la file Apple Music
.\.venv\Scripts\python.exe -m bot.selfcheck --catalog "paranoid"         # tester la recherche dans le catalogue
.\.venv\Scripts\python.exe -m bot.selfcheck --enqueue "Titre" "Artiste"  # ⚠️ met VRAIMENT le morceau en Play Next
.\.venv\Scripts\python.exe -m bot.selfcheck --enqueue-catalog "Titre" "Artiste"  # ⚠️ idem, depuis le catalogue
```

> Dans le Gestionnaire des tâches, le bot apparaît comme **deux** `pythonw.exe` : le lanceur du venv et le vrai bot. C'est normal. `bot.ps1 status` ne compte que le vrai.

### Cas courants

| Je veux… | Faire |
|---|---|
| Changer un réglage (mode, salon, limites…) | Modifier `.env`, puis `.\scripts\bot.ps1 restart` |
| Changer le mode des demandes sans redémarrer | `/mode` sur Discord (le choix est conservé aux redémarrages) |
| Prendre en compte de nouveaux morceaux ou playlists | `/reload` sur Discord |
| Mettre à jour les dépendances | `.\scripts\bot.ps1 stop`, puis `.\.venv\Scripts\python.exe -m pip install -U -r requirements.txt`, puis `start` |
| Repartir d'une file vide | `stop`, supprimer `data\state.json`, puis `start` (le message d'aide sera recréé) |
| Changer de token (fuite, reset) | Developer Portal ▸ Bot ▸ Reset Token, coller dans `.env`, puis `restart` |

Fichiers utiles : `logs\bot.log` (tourne sur 5 fichiers de 2 Mo), `data\state.json` (file des demandes, favoris, mode, salon de diffusion, station auto, ID du message épinglé), `data\library_cache.json` (cache du scan de la bibliothèque, se reconstruit tout seul).

---

## Comment ça marche

| Besoin | Moyen | Fiabilité |
|---|---|---|
| Titre, artiste, pochette, play/pause/suivant | **SMTC** (Windows Media Transport Controls) | ✅ API officielle Windows, 100 % en arrière-plan |
| Recherche, autocomplétion | **Index local** : tags des fichiers de `LIBRARY_DIRS` (ou un export XML via `LIBRARY_XML`). Chaque mot tapé doit se retrouver dans le titre, l'artiste ou l'album (fautes de frappe tolérées seulement si rien ne correspond exactement) | ✅ instantané |
| Recherche dans tout Apple Music (`/search`, texte libre de `/request`) | **UI Automation** : recherche « Apple Music » de l'appli, complétée par l'API iTunes (pochettes, résultats en plus). L'appli affiche sa page de résultats, sans voler le focus | ✅ ~3 s, une recherche à la fois |
| Autocomplétion de `/request`, liens Apple Music | **API publique iTunes** d'Apple (gratuite, sans clé) : rapide, mais son index est incomplet, d'où le choix « 🔎 Chercher dans tout Apple Music ». Résultats contenant tous les mots d'abord. Cache + 18 appels/min max (limite d'Apple ≈ 20/min) | ✅ ~1 s |
| Insérer un morceau du catalogue | **UI Automation** : recherche « Apple Music » dans l'appli ▸ ligne titre + artiste ▸ bouton ⋯ ▸ **Play Next**, puis contrôle dans le panneau File (second essai s'il n'y est pas). Tout de suite, même si tu utilises le PC ; la confirmation Discord part une fois le morceau vu dans la file. Marche aussi pour un morceau hors de ta bibliothèque (il n'y est pas ajouté) | 🟡 ~10 s |
| Secours si l'API est injoignable | **UI Automation** : recherche « Your Library » de l'appli (streaming compris, mais sans l'artiste) | 🟡 5 à 10 s |
| Insérer une demande | **UI Automation** : recherche « Your Library » ▸ bouton ⋯ du morceau ▸ **Play Next** | 🟡 testé et fiable, mais dépend de l'interface d'Apple Music |
| Lancer une playlist | **UI Automation** : barre latérale ▸ page de la playlist ▸ bouton Play / Shuffle | 🟡 idem |

**Pourquoi pas mieux ?** L'appli Apple Music pour Windows n'a aucune API. iTunes COM ne voit plus la bibliothèque musicale depuis l'installation d'Apple Music. Les liens `music://` ouvrent le catalogue en ligne, pas ta bibliothèque. Enfin, la base `Library.musicdb` est chiffrée.

**Ordre des demandes.** Le bot tient sa propre file et ne pousse **qu'une demande à la fois** en « Play Next », juste après le morceau en cours. Quand elle démarre (détectée via SMTC), il pousse la suivante. Si elle disparaît, par exemple après un `/playlist`, il la repousse (3 essais, puis il prévient les DJ). On évite « Add to Queue », dont la position varie selon l'état de la file d'Apple Music (parfois après tout l'AutoPlay).

### Station automatique (lecture à l'infini)

Quand la file d'Apple Music se termine, le bot lance une **station** (« Create Station ») dans le style du **dernier morceau joué**, qu'il vienne d'une demande, d'une playlist ou de n'importe qui. Il le signale dans le salon texte. Les demandes (`/request`) continuent de passer en priorité pendant la station.

Le bot détecte la fin de file sans ouvrir le panneau File d'Apple Music, qui volerait le focus. Il faut que le morceau arrive à sa fin, que la lecture s'arrête pendant environ 4 s, et qu'il n'y ait plus de morceau suivant (ou que la lecture soit revenue au début). Un `/pause` de DJ ne déclenche rien. La station démarre via le menu ⋯ de la barre de lecture, sinon via le catalogue. Il peut y avoir quelques secondes de silence entre la fin de la file et le début de la station. En cas d'échec, tu reçois un MP.

À désactiver avec `/station auto : désactiver`, ou `AUTO_STATION=false` dans `.env`. Si l'**Autoplay** (∞) d'Apple Music est activé, la file ne se termine jamais : c'est lui qui enchaîne, et la station automatique ne se déclenche pas.

### Alerte « radio muette »

Le son passe par le compte RADIO CREEP connecté dans Chrome. Si ce compte sort de `-radio` ou coupe son micro (onglet fermé, coupure réseau, PC redémarré), le bot t'envoie un **MP au bout de 45 s**, puis un autre quand il revient. Les micro-coupures qui se reconnectent seules ne déclenchent rien. Pas d'alerte quand le bot diffuse lui-même dans `-radio`.

### Diffusion par le bot (sans fenêtre Chrome)

Avec `/diffusion démarrer`, le bot rejoint lui-même le salon vocal et y envoie la sortie **Voicemeeter Out B2**, celle que RADIO CREEP utilise comme micro. Le routage audio ne change pas : le bot devient simplement un auditeur de B2, en capture partagée, à 256 kbps (`STREAM_BITRATE`). Il se reconnecte tout seul après une coupure ou un redémarrage du PC, sans clic. Le salon choisi est mémorisé.

**Essayer sans couper la radio :**
1. `/diffusion démarrer salon:` un salon vocal vide (ex. 🦖-trex), puis rejoins-le et écoute. RADIO CREEP continue de diffuser dans `-radio` pendant ce temps.
2. `/diffusion état` affiche le ping et les éventuelles coupures. Pour couper : `/diffusion arrêter`.

**Basculer pour de bon :**
1. Dans Chrome, déconnecte RADIO CREEP de `-radio`. Sinon le son est doublé : le bot te prévient si c'est le cas.
2. `/diffusion démarrer`, sans salon : `-radio` par défaut.
3. Tu peux fermer la fenêtre Chrome. Pour revenir en arrière : `/diffusion arrêter`, puis reconnecte RADIO CREEP.

Le salon `-radio` est à 256 kbps ; les autres salons du serveur à 96 kbps, ce qui suffit pour un essai.

### Modes de demande (`REQUEST_MODE`, ou `/mode` à chaud)

Le mode choisi avec `/mode` est mémorisé (`data/state.json`) et **prime sur `REQUEST_MODE`** du `.env`, même après un redémarrage. Mode actuel : `auto`.

- `auto` : la demande entre directement dans la file du bot. En cas d'échec, les DJ sont prévenus.
- `approve` : tu (ou un DJ) reçois la demande avec les boutons **Ajouter** / **C'est fait** / **Refuser**. « Ajouter » laisse le bot l'insérer.
- `manual` : le bot ne touche **jamais** à l'interface d'Apple Music. Tu ajoutes le morceau toi-même (clic droit ▸ Play Next), puis tu cliques **C'est fait**. Le bot détecte quand il passe.

Les notifications partent en MP à `OWNER_ID` et, si configuré, dans `DJ_CHANNEL_ID`. Les boutons restent actifs après un redémarrage du bot.

## Limites connues

- **Catalogue Apple Music** : l'appli montre une dizaine de morceaux par recherche, complétés par l'API. Pour un mot très courant, ajoute l'artiste ou colle le lien du morceau. L'API publique d'Apple oublie certains titres et se limite à environ 20 requêtes par minute. Si la limite est atteinte, l'autocomplétion ne montre plus que les fichiers 📀 pendant une minute, et `/search` continue avec l'appli seule (sans pochette).
- **Plusieurs versions d'un même titre** (remaster, live, best-of) : le bot met la première version de l'appli qui a exactement le même titre et le même artiste. Pour une version précise, choisis-la dans la liste : son titre complet (« Paranoid (2012 - Remaster) ») suffit en général à la distinguer.
- **Catalogue du pays de ton compte** (`CATALOG_COUNTRY`) : un morceau indisponible dans ce pays peut apparaître dans la recherche mais pas dans l'appli. L'ajout échoue alors et le DJ est prévenu.
- **Fichiers sur le disque mais retirés de la bibliothèque** : visibles dans `/search`, mais l'ajout échouera. Le DJ est alors prévenu.
- **Mise à jour d'Apple Music** : si l'interface change, l'insertion peut casser. Lecture, now playing, pause/suivant et annonces continuent de marcher, et les demandes basculent en notification DJ. Lance `python -m bot.selfcheck` pour diagnostiquer.
- `/retirer` sur une demande déjà dans Apple Music : elle n'est pas retirée de la file de l'appli (cela volerait le focus) ; le bot la **saute** quand elle démarre.
- `SHOW_APPLE_QUEUE=true` affiche la suite d'Apple Music dans `/queue`, mais ouvre le panneau « File ». Si la fenêtre est réduite, cela lui donne le focus environ 1 s. Désactivé par défaut.

## Dépannage

| Symptôme | Piste |
|---|---|
| « Fenêtre Apple Music … sur un autre bureau virtuel » | Épingle-la sur tous les bureaux (étape 5) |
| `/now` : « Rien ne joue » | Apple Music doit être ouvert et avoir joué au moins un morceau depuis son lancement |
| Problème d'identifiant ou de permission | `python -m bot.discordcheck` vérifie token, serveur, salons, rôle, membres et permissions (n'envoie rien) |
| `403 Missing Access` au démarrage / commandes absentes | Ré-invite le bot avec l'URL de l'étape 2 (scope `applications.commands`), sans le retirer du serveur |
| `/skip` dit que tu n'es pas dans le salon | Server Members Intent activé ? `VOICE_CHANNEL_ID` correct ? Pas en sourdine ? |
| Pas de message épinglé | Permission **Épingler des messages** dans le salon texte |
| Le bot est hors ligne | `.\scripts\bot.ps1 status`, puis `logs`. Si besoin, `console` pour voir l'erreur en direct |
| Titres annoncés en double | Deux copies tournent : `.\scripts\bot.ps1 restart` |
| `/diffusion` : le bot s'allume en vert mais personne n'entend | `/diffusion état` doit afficher « chiffrement OK ». Sinon, vérifie la version de davey : `.\.venv\Scripts\python.exe -m pip show davey` doit donner 0.1.x (la 0.2.0 casse le chiffrement vocal avec discord.py 2.7.1). Réinstalle avec `pip install -r requirements.txt` |
| `/diffusion état` : « son capté : silence ! » | Apple Music est en pause, ou la sortie B2 de Voicemeeter ne reçoit rien (`STREAM_DEVICE`) |
| L'icône « sourdine » sur le bot en vocal | Normal : son casque est coupé (il n'écoute pas le salon), son micro marche |
