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
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import data_source as ds
import graph_model as gm
from graph_view import (
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
        self.setWindowTitle("Hydra — Graphe de dépendances de scènes")
        self.resize(1280, 860)

        self._settings = QSettings("Hydra", "DependencyGraph")
        # Cache des données par signature de source (en mémoire uniquement).
        self._data_cache = {}

        self._build_ui()
        self._restore_settings()

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        root.addWidget(self._build_source_panel())
        root.addLayout(self._build_action_bar())

        self.view = DependencyGraphView()
        root.addWidget(self.view, 1)

        root.addWidget(self._build_legend())

        self._build_toolbar()
        self.statusBar().showMessage("Prêt.")

    def _build_source_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # En-tête repliable.
        self._toggle = QToolButton()
        self._toggle.setText("  Source de données")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(True)
        self._toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(Qt.DownArrow)
        self._toggle.setAutoRaise(True)
        self._toggle.toggled.connect(self._on_toggle_source)
        lay.addWidget(self._toggle, 0, Qt.AlignLeft)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_mysql_tab(), "phpMyAdmin (MySQL)")
        self.tabs.addTab(self._build_csv_tab(), "Fichiers CSV")
        self.tabs.addTab(self._build_sql_tab(), "Dump SQL")
        lay.addWidget(self.tabs)
        return panel

    def _on_toggle_source(self, checked):
        self.tabs.setVisible(checked)
        self._toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

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
        self.my_pass.setPlaceholderText("(en mémoire uniquement)")

        show = QCheckBox("Afficher")
        show.toggled.connect(
            lambda on: self.my_pass.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))

        grid.addWidget(QLabel("Hôte"), 0, 0)
        grid.addWidget(self.my_host, 0, 1)
        grid.addWidget(QLabel("Port"), 0, 2)
        grid.addWidget(self.my_port, 0, 3)

        grid.addWidget(QLabel("Base"), 1, 0)
        grid.addWidget(self.my_db, 1, 1, 1, 3)

        grid.addWidget(QLabel("Utilisateur"), 2, 0)
        grid.addWidget(self.my_user, 2, 1, 1, 3)

        grid.addWidget(QLabel("Mot de passe"), 3, 0)
        pass_row = QHBoxLayout()
        pass_row.addWidget(self.my_pass, 1)
        pass_row.addWidget(show)
        grid.addLayout(pass_row, 3, 1, 1, 3)

        self.my_remember = QCheckBox("Se souvenir (trousseau système)")
        if not _HAS_KEYRING:
            self.my_remember.setEnabled(False)
            self.my_remember.setToolTip(
                "Installez le module « keyring » pour activer cette option. "
                "Sans lui, aucun mot de passe n'est écrit sur disque.")
        grid.addWidget(self.my_remember, 4, 1, 1, 3)

        btn_test = QPushButton("Tester la connexion")
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
            btn = QPushButton("Parcourir…")
            btn.clicked.connect(
                lambda _=False, e=edit, lbl=label: self._browse_csv(e, lbl))
            grid.addWidget(btn, row, 2)

        folder_btn = QPushButton("Dossier… (détection auto)")
        folder_btn.clicked.connect(self._browse_csv_folder)
        grid.addWidget(folder_btn, 3, 1)

        hint = QLabel("Exports par table depuis phpMyAdmin "
                      "(Exporter → CSV, en-têtes en 1re ligne).")
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
        grid.addWidget(QLabel("Dump .sql"), 0, 0)
        grid.addWidget(self.sql_path, 0, 1)
        btn = QPushButton("Parcourir…")
        btn.clicked.connect(self._browse_sql)
        grid.addWidget(btn, 0, 2)

        hint = QLabel("Export mysqldump complet (phpMyAdmin → Exporter → SQL). "
                      "Les tables assets, scenes et binds sont extraites.")
        hint.setStyleSheet("color:#8a93a0;")
        hint.setWordWrap(True)
        grid.addWidget(hint, 1, 1, 1, 2)

        grid.setColumnStretch(1, 1)
        return w

    def _build_action_bar(self):
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Scène :"))
        self.scene_edit = QLineEdit()
        self.scene_edit.setPlaceholderText(_DEFAULT_SCENE)
        self.scene_edit.returnPressed.connect(self._on_grapher)
        bar.addWidget(self.scene_edit, 1)

        self.btn_graph = QPushButton("Grapher")
        self.btn_graph.setDefault(True)
        self.btn_graph.clicked.connect(self._on_grapher)
        bar.addWidget(self.btn_graph)
        return bar

    def _build_toolbar(self):
        tb = self.addToolBar("Vue")
        tb.setMovable(False)

        act_recenter = QAction("Recentrer", self)
        act_recenter.setShortcut(QKeySequence("Ctrl+0"))
        act_recenter.triggered.connect(lambda: self.view.reset_view())
        tb.addAction(act_recenter)

        act_reset = QAction("Réinitialiser la disposition", self)
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

        swatch(COL_OK_BORDER, "à jour")
        swatch(COL_STALE_BORDER, "périmé")
        swatch(COL_START_BORDER, "scène de départ", border=True)
        lay.addStretch(1)
        tip = QLabel("Survol : dépendances directes · Molette : zoom · "
                     "Bouton du milieu : déplacer · Glisser un nœud : "
                     "réordonner (horizontal)")
        tip.setStyleSheet("color:#8a93a0;")
        lay.addWidget(tip)
        return w

    # ------------------------------------------------------- file dialogs --
    def _browse_csv(self, edit, label):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Choisir {label}", edit.text() or "",
            "CSV (*.csv);;Tous les fichiers (*)")
        if path:
            edit.setText(path)

    def _browse_csv_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choisir un dossier contenant assets/scenes/binds.csv")
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
                "Fichiers non trouvés dans le dossier : "
                + ", ".join(f"{m}.csv" for m in missing))
        else:
            self.statusBar().showMessage("Trois fichiers CSV détectés.")

    def _browse_sql(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choisir un dump SQL", self.sql_path.text() or "",
            "SQL (*.sql);;Tous les fichiers (*)")
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
        self.my_status.setText("Test en cours…")
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
        self.statusBar().showMessage("Chargement des données…")
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
                    raise ds.DataSourceError("Aucun dump SQL sélectionné.")
                data = ds.load_from_sql_dump(path)
        finally:
            QApplication.restoreOverrideCursor()

        self._data_cache[sig] = data
        return data

    # ---------------------------------------------------------- Grapher ----
    def _on_grapher(self):
        scene_name = self.scene_edit.text().strip()
        if not scene_name:
            self._error("Saisissez un nom de scène (ex. "
                        f"« {_DEFAULT_SCENE} »).")
            return

        try:
            assets, scenes, binds = self._load_data()
        except ds.DataSourceError as exc:
            self._error(str(exc), title="Erreur de chargement")
            return
        except Exception as exc:  # robustesse
            self._error(f"Erreur inattendue au chargement : {exc}",
                        title="Erreur")
            return

        try:
            result = gm.build_graph(assets, scenes, binds, scene_name)
        except gm.SceneResolutionError as exc:
            self._error(str(exc), title="Scène introuvable")
            return
        except Exception as exc:  # robustesse
            self._error(f"Erreur lors du calcul du graphe : {exc}",
                        title="Erreur")
            return

        self.view.set_graph(result)
        stats = result.stats
        self.statusBar().showMessage(
            f"« {scene_name} » : {stats['nodes']} scènes, "
            f"{stats['edges']} liens "
            f"({len(assets)} assets, {len(scenes)} scènes en base).")
        self._maybe_store_password()

    def _error(self, message, title="Erreur"):
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
    app.setApplicationName("Hydra — Graphe de dépendances")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
