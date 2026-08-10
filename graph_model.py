"""Modèle du graphe de dépendances (indépendant de l'UI et de la source).

Responsabilités :
  * résoudre le nom de scène saisi vers une scène de départ S0 ;
  * parcourir récursivement les dépendances au niveau *asset* via
    scenes + binds (cycle-safe) ;
  * regrouper les assets par scène productrice
    (project, entity_name, task_name, av_name, version) ;
  * construire les arêtes scène -> scène ;
  * calculer le statut (à jour / périmé) et la couleur de chaque nœud ;
  * assigner une position (ligne de tâche + colonne) à chaque nœud.

IMPORTANT : la table ``assets_parents`` est volontairement ignorée
(cache partiel et incohérent). Toute la récursion passe par scenes+binds.
"""

from __future__ import annotations

import re
from collections import defaultdict


class SceneResolutionError(Exception):
    """Le nom de scène saisi n'a pu être résolu vers une scène connue."""


# Ordre des lignes de tâche, du haut (sources) vers le bas (compositing).
# Ordre par défaut des lignes de tâche (haut -> bas), en deux niveaux
# séparés par un trait horizontal.
ASSET_TASKS = ["modeling", "shading", "rigging"]
SHOT_TASKS = ["tracking", "layout", "animation", "fx", "lighting", "compositing"]

# Étiquette affichée à gauche de chaque ligne (reprend les noms courts).
_ROW_LABEL = {
    "modeling": "modeling", "shading": "shading", "rigging": "rig",
    "tracking": "tracking", "layout": "layout", "animation": "anim",
    "fx": "fx", "lighting": "lighting", "compositing": "compositing",
}

# Alias nom de tâche -> tâche canonique, pour le classement des lignes.
_LAYOUT_ALIASES = {
    "model": "modeling", "modeling": "modeling", "modelling": "modeling",
    "mod": "modeling",
    "shading": "shading", "shade": "shading", "lookdev": "shading",
    "look": "shading", "surfacing": "shading", "texturing": "shading",
    "rig": "rigging", "rigging": "rigging",
    "track": "tracking", "tracking": "tracking", "matchmove": "tracking",
    "mm": "tracking",
    "layout": "layout", "lay": "layout",
    "anim": "animation", "animation": "animation",
    "fx": "fx", "effects": "fx", "simulation": "fx", "simu": "fx",
    "sim": "fx", "cfx": "fx",
    "light": "lighting", "lighting": "lighting", "lgt": "lighting",
    "comp": "compositing", "compositing": "compositing",
}

# Rangs de base, avec de la marge pour intercaler les tâches inconnues.
_RANK_STEP = 100
_ASSET_RANK = {t: (i + 1) * _RANK_STEP for i, t in enumerate(ASSET_TASKS)}
_SHOT_RANK = {t: (len(ASSET_TASKS) + 1 + i + 1) * _RANK_STEP
              for i, t in enumerate(SHOT_TASKS)}
_BASE_RANK = {**_ASSET_RANK, **_SHOT_RANK}

# Alias de tâches utilisés pour la RÉSOLUTION du nom de scène (comp<->…).
_TASK_ALIASES = {
    "compositing": {"comp"},
    "animation": {"anim"},
    "lighting": {"light", "lgt"},
    "modeling": {"model", "mod"},
    "tracking": {"track", "matchmove", "mm"},
    "layout": {"lay"},
    "shading": {"shade", "lookdev", "look"},
    "rigging": {"rig"},
    "fx": {"effects", "simulation", "simu", "sim", "cfx"},
}


def task_display(task_name):
    """Nom court d'une tâche pour l'affichage (compositing -> comp)."""
    return "comp" if task_name == "compositing" else task_name


def canon_task(task_name):
    """Tâche canonique (rig -> rigging, anim -> animation, …)."""
    key = (task_name or "").strip().lower()
    return _LAYOUT_ALIASES.get(key, key)


def task_level(task_name):
    """Niveau d'une tâche : 'asset', 'shot' ou 'other'."""
    c = canon_task(task_name)
    if c in _ASSET_RANK:
        return "asset"
    if c in _SHOT_RANK:
        return "shot"
    return "other"


