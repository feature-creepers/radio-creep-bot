"""Pilotage de l'interface de l'appli Apple Music (Windows) via UI Automation.

L'appli n'a aucune API : on manipule son interface WinUI 3 comme le ferait un
lecteur d'écran, sans souris ni clavier (pas de vol de focus). Tout tourne dans
UN thread dédié (COM/UIA n'aime pas changer de thread).

Validé le 2026-11-04 sur Apple Music 1.1792 (interface en anglais) :
  - recherche « Your Library » + bouton « More » d'un morceau ▸ « Play Next »
    → inséré juste après le morceau en cours (« Add to Queue » : position variable) ;
  - fenêtre réduite : la recherche marche mais pas les menus → _call_shown la réaffiche
    sans l'activer le temps de l'opération ;
  - fenêtre sur un autre bureau virtuel : interface invisible (UIError explicite) ;
  - playlists de la barre latérale exposées avec un identifiant persistant (PID).

Si Apple Music change d'interface ou de langue, ajuster UI_TEXT ci-dessous.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from .textutil import artist_matches, norm, same_title

log = logging.getLogger(__name__)

UI_TEXT = {
    "window": "Apple Music",
    "songs_section": "Songs",
    "more": "More",
    "play_next": "Play Next",
    "add_to_queue": "Add to Queue",
    "remove_from_queue": "Remove from Playing Next",
    "play_prefix": "Play “",
    "shuffle_prefix": "Shuffle “",
    "create_station": "Create Station",
    "explicit": "Explicit",
    "favorite": "Favorite",  # bouton ☆ de la barre de lecture (devient autre chose une fois favori)
}
AID = {
    "search_box": "TextBox",
    "search_button": "QueryButton",
    "scope_library": "SearchLibrary",
    "scope_catalog": "SearchAppleMusic",
    "playlists_header": "Sidebar_Header_Playlists",
    "queue_toggle": "PlayQueueToggleButton",
    "queue_manual": "PlayQueueListViewManual",
    "queue_next": "PlayQueueListView",
    "queue_autoplay": "PlayQueueListViewAutoplay",
    "page_play": "PlayButtonElement",  # bouton Play d'une page playlist/album
    "page_shuffle": "ShuffleButtonElement",
}


SCOPE_CHILDREN = 2  # UIA TreeScope_Children
SCOPE_DESCENDANTS = 4  # UIA TreeScope_Descendants


def _process_name(pid: int) -> str:
    """Nom de l'exécutable d'un processus (sans dépendance externe)."""
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1]
    finally:
        k32.CloseHandle(handle)


def _apple_music_windows() -> list[int]:
    """HWND des fenêtres principales d'AppleMusic.exe (même sur un autre bureau virtuel)."""
    u32 = ctypes.windll.user32
    found: list[int] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "WinUIDesktopWin32WindowClass":
            return True
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _process_name(pid.value).lower() == "applemusic.exe":
            found.append(hwnd)
        return True

    u32.EnumWindows(proto(cb), 0)
    return found


def _is_cloaked(hwnd: int) -> bool:
    """Vrai si DWM masque la fenêtre (cas typique : autre bureau virtuel)."""
    val = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))  # DWMWA_CLOAKED
    return val.value != 0


WS_EX_NOACTIVATE = 0x08000000
_GWL_EXSTYLE = -20


def _get_exstyle(hwnd: int) -> int | None:
    u32 = ctypes.windll.user32
    u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    val = u32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    return val if val else None


def _set_exstyle(hwnd: int, style: int) -> None:
    u32 = ctypes.windll.user32
    u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    u32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, style)


def _restore_foreground(prev: int, apple_hwnd: int) -> None:
    """Si Apple Music a pris le premier plan, le rend à la fenêtre précédente."""
    u32 = ctypes.windll.user32

    def apple_has_focus() -> bool:
        cur = u32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(cur, ctypes.byref(pid))
        return cur == apple_hwnd or _process_name(pid.value).lower() == "applemusic.exe"

    # surveille ~1,5 s : l'activation différée peut arriver un peu après
    for attempt in range(10):
        if not apple_has_focus():
            if attempt >= 4:  # stable depuis un moment : terminé
                return
            time.sleep(0.15)
            continue
        if not u32.IsWindow(prev):
            return
        cur = u32.GetForegroundWindow()
        cur_thread = u32.GetWindowThreadProcessId(cur, None)
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        u32.AttachThreadInput(me, cur_thread, True)
        try:
            u32.BringWindowToTop(prev)
            u32.SetForegroundWindow(prev)
        finally:
            u32.AttachThreadInput(me, cur_thread, False)
        if apple_has_focus():
            u32.SwitchToThisWindow(prev, True)  # repli
        time.sleep(0.15)
    if apple_has_focus():
        log.warning("impossible de rendre le premier plan à la fenêtre précédente")


