#!/usr/bin/env python3
"""Dedale — graphe interactif de dépendances de scènes (PySide6).

Application de bureau native. À partir du nom d'une scène et d'une source de
données (MySQL / CSV / dump SQL), affiche un graphe interactif des scènes dont
elle dépend, coloré selon leur fraîcheur (vert = à jour, rouge = périmé).

Lancement :  python main.py

Packaging (Windows) :
    python -m PyInstaller --onefile --windowed .\\main.py ^
        --icon Dedale.ico --add-data "Dedale.ico;."
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QHeaderView,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import artist_model as am
import data_source as ds
import graph_model as gm
import mutes_store as ms
from graph_view import (
    COL_INHERITED_BORDER,
    COL_OK_BORDER,
    COL_STALE_BORDER,
    COL_START_BORDER,
    DependencyGraphView,
)

# Le trousseau système est optionnel : la persistance du mot de passe n'est
# proposée que si le module « keyring » est disponible. Sans lui, aucun mot de
# passe n'est jamais écrit sur disque.
try:  # pragma: no cover - dépend de l'environnement
    import keyring
    _HAS_KEYRING = True
except Exception:  # pragma: no cover
    keyring = None
    _HAS_KEYRING = False

APP_ICON_NAME = "Dedale.ico"
# Identifiant Windows : sans lui, la barre des tâches affiche l'icône de
# python.exe au lieu de celle de l'application.
_WINDOWS_APP_ID = "Dedale.DependencyGraph"


def resource_path(name):
    """Chemin d'une ressource, en développement comme une fois packagée.

    PyInstaller en ``--onefile`` extrait les fichiers passés via
    ``--add-data`` dans un dossier temporaire exposé par ``sys._MEIPASS``.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base is None:                      # exécution depuis les sources
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def app_icon():
    """Icône de l'application (QIcon vide si le fichier est absent)."""
    path = resource_path(APP_ICON_NAME)
    return QIcon(path) if os.path.isfile(path) else QIcon()