def is_active_bind(value):
    """Vrai si un bind est actif (active dans ('1', 1))."""
    if value in (1, "1"):
        return True
    return isinstance(value, str) and value.strip() == "1"


# Alias interne historique.
_active = is_active_bind


# ---------------------------------------------------------------------------
# Nœud de graphe et résultat
# ---------------------------------------------------------------------------

class SceneNode:
    """Un rectangle du graphe : une scène (une version) et ses outputs."""

    __slots__ = (
        "key", "project", "entity_name", "task_name", "av_name", "version",
        "node_names", "outputs", "inputs", "artists", "scene_names",
        "is_start", "status", "display_name", "row", "col",
    )

    def __init__(self, key, project, entity_name, task_name, av_name, version):
        self.key = key
        self.project = project
        self.entity_name = entity_name
        self.task_name = task_name
        self.av_name = av_name
        self.version = version
        self.node_names = []       # noms des outputs (node_name), triés
        self.outputs = []          # list[(name, latest_version)] triée par name
        self.inputs = []           # list[(label, version, latest_version)]
        self.artists = []          # graphistes ayant publié cette scène
        self.scene_names = []      # noms bruts des lignes scenes (pour survol)
        self.is_start = False
        # "ok" (vert) | "inherited" (orange) | "stale" (rouge)
        self.status = "ok"
        self.display_name = ""
        self.row = 0
        self.col = 0


class GraphResult:
    """Résultat complet prêt à afficher."""

    __slots__ = ("nodes", "edges", "start_key", "row_tasks", "row_levels",
                 "separator_after_row", "stats",
                 "edge_assets", "edge_details", "stale_asset_ids",
                 "stale_edges")

    def __init__(self):
        self.nodes = {}            # key -> SceneNode
        self.edges = []            # list[(top_key, bottom_key)] (parent -> enfant)
        self.start_key = None
        self.row_tasks = {}        # row_index -> étiquette de tâche
        self.row_levels = {}       # row_index -> 'asset' | 'shot' | 'other'
        self.separator_after_row = None  # ligne après laquelle placer le trait
        self.stats = {}
        # Données par arête : assets qui transitent par le lien, leur détail
        # d'affichage, et l'ensemble des assets périmés (pour recalculer les
        # statuts quand des liens sont désactivés).
        self.edge_assets = {}      # (top, bottom) -> frozenset[asset_id]
        self.edge_details = {}     # (top, bottom) -> [(label, version, latest)]
        self.stale_asset_ids = frozenset()
        # Liens dont au moins un asset transporté est supplanté.
        self.stale_edges = frozenset()


# ---------------------------------------------------------------------------
# Indexation en mémoire (une seule passe, pas de requête par nœud)
# ---------------------------------------------------------------------------

def _build_indexes(assets, scenes, binds):
    # scene_id -> set des asset_id des binds ACTIFS de cette scène
    scene_active_assets = defaultdict(set)
    for asset_id, scene_id, active in binds:
        if _active(active):
            scene_active_assets[scene_id].add(asset_id)

    # (project, entity, task, av, version) -> liste des scene_id (productrices)
    scenes_by_identity_version = defaultdict(list)
    # (project, entity, task, av) -> version max (flux de la scène)
    scene_stream_max = {}
    for scene_id, s in scenes.items():
        idv = (s["project"], s["entity_name"], s["task_name"],
               s["av_name"], s["version"])
        scenes_by_identity_version[idv].append(scene_id)
        stream = (s["project"], s["entity_name"], s["task_name"], s["av_name"])
        v = s["version"]
        if v is not None and (stream not in scene_stream_max
                              or v > scene_stream_max[stream]):
            scene_stream_max[stream] = v

    # (project, entity, task, av, node_name) -> version max (flux de l'output)
    asset_stream_max = {}
    for a in assets.values():
        stream = (a["project"], a["entity_name"], a["task_name"],
                  a["av_name"], a["node_name"])
        v = a["version"]
        if v is not None and (stream not in asset_stream_max
                              or v > asset_stream_max[stream]):
            asset_stream_max[stream] = v

    return (scene_active_assets, scenes_by_identity_version,
            scene_stream_max, asset_stream_max)


def _asset_scene_key(a):
    """Identité+version de la scène productrice d'un asset."""
    return (a["project"], a["entity_name"], a["task_name"],
            a["av_name"], a["version"])


