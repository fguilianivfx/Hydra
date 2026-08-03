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
TASK_ORDER = [
    "modeling",
    "tracking",
    "rigging",
    "layout",
    "shading",
    "animation",
    "lighting",
    "compositing",
]
_TASK_RANK = {name: i for i, name in enumerate(TASK_ORDER)}
# Une tâche inconnue est placée tout en bas.
_UNKNOWN_RANK = len(TASK_ORDER)

# Alias de tâches (nom saisi <-> nom en base) utilisés pour la résolution.
# Le seul mapping imposé est comp <-> compositing ; les autres aident à
# retrouver une scène quand l'utilisateur emploie une abréviation courante.
_TASK_ALIASES = {
    "compositing": {"comp"},
    "animation": {"anim"},
    "lighting": {"light", "lgt"},
    "modeling": {"model", "mod"},
    "tracking": {"track", "matchmove", "mm"},
    "layout": {"lay"},
    "shading": {"shade", "lookdev", "look"},
    "rigging": {"rig"},
}


def task_display(task_name):
    """Nom court d'une tâche pour l'affichage (compositing -> comp)."""
    return "comp" if task_name == "compositing" else task_name


def _active(value):
    """Vrai si un bind est actif (active dans ('1', 1))."""
    if value in (1, "1"):
        return True
    return isinstance(value, str) and value.strip() == "1"


def canonical_name(prefix, entity_name, task_name, av_name, version):
    """Nom canonique complet d'une scène pour l'affichage.

    f"{prefix}_{entity}_{taskdisp}" + ("_{av}" si av) + "_v{NNN}".
    """
    taskdisp = task_display(task_name)
    name = f"{prefix}_{entity_name}_{taskdisp}"
    if av_name:
        name += f"_{av_name}"
    if version is None:
        name += "_v???"
    else:
        name += f"_v{version:03d}"
    return name


# ---------------------------------------------------------------------------
# Nœud de graphe et résultat
# ---------------------------------------------------------------------------