class UIError(RuntimeError):
    """L'interface d'Apple Music n'a pas réagi comme prévu."""


@dataclass(frozen=True)
class QueueEntry:
    title: str
    artist: str
    album: str
    duration: str


@dataclass(frozen=True)
class PlaylistInfo:
    name: str
    aid: str
    path: str  # « Dossier / Nom » pour l'affichage
    is_folder: bool


class AppleMusicUI:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia", initializer=self._init_thread)
        self.auto: Any = None
        self._elem3: Any = None
        self._core: Any = None
        self._dirty = False  # une opération a laissé une activation en attente (voir _call_shown)

    # ------------------------------------------------------------ plomberie
    def _init_thread(self) -> None:
        import uiautomation as auto  # import ici : comtypes initialise COM dans CE thread

        auto.InitializeUIAutomationInCurrentThread()
        auto.SetGlobalSearchTimeout(2)
        from comtypes.gen.UIAutomationClient import IUIAutomationElement3

        import uiautomation.uiautomation as core

        self.auto = auto
        self._core = core
        self._elem3 = IUIAutomationElement3

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Exécute une opération d'interface dans le thread UIA."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._call_shown, fn, *args)

    def _call_shown(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Encadre chaque opération pour ne jamais gêner l'utilisateur du PC.

        Mesures du 2026-11-04 (premier plan relevé toutes les 30 ms) :
        - écrire dans la recherche donne le premier plan à Apple Music → WS_EX_NOACTIVATE
          posé temporairement sur sa fenêtre : le focus reste sur ta fenêtre ;
        - recherche + menu « Play Next » + page playlist ▸ Play : aucun vol de focus ;
        - ouvrir le panneau « File » ou un menu contextuel (ShowContextMenu) laisse une
          activation EN ATTENTE, qui se déclenche au réaffichage suivant de la fenêtre.
          Ces opérations (rares : /retirer, file Apple Music dans /queue) marquent
          self._dirty ; on purge alors l'activation tout de suite et on rend le focus (~1 s) ;
        - fenêtre réduite : la recherche marche mais pas les menus → réaffichée sans être
          activée, tout en dessous des autres fenêtres, puis réduite à nouveau."""
        self._dirty = False
        u32 = ctypes.windll.user32
        hwnds = [h for h in _apple_music_windows() if not _is_cloaked(h)]
        hwnd = hwnds[0] if hwnds else 0
        prev_fg = u32.GetForegroundWindow()
        old_style = _get_exstyle(hwnd) if hwnd else None
        if old_style is not None:
            # le style d'origine n'a jamais WS_EX_NOACTIVATE : s'il est déjà là, c'est un reste
            # d'une opération interrompue (bot arrêté au milieu), qu'on ne doit pas reconduire
            old_style &= ~WS_EX_NOACTIVATE
            _set_exstyle(hwnd, old_style | WS_EX_NOACTIVATE)
        was_minimized = bool(hwnd and u32.IsIconic(hwnd))
        if was_minimized:
            u32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            # HWND_BOTTOM ; SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE
            u32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0010 | 0x0002 | 0x0001)
            time.sleep(1.0)  # laisse WinUI se redessiner
        try:
            return fn(*args)
        finally:
            if was_minimized:
                u32.ShowWindow(hwnd, 7)  # SW_SHOWMINNOACTIVE
            time.sleep(0.4)
            if old_style is not None:
                _set_exstyle(hwnd, old_style)
            if hwnd and self._dirty and was_minimized:
                # purge l'activation en attente maintenant, plutôt qu'à la prochaine opération
                u32.ShowWindow(hwnd, 4)
                u32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0010 | 0x0002 | 0x0001)
                time.sleep(0.3)
                u32.ShowWindow(hwnd, 7)
                time.sleep(0.2)
            if hwnd and prev_fg and prev_fg != hwnd:
                _restore_foreground(prev_fg, hwnd)

    def _ia(self):
        return self._core._AutomationClient.instance().IUIAutomation

    def _condition(self, aid=None, name=None, ctype=None, cls=None):
        a, ia = self.auto, self._ia()
        conds = []
        if aid is not None:
            conds.append(ia.CreatePropertyCondition(a.PropertyId.AutomationIdProperty, aid))
        if name is not None:
            conds.append(ia.CreatePropertyCondition(a.PropertyId.NameProperty, name))
        if ctype is not None:
            conds.append(ia.CreatePropertyCondition(a.PropertyId.ControlTypeProperty, ctype))
        if cls is not None:
            conds.append(ia.CreatePropertyCondition(a.PropertyId.ClassNameProperty, cls))
        if not conds:
            return ia.CreateTrueCondition()
        cond = conds[0]
        for c in conds[1:]:
            cond = ia.CreateAndCondition(cond, c)
        return cond

    # La recherche native (FindFirst/FindAll) est rapide mais ne voit pas tout l'arbre WinUI
    # (ex. les boutons internes du champ de recherche) : on se rabat alors sur un parcours
    # élément par élément, plus lent mais complet.
    @staticmethod
    def _matches(c, aid=None, name=None, ctype=None, cls=None) -> bool:
        return (
            (aid is None or c.AutomationId == aid)
            and (name is None or c.Name == name)
            and (ctype is None or c.ControlType == ctype)
            and (cls is None or c.ClassName == cls)
        )

    def _walk(self, root, first: bool, children_only: bool, max_depth: int = 30, **kw) -> list:
        out: list = []
        stack = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            kids = node.GetChildren()
            for ch in kids:
                if self._matches(ch, **kw):
                    out.append(ch)
                    if first:
                        return out
            if not children_only and depth < max_depth:
                stack.extend((ch, depth + 1) for ch in reversed(kids))
        return out

    def _find(self, root, scope=None, **kw):
        """Premier descendant correspondant, ou None."""
        scope = scope or SCOPE_DESCENDANTS
        el = root.Element.FindFirst(scope, self._condition(**kw))
        if el:
            return self.auto.Control.CreateControlFromElement(el)
        res = self._walk(root, first=True, children_only=scope == SCOPE_CHILDREN, **kw)
        return res[0] if res else None

    def _find_all(self, root, scope=None, **kw):
        scope = scope or SCOPE_DESCENDANTS
        arr = root.Element.FindAll(scope, self._condition(**kw))
        if arr and arr.Length:
            return [self.auto.Control.CreateControlFromElement(arr.GetElement(i)) for i in range(arr.Length)]
        return self._walk(root, first=False, children_only=scope == SCOPE_CHILDREN, **kw)

    def _wait(self, fn: Callable[[], Any], timeout: float, interval: float = 0.25) -> Any:
        end = time.monotonic() + timeout
        while True:
            res = fn()
            if res:
                return res
            if time.monotonic() > end:
                return res
            time.sleep(interval)

    def _window(self):
        for _ in range(3):
            hwnds = _apple_music_windows()
            visible = [h for h in hwnds if not _is_cloaked(h)]
            if visible:
                return self.auto.ControlFromHandle(visible[0])
            if hwnds:
                raise UIError(
                    "La fenêtre Apple Music est sur un autre bureau virtuel : Windows masque alors son interface. "
                    "Épingle-la sur tous les bureaux (Win+Tab ▸ clic droit sur Apple Music ▸ "
                    "« Afficher cette fenêtre sur tous les bureaux »)."
                )
            time.sleep(0.5)
        raise UIError("Fenêtre Apple Music introuvable (l'appli est-elle ouverte ?)")

    def _menu_items(self, w) -> list:
        items = self._find_all(w, ctype=self.auto.ControlType.MenuItemControl)
        return [m for m in items if m.Name and m.Name != "System"]

    def _wait_menu(self, w, timeout: float = 3.0) -> list:
        return self._wait(lambda: self._menu_items(w), timeout) or []

    def _close_menus(self, w) -> None:
        """Ferme un menu contextuel resté ouvert, en envoyant Échap à SA fenêtre popup
        (PostMessage : ne vole pas le focus, contrairement à SendKeys)."""
        a = self.auto
        for menu in self._find_all(w, ctype=a.ControlType.MenuControl):
            c, hwnd = menu, 0
            while c is not None and not hwnd:
                hwnd = c.NativeWindowHandle
                c = c.GetParentControl()
            if hwnd:
                ctypes.windll.user32.PostMessageW(hwnd, 0x100, 0x1B, 0)  # WM_KEYDOWN VK_ESCAPE
                ctypes.windll.user32.PostMessageW(hwnd, 0x101, 0x1B, 0xC0000001)  # WM_KEYUP
        time.sleep(0.3)

    def _context_menu(self, w, control) -> list:
        """Ouvre le menu contextuel d'un élément (sans clic droit). Essaie l'élément puis ses enfants."""
        for target in [control, *control.GetChildren()[:2]]:
            try:
                self._dirty = True  # laisse une activation en attente
                target.Element.QueryInterface(self._elem3).ShowContextMenu()
            except Exception:
                continue
            items = self._wait_menu(w, 2.0)
            if items:
                return items
        return []

    def _invoke_menu(self, items: list, predicate: Callable[[str], bool]) -> bool:
        for m in items:
            if predicate(m.Name):
                m.GetInvokePattern().Invoke()
                time.sleep(0.6)
                return True
        return False

    @staticmethod
    def _texts(control) -> list[str]:
        out: list[str] = []

        def walk(c, depth):
            if depth > 3:
                return
            for ch in c.GetChildren():
                if ch.ControlTypeName == "TextControl" and ch.Name:
                    # ignore les glyphes d'icônes (zone Unicode privée, ex. « Explicit »)
                    if not all(0xE000 <= ord(x) <= 0xF8FF or ord(x) >= 0xF0000 for x in ch.Name):
                        out.append(ch.Name)
                walk(ch, depth + 1)

        walk(control, 0)
        return out

    # ------------------------------------------------------------ file d'attente
    def _open_queue_panel(self, w) -> None:
        toggle = self._find(w, aid=AID["queue_toggle"])
        if toggle is None:
            raise UIError("Bouton « Queue » introuvable")
        pat = toggle.GetTogglePattern()
        if pat.ToggleState != 1:
            self._dirty = True  # ouvrir le panneau laisse une activation en attente
            pat.Toggle()
            self._wait(lambda: self._find(w, aid=AID["queue_next"]) or self._find(w, aid=AID["queue_manual"]), 3.0)

    def _queue_items(self, w, aid: str) -> list[tuple[Any, QueueEntry]]:
        lst = self._find(w, aid=aid)
        if lst is None:
            return []
        res = []
        for it in self._find_all(
            lst, scope=SCOPE_CHILDREN, ctype=self.auto.ControlType.ListItemControl
        ):
            t = self._texts(it)
            title = t[0] if t else it.Name
            sub = t[1] if len(t) > 1 else ""
            artist, _, album = sub.partition(" — ")
            res.append((it, QueueEntry(title, artist.strip(), album.strip(), t[2] if len(t) > 2 else "")))
        return res

    def read_queue(self) -> list[QueueEntry]:
        w = self._window()
        self._open_queue_panel(w)
        return [e for _, e in self._all_queue_items(w)]

    def _remove_from_queue(self, w, item) -> bool:
        items = self._context_menu(w, item)
        ok = self._invoke_menu(items, lambda n: n == UI_TEXT["remove_from_queue"])
        if not ok:
            self._close_menus(w)
        return ok

    # ------------------------------------------------------------ recherche + ajout
    def _search_library(self, w, query: str) -> list:
        """Lance une recherche « Your Library » et renvoie les lignes de la section Songs."""
        return self._search(w, query, AID["scope_library"])

    def _search(self, w, query: str, scope_aid: str, accept: Callable[[list], bool] | None = None) -> list:
        """Recherche dans l'appli (filtre « Your Library » ou « Apple Music »).
        Renvoie les lignes de la section Songs. `accept(lignes)` : attendre que les résultats
        conviennent (évite de lire les résultats de la recherche précédente)."""
        a = self.auto
        box = self._find(w, aid=AID["search_box"], ctype=a.ControlType.EditControl)
        if box is None:
            raise UIError("Champ de recherche introuvable")
        vp = box.GetValuePattern()
        for _ in range(3):  # relit la valeur : si quelqu'un tape/colle dans l'appli au même moment
            vp.SetValue(query)
            time.sleep(0.4)
            if vp.Value == query:
                break
        else:
            raise UIError("Le champ de recherche d'Apple Music ne prend pas le texte")
        btn = self._find(w, aid=AID["search_button"])
        if btn is None:
            raise UIError("Bouton de recherche introuvable")
        btn.GetInvokePattern().Invoke()
        scope = self._wait(lambda: self._find(w, aid=scope_aid), 4.0)
        if scope is None:
            raise UIError(f"Filtre de recherche {scope_aid} introuvable")
        tp = scope.GetTogglePattern()
        if tp.ToggleState != 1:
            tp.Toggle()
        # laisse la page se rafraîchir (sinon on lit les anciens résultats) ; avec `accept`, c'est
        # lui qui vérifie que les résultats sont les bons : inutile d'attendre davantage
        time.sleep(1.2 if accept is None else 0.3)

        def rows():
            found = self._song_rows(w)
            return found if accept is None or accept(found) else []

        return self._wait(rows, 5.0 if accept is None else 7.0) or []

    def _song_rows(self, w) -> list:
        """Lignes de la section Songs de la page de résultats."""
        a = self.auto
        # cls=ListViewItem : exclut l'entrée « Library ▸ Songs » de la barre latérale (NavigationViewItem)
        section = self._find(w, name=UI_TEXT["songs_section"], ctype=a.ControlType.ListItemControl, cls="ListViewItem")
        if section is None:
            return []
        return self._find_all(section, ctype=a.ControlType.ListItemControl)

    def search_catalog(self, query: str) -> list[tuple[str, str, bool]]:
        """Recherche « Apple Music » de l'appli : (titre, artiste, explicite), le plus pertinent
        d'abord. C'est la recherche de référence : elle trouve des morceaux que l'API iTunes
        ignore. L'appli montre une dizaine de morceaux par recherche. Ne vole pas le focus."""
        a = self.auto
        w = self._window()

        def signature(rows: list) -> list[list[str]]:
            return [self._texts(r)[:2] for r in rows[:5]]

        # Attend que la page change, pour ne pas lire les résultats de la recherche précédente
        # (sauf si c'est la même recherche : ils ne changeront pas).
        box = self._find(w, aid=AID["search_box"], ctype=a.ControlType.EditControl)
        same = box is not None and norm(box.GetValuePattern().Value) == norm(query)
        before = [] if same else signature(self._song_rows(w))
        accept = (lambda rs: signature(rs) != before) if before else None
        rows = self._search(w, query, AID["scope_catalog"], accept=accept)
        if before and not rows:
            rows = self._song_rows(w)  # résultats identiques ou aucun changement : on lit la page telle quelle
            if rows:
                log.info("recherche « %s » : la page n'a pas changé après 8 s", query)
        out: list[tuple[str, str, bool]] = []
        for r in rows:
            texts = self._texts(r)
            if texts:
                artist = texts[1] if len(texts) > 1 else ""
                out.append((texts[0], artist, (r.Name or "").endswith(f" {UI_TEXT['explicit']}")))
        return out

    def search_library(self, query: str) -> list[str]:
        """Titres trouvés par la recherche de l'appli (diagnostic)."""
        w = self._window()
        return [(self._texts(r) or [r.Name])[0] for r in self._search_library(w, query)]

    def search_library_full(self, query: str) -> tuple[list[str], list[str]]:
        """Recherche « Your Library » d'Apple Music (couvre aussi le streaming).

        Renvoie (titres de la section Songs, noms de la section Artists). Les lignes Songs
        ne donnent pas l'artiste. Ne vole pas le focus (mesuré)."""
        a = self.auto
        w = self._window()
        songs = [(self._texts(r) or [r.Name])[0] for r in self._search_library(w, query)]
        artists: list[str] = []
        section = self._find(w, name="Artists", ctype=a.ControlType.ListItemControl, cls="ListViewItem")
        if section is not None:
            for r in self._find_all(section, ctype=a.ControlType.ListItemControl):
                t = self._texts(r)
                if t:
                    artists.append(t[0])
        return songs, artists

    def push_next(self, title: str, artist: str, album: str = "", hint: str = "", catalog: bool = False) -> None:
        """« Play Next » puis contrôle dans le panneau File, avec un second essai : le menu peut
        répondre sans que le morceau arrive dans la file (mesuré le 2026-11-04 : demande #16,
        trois ajouts « réussis », aucun dans la file). Lève UIError si le morceau n'y est pas."""
        w = self._window()
        for attempt in (1, 2):
            if catalog:
                self.enqueue_catalog(title, artist, True)
            else:
                self.enqueue(title, artist, album, True, False, hint)
            if self._landed_next(w, title, artist):
                return
            log.warning("« %s » absent de la file après Play Next (essai %d)", title, attempt)
        raise UIError(f"Apple Music n'a pas pris « {title} » dans sa file")

    def _landed_next(self, w, title: str, artist: str, timeout: float = 6.0) -> bool:
        """Le morceau est-il le prochain de la file ? Vrai aussi si le panneau est illisible
        (un panneau changé par une mise à jour ne doit pas bloquer les demandes ni les doubler)."""
        toggle = self._find(w, aid=AID["queue_toggle"])
        if toggle is None:
            log.warning("panneau File introuvable : ajout non vérifié")
            return True
        was_open = toggle.GetTogglePattern().ToggleState == 1
        self._open_queue_panel(w)
        try:
            if self._find(w, aid=AID["queue_next"]) is None and self._find(w, aid=AID["queue_manual"]) is None:
                log.warning("listes de la file introuvables : ajout non vérifié")
                return True

            def first_is_it() -> bool:
                q = self._all_queue_items(w)
                return bool(q) and same_title(q[0][1].title, title) and artist_matches(artist, q[0][1].artist)

            t0 = time.monotonic()
            ok = bool(self._wait(first_is_it, timeout))
            log.info("contrôle de la file pour « %s » : %s en %.1f s", title, "OK" if ok else "absent", time.monotonic() - t0)
            return ok
        finally:
            if not was_open:
                toggle.GetTogglePattern().Toggle()

    def _all_queue_items(self, w) -> list[tuple[Any, QueueEntry]]:
        """Toute la file, dans l'ordre d'affichage. Selon son état, Apple Music la répartit
        sur 1 à 3 listes (Playing Next / suite / AutoPlay) : on les concatène."""
        out: list[tuple[Any, QueueEntry]] = []
        for aid in (AID["queue_manual"], AID["queue_next"], AID["queue_autoplay"]):
            out += self._queue_items(w, aid)
        return out

    def enqueue(
        self,
        title: str,
        artist: str = "",
        album: str = "",
        play_next: bool = True,
        verify: bool = False,
        hint: str = "",
    ) -> QueueEntry:
        """Met un morceau de la bibliothèque dans la file d'Apple Music.

        play_next=True (« Play Next ») : juste après le morceau en cours. C'est le mode
        utilisé par le bot, qui ne pousse qu'une demande à la fois (ordre garanti).
        play_next=False (« Add to Queue ») : en fin de file ; la position varie selon
        l'état de la file d'Apple Music (parfois après tout l'AutoPlay).

        Bon artiste : la recherche « titre artiste » d'Apple Music ne renvoie que les
        morceaux contenant les deux. Si seul le titre donne des résultats et qu'il y en a
        plusieurs, on refuse (ambigu). verify=True vérifie en plus dans le panneau File,
        mais ouvrir ce panneau vole brièvement le focus (voir _call_shown).
        hint : texte de recherche qui a déjà trouvé ce morceau dans l'appli (morceaux en
        streaming dont on ne connaît pas l'artiste) ; essayé avant le titre seul.
        Lève UIError en cas d'échec."""
        w = self._window()
        menu_label = UI_TEXT["play_next"] if play_next else UI_TEXT["add_to_queue"]
        try:
            candidates = []
            queries = [f"{title} {artist}".strip()] if artist else []
            if hint and hint not in queries:
                queries.append(hint)
            queries.append(title)
            for qi, query in enumerate(queries):
                rows = self._search_library(w, query)
                candidates = [r for r in rows if same_title((self._texts(r) or [""])[0], title)]
                # titre seul après échec des recherches précises : plusieurs résultats = ambigu
                if candidates and qi > 0 and query == title and len(candidates) > 1 and not verify:
                    raise UIError(f"Plusieurs « {title} » dans la bibliothèque, artiste introuvable : ambigu")
                if candidates:
                    break
            if not candidates:
                raise UIError(f"« {title} » introuvable dans la bibliothèque Apple Music")

            for row in candidates[:4]:
                more = self._find(row, name=UI_TEXT["more"], ctype=self.auto.ControlType.ButtonControl)
                if more is not None:
                    self._dirty = True  # le menu flottant laisse aussi une activation en attente
                    more.GetInvokePattern().Invoke()
                    items = self._wait_menu(w)
                else:
                    items = self._context_menu(w, row)
                if not self._invoke_menu(items, lambda n: n == menu_label):
                    self._close_menus(w)
                    continue
                if not verify:
                    return QueueEntry(title, artist, album, "")
                # Vérification dans le panneau « File »
                def find_it():
                    q = self._all_queue_items(w)
                    if play_next:
                        q = q[:3]  # doit être tout en haut
                    return [x for x in q if same_title(x[1].title, title)]

                try:
                    self._open_queue_panel(w)
                    queued = self._wait(find_it, 3.0)
                except UIError:
                    queued = []
                if not queued:
                    log.warning("ajout de %r non vérifiable dans la file, supposé OK", title)
                    return QueueEntry(title, artist, album, "")
                item, entry = queued[0] if play_next else queued[-1]
                if artist_matches(artist, entry.artist):
                    return entry
                log.info("mauvais artiste (%s au lieu de %s), on retire et on essaie le suivant", entry.artist, artist)
                self._remove_from_queue(w, item)
            raise UIError(f"Impossible d'ajouter « {title} » ({artist}) à la file")
        finally:
            self._close_menus(w)

    def _catalog_action(self, w, title: str, artist: str, menu_label: str) -> bool:
        """Recherche « Apple Music » dans l'appli ▸ ligne titre + artiste ▸ ⋯ ▸ `menu_label`.
        Les lignes du catalogue affichent l'artiste : pas d'ambiguïté. Lève UIError si introuvable."""

        def matching(rows: list) -> list:
            out = []
            for r in rows:
                t = self._texts(r)
                if t and same_title(t[0], title) and (len(t) < 2 or artist_matches(artist, t[1])):
                    # la version exacte d'abord : same_title ignore « (Remix) », « (Live) »…
                    rank = (norm(t[0]) != norm(title), len(t) > 1 and norm(t[1]) != norm(artist))
                    out.append((rank, r))
            out.sort(key=lambda x: x[0])
            return [r for _, r in out]

        candidates: list = []
        for query in (f"{title} {artist}".strip(), title):
            rows = self._search(w, query, AID["scope_catalog"], accept=lambda rs: bool(matching(rs)))
            candidates = matching(rows)
            if candidates:
                break
        if not candidates:
            raise UIError(f"« {title} » ({artist}) introuvable dans le catalogue Apple Music")
        for row in candidates[:3]:
            more = self._find(row, name=UI_TEXT["more"], ctype=self.auto.ControlType.ButtonControl)
            if more is not None:
                self._dirty = True
                more.GetInvokePattern().Invoke()
                items = self._wait_menu(w)
            else:
                items = self._context_menu(w, row)
            if self._invoke_menu(items, lambda n: n == menu_label):
                return True
            self._close_menus(w)
        return False

    def enqueue_catalog(self, title: str, artist: str, play_next: bool = True) -> QueueEntry:
        """Met un morceau du catalogue Apple Music (pas forcément dans la bibliothèque) dans
        la file : recherche « Apple Music » ▸ ligne titre + artiste ▸ ⋯ ▸ Play Next. Lève UIError."""
        w = self._window()
        menu_label = UI_TEXT["play_next"] if play_next else UI_TEXT["add_to_queue"]
        try:
            if self._catalog_action(w, title, artist, menu_label):
                return QueueEntry(title, artist, "", "")
            raise UIError(f"Impossible d'ajouter « {title} » ({artist}) à la file")
        finally:
            self._close_menus(w)

    def create_station(self, title: str, artist: str) -> str:
        """Lance une station Apple Music « dans le style » d'un morceau (Create Station).
        D'abord via le menu ⋯ de la barre de lecture si elle affiche ce morceau (le dernier
        joué reste affiché en fin de file), sinon via le catalogue. La station démarre
        aussitôt. Renvoie "lcd" ou "catalog". Lève UIError."""
        w = self._window()
        label = UI_TEXT["create_station"]
        try:
            lcd = self._find(w, aid="LCD")
            if lcd is not None and title and norm(title) in norm(lcd.Name or ""):
                act = self._find(lcd, aid="ActionButton")
                if act is not None:
                    self._dirty = True
                    act.GetInvokePattern().Invoke()
                    if self._invoke_menu(self._wait_menu(w), lambda n: n == label):
                        log.info("station créée depuis la barre de lecture : %s — %s", title, artist)
                        return "lcd"
                    self._close_menus(w)
            if self._catalog_action(w, title, artist, label):
                log.info("station créée depuis le catalogue : %s — %s", title, artist)
                return "catalog"
            raise UIError(f"« Create Station » indisponible pour « {title} »")
        finally:
            self._close_menus(w)

    def favorite_current(self, expected_title: str) -> str:
        """Clique « Favorite » dans la barre de lecture, pour le morceau en cours.
        Renvoie "added" ou "already". Vérifie d'abord que la barre affiche bien ce titre.
        Lève UIError."""
        a = self.auto
        w = self._window()
        lcd = self._find(w, aid="LCD")
        if lcd is None:
            raise UIError("barre de lecture introuvable")
        shown = norm(lcd.Name or "")
        if expected_title and norm(expected_title) not in shown:
            raise UIError(f"l'appli affiche un autre morceau ({lcd.Name})")

        def fav_button():
            for b in self._find_all(lcd, ctype=a.ControlType.ButtonControl):
                n = (b.Name or "").lower()
                if "favorite" in n or "favourite" in n:
                    return b
            return None

        btn = fav_button()
        if btn is None:
            raise UIError("bouton Favorite introuvable")
        before = btn.Name
        if before != UI_TEXT["favorite"]:
            return "already"
        btn.GetInvokePattern().Invoke()
        time.sleep(0.8)
        after = (fav_button() or btn).Name
        if after == before:
            raise UIError("Apple Music n'a pas confirmé")
        log.info("favori Apple Music : %s (%s → %s)", expected_title, before, after)
        return "added"

    def volume(self, value: float | None = None) -> float:
        """Lit (et règle si `value`, de 0 à 1) le volume de l'appli Apple Music : bouton
        « Volume » de la barre de lecture → curseur du popup, refermé ensuite."""
        a = self.auto
        w = self._window()
        btn = self._find(w, aid="VolumeButton")
        if btn is None:
            raise UIError("bouton Volume introuvable")
        btn.GetInvokePattern().Invoke()
        slider = self._wait(lambda: self._find(w, aid="VolumeSlider"), 3.0)
        if slider is None:
            raise UIError("le réglage du volume ne s'est pas ouvert")
        try:
            rv = slider.GetRangeValuePattern()
            if value is not None:
                rv.SetValue(max(rv.Minimum, min(rv.Maximum, value)))
                time.sleep(0.3)
            return float(rv.Value)
        finally:
            popup = slider.GetParentControl()
            while popup is not None and popup.ControlType != a.ControlType.WindowControl:
                popup = popup.GetParentControl()
            try:
                if popup is not None and popup.Name != w.Name:
                    popup.GetWindowPattern().Close()
            except Exception:
                log.debug("fermeture du popup Volume impossible", exc_info=True)

    def clear_noactivate(self) -> None:
        """Retire un WS_EX_NOACTIVATE resté collé sur la fenêtre d'Apple Music."""
        for h in _apple_music_windows():
            style = _get_exstyle(h)
            if style is not None and style & WS_EX_NOACTIVATE:
                _set_exstyle(h, style & ~WS_EX_NOACTIVATE)
                log.info("fenêtre Apple Music de nouveau activable (réglage resté collé retiré)")

    def remove_from_queue(self, title: str, artist: str = "") -> bool:
        """Retire la première occurrence d'un morceau de la file d'Apple Music."""
        w = self._window()
        try:
            self._open_queue_panel(w)
            for item, entry in self._all_queue_items(w):
                if same_title(entry.title, title) and artist_matches(artist, entry.artist):
                    return self._remove_from_queue(w, item)
            return False
        finally:
            self._close_menus(w)

    # ------------------------------------------------------------ playlists
    def _playlist_items(self, w) -> list[tuple[Any, PlaylistInfo]]:
        a = self.auto
        header = self._find(w, aid=AID["playlists_header"])
        if header is None:
            raise UIError("Section « Playlists » introuvable dans la barre latérale")
        out: list[tuple[Any, PlaylistInfo]] = []

        def expand(ctrl) -> bool:
            pat = ctrl.GetPattern(a.PatternId.ExpandCollapsePattern)
            if pat is None or pat.ExpandCollapseState == 3:  # LeafNode
                return False
            if pat.ExpandCollapseState != 1:
                pat.Expand()
                time.sleep(0.5)
            return True

        def walk(ctrl, prefix: str, depth: int) -> None:
            if depth > 4:
                return
            for it in self._find_all(ctrl, scope=SCOPE_CHILDREN, ctype=a.ControlType.ListItemControl):
                aid = it.AutomationId or ""
                if "ePlaylist" not in aid:
                    walk(it, prefix, depth + 1)  # conteneurs intermédiaires
                    continue
                is_folder = expand(it)
                path = f"{prefix}{it.Name}"
                out.append((it, PlaylistInfo(it.Name, aid, path, is_folder)))
                if is_folder:
                    walk(it, path + " / ", depth + 1)

        expand(header)
        walk(header, "", 0)
        # Les enfants peuvent être rattachés au conteneur de la liste plutôt qu'au dossier : dédoublonnage par ID
        seen, uniq = set(), []
        for it, p in out:
            if p.aid not in seen:
                seen.add(p.aid)
                uniq.append((it, p))
        return uniq

    def list_playlists(self) -> list[PlaylistInfo]:
        w = self._window()
        return [p for _, p in self._playlist_items(w)]

    def play_playlist(self, aid: str, shuffle: bool = False) -> PlaylistInfo:
        w = self._window()
        try:
            for it, info in self._playlist_items(w):
                if info.aid != aid:
                    continue
                # 1) Ouvre la page de la playlist et clique son bouton Play/Shuffle :
                #    pas de menu contextuel, donc pas de vol de focus (mesuré).
                sel = it.GetPattern(self.auto.PatternId.SelectionItemPattern)
                if sel is not None:
                    sel.Select()
                    btn_aid = AID["page_shuffle"] if shuffle else AID["page_play"]
                    btn = self._wait(lambda: self._find(w, aid=btn_aid), 4.0)
                    if btn is not None and it.GetSelectionItemPattern().IsSelected:
                        time.sleep(0.5)  # la page finit de charger
                        btn.GetInvokePattern().Invoke()
                        return info
                # 2) Repli : menu contextuel de la barre latérale
                items = self._context_menu(w, it)
                prefix = UI_TEXT["shuffle_prefix"] if shuffle else UI_TEXT["play_prefix"]
                if not self._invoke_menu(items, lambda n: n.startswith(prefix)):
                    raise UIError(f"Entrée « {prefix}… » absente du menu de la playlist")
                return info
            raise UIError("Playlist introuvable dans la barre latérale")
        finally:
            self._close_menus(w)