# ---------------------------------------------------------------------------
# Résolution du nom de scène
# ---------------------------------------------------------------------------

def resolve_scene(input_name, scenes):
    """Résout ``input_name`` vers (start_key, scene_ids, prefix).

    Le nom est de la forme ``{prefix}_{entity}_{task}[_{av}]_v{NNN}``.
    On ne matche PAS sur ``scenes.name`` (sale) : on reconstruit les noms
    canoniques à partir des colonnes structurées et on compare.
    """
    raw = (input_name or "").strip()
    if not raw:
        raise SceneResolutionError("No scene name entered.")

    m = re.search(r"_v0*(\d+)\s*$", raw, re.IGNORECASE)
    if not m:
        raise SceneResolutionError(
            "Invalid name: a trailing version is expected "
            "(e.g. \"qua_077_02000_comp_v019\")."
        )
    version = int(m.group(1))
    stem = raw[: m.start()]
    parts = stem.split("_")
    if len(parts) < 2:
        raise SceneResolutionError(
            "Invalid name: a prefix and an entity are expected "
            "(e.g. \"qua_077_02000_comp_v019\")."
        )
    prefix = parts[0]
    core = "_".join(parts[1:]).lower()   # entity_task[_av]

    # 1) Correspondance canonique (robuste : entité avec underscores, av
    #    optionnel, alias de tâche) sur les scènes de la bonne version.
    matches = []
    for scene_id, s in scenes.items():
        if s["version"] != version:
            continue
        for variant in _core_variants(s):
            if variant == core:
                matches.append(scene_id)
                break

    if not matches:
        # 2) Dernier recours : sous-chaîne sur le nom (colonne sale) parmi
        #    les scènes de la bonne version.
        matches = _fallback_by_name(scenes, version, prefix, stem)

    if not matches:
        raise SceneResolutionError(
            f"Scene not found for \"{raw}\". "
            "Check the entity, task, av and version."
        )

    # Toutes les correspondances partagent normalement la même identité ;
    # on retient l'identité la plus représentée, puis toutes ses lignes.
    key_counts = defaultdict(list)
    for scene_id in matches:
        s = scenes[scene_id]
        idv = (s["project"], s["entity_name"], s["task_name"],
               s["av_name"], s["version"])
        key_counts[idv].append(scene_id)
    best_key = max(key_counts, key=lambda k: len(key_counts[k]))

    # On regroupe TOUTES les lignes scenes de cette identité+version (dédup).
    scene_ids = [sid for sid, s in scenes.items()
                 if (s["project"], s["entity_name"], s["task_name"],
                     s["av_name"], s["version"]) == best_key]
    return best_key, scene_ids, prefix


def _core_variants(scene):
    """Formes ``entity_task[_av]`` possibles d'une scène (avec alias)."""
    entity = scene["entity_name"]
    task = scene["task_name"]
    av = scene["av_name"]
    tasks = {task, task_display(task)}
    tasks |= _TASK_ALIASES.get(task, set())
    variants = set()
    for t in tasks:
        base = f"{entity}_{t}"
        if av:
            variants.add(f"{base}_{av}".lower())
        else:
            variants.add(base.lower())
    return variants


def _fallback_by_name(scenes, version, prefix, stem):
    """Filtre par sous-chaîne sur ``scenes.name`` (dernier recours)."""
    needle = stem.lower()
    short = "_".join(stem.split("_")[1:]).lower()  # sans le préfixe
    out = []
    for scene_id, s in scenes.items():
        if s["version"] != version:
            continue
        name = (s["name"] or "").lower()
        if not name:
            continue
        if needle in name or (short and short in name):
            out.append(scene_id)
    return out


# ---------------------------------------------------------------------------
# Construction du graphe
# ---------------------------------------------------------------------------

