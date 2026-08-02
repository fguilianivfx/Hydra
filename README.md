# Hydra — Graphe interactif de dépendances de scènes

Application de bureau **native** (Python + **PySide6**) qui, à partir du nom
d'une scène et d'une source de données, affiche un **graphe interactif** des
scènes dont elle dépend, coloré selon leur fraîcheur.

* un **rectangle** = une scène (une version), avec le nom complet en haut et
  la liste de ses *nodes* (outputs) regroupés en dessous ;
* une **ligne par type de tâche**, les sources en haut et le compositing en
  bas (le flux descend) ;
* **vert** = scène à jour · **rouge** = au moins un output supplanté par une
  version plus récente ;
* nœuds **déplaçables horizontalement** (le Y reste verrouillé sur la ligne
  de la tâche) ; **survol** = mise en évidence des dépendances directes ;
  **molette** = zoom ; **bouton du milieu** = déplacement (pan).

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

`requirements.txt` contient uniquement `PySide6` et `pymysql`.

> Le module `keyring` est **optionnel** : s'il est installé, la case
> « Se souvenir » enregistre le mot de passe MySQL dans le **trousseau
> système**. Sans lui, **aucun mot de passe n'est jamais écrit sur disque**.

## Lancement

```bash
python main.py
```

1. Choisissez une **source de données** (onglets en haut).
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

### 1. Base phpMyAdmin (MySQL / MariaDB)

Connexion directe au serveur MySQL/MariaDB administré par phpMyAdmin.

| Champ         | Défaut               |
|---------------|----------------------|
| Hôte          | `127.0.0.1`          |
| Port          | `3306`               |
| Base          | `dd_assets_tracking` |
| Utilisateur   | *(login)*            |
| Mot de passe  | *(masqué)*           |

* Bouton **Tester la connexion** (retour succès / erreur) avant de charger.
* Le mot de passe **reste en mémoire uniquement** : il n'est ni écrit sur
  disque ni journalisé (sauf trousseau système via `keyring`, sur demande).
* Les requêtes sont **statiques / paramétrées** — aucune concaténation
  d'entrée utilisateur.

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
  version`
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

* la colonne `scenes.name` est « sale » (noms d'artistes, suffixes) : la
  résolution se fait sur les colonnes structurées `(entity_name, task_name,
  av_name, version)`, **pas** sur `name` ;
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

**Couleur d'un nœud** :

* scène **à outputs** → **rouge** si au moins un de ses outputs est supplanté
  (il existe une version plus récente du *même* node sur son stream) ; sinon
  vert. Le test est fait **au niveau output**, pas au niveau version max de la
  scène (une `v22` ne paraît pas périmée juste parce qu'une `v23` a republié
  un *autre* node) ;
* scène **sans output suivi** (la compo) → rouge si sa version est inférieure
  à la dernière version connue de son stream de scène.

Chaque nœud indique aussi s'il n'est pas la dernière version
(« ⚠ dernière : v020 »).

---

## Interactions

| Action                         | Effet                                       |
|--------------------------------|---------------------------------------------|
| **Glisser** un nœud            | Réordonner (déplacement **horizontal** seul)|
| **Survol** d'un nœud           | Met en évidence parents & enfants directs   |
| **Molette**                    | Zoom (ancré sous le curseur)                |
| **Bouton du milieu** + glisser | Déplacement (pan)                           |
| **Recentrer** (`Ctrl+0`)       | Ajuste le zoom pour tout voir               |
| **Réinitialiser la disposition** | Replace les nœuds dans leurs colonnes     |

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

Du haut (sources) vers le bas (compositing) :

```
modeling · tracking · rigging · layout · shading · animation · lighting · compositing
```

Une tâche inconnue est placée tout en bas.
