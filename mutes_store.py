"""Persistance des liens muted via le module studio (Kraken).

La base enregistre désormais les assets « muted » par tâche, à travers
``dd.utils.assets_mutes`` fourni par Kraken. Dedale n'utilise que les CINQ
fonctions prévues pour un outil interactif :

    get_scene_mutes(scene_path)                              -> list
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

Le module est cherché dans ``$DEDALE_KRAKEN_PATH`` puis dans l'installation
Kraken par défaut. Absent, Dedale retombe sur les mutes en mémoire
(comportement historique) : rien d'autre ne change.
"""

import os
import sys

# Installation Kraken par défaut (poste graphiste Windows).
DEFAULT_KRAKEN_PATH = "C:/Program Files/Kraken"


def kraken_path():
    """Dossier où chercher le module (surchargeable par l'environnement)."""
    return os.environ.get("DEDALE_KRAKEN_PATH") or DEFAULT_KRAKEN_PATH


class MutesStore:
    """Cache + garde-fous autour des cinq fonctions autorisées du module."""

    def __init__(self):
        self._api = None
        self._error = ""
        self._entries = {}   # scene_path -> liste rendue par get_scene_mutes
        self._import_api()

    # -------------------------------------------------------------- import --
    def _import_api(self):
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
        except Exception as exc:      # module absent, cassé ou incomplet
            self._api = None
            self._error = str(exc) or exc.__class__.__name__

    @property
    def available(self):
        """Vrai si le module studio est chargé : les mutes sont persistants."""
        return self._api is not None

    @property
    def error(self):
        """Raison de l'indisponibilité ('' si le module est chargé)."""
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
    def mute(self, scene_path, asset_path):
        return bool(self._api["mute_asset"](scene_path, asset_path))

    def unmute(self, scene_path, asset_path):
        return bool(self._api["unmute_asset"](scene_path, asset_path))

    def unmute_scene(self, scene_path):
        """Tout réafficher pour la tâche de cette scène (pas branché en UI)."""
        return bool(self._api["unmute_scene"](scene_path))
