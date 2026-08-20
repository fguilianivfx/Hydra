# Dedale — Graphe interactif de dépendances de scènes

Application de bureau **native** (Python + **PySide6**) qui, à partir du nom
d'une scène et d'une source de données, affiche un **graphe interactif** des
scènes dont elle dépend, coloré selon leur fraîcheur.

* un **rectangle** = une scène (une version) : en titre, le **nom de la table**
  (`scenes.name`, ou l'identité reconstruite pour un nœud issu d'assets) suivi
  de la version — ex. `tmp_024C_0060_lighting_main (v010)` ; puis **un output
  par ligne** — chaque output en **vert** s'il est à sa dernière version
  publiée, en **rouge** sinon avec la version disponible (`rendercam ⚠
  (v001 → v002)`). Un rectangle ne liste que les outputs qui **alimentent
  réellement** le graphe : beaucoup de scènes bindent aussi leurs *propres*
  publications, et ces auto-binds ne relient rien — les afficher revenait à
  marquer « périmé » un asset que personne n'importe. **Survoler le rectangle**
  indique, sous chaque output, **quelle(s) scène(s) l'importent** — utile quand
  une ligne semble en trop : elle alimente souvent une autre version de la même
  task (une bibliothèque de matériaux chaînée d'une version à l'autre, par
  exemple), pas la scène interrogée. Le nom affiché vient de
  `assets.name` (et non de la clé de flux `node_name`), **débarrassé du nom de
  la scène productrice** que le titre donne déjà — avec ou sans le code du
  show : `28_rues_armel_shading_bank_abcdef_matlib` → `matlib`,
  `qua_28_rues_armel_shading_bank_abcdef_building_bank_abcdef` →
  `building_bank_abcdef`. Il est écrit sur ses **20 premiers caractères**
  suivis de `...` s'il est tronqué ;
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

Le module `keyring` active la case **« Remember »**, qui enregistre le mot de
passe MySQL dans le **trousseau système** (Windows Credential Manager, macOS
Keychain, Secret Service sous Linux) :

* cocher la case l'enregistre **immédiatement** (puis à chaque connexion
  réussie et à la fermeture) ;
* **décocher supprime** l'entrée stockée ;
* le mot de passe n'est **jamais** écrit dans le fichier de réglages de
  l'appli. Sans `keyring`, la case est désactivée et rien n'est persisté.

## Lancement

```bash
python main.py
```

La fenêtre est un **split vertical** : les saisies (source de données + nom de
scène) à **gauche**, le graphe à **droite**.

1. Choisissez une **source de données** (onglets à gauche).
2. Saisissez le **nom de la scène**, ex. `qua_077_02000_comp_v019`.
3. Cliquez **Grapher**.

### Démarrer sur un travail précis (`-sc`, `-gr`, `-pr`)

```bat
:: graphe une scène
C:\Python39\python.exe .\main.py -sc qua_077_02000_comp_v022

:: contrôle les scènes d'un graphiste
C:\Python39\python.exe .\main.py -gr "sebastien ginestra" -pr qua
```

| Option | Forme longue | Effet au lancement |
|--------|--------------|--------------------|
| `-sc NOM`  | `--scene`    | onglet *Scene to graph*, scène **déjà graphée** |
| `-gr NOM`  | `--graphist` | onglet *Graphist to graph*, **Check scenes déjà passé** |
| `-pr CODE` | `--project`  | projet du contrôle : code court (`qua`) ou nom complet (`quasimodo_26`) |
| `-sb 1`    | `--sandbox`  | écrit les mutes dans un **fichier d'essai** au lieu de la base |
| `-debug 1` | `--debug`    | affiche les sources **CSV** et **SQL dump**, masquées en temps normal |

Le travail démarre **dès que la fenêtre est à l'écran** : le chargement se
voit, au lieu d'un démarrage figé.

* **sans option, rien ne change** : l'outil se lance exactement comme
  aujourd'hui, sur les dernières saisies, sans rien lancer ;
* les valeurs de la ligne de commande **l'emportent** sur celles mémorisées,
  et **complètent** ce qui manque : `-gr` seul suffit si le projet de la
  dernière session convient ;
* s'il manque encore le graphiste ou le projet, l'onglet s'ouvre quand même
  et la barre d'état dit lequel — **aucune boîte modale au démarrage** ;
* `-sc` et `-gr` se **combinent** : le graphe est construit *et* les scènes
  listées ; l'onglet graphiste passe devant, le graphe restant visible à
  droite ;
* la **source de données** reste celle de la dernière session (onglet et
  chemins mémorisés) : ces options ne choisissent que le travail à faire ;
* une scène introuvable affiche l'erreur habituelle et **laisse l'outil
  utilisable** : on corrige la saisie et on relance ;
* `-sb` et `-debug` acceptent `1`/`0` (ou `true`/`false`, `yes`/`no`) et
  valent `1` s'ils sont passés **sans valeur** ; absents, ils valent `0` ;
