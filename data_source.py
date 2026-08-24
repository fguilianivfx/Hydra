"""Chargement des données depuis les trois sources supportées.

Ce module expose une interface unique qui renvoie toujours les mêmes
structures en mémoire, quelle que soit la provenance des données :

    assets : dict[int, dict]
        clés du dict interne : project, entity_name, task_name, av_name,
        node_name, version (int | None), mute_row (ligne brute)
    scenes : dict[int, dict]
        clés du dict interne : project, entity_name, task_name, av_name,
        version (int | None), name, task_id (int | None)
    binds  : list[tuple]
        (asset_id: int, scene_id: int, active: str | int)

Trois fonctions d'entrée :

    load_from_mysql(config)   -> (assets, scenes, binds)
    load_from_csv(paths)      -> (assets, scenes, binds)
    load_from_sql_dump(path)  -> (assets, scenes, binds)

Toute la logique de graphe (graph_model) travaille sur ces structures,
indépendamment de la source.
"""

from __future__ import annotations

import csv
import datetime
import os
import re

# csv peut rencontrer des tuples très longs dans un dump SQL : on relève la
# limite par défaut pour éviter un « field larger than field limit ».
try:
    csv.field_size_limit(2 ** 24)
except (OverflowError, ValueError):  # pragma: no cover - dépend de la plateforme
    pass


class DataSourceError(Exception):
    """Erreur de chargement (fichier manquant, colonne absente, connexion…)."""


# Colonnes minimales requises par table (les autres sont ignorées).
REQUIRED_COLUMNS = {
    "assets": ["id", "project", "entity_name", "task_name",
               "av_name", "node_name", "version"],
    "scenes": ["id", "name", "project", "entity_name", "task_name",
               "av_name", "version"],
    "binds": ["asset_id", "scene_id", "active"],
}


# ---------------------------------------------------------------------------
# Conversions de types (partagées par toutes les sources)
# ---------------------------------------------------------------------------

def to_int(value):
    """Convertit vers int, ou None si vide / non numérique / NULL."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s == "" or s.upper() == "NULL":
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def norm_str(value):
    """Normalise une chaîne : None / NULL -> '' ; strip des espaces."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.upper() == "NULL":
        return ""
    return s


def _asset_from_mapping(get, date_column="", format_spec=("", "")):
    """Construit un enregistrement asset depuis un accès par nom de colonne.

    ``date_column`` est la colonne de date retenue pour la table courante (voir
    ``resolve_date_column``) ; à défaut on retombe sur les noms connus.
    ``format_spec`` est le couple (colonne, mode) rendu par
    ``resolve_format_column``.
    """
    return {
        "project": norm_str(get("project")),
        "entity_name": norm_str(get("entity_name")),
        "task_name": norm_str(get("task_name")),
        "av_name": norm_str(get("av_name")),
        "node_name": norm_str(get("node_name")),
        "version": to_int(get("version")),
        # Colonnes facultatives : libellé de repli quand node_name est vide,
        # puis auteur, date de publication (affichés au survol) et format du
        # fichier exporté (abc, mb, exr…).
        "name": norm_str(get("name")),
        "artist": _pick(get, _ARTIST_COLUMNS),
        "date": (norm_str(get(date_column)) if date_column
                 else _pick(get, _DATE_COLUMNS)),
        "format": _asset_format(get, format_spec),
        # Chemin publié : sert au mute persistant (module Kraken).
        "path": row_path(get),
        # Ligne brute pour la couche « FROM ROWS » du module de mutes.
        "mute_row": mute_row(get),
    }


# Identité d'un asset pour le module de mutes : son MUTE_FIELDS.
MUTE_ROW_IDENTITY = ("project", "entity_name", "task_name", "av_name",
                     "node_name", "extension")
# Ce qu'on lui transmet en plus : la version — qui n'a ainsi plus à être
# devinée depuis le nom de fichier — et l'id de la ligne elle-même.
MUTE_ROW_FIELDS = ("id",) + MUTE_ROW_IDENTITY + ("version",)


