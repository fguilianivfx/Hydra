# Hydra — Graphe interactif de dépendances de scènes

Application de bureau **native** (Python + **PySide6**) qui, à partir du nom
d'une scène et d'une source de données, affiche un **graphe interactif** des
scènes dont elle dépend, coloré selon leur fraîcheur.

* un **rectangle** = une scène (une version) : en titre, le **nom de la table**
  (`scenes.name`, ou l'identité reconstruite pour un nœud issu d'assets) suivi
  de la version — ex. `tmp_024C_0060_lighting_main (v010)` ; puis **un output
  par ligne** — chaque output en **vert** s'il est à sa dernière version
  publiée, en **rouge** sinon avec la version disponible (`rendercam ⚠
  (v001 → v002)`) ;
* une **ligne par type de tâche**, en deux niveaux (**asset** puis **shot**)
  séparés par un trait, le flux descendant ; lignes **réordonnables** en
  glissant leur poignée à gauche ; la **scène interrogée** reste tout en bas ;
* couleur du nœud par **propagation** : **vert** = tous les inputs à jour ·
  **rouge** = importe au moins un asset supplanté · **jaune** = obsolète *par
  héritage* seulement (ses inputs sont à jour mais un ancêtre est obsolète) ;
* nœuds **déplaçables horizontalement** (le Y reste verrouillé sur la ligne
  de la tâche) ; **survol** = dépendances directes mises en évidence + nom du
  **graphiste** ; **molette** = zoom ; **bouton du milieu** = déplacement.

Le rendu utilise `QGraphicsView` / `QGraphicsScene` avec des `QGraphicsItem`
personnalisés — **aucune librairie de graphe externe**.

![Aperçu](docs/preview.png)

---

## Installation