* les autres arguments sont **transmis à Qt** (`-platform`, `-style`…) plutôt
  que rejetés, et `-h` affiche l'aide.

En usage courant, le panneau **Data source** ne montre donc que **MySQL** :
les sources CSV et SQL dump servent à la mise au point et n'apparaissent
qu'avec `-debug 1`. Si la dernière session s'était terminée sur l'une
d'elles, l'outil rouvre sur MySQL plutôt que sur une source invisible.

> **Sur l'exécutable compilé, mêmes options** : `Dedale.exe -sc
> qua_077_02000_comp_v022`. Compilé en `--windowed` il n'y a pas de console :
> `-h`, ou une option mal formée (`-sc` sans nom), s'afficheraient dans le
> vide et l'exe se refermerait **sans un mot**. Le texte d'argparse est donc
> récupéré et montré dans une **boîte de dialogue** ; lancé depuis un
> terminal, il s'écrit normalement dans le terminal, avec le code de sortie
> habituel (`0` pour l'aide, `2` pour une erreur).

### Thème sombre

Toute la fenêtre est sombre, accordée au graphe qui l'était déjà : palette
Fusion, champs et listes en `#1b1f26`, **barre d'état en noir** (`#0f1216`)
et, sous Windows 10/11, **barre de titre sombre** elle aussi — Qt ne
l'expose pas, elle passe par `DwmSetWindowAttribute` (sans effet ailleurs, et
jamais bloquant). Les couleurs de version ont été éclaircies pour rester
lisibles : vert `#4ec97e`, rouge `#ff6f61`, bleu des assets non importés
`#5aa9ff`. L'indicateur des cases à cocher est redessiné, sans quoi une case
décochée devient invisible sur fond sombre.

### Packaging (exécutable Windows)

Placez **`Dedale.ico`** à côté de `main.py`, puis :

```bat
python -m PyInstaller --onefile --windowed .\main.py --icon Dedale.ico --add-data "Dedale.ico;."
```

* `--icon` fixe l'icône **du fichier `.exe`** ;
* `--add-data "Dedale.ico;."` embarque l'icône pour qu'elle serve aussi
  **d'icône de fenêtre et de barre des tâches** à l'exécution. En `--onefile`,
  PyInstaller l'extrait dans un dossier temporaire : `resource_path()`
  (dans `main.py`) la retrouve via `sys._MEIPASS`, et retombe sur le dossier
  du script quand on lance depuis les sources.

Si `Dedale.ico` est absent, l'application démarre normalement, simplement sans
icône personnalisée.

Pour un essai immédiat sans base de données, lancez avec **`-debug 1`** (les
sources hors MySQL sont masquées sans elle) et utilisez le jeu d'exemple de
[`sample_data/`](sample_data) : onglet **CSV** → *Dossier…* → sélectionnez
`sample_data`, puis graphez `qua_077_02000_comp_v019`. (Le même jeu est
disponible en dump SQL : `sample_data/dump.sql`.)

---

## Les deux outils

Le panneau de gauche propose deux onglets, qui partagent la même source de
données.

### « Scene to graph »

L'outil historique : saisir un nom de scène, cliquer **Graph**, explorer le
graphe de dépendances (voir le reste de ce document).

### « Graphist to graph »

Contrôle du travail d'un graphiste — reprend la sémantique de l'outil en ligne
de commande `checkGraph` :

| Champ        | Rôle                                                       |
|--------------|------------------------------------------------------------|
| Graphist     | nom du graphiste (colonne `scenes.author`)                  |
| Project      | **code court** (`tem`) ou nom complet (`tempete_26`), en préfixe |
| Scenes task  | ne liste que les scènes du graphiste **de ces tasks** : `all` (défaut) ou une liste séparée par espaces/virgules (`fx lighting`) |
| Filter assets tasks | *filtre d'affichage*, **sous les cases à cocher** : restreint les **assets** listés — `all` ou `fx anim tracking` |

> Ne pas confondre : **Scenes task** choisit *quelles scènes* apparaissent,
> **Filter assets tasks** restreint *quels assets* sont listés sous chacune.

> La colonne `scenes.project` contient le nom complet du show (`tempete_26`)
> alors que les **noms de scènes** utilisent son **code court** (`tem`). Les
> deux sont acceptés dans le champ *Project*, et c'est toujours le **code
> court** qui sert à reconstruire un nom de scène exploitable par l'outil
> « Scene to graph ».

**Check scenes** liste, **par ordre alphabétique**, toutes les scènes du projet
assignées au graphiste, et vérifie pour chacune — dans sa **dernière version** —
si ses imports sont à jour. Un import est *obsolète* si son flux d'output
possède une version publiée plus récente que celle bindée.

* le contrôle porte **toujours sur toutes les tasks d'assets** ; le champ
  *Filter assets tasks* ne fait que **filtrer l'affichage** — il s'applique donc
  instantanément, sans relancer le contrôle ;