def mute_row(get):
    """Valeurs BRUTES des colonnes que le module de mutes identifie.

    Rien n'est normalisé ici, volontairement : le module lit la même table
    ``assets`` que nous, la comparaison se fait donc sur ce que
    ``get_infoasset()`` y a écrit. Normaliser (retirer le point de
    « .hda », passer en minuscules) fabriquerait une valeur qui ne
    correspond plus à aucune ligne.

    Seule exception, les champs d'identité : NULL y devient '' plutôt que de
    disparaître du dict. Un asset sans ``av_name`` en a un vide, ce n'est
    pas un asset incomplet — et une clé manquante laisserait le module
    appliquer son propre défaut.
    """
    row = {}
    for name in MUTE_ROW_FIELDS:
        value = get(name)
        if value is None or str(value).strip().upper() == "NULL":
            if name in MUTE_ROW_IDENTITY:
                row[name] = ""
            continue
        row[name] = value
    return row


def mute_row_usable(row):
    """Vrai si cette ligne brute suffit à identifier l'asset pour le module.

    Les cinq premiers champs d'identité sont des colonnes OBLIGATOIRES de la
    table ``assets`` (voir ``REQUIRED_COLUMNS``) : elles sont toujours là.
    L'``extension``, elle, est facultative — et c'est elle qui sépare deux
    exports d'un même node (un ``.hda`` et un ``.bgeo.sc``). Sans elle, la
    ligne n'identifie rien de sûr : le lien retombe sur les chemins.
    """
    return bool(row) and bool(str(row.get("extension", "")).strip())


# Colonnes possibles pour le graphiste ayant publié la scène (facultatif).
_ARTIST_COLUMNS = ("artist", "user", "username", "created_by", "author",
                   "publisher", "login", "owner")
# Colonnes possibles pour la date de publication d'un asset (facultatives).
_DATE_COLUMNS = ("date", "created_at", "creation_date", "created", "ctime",
                 "export_date", "publish_date", "published_at", "timestamp",
                 "mtime", "updated_at")
# Fragments de noms trahissant une colonne de date, quand aucun nom connu ne
# correspond (les schémas varient d'un studio à l'autre).
_DATE_HINTS = ("date", "time", "stamp", "creat", "publi", "export", "modif",
               "updat", "jour")
# Colonnes qui ne peuvent pas porter de date, même si leur valeur y ressemble.
_NEVER_DATE_COLUMNS = frozenset(
    ("id", "asset_id", "scene_id", "version", "active", "project",
     "entity_name", "task_name", "av_name", "node_name", "name",
     "path", "file_path", "filepath", "scene_path", "asset_path",
     "output_path")
    + _ARTIST_COLUMNS)

# Un vrai horodatage porte un séparateur : un entier seul (numéro de frame,
# taille de fichier…) ne doit jamais être pris pour une date.
_DATE_LIKE_RE = re.compile(r"\d{2,4}[-/.]\d{1,2}[-/.]\d{1,4}")


