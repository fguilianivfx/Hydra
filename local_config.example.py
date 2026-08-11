"""Réglages locaux de connexion — MODÈLE.

Copiez ce fichier en **local_config.py** (à côté de main.py) et renseignez le
vrai mot de passe. `local_config.py` est ignoré par git : il ne partira jamais
dans le dépôt.

PyInstaller embarque automatiquement ce module dans l'exécutable, donc :

    copy local_config.example.py local_config.py
    (éditer local_config.py)
    python -m PyInstaller --onefile --windowed .\\main.py ^
        --icon Dedale.ico --add-data "Dedale.ico;."

…produit un Dedale.exe qui contient le bon mot de passe, sans jamais avoir
modifié data_source.py.

Priorité de résolution : variable d'environnement > ce fichier > valeur par
défaut de data_source.py. Toute ligne peut être supprimée pour conserver la
valeur par défaut.

⚠ Le mot de passe reste lisible dans l'exécutable (PyInstaller ne chiffre
rien) : réservez cette méthode à une diffusion interne, avec un compte MySQL
en lecture seule.
"""

MYSQL_HOST = "dd-intra"
MYSQL_DATABASE = "dd_assets_tracking"
MYSQL_USER = "f.guiliani"
MYSQL_PASSWORD = "REMPLACER_PAR_LE_VRAI_MOT_DE_PASSE"