* le nom du graphiste est cherché à l'identique, puis de façon **approchée sur
  le nom de famille** (`ginestra` → `sebastien ginestra`) ;
* chaque ligne d'asset affiche le **nom complet de la scène productrice**
  (`tem_024c_0035_fx_wave · arbre_cyprin`) ; les noms de scènes sont en
  **gras** et **repliés par défaut** — le triangle déplie le détail ;
* une fois dépliée, une scène liste **tous** ses imports : les périmés en
  **rouge** (`v009 → v011`) et ceux **à jour en vert** (`v011`) ;
* **survoler une ligne d'asset** affiche sa task, sa version, sa **date
  d'export** et le **graphiste qui l'a publié** (colonnes facultatives de la
  table `assets` : `date`/`created_at`… et `author`/`user`…) ;
* chaque ligne d'asset porte un bouton **×** (*mute*) qui masque la ligne ; les
  mutes survivent à un nouveau *Check scenes* et **Unmute all** les rétablit.
  Muter un asset ou changer une option **ne replie pas** les triangles déjà
  ouverts ;
* *Shots only* (coché par défaut) ne garde que les scènes de **plans**. Le tri
  se fait sur le **nom de l'entité**, pas sur la task : un plan se nomme
  `<séquence>_<numéro>` (`077_0100`, `024C_0035`, `seq010_sh0020` — les deux
  derniers segments comportent des chiffres) tandis qu'un asset porte un nom
  (`chaise`, `caravane_fx`, `robot_01`). La task seule ne suffisait pas : un
  asset peut porter une task **inconnue** (`grooming`, `sculpt`) voire une task
  de **niveau shot** (`caravane_fx` en task `fx`), et ces scènes ressortaient à
  tort. Une task de niveau asset portée par un plan reste écartée, une task
  inconnue portée par un plan est **conservée** — jamais de travail masqué par
  erreur. Si **aucune** entité du projet ne suit cette convention de nommage,
  l'outil retombe sur le niveau de la task plutôt que de vider la liste ;
* *Only scenes to update* masque les scènes entièrement à jour ;
* *Show assets up to date* (cochée par défaut) affiche ou masque les imports
  déjà à leur dernière version (les lignes vertes). Les masquer ne change ni
  le statut des scènes ni le compteur d'imports vérifiés ;
* *Show assets not imported*, avec ses deux champs à sa droite — **Tasks** et
  **Formats**, `all` par défaut : ils restreignent les **lignes bleues** à ces
  tasks et à ces formats d'export (`exr`, `abc`, `bgeo.sc`…), en plus du
  *Filter assets tasks* global. Pratique pour ne voir que ce qui manque d'une
  task ou d'un type de fichier donné, sans toucher au reste de la liste ; les
  deux se cumulent, la syntaxe est la même (espaces ou virgules), et le format
  de chaque ligne figure dans son info-bulle. Cette option liste, **en
  bleu**, les assets publiés sur
  le **plan** que la scène **n'importe pas**, **une seule ligne par asset, à sa
  dernière version publiée** — même quand plusieurs flux (variantes d'un même
  node) aboutissent au même libellé, seule la version la plus récente est
  affichée. Ils
  sont purement informatifs : ils **ne rendent jamais une scène obsolète**, ne
  sont pas comptés dans « to update », et n'entraînent pas l'affichage d'une
  scène sous *Only scenes to update*. Le filtre *Filter assets tasks* et le
  bouton **×** s'y appliquent aussi ;
* **double-cliquer une scène** bascule sur l'onglet « Scene to graph » et la
  graphe directement.

Les mutes de cette liste (outil graphiste) sont **en mémoire uniquement** :
rien n'est écrit — seuls les mutes de **liens** du graphe (clic droit) sont
enregistrés, voir *Muter des assets sur un lien*.

#### « What's up ? »

Le bouton **What's up ?**, à gauche de *Unmute all*, bascule la même liste
d'un regroupement **par scène** à un regroupement **par date de publication**.
Les filtres sont rigoureusement identiques (mutes, *Filter assets tasks*,
*Only scenes to update*, *Show assets up to date*, *Show assets not imported*) :
seuls les groupes changent.

* les groupes sont **Today**, **Yesterday**, puis les dates ISO de la plus
  récente à la plus ancienne ; les assets sans date lisible ferment la marche
  sous *Unknown date* ;
* chaque date est un **triangle de dépliage**, replié par défaut, et porte le
  nombre d'assets ;
* la date retenue est celle de la **version qui fait la nouveauté** : pour un
  import périmé, c'est la date de la version **qui le périme** (`v005`), pas
  celle de la version encore utilisée (`v003`). L'info-bulle rappelle les deux
  ainsi que le graphiste qui a publié chacune ;
* un asset importé par plusieurs scènes du graphiste n'apparaît **qu'une fois** ;
  l'info-bulle liste les scènes concernées et le bouton **×** les mute toutes.