def _looks_like_date(value):
    """Vrai si la valeur est une date/datetime ou un texte qui y ressemble."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return True
    return bool(_DATE_LIKE_RE.search(norm_str(value)))


def resolve_date_column(columns, samples=()):
    """Colonne portant la date de publication d'un asset.

    Trois passes, de la plus sûre à la plus permissive : nom connu
    (``date``, ``created_at``…), nom évocateur (``*date*``, ``*creat*``…),
    puis n'importe quelle colonne dont les valeurs ressemblent à des dates.
    Sans cette détection, un schéma nommant sa colonne autrement renvoie tous
    les assets sous « Unknown date ».

    ``samples`` est une liste d'accesseurs ``get(nom)`` sur quelques lignes.
    """
    names = [norm_str(c) for c in columns if norm_str(c)]
    by_lower = {}
    for name in names:
        by_lower.setdefault(name.lower(), name)

    def dated(name):
        return any(_looks_like_date(get(name)) for get in samples)

    def named_ok(name):
        """Un nom parlant suffit ; une colonne vide partout reste valable."""
        if not samples or dated(name):
            return True
        return not any(norm_str(get(name)) for get in samples)

    def by_name(accept):
        for known in _DATE_COLUMNS:
            name = by_lower.get(known)
            if name and accept(name):
                return name
        for name in names:
            low = name.lower()
            if low in _NEVER_DATE_COLUMNS:
                continue
            if any(hint in low for hint in _DATE_HINTS) and accept(name):
                return name
        return ""

    # Un nom connu mais vide partout ne doit pas l'emporter sur une colonne
    # réellement remplie : on exige d'abord des valeurs datées.
    if samples:
        found = by_name(dated)
        if found:
            return found
    found = by_name(named_ok)
    if found:
        return found
    # Dernier recours : le contenu fait foi.
    return next((name for name in names
                 if name.lower() not in _NEVER_DATE_COLUMNS and dated(name)),
                "")


# Colonnes portant directement le format d'un asset (« abc », « exr »…).
_FORMAT_COLUMNS = ("format", "ext", "extension", "file_format", "filetype",
                   "file_type", "output_format", "fmt")
# Colonnes portant un chemin ou un nom de fichier, dont on tire l'extension.
_PATH_COLUMNS = ("path", "file", "filename", "file_path", "filepath",
                 "output", "output_path", "name")
# Extension simple, éventuellement précédée d'une seconde (« .bgeo.sc »).
_EXT_RE = re.compile(r"\.([A-Za-z][A-Za-z0-9]{0,9})$")
_COMPOUND_EXT_RE = re.compile(
    r"\.([A-Za-z][A-Za-z0-9]{0,9})\.([A-Za-z][A-Za-z0-9]{0,3})$")
# Suffixes de compression qui prolongent l'extension au lieu de la remplacer :
# un cache Houdini « smoke.bgeo.sc » est un « bgeo.sc », pas un « sc ».
_EXT_SUFFIXES = frozenset(("sc", "gz", "bz2", "xz", "zip", "lz4", "zst"))


def norm_format(value):
    """Normalise un format d'asset : « .ABC » -> « abc » ('' si invalide).

    Les formats composés sont acceptés (``bgeo.sc``) : seuls les caractères
    alphanumériques et le point sont admis, et chaque segment doit être
    alphanumérique — un chemin ou une phrase est donc rejeté.
    """
    text = norm_str(value).lower().strip().lstrip(".")
    if not text or len(text) > 16:
        return ""
    parts = text.split(".")
    if len(parts) > 2 or not all(p.isalnum() for p in parts):
        return ""
    return text


def _extension_of(value):
    """Extension d'un chemin ou d'un nom de fichier ('' si aucune).

    Reconnaît les extensions composées de Houdini (``.bgeo.sc``,
    ``.geo.gz``) : sans cela un cache compressé se réduisait à « sc ».
    """
    text = norm_str(value)
    match = _COMPOUND_EXT_RE.search(text)
    if match and match.group(2).lower() in _EXT_SUFFIXES:
        return f"{match.group(1)}.{match.group(2)}".lower()
    match = _EXT_RE.search(text)
    return match.group(1).lower() if match else ""


def resolve_format_column(columns, samples=()):
    """(colonne, mode) portant le format d'un asset.

    ``mode`` vaut ``"value"`` quand la colonne contient déjà le format
    (« abc »), ou ``"ext"`` quand elle contient un chemin / nom de fichier dont
    il faut extraire l'extension. Renvoie ``("", "")`` si rien n'est
    exploitable — la liste de formats reste alors vide plutôt que fantaisiste.
    """
    names = [norm_str(c) for c in columns if norm_str(c)]
    by_lower = {}
    for name in names:
        by_lower.setdefault(name.lower(), name)

    for known in _FORMAT_COLUMNS:
        name = by_lower.get(known)
        if name and (not samples
                     or any(norm_format(get(name)) for get in samples)):
            return name, "value"
    for known in _PATH_COLUMNS:
        name = by_lower.get(known)
        if name and samples and any(_extension_of(get(name))
                                    for get in samples):
            return name, "ext"
    return "", ""


def _asset_format(get, spec):
    column, mode = spec or ("", "")
    if not column:
        return ""
    return (_extension_of(get(column)) if mode == "ext"
            else norm_format(get(column)))


# Colonnes portant le chemin brut du fichier publié (scène ou asset). Elles
# alimentent la persistance des mutes (module studio Kraken), qui parle en
# chemins. « name » n'en fait pas partie : un libellé n'est pas un chemin.
_RAW_PATH_COLUMNS = ("path", "file_path", "filepath", "scene_path",
                     "asset_path", "output_path", "file", "filename",
                     "output")


def _looks_like_raw_path(value):
    """Vrai si la valeur ressemble à un chemin (au moins un séparateur)."""
    s = norm_str(value)
    return len(s) > 3 and ("/" in s or "\\" in s)


def row_path(get):
    """Chemin brut d'une ligne assets/scenes ('' si aucune colonne n'en a).

    Détection par ligne : première colonne au nom connu dont la valeur
    ressemble vraiment à un chemin. Sans chemin, la persistance des mutes est
    simplement inactive pour cette ligne — on ne reconstruit jamais un chemin
    approximatif.
    """
    for name in _RAW_PATH_COLUMNS:
        value = norm_str(get(name))
        if _looks_like_raw_path(value):
            return value
    return ""


# Dernier schéma lu pour la table « assets » : permet d'expliquer dans
# l'interface pourquoi aucune date n'a pu être trouvée.
_LAST_ASSET_SCHEMA = {"columns": (), "date_column": "", "format_column": ""}


def asset_schema():
    """Colonnes vues au dernier chargement + colonnes date/format retenues."""
    return dict(_LAST_ASSET_SCHEMA)


def _remember_asset_schema(columns, date_column, format_spec=("", "")):
    _LAST_ASSET_SCHEMA["columns"] = tuple(
        norm_str(c) for c in columns if norm_str(c))
    _LAST_ASSET_SCHEMA["date_column"] = date_column
    _LAST_ASSET_SCHEMA["format_column"] = format_spec[0]
    return date_column


def _asset_columns_spec(rows):
    """Résout (et mémorise) les colonnes date et format des lignes chargées."""
    columns = list(rows[0].keys()) if rows else []
    samples = [r.get for r in rows[:200]]
    date_column = resolve_date_column(columns, samples)
    format_spec = resolve_format_column(columns, samples)
    _remember_asset_schema(columns, date_column, format_spec)
    return date_column, format_spec


def _pick(get, names):
    """Renvoie la 1re valeur non vide parmi plusieurs colonnes candidates."""
    for name in names:
        value = norm_str(get(name))
        if value:
            return value
    return ""


def _scene_from_mapping(get):
    return {
        "name": norm_str(get("name")),
        "project": norm_str(get("project")),
        "entity_name": norm_str(get("entity_name")),
        "task_name": norm_str(get("task_name")),
        "av_name": norm_str(get("av_name")),
        "version": to_int(get("version")),
        "artist": _pick(get, _ARTIST_COLUMNS),
        # Chemin du fichier scène : attendu par la couche « par chemins » des
        # mutes, et seul repli quand la tâche n'est pas identifiée.
        "path": row_path(get),
        # Tâche de la scène : clé de la couche « FROM ROWS », qui mute sans
        # avoir à faire résoudre un chemin par le module.
        "task_id": to_int(get("task_id")),
    }


# ---------------------------------------------------------------------------
# Source 1 : MySQL / MariaDB
# ---------------------------------------------------------------------------
# Les identifiants ne sont pas demandés dans l'UI. Chaque paramètre est résolu
# dans cet ordre :
#
#   1. variable d'environnement (MYSQL_HOST / MYSQL_DATABASE / MYSQL_USER /
#      MYSQL_PASS) ;
#   2. fichier « local_config.py » à côté des sources — NON VERSIONNÉ, c'est
#      là qu'on met le vrai mot de passe ; PyInstaller l'embarque
#      automatiquement dans l'exécutable ;
#   3. valeurs par défaut ci-dessous (factices pour le mot de passe).

_DEFAULT_MYSQL_HOST = "dd-intra"
DEFAULT_DATABASE = "dd_assets_tracking"
_DEFAULT_MYSQL_USER = "f.guiliani"
_DEFAULT_MYSQL_PASSWORD = "password_fab"   # factice : voir local_config.py

try:                       # pragma: no cover - présent seulement en local
    import local_config as _local
except ImportError:
    _local = None


def _resolve(env_var, local_name, default):
    """Renvoie (valeur, origine) : environnement > local_config.py > défaut."""
    value = os.environ.get(env_var)
    if value:
        return value, f"${env_var}"
    if _local is not None:
        value = getattr(_local, local_name, None)
        if value:
            return value, "local_config.py"
    return default, "default"


def _setting(env_var, local_name, default):
    return _resolve(env_var, local_name, default)[0]


def has_local_config():
    """Vrai si un fichier local_config.py a été chargé."""
    return _local is not None


def local_config_path():
    """Chemin du local_config.py chargé (ou None)."""
    return getattr(_local, "__file__", None) if _local is not None else None


def settings_origin():
    """Origine de chaque paramètre de connexion, pour l'affichage/diagnostic."""
    return {
        "host": _resolve("MYSQL_HOST", "MYSQL_HOST", _DEFAULT_MYSQL_HOST)[1],
        "database": _resolve("MYSQL_DATABASE", "MYSQL_DATABASE",
                             DEFAULT_DATABASE)[1],
        "user": _resolve("MYSQL_USER", "MYSQL_USER", _DEFAULT_MYSQL_USER)[1],
        "password": _resolve("MYSQL_PASS", "MYSQL_PASSWORD",
                             _DEFAULT_MYSQL_PASSWORD)[1],
    }