class SceneNode:
    """Un rectangle du graphe : une scène (une version) et ses outputs."""

    __slots__ = (
        "key", "project", "entity_name", "task_name", "av_name", "version",
        "node_names", "outputs", "artists", "scene_names",
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
        self.artists = []          # graphistes ayant publié cette scène
        self.scene_names = []      # noms bruts des lignes scenes (pour survol)
        self.is_start = False
        # "ok" (vert) | "inherited" (orange) | "stale" (rouge)
        self.status = "ok"
        self.display_name = ""
        self.row = _UNKNOWN_RANK
        self.col = 0

    @property
    def task_rank(self):
        return _TASK_RANK.get(self.task_name, _UNKNOWN_RANK)


class GraphResult:
    """Résultat complet prêt à afficher."""

    __slots__ = ("nodes", "edges", "start_key", "row_tasks", "stats")

    def __init__(self):
        self.nodes = {}            # key -> SceneNode
        self.edges = []            # list[(top_key, bottom_key)] (parent -> enfant)
        self.start_key = None
        self.row_tasks = {}        # row_index -> nom de tâche (pour les labels)
        self.stats = {}


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
        raise SceneResolutionError("Aucun nom de scène saisi.")

    m = re.search(r"_v0*(\d+)\s*$", raw, re.IGNORECASE)
    if not m:
        raise SceneResolutionError(
            "Nom invalide : la version finale est attendue "
            "(ex. « qua_077_02000_comp_v019 »)."
        )
    version = int(m.group(1))
    stem = raw[: m.start()]
    parts = stem.split("_")
    if len(parts) < 2:
        raise SceneResolutionError(
            "Nom invalide : préfixe et entité attendus "
            "(ex. « qua_077_02000_comp_v019 »)."
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
            f"Scène introuvable pour « {raw} ». "
            "Vérifiez l'entité, la tâche, l'av et la version."
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
    # sans self-loop.
    edge_set = set()
    for child_aid, parent_aid in asset_edges:
        child_key = _asset_scene_key(assets[child_aid])
        parent_key = _asset_scene_key(assets[parent_aid])
        if child_key == parent_key:
            continue
        ensure_node(child_key)
        ensure_node(parent_key)
        edge_set.add((parent_key, child_key))
    # Arêtes de S0 vers les scènes de ses dépendances directes.
    for aid in s0_dep_assets:
        dep_key = _asset_scene_key(assets[aid])
        if dep_key != start_key:
            ensure_node(dep_key)
            edge_set.add((dep_key, start_key))
    result.edges = sorted(edge_set)

    # Inputs (assets importés) de chaque nœud, pour la coloration par
    # propagation. asset_edges = (enfant, parent) : l'enfant importe le
    # parent, donc le parent est un input du nœud producteur de l'enfant.
    node_inputs = defaultdict(set)
    for child_aid, parent_aid in asset_edges:
        node_inputs[_asset_scene_key(assets[child_aid])].add(parent_aid)
    for aid in s0_dep_assets:                      # inputs directs de S0
        node_inputs[start_key].add(aid)

    # Détails par output + graphiste(s), et nom canonique d'affichage.
    for node in result.nodes.values():
        _compute_outputs(node, asset_stream_max)
        _fill_scene_meta(node, scenes_by_iv, scenes)
        node.display_name = canonical_name(
            node.project or prefix, node.entity_name, node.task_name,
            node.av_name, node.version,
        )

    # Statut par propagation à trois états :
    #   stale (rouge)     = importe au moins un asset supplanté ;
    #   inherited (orange)= inputs à jour mais un ancêtre est obsolète ;
    #   ok (vert)         = à jour et aucun ancêtre obsolète.
    _propagate_status(result, node_inputs, assets, asset_stream_max)

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


def _fill_scene_meta(node, scenes_by_iv, scenes):
    """Renseigne node.scene_names et node.artists depuis les lignes scenes."""
    names, artists = [], []
    for sid in scenes_by_iv.get(node.key, ()):
        raw = scenes.get(sid, {}).get("name", "")
        if raw and raw not in names:
            names.append(raw)
        artist = _extract_artist(raw, node)
        if artist and artist not in artists:
            artists.append(artist)
    node.scene_names = names
    node.artists = artists


def _extract_artist(scene_name, node):
    """Devine le nom du graphiste depuis le nom (sale) de la scène.

    On retire du nom les segments connus (préfixe, entité, tâche, av, version)
    et on garde le reste, qui contient le nom d'artiste (et d'éventuels
    suffixes).
    """
    if not scene_name:
        return ""
    known = {(node.project or "").lower(),
             (node.task_name or "").lower(),
             task_display(node.task_name).lower()}
    for tok in (node.entity_name or "").split("_"):
        known.add(tok.lower())
    if node.av_name:
        known.add(node.av_name.lower())
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


def _propagate_status(result, node_inputs, assets, asset_stream_max):
    """Coloration par propagation à trois états.

    * ``stale`` (rouge) : le nœud importe au moins un asset supplanté (comparé
      à la dernière version exportée de cet asset) ;
    * ``inherited`` (orange) : ses inputs directs sont à jour, mais un de ses
      ancêtres (amont) est obsolète — obsolète par héritage uniquement ;
    * ``ok`` (vert) : à jour et aucun ancêtre obsolète.
    """
    direct_stale = set()
    for key in result.nodes:
        for pid in node_inputs.get(key, ()):
            if _input_is_stale(assets.get(pid), asset_stream_max):
                direct_stale.add(key)
                break

    children = defaultdict(list)
    for top_key, bottom_key in result.edges:
        children[top_key].append(bottom_key)

    # Propagation de l'obsolescence vers l'aval (rouge ET orange se propagent).
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

def _assign_layout(result):
    """Assigne node.row (rang de tâche compacté) et node.col (ordre).

    La scène interrogée (S0) est toujours placée sur la ligne la plus basse,
    quelle que soit sa tâche.
    """
    nodes = result.nodes
    if not nodes:
        return

    # La scène de départ reçoit un rang strictement inférieur à toute tâche
    # (connue ou inconnue) pour se retrouver tout en bas du graphe.
    start_key = result.start_key
    _BOTTOM_RANK = _UNKNOWN_RANK + 1

    def eff_rank(node):
        return _BOTTOM_RANK if node.key == start_key else node.task_rank

    # Rangs présents -> lignes compactées (pas de trous).
    ranks_present = sorted({eff_rank(n) for n in nodes.values()})
    rank_to_row = {rank: i for i, rank in enumerate(ranks_present)}
    for node in nodes.values():
        node.row = rank_to_row[eff_rank(node)]

    # Libellés de ligne : nom de tâche représentatif par rang.
    rank_label = {}
    for node in nodes.values():
        rank_label.setdefault(eff_rank(node), node.task_name or "?")
    result.row_tasks = {
        rank_to_row[rank]: (TASK_ORDER[rank] if rank < len(TASK_ORDER)
                            else rank_label[rank])
        for rank in ranks_present
    }

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