**D'où vient la date ?** De la table `assets`. Le nom de la colonne varie d'un
studio à l'autre : elle est donc **détectée automatiquement**, dans cet ordre —
un nom connu (`date`, `created_at`, `publish_date`…), puis un nom évocateur
(contenant `date`, `time`, `creat`, `publi`, `export`, `updat`…), puis, en
dernier recours, **toute colonne dont les valeurs ressemblent à des dates**
(`2026-08-08 17:49`, `08/08/2026`, ou un `DATETIME` MySQL). Une colonne connue
mais vide ne l'emporte jamais sur une colonne réellement remplie, et les
colonnes d'identité (`id`, `version`, `name`, `node_name`…) sont exclues — un
intervalle de frames `1001-1240` ne peut donc pas passer pour une date.

Si aucune colonne exploitable n'existe, tout se retrouve sous **Unknown date**
et l'outil le **dit** : le résumé l'indique et l'info-bulle du groupe liste les
colonnes réellement vues dans `assets`.

---

## Les trois sources de données

Toutes renvoient les mêmes structures en mémoire (voir `data_source.py`), donc
toute la logique de graphe est indépendante de la provenance.

### 1. MySQL / MariaDB

Connexion directe au serveur MySQL/MariaDB (via le connecteur `mariadb`,
`autocommit=True`, curseur en mode dictionnaire).

L'onglet ne contient **aucun champ** : tous les paramètres viennent du code ou
de l'environnement, ce qui évite de ressaisir des identifiants et de confondre
nom de serveur et nom de base. Il affiche une seule ligne d'état, retestée à
chaque fois qu'on ouvre l'onglet :

* **Connection DB OK**, en vert, quand le serveur répond — écrit **dès le
  lancement** quand l'onglet MySQL est celui affiché, sans attendre un
  changement d'onglet ;
* **Connection Error**, en rouge, suivi du message du serveur *et* du détail des
  réglages (hôte, base, user, et l'**origine** de chacun : `$MYSQL_HOST`,
  `local_config.py` ou `default`). Ce détail n'apparaît **qu'en cas d'erreur**,
  et ne contient jamais le mot de passe.