def mysql_host():
    """Hôte MySQL."""
    return _setting("MYSQL_HOST", "MYSQL_HOST", _DEFAULT_MYSQL_HOST)


def mysql_database():
    """Base MySQL."""
    return _setting("MYSQL_DATABASE", "MYSQL_DATABASE", DEFAULT_DATABASE)


def mysql_user():
    """Utilisateur MySQL."""
    return _setting("MYSQL_USER", "MYSQL_USER", _DEFAULT_MYSQL_USER)


def mysql_password():
    """Mot de passe MySQL.

    Jamais journalisé ni écrit dans les réglages de l'application.
    """
    return _setting("MYSQL_PASS", "MYSQL_PASSWORD", _DEFAULT_MYSQL_PASSWORD)


def _import_mysql_driver():
    """Importe le connecteur : mariadb en priorité, pymysql en repli."""
    try:
        import mariadb
        return "mariadb", mariadb
    except ImportError:
        pass
    try:
        import pymysql
        return "pymysql", pymysql
    except ImportError as exc:  # pragma: no cover - dépendance manquante
        raise DataSourceError(
            "The 'mariadb' module is required for the MySQL source "
            "(pip install mariadb). 'pymysql' is used as a fallback."
        ) from exc


def _available_driver():
    """Nom du connecteur disponible ('mariadb' / 'pymysql' / None)."""
    try:
        return _import_mysql_driver()[0]
    except DataSourceError:
        return None


