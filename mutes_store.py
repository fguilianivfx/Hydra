"""Persistance des liens muted : bac à sable de test, ou base du studio.

La base enregistre les assets « muted » par tâche, à travers
``dd.utils.assets_mutes`` fourni par Kraken. Le module offre DEUX couches
pour un outil interactif ; Dedale préfère la première et retombe sur la
seconde quand elle n'est pas utilisable.

COUCHE « FROM ROWS » (préférée) — l'outil fournit lui-même les lignes des
tables de suivi, ce que fait Dedale, qui les a déjà toutes en mémoire :

    get_task_mutes(scene_task_id)        -> list
    is_asset_muted(asset, entries)       -> bool
    mute_asset_row(scene_task_id, asset) -> bool
    unmute_asset_row(scene_task_id, asset) -> bool

Deux gains décisifs sur la couche par chemins :

* **aucune résolution de chemin**, donc plus aucune ambiguïté possible. Un
  dossier partagé par deux assets (un ``.hda`` de bibliothèque et
  l'opérateur importé) faisait renoncer le module, faute de savoir lequel
  viser ; la ligne, elle, ne se confond avec rien ;
* **la version est celle de la ligne**, pas celle devinée du nom de
  fichier. Un ``.hda`` dont le nom ne porte pas de version était muté pour
  TOUTES les versions (repli ``ALL_VERSIONS``) : ici la version importée
  est mutée, et elle seule.

Les valeurs passées au module sont celles de la base, **non normalisées**
(voir ``data_source.mute_row``) : le module lit la même table ``assets`` que
nous, la correspondance est donc exacte par construction, quelle que soit la
convention d'écriture des colonnes — le point de « .hda » compris. C'est
pourquoi la valeur normalisée par l'outil (« hda ») ne doit jamais sortir
d'ici : elle ne correspondrait à aucune ligne.

COUCHE PAR CHEMINS (repli) — quand le module installé n'expose pas la
première, ou quand la source chargée ne donne ni ``scenes.task_id`` ni
l'``extension`` des assets (un CSV réduit, par exemple) :

    get_scene_mutes(scene_path)                               -> list
    is_asset_path_muted(scene_path, asset_path, entries=None) -> bool
    mute_asset(scene_path, asset_path, all_versions=False)    -> bool
    unmute_asset(scene_path, asset_path)                      -> bool
    unmute_scene(scene_path)                                  -> bool

Tout s'y parle en chemins bruts : pas d'id à chercher, pas de casse à
gérer — la normalisation est faite dans ces fonctions.

Ce que les deux couches partagent, et dont Dedale dépend :

* après chaque écriture, la lecture est refaite pour que l'interface
  reflète la base et non l'intention du clic (consigne du développeur) ;
* les entrées rendues sont des n-uplets **opaques** : jamais inspectées
  ici, seulement repassées au test d'appartenance. C'est ce qui a rendu
  indolore leur passage de 2 à 3 valeurs ;
* la lecture préchauffe le cache de résolution du module pour toute la
  tâche : une lecture par tâche, puis des vérifications en mémoire — d'où
  le cache ci-dessous ;
* ``clear_resolution_caches`` (facultative) oublie ces résolutions ; Dedale
  l'appelle à chaque nouveau graphe, au cas où la base aurait été corrigée.

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
import inspect
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
# Cible d'écriture par défaut : la base du studio, qui sait désormais muter
# une version d'asset pour une scène. Le bac à sable reste accessible par
# « -sb 1 » (ou DEDALE_MUTES_TARGET=sandbox) pour rejouer un essai.
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


# Les deux couches du module, par leurs fonctions. La première l'emporte dès
# que le module installé les expose toutes les quatre.
ROW_FUNCTIONS = ("get_task_mutes", "is_asset_muted", "mute_asset_row",
                 "unmute_asset_row")
PATH_FUNCTIONS = ("get_scene_mutes", "is_asset_path_muted", "mute_asset",
                  "unmute_asset", "unmute_scene")


def _takes_scope(fn):
    """Vrai si ``is_asset_muted`` attend la tâche avant l'asset.

    La signature documentée est ``is_asset_muted(asset, entries)``. Le
    module vit sur le poste du studio et peut évoluer : plutôt que de figer
    une variante, on lit la signature réellement installée. Trois
    paramètres positionnels ou plus = la tâche est attendue en tête.
    """
    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):    # pragma: no cover - builtin/C
        return False
    return len(params) >= 3


class MuteScope:
    """Ce à quoi un mute est rattaché : une tâche, ou un chemin de scène.

    ``layer`` vaut ``"rows"`` (clé = ``scenes.task_id``) ou ``"paths"``
    (clé = chemin du fichier scène). L'objet sert aussi de clé de cache :
    deux versions d'une même scène partagent leur tâche, donc une seule
    lecture pour toutes.
    """

    __slots__ = ("layer", "key")

    def __init__(self, layer, key):
        self.layer = layer
        self.key = key

    def __bool__(self):
        """Faux quand rien n'identifie la cible : rien n'est enregistrable."""
        return self.key is not None and self.key != ""

    def __eq__(self, other):
        return (isinstance(other, MuteScope) and self.layer == other.layer
                and self.key == other.key)

    def __hash__(self):
        return hash((self.layer, self.key))

    def __repr__(self):                # pragma: no cover - confort de debug
        return f"MuteScope({self.layer!r}, {self.key!r})"


class SandboxMutes:
    """Bac à sable : même contrat que le module, dans un fichier JSON.

    Sert à essayer le mécanisme sans écrire dans la base du studio. Les deux
    couches y sont reproduites : « FROM ROWS » (clé = tâche, entrée =
    identité de la ligne asset) et par chemins. La normalisation imite
    l'esprit du module (tâche = dossier du fichier scène, chemins
    insensibles à la casse et au sens des séparateurs) mais reste **une
    approximation** : elle valide le fonctionnement de l'outil, pas les
    règles de correspondance internes de Kraken.
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

    # --- couche « FROM ROWS » ----------------------------------------------
    # L'identité d'une ligne asset = MUTE_FIELDS + la version, telle que le
    # module la lit. Elle est sérialisée en texte pour tenir dans le JSON.
    _ROW_KEYS = ("project", "entity_name", "task_name", "av_name",
                 "node_name", "extension", "version")

    @classmethod
    def _row_key(cls, asset):
        return "|".join(str(asset.get(k, "")) for k in cls._ROW_KEYS)

    @staticmethod
    def _rows_task(task_id):
        """Clé de tâche distincte de celle des chemins (pas de collision)."""
        return f"task:{task_id}"

    def get_task_mutes(self, task_id):
        return list(self._load().get(self._rows_task(task_id), []))

    def is_asset_muted(self, asset, entries):
        return self._row_key(asset) in set(entries or ())

    def mute_asset_row(self, task_id, asset):
        data = self._load()
        task, entry = self._rows_task(task_id), self._row_key(asset)
        entries = data.setdefault(task, [])
        if entry in entries:
            return False
        entries.append(entry)
        self._save(data)
        return True

    def unmute_asset_row(self, task_id, asset):
        data = self._load()
        task, entry = self._rows_task(task_id), self._row_key(asset)
        if entry not in data.get(task, []):
            return False
        data[task].remove(entry)
        if not data[task]:
            del data[task]
        self._save(data)
        return True