Le bloc se cale sur l'onglet **courant** : réduit à sa ligne d'état sur MySQL
(deux fois plus court que l'onglet CSV), il ne s'agrandit que pour afficher un
message d'erreur.

Chaque paramètre est résolu dans cet ordre : **variable d'environnement** →
**`local_config.py`** → **valeur par défaut**.

| Paramètre | Variable d'env.  | Clé de `local_config.py` | Défaut               |
|-----------|------------------|--------------------------|----------------------|
| Hôte      | `MYSQL_HOST`     | `MYSQL_HOST`             | `dd-intra`           |
| Base      | `MYSQL_DATABASE` | `MYSQL_DATABASE`         | `dd_assets_tracking` |
| User      | `MYSQL_USER`     | `MYSQL_USER`             | `f.guiliani`         |
| Password  | `MYSQL_PASS`     | `MYSQL_PASSWORD`         | *(factice)*          |

### Le vrai mot de passe : `local_config.py`

Pour ne **jamais** toucher à `data_source.py` ni versionner un secret :

```bat
copy local_config.example.py local_config.py
:: puis renseigner MYSQL_PASSWORD dans local_config.py
```

`local_config.py` est **ignoré par git**, et **PyInstaller l'embarque
automatiquement** (il est importé par `data_source.py`) : la compilation
habituelle suffit, sans `--add-data` ni `--hidden-import`.

> ⚠ Le mot de passe reste **lisible dans l'exécutable** — PyInstaller ne
> chiffre rien, `strings Dedale.exe` le révèle. À réserver à une diffusion
> interne, avec un compte MySQL en **lecture seule**. Pour une distribution
> plus large, laissez le placeholder et fournissez `MYSQL_PASS` sur chaque
> poste.
* Le statut de connexion est retesté à chaque ouverture de l'onglet.
* Le mot de passe **reste en mémoire uniquement** : il n'est ni écrit sur
  disque ni journalisé (sauf trousseau système via `keyring`, sur demande).
* Les requêtes sont **statiques** — aucune concaténation d'entrée utilisateur.
* Dedale n'écrit **jamais** par cette connexion (lecture seule) : la seule
  écriture de l'outil — les mutes de liens — passe par le module **Kraken**
  et sa propre connexion (voir *Muter des assets sur un lien*).

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

## Outputs techniques ignorés

Certaines scènes publient, **dans la même task**, un fichier Nuke (`.nk`)
nommé **`houslapcomp`** à côté du fichier Houdini : c'est un *slap comp*
automatique, seul le fichier Houdini est pertinent. Il est donc **écarté
partout** — il ne crée ni nœud, ni output, ni lien, n'apparaît ni dans les
imports ni dans les assets disponibles, ne compte pas dans le nombre d'imports
vérifiés, ne rend jamais une scène périmée (même si son propre numéro de
version grimpe), et son format n'alimente pas la liste *Show formats*.

Il se présente sous **deux formes**, toutes deux écartées :

* un **output** au milieu des autres (`node_name` = `houslapcomp`) ;
* une **variante de scène entière** — `qua_077_04900_lighting_main_houslapcomp`
  à côté de `qua_077_04900_lighting_main` (`av_name` = `main_houslapcomp`).
  Cette scène ne figure plus dans la liste du graphiste ni dans le graphe, et
  **tout ce qu'elle publie** est ignoré, même quand ses outputs portent des
  noms parfaitement normaux (`render`…).

La reconnaissance porte sur `node_name`, `name` et `av_name` pour les assets,
sur `av_name` et `name` pour les scènes, en **sous-chaîne** et sans tenir compte
de la casse (`fx_houslapcomp_main` est reconnu). Saisir explicitement le nom
d'une variante ignorée dans « Scene to graph » la graphe quand même : on
n'écarte que ce qui n'a pas été demandé. La liste est modifiable sans
recompiler, via la variable d'environnement **`DEDALE_IGNORED_NODES`**
(séparateurs : virgules ou espaces) :

```bat
set DEDALE_IGNORED_NODES=houslapcomp,previz
set DEDALE_IGNORED_NODES=            :: ne rien ignorer du tout
```

---

## Formats CSV attendus

Colonnes **requises** (les autres sont ignorées) :

* **`assets.csv`** : `id, project, entity_name, task_name, av_name,
  node_name, version` — colonnes facultatives : **`name`**, le nom réellement
  publié (`dd_28_rues_armel_shd`), affiché partout à la place de `node_name`
  (`dd`) qui ne sert que de clé de flux pour le versionnage ; **date** de
  publication (`date`, `created_at`… ou détectée), **graphiste** (`artist`,
  `author`…), **format** (`format`, `ext`… ou l'extension d'un chemin), qui
  alimente la liste *Show formats*, et **chemin publié** (`path`,
  `file_path`, `output`…), requis pour enregistrer les mutes en base (voir
  *Muter des assets sur un lien*).
* **`scenes.csv`** : `id, name, project, entity_name, task_name, av_name,
  version` — le titre du rectangle reprend `name` + version ; colonnes
  facultatives : **graphiste** (`artist`, `user`, `created_by`…) pour le
  survol, et **chemin du fichier scène** (`path`, `scene_path`…), requis lui
  aussi pour les mutes en base.
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

**Couleur d'un lien** — elle indique **par où arrive l'obsolescence** :

| Nœud enfant | Le lien transporte un asset périmé | Nœud parent | Lien |
|-------------|-----------------------------------|-------------|------|
| **vert**    | —                                 | —           | **vert** |
| rouge       | **oui**                           | —           | **rouge** |
| rouge/jaune | non                               | **vert**    | **vert** |
| rouge/jaune | non                               | rouge/jaune | **jaune** |

Autrement dit : **rouge** = ce lien est la cause directe (il apporte un asset
supplanté) · **jaune** = le lien est sain mais son parent est lui-même obsolète
(l'obsolescence est héritée par ce chemin) · **vert** = ce chemin est sain.

Sur un nœud rouge alimenté par plusieurs liens, on repère donc immédiatement
lequel est en cause, et lesquels ne font que transmettre.

### Muter des assets sur un lien (clic droit)

L'unité mutée est le **couple (version d'asset → scène)** : muter, c'est dire
« cette version-là de cet asset n'alimente plus cette scène-là ». Un lien du
graphe en porte un ou plusieurs — un par asset qu'il transporte — et n'est
**coupé que lorsque tous les siens sont mutés**.

Un **clic droit** sur un lien ouvre un menu qui **liste ses couples**, avec
pour chacun sa version et son format :

* **un seul couple** → une seule entrée, *Mute* ou *Unmute* selon son état ;
* **plusieurs couples** → une entrée **par couple** (cochée quand il est
  muté), puis **Mute all (n)** et **Unmute all (n)**. Les deux entrées
  globales se grisent quand elles n'ont plus rien à faire.

Chaque entrée rappelle l'état du couple : *muted in DB* / *muted in test
file* quand il est enregistré, *muted (session only)* quand il n'a pu l'être.
Les statuts sont **recalculés** à chaque changement — un nœud dont le seul
input périmé passait par un couple muté redevient vert.

**L'enregistrement** passe par le module Kraken (`dd.utils.assets_mutes`,
chargé depuis `C:/Program Files/Kraken` ou `DEDALE_KRAKEN_PATH`), depuis
n'importe quelle source :

* muter un couple appelle `mute_asset(chemin_scène, chemin_asset)` avec le
  chemin de la **scène enfant** — celle qui importe, jamais la scène parente
  qui publie. *Mute all* sur un lien à cinq assets pose donc **cinq mutes
  vers cette scène enfant** ; *Unmute* fait de même avec `unmute_asset` ;
