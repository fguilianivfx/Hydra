"""Outil « Graphist to graph » : contrôle des scènes d'un graphiste.

Pour un graphiste et un projet, on liste ses scènes (dernière version de
chacune) et on vérifie si leurs imports sont à jour : un import est
**obsolète** si son flux d'output — clé ``(project, entity, task, av, node)``
— possède une version publiée plus récente que celle actuellement bindée.

Reprend la sémantique du script en ligne de commande ``checkGraph`` :

* le **projet** est un *préfixe* (``tem`` retient ``tem``, ``tem_xxx``…) ;
* le **graphiste** est cherché d'abord à l'identique (colonne ``author``),
  puis, à défaut, de façon approchée sur le nom de famille ;
* seule la **dernière version** de chaque scène est contrôlée.

Aucune écriture n'est faite sur la base.
"""

from __future__ import annotations

from collections import defaultdict

import graph_model as gm

# Filtre de task « toutes les tasks ».
ALL_TASKS = "all"


class ImportRow:
    """Un asset importé par une scène, avec sa fraîcheur."""

    __slots__ = ("label", "task_name", "version", "latest", "outdated")

    def __init__(self, label, task_name, version, latest):
        self.label = label
        self.task_name = task_name
        self.version = version
        self.latest = latest
        self.outdated = (version is not None and latest is not None
                         and version < latest)


class SceneEntry:
    """Une scène du graphiste (sa dernière version) et ses imports."""

    __slots__ = ("scene_ids", "display_name", "project", "code", "entity_name",
                 "task_name", "av_name", "version", "author",
                 "imports", "checked_count")

    def __init__(self, display_name, version):
        self.scene_ids = []
        self.display_name = display_name
        self.project = self.entity_name = self.task_name = self.av_name = ""
        # Code court du projet tel qu'il apparaît DANS le nom de la scène
        # (« tem »), qui diffère du nom complet en base (« tempete_26 »).
        self.code = ""
        self.version = version
        self.author = ""
        self.imports = []        # imports retenus (obsolètes)
        self.checked_count = 0   # nombre d'imports examinés

    @property
    def outdated_count(self):
        return sum(1 for row in self.imports if row.outdated)

    @property
    def has_outdated(self):
        return self.outdated_count > 0

    @property
    def scene_name(self):
        """Nom canonique pour relancer l'outil « Scene to graph ».

        On utilise le **code court** du projet (« tem »), tel qu'il figure dans
        le nom de la scène, et non le nom complet de la colonne ``project``
        (« tempete_26 ») : c'est le code qui sert à nommer les scènes.
        """
        task = gm.task_display(self.task_name)
        name = f"{self.code or self.project}_{self.entity_name}_{task}"
        if self.av_name:
            name += f"_{self.av_name}"
        if self.version is not None:
            name += f"_v{self.version:03d}"
        return name


class ArtistReport:
    """Résultat complet d'un contrôle."""

    __slots__ = ("artists", "match_mode", "projects", "tasks", "entries",
                 "up_to_date", "outdated", "no_imports")

    def __init__(self):
        self.artists = []
        self.match_mode = "exact"    # "exact" | "approx"
        self.projects = []
        self.tasks = []              # [] = toutes les tasks
        self.entries = []
        self.up_to_date = 0
        self.outdated = 0
        self.no_imports = 0

    @property
    def total(self):
        return len(self.entries)


def parse_tasks(text):
    """« all » -> [] (toutes) ; « fx, animation » -> ['fx', 'animation'].

    Les alias sont normalisés (``anim`` -> ``animation``, ``comp`` ->
    ``compositing``…) pour coller aux valeurs de la base.
    """
    raw = (text or "").strip()
    if not raw or raw.lower() == ALL_TASKS:
        return []
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    tasks = []
    for part in parts:
        if not part or part.lower() == ALL_TASKS:
            continue
        canon = gm.canon_task(part)
        if canon not in tasks:
            tasks.append(canon)
    return tasks


def scene_author(scene):
    """Graphiste d'une scène : colonne dédiée, sinon déduit du nom."""
    author = (scene.get("artist") or "").strip()
    if author:
        return author
    return gm.extract_artist(scene.get("name", ""), scene.get("project", ""),
                             scene.get("entity_name", ""),
                             scene.get("task_name", ""),
                             scene.get("av_name", ""))