class MutesStore:
    """Cache + garde-fous autour des fonctions autorisées du module."""

    def __init__(self, target=None, sandbox_file=None):
        self._target = (target or default_target()).lower()
        self._sandbox_file = sandbox_file or default_sandbox_file()
        self._api = None
        self._mode = ""          # "sandbox" | "kraken" | "" (indisponible)
        self._error = ""
        self._entries = {}       # MuteScope -> entrées rendues par la lecture
        self._clear_caches = None
        self._rows = False       # couche « FROM ROWS » disponible
        self._scoped_test = False  # is_asset_muted attend-elle la tâche ?
        if self._target == "db":
            self._use_kraken()
        else:
            self._use_sandbox()

    # -------------------------------------------------------- backends -----
    def _use_sandbox(self):
        sandbox = SandboxMutes(self._sandbox_file)
        self._api = {name: getattr(sandbox, name)
                     for name in PATH_FUNCTIONS + ROW_FUNCTIONS}
        self._rows = True
        self._mode = "sandbox"

    def _use_kraken(self):
        path = kraken_path()
        try:
            if os.path.isdir(path) and path not in sys.path:
                sys.path.insert(0, path)
            from dd.utils import assets_mutes as api
            # On ne référence QUE les fonctions des deux couches interactives :
            # le reste du module appartient à la tâche cron (voir l'en-tête).
            self._api = {name: getattr(api, name) for name in PATH_FUNCTIONS}
            # Couche « FROM ROWS » : préférée, mais seulement si le module
            # installé l'expose EN ENTIER. Une installation plus ancienne
            # continue de fonctionner par les chemins, sans rien casser.
            if all(callable(getattr(api, name, None)) for name in ROW_FUNCTIONS):
                self._api.update({name: getattr(api, name)
                                  for name in ROW_FUNCTIONS})
                self._scoped_test = _takes_scope(self._api["is_asset_muted"])
                self._rows = True
            # Facultatif : le module mémorise les résolutions chemin -> tâche
            # et chemin -> asset. Ce sont des faits immuables, mais ils
            # cessent de l'être quand la base est corrigée sous nos pieds :
            # on les purge à chaque nouveau graphe.
            self._clear_caches = getattr(api, "clear_resolution_caches", None)
            self._mode = "kraken"
        except Exception as exc:      # module absent, cassé ou incomplet
            self._api = None
            self._mode = ""
            self._rows = False
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
        if self.writes_to_db:
            return "DB"
        if self._mode == "sandbox":
            return "test file"
        # Aucune cible : les mutes ne vivront que le temps de la session.
        return "memory"

    @property
    def rows_supported(self):
        """Vrai si la couche « FROM ROWS » du module est disponible ici."""
        return self._rows

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

    # -------------------------------------------------------- périmètre ----
    def scope(self, task_id=None, scene_path=""):
        """Périmètre d'écriture pour une scène : « rows » si possible.

        La tâche l'emporte sur le chemin dès que les deux sont connus : elle
        évite au module toute résolution, donc toute ambiguïté. Sans tâche
        (module ancien, ou source sans colonne ``task_id``), le chemin
        reprend la main ; sans l'un ni l'autre, le périmètre est faux et
        rien n'est enregistrable.
        """
        if self._rows and task_id is not None:
            return MuteScope("rows", task_id)
        return MuteScope("paths", scene_path or "")

    # ------------------------------------------------------------- lecture --
    def entries(self, scope, refresh=False):
        """Mutes enregistrés pour ce périmètre (relus si ``refresh``).

        Une lecture par tâche : toutes les versions d'une même scène la
        partagent, et elle préchauffe le cache de résolution du module.
        """
        if refresh or scope not in self._entries:
            if scope.layer == "rows":
                got = self._api["get_task_mutes"](scope.key)
            else:
                got = self._api["get_scene_mutes"](scope.key)
            self._entries[scope] = list(got) if got else []
        return self._entries[scope]

    def is_muted(self, scope, target, entries=None):
        """Vrai si cet asset est muted dans ce périmètre.

        ``target`` est la **ligne brute** de la table ``assets`` sur la
        couche « rows », et le chemin publié sur la couche par chemins.
        """
        if entries is None:
            entries = self.entries(scope)
        if scope.layer == "rows":
            test = self._api["is_asset_muted"]
            if self._scoped_test:
                return bool(test(scope.key, target, entries))
            return bool(test(target, entries))
        return bool(self._api["is_asset_path_muted"](
            scope.key, target, entries))

    def clear_cache(self):
        """Oublie nos entrées **et** les résolutions mémorisées du module."""
        self._entries.clear()
        if self._clear_caches is not None:
            try:
                self._clear_caches()
            except Exception:      # purement une optimisation : jamais bloquant
                logging.getLogger(__name__).debug(
                    "clear_resolution_caches a échoué", exc_info=True)

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

    def mute(self, scope, target):
        """Mute un asset dans ce périmètre (ligne brute, ou chemin publié)."""
        if scope.layer == "rows":
            return bool(self._api["mute_asset_row"](scope.key, target))
        return bool(self._api["mute_asset"](scope.key, target))

    def unmute(self, scope, target):
        if scope.layer == "rows":
            return bool(self._api["unmute_asset_row"](scope.key, target))
        return bool(self._api["unmute_asset"](scope.key, target))

    def unmute_scene(self, scene_path):
        """Tout réafficher pour la tâche de cette scène (pas branché en UI)."""
        return bool(self._api["unmute_scene"](scene_path))