* après **chaque** écriture, `get_scene_mutes()` est **relu** et l'affichage
  se resynchronise sur ce que la cible contient vraiment (consigne du
  développeur du module). Ses entrées sont traitées comme **opaques** : elles
  ne sont jamais inspectées, seulement repassées à `is_asset_path_muted`.
  C'est ce qui a rendu indolore leur passage de 2 à 3 valeurs (ajout de la
  version) lors de la mise à jour du module ;
* à chaque *Graph* — donc aussi dans une **nouvelle session** — les couples
  enregistrés sont relus et réappliqués (« *n muted link(s) restored…* ») :
  **on retrouve le même état d'une session à l'autre**, depuis n'importe quel
  poste ;
* une **version** d'asset est identifiée par son **chemin publié** ; sans
  colonne de chemin, elle l'est par `libellé|vNNN`, de sorte que deux
  versions d'un même flux ne se confondent jamais ;
* tout se parle en **chemins bruts** (colonnes `path`/`file_path`/… des
  tables `scenes` et `assets`) — pas d'id à chercher ni de séquence à
  reconstruire, la normalisation est faite dans les fonctions du module. Si
  la source n'expose pas ces chemins (le `dump.sql` d'exemple, par exemple),
  le mute reste **en mémoire seulement** et la barre d'état le dit.

> **Un couple muté ailleurs** — posé depuis un autre outil, le standalone par
> exemple — est repris tel quel : il apparaît muté dans le menu et dans
> l'info-bulle, sans couper le lien tant que les autres couples passent.

> **Les assets d'un même dossier sont mutés ensemble.** Le module résout un
> asset par **son dossier** : quand plusieurs y sont publiés — `matlib` et
> `paille_shd_main` dans un même `hda/`, `chair` et `chair_v001` dans un même
> `obj/` — il ne sait pas lequel viser et renonce
> (`2 different assets share the folder of …, cannot tell which one to
> mute`). Dedale les traite donc comme un **bloc** : muter un couple mute
> tous les chemins de son dossier, l'unmute les lève tous, et **la lecture
> suit la même règle** — un couple est montré muté dès qu'un asset de son
> dossier l'est, fût-il posé depuis un autre outil. Sans cette symétrie,
> l'affichage et la base divergeraient au premier aller-retour.

> Si le module refuse malgré tout, **le clic produit quand même son effet**
> (le couple est muté en mémoire, les statuts recalculés) et Dedale **ne
> prétend pas que c'est enregistré** : l'état est revérifié après l'écriture,
> et la barre d'état affiche ce qui n'est pas passé, la raison du module et
> la portée : `1/2 asset version(s) not recorded in the DB: … — 2 different
> assets share the folder of …  Applied for this session only.` Dedale
> n'utilise pas `mute_family` pour contourner : c'est une fonction de la
> couche cron, qui écrirait le mute **avec la mauvaise tâche** — or un mute
> vise ici une scène enfant précise.

Seules les **cinq fonctions prévues** pour un outil interactif sont
utilisées : `get_scene_mutes`, `is_asset_path_muted`, `mute_asset`,
`unmute_asset`, `unmute_scene`. La couche de la tâche cron qui rafraîchit les
statuts côté serveur (`mute_family`, `load_all_mutes`, `load_changed_mutes`,
`filter_muted_assets`, `mutes_of_task`, `delete_inactive_mutes`) n'est
**jamais** appelée : depuis un outil, elle ferait une requête par ligne
affichée, ou écrirait un mute avec la mauvaise tâche.

#### Bac à sable (`-sb 1`) et réglages avancés

Dedale écrit **dans la base du studio**. Pour rejouer un essai sans y
toucher, `-sb 1` bascule sur un fichier local ; deux variables
d'environnement restent disponibles quand aucune option n'est passée.

**1. Où vont les mutes** — `DEDALE_MUTES_TARGET` :

| Valeur | Effet |
|--------|-------|
| `db` (**défaut**) | Écriture réelle en base via le module Kraken. |
| `sandbox` | Comme `-sb 1` : les mutes vont dans un **fichier JSON local** et le module Kraken n'est même pas importé, **la base n'est pas touchée**. Tout le mécanisme fonctionne (mute, relecture, restauration à la session suivante), y compris sur un poste **sans Kraken**. |

Le fichier d'essai est `DEDALE_MUTES_SANDBOX_FILE` s'il est défini, sinon
`~/.dedale/mutes_sandbox.json` ; **le supprimer remet l'essai à zéro**. Son
chemin est rappelé dans la barre d'état au lancement.

**2. Depuis quelle source** — `DEDALE_MUTES_SOURCES` (défaut : **`all`**).
Restreindre à `sql` (ou à une liste, `sql,mysql`) fait que seuls les graphes
issus de ces sources enregistrent des mutes ; ailleurs, clic droit =
désactivation **temporaire en mémoire**, et les mutes déjà enregistrés ne
sont **ni lus ni appliqués**. La restriction porte sur la source **qui a
produit le graphe affiché**, pas sur l'onglet sélectionné : changer d'onglet
sans regrapher n'ouvre jamais l'écriture.

