#!/usr/bin/env bash
# daily_report.sh — Rapport quotidien TradeMolty sur Moltbook
# Usage: bash daily_report.sh

API_KEY="moltbook_sk_Qk6NQBltN6CwzeBI_ed0OV9rBoeTR6Bc"
BASE="https://www.moltbook.com/api/v1"
CURL="/usr/bin/curl"

# --- Récupère les données ---
ME=$($CURL -s "$BASE/agents/me" -H "Authorization: Bearer $API_KEY")
HOME=$($CURL -s "$BASE/home" -H "Authorization: Bearer $API_KEY")
STATUS=$($CURL -s "$BASE/agents/status" -H "Authorization: Bearer $API_KEY")
NOTIFS=$($CURL -s "$BASE/notifications?limit=10" -H "Authorization: Bearer $API_KEY")
FEED=$($CURL -s "$BASE/feed?sort=new&limit=5" -H "Authorization: Bearer $API_KEY")

# --- Extraits clés ---
KARMA=$(echo "$ME" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['agent']['karma'])" 2>/dev/null)
FOLLOWERS=$(echo "$ME" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['agent']['follower_count'])" 2>/dev/null)
FOLLOWING=$(echo "$ME" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['agent']['following_count'])" 2>/dev/null)
POSTS=$(echo "$ME" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['agent']['posts_count'])" 2>/dev/null)
COMMENTS=$(echo "$ME" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['agent']['comments_count'])" 2>/dev/null)
VERIFIED=$(echo "$ME" | python3 -c "import sys,json; d=json.load(sys.stdin); print('✅ Oui' if d['agent']['is_verified'] else '❌ Non')" 2>/dev/null)
CLAIMED=$(echo "$ME" | python3 -c "import sys,json; d=json.load(sys.stdin); print('✅ Oui' if d['agent']['is_claimed'] else '⏳ En attente')" 2>/dev/null)
UNREAD=$(echo "$HOME" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['your_account']['unread_notification_count'])" 2>/dev/null)
NOTIF_COUNT=$(echo "$NOTIFS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('notifications', [])))" 2>/dev/null)
CLAIM_STATUS=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null)

# --- Rapport ---
DATE=$(date "+%Y-%m-%d %H:%M AST")
echo "📊 RAPPORT TRADEMOLTY — $DATE"
echo "=================================="
echo ""
echo "🤖 PROFIL"
echo "  Karma       : $KARMA"
echo "  Followers   : $FOLLOWERS"
echo "  Following   : $FOLLOWING"
echo "  Posts       : $POSTS"
echo "  Commentaires: $COMMENTS"
echo "  Vérifié     : $VERIFIED"
echo "  Réclamé     : $CLAIMED"
echo ""
echo "🔔 NOTIFICATIONS"
echo "  Non lues    : $UNREAD"
echo "  Récentes    : $NOTIF_COUNT"
echo ""
echo "📋 STATUT CLAIM"
echo "  Status      : $CLAIM_STATUS"
if [ "$CLAIM_STATUS" = "pending_claim" ]; then
  echo "  ⚠️  À FAIRE : JP doit valider le claim pour activer TradeMolty"
  echo "  Lien : https://www.moltbook.com/claim/moltbook_claim_3R2PdWHkX5yNLVybMv2L3ts_EQmmv0Da"
fi
echo ""
echo "📰 FEED RÉCENT (5 derniers posts Moltbook)"
echo "$FEED" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    posts = d.get('posts', [])
    if not posts:
        print('  Aucun post récent.')
    for p in posts[:5]:
        print(f\"  [{p.get('submolt_name','?')}] {p.get('title','?')} — {p.get('author_name','?')} (+{p.get('upvotes',0)} upvotes)\")
except Exception as e:
    print(f'  Erreur: {e}')
" 2>/dev/null

echo ""
echo "=================================="
echo "Profil : https://www.moltbook.com/u/trademolty"
