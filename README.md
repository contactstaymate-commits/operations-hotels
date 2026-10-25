# Plateforme Opérations Hôtels — v2 (multi-hôtels + prestataires)

## Lancer
    pip install -r requirements.txt
    export ADMIN_EMAIL=toi@exemple.com ADMIN_PASSWORD='un-mot-de-passe-solide'
    uvicorn app:app --host 0.0.0.0 --port 8000
Puis http://localhost:8000. Le compte admin est créé au premier démarrage.

## Trois espaces
- **Hôtel** : l'hôtel crée son espace seul (« Créer mon hôtel »). Rôles : agent (signale), responsable (assigne, appelle un prestataire,
  tableau de bord), patron (tout + équipe + réglages). Chaque hôtel ne voit que ses données.
- **Prestataire** : inscription libre (métiers + ville), compte « en attente » jusqu'à validation par l'admin.
  Reçoit les missions de son métier dans sa ville, accepte (le premier gagne), puis déclare le montant encaissé sur place.
- **Admin plateforme** : valide/suspend les prestataires, suit les commissions, arbitre les litiges, voit les hôtels.

## Parcours d'une mission
1. Ticket créé -> le responsable clique « Appeler un prestataire » (métier, budget indicatif).
2. Prestataires validés de la ville notifiés ; le premier qui accepte prend la mission. Nom/adresse de l'hôtel révélés **après** acceptation.
3. Intervention faite -> le prestataire déclare le montant encaissé (espèces) + photo.
4. L'hôtel confirme (ou conteste) le montant et note le prestataire -> ticket résolu.
5. Commission (COMMISSION_RATE, 10 % par défaut) calculée sur le montant confirmé ; suivie dans « Commissions » (marquer réglée).

## Réglages
HOTEL: dans l'app (Réglages) · COMMISSION_RATE (0.10) · PUBLIC_URL (QR codes) · DB_PATH · ANTHROPIC_API_KEY (tri IA, optionnel) · MODEL.

## Limites connues (à traiter avant un lancement large)
- Notifications dans l'application seulement (pas d'email/WhatsApp/push) ; l'appli interroge toutes les 20 s.
- La commission est déclarative (paiement en espèces) : risque de sous-déclaration, d'où la confirmation de l'hôtel, la notation et les litiges.
  Les coordonnées ne sont révélées qu'après acceptation. Prévoir les conditions générales et un contrat prestataire.
- Pas de mot de passe oublié, pas de vérification d'email, pas de langue arabe.
- SQLite : suffisant pour un pilote (dizaines d'hôtels). Au-delà, passer à PostgreSQL.
- Photos accessibles par lien non devinable, sans authentification.
- Valider auprès d'un juriste : statut de la mise en relation, commission sur espèces, RGPD.