def _connect_error_message(exc):
    """Message d'erreur de connexion, avec l'hôte, le connecteur et un indice."""
    text = str(exc)
    # Identifiants refusés : le serveur a répondu, seul le compte est en cause.
    if "access denied" in text.lower():
        where = ("local_config.py" if settings_origin()["password"]
                 == "local_config.py" else settings_origin()["password"])
        return (
            f"MySQL refused the credentials for user '{mysql_user()}' on "
            f"'{mysql_host()}'. The server is reachable — only the login "
            f"failed.\nPassword source: {where}. "
            f"local_config.py {'loaded' if has_local_config() else 'NOT found'}"
            f".\nMySQL accounts are per client host: the account may exist for "
            f"the server itself (as phpMyAdmin uses it) but not for your "
            f"workstation's IP.\nDetails: {exc}")
    driver = _available_driver()
    if driver == "mariadb":
        extra = ""
    else:
        extra = (" The 'mariadb' connector is not installed, so the pymysql "
                 "fallback (plain TCP) was used — run 'pip install mariadb' "
                 "to use the MariaDB connector.")
    return (f"Cannot connect to MySQL at host '{mysql_host()}' "
            f"(set the MYSQL_HOST environment variable to change it).{extra} "
            f"Details: {exc}")


class _MysqlSession:
    """Contexte de connexion (modèle fourni), curseur en mode dictionnaire.

    Le mot de passe reste en mémoire : il n'est ni écrit sur disque ni
    journalisé.
    """

    def __init__(self, config):
        self._config = config
        self.conn = None
        self.cursor = None

    def __enter__(self):
        user = self._config.get("user") or ""
        password = self._config.get("password") or ""
        database = self._config.get("database") or mysql_database()
        kind, driver = _import_mysql_driver()
        if kind == "mariadb":
            self.conn = driver.connect(
                host=mysql_host(), user=user, password=password,
                database=database, autocommit=True,
            )
            self.cursor = self.conn.cursor(dictionary=True)
        else:  # pymysql (repli)
            self.conn = driver.connect(
                host=mysql_host(), user=user, password=password,
                database=database, charset="utf8mb4",
                cursorclass=driver.cursors.DictCursor, autocommit=True,
            )
            self.cursor = self.conn.cursor()
        return self.cursor

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.cursor is not None:
                self.cursor.close()
        finally:
            if self.conn is not None:
                self.conn.close()