def build_graph(assets, scenes, binds, input_name):
    """Construit le GraphResult complet à partir des données et du nom saisi."""
    (scene_active_assets, scenes_by_iv,
     scene_stream_max, asset_stream_max) = _build_indexes(assets, scenes, binds)

    start_key, start_scene_ids, prefix = resolve_scene(input_name, scenes)

    # Assets directement bindés à S0 (union des binds actifs de ses lignes).
    s0_dep_assets = set()
    for scene_id in start_scene_ids:
        s0_dep_assets |= scene_active_assets.get(scene_id, set())
    # On ne garde que les asset_id réellement présents dans la table assets.
    s0_dep_assets = {aid for aid in s0_dep_assets if aid in assets}

    # Parcours récursif au niveau asset, cycle-safe.
    seen_assets = set()
    asset_edges = set()          # (child_asset_id, parent_asset_id)
    stack = list(s0_dep_assets)
    while stack:
        aid = stack.pop()
        if aid in seen_assets:
            continue
        seen_assets.add(aid)
        a = assets.get(aid)
        if a is None:
            continue
        # Scènes productrices de cet asset -> leurs binds actifs = parents.
        producing = scenes_by_iv.get(_asset_scene_key(a), ())
        parents = set()
        for psid in producing:
            parents |= scene_active_assets.get(psid, set())
        for pid in parents:
            if pid not in assets:
                continue
            asset_edges.add((aid, pid))     # enfant importe parent
            if pid not in seen_assets:
                stack.append(pid)

    # Regroupement des assets par scène productrice.
    group_node_names = defaultdict(set)
    for aid in seen_assets:
        a = assets[aid]
        group_node_names[_asset_scene_key(a)].add(a["node_name"])

    result = GraphResult()

    def ensure_node(key):
        node = result.nodes.get(key)
        if node is None:
            project, entity, task, av, version = key
            node = SceneNode(key, project, entity, task, av, version)
            result.nodes[key] = node
        return node

    # Nœud de départ S0 (peut n'avoir aucun output suivi -> la compo).
    s0 = ensure_node(start_key)
    s0.is_start = True
    result.start_key = start_key

    # Un nœud par groupe d'assets.
    for key, names in group_node_names.items():
        node = ensure_node(key)
        node.node_names = sorted(n for n in names if n)

    # Arêtes scène -> scène (parent en haut, enfant en bas), dédupliquées,
    # sans self-loop. On mémorise aussi les assets qui transitent par chaque
    # lien (l'enfant importe ces assets du parent).
    edge_assets = defaultdict(set)
    for child_aid, parent_aid in asset_edges:
        child_key = _asset_scene_key(assets[child_aid])
        parent_key = _asset_scene_key(assets[parent_aid])
        if child_key == parent_key:
            continue
        ensure_node(child_key)
        ensure_node(parent_key)
        edge_assets[(parent_key, child_key)].add(parent_aid)
    # Arêtes de S0 vers les scènes de ses dépendances directes.
    for aid in s0_dep_assets:
        dep_key = _asset_scene_key(assets[aid])
        if dep_key != start_key:
            ensure_node(dep_key)
            edge_assets[(dep_key, start_key)].add(aid)
    result.edges = sorted(edge_assets)
    result.edge_assets = {edge: frozenset(aids)
                          for edge, aids in edge_assets.items()}

    # Inputs (assets importés) de chaque nœud, pour la coloration par
    # propagation : union des assets arrivant par ses liens entrants.
    node_inputs = defaultdict(set)
    for (_top, bottom), aids in edge_assets.items():
        node_inputs[bottom] |= aids

    # Détails par output + inputs + graphiste(s), et titre (nom de table).
    for node in result.nodes.values():
        _compute_outputs(node, asset_stream_max)
        _compute_inputs(node, node_inputs.get(node.key, ()),
                        assets, asset_stream_max)
        _fill_scene_meta(node, scenes_by_iv, scenes)
        node.display_name = _display_title(node, prefix)

    # Détail des assets transitant par chaque lien (panneau de sélection).
    result.edge_details = {
        edge: _asset_details(aids, assets, asset_stream_max)
        for edge, aids in result.edge_assets.items()
    }
    # Assets périmés : suffit pour recalculer les statuts (pas besoin de
    # regarder à nouveau les versions ensuite).
    result.stale_asset_ids = frozenset(
        aid for aid in seen_assets
        if _input_is_stale(assets.get(aid), asset_stream_max))
    # Liens qui transportent au moins un asset supplanté : sert à colorer le
    # lien responsable de l'obsolescence d'un nœud rouge.
    result.stale_edges = frozenset(
        edge for edge, aids in result.edge_assets.items()
        if aids & result.stale_asset_ids)

    # Statut par propagation à trois états :
    #   stale (rouge)     = importe au moins un asset supplanté ;
    #   inherited (jaune) = inputs à jour mais un ancêtre est obsolète ;
    #   ok (vert)         = à jour et aucun ancêtre obsolète.
    recompute_status(result)

    _assign_layout(result)

    result.stats = {
        "nodes": len(result.nodes),
        "edges": len(result.edges),
        "assets_visited": len(seen_assets),
    }
    return result


