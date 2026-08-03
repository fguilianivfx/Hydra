#!/usr/bin/env python3
"""Hydra — graphe interactif de dépendances de scènes (PySide6).

Application de bureau native. À partir du nom d'une scène et d'une source de
données (MySQL / CSV / dump SQL), affiche un graphe interactif des scènes dont
elle dépend, coloré selon leur fraîcheur (vert = à jour, rouge = périmé).

Lancement :  python main.py
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
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
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
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

_KEYRING_SERVICE = "Hydra-DependencyGraph"
_DEFAULT_SCENE = "qua_077_02000_comp_v019"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hydra — Scene Dependency Graph")
        self.resize(1280, 860)

        self._settings = QSettings("Hydra", "DependencyGraph")
        # Cache des données par signature de source (en mémoire uniquement).
        self._data_cache = {}

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

        lay.addStretch(1)
        return panel

    def _build_right_panel(self):
        """Panneau de droite : la vue du graphe et sa légende."""
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(6, 10, 10, 6)
        lay.setSpacing(6)
        self.view = DependencyGraphView()
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

        self.my_host = QLineEdit("127.0.0.1")
        self.my_port = QSpinBox()
        self.my_port.setRange(1, 65535)
        self.my_port.setValue(3306)
        self.my_db = QLineEdit("dd_assets_tracking")
        self.my_user = QLineEdit()
        self.my_pass = QLineEdit()
        self.my_pass.setEchoMode(QLineEdit.Password)
        self.my_pass.setPlaceholderText("(kept in memory only)")

        show = QCheckBox("Show")
        show.toggled.connect(
            lambda on: self.my_pass.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))

        grid.addWidget(QLabel("Host"), 0, 0)
        grid.addWidget(self.my_host, 0, 1)
        grid.addWidget(QLabel("Port"), 0, 2)
        grid.addWidget(self.my_port, 0, 3)

        grid.addWidget(QLabel("Database"), 1, 0)
        grid.addWidget(self.my_db, 1, 1, 1, 3)

        grid.addWidget(QLabel("User"), 2, 0)
        grid.addWidget(self.my_user, 2, 1, 1, 3)

        grid.addWidget(QLabel("Password"), 3, 0)
        pass_row = QHBoxLayout()
        pass_row.addWidget(self.my_pass, 1)
        pass_row.addWidget(show)
        grid.addLayout(pass_row, 3, 1, 1, 3)

        self.my_remember = QCheckBox("Remember (system keyring)")
        if not _HAS_KEYRING:
            self.my_remember.setEnabled(False)
            self.my_remember.setToolTip(
                "Install the 'keyring' module to enable this option. "
                "Without it, no password is ever written to disk.")
        grid.addWidget(self.my_remember, 4, 1, 1, 3)

        btn_test = QPushButton("Test connection")
        btn_test.clicked.connect(self._on_test_connection)
        grid.addWidget(btn_test, 5, 1)

        self.my_status = QLabel("")
        self.my_status.setWordWrap(True)
        grid.addWidget(self.my_status, 5, 2, 1, 2)

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
        tip = QLabel("Hover: dependencies + artist · Wheel: zoom · "
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
        return {
            "host": self.my_host.text().strip(),
            "port": self.my_port.value(),
            "database": self.my_db.text().strip(),
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

    # ------------------------------------------------------- data loading --
    def _current_source(self):
        return ("mysql", "csv", "sql")[self.tabs.currentIndex()]

    def _source_signature(self):
        src = self._current_source()
        if src == "mysql":
            cfg = self._mysql_config()
            return ("mysql", cfg["host"], cfg["port"], cfg["database"],
                    cfg["user"], cfg["password"])
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
        self._maybe_store_password()

    def _error(self, message, title="Error"):
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, title, message)

    # --------------------------------------------------------- settings ----
    def _maybe_store_password(self):
        if _HAS_KEYRING and self.my_remember.isChecked():
            user = self.my_user.text().strip()
            pwd = self.my_pass.text()
            if user and pwd:
                try:
                    keyring.set_password(_KEYRING_SERVICE, user, pwd)
                except Exception:
                    pass  # jamais bloquant, jamais journalisé

    def _restore_settings(self):
        s = self._settings
        self.my_host.setText(s.value("mysql/host", "127.0.0.1"))
        self.my_port.setValue(int(s.value("mysql/port", 3306)))
        self.my_db.setText(s.value("mysql/database", "dd_assets_tracking"))
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
            self.my_remember.setChecked(True)
            user = self.my_user.text().strip()
            if user:
                try:
                    pwd = keyring.get_password(_KEYRING_SERVICE, user)
                    if pwd:
                        self.my_pass.setText(pwd)
                except Exception:
                    pass

    def _save_settings(self):
        s = self._settings
        s.setValue("mysql/host", self.my_host.text())
        s.setValue("mysql/port", self.my_port.value())
        s.setValue("mysql/database", self.my_db.text())
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
        # Le mot de passe n'est JAMAIS écrit dans QSettings.

    def closeEvent(self, event):
        self._save_settings()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Hydra — Dependency Graph")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