def test_mysql_connection(config):
    """Teste la connexion. Renvoie (ok: bool, message: str)."""
    try:
        with _MysqlSession(config) as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True, "Connection successful."
    except DataSourceError as exc:
        return False, str(exc)
    except Exception as exc:  # jamais le mot de passe dans le message
        return False, _connect_error_message(exc)


def load_from_mysql(config):
    """Charge assets/scenes/binds depuis MySQL/MariaDB.

    Requêtes statiques (aucune concaténation d'entrée utilisateur). Les
    tables sont lues d'un seul coup puis indexées en mémoire par graph_model.
    """
    assets, scenes, binds = {}, {}, []
    try:
        with _MysqlSession(config) as cursor:
            # SELECT * : récupère aussi les colonnes facultatives (name…).
            cursor.execute("SELECT * FROM assets")
            asset_rows = cursor.fetchall()
            date_column, format_spec = _asset_columns_spec(asset_rows)
            for row in asset_rows:
                aid = to_int(row.get("id"))
                if aid is None:
                    continue
                assets[aid] = _asset_from_mapping(row.get, date_column,
                                                  format_spec)

            # SELECT * : récupère aussi une éventuelle colonne « graphiste »
            # (artist/user/created_by…) sans échouer si elle est absente.
            cursor.execute("SELECT * FROM scenes")
            for row in cursor.fetchall():
                sid = to_int(row.get("id"))
                if sid is None:
                    continue
                scenes[sid] = _scene_from_mapping(row.get)

            cursor.execute("SELECT asset_id, scene_id, active FROM binds")
            for row in cursor.fetchall():
                aid = to_int(row.get("asset_id"))
                sid = to_int(row.get("scene_id"))
                if aid is None or sid is None:
                    continue
                binds.append((aid, sid, row.get("active")))
    except DataSourceError:
        raise
    except Exception as exc:
        raise DataSourceError(_connect_error_message(exc)) from exc
    return assets, scenes, binds


