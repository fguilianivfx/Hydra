"""Persistance des liens muted : bac à sable de test, ou base du studio.

La base enregistre les assets « muted » par tâche, à travers
``dd.utils.assets_mutes`` fourni par Kraken. Dedale n'utilise que les CINQ
fonctions prévues pour un outil interactif :

    get_scene_mutes(scene_path)                               -> list
    is_asset_path_muted(scene_path, asset_path, entries=None) -> bool
    mute_asset(scene_path, asset_path)                        -> bool
    unmute_asset(scene_path, asset_path)                      -> bool
    unmute_scene(scene_path)                                  -> bool

Tout se parle en chemins bruts (fichier de la scène ouverte, fichier publié
de l'asset) : pas d'id à chercher, pas de casse à gérer, pas de séquence à
reconstruire — la normalisation est faite dans ces fonctions. Après chaque
écriture, ``get_scene_mutes`` est relu pour que l'interface reflète la base
(consigne du développeur du module).

NE JAMAIS appeler les autres fonctions du module : ``mute_family``,
``load_all_mutes``, ``load_changed_mutes``, ``filter_muted_assets``,
``mutes_of_task``, ``delete_inactive_mutes``. C'est la couche de la tâche
cron qui rafraîchit les statuts (OK/NAN/NT…) toutes les 10 minutes côté
serveur ; appelées depuis un outil, elles feraient une requête par ligne
affichée, ou écriraient un mute avec la mauvaise tâche.

DEUX CIBLES D'ÉCRITURE, choisies par ``DEDALE_MUTES_TARGET`` :

* ``db`` (**défaut**) — écriture réelle dans la base du studio, via le
  module Kraken. C'est le mode nominal : un mute posé ici est vu par les
  autres outils et retrouvé à la session suivante, depuis n'importe quel
  poste ;
* ``sandbox`` — les mutes vont dans un simple fichier JSON local et le
  module Kraken n'est pas appelé du tout : **la base n'est pas touchée**.
  Reste disponible pour rejouer un essai de bout en bout (mute, relecture,
  restauration) sans conséquence, y compris sur un poste sans Kraken.

Le fichier du bac à sable est ``DEDALE_MUTES_SANDBOX_FILE`` s'il est défini,
sinon ``~/.dedale/mutes_sandbox.json`` ; le supprimer remet l'essai à zéro.
"""

import contextlib
import json
import logging
import os
import sys

# Le module journalise ses refus (« 2 different assets share the folder … »)
# au lieu de lever : on écoute son logger pour porter la raison jusqu'à
# l'interface, au lieu de la laisser dans une console.
MODULE_LOGGER = "dd.utils.assets_mutes"


class _MessageCatcher(logging.Handler):
    """Retient les avertissements/erreurs émis pendant une écriture."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        try:
            text = record.getMessage()
        except Exception:                      # pragma: no cover - défensif
            return
        if text not in self.messages:
            self.messages.append(text)

# Installation Kraken par défaut (poste graphiste Windows).
DEFAULT_KRAKEN_PATH = "C:/Program Files/Kraken"
# Cible d'écriture par défaut : la base du studio (mode nominal). Le bac à
# sable reste accessible par DEDALE_MUTES_TARGET=sandbox pour un essai.
DEFAULT_TARGET = "db"


def kraken_path():
    """Dossier où chercher le module (surchargeable par l'environnement)."""
    return os.environ.get("DEDALE_KRAKEN_PATH") or DEFAULT_KRAKEN_PATH


def default_target():
    """Cible d'écriture demandée : « db » (défaut) ou « sandbox »."""
    value = (os.environ.get("DEDALE_MUTES_TARGET") or DEFAULT_TARGET).strip()
    return value.lower() or DEFAULT_TARGET


def default_sandbox_file():
    """Fichier JSON du bac à sable (surchargeable par l'environnement)."""
    return (os.environ.get("DEDALE_MUTES_SANDBOX_FILE")
            or os.path.join(os.path.expanduser("~"), ".dedale",
                            "mutes_sandbox.json"))


class SandboxMutes:
    """Bac à sable : même contrat que le module, dans un fichier JSON.

    Sert à essayer le mécanisme sans écrire dans la base du studio. La
    normalisation imite l'esprit du module (tâche = dossier du fichier
    scène, chemins insensibles à la casse et au sens des séparateurs) mais
    reste **une approximation** : elle valide le fonctionnement de l'outil,
    pas les règles de correspondance internes de Kraken.
    """

    def __init__(self, path):
        self.path = path

    # --- stockage ----------------------------------------------------------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data):
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)

    @staticmethod
    def _norm(path):
        return str(path or "").replace("\\", "/").strip().lower()

    def _task_key(self, scene_path):
        """Toutes les versions d'une scène partagent leur dossier = la tâche."""
        return os.path.dirname(self._norm(scene_path))

    # --- contrat (les cinq mêmes fonctions) --------------------------------
    def get_scene_mutes(self, scene_path):
        return list(self._load().get(self._task_key(scene_path), []))

    def is_asset_path_muted(self, scene_path, asset_path, entries=None):
        if entries is None:
            entries = self.get_scene_mutes(scene_path)
        return self._norm(asset_path) in {self._norm(e) for e in entries}

    def mute_asset(self, scene_path, asset_path):
        data = self._load()
        task, entry = self._task_key(scene_path), self._norm(asset_path)
        entries = data.setdefault(task, [])
        if entry in entries:
            return False
        entries.append(entry)
        self._save(data)
        return True

    def unmute_asset(self, scene_path, asset_path):
        data = self._load()
        task, entry = self._task_key(scene_path), self._norm(asset_path)
        if entry not in data.get(task, []):
            return False
        data[task].remove(entry)
        if not data[task]:
            del data[task]
        self._save(data)
        return True

    def unmute_scene(self, scene_path):
        data = self._load()
        task = self._task_key(scene_path)
        if task not in data:
            return False
        del data[task]
        self._save(data)
        return True