def _compute_outputs(node, asset_stream_max):
    """Renseigne node.outputs = [(name, latest_version)] pour chaque output.

    ``latest_version`` est la dernière version EXPORTÉE de cet asset (flux
    ``project, entity, task, av, node_name``). L'output est à jour si
    ``node.version >= latest_version`` (comparaison au niveau de l'asset).
    """
    outputs = []
    for name in node.node_names:
        latest = asset_stream_max.get(
            (node.project, node.entity_name, node.task_name,
             node.av_name, name))
        if latest is None:
            latest = node.version
        outputs.append((name, latest))
    node.outputs = outputs


def _input_label(asset):
    """Libellé court d'un asset importé (pour le survol)."""
    taskdisp = task_display(asset["task_name"])
    base = f"{asset['entity_name']}_{taskdisp}"
    if asset["av_name"]:
        base += f"_{asset['av_name']}"
    node_name = asset["node_name"]
    return f"{base} · {node_name}" if node_name else base


def _compute_inputs(node, input_ids, assets, asset_stream_max):
    """Renseigne node.inputs = [(label, version, latest_version)].

    Chaque asset importé est comparé à sa dernière version exportée.
    """
    seen = {}
    for pid in input_ids:
        a = assets.get(pid)
        if a is None:
            continue
        stream = (a["project"], a["entity_name"], a["task_name"],
                  a["av_name"], a["node_name"])
        latest = asset_stream_max.get(stream, a["version"])
        seen[(_input_label(a), a["version"], latest)] = None
    node.inputs = sorted(seen, key=lambda t: t[0])


def _fill_scene_meta(node, scenes_by_iv, scenes):
    """Renseigne node.scene_names et node.artists depuis les lignes scenes.

    Le graphiste provient d'une colonne dédiée (artist/user/…) si elle
    existe ; sinon il est déduit du nom (sale) de la scène.
    """
    names, artists = [], []
    for sid in scenes_by_iv.get(node.key, ()):
        s = scenes.get(sid, {})
        raw = s.get("name", "")
        if raw and raw not in names:
            names.append(raw)
        artist = s.get("artist", "") or extract_artist(
            raw, node.project, node.entity_name, node.task_name, node.av_name)
        if artist and artist not in artists:
            artists.append(artist)
    node.scene_names = names
    node.artists = artists


def _display_title(node, prefix):
    """Titre du rectangle : nom brut de la table + version, ex.
    « tmp_024C_0060_lighting_main (v010) ».

    On privilégie le nom de la ligne ``scenes`` ; à défaut (nœud issu
    uniquement d'assets, sans ligne scene) on reconstruit l'identité depuis
    les colonnes structurées. Une version en suffixe est retirée pour éviter
    la redondance avec « (vNNN) ».
    """
    base = node.scene_names[0] if node.scene_names else ""
    if not base:
        taskdisp = task_display(node.task_name)
        base = f"{node.project or prefix}_{node.entity_name}_{taskdisp}"
        if node.av_name:
            base += f"_{node.av_name}"
    base = re.sub(r"_[vV]\d+$", "", base)
    ver = f"v{node.version:03d}" if node.version is not None else "v?"
    return f"{base} ({ver})"


def extract_artist(scene_name, project, entity_name, task_name, av_name):
    """Devine le nom du graphiste depuis le nom (sale) de la scène.

    On retire du nom les segments connus (préfixe, entité, tâche, av, version)
    et on garde le reste, qui contient le nom d'artiste (et d'éventuels
    suffixes).
    """
    if not scene_name:
        return ""
    known = {(project or "").lower(),
             (task_name or "").lower(),
             task_display(task_name or "").lower()}
    for tok in (entity_name or "").split("_"):
        known.add(tok.lower())
    if av_name:
        known.add(av_name.lower())
    known.discard("")

    remaining = []
    for tok in scene_name.split("_"):
        low = tok.lower()
        if not low or low in known or re.fullmatch(r"v\d+", low):
            continue
        remaining.append(tok)
    return "_".join(remaining)