Python 3.9+ recommandé.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` contient `PySide6`, `mariadb` et `pymysql`. Le connecteur
`mariadb` (celui utilisé pour se connecter) nécessite la bibliothèque système
**MariaDB Connector/C** ; si `mariadb` n'est pas importable, l'appli bascule
automatiquement sur `pymysql` (100 % Python).

> Le module `keyring` est **optionnel** : s'il est installé, la case
> « Remember » enregistre le mot de passe MySQL dans le **trousseau
> système**. Sans lui, **aucun mot de passe n'est jamais écrit sur disque**.

## Lancement

```bash
python main.py
```

La fenêtre est un **split vertical** : les saisies (source de données + nom de
scène) à **gauche**, le graphe à **droite**.

1. Choisissez une **source de données** (onglets à gauche).
2. Saisissez le **nom de la scène**, ex. `qua_077_02000_comp_v019`.
3. Cliquez **Grapher**.

Pour un essai immédiat sans base de données, utilisez le jeu d'exemple fourni
dans [`sample_data/`](sample_data) : onglet **Fichiers CSV** → *Dossier…* →
sélectionnez `sample_data`, puis graphez `qua_077_02000_comp_v019`.
(Le même jeu est disponible en dump SQL : `sample_data/dump.sql`.)

---

## Les trois sources de données

Toutes renvoient les mêmes structures en mémoire (voir `data_source.py`), donc
toute la logique de graphe est indépendante de la provenance.

### 1. MySQL / MariaDB

Connexion directe au serveur MySQL/MariaDB (via le connecteur `mariadb`,
`autocommit=True`, curseur en mode dictionnaire).

| Champ         | Défaut               |
|---------------|----------------------|
| Database      | `dd_assets_tracking` |
| User          | *(login)*            |
| Password      | *(masqué)*           |

* **L'hôte n'est pas demandé** : il est lu depuis la variable
  d'environnement **`MYSQL_HOST`** (défaut `dd-intra.ddprs.net`). L'onglet
  affiche l'hôte effectivement utilisé.
* Bouton **Test connection** (retour succès / erreur) avant de charger.
* Le mot de passe **reste en mémoire uniquement** : il n'est ni écrit sur
  disque ni journalisé (sauf trousseau système via `keyring`, sur demande).
* Les requêtes sont **statiques** — aucune concaténation d'entrée utilisateur.

### 2. Fichiers CSV (hors-ligne)

Trois fichiers correspondant à l'export **par table** depuis phpMyAdmin
(*Exporter → CSV*, noms de colonnes en 1re ligne) : `assets.csv`,
`scenes.csv`, `binds.csv`.

Sélectionnez-les un par un, ou utilisez **Dossier…** pour les détecter
automatiquement dans un répertoire.

### 3. Dump SQL unique (`.sql`) — hors-ligne, le plus simple

Un export **mysqldump** complet (phpMyAdmin → *Exporter → SQL*). Un seul
fichier auto-suffisant ; l'appli en extrait les tables `assets`, `scenes`,
`binds`. Le dump est lu **en streaming, ligne par ligne** (adapté aux gros
fichiers de plusieurs dizaines de Mo) ; les colonnes sont mappées **par nom**
depuis l'en-tête de chaque `INSERT`, et les rares lignes non parseables sont
ignorées proprement.

---

## Formats CSV attendus

Colonnes **requises** (les autres sont ignorées) :

* **`assets.csv`** : `id, project, entity_name, task_name, av_name,
  node_name, version`
* **`scenes.csv`** : `id, name, project, entity_name, task_name, av_name,
  version` — le titre du rectangle reprend `name` + version ; une colonne
  **graphiste** facultative (`artist`, `user`, `created_by`…) alimente le survol.
* **`binds.csv`** : `asset_id, scene_id, active`

Conversions : `version` → entier (vide / non numérique → *inconnu*) ;
`av_name` vide = `""` ; un *bind* est actif si `active ∈ ('1', 1)`.
Encodage **UTF-8** (le BOM éventuel est géré).

Si une colonne requise manque, un message clair l'indique.

---

## Nom de scène (entrée)

Format : `{prefix}_{entity}_{task}[_{av}]_v{NNN}`, ex.
`qua_077_02000_comp_v019` (préfixe `qua`, entité `077_02000`, tâche `comp`,
av absent, version `19`).

* la **résolution** se fait sur les colonnes structurées `(entity_name,
  task_name, av_name, version)`, **pas** sur `name` (qui peut être « sale ») ;
  le **nom du graphiste** au survol vient d'une colonne dédiée si elle existe,
  sinon il est déduit des segments non canoniques de `name` ;
* `comp` est mappé sur `task_name = "compositing"` ;
* un `av` absent du nom correspond à `av_name = ""` ;
* en dernier recours, un filtrage par sous-chaîne sur `name` est tenté ;
* si aucune scène ne correspond, l'erreur est affichée clairement.

---

## Sémantique & couleurs

Trois tables comptent : `assets`, `scenes`, `binds`.

* `assets` — une ligne = une **version publiée d'un output**. Identité d'un
  *stream* : `(project, entity_name, task_name, av_name, node_name)`.
* `scenes` — une ligne = une **version d'une scène** de travail. Identité :
  `(project, entity_name, task_name, av_name)`.
* `binds` — `(asset_id, scene_id, active)` : l'asset est **importé** dans la
  scène. Seuls les binds **actifs** comptent. C'est la **source de vérité**
  des dépendances.

> La table `assets_parents` est **ignorée** (cache partiel/incohérent) : toute
> la récursion passe par `scenes` + `binds`.

**Détail des outputs (dans le rectangle)** — un output par ligne, avec sa
fraîcheur individuelle (comparaison à la **dernière version exportée de cet
asset**, jamais à la version de scène) :

* à jour → **vert** : `location (v001)` ;
* périmé → **rouge** : `rendercam ⚠ (v001 → v002)` (version courante → version
  disponible).

**Couleur du nœud — par propagation, à trois états** :

Un *input* est un asset importé (via un bind actif) ; il est « à jour » si sa
version est la dernière version exportée de cet asset.

* **vert** — tous les inputs sont à jour *et* aucun ancêtre n'est obsolète ;
* **rouge** — le nœud importe au moins un asset supplanté (input périmé) ;
* **jaune** — obsolète **par héritage uniquement** : ses inputs directs sont à
  jour, mais un de ses ancêtres (amont) est obsolète.

L'obsolescence se **propage vers l'aval** : un nœud construit, même
indirectement, sur une dépendance périmée est signalé (rouge s'il importe
directement un asset périmé, sinon jaune).

> Nuance : le nœud à l'origine d'une republication (ex. un `modeling` dont un
> `v007` existe alors que le graphe tire le `v005`) reste **vert** — ses
> propres inputs sont sains — mais ses lignes d'output apparaissent en **rouge**
> (`chaise ⚠ (v005 → v007)`), et tout ce qui l'importe passe au rouge.

---

## Interactions

| Action                          | Effet                                      |
|---------------------------------|--------------------------------------------|
| **Glisser** un nœud             | Réordonner (déplacement **horizontal** seul)|
| **Glisser la poignée** de ligne (à gauche) | **Réordonner les lignes** de tâche (vertical) |
| **Survol** d'un nœud            | Dépendances directes + graphiste + inputs/outputs avec versions `(vXXX)` ou `(vXXX → vYYY)` |
| **Molette**                     | Zoom (ancré sous le curseur)               |
| **Bouton du milieu** + glisser  | Déplacement (pan)                          |
| **Recentrer** (`Ctrl+0`)        | Ajuste le zoom pour tout voir              |
| **Réinitialiser la disposition**| Rétablit l'ordre des lignes et les colonnes|

---

## Structure du projet

| Fichier           | Rôle                                                         |
|-------------------|-------------------------------------------------------------|
| `main.py`         | Point d'entrée, fenêtre principale, panneau de source, UI.  |
| `data_source.py`  | `load_from_mysql` / `load_from_csv` / `load_from_sql_dump`. |
| `graph_model.py`  | Résolution du nom, parcours scenes+binds, regroupement, statut, disposition. |
| `graph_view.py`   | `QGraphicsScene`/`QGraphicsView`, items nœud & arête, drag horizontal, survol, zoom/pan. |
| `sample_data/`    | Jeu d'exemple (CSV + dump SQL) pour un essai immédiat.       |

## Ordre des lignes de tâche

Ordre par défaut, de haut en bas, en **deux niveaux** séparés par un trait :

```
niveau asset :   modeling · shading · rig
──────────────── (séparateur) ────────────────
niveau shot :    tracking · layout · anim · fx · lighting · compositing
```

* les tâches **absentes de cette liste** sont **intercalées** d'après le
  graphe, entre leurs inputs (au-dessus) et leurs outputs (en dessous) ;
* la **scène interrogée** reste tout en bas ;
* l'ordre est **modifiable** : glissez la **poignée** à gauche d'une ligne pour
  la déplacer verticalement ; le séparateur asset/shot se replace tout seul.