class MutesStore:
    """Cache + garde-fous autour des cinq fonctions autorisées."""

    def __init__(self, target=None, sandbox_file=None):
        self._target = (target or default_target()).lower()
        self._sandbox_file = sandbox_file or default_sandbox_file()
        self._api = None
        self._mode = ""          # "sandbox" | "kraken" | "" (indisponible)
        self._error = ""
        self._entries = {}       # scene_path -> liste rendue par get_scene_mutes
        if self._target == "db":
            self._use_kraken()
        else:
            self._use_sandbox()

    # -------------------------------------------------------- backends -----
    def _use_sandbox(self):
        sandbox = SandboxMutes(self._sandbox_file)
        self._api = {name: getattr(sandbox, name) for name in (
            "get_scene_mutes", "is_asset_path_muted", "mute_asset",
            "unmute_asset", "unmute_scene")}
        self._mode = "sandbox"

    def _use_kraken(self):
        path = kraken_path()
        try:
            if os.path.isdir(path) and path not in sys.path:
                sys.path.insert(0, path)
            from dd.utils import assets_mutes as api
            # On ne référence QUE les cinq fonctions autorisées : le reste du
            # module appartient à la tâche cron (voir l'en-tête du fichier).
            self._api = {
                "get_scene_mutes": api.get_scene_mutes,
                "is_asset_path_muted": api.is_asset_path_muted,
                "mute_asset": api.mute_asset,
                "unmute_asset": api.unmute_asset,
                "unmute_scene": api.unmute_scene,
            }
            self._mode = "kraken"
        except Exception as exc:      # module absent, cassé ou incomplet
            self._api = None
            self._mode = ""
            self._error = str(exc) or exc.__class__.__name__

    # ----------------------------------------------------------- état ------
    @property
    def available(self):
        """Vrai si les mutes sont persistants (bac à sable ou base)."""
        return self._api is not None

    @property
    def mode(self):
        """« sandbox », « kraken », ou '' si rien n'est disponible."""
        return self._mode

    @property
    def writes_to_db(self):
        """Vrai seulement quand les écritures partent vers la base du studio."""
        return self._mode == "kraken"

    @property
    def label(self):
        """Nom court de la cible, pour les messages et les info-bulles."""
        return "DB" if self.writes_to_db else "test file"

    @property
    def sandbox_file(self):
        return self._sandbox_file

    @property
    def error(self):
        """Raison de l'indisponibilité ('' si une cible est active)."""
        return "" if self.available else self._error

    def expected(self):
        """Vrai si une installation Kraken semble présente sur ce poste."""
        return os.path.isdir(kraken_path())

    # ------------------------------------------------------------- lecture --
    def entries(self, scene_path, refresh=False):
        """Mutes de la tâche de cette scène (relus si ``refresh``)."""
        if refresh or scene_path not in self._entries:
            got = self._api["get_scene_mutes"](scene_path)
            self._entries[scene_path] = list(got) if got else []
        return self._entries[scene_path]

    def is_muted(self, scene_path, asset_path, entries=None):
        """Vrai si cet asset est muted pour la tâche de cette scène."""
        if entries is None:
            entries = self.entries(scene_path)
        return bool(self._api["is_asset_path_muted"](
            scene_path, asset_path, entries))

    def clear_cache(self):
        self._entries.clear()

    # ------------------------------------------------------------ écriture --
    @contextlib.contextmanager
    def capture_messages(self):
        """Recueille ce que le module journalise pendant les écritures.

        Un refus (« 2 different assets share the folder … ») n'est ni une
        exception ni toujours visible dans le retour : c'est une ligne de
        log. On la remonte pour l'afficher au lieu de la perdre.
        """
        catcher = _MessageCatcher()
        logger = logging.getLogger(MODULE_LOGGER)
        logger.addHandler(catcher)
        try:
            yield catcher.messages
        finally:
            logger.removeHandler(catcher)

    def mute(self, scene_path, asset_path):
        return bool(self._api["mute_asset"](scene_path, asset_path))

    def unmute(self, scene_path, asset_path):
        return bool(self._api["unmute_asset"](scene_path, asset_path))

    def unmute_scene(self, scene_path):
        """Tout réafficher pour la tâche de cette scène (pas branché en UI)."""
        return bool(self._api["unmute_scene"](scene_path))