def _input_is_stale(asset, asset_stream_max):
    """Vrai si cet asset importé n'est pas à sa dernière version exportée."""
    if asset is None or asset["version"] is None:
        return False
    stream = (asset["project"], asset["entity_name"], asset["task_name"],
              asset["av_name"], asset["node_name"])
    latest = asset_stream_max.get(stream)
    return latest is not None and asset["version"] < latest


def _asset_details(asset_ids, assets, asset_stream_max):
    """[(label, version, latest_version)] trié, pour un ensemble d'assets."""
    rows = set()
    for aid in asset_ids:
        a = assets.get(aid)
        if a is None:
            continue
        stream = (a["project"], a["entity_name"], a["task_name"],
                  a["av_name"], a["node_name"])
        rows.add((_input_label(a), a["version"],
                  asset_stream_max.get(stream, a["version"])))
    return sorted(rows, key=lambda t: t[0])


def recompute_status(result, disabled_edges=()):
    """(Re)calcule le statut de chaque nœud, à trois états.

    * ``stale`` (rouge) : le nœud importe au moins un asset supplanté (comparé
      à la dernière version exportée de cet asset) ;
    * ``inherited`` (jaune) : ses inputs directs sont à jour, mais un de ses
      ancêtres (amont) est obsolète — obsolète par héritage uniquement ;
    * ``ok`` (vert) : à jour et aucun ancêtre obsolète.

    ``disabled_edges`` : liens temporairement désactivés (in-memory), ignorés
    aussi bien pour les inputs directs que pour la propagation. Rien n'est
    écrit en base : seuls les statuts en mémoire changent.
    """
    disabled = set(disabled_edges)
    stale_assets = result.stale_asset_ids

    # Inputs actifs par nœud + arêtes actives.
    direct_stale = set()
    children = defaultdict(list)
    for edge, aids in result.edge_assets.items():
        if edge in disabled:
            continue
        top_key, bottom_key = edge
        children[top_key].append(bottom_key)
        if aids & stale_assets:
            direct_stale.add(bottom_key)

    # Propagation de l'obsolescence vers l'aval, par les liens actifs.
    obsolete = set(direct_stale)
    stack = list(direct_stale)
    while stack:
        key = stack.pop()
        for child in children.get(key, ()):
            if child not in obsolete:
                obsolete.add(child)
                stack.append(child)

    for key, node in result.nodes.items():
        if key in direct_stale:
            node.status = "stale"
        elif key in obsolete:
            node.status = "inherited"
        else:
            node.status = "ok"


# ---------------------------------------------------------------------------
# Disposition : lignes de tâche (Y) + colonnes (X) par barycentre
# ---------------------------------------------------------------------------

def _rank_tasks_into_rows(result):
    """Calcule l'ordre des lignes (une par tâche canonique) et le séparateur.

    Renseigne node.row, result.row_tasks, result.row_levels et
    result.separator_after_row.
    """
    nodes = result.nodes
    node_task = {k: canon_task(n.task_name) for k, n in nodes.items()}
    tasks_present = set(node_task.values())

    # Arêtes au niveau tâche (pour intercaler les tâches inconnues).
    task_parents = defaultdict(set)
    task_children = defaultdict(set)
    for top, bottom in result.edges:
        tp, tb = node_task[top], node_task[bottom]
        if tp != tb:
            task_parents[tb].add(tp)
            task_children[tp].add(tb)

    # Rangs fixes pour les tâches connues.
    rank = {t: float(_BASE_RANK[t]) for t in tasks_present if t in _BASE_RANK}
    unknown = [t for t in tasks_present if t not in _BASE_RANK]

    # Intercalation des tâches inconnues entre leurs inputs et leurs outputs.
    for _ in range(12):
        for t in unknown:
            los = [rank[p] for p in task_parents[t] if p in rank]
            his = [rank[c] for c in task_children[t] if c in rank]
            lo = max(los) if los else None
            hi = min(his) if his else None
            if lo is not None and hi is not None:
                rank[t] = (lo + hi) / 2.0 if hi > lo else lo + 1.0
            elif lo is not None:
                rank[t] = lo + _RANK_STEP / 2.0
            elif hi is not None:
                rank[t] = hi - _RANK_STEP / 2.0
    fallback = (max(rank.values()) if rank else 0.0) + _RANK_STEP
    for t in unknown:
        rank.setdefault(t, fallback)

    # La scène interrogée reste tout en bas.
    start_task = node_task.get(result.start_key)
    if start_task is not None and rank:
        rank[start_task] = max(rank.values()) + _RANK_STEP

    # Tri des tâches -> lignes compactées.
    ordered = sorted(tasks_present, key=lambda t: (rank[t], t))
    task_to_row = {t: i for i, t in enumerate(ordered)}
    for k, node in nodes.items():
        node.row = task_to_row[node_task[k]]

    result.row_tasks = {task_to_row[t]: _ROW_LABEL.get(t, t or "?")
                        for t in ordered}
    result.row_levels = {task_to_row[t]: task_level(t) for t in ordered}
    result.separator_after_row = _separator_row(result.row_levels)


