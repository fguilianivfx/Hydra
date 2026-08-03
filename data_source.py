"""Chargement des données depuis les trois sources supportées.

Ce module expose une interface unique qui renvoie toujours les mêmes
structures en mémoire, quelle que soit la provenance des données :

    assets : dict[int, dict]
        clés du dict interne : project, entity_name, task_name, av_name,
        node_name, version (int | None)
    scenes : dict[int, dict]
        clés du dict interne : project, entity_name, task_name, av_name,
        version (int | None), name
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


def _asset_from_mapping(get):
    """Construit un enregistrement asset depuis un accès par nom de colonne."""
    return {
        "project": norm_str(get("project")),
        "entity_name": norm_str(get("entity_name")),
        "task_name": norm_str(get("task_name")),
        "av_name": norm_str(get("av_name")),
        "node_name": norm_str(get("node_name")),
        "version": to_int(get("version")),
    }


# Colonnes possibles pour le graphiste ayant publié la scène (facultatif).
_ARTIST_COLUMNS = ("artist", "user", "username", "created_by", "author",
                   "publisher", "login", "owner")


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
    }


# ---------------------------------------------------------------------------
# Source 1 : MySQL / MariaDB
# ---------------------------------------------------------------------------
# L'hôte n'est PAS demandé dans l'UI : il vient de la variable
# d'environnement MYSQL_HOST, avec le serveur du studio en défaut. Seuls
# l'utilisateur et le mot de passe sont saisis. Base par défaut :
# « dd_assets_tracking ».

_DEFAULT_MYSQL_HOST = "dd-intra"
DEFAULT_DATABASE = "dd_assets_tracking"


def mysql_host():
    """Hôte MySQL : $MYSQL_HOST, sinon le serveur du studio."""
    return os.environ.get("MYSQL_HOST", _DEFAULT_MYSQL_HOST)


def mysql_database():
    """Base MySQL : $MYSQL_DATABASE, sinon dd_assets_tracking."""
    return os.environ.get("MYSQL_DATABASE", DEFAULT_DATABASE)


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
            cursor.execute(
                "SELECT id, project, entity_name, task_name, av_name, "
                "node_name, version FROM assets"
            )
            for row in cursor.fetchall():
                aid = to_int(row.get("id"))
                if aid is None:
                    continue
                assets[aid] = _asset_from_mapping(row.get)

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

    for row in _read_csv_rows(paths["assets"], "assets"):
        aid = to_int(row.get("id"))
        if aid is None:
            continue
        assets[aid] = _asset_from_mapping(row.get)

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
            assets[aid] = _asset_from_mapping(get)
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
