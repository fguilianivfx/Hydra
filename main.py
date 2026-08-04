#!/usr/bin/env python3
"""Dedale — graphe interactif de dépendances de scènes (PySide6).

Application de bureau native. À partir du nom d'une scène et d'une source de
données (MySQL / CSV / dump SQL), affiche un graphe interactif des scènes dont
elle dépend, coloré selon leur fraîcheur (vert = à jour, rouge = périmé).

Lancement :  python main.py
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import data_source as ds
import graph_model as gm
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

_KEYRING_SERVICE = "Dedale-DependencyGraph"
_ORG = "Dedale"
_APP = "DependencyGraph"
# Anciens noms (outil renommé) : migrés au premier lancement.
_LEGACY_KEYRING_SERVICE = "Hydra-DependencyGraph"
_LEGACY_ORG = "Hydra"
_DEFAULT_SCENE = "qua_077_02000_comp_v019"


def _version_state(current, latest):
    """« (vXXX) » si à jour, « (vXXX → vYYY) » si une version plus récente existe."""
    cur = f"v{current:03d}" if current is not None else "v?"
    if current is not None and latest is not None and current < latest:
        return f"({cur} → v{latest:03d})", True
    return f"({cur})", False


class LinkDetailsDialog(QDialog):
    """Détail d'un lien sélectionné : assets transitant, inputs et outputs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Link details")
        self.resize(460, 420)
        self.setModal(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        self._header = QLabel()
        self._header.setWordWrap(True)
        self._header.setTextFormat(Qt.RichText)
        lay.addWidget(self._header)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Asset", "Version"])
        self._tree.setRootIsDecorated(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setColumnWidth(0, 280)
        lay.addWidget(self._tree, 1)

        hint = QLabel("Right-click a link in the graph to disable it "
                      "temporarily (nothing is written to the database).")
        hint.setStyleSheet("color:#8a93a0;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

    def show_link(self, info):
        state = ("  —  <b>DISABLED</b>" if info["disabled"] else "")
        if info["carries_stale"]:
            verdict = ("<span style='color:#b4392c;'>This link carries an "
                       "outdated asset.</span>")
        else:
            verdict = ("<span style='color:#1c7a44;'>All assets through this "
                       "link are up to date.</span>")
        self._header.setText(
            f"<b>{info['parent']}</b><br>&nbsp;&nbsp;↓ feeds<br>"
            f"<b>{info['child']}</b>{state}<br>{verdict}")

        self._tree.clear()
        # 1) ce qui transite réellement par ce lien.
        flowing = QTreeWidgetItem(
            self._tree, [f"Assets through this link ({len(info['assets'])})", ""])
        flowing.setExpanded(True)
        for label, cur, latest in info["assets"]:
            text, stale = _version_state(cur, latest)
            item = QTreeWidgetItem(flowing, [label, text])
            if stale:
                item.setForeground(1, QColor("#b4392c"))
            else:
                item.setForeground(1, QColor("#1c7a44"))

        # 2) rappel des outputs de chaque extrémité.
        self._add_outputs("Outputs of " + info["parent"],
                          info["parent_outputs"], info["parent_version"])
        self._add_outputs("Outputs of " + info["child"],
                          info["child_outputs"], info["child_version"])
        self.show()
        self.raise_()

    def _add_outputs(self, title, outputs, version):
        root = QTreeWidgetItem(self._tree, [f"{title} ({len(outputs)})", ""])
        for name, latest in outputs:
            text, stale = _version_state(version, latest)
            item = QTreeWidgetItem(root, [name, text])
            item.setForeground(1, QColor("#b4392c") if stale
                               else QColor("#1c7a44"))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dedale — Scene Dependency Graph")
        self.resize(1280, 860)

        self._settings = QSettings(_ORG, _APP)
        self._migrate_legacy_settings()
        # Cache des données par signature de source (en mémoire uniquement).
        self._data_cache = {}
        self._link_dialog = None

        self._build_ui()
        self._restore_settings()

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        # Split vertical : saisies à gauche, graphe à droite.
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([390, 900])
        self.setCentralWidget(splitter)

        self._build_toolbar()
        self.statusBar().showMessage("Ready.")

    def _build_left_panel(self):
        """Panneau de gauche : source de données + saisie de la scène."""
        panel = QWidget()
        panel.setMinimumWidth(340)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 10, 6, 10)
        lay.setSpacing(8)

        lay.addWidget(self._bold_label("Data source"))
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_mysql_tab(), "MySQL")
        self.tabs.addTab(self._build_csv_tab(), "CSV")
        self.tabs.addTab(self._build_sql_tab(), "SQL dump")
        lay.addWidget(self.tabs)

        lay.addSpacing(6)
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
        self.chk_show_disconnected = QCheckBox(
            "Show nodes cut off by disabled links")
        self.chk_show_disconnected.setChecked(True)
        self.chk_show_disconnected.setToolTip(
            "When a link is disabled, the parents it fed may no longer reach "
            "the queried scene. Uncheck to hide them.")
        self.chk_show_disconnected.toggled.connect(
            lambda on: self.view.set_show_disconnected(on))
        lay.addWidget(self.chk_show_disconnected)

        self.lbl_hidden = QLabel("")
        self.lbl_hidden.setStyleSheet("color:#8a93a0;")
        self.lbl_hidden.setWordWrap(True)
        lay.addWidget(self.lbl_hidden)

        lay.addStretch(1)
        return panel

    def _build_right_panel(self):
        """Panneau de droite : la vue du graphe et sa légende."""
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(6, 10, 10, 6)
        lay.setSpacing(6)
        self.view = DependencyGraphView()
        self.view.edge_selected.connect(self._on_edge_selected)
        self.view.links_changed.connect(self._on_links_changed)
        self.view.hidden_nodes_changed.connect(self._on_hidden_nodes_changed)
        lay.addWidget(self.view, 1)
        lay.addWidget(self._build_legend())
        return panel

    @staticmethod
    def _bold_label(text):
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _build_mysql_tab(self):
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        # Ni l'hôte ni la base ne sont demandés : ils viennent de
        # $MYSQL_HOST / $MYSQL_DATABASE (voir data_source).
        self.my_user = QLineEdit()
        self.my_pass = QLineEdit()
        self.my_pass.setEchoMode(QLineEdit.Password)
        self.my_pass.setPlaceholderText("(kept in memory only)")

        show = QCheckBox("Show")
        show.toggled.connect(
            lambda on: self.my_pass.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))

        grid.addWidget(QLabel("User"), 0, 0)
        grid.addWidget(self.my_user, 0, 1, 1, 3)

        grid.addWidget(QLabel("Password"), 1, 0)
        pass_row = QHBoxLayout()
        pass_row.addWidget(self.my_pass, 1)
        pass_row.addWidget(show)
        grid.addLayout(pass_row, 1, 1, 1, 3)

        self.my_remember = QCheckBox("Remember (system keyring)")
        if _HAS_KEYRING:
            self.my_remember.setToolTip(
                "Store the password in the OS keyring (Windows Credential "
                "Manager, macOS Keychain, Secret Service). Never written to "
                "the app's settings file.")
            self.my_remember.toggled.connect(self._on_remember_toggled)
        else:
            self.my_remember.setEnabled(False)
            self.my_remember.setToolTip(
                "Install the 'keyring' module to enable this option "
                "(pip install keyring). Without it, no password is ever "
                "written to disk.")
        grid.addWidget(self.my_remember, 2, 1, 1, 3)

        btn_test = QPushButton("Test connection")
        btn_test.clicked.connect(self._on_test_connection)
        grid.addWidget(btn_test, 3, 1)

        self.my_status = QLabel("")
        self.my_status.setWordWrap(True)
        grid.addWidget(self.my_status, 3, 2, 1, 2)

        hint = QLabel(f"Host: {ds.mysql_host()} (${{MYSQL_HOST}}) · "
                      f"Database: {ds.mysql_database()} (${{MYSQL_DATABASE}})")
        hint.setStyleSheet("color:#8a93a0;")
        hint.setWordWrap(True)
        grid.addWidget(hint, 4, 0, 1, 4)

        grid.setColumnStretch(1, 1)
        return w

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

    def _build_toolbar(self):
        tb = self.addToolBar("View")
        tb.setMovable(False)

        act_recenter = QAction("Recenter", self)
        act_recenter.setShortcut(QKeySequence("Ctrl+0"))
        act_recenter.triggered.connect(lambda: self.view.reset_view())
        tb.addAction(act_recenter)

        act_reset = QAction("Reset layout", self)
        act_reset.triggered.connect(lambda: self.view.reset_layout())
        tb.addAction(act_reset)

        self.act_enable_links = QAction("Enable all links", self)
        self.act_enable_links.setToolTip(
            "Re-enable every temporarily disabled link")
        self.act_enable_links.setEnabled(False)
        self.act_enable_links.triggered.connect(
            lambda: self.view.enable_all_links())
        tb.addAction(self.act_enable_links)

    def _build_legend(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(14)

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
        tip = QLabel("Hover: dependencies + artist · Click a link: details · "
                     "Right-click a link: disable (temporary) · Wheel: zoom · "
                     "Middle button: pan · Drag a node: horizontal · "
                     "Drag a row handle: reorder tasks")
        tip.setStyleSheet("color:#8a93a0;")
        lay.addWidget(tip)
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
        # L'hôte vient de $MYSQL_HOST (data_source.mysql_host), pas de l'UI.
        return {
            "database": ds.mysql_database(),
            "user": self.my_user.text().strip(),
            "password": self.my_pass.text(),   # jamais journalisé
        }

    def _on_test_connection(self):
        cfg = self._mysql_config()
        self.my_status.setText("Testing…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            ok, msg = ds.test_mysql_connection(cfg)
        finally:
            QApplication.restoreOverrideCursor()
        color = "#4fa06d" if ok else "#c9564e"
        self.my_status.setText(msg)
        self.my_status.setStyleSheet(f"color:{color};")
        if ok:
            self._store_password()

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

        self.view.set_graph(result)
        stats = result.stats
        self.statusBar().showMessage(
            f"\"{scene_name}\": {stats['nodes']} scenes, "
            f"{stats['edges']} links "
            f"({len(assets)} assets, {len(scenes)} scenes in DB).")
        self._store_password()

    def _error(self, message, title="Error"):
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, title, message)

    # ------------------------------------------------------------- links ---
    def _on_edge_selected(self, info):
        """Affiche le détail du lien cliqué (fenêtre non modale)."""
        if self._link_dialog is None:
            self._link_dialog = LinkDetailsDialog(self)
        self._link_dialog.show_link(info)

    def _on_hidden_nodes_changed(self, hidden_count):
        """Rappelle combien de nœuds sont masqués par l'option d'affichage."""
        self.lbl_hidden.setText(
            f"{hidden_count} node(s) hidden (no active path to the queried "
            "scene)." if hidden_count else "")

    def _on_links_changed(self, disabled_count):
        """Un lien a été désactivé/réactivé (changement temporaire)."""
        self.act_enable_links.setEnabled(disabled_count > 0)
        if disabled_count:
            self.statusBar().showMessage(
                f"{disabled_count} link(s) temporarily disabled — statuses "
                "recomputed. Nothing is written to the database.")
        else:
            self.statusBar().showMessage("All links enabled.")

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
        idx = int(s.value("source_tab", 0))
        if 0 <= idx < self.tabs.count():
            self.tabs.setCurrentIndex(idx)
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
        s.setValue("source_tab", self.tabs.currentIndex())
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
    app = QApplication(sys.argv)
    app.setApplicationName("Dedale — Dependency Graph")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