def _matches_project(scene, prefix):
    """Le projet saisi désigne-t-il cette scène ?

    On accepte aussi bien le nom complet en base (``tempete_26``) que le code
    court utilisé dans le nom de la scène (``tem``).
    """
    if not prefix:
        return True
    if scene.get("project", "").lower().startswith(prefix):
        return True
    code = project_code(scene.get("name", ""), scene.get("entity_name", ""),
                        "").lower()
    return bool(code) and code.startswith(prefix)


def list_authors(scenes, project=""):
    """Graphistes connus (facultativement restreints à un préfixe de projet)."""
    prefix = (project or "").strip().lower()
    found = set()
    for scene in scenes.values():
        if not _matches_project(scene, prefix):
            continue
        author = scene_author(scene)
        if author:
            found.add(author)
    return sorted(found, key=str.lower)


def resolve_authors(scenes, query, project=""):
    """(auteurs, mode) — exact d'abord, sinon approché sur le nom de famille."""
    wanted = (query or "").strip()
    if not wanted:
        return [], "exact"
    known = list_authors(scenes, project)
    exact = [a for a in known if a.lower() == wanted.lower()]
    if exact:
        return exact, "exact"
    # Repli : dernier mot du nom saisi, en sous-chaîne.
    surname = wanted.split()[-1].lower()
    approx = [a for a in known if surname in a.lower()]
    return approx, "approx"


def _latest_asset_versions(assets):
    """(project, entity, task, av, node) -> version max publiée."""
    latest = {}
    for asset in assets.values():
        version = asset["version"]
        if version is None:
            continue
        key = (asset["project"], asset["entity_name"], asset["task_name"],
               asset["av_name"], asset["node_name"])
        if key not in latest or version > latest[key]:
            latest[key] = version
    return latest


def project_code(scene_name, entity_name, project):
    """Code court du projet, extrait du nom de la scène.

    Le nom d'une scène commence par le code du show (« tem_077_0100_fx »)
    alors que la colonne ``project`` contient le nom complet
    (« tempete_26 ») : c'est le code qu'il faut réutiliser pour renommer une
    scène ou la passer à l'outil « Scene to graph ».
    """
    name = (scene_name or "").strip()
    if name:
        head = name.split("_", 1)[0]
        first_entity = (entity_name or "").split("_", 1)[0]
        # On refuse un « code » qui serait en fait le début de l'entité.
        if head and head != first_entity:
            return head
    return project or ""


def _scene_names_by_identity(scenes):
    """(project, entity, task, av) -> nom de scène brut (dernière version).

    Sert à nommer la scène **productrice** d'un asset importé exactement comme
    elle apparaît ailleurs dans la liste (avec le code court du show).
    """
    names, versions = {}, {}
    for scene in scenes.values():
        name = scene.get("name")
        if not name:
            continue
        key = (scene["project"], scene["entity_name"], scene["task_name"],
               scene["av_name"])
        version = scene.get("version") or -1
        if key not in versions or version > versions[key]:
            versions[key] = version
            names[key] = name
    return names


def _codes_by_project(scenes):
    """project (nom complet) -> code court du show, vu dans les noms de scènes."""
    codes = {}
    for scene in scenes.values():
        project = scene.get("project", "")
        if project in codes:
            continue
        code = project_code(scene.get("name", ""),
                            scene.get("entity_name", ""), "")
        if code:
            codes[project] = code
    return codes


def _asset_label(asset, scene_names=None, codes=None):
    """Libellé d'un asset importé : « nom de la scène productrice · node »."""
    node = asset.get("node_name") or asset.get("name") or "?"
    key = (asset["project"], asset["entity_name"], asset["task_name"],
           asset["av_name"])
    base = (scene_names or {}).get(key)
    if not base:
        # Aucune scène productrice connue : on reconstruit l'identité, en
        # gardant le code court du show pour rester homogène avec le reste.
        code = (codes or {}).get(asset["project"], "")
        base = f"{asset['entity_name']}_{gm.task_display(asset['task_name'])}"
        if asset["av_name"]:
            base += f"_{asset['av_name']}"
        if code:
            base = f"{code}_{base}"
    return f"{base} · {node}"