def _set_windows_app_id():
    """Associe l'appli à son propre identifiant (icône de barre des tâches)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            _WINDOWS_APP_ID)
    except Exception:
        pass    # purement cosmétique : ne doit jamais empêcher le lancement


_KEYRING_SERVICE = "Dedale-DependencyGraph"
_ORG = "Dedale"
_APP = "DependencyGraph"
# Anciens noms (outil renommé) : migrés au premier lancement.
_LEGACY_KEYRING_SERVICE = "Hydra-DependencyGraph"
_LEGACY_ORG = "Hydra"
_DEFAULT_SCENE = "qua_077_02000_comp_v019"


# Bleu des assets publiés sur le plan mais non importés (purement informatif).
_COL_AVAILABLE = QColor("#1a5fb4")

# Clé d'un groupe de premier niveau (scène ou date) : sert à retrouver les
# triangles dépliés après reconstruction de l'arbre.
_ROLE_GROUP = Qt.UserRole + 1


# Sources autorisées à enregistrer des mutes. Le nom de la source vient de
# MainWindow._current_source() : « mysql », « csv », « sql ».
_MUTE_SOURCES_DEFAULT = "all"
_SOURCE_LABELS = {"mysql": "MySQL", "csv": "CSV files", "sql": "SQL dump"}


def _mute_sources():
    """Sources dont les mutes sont persistants (``DEDALE_MUTES_SOURCES``).

    Défaut : **toutes**. La restriction reste disponible pour rejouer un
    essai sur un export sans que grapher depuis MySQL n'enregistre quoi que
    ce soit (``DEDALE_MUTES_SOURCES=sql``, ou une liste « sql,mysql »).
    """
    raw = (os.environ.get("DEDALE_MUTES_SOURCES")
           or _MUTE_SOURCES_DEFAULT).strip().lower()
    if raw in ("all", "*"):
        return set(_SOURCE_LABELS)
    wanted = {part.strip() for part in raw.split(",") if part.strip()}
    return wanted & set(_SOURCE_LABELS)


def _with_format(label, fmt):
    """« smoke » + « bgeo.sc » -> « smoke [bgeo.sc] » (inchangé si inconnu)."""
    return f"{label} [{fmt}]" if fmt else label


def _version_state(current, latest):
    """« (vXXX) » si à jour, « (vXXX → vYYY) » si une version plus récente existe."""
    cur = f"v{current:03d}" if current is not None else "v?"
    if current is not None and latest is not None and current < latest:
        return f"({cur} → v{latest:03d})", True
    return f"({cur})", False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dedale — Scene Dependency Graph")
        icon = app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(1280, 860)

        self._settings = QSettings(_ORG, _APP)
        self._migrate_legacy_settings()
        # Cache des données par signature de source (en mémoire uniquement).
        self._data_cache = {}
        # Résultat du graphe affiché (chemins scène/assets des liens : sert à
        # la persistance des mutes), et source dont il provient.
        self._graph_result = None
        self._graph_source = ""
        # Persistance des liens muted : bac à sable de test par défaut, base
        # du studio sur demande (DEDALE_MUTES_TARGET=db).
        self._mutes = ms.MutesStore()
        # Sécurité : seules ces sources peuvent enregistrer des mutes. Par
        # défaut le dump .sql seul, pour essayer sans toucher au reste.
        self._mute_sources = _mute_sources()
        # Message à faire survivre au rafraîchissement links_changed (raison
        # d'un repli mémoire : le compteur l'écraserait sinon aussitôt).
        self._mute_notice = ""
        # Outil « Graphist to graph » : dernier rapport et assets masqués
        # (en mémoire uniquement, rien n'est écrit en base).
        self._artist_report = None
        self._muted_assets = set()
        # Triangles dépliés, mémorisés par mode d'affichage : muter un asset ou
        # changer une option reconstruit l'arbre sans tout replier.
        self._artist_tree_mode = "scene"
        self._expanded_groups = {"scene": set(), "date": set()}

        self._build_ui()
        self._sync_mute_backend()
        self._restore_settings()
        # État de la source affiché d'emblée : sur l'onglet MySQL, la connexion
        # est testée au lancement plutôt qu'au premier changement d'onglet.
        self._fit_source_tab()
        if self.tabs.currentIndex() == 0:
            self._refresh_mysql_status()
        self.statusBar().showMessage(self._mute_mode_message())

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        # Split vertical : saisies à gauche, graphe à droite.
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([440, 900])
        self.setCentralWidget(splitter)

        self.statusBar().showMessage("Ready.")

    def _build_left_panel(self):
        """Panneau de gauche : source de données + outils."""
        panel = QWidget()
        panel.setMinimumWidth(390)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 10, 6, 10)
        lay.setSpacing(8)

        lay.addWidget(self._bold_label("Data source"))
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_mysql_tab(), "MySQL")
        self.tabs.addTab(self._build_csv_tab(), "CSV")
        self.tabs.addTab(self._build_sql_tab(), "SQL dump")
        self.tabs.currentChanged.connect(self._on_source_tab_changed)
        lay.addWidget(self.tabs)

        lay.addSpacing(6)
        self.tool_tabs = QTabWidget()
        self.tool_tabs.addTab(self._build_scene_tool(), "Scene to graph")
        self.tool_tabs.addTab(self._build_artist_tool(), "Graphist to graph")
        lay.addWidget(self.tool_tabs, 1)
        return panel

    # --- outil 1 : grapher une scène ---------------------------------------
    def _build_scene_tool(self):
        tool = QWidget()
        lay = QVBoxLayout(tool)
        lay.setContentsMargins(8, 10, 8, 8)
        lay.setSpacing(8)

        lay.addWidget(self._bold_label("Scene to graph"))
        self.scene_edit = QLineEdit()
        self.scene_edit.setPlaceholderText(_DEFAULT_SCENE)
        self.scene_edit.returnPressed.connect(self._on_grapher)
        lay.addWidget(self.scene_edit)

        self.btn_graph = QPushButton("Graph")
        self.btn_graph.setDefault(True)
        self.btn_graph.clicked.connect(self._on_grapher)
        lay.addWidget(self.btn_graph)

        lay.addSpacing(10)
        lay.addWidget(self._bold_label("Display"))
        self.chk_show_disconnected = QCheckBox("Show disable branches")
        self.chk_show_disconnected.setChecked(True)
        self.chk_show_disconnected.setToolTip(
            "When a link is disabled, the parents it fed may no longer reach "
            "the queried scene. Uncheck to hide them.")
        self.chk_show_disconnected.toggled.connect(
            lambda on: self.view.set_show_disconnected(on))
        lay.addWidget(self.chk_show_disconnected)

        self.chk_mute_same_task = QCheckBox("Mute same task connections")
        self.chk_mute_same_task.setToolTip(
            "Disable every link between two scenes carrying the same task "
            "(lighting → lighting…). Temporary: nothing is written to the "
            "database.")
        self.chk_mute_same_task.toggled.connect(
            lambda on: self.view.set_mute_same_task(on))
        lay.addWidget(self.chk_mute_same_task)

        # Liste dynamique des formats rencontrés dans le graphe : une case par
        # format, cochée par défaut. La décocher coupe les liens apportant ce
        # type de fichier.
        self.lbl_formats = QLabel("Show formats")
        self.lbl_formats.setStyleSheet("color:#8a93a0;")
        self.lbl_formats.setVisible(False)
        lay.addWidget(self.lbl_formats)
        self.formats_box = QWidget()
        self._formats_layout = QVBoxLayout(self.formats_box)
        self._formats_layout.setContentsMargins(12, 0, 0, 0)
        self._formats_layout.setSpacing(2)
        self._format_boxes = {}
        self.formats_box.setVisible(False)
        lay.addWidget(self.formats_box)

        # Même principe pour les tasks présentes dans le graphe : décocher une
        # task coupe les liens qui en partent.
        self.lbl_tasks = QLabel("Show tasks")
        self.lbl_tasks.setStyleSheet("color:#8a93a0;")
        self.lbl_tasks.setVisible(False)
        lay.addWidget(self.lbl_tasks)
        self.tasks_box = QWidget()
        self._tasks_layout = QVBoxLayout(self.tasks_box)
        self._tasks_layout.setContentsMargins(12, 0, 0, 0)
        self._tasks_layout.setSpacing(2)
        self._task_boxes = {}
        self.tasks_box.setVisible(False)
        lay.addWidget(self.tasks_box)

        self.lbl_hidden = QLabel("")
        self.lbl_hidden.setStyleSheet("color:#8a93a0;")
        self.lbl_hidden.setWordWrap(True)
        lay.addWidget(self.lbl_hidden)

        lay.addStretch(1)
        return tool

    def _rebuild_format_boxes(self, formats):
        """Reconstruit la liste des formats après un nouveau graphe.

        Tout est coché par défaut (rien n'est masqué) ; les formats que l'on
        avait décochés le restent s'ils existent encore, pour ne pas perdre un
        filtre en re-graphant une scène voisine.
        """
        hidden = {fmt for fmt, box in self._format_boxes.items()
                  if not box.isChecked()} & set(formats)
        while self._formats_layout.count():
            item = self._formats_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._format_boxes = {}
        for fmt in formats:
            box = QCheckBox(fmt)
            box.setChecked(fmt not in hidden)
            box.setToolTip(
                f"Uncheck to hide the \"{fmt}\" files: the links carrying "
                "them are cut, along with the connections to the scenes "
                "exporting that format.")
            box.toggled.connect(lambda _on: self._on_formats_toggled())
            self._formats_layout.addWidget(box)
            self._format_boxes[fmt] = box
        has_any = bool(formats)
        self.lbl_formats.setVisible(has_any)
        self.formats_box.setVisible(has_any)
        self.view.set_shown_formats(set(formats) - hidden)

    def _on_redraw_layout(self):
        """Resserre le graphe sur les nœuds encore affichés."""
        self.view.redraw_layout()
        self.statusBar().showMessage("Layout redrawn on the visible nodes.")

    def _rebuild_task_boxes(self, tasks):
        """Liste des tasks du graphe, dans l'ordre des lignes (haut -> bas).

        Tout est coché par défaut ; les tasks décochées le restent tant
        qu'elles existent encore dans le graphe affiché.
        """
        hidden = {task for task, box in self._task_boxes.items()
                  if not box.isChecked()} & set(tasks)
        while self._tasks_layout.count():
            item = self._tasks_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._task_boxes = {}
        for task in tasks:
            box = QCheckBox(task)
            box.setChecked(task not in hidden)
            box.setToolTip(
                f"Uncheck to cut every link coming from a \"{task}\" scene: "
                "that task stops feeding the graph, and the outputs it was "
                "the only consumer of disappear too.")
            box.toggled.connect(lambda _on: self._on_tasks_toggled())
            self._tasks_layout.addWidget(box)
            self._task_boxes[task] = box
        has_any = bool(tasks)
        self.lbl_tasks.setVisible(has_any)
        self.tasks_box.setVisible(has_any)
        self.view.set_shown_tasks(set(tasks) - hidden)

    def _on_tasks_toggled(self):
        self.view.set_shown_tasks(
            {task for task, box in self._task_boxes.items()
             if box.isChecked()})

    def _on_formats_toggled(self):
        self.view.set_shown_formats(
            {fmt for fmt, box in self._format_boxes.items() if box.isChecked()})

    def _on_link_filters_cleared(self):
        """« Enable all links » a aussi levé les filtres : on remet à zéro.

        Les formats sont **recochés** (tout est affiché), la case « même
        tâche » est décochée.
        """
        self.chk_mute_same_task.blockSignals(True)
        self.chk_mute_same_task.setChecked(False)
        self.chk_mute_same_task.blockSignals(False)
        for box in list(self._format_boxes.values()) + \
                list(self._task_boxes.values()):
            box.blockSignals(True)
            box.setChecked(True)
            box.blockSignals(False)

    # --- outil 2 : contrôler les scènes d'un graphiste ----------------------
    def _build_artist_tool(self):
        tool = QWidget()
        lay = QVBoxLayout(tool)
        lay.setContentsMargins(8, 10, 8, 8)
        lay.setSpacing(6)

        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)

        self.artist_edit = QLineEdit()
        self.artist_edit.setPlaceholderText("sebastien ginestra")
        self.artist_edit.setToolTip(
            "Artist name (scenes.author). Exact match first, otherwise a "
            "loose match on the surname.")
        self.artist_edit.returnPressed.connect(self._on_check_scenes)
        form.addWidget(QLabel("Graphist"), 0, 0)
        form.addWidget(self.artist_edit, 0, 1)

        self.project_edit = QLineEdit()
        self.project_edit.setPlaceholderText("qua")
        self.project_edit.setMaxLength(16)
        self.project_edit.setToolTip(
            "Project code (3 letters). Used as a prefix.")
        self.project_edit.returnPressed.connect(self._on_check_scenes)
        form.addWidget(QLabel("Project"), 1, 0)
        form.addWidget(self.project_edit, 1, 1)

        # Filtre sur la task DES SCÈNES du graphiste (≠ tasks des assets).
        self.scene_tasks_edit = QLineEdit(am.ALL_TASKS)
        self.scene_tasks_edit.setPlaceholderText("all")
        self.scene_tasks_edit.setToolTip(
            "List only the graphist's scenes of these tasks: \"all\", or one "
            "or more tasks separated by spaces or commas (e.g. \"fx "
            "lighting\").")
        self.scene_tasks_edit.returnPressed.connect(self._on_check_scenes)
        form.addWidget(QLabel("Scenes task"), 2, 0)
        form.addWidget(self.scene_tasks_edit, 2, 1)

        form.setColumnStretch(1, 1)
        lay.addLayout(form)

        self.btn_check = QPushButton("Check scenes")
        self.btn_check.clicked.connect(self._on_check_scenes)
        lay.addWidget(self.btn_check)

        self.chk_shots_only = QCheckBox("Shots only")
        self.chk_shots_only.setChecked(True)
        self.chk_shots_only.setToolTip(
            "Keep only shot scenes: excludes asset-level tasks (modeling, "
            "shading, rigging). Unknown tasks are kept.")
        # Re-lance le contrôle seulement si un résultat existe déjà (sinon le
        # simple fait de cocher réclamerait un nom de graphiste).
        self.chk_shots_only.toggled.connect(
            lambda _on: self._artist_report and self._on_check_scenes())
        lay.addWidget(self.chk_shots_only)

        self.chk_only_outdated = QCheckBox("Only scenes to update")
        self.chk_only_outdated.setToolTip(
            "Hide scenes whose checked imports are all up to date.")
        self.chk_only_outdated.toggled.connect(
            lambda _on: self._populate_artist_tree())
        lay.addWidget(self.chk_only_outdated)

        self.chk_show_uptodate = QCheckBox("Show assets up to date")
        self.chk_show_uptodate.setChecked(True)
        self.chk_show_uptodate.setToolTip(
            "List the imports that are already at their latest published "
            "version (shown in green). Uncheck to keep only what needs "
            "updating.")
        self.chk_show_uptodate.toggled.connect(
            lambda _on: self._populate_artist_tree())
        lay.addWidget(self.chk_show_uptodate)

        available_row = QHBoxLayout()
        self.chk_show_available = QCheckBox("Show assets not imported")
        self.chk_show_available.setToolTip(
            "Also list, in blue, the assets published on the shot that the "
            "scene does not import. They never make a scene outdated.")
        self.chk_show_available.toggled.connect(
            lambda _on: self._populate_artist_tree())
        available_row.addWidget(self.chk_show_available)
        # Filtre propre aux lignes bleues : on veut souvent voir « tout ce qui
        # manque » d'une seule task sans restreindre le reste de la liste.
        available_row.addWidget(QLabel("Tasks"))
        self.avail_tasks_edit = QLineEdit(am.ALL_TASKS)
        self.avail_tasks_edit.setPlaceholderText("all")
        self.avail_tasks_edit.setToolTip(
            "Restrict the assets not imported (blue lines) to these tasks: "
            "\"all\", or one or more tasks separated by spaces or commas "
            "(e.g. \"shading rigging\"). Applies on top of \"Filter assets "
            "tasks\".")
        self.avail_tasks_edit.textChanged.connect(
            lambda _text: self._populate_artist_tree())
        available_row.addWidget(self.avail_tasks_edit, 1)

        available_row.addWidget(QLabel("Formats"))
        self.avail_formats_edit = QLineEdit(am.ALL_TASKS)
        self.avail_formats_edit.setPlaceholderText("all")
        self.avail_formats_edit.setToolTip(
            "Restrict the assets not imported (blue lines) to these export "
            "formats: \"all\", or one or more formats separated by spaces or "
            "commas (e.g. \"exr abc bgeo.sc\"). An asset whose format is "
            "unknown is kept only under \"all\".")
        self.avail_formats_edit.textChanged.connect(
            lambda _text: self._populate_artist_tree())
        available_row.addWidget(self.avail_formats_edit, 1)
        lay.addLayout(available_row)

        # Filtre d'affichage des ASSETS listés : le contrôle porte toujours sur
        # toutes leurs tasks, ce champ ne fait que restreindre l'affichage — il
        # s'applique donc immédiatement, sans relancer le contrôle.
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter assets tasks"))
        self.tasks_edit = QLineEdit(am.ALL_TASKS)
        self.tasks_edit.setPlaceholderText("all")
        self.tasks_edit.setToolTip(
            "Filter the listed assets by task: \"all\", or one or more tasks "
            "separated by spaces or commas (e.g. \"fx anim tracking\").")
        self.tasks_edit.textChanged.connect(
            lambda _text: self._populate_artist_tree())
        filter_row.addWidget(self.tasks_edit, 1)
        lay.addLayout(filter_row)

        self.artist_summary = QLabel("")
        self.artist_summary.setWordWrap(True)
        self.artist_summary.setStyleSheet("color:#8a93a0;")
        lay.addWidget(self.artist_summary)

        self.artist_tree = QTreeWidget()
        self.artist_tree.setHeaderLabels(["Scene / asset", "Version", ""])
        # Triangles de dépliage : ils réclament de l'indentation, les lignes
        # d'asset sont donc décalées sous leur scène.
        self.artist_tree.setRootIsDecorated(True)
        self.artist_tree.setIndentation(14)
        self.artist_tree.setAlternatingRowColors(True)
        header = self.artist_tree.header()
        # Sans cela, la dernière colonne (mute) s'étire et mange la place.
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.artist_tree.setColumnWidth(2, 28)
        self.artist_tree.itemDoubleClicked.connect(self._on_artist_item_activated)
        lay.addWidget(self.artist_tree, 1)

        muted_row = QHBoxLayout()
        self.btn_whats_up = QPushButton("What's up ?")
        self.btn_whats_up.setCheckable(True)
        self.btn_whats_up.setToolTip(
            "Group the very same assets by publication date instead of by "
            "scene: Today, Yesterday, then older dates.")
        self.btn_whats_up.toggled.connect(
            lambda _on: self._populate_artist_tree())
        muted_row.addWidget(self.btn_whats_up)
        self.lbl_muted = QLabel("")
        self.lbl_muted.setStyleSheet("color:#8a93a0;")
        muted_row.addWidget(self.lbl_muted, 1)
        self.btn_unmute = QPushButton("Unmute all")
        self.btn_unmute.setEnabled(False)
        self.btn_unmute.clicked.connect(self._on_unmute_all)
        muted_row.addWidget(self.btn_unmute)
        lay.addLayout(muted_row)

        hint = QLabel("Double-click a scene to graph it.")
        hint.setStyleSheet("color:#8a93a0;")
        lay.addWidget(hint)
        return tool

    def _build_right_panel(self):
        """Panneau de droite : la vue du graphe et sa légende."""
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(6, 10, 10, 6)
        lay.setSpacing(6)
        self.view = DependencyGraphView()
        self.view.links_changed.connect(self._on_links_changed)
        self.view.hidden_nodes_changed.connect(self._on_hidden_nodes_changed)
        self.view.edge_menu_requested.connect(self._on_edge_menu)
        self.view.formats_changed.connect(self._rebuild_format_boxes)
        self.view.tasks_changed.connect(self._rebuild_task_boxes)
        self.view.filters_cleared.connect(self._on_link_filters_cleared)
        # Minimum volontairement bas : la partie gauche peut ainsi être
        # élargie très largement en prenant sur le graphe.
        self.view.setMinimumWidth(80)
        panel.setMinimumWidth(120)
        lay.addWidget(self.view, 1)

        # Accolé au graphe : les options d'affichage masquent des nœuds sans
        # bouger les autres, ce bouton resserre l'ensemble à la demande. Il
        # partage la ligne de la légende, juste sous le graphe.
        self.btn_redraw = QPushButton("Redraw layout")
        self.btn_redraw.setToolTip(
            "Pack the visible nodes back together, dropping the columns "
            "freed by the hidden ones, and refit the view.")
        self.btn_redraw.clicked.connect(self._on_redraw_layout)

        lay.addWidget(self._build_legend(self.btn_redraw))
        return panel

    @staticmethod
    def _bold_label(text):
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _build_mysql_tab(self):
        w = self._mysql_tab = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        # Rien n'est demandé ici : hôte, base, utilisateur et mot de passe
        # viennent de data_source (valeurs par défaut, surchargeables par
        # variables d'environnement). Les champs restent créés — mais masqués —
        # pour garder l'option « Remember » disponible si l'on repasse un jour
        # à une saisie interactive.
        self.my_user = QLineEdit()
        self.my_pass = QLineEdit()
        self.my_pass.setEchoMode(QLineEdit.Password)
        self.my_remember = QCheckBox("Remember (system keyring)")
        if _HAS_KEYRING:
            self.my_remember.toggled.connect(self._on_remember_toggled)
        else:
            self.my_remember.setEnabled(False)
        for widget in (self.my_user, self.my_pass, self.my_remember):
            widget.setVisible(False)

        # Une seule ligne d'état : OK en noir, sinon l'erreur en rouge avec le
        # détail (hôte, base, origine des réglages) uniquement en cas d'échec.
        self.my_status = QLabel("")
        self.my_status.setWordWrap(True)
        self.my_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # heightForWidth : le message d'erreur replié se ferait sinon tronquer.
        # Vertical « Minimum » et non « MinimumExpanding » : la ligne « OK »
        # tient sur sa hauteur au lieu de gonfler l'onglet.
        policy = self.my_status.sizePolicy()
        policy.setVerticalPolicy(QSizePolicy.Minimum)
        policy.setHeightForWidth(True)
        self.my_status.setSizePolicy(policy)
        self.my_status.setAlignment(Qt.AlignTop)
        grid.addWidget(self.my_status, 0, 0, 1, 4)

        grid.setColumnStretch(1, 1)
        return w

    def _connection_details(self):
        """Contexte affiché en cas d'erreur seulement."""
        origin = ds.settings_origin()
        local_state = ("local_config.py loaded" if ds.has_local_config()
                       else "no local_config.py found")
        return (f"Host: {ds.mysql_host()} [{origin['host']}] · "
                f"Database: {ds.mysql_database()} [{origin['database']}] · "
                f"User: {ds.mysql_user()} [{origin['user']}] · "
                f"Password [{origin['password']}] · {local_state}")

    def _refresh_mysql_status(self):
        """Teste la connexion et met à jour la ligne d'état."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            ok, message = ds.test_mysql_connection(self._mysql_config())
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            self.my_status.setText("Connection DB OK")
            self.my_status.setStyleSheet("color:#1c7a44;")
        else:
            self.my_status.setText(
                f"Connection Error\n{message}\n{self._connection_details()}")
            self.my_status.setStyleSheet("color:#b4392c;")
        # Le message tient sur une ligne (OK) ou sur plusieurs (erreur) : le
        # bloc se réajuste dans les deux cas.
        self._fit_source_tab()
        return ok

    def _on_source_tab_changed(self, index):
        """Le test de connexion n'a lieu qu'en arrivant sur l'onglet MySQL."""
        self._fit_source_tab(index)
        if index == 0:
            self._refresh_mysql_status()

    def _fit_source_tab(self, index=None):
        """Ajuste la hauteur du bloc à l'onglet **courant**.

        ``QTabWidget.sizeHint()`` se cale toujours sur la page la plus haute :
        l'onglet MySQL, qui tient en une ligne, héritait de la hauteur du CSV
        et laissait un grand vide. On plafonne donc explicitement.
        """
        if index is None:
            index = self.tabs.currentIndex()
        page = self.tabs.widget(index)
        height = page.sizeHint().height()
        if page is self._mysql_tab and self.my_status.text():
            # Message replié : sa hauteur dépend de la largeur disponible.
            width = self.my_status.width() or max(1, page.width() - 20)
            height = max(height, self.my_status.heightForWidth(width) + 20)
        self.tabs.setMaximumHeight(
            height + self.tabs.tabBar().sizeHint().height() + 12)

    def _build_csv_tab(self):
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self.csv_assets = QLineEdit()
        self.csv_scenes = QLineEdit()
        self.csv_binds = QLineEdit()

        for row, (label, edit) in enumerate([
            ("assets.csv", self.csv_assets),
            ("scenes.csv", self.csv_scenes),
            ("binds.csv", self.csv_binds),
        ]):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(edit, row, 1)
            btn = QPushButton("Browse…")
            btn.clicked.connect(
                lambda _=False, e=edit, lbl=label: self._browse_csv(e, lbl))
            grid.addWidget(btn, row, 2)

        folder_btn = QPushButton("Folder… (auto-detect)")
        folder_btn.clicked.connect(self._browse_csv_folder)
        grid.addWidget(folder_btn, 3, 1)

        hint = QLabel("Per-table exports from phpMyAdmin "
                      "(Export → CSV, column names on the first line).")
        hint.setStyleSheet("color:#8a93a0;")
        grid.addWidget(hint, 3, 2)

        grid.setColumnStretch(1, 1)
        return w

    def _build_sql_tab(self):
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self.sql_path = QLineEdit()
        grid.addWidget(QLabel("SQL dump"), 0, 0)
        grid.addWidget(self.sql_path, 0, 1)
        btn = QPushButton("Browse…")
        btn.clicked.connect(self._browse_sql)
        grid.addWidget(btn, 0, 2)

        hint = QLabel("Full mysqldump export (phpMyAdmin → Export → SQL). "
                      "The assets, scenes and binds tables are extracted.")
        hint.setStyleSheet("color:#8a93a0;")
        hint.setWordWrap(True)
        grid.addWidget(hint, 1, 1, 1, 2)

        grid.setColumnStretch(1, 1)
        return w

    def _build_legend(self, leading=None):
        """Légende des couleurs ; ``leading`` ouvre la ligne (bouton Redraw)."""
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(2, 0, 2, 0)
        outer.setSpacing(4)
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        if leading is not None:
            lay.addWidget(leading)

        def swatch(color, text, border=False):
            box = QFrame()
            box.setFixedSize(16, 12)
            if border:
                box.setStyleSheet(
                    f"background:transparent;border:2px solid {color.name()};"
                    "border-radius:3px;")
            else:
                box.setStyleSheet(
                    f"background:{color.name()};border-radius:3px;")
            lay.addWidget(box)
            lay.addWidget(QLabel(text))

        swatch(COL_OK_BORDER, "up to date")
        swatch(COL_STALE_BORDER, "stale input")
        swatch(COL_INHERITED_BORDER, "stale by inheritance")
        swatch(COL_START_BORDER, "queried scene", border=True)
        lay.addStretch(1)
        outer.addLayout(lay)

        # Le rappel des raccourcis occupe sa propre ligne : sur la même que les
        # pastilles il se réduisait à une colonne étroite dès que l'on
        # élargissait la partie gauche.
        self.lbl_tip = tip = QLabel()
        self._refresh_tip()
        tip.setStyleSheet("color:#8a93a0;")
        # Sans cela, ce libellé impose une largeur minimale au panneau droit
        # et empêche d'élargir la partie gauche.
        tip.setWordWrap(True)
        tip.setMinimumWidth(1)
        outer.addWidget(tip)
        return w

    # ------------------------------------------------------- file dialogs --
    def _browse_csv(self, edit, label):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Choose {label}", edit.text() or "",
            "CSV (*.csv);;All files (*)")
        if path:
            edit.setText(path)

    def _browse_csv_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder containing assets/scenes/binds.csv")
        if not folder:
            return
        found = ds.detect_csv_files(folder)
        mapping = {"assets": self.csv_assets, "scenes": self.csv_scenes,
                   "binds": self.csv_binds}
        for key, edit in mapping.items():
            if key in found:
                edit.setText(found[key])
        missing = [k for k in mapping if k not in found]
        if missing:
            self.statusBar().showMessage(
                "Files not found in folder: "
                + ", ".join(f"{m}.csv" for m in missing))
        else:
            self.statusBar().showMessage("Three CSV files detected.")

    def _browse_sql(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an SQL dump", self.sql_path.text() or "",
            "SQL (*.sql);;All files (*)")
        if path:
            self.sql_path.setText(path)

    # ------------------------------------------------------------ MySQL ----
    def _mysql_config(self):
        # Tout vient de data_source (défauts du code ou variables
        # d'environnement) : l'UI ne demande plus d'identifiants.
        return {
            "database": ds.mysql_database(),
            "user": ds.mysql_user(),
            "password": ds.mysql_password(),   # jamais journalisé
        }

    # ------------------------------------------------------- data loading --
    def _current_source(self):
        return ("mysql", "csv", "sql")[self.tabs.currentIndex()]

    def _source_signature(self):
        src = self._current_source()
        if src == "mysql":
            cfg = self._mysql_config()
            return ("mysql", cfg["database"], cfg["user"], cfg["password"])
        if src == "csv":
            return ("csv", self.csv_assets.text(), self.csv_scenes.text(),
                    self.csv_binds.text())
        return ("sql", self.sql_path.text())

    def _load_data(self):
        """Charge (avec cache) les données de la source courante."""
        sig = self._source_signature()
        if sig in self._data_cache:
            return self._data_cache[sig]

        src = self._current_source()
        self.statusBar().showMessage("Loading data…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            if src == "mysql":
                data = ds.load_from_mysql(self._mysql_config())
            elif src == "csv":
                data = ds.load_from_csv({
                    "assets": self.csv_assets.text().strip(),
                    "scenes": self.csv_scenes.text().strip(),
                    "binds": self.csv_binds.text().strip(),
                })
            else:
                path = self.sql_path.text().strip()
                if not path:
                    raise ds.DataSourceError("No SQL dump selected.")
                data = ds.load_from_sql_dump(path)
        finally:
            QApplication.restoreOverrideCursor()

        self._data_cache[sig] = data
        return data

    # ---------------------------------------------------------- Grapher ----
    def _on_grapher(self):
        scene_name = self.scene_edit.text().strip()
        if not scene_name:
            self._error("Enter a scene name (e.g. "
                        f"\"{_DEFAULT_SCENE}\").")
            return

        try:
            assets, scenes, binds = self._load_data()
        except ds.DataSourceError as exc:
            self._error(str(exc), title="Loading error")
            return
        except Exception as exc:  # robustesse
            self._error(f"Unexpected error while loading: {exc}",
                        title="Error")
            return

        try:
            result = gm.build_graph(assets, scenes, binds, scene_name)
        except gm.SceneResolutionError as exc:
            self._error(str(exc), title="Scene not found")
            return
        except Exception as exc:  # robustesse
            self._error(f"Error while building the graph: {exc}",
                        title="Error")
            return

        # La sécurité porte sur la source qui a produit CE graphe.
        self._graph_result = result
        self._graph_source = self._current_source()
        self._sync_mute_backend()
        self.view.set_graph(result)
        # Les mutes enregistrés sont relus à chaque graphe : une nouvelle
        # session retrouve le même état.
        restored = self._apply_db_mutes(refresh=True)
        stats = result.stats
        message = (f"\"{scene_name}\": {stats['nodes']} scenes, "
                   f"{stats['edges']} links "
                   f"({len(assets)} assets, {len(scenes)} scenes in DB).")
        if restored:
            message += (f" {restored} muted link(s) restored from the "
                        f"{self._mutes.label}.")
        elif not self._mutes_enabled() and self._mutes.available:
            message += (" Mutes stay in memory here (saved from "
                        f"{self._mute_sources_label()} only).")
        elif not self._mutes.available and self._mutes.expected():
            message += f" Mutes stay in memory ({self._mutes.error})."
        self.statusBar().showMessage(message)
        self._store_password()

    # ------------------------------------------ mutes persistants (Kraken) --
    def _mute_sources_label(self):
        if self._mute_sources == set(_SOURCE_LABELS):
            return "any source"
        names = [_SOURCE_LABELS[s] for s in ("mysql", "csv", "sql")
                 if s in self._mute_sources]
        return " / ".join(names) if names else "no source"

    def _restricted_sources(self):
        """Vrai si la liste des sources autorisées a été restreinte."""
        return self._mute_sources != set(_SOURCE_LABELS)

    def _mutes_enabled(self, source=None):
        """Vrai si les mutes de CE graphe sont persistants.

        Deux conditions : une cible d'écriture disponible, et une source
        autorisée. La source retenue est celle qui a produit le graphe
        affiché — pas l'onglet courant : changer d'onglet sans regrapher ne
        doit pas ouvrir l'écriture sur des données venues d'ailleurs.
        """
        source = self._graph_source if source is None else source
        return self._mutes.available and source in self._mute_sources

    def _mute_mode_message(self):
        """Ligne d'état résumant où vont les mutes (et d'où)."""
        if not self._mutes.available:
            if self._mutes.expected():
                return (f"Kraken mutes module not loaded "
                        f"({self._mutes.error}) — mutes stay in memory.")
            return "Mutes stay in memory (no mutes backend configured)."
        scope = ("" if not self._restricted_sources()
                 else f", from {self._mute_sources_label()} only")
        if self._mutes.writes_to_db:
            return (f"Mutes (asset version → scene) are written to the studio "
                    f"DB{scope}.")
        return (f"Test mode: mutes (asset version → scene) go to "
                f"{self._mutes.sandbox_file} (studio DB untouched){scope}.")

    def _refresh_tip(self):
        """Rappel des raccourcis sous le graphe, accordé au mode des mutes."""
        action = (f"mute assets (saved in {self._mutes.label})"
                  if self._mutes_enabled() else "mute assets (temporary)")
        self.lbl_tip.setText(
            "Hover: dependencies + artist · Click a link: highlight · "
            f"Right-click a link: {action} · Wheel: zoom · "
            "Middle button: pan · Drag a node: horizontal · "
            "Drag a row handle: reorder tasks")

    def _sync_mute_backend(self):
        """Branche/débranche l'écriture des mutes selon la source graphée."""
        self._refresh_tip()
        if not self._mutes_enabled():
            self.view.set_mute_backend(None)
            return
        target = self._mutes.label          # « DB » ou « test file »
        self.view.set_mute_backend(
            self._couples_mute_backend,
            action_text=f"Right-click: mute assets (saved in {target})",
            muted_text=f"MUTED (saved in {target}) — right-click to unmute",
            partial_text=f"— muted in {target}")

    # ------------------------------------------- menu du clic droit --------
    def _couple_text(self, state):
        """« chaise_anim [abc] (v012) » + marque d'état, pour le menu."""
        text = _with_format(state["label"], state["fmt"])
        version, _stale = _version_state(state["version"], state["latest"])
        text = f"{text} {version}"
        if state["stored"]:
            return f"{text}  —  muted in {self._mutes.label}"
        if state["muted"]:
            return f"{text}  —  muted (session only)"
        return text

    def _build_edge_menu(self, edge_key):
        """Menu des couples (version d'asset, scène) portés par un lien.

        Un seul couple : une entrée, *Mute* ou *Unmute*. Plusieurs : une
        entrée par couple, plus *Mute all* / *Unmute all*. Séparé de
        ``_on_edge_menu`` pour être vérifiable sans ouvrir de fenêtre.
        """
        states = self.view.edge_couple_states(edge_key)
        menu = QMenu(self)
        result = self._graph_result
        if result is not None and edge_key[1] in result.nodes:
            title = menu.addAction(
                f"{result.nodes[edge_key[0]].display_name}  →  "
                f"{result.nodes[edge_key[1]].display_name}")
            title.setEnabled(False)
            menu.addSeparator()
        if not states:
            none = menu.addAction("No asset goes through this link")
            none.setEnabled(False)
            return menu

        def act_for(ids, muted, text):
            action = menu.addAction(text)
            action.triggered.connect(
                lambda _checked=False, i=tuple(ids), m=muted:
                self.view.set_couples_muted(edge_key, i, m))
            return action

        if len(states) == 1:
            state = states[0]
            verb = "Unmute" if state["muted"] else "Mute"
            act_for([state["id"]], not state["muted"],
                    f"{verb}  {self._couple_text(state)}")
            return menu
        for state in states:
            verb = "Unmute" if state["muted"] else "Mute"
            action = act_for([state["id"]], not state["muted"],
                             f"{verb}  {self._couple_text(state)}")
            action.setCheckable(True)
            action.setChecked(state["muted"])
        menu.addSeparator()
        ids = [s["id"] for s in states]
        act_for(ids, True, f"Mute all ({len(ids)})").setEnabled(
            not all(s["muted"] for s in states))
        act_for(ids, False, f"Unmute all ({len(ids)})").setEnabled(
            any(s["muted"] for s in states))
        return menu

    def _on_edge_menu(self, edge_key, global_pos):
        """Ouvre le menu du clic droit sur un lien."""
        self._build_edge_menu(edge_key).exec(global_pos)

    def _mute_scene_path(self, edge_key):
        """(chemin de la scène importatrice, '') ou ('', raison).

        Le couple muté est (version d'asset, **scène enfant**) : c'est donc
        le chemin du bas du lien qui compte. Sans chemin, rien n'est
        enregistrable — les API du module parlent en chemins bruts, on n'en
        reconstruit jamais.
        """
        result = self._graph_result
        if result is None:
            return "", "no graph is displayed"
        node = result.nodes.get(edge_key[1])
        scene_path = node.path if node is not None else ""
        if scene_path:
            return scene_path, ""
        columns = ", ".join(ds._RAW_PATH_COLUMNS[:4]) + "…"
        return "", (f"table \"scenes\" has no file path column "
                    f"({columns}) in this source")

    def _couple_paths(self, edge_key, couple_ids=None):
        """{id de couple: chemin publié} pour les couples demandés.

        Un couple sans chemin (source sans colonne dédiée) est absent : il
        restera mutable en mémoire seulement.
        """
        couples = self._graph_result.edge_couples.get(edge_key, ())
        wanted = None if couple_ids is None else set(couple_ids)
        return {cid: path for cid, _l, _v, _lat, _f, path in couples
                if path and (wanted is None or cid in wanted)}

    def _compute_db_mutes(self, refresh=False):
        """{lien: {couples mutés}} d'après la cible d'écriture.

        Une seule lecture (``get_scene_mutes``) par scène importatrice, quel
        que soit le nombre de liens qui y aboutissent.
        """
        result = self._graph_result
        stored = {}
        refreshed = set()
        for edge_key in result.edge_couples:
            scene_path, _why = self._mute_scene_path(edge_key)
            if not scene_path:
                continue
            paths = self._couple_paths(edge_key)
            if not paths:
                continue
            entries = self._mutes.entries(
                scene_path, refresh=refresh and scene_path not in refreshed)
            refreshed.add(scene_path)
            muted = {cid for cid, path in paths.items()
                     if self._mutes.is_muted(scene_path, path, entries)}
            if muted:
                stored[edge_key] = muted
        return stored

    def _apply_db_mutes(self, refresh=False):
        """Resynchronise l'affichage depuis la cible ; rend le nb de liens muted.

        Gardée par la même sécurité que l'écriture : sur une source non
        autorisée, les mutes enregistrés ne sont ni lus ni appliqués — le
        graphe reste exactement celui d'avant cette fonctionnalité.
        """
        if not self._mutes_enabled() or self._graph_result is None:
            return 0
        try:
            stored = self._compute_db_mutes(refresh=refresh)
        except Exception as exc:
            self.statusBar().showMessage(
                f"Mutes {self._mutes.label} unreachable: {exc}")
            return 0
        self.view.apply_db_mutes(stored)
        return len(self.view.db_muted_edges())

    def _couples_mute_backend(self, edge_key, couple_ids, disable):
        """Enregistre le mute/unmute de couples précis ; True si pris en charge.

        Un couple = une **version d'asset** vers la **scène enfant** : un
        ``mute_asset``/``unmute_asset`` par couple, avec le chemin de cette
        scène. Après l'écriture la cible est relue (``get_scene_mutes``) :
        l'affichage reflète ce qu'elle contient vraiment, pas l'intention du
        clic (consigne du module).
        """
        if not self._mutes_enabled():
            self._mute_notice = (
                "Kept in memory only — mutes are saved from "
                f"{self._mute_sources_label()} only.")
            return False
        scene_path, why = self._mute_scene_path(edge_key)
        if not scene_path:
            # Affiché par _on_links_changed, qui suit le basculement local.
            self._mute_notice = f"Kept in memory only — {why}."
            return False
        paths = self._couple_paths(edge_key, couple_ids)
        missing = [cid for cid in couple_ids if cid not in paths]
        if not paths:
            columns = ", ".join(ds._RAW_PATH_COLUMNS[:4]) + "…"
            self._mute_notice = (
                f"Kept in memory only — table \"assets\" has no file path "
                f"column ({columns}) in this source.")
            return False
        write = self._mutes.mute if disable else self._mutes.unmute
        error = ""
        # Le module journalise ses refus au lieu de lever : on les recueille
        # pour pouvoir dire POURQUOI un couple n'a pas suivi.
        with self._mutes.capture_messages() as messages:
            try:
                for path in paths.values():
                    write(scene_path, path)
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
        self._apply_db_mutes(refresh=True)
        target = self._mutes.label
        # Vérification couple par couple : un refus ne porte souvent que sur
        # une partie, et rien ne doit être annoncé comme enregistré à tort.
        refused = self._refused_couples(edge_key, scene_path, paths, disable)
        if error or refused or missing:
            # Le clic doit malgré tout produire son effet : on rend False pour
            # que la vue mute en mémoire ce qui n'a pas été enregistré.
            self._mute_notice = self._mute_failure_message(
                target, couple_ids, refused + missing, error, list(messages))
            return False
        verb = "muted" if disable else "unmuted"
        extra = (" — every session will now start with them muted."
                 if disable else ".")
        # Passe par _mute_notice comme les échecs : le rafraîchissement
        # links_changed qui suit écraserait un showMessage direct.
        self._mute_notice = (
            f"{len(paths)} asset version(s) {verb} in {target} towards "
            f"{self._graph_result.nodes[edge_key[1]].display_name}{extra}")
        return True

    def _refused_couples(self, edge_key, scene_path, paths, disable):
        """Libellés des couples dont l'état voulu n'a pas été enregistré."""
        labels = {cid: label for cid, label, _v, _lat, _f, _p
                  in self._graph_result.edge_couples.get(edge_key, ())}
        try:
            entries = self._mutes.entries(scene_path)
            return [labels.get(cid, cid) for cid, path in paths.items()
                    if self._mutes.is_muted(scene_path, path, entries)
                    != disable]
        except Exception:
            return [labels.get(cid, cid) for cid in paths]

    def _mute_failure_message(self, target, couple_ids, refused, error,
                              messages):
        """Explique ce qui n'a pas été enregistré, et si possible pourquoi."""
        head = (f"{len(refused)}/{len(couple_ids)} asset version(s) not "
                f"recorded in the {target}: {', '.join(refused[:3])}"
                + ("…" if len(refused) > 3 else "")
                if refused else
                f"The mutes {target} did not record the change")
        # Le clic reste appliqué en mémoire : il faut dire que l'effet est
        # visible mais ne survivra pas à la session.
        tail = " Applied for this session only."
        if error:
            return f"{head} ({error}).{tail}"
        if messages:
            # Le message du module est plus précis que tout ce qu'on pourrait
            # deviner (dossier partagé, asset introuvable…).
            return f"{head} — {messages[0]}{tail}"
        return f"{head}.{tail}"

    def _error(self, message, title="Error"):
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, title, message)

    # -------------------------------------------- Graphist to graph (tool) --
    def _on_check_scenes(self):
        """Contrôle les scènes du graphiste et remplit la liste de gauche."""
        artist = self.artist_edit.text().strip()
        project = self.project_edit.text().strip()
        if not artist:
            self._error("Enter a graphist name.", title="Check scenes")
            return
        if not project:
            self._error("Enter a project code (e.g. \"qua\").",
                        title="Check scenes")
            return

        try:
            assets, scenes, binds = self._load_data()
        except ds.DataSourceError as exc:
            self._error(str(exc), title="Loading error")
            return
        except Exception as exc:
            self._error(f"Unexpected error while loading: {exc}", title="Error")
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            # Le contrôle porte sur TOUTES les tasks d'assets ; le champ
            # « Filter assets tasks » ne filtre que l'affichage.
            # only_outdated_imports=False : on garde aussi les imports à jour,
            # affichés en vert quand on déplie une scène.
            report = am.check_artist_scenes(
                assets, scenes, binds, artist, project, tasks=(),
                shots_only=self.chk_shots_only.isChecked(),
                only_outdated_imports=False,
                scene_tasks=am.parse_tasks(self.scene_tasks_edit.text()))
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            self._error(f"Error while checking scenes: {exc}", title="Error")
            return
        finally:
            QApplication.restoreOverrideCursor()

        self._artist_report = report
        if not report.artists:
            known = am.list_authors(scenes, project)
            extra = (" Known graphists: " + ", ".join(known[:12]) + ("…"
                     if len(known) > 12 else "")) if known else ""
            self._error(f"No graphist matching \"{artist}\" on project "
                        f"\"{project}\".{extra}", title="Check scenes")
            self.artist_tree.clear()
            self.artist_summary.setText("")
            return
        self._populate_artist_tree()
        self.statusBar().showMessage(
            f"{report.total} scene(s) for {', '.join(report.artists)} on "
            f"\"{project}\": {report.outdated} to update.")

    def _artist_rows(self):
        """Lignes retenues, scène par scène, après mutes et filtres d'affichage.

        Renvoie une liste de ``(entry, rows, available_rows, outdated_rows)``
        partagée par les deux modes d'affichage — par scène et par date — pour
        que « What's up ? » liste rigoureusement les mêmes assets.
        """
        report = getattr(self, "_artist_report", None)
        if report is None:
            return []
        only_outdated = self.chk_only_outdated.isChecked()
        # Filtre d'affichage par task d'asset (vide = toutes).
        wanted = am.parse_tasks(self.tasks_edit.text())
        wanted = {gm.canon_task(t) for t in wanted} if wanted else None
        show_uptodate = self.chk_show_uptodate.isChecked()
        show_available = self.chk_show_available.isChecked()
        # Filtres supplémentaires, propres aux assets non importés (bleus).
        avail_wanted = am.parse_tasks(self.avail_tasks_edit.text())
        avail_wanted = ({gm.canon_task(t) for t in avail_wanted}
                        if avail_wanted else None)
        avail_formats = am.parse_formats(self.avail_formats_edit.text())
        avail_formats = set(avail_formats) if avail_formats else None

        def keep(entry, row):
            return ((entry.scene_name, row.label) not in self._muted_assets
                    and (wanted is None
                         or gm.canon_task(row.task_name) in wanted))

        def keep_available(entry, row):
            return (keep(entry, row)
                    and (avail_wanted is None
                         or gm.canon_task(row.task_name) in avail_wanted)
                    and (avail_formats is None or row.fmt in avail_formats))

        prepared = []
        for entry in report.entries:
            rows = [r for r in entry.imports if keep(entry, r)]
            # Le statut de la scène se calcule AVANT de masquer les imports à
            # jour : les cacher ne doit rien changer au diagnostic.
            outdated_rows = [r for r in rows if r.outdated]
            if not show_uptodate:
                rows = outdated_rows
            # Assets publiés sur le plan mais non importés : informatifs, ils
            # ne rendent jamais la scène obsolète (donc pas de « continue »
            # basé sur eux).
            available_rows = ([r for r in entry.available
                               if keep_available(entry, r)]
                              if show_available else [])
            if only_outdated and not outdated_rows:
                continue
            prepared.append((entry, rows, available_rows, outdated_rows))
        return prepared

    def _capture_expanded(self):
        """Mémorise les groupes dépliés du mode actuellement affiché."""
        root = self.artist_tree.invisibleRootItem()
        if root.childCount() == 0:
            return          # arbre vide : on garde la mémoire précédente
        keys = set()
        for i in range(root.childCount()):
            item = root.child(i)
            key = item.data(0, _ROLE_GROUP)
            if item.isExpanded() and key is not None:
                keys.add(key)
        self._expanded_groups[self._artist_tree_mode] = keys

    def _restore_expanded(self, mode):
        """Redéplie les groupes qui l'étaient ; les nouveaux restent repliés."""
        keys = self._expanded_groups.get(mode, set())
        root = self.artist_tree.invisibleRootItem()
        for i in range(root.childCount()):
            item = root.child(i)
            item.setExpanded(item.data(0, _ROLE_GROUP) in keys)

    def _populate_artist_tree(self):
        """(Re)construit l'arbre des résultats, en respectant les mutes."""
        report = getattr(self, "_artist_report", None)
        # L'état des triangles est relevé AVANT le clear() : muter un asset ou
        # cocher une option ne doit rien replier.
        self._capture_expanded()
        self.artist_tree.clear()
        if report is None:
            return

        prepared = self._artist_rows()
        mode = "date" if self.btn_whats_up.isChecked() else "scene"
        if mode == "date":
            detail = self._fill_tree_by_date(prepared)
        else:
            detail = self._fill_tree_by_scene(prepared)
        self._artist_tree_mode = mode
        self._restore_expanded(mode)

        tasks = ", ".join(report.tasks) if report.tasks else "all"
        approx = ("  (loose name match)" if report.match_mode == "approx"
                  else "")
        self.artist_summary.setText(
            f"{', '.join(report.artists)}{approx} · project(s): "
            f"{', '.join(report.projects) or '—'} · tasks: {tasks}\n"
            f"{report.total} scene(s): {report.outdated} to update, "
            f"{report.up_to_date} up to date, {report.no_imports} without "
            f"matching import." + detail)
        self._refresh_muted_label()

    def _fill_tree_by_scene(self, prepared):
        """Affichage par défaut : une scène par ligne, ses assets en dessous."""
        for entry, rows, available_rows, outdated_rows in prepared:
            if entry.checked_count == 0:
                state = "no import"
                color = QColor("#8a93a0")
            elif outdated_rows:
                state = f"{len(outdated_rows)} to update"
                color = QColor("#b4392c")
            elif entry.has_outdated:
                # Périmée, mais rien ne correspond au filtre de tasks courant.
                state = "nothing matching the task filter"
                color = QColor("#8a93a0")
            else:
                state = "up to date"
                color = QColor("#1c7a44")
            if available_rows:
                state += f" · {len(available_rows)} not imported"

            top = QTreeWidgetItem(
                self.artist_tree,
                [entry.display_name,
                 f"v{entry.version:03d}" if entry.version is not None else "v?",
                 ""])
            top.setToolTip(0, f"{entry.scene_name}\nauthor: {entry.author}\n"
                              f"{entry.checked_count} import(s) checked — {state}")
            top.setForeground(1, color)
            top.setData(0, Qt.UserRole, entry.scene_name)
            top.setData(0, _ROLE_GROUP, entry.scene_name)
            # Toutes les scènes en gras : elles se distinguent ainsi des
            # lignes d'asset, quelle que soit leur fraîcheur.
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)

            for row in rows:
                text, stale = _version_state(row.version, row.latest)
                child = QTreeWidgetItem(top, [row.label, text, ""])
                child.setForeground(1, QColor("#b4392c") if stale
                                    else QColor("#1c7a44"))
                tip = self._asset_tooltip(row)
                child.setToolTip(0, tip)
                child.setToolTip(1, tip)
                self.artist_tree.setItemWidget(
                    child, 2, self._make_mute_button(entry.scene_name, row.label))

            # Assets disponibles mais non importés : en bleu.
            for row in available_rows:
                child = QTreeWidgetItem(
                    top, [row.label, f"v{row.version:03d}"
                          if row.version is not None else "v?", ""])
                child.setForeground(0, _COL_AVAILABLE)
                child.setForeground(1, _COL_AVAILABLE)
                tip = self._asset_tooltip(
                    row, "Published on the shot but not imported by this "
                         "scene — does not make it outdated.")
                child.setToolTip(0, tip)
                child.setToolTip(1, tip)
                self.artist_tree.setItemWidget(
                    child, 2, self._make_mute_button(entry.scene_name, row.label))

        total = getattr(self._artist_report, "total", len(prepared))
        return "" if len(prepared) == total else f"  ({len(prepared)} shown)"

    def _fill_tree_by_date(self, prepared):
        """Mode « What's up ? » : les mêmes assets, regroupés par date.

        Un asset publié une fois mais importé par plusieurs scènes du graphiste
        n'apparaît qu'une seule fois ; les scènes concernées sont rappelées dans
        l'info-bulle, et le bouton « mute » les couvre toutes.
        """
        # date (ou None) -> label d'asset -> [row, [scènes qui l'utilisent]]
        groups = {}
        for entry, rows, available_rows, _outdated in prepared:
            for row in list(rows) + list(available_rows):
                # On classe sur la dernière version publiée : un import périmé
                # est une nouvelle du jour où la version qui le périme est
                # sortie, pas du jour de l'ancienne.
                day = am.parse_asset_date(row.news_date)
                bucket = groups.setdefault(day, {})
                key = (row.label, row.version, row.kind)
                slot = bucket.get(key)
                if slot is None:
                    bucket[key] = [row, [entry]]
                else:
                    slot[1].append(entry)

        # Aujourd'hui d'abord, puis du plus récent au plus ancien ; les assets
        # sans date lisible ferment la marche.
        days = sorted((d for d in groups if d is not None), reverse=True)
        if None in groups:
            days.append(None)

        assets = 0
        for day in days:
            bucket = groups[day]
            items = sorted(bucket.values(), key=lambda slot: slot[0].label)
            assets += len(items)
            outdated = sum(1 for row, _users in items if row.outdated)
            label = am.date_group_label(day)
            top = QTreeWidgetItem(self.artist_tree,
                                  [label, f"{len(items)}", ""])
            top.setData(0, _ROLE_GROUP, label)
            top.setToolTip(0, self._no_date_hint(len(items)) if day is None
                           else f"{len(items)} asset(s) published — "
                                f"{outdated} outdated in the scenes listed.")
            top.setForeground(1, QColor("#b4392c") if outdated
                              else QColor("#8a93a0"))
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)

            for row, users in items:
                if row.available:
                    text = (f"v{row.version:03d}"
                            if row.version is not None else "v?")
                else:
                    text, _stale = _version_state(row.version, row.latest)
                child = QTreeWidgetItem(top, [row.label, text, ""])
                if row.available:
                    child.setForeground(0, _COL_AVAILABLE)
                    child.setForeground(1, _COL_AVAILABLE)
                else:
                    child.setForeground(1, QColor("#b4392c") if row.outdated
                                        else QColor("#1c7a44"))
                names = sorted({e.display_name for e in users})
                tip = self._asset_tooltip(
                    row, ("not imported by: " if row.available
                          else "imported by: ") + ", ".join(names))
                child.setToolTip(0, tip)
                child.setToolTip(1, tip)
                self.artist_tree.setItemWidget(
                    child, 2, self._make_mute_button(
                        [e.scene_name for e in users], row.label))

        detail = (f"  (what's up: {assets} asset(s) over {len(days)} date(s) "
                  f"in {len(prepared)} scene(s))")
        if days == [None]:
            # Tout sous « Unknown date » : la table assets n'expose aucune
            # colonne de date exploitable — on le dit plutôt que de laisser
            # croire à un bug d'affichage.
            column = ds.asset_schema().get("date_column")
            detail += ("\nNo publication date in the assets table"
                       + (f" (column \"{column}\" is empty)." if column
                          else " — hover \"Unknown date\" for its columns."))
        return detail

    @staticmethod
    def _no_date_hint(count):
        """Explique pourquoi des assets n'ont aucune date exploitable."""
        schema = ds.asset_schema()
        columns = ", ".join(schema.get("columns") or ()) or "unknown"
        used = schema.get("date_column") or "none found"
        return (f"{count} asset(s) without a readable publication date.\n"
                f"assets date column used: {used}\n"
                f"assets columns seen: {columns}")

    @staticmethod
    def _asset_tooltip(row, extra=""):
        """Info-bulle d'une ligne d'asset : version, date d'export, graphiste."""
        lines = [row.label, f"task: {row.task_name}"]
        if row.fmt:
            lines.append(f"format: {row.fmt}")
        if row.outdated:
            lines.append(f"version: {_version_state(row.version, row.latest)[0]}"
                         "  (outdated)")
        else:
            lines.append(f"version: {_version_state(row.version, row.latest)[0]}")
        lines.append(f"exported: {row.date or 'unknown date'}")
        lines.append(f"published by: {row.author or 'unknown'}")
        if row.outdated:
            # Ce qui rend l'import périmé : quand et par qui la nouvelle
            # version est sortie.
            lines.append(f"v{row.latest:03d} published: "
                         f"{row.latest_date or 'unknown date'} by "
                         f"{row.latest_author or 'unknown'}")
        if extra:
            lines.append(extra)
        return "\n".join(lines)

    def _make_mute_button(self, scene_names, label):
        """Bouton « mute » d'une ligne d'asset (une ou plusieurs scènes)."""
        names = ((scene_names,) if isinstance(scene_names, str)
                 else tuple(scene_names))
        button = QToolButton()
        button.setText("×")
        button.setAutoRaise(True)
        button.setToolTip("Mute this asset (hide the line)" if len(names) < 2
                          else f"Mute this asset in the {len(names)} scenes "
                               "that use it")
        button.clicked.connect(
            lambda _checked=False, s=names, a=label: self._mute_asset(s, a))
        return button

    def _mute_asset(self, scene_names, label):
        """Masque l'asset (mémoire seulement : rien n'est écrit en base)."""
        names = ((scene_names,) if isinstance(scene_names, str)
                 else tuple(scene_names))
        self._muted_assets.update((name, label) for name in names)
        self._populate_artist_tree()
        self.statusBar().showMessage(f"Muted: {label}")

    def _on_unmute_all(self):
        if not self._muted_assets:
            return
        count = len(self._muted_assets)
        self._muted_assets.clear()
        self._populate_artist_tree()
        self.statusBar().showMessage(f"{count} asset(s) unmuted.")

    def _refresh_muted_label(self):
        count = len(self._muted_assets)
        self.lbl_muted.setText(f"{count} asset(s) muted." if count else "")
        self.btn_unmute.setEnabled(bool(count))

    def _on_artist_item_activated(self, item, _column):
        """Double-clic sur une scène : bascule vers l'outil de graphe."""
        scene_name = item.data(0, Qt.UserRole)
        if not scene_name:
            return
        self.scene_edit.setText(scene_name)
        self.tool_tabs.setCurrentIndex(0)
        self._on_grapher()

    # ------------------------------------------------------------- links ---
    def _on_hidden_nodes_changed(self, hidden_count):
        """Rappelle combien de nœuds sont masqués par l'option d'affichage."""
        self.lbl_hidden.setText(
            f"{hidden_count} node(s) hidden (no active path to the queried "
            "scene)." if hidden_count else "")

    def _on_links_changed(self, disabled_count):
        """Un couple ou un filtre a changé : résume l'état des liens.

        Un mute de couple ne coupe pas forcément un lien (il en faut *tous*
        les couples) : le compte de liens coupés ne suffit donc pas à décrire
        ce qui vient de se passer — d'où le message posé par le backend.
        """
        notice, self._mute_notice = self._mute_notice, ""
        if notice:
            tail = (f" ({disabled_count} link(s) disabled)" if disabled_count
                    else "")
            self.statusBar().showMessage(notice + tail)
            return
        if not disabled_count:
            muted = sum(len(ids)
                        for ids in self.view.memory_muted_couples().values())
            muted += sum(len(self.view.stored_couple_ids(key))
                         for key in self.view.db_muted_edges() or ())
            self.statusBar().showMessage(
                f"{muted} asset version(s) muted — no link fully cut."
                if muted else "All links enabled.")
            return
        if self._mutes_enabled():
            db_count = len(self.view.db_muted_edges())
            tail = (f" ({db_count} saved in {self._mutes.label})"
                    if db_count else "")
            message = (f"{disabled_count} link(s) disabled{tail} — statuses "
                       "recomputed.")
        else:
            message = (f"{disabled_count} link(s) temporarily disabled — "
                       "statuses recomputed. Nothing is written to the "
                       "database.")
        self.statusBar().showMessage(message)

    # ------------------------------------------------- keyring (password) --
    def _on_remember_toggled(self, checked):
        """Coché : enregistre tout de suite · décoché : oublie."""
        if checked:
            self._store_password(verbose=True)
        else:
            self._forget_password(verbose=True)

    def _store_password(self, verbose=False):
        """Enregistre le mot de passe dans le trousseau système."""
        if not (_HAS_KEYRING and self.my_remember.isChecked()):
            return
        user = self.my_user.text().strip()
        pwd = self.my_pass.text()
        if not user or not pwd:
            if verbose:
                self._keyring_status(
                    "Enter user and password to remember them.", ok=False)
            return
        # Si le login a changé, on nettoie l'entrée précédente.
        previous = self._settings.value("mysql/remember_user", "")
        if previous and previous != user:
            self._delete_password(previous)
        try:
            keyring.set_password(_KEYRING_SERVICE, user, pwd)
        except Exception as exc:      # message sans le mot de passe
            if verbose:
                self._keyring_status(f"Keyring error: {exc}", ok=False)
            return
        self._settings.setValue("mysql/remember_user", user)
        if verbose:
            self._keyring_status(f"Password saved for \"{user}\".", ok=True)

    def _forget_password(self, verbose=False):
        """Supprime le mot de passe stocké (décochage)."""
        if not _HAS_KEYRING:
            return
        user = (self._settings.value("mysql/remember_user", "")
                or self.my_user.text().strip())
        self._settings.remove("mysql/remember_user")
        if not user:
            return
        ok = self._delete_password(user)
        if verbose:
            self._keyring_status(
                f"Stored password removed for \"{user}\"." if ok
                else "No stored password to remove.", ok=True)

    @staticmethod
    def _delete_password(user):
        try:
            keyring.delete_password(_KEYRING_SERVICE, user)
            return True
        except Exception:
            return False   # entrée absente : rien à faire

    def _keyring_status(self, message, ok):
        color = "#4fa06d" if ok else "#c9564e"
        self.my_status.setText(message)
        self.my_status.setStyleSheet(f"color:{color};")
        self.statusBar().showMessage(message)

    # --------------------------------------------------------- settings ----

    def _migrate_legacy_settings(self):
        """Reprend les réglages/mot de passe de l'ancien nom de l'outil.

        L'outil s'appelait « Hydra » : on récupère une seule fois ses réglages
        et son entrée de trousseau, puis on efface les anciens pour ne pas
        laisser traîner un identifiant orphelin.
        """
        legacy = QSettings(_LEGACY_ORG, _APP)
        keys = legacy.allKeys()
        if keys and not self._settings.allKeys():
            for key in keys:
                self._settings.setValue(key, legacy.value(key))
            self._settings.sync()
        legacy_user = legacy.value("mysql/remember_user", "") or \
            legacy.value("mysql/user", "")
        if keys:
            legacy.clear()
            legacy.sync()

        if not (_HAS_KEYRING and legacy_user):
            return
        try:
            pwd = keyring.get_password(_LEGACY_KEYRING_SERVICE, legacy_user)
            if pwd:
                keyring.set_password(_KEYRING_SERVICE, legacy_user, pwd)
            keyring.delete_password(_LEGACY_KEYRING_SERVICE, legacy_user)
        except Exception:
            pass   # rien de bloquant : au pire, à ressaisir une fois

    def _restore_settings(self):
        s = self._settings
        # Ancien réglage devenu obsolète (la base vient de $MYSQL_DATABASE) :
        # on le purge pour qu'une valeur erronée ne traîne pas.
        s.remove("mysql/database")
        self.my_user.setText(s.value("mysql/user", ""))
        self.csv_assets.setText(s.value("csv/assets", ""))
        self.csv_scenes.setText(s.value("csv/scenes", ""))
        self.csv_binds.setText(s.value("csv/binds", ""))
        self.sql_path.setText(s.value("sql/path", ""))
        self.scene_edit.setText(s.value("scene", ""))
        self.artist_edit.setText(s.value("artist/name", ""))
        self.project_edit.setText(s.value("artist/project", ""))
        self.tasks_edit.setText(s.value("artist/tasks", am.ALL_TASKS))
        self.scene_tasks_edit.setText(
            s.value("artist/scene_tasks", am.ALL_TASKS))
        self.chk_shots_only.setChecked(
            s.value("artist/shots_only", "true") == "true")
        self.chk_show_uptodate.setChecked(
            s.value("artist/show_uptodate", "true") == "true")
        self.chk_show_available.setChecked(
            s.value("artist/show_available", "false") == "true")
        idx = int(s.value("source_tab", 0))
        if 0 <= idx < self.tabs.count():
            self.tabs.setCurrentIndex(idx)
        idx = int(s.value("tool_tab", 0))
        if 0 <= idx < self.tool_tabs.count():
            self.tool_tabs.setCurrentIndex(idx)
        # Le mot de passe n'est jamais lu depuis QSettings ; seulement du
        # trousseau système si l'utilisateur l'a explicitement demandé.
        if _HAS_KEYRING and s.value("mysql/remember", "false") == "true":
            # setChecked déclencherait _on_remember_toggled (et un faux
            # message d'erreur, le champ étant encore vide) : on le bloque.
            self.my_remember.blockSignals(True)
            self.my_remember.setChecked(True)
            self.my_remember.blockSignals(False)
            user = (s.value("mysql/remember_user", "")
                    or self.my_user.text().strip())
            if user:
                try:
                    pwd = keyring.get_password(_KEYRING_SERVICE, user)
                except Exception as exc:
                    self.statusBar().showMessage(f"Keyring error: {exc}")
                    pwd = None
                if pwd:
                    self.my_pass.setText(pwd)
                    if not self.my_user.text().strip():
                        self.my_user.setText(user)

    def _save_settings(self):
        s = self._settings
        s.setValue("mysql/user", self.my_user.text())
        s.setValue("csv/assets", self.csv_assets.text())
        s.setValue("csv/scenes", self.csv_scenes.text())
        s.setValue("csv/binds", self.csv_binds.text())
        s.setValue("sql/path", self.sql_path.text())
        s.setValue("scene", self.scene_edit.text())
        s.setValue("artist/name", self.artist_edit.text())
        s.setValue("artist/project", self.project_edit.text())
        s.setValue("artist/tasks", self.tasks_edit.text())
        s.setValue("artist/scene_tasks", self.scene_tasks_edit.text())
        s.setValue("artist/shots_only",
                   "true" if self.chk_shots_only.isChecked() else "false")
        s.setValue("artist/show_uptodate",
                   "true" if self.chk_show_uptodate.isChecked() else "false")
        s.setValue("artist/show_available",
                   "true" if self.chk_show_available.isChecked() else "false")
        s.setValue("source_tab", self.tabs.currentIndex())
        s.setValue("tool_tab", self.tool_tabs.currentIndex())
        s.setValue("mysql/remember",
                   "true" if (_HAS_KEYRING and self.my_remember.isChecked())
                   else "false")
        # Le mot de passe n'est JAMAIS écrit dans QSettings : il part dans le
        # trousseau système, et seulement si « Remember » est coché.
        self._store_password()

    def closeEvent(self, event):
        self._save_settings()
        super().closeEvent(event)


def main():
    _set_windows_app_id()          # avant toute fenêtre
    app = QApplication(sys.argv)
    app.setApplicationName("Dedale — Dependency Graph")
    icon = app_icon()
    if not icon.isNull():
        # Au niveau application : hérité par la fenêtre et les dialogues.
        app.setWindowIcon(icon)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