def _separator_row(row_levels):
    """Indice de la dernière ligne « asset » suivie d'au moins une autre."""
    asset_rows = [r for r, lvl in row_levels.items() if lvl == "asset"]
    if not asset_rows:
        return None
    last_asset = max(asset_rows)
    if any(r > last_asset for r in row_levels):
        return last_asset
    return None


def _assign_layout(result):
    """Assigne node.row (une ligne par tâche) et node.col (ordre).

    Ordre par défaut : niveau asset (modeling, shading, rig) puis, sous un
    séparateur, niveau shot (tracking, layout, anim, fx, lighting,
    compositing). Les tâches inconnues sont intercalées d'après leur position
    dans le graphe (entre leurs inputs et leurs outputs). La scène interrogée
    reste tout en bas.
    """
    nodes = result.nodes
    if not nodes:
        return

    _rank_tasks_into_rows(result)

    # Groupes par ligne.
    rows = defaultdict(list)
    for node in nodes.values():
        rows[node.row].append(node)
    max_row = max(rows)

    # Arêtes indexées par nœud (parents en haut, enfants en bas).
    parents_of = defaultdict(list)   # enfant -> parents
    children_of = defaultdict(list)  # parent -> enfants
    for top_key, bottom_key in result.edges:
        parents_of[bottom_key].append(top_key)
        children_of[top_key].append(bottom_key)

    # Ordre initial : par entité puis av puis nom (stable et lisible).
    def initial_sort(node):
        return (node.entity_name, node.av_name, node.display_name)

    order = {}   # key -> position (float) courante dans sa ligne
    for row in range(max_row + 1):
        for i, node in enumerate(sorted(rows[row], key=initial_sort)):
            order[node.key] = float(i)

    def barycenter(node, neighbor_keys):
        cols = [order[k] for k in neighbor_keys if k in order]
        return sum(cols) / len(cols) if cols else None

    # Quelques passes de barycentre (bas puis haut) pour réduire les
    # croisements ; on ré-indexe les positions après chaque tri.
    for _ in range(4):
        # Descente : ordonner chaque ligne selon la moyenne des parents.
        for row in range(1, max_row + 1):
            _reorder_row(rows[row], order,
                         lambda n: barycenter(n, parents_of[n.key]),
                         initial_sort)
        # Remontée : selon la moyenne des enfants.
        for row in range(max_row - 1, -1, -1):
            _reorder_row(rows[row], order,
                         lambda n: barycenter(n, children_of[n.key]),
                         initial_sort)

    # Colonnes entières finales par ligne.
    for row in range(max_row + 1):
        ordered = sorted(rows[row], key=lambda n: order[n.key])
        for i, node in enumerate(ordered):
            node.col = i
            order[node.key] = float(i)


def _reorder_row(row_nodes, order, key_fn, tiebreak):
    """Réordonne une ligne selon key_fn (barycentre) et réindexe order."""
    def sort_key(node):
        b = key_fn(node)
        # Les nœuds sans voisin gardent leur position actuelle.
        primary = b if b is not None else order[node.key]
        return (primary, tiebreak(node))

    ordered = sorted(row_nodes, key=sort_key)
    for i, node in enumerate(ordered):
        order[node.key] = float(i)