# ---------------------------------------------------------------------------
# Source 2 : fichiers CSV exportés par table depuis phpMyAdmin
# ---------------------------------------------------------------------------

def _read_csv_rows(path, table):
    """Lit un CSV (en-têtes en 1re ligne) et vérifie les colonnes requises."""
    try:
        f = open(path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise DataSourceError(
            f"Cannot open the \"{table}\" CSV: {exc}"
        ) from exc
    with f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise DataSourceError(
                f"\"{table}\" CSV is empty or unreadable: {path}")
        header = {(name or "").strip() for name in reader.fieldnames}
        missing = [c for c in REQUIRED_COLUMNS[table] if c not in header]
        if missing:
            raise DataSourceError(
                f"\"{table}\" CSV ({path}): missing columns: "
                f"{', '.join(missing)}."
            )
        # On renvoie une liste (le fichier est fermé à la sortie du with).
        rows = []
        for raw in reader:
            rows.append({(k or "").strip(): v for k, v in raw.items()})
        return rows


def load_from_csv(paths):
    """Charge depuis trois CSV.

    ``paths`` : dict avec les clés 'assets', 'scenes', 'binds' -> chemins.
    """
    for key in ("assets", "scenes", "binds"):
        if not paths.get(key):
            raise DataSourceError(f"Path for the \"{key}\" CSV is not set.")

    assets, scenes, binds = {}, {}, []

    asset_rows = _read_csv_rows(paths["assets"], "assets")
    date_column, format_spec = _asset_columns_spec(asset_rows)
    for row in asset_rows:
        aid = to_int(row.get("id"))
        if aid is None:
            continue
        assets[aid] = _asset_from_mapping(row.get, date_column, format_spec)

    for row in _read_csv_rows(paths["scenes"], "scenes"):
        sid = to_int(row.get("id"))
        if sid is None:
            continue
        scenes[sid] = _scene_from_mapping(row.get)

    for row in _read_csv_rows(paths["binds"], "binds"):
        aid = to_int(row.get("asset_id"))
        sid = to_int(row.get("scene_id"))
        if aid is None or sid is None:
            continue
        binds.append((aid, sid, norm_str(row.get("active"))))

    return assets, scenes, binds


def detect_csv_files(folder):
    """Cherche assets.csv / scenes.csv / binds.csv dans un dossier.

    Renvoie un dict {table: chemin} pour les fichiers trouvés (correspondance
    insensible à la casse).
    """
    import os

    found = {}
    try:
        entries = os.listdir(folder)
    except OSError:
        return found
    lower = {name.lower(): name for name in entries}
    for table in ("assets", "scenes", "binds"):
        candidate = f"{table}.csv"
        if candidate in lower:
            found[table] = os.path.join(folder, lower[candidate])
    return found


# ---------------------------------------------------------------------------
# Source 3 : dump mysqldump (.sql) — parsing en streaming, ligne par ligne
# ---------------------------------------------------------------------------

# En-tête d'un INSERT : capture le nom de table, la liste de colonnes et la
# fin de ligne (qui peut déjà contenir des tuples de données).
_INSERT_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+`?(?P<table>\w+)`?\s*"
    r"\((?P<cols>[^)]*)\)\s*VALUES\b(?P<tail>.*)$",
    re.IGNORECASE,
)
# INSERT sans liste de colonnes (on ne peut pas mapper par nom -> ignoré).
_INSERT_NOCOLS_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+`?(?P<table>\w+)`?\s*VALUES\b",
    re.IGNORECASE,
)


def _build_colmap(cols_str):
    """`col1`, `col2`, ... -> {nom: index}. Robuste à l'ordre des colonnes."""
    cols = [c.strip().strip("`").strip() for c in cols_str.split(",")]
    return {name: i for i, name in enumerate(cols) if name}


