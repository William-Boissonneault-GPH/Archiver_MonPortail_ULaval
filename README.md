# Archiver_MonPortail_ULaval
Automatisation afin d'archiver vos documents de cours avant la fermeture de MonPortail 2026

MONPORTAIL ARCHIVER — GUIDE POUR LES ÉTUDIANTS (WINDOWS)
=========================================================

OBJECTIF
Sauvegarder localement les documents accessibles dans vos anciens cours
monPortail : modules, documents, évaluations, travaux remis et corrections.
***L'outil n'est PAS un logiciel officiel de l'Université Laval.***
***VEUILLEZ respecter les règles de partages de l'université concernant les documents de cours, ce n'est qu'une aide au télechargement***
AUCUN SUPPORT OU AIDE SI NON FONCTIONNEL POUR VOUS
partagé bénévolement

Il n'est pas garanti que tous les éléments soient accessibles ou récupérés.
Fonctionne bien selon mon expérience

AVANT DE COMMENCER
- Prévoyez suffisamment d'espace sur votre disque dur externe.
- Gardez le disque branché et une connexion Internet stable.
- Votre connexion monPortail se fait directement dans un navigateur ; ne
  donnez jamais votre mot de passe à une autre personne.
- Évitez de déplacer/renommer le dossier d'archive pendant l'exécution.
- Téléchargez uniquement les contenus dont vous avez le droit de conserver
  une copie. L'outil ne contourne pas les protections d'accès.


UTILISATION
1. Une fenêtre vous demande où enregistrer l'archive : choisissez un dossier
   sur votre disque dur (par exemple E:\Sauvegarde_ULaval).
   Le programme y crée lui-même un sous-dossier MonPortail_Archive.
2. Un navigateur Chromium s'ouvre. Connectez-vous à monPortail normalement.
3. Dans le navigateur OUVERT PAR LE PROGRAMME, allez dans Études > Cours.
   Ouvrez un cours quelconque sur sa page « Introduction ».
   Vérifiez que « Liste des cours » apparaît en haut à droite.
4. Revenez dans la fenêtre noire, puis appuyez sur Entrée.
5. Une fenêtre demande combien de premières sessions ignorer.
   Laissez 0 (valeur par défaut) pour sauvegarder tout le baccalauréat.
   Choisissez 2 seulement si vous voulez ignorer les deux premières sessions, par exemple si la sauvegarde des deux premières à réussi et que vous recommencer.
6. Le programme inventorie d'abord les cours, PUIS télécharge les documents.
   Ne fermez pas Chromium avant la fin.

APRÈS L'ARCHIVAGE
- Examinez les dossiers sous MonPortail_Archive.
- Vérifiez journal_execution.txt : des lignes [À VÉRIFIER] indiquent des
  éléments manquants, incertains ou des pages non accessibles.
- Consultez inventaire_cours.json pour voir les cours détectés.
- Faites une sauvegarde supplémentaire de l'archive sur un autre support.

REPRENDRE APRÈS UNE INTERRUPTION
Relancez LANCER_MONPORTAIL.bat et choisissez le MÊME DOSSIER de destination.
Le programme tente de réutiliser les fichiers déjà téléchargés grâce à
MonPortail_Archive\journal.json. Vérifiez toutefois le journal final :
les fichiers présents ne sont pas systématiquement contrôlés par empreinte.

LIMITES ET DÉPANNAGE
- Si le programme ne trouve pas de cours, vérifiez que vous avez ouvert
  un cours dans SON navigateur Chromium, et non Chrome/Edge personnel.
- Certains cours n'ont pas de boîte de dépôt ou d'onglet Résultats. Normal
- Certains liens externes, contenus protégés et pages non téléchargeables
  peuvent exiger une sauvegarde manuelle.
- Sur Windows, le fichier .bat peut être bloqué par une politique de sécurité.
  Dans ce cas, demandez à un collègue de vous aider avec Python.
- Ne publiez pas vos fichiers de cours, vos notes, vos soumissions,
  votre dossier de profil Chromium, ni le journal de votre archive sur GitHub.
  Seuls le code source et le guide de l'outil peuvent être partagés publiquement.