def is_shot_scene(task_name):
    """Vrai si la scène relève d'un plan (et non d'un asset).

    On écarte uniquement les tasks de **niveau asset** (modeling, shading,
    rigging) : une task inconnue est conservée, pour ne jamais masquer
    silencieusement du travail.
    """
    return gm.task_level(task_name) != "asset"


def check_artist_scenes(assets, scenes, binds, artist, project,
                        tasks=(), only_outdated_imports=True,
                        shots_only=False):
    """Contrôle les scènes d'un graphiste sur un projet.

    ``tasks`` vide = toutes les tasks. Sinon seuls les imports dont la task
    correspond sont retenus. ``only_outdated_imports`` ne conserve que les
    imports périmés (comportement par défaut de l'outil). ``shots_only``
    restreint aux scènes de plans (exclut modeling / shading / rigging).
    """
    report = ArtistReport()
    report.tasks = list(tasks)

    prefix = (project or "").strip().lower()
    report.artists, report.match_mode = resolve_authors(scenes, artist, project)
    if not report.artists:
        return report
    wanted_authors = {a.lower() for a in report.artists}
    wanted_tasks = {gm.canon_task(t) for t in tasks} if tasks else None

    # Dernière version de chaque scène (project, entity, task, av) du projet.
    latest_scene = {}
    for scene_id, scene in scenes.items():
        if prefix and not _matches_project(scene, prefix):
            continue
        if shots_only and not is_shot_scene(scene.get("task_name", "")):
            continue
        version = scene.get("version")
        if version is None:
            continue
        key = (scene["project"], scene["entity_name"], scene["task_name"],
               scene["av_name"])
        current = latest_scene.get(key)
        if current is None or version > current[0]:
            latest_scene[key] = (version, [scene_id])
        elif version == current[0]:
            current[1].append(scene_id)

    # Binds actifs par scène.
    scene_assets = defaultdict(set)
    for asset_id, scene_id, active in binds:
        if gm.is_active_bind(active):
            scene_assets[scene_id].add(asset_id)

    latest_assets = _latest_asset_versions(assets)
    scene_names = _scene_names_by_identity(scenes)
    codes = _codes_by_project(scenes)
    projects = set()

    for key, (version, scene_ids) in latest_scene.items():
        # La scène doit être assignée au graphiste recherché.
        authors = {scene_author(scenes[sid]) for sid in scene_ids}
        authors = {a for a in authors if a}
        if not any(a.lower() in wanted_authors for a in authors):
            continue

        project_name, entity, task, av = key
        projects.add(project_name)
        raw_name = ""
        for sid in scene_ids:
            raw_name = scenes[sid].get("name") or raw_name
        display = raw_name or f"{project_name}_{entity}_{gm.task_display(task)}"
        entry = SceneEntry(display, version)
        entry.scene_ids = sorted(scene_ids)
        entry.project, entry.entity_name = project_name, entity
        entry.task_name, entry.av_name = task, av
        entry.code = project_code(raw_name, entity, project_name)
        entry.author = ", ".join(sorted(authors))

        seen = set()
        for sid in scene_ids:
            for asset_id in scene_assets.get(sid, ()):
                asset = assets.get(asset_id)
                if asset is None or asset_id in seen:
                    continue
                seen.add(asset_id)
                if (wanted_tasks is not None
                        and gm.canon_task(asset["task_name"]) not in wanted_tasks):
                    continue
                entry.checked_count += 1
                stream = (asset["project"], asset["entity_name"],
                          asset["task_name"], asset["av_name"],
                          asset["node_name"])
                row = ImportRow(_asset_label(asset, scene_names, codes),
                                asset["task_name"],
                                asset["version"],
                                latest_assets.get(stream, asset["version"]))
                if row.outdated or not only_outdated_imports:
                    entry.imports.append(row)

        entry.imports.sort(key=lambda r: r.label)
        report.entries.append(entry)

        if entry.checked_count == 0:
            report.no_imports += 1
        elif entry.has_outdated:
            report.outdated += 1
        else:
            report.up_to_date += 1

    # Liste alphabétique des scènes.
    report.entries.sort(key=lambda e: (e.display_name.lower(), e.version or 0))
    report.projects = sorted(projects)
    return report