def _split_tuples(text):
    """Découpe une chaîne en sous-chaînes ``(...)`` de premier niveau.

    Respecte les chaînes entre apostrophes (doublage '' et échappement \\').
    Gère aussi bien un tuple par ligne que plusieurs (extended insert).
    """
    out = []
    depth = 0
    in_str = False
    start = None
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2  # échappement backslash : saute le caractère suivant
                continue
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2  # apostrophe doublée -> reste dans la chaîne
                    continue
                in_str = False
        else:
            if c == "'":
                in_str = True
            elif c == "(":
                if depth == 0:
                    start = i
                depth += 1
            elif c == ")":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        out.append(text[start:i + 1])
                        start = None
        i += 1
    return out


def _parse_values(inner):
    """Découpe l'intérieur d'un tuple en valeurs (module csv, quotechar=\"'\")."""
    reader = csv.reader(
        [inner],
        delimiter=",",
        quotechar="'",
        doublequote=True,
        skipinitialspace=True,
    )
    return next(reader)


def load_from_sql_dump(path):
    """Extrait assets/scenes/binds d'un dump mysqldump, en streaming.

    - lecture ligne par ligne (le fichier peut faire des dizaines de Mo) ;
    - repérage des blocs INSERT INTO `assets|scenes|binds` ;
    - mapping colonnes -> index depuis l'en-tête de chaque INSERT ;
    - lignes non parseables ignorées proprement (comptées).
    """
    targets = ("assets", "scenes", "binds")
    assets, scenes, binds = {}, {}, []
    stats = {"skipped": 0, "rows": 0}

    current = None   # table cible en cours, ou None
    colmap = None
    # Le dump est lu en streaming : les colonnes date et format sont résolues
    # sur la première ligne d'assets rencontrée, puis réutilisées.
    date_state = {"column": None, "format": ("", "")}

    def store(vals):
        """Range un tuple de valeurs dans la bonne table selon ``current``."""
        def get(name):
            idx = colmap.get(name)
            if idx is None or idx >= len(vals):
                return None
            return vals[idx]

        if current == "assets":
            aid = to_int(get("id"))
            if aid is None:
                raise ValueError("id asset manquant")
            if date_state["column"] is None:
                date_state["format"] = resolve_format_column(colmap, [get])
                date_state["column"] = _remember_asset_schema(
                    colmap, resolve_date_column(colmap, [get]),
                    date_state["format"])
            assets[aid] = _asset_from_mapping(get, date_state["column"],
                                              date_state["format"])
        elif current == "scenes":
            sid = to_int(get("id"))
            if sid is None:
                raise ValueError("id scene manquant")
            scenes[sid] = _scene_from_mapping(get)
        elif current == "binds":
            aid = to_int(get("asset_id"))
            sid = to_int(get("scene_id"))
            if aid is None or sid is None:
                raise ValueError("asset_id/scene_id manquant")
            binds.append((aid, sid, norm_str(get("active"))))

    def consume(data_str):
        for tup in _split_tuples(data_str):
            inner = tup[1:-1]
            try:
                vals = _parse_values(inner)
                store(vals)
                stats["rows"] += 1
            except Exception:
                stats["skipped"] += 1

    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DataSourceError(f"Cannot open the SQL dump: {exc}") from exc

    with f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s[:11].upper() == "INSERT INTO":
                m = _INSERT_RE.match(s)
                if m and m.group("table").lower() in targets:
                    current = m.group("table").lower()
                    colmap = _build_colmap(m.group("cols"))
                    tail = m.group("tail").strip()
                    if tail:
                        consume(tail)
                else:
                    # Autre table, ou INSERT sans colonnes : on sort du mode.
                    current = None
                    colmap = None
                continue
            if current is not None and s.startswith("("):
                consume(s)
                continue
            # Toute autre instruction termine le bloc courant.
            current = None
            colmap = None

    if not assets and not scenes and not binds:
        raise DataSourceError(
            "No assets/scenes/binds data found in the SQL dump. "
            "Make sure it is an export of the expected database."
        )
    return assets, scenes, binds