`-sb 1` l'emporte sur cette variable ; sans option ni variable, c'est la
base.

L'interface dit toujours dans quel régime on est : ligne d'état au lancement,
rappel sous le graphe (`Right-click a link: mute assets (saved in test
file)` / `(saved in DB)` / `(temporary)`), entrées du menu et info-bulles des
liens.

> Sans module Kraken sur le poste : comportement historique — les
> désactivations sont **temporaires et en mémoire uniquement**, **rien n'est
> écrit**, et tout est perdu dès qu'un nouveau graphe est affiché.

> ⚠ Les tables `scenes` et `assets` doivent porter une **colonne de chemin**,
> sinon il n'y a rien à donner aux fonctions du module et les mutes restent en
> mémoire — la barre d'état le dit précisément (`table "scenes" has no file
> path column (path, file_path, filepath, scene_path…)`). C'est le cas du
> `dump.sql` livré dans `sample_data/`, qui n'a **volontairement** ni colonne
> de format ni colonne de chemin (cas « colonnes facultatives absentes »).

Le panneau **Display** (à gauche) propose quatre réglages :

**Show disable branches** — masque les nœuds qui n'ont plus **aucun chemin
actif** jusqu'à la scène interrogée, c'est-à-dire ceux qui n'y sont plus reliés
que par des liens désactivés. Le masquage est **récursif** (un parent qui
n'alimentait que des nœuds masqués disparaît aussi), les lignes de tâche
devenues vides sont escamotées, et la scène interrogée reste toujours visible.
Le nombre de nœuds masqués est rappelé sous la case.

**Mute same task connections** — coupe d'un coup tous les liens entre deux
scènes portant la **même tâche** (`lighting → lighting`, deux variantes d'un
même `fx`…). Pratique pour ne garder que les dépendances entre étapes
différentes.

**Show formats** — liste **dynamique** : une case par format de fichier
réellement présent dans le graphe affiché (`abc`, `bgeo.sc`, `hda`, `usd`,
`vdb`, `rs`, `obj`, `ass`, `exr`…), **toutes cochées par défaut**. **Décocher**
un format coupe **tous les liens qui transportent ce type de fichier**, donc
les connexions venant des scènes qui l'exportent. La liste se reconstruit à
chaque *Graph* ; les cases **décochées** le restent si leur format existe
encore. Si la table `assets` n'expose aucun format exploitable, la section
n'apparaît pas.

> La liste couvre **tous les assets du graphe**, y compris ceux qu'une scène
> publie sans consommateur (les outputs de la scène interrogée, par exemple) —
> sinon un format n'apparaissant sur aucun lien manquait à l'appel.

> Le format est lu dans une colonne dédiée (`format`, `ext`, `extension`,
> `file_format`, `filetype`…) ; à défaut, il est extrait de **l'extension**
> d'une colonne de chemin ou de nom de fichier (`path`, `file`, `filename`,
> `output`, `name`). Les extensions **composées** sont préservées
> (`smoke.v005.bgeo.sc` → `bgeo.sc`, `geo.gz`…) au lieu d'être réduites à leur
> suffixe de compression. Aucun format n'est inventé : un chemin, une phrase ou
> un simple `1001-1240` sont rejetés, et sans source exploitable la liste reste
> vide.

**Formats dans les info-bulles** — survoler une **scène** liste ses *inputs* et
ses *outputs* avec le format entre crochets et l'état de version
(`077_02000_shading · chaise_shd [rs] (v002)`,
`quasimodo [abc] (v012 → v014)`). Survoler un **lien** liste les assets qui y
transitent, avec le même détail, et le menu du clic droit les reprend aussi.
Un format inconnu n'affiche pas de crochets vides.

> Le rectangle de la **scène interrogée** ne liste pas ses propres outputs (le
> graphe ne remonte que ses dépendances) : son info-bulle n'affiche donc que
> ses inputs. Ses formats sont bien pris en compte par *Show formats*.

Les trois réglages se **cumulent** avec les clics droit, chacun se levant
indépendamment. Contrairement au clic droit, ces filtres n'écrivent
**jamais** dans la base : ce sont des réglages d'affichage locaux, perdus à
la fermeture.

**Les rectangles listent les assets réellement connectés en output — sauf
ceux mutés.** Le tri se fait **couple par couple**, et **sur le lien direct**
seulement : une ligne disparaît quand **tous** les couples qui la transportent
sont mutés ou coupés par un filtre. Muter un seul asset d'un lien retire donc
**sa seule ligne** et laisse les autres — le lien, lui, reste visible tant
qu'il transporte encore quelque chose.

> **La propagation ne joue pas sur le contenu des boîtes.** Tant que le lien
> qui porte l'output est actif, la ligne reste — même si la scène qui
> l'importe est, plus bas, coupée de la scène interrogée. Un asset exporté par
> l'`animation` vers le `lighting` reste donc listé quand on mute
> `lighting → comp` : ce que la boîte annonce, ce sont les assets qu'elle
> exporte **vers un lien encore actif**, pas ceux qui atteignent le bout du
> graphe.

La boîte rétrécit d'autant, son info-bulle suit, et l'affichage ou non des
branches coupées (*Show disable branches*) ne change rien au contenu des
boîtes. Un output sans consommateur connu n'est jamais masqué, et tout démuter
rend les boîtes à l'identique.

**Show tasks** — seconde liste **dynamique**, sous celle des formats : une case
par task présente dans le graphe, **dans l'ordre des lignes** (haut → bas :
modeling, shading, rig… compositing), toutes cochées par défaut. **Décocher**
une task coupe **tous les liens qui en partent** : cette étape cesse
d'alimenter le graphe, et les outputs dont elle était le seul consommateur
disparaissent aussi. Les liens qui *arrivent* sur cette task restent actifs —
c'est bien la contribution de la task en aval que l'on retire. Comme pour les
formats, les cases décochées survivent à un nouveau *Graph* tant que la task
existe encore.

**Redraw layout** — bouton **sous le graphe**, en tête de la ligne de légende. Masquer des nœuds **ne déplace
jamais** les autres : ils restent où ils étaient, ce qui laisse des colonnes
vides (masquer 150 nœuds sur 170 étalait les rescapés sur toute la largeur
d'origine). Ce bouton **resserre le graphe à la demande** : les colonnes
libérées disparaissent, les nœuds restants sont ramenés vers la gauche et la
vue est recadrée sur l'ensemble. L'ordre gauche-droite calculé par barycentre
est conservé, l'opération est idempotente, et tout réafficher puis redessiner
rend exactement la disposition de départ.

> Le classement se fait sur la colonne du modèle : un nœud **déplacé à la
> main** revient donc sur sa colonne au prochain *Redraw layout*. Le bouton
> recadre aussi la vue sur l'ensemble — c'est le seul recentrage manuel de
> l'interface, le graphe étant déjà ajusté automatiquement à chaque *Graph*.

> Nuance : le nœud à l'origine d'une republication (ex. un `modeling` dont un
> `v007` existe alors que le graphe tire le `v005`) reste **vert** — ses
> propres inputs sont sains — mais ses lignes d'output apparaissent en **rouge**
> (`chaise ⚠ (v005 → v007)`), et tout ce qui l'importe passe au rouge.

---

## Interactions

| Action                          | Effet                                      |
|---------------------------------|--------------------------------------------|
| **Clic** sur un lien            | Met le lien en évidence (le détail est au survol et dans le menu) |
| **Clic droit** sur un lien      | **Menu des couples** (version d'asset → scène) : mute/unmute un asset, ou *Mute all* / *Unmute all* |
| **Glisser** un nœud             | Réordonner (déplacement **horizontal** seul)|
| **Glisser la poignée** de ligne (à gauche) | **Réordonner les lignes** de tâche (vertical) |
| **Survol** d'un nœud            | Dépendances directes + graphiste + inputs/outputs avec **format** `[abc]`, versions `(vXXX)` ou `(vXXX → vYYY)`, et **qui importe** chaque output |
| **Survol** d'un lien            | Les deux scènes reliées **sur une seule ligne** (`A → B`, comme l'en-tête du menu), puis les assets qui y transitent avec format, versions et état de mute |
| **Molette**                     | Zoom (ancré sous le curseur)               |
| **Bouton du milieu** + glisser  | Déplacement (pan)                          |
| **Redraw layout** (sous le graphe) | Resserre le graphe sur les nœuds encore affichés, puis recadre la vue |

---

## Structure du projet

| Fichier           | Rôle                                                         |
|-------------------|-------------------------------------------------------------|
| `main.py`         | Point d'entrée (options `-sc`, `-gr`, `-pr`), fenêtre principale, panneau de source, UI. |
| `data_source.py`  | `load_from_mysql` / `load_from_csv` / `load_from_sql_dump`. |
| `graph_model.py`  | Résolution du nom, parcours scenes+binds, regroupement, statut, disposition. |
| `graph_view.py`   | `QGraphicsScene`/`QGraphicsView`, items nœud & arête, drag horizontal, survol, zoom/pan. |
| `artist_model.py` | Outil « Graphist to graph » : scènes d'un graphiste, imports périmés, assets disponibles, dates de publication. |
| `mutes_store.py`  | Persistance des liens muted via `dd.utils.assets_mutes` (Kraken) — les cinq fonctions autorisées, cache `get_scene_mutes`, bac à sable JSON en option, repli mémoire. |
| `local_config.example.py` | Modèle à copier en `local_config.py` (non versionné) pour le vrai mot de passe. |
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
