#!/usr/bin/env python3
"""
TradingView Automation v2 — JP v6.1
- Charge JP v6.1 sur BYBIT:SOLUSDT (SOL) et BYBIT:ETHUSDT (ETH) en 29M
- Supprime les alertes existantes (SOL + ETH)
- Crée deux nouvelles alertes webhook → Railway bot

Usage:
    python3 tv_auto_v2.py             # SOL + ETH (défaut)
    python3 tv_auto_v2.py --chart SOL
    python3 tv_auto_v2.py --chart ETH
    python3 tv_auto_v2.py --skip-load  # juste alertes, pas de rechargement script
"""

import time, os, subprocess, pyotp, argparse, sys
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

TV_EMAIL = "hue.jp@hotmail.fr"
TV_PASS  = "Sebastien2"
TV_TOTP  = "hkGDPPzw"
WEBHOOK  = "https://hl-webhook-bot-production.up.railway.app/webhook/jp_bot_secret_2026"
WORK     = os.path.expanduser("~/.openclaw/workspace/trading")

CHARTS = {
    "SOL": {
        "url":    "https://fr.tradingview.com/chart/?symbol=BYBIT%3ASOLUSDT",
        "script": "JP_v61_bybit.pine",
        "coin":   "SOL",
    },
    "ETH": {
        "url":    "https://fr.tradingview.com/chart/?symbol=BYBIT%3AETHUSDT",
        "script": "JP_v61_bybit_ETH.pine",
        "coin":   "ETH",
    },
}

def log(m): print(m, flush=True)
def w(s):   time.sleep(s)
def shot(page, n):
    path = os.path.join(WORK, f"tv_{n}.png")
    page.screenshot(path=path)
    log(f"  📸 {path}")

def get_totp(): return pyotp.TOTP(TV_TOTP).now()

def pbcopy(text):
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf-8"))

def read_script(fn):
    path = os.path.join(WORK, fn)
    if os.path.exists(path):
        return open(path).read()
    raise FileNotFoundError(f"Script introuvable : {path}")

# ─────────────────────────────────────────────────────────────
#  LOGIN
# ─────────────────────────────────────────────────────────────
def login(page):
    log("\n🔐 Connexion TradingView...")
    page.goto("https://fr.tradingview.com/", wait_until="domcontentloaded", timeout=30000)
    w(3)

    # Déjà connecté ?
    try:
        page.locator('[data-name="header-user-menu-button"]').wait_for(timeout=4000)
        log("  ✅ Déjà connecté")
        return
    except: pass

    # Cliquer Se connecter
    for sel in ['button:has-text("Se connecter")', 'a:has-text("Se connecter")',
                '[data-name="header-user-menu-sign-in"]']:
        try: page.locator(sel).first.click(timeout=3000); w(1.5); break
        except: continue

    # Choisir Email
    try: page.locator('text=Email').first.click(timeout=3000); w(1)
    except: pass

    # Formulaire login
    try:
        page.locator('input[name="username"]').fill(TV_EMAIL, timeout=5000); w(0.3)
        page.locator('input[name="password"]').fill(TV_PASS); w(0.3)
        page.locator('button[type="submit"]').click(); w(4)
    except Exception as e:
        log(f"  ⚠️ Formulaire: {e}"); shot(page, "login_fail"); return

    # TOTP 2FA
    try:
        otp_input = page.locator('input[inputmode="numeric"]')
        if otp_input.is_visible(timeout=5000):
            code = get_totp()
            log(f"  🔑 TOTP: {code}")
            otp_input.fill(code); w(2)
            try: page.locator('button[type="submit"]').click(); w(3)
            except: pass
    except: pass

    w(3)
    shot(page, "after_login")
    log("  ✅ Login terminé")

# ─────────────────────────────────────────────────────────────
#  CHANGER TIMEFRAME → 29M
# ─────────────────────────────────────────────────────────────
def set_timeframe_29m(page):
    log("  ⏳ Réglage timeframe 29M...")
    try:
        # Cliquer sur le sélecteur de timeframe (bouton avec "1h", "30m", etc.)
        page.locator('[data-name="time-interval-button"]').click(timeout=5000)
        w(1)
        # Chercher option "Custom..."
        try:
            page.locator('button:has-text("Custom...")').click(timeout=3000); w(0.5)
            page.locator('input[type="number"]').fill("29"); w(0.3)
            page.locator('button:has-text("Minutes")').click(timeout=3000); w(0.5)
            page.keyboard.press("Enter"); w(1)
            log("  ✅ Timeframe 29M (custom)")
            return
        except: pass
    except: pass

    # Méthode alternative : taper directement dans le champ timeframe
    try:
        page.locator('[data-name="time-interval-button"]').click(timeout=5000); w(0.5)
        page.keyboard.type("29"); w(0.5)
        page.keyboard.press("Enter"); w(1)
        log("  ✅ Timeframe 29M (clavier)")
        return
    except: pass

    log("  ⚠️ Timeframe non changé — vérifier manuellement")

# ─────────────────────────────────────────────────────────────
#  CHARGER SCRIPT PINE
# ─────────────────────────────────────────────────────────────
def open_pine_editor(page):
    log("  ⏳ Ouverture Pine Editor...")
    page.keyboard.press("Escape"); w(0.3)
    page.keyboard.press("Escape"); w(0.3)

    for sel in [
        '[data-name="bottom-area"] button, [class*="bottomBar"] button',
        '[data-name="pine-editor-activate-btn"]',
    ]:
        try:
            page.locator(sel).filter(has_text="Pine Editor").click(timeout=3000)
            w(2); log("  ✅ Pine Editor ouvert"); return True
        except: pass

    try:
        page.get_by_role("tab", name="Pine Editor").click(timeout=3000)
        w(2); log("  ✅ Pine Editor (tab)"); return True
    except: pass

    # Alt+P
    page.keyboard.press("Alt+p"); w(2)
    log("  ✅ Pine Editor (Alt+P)")
    return True

def focus_and_paste(page, content):
    log("  ⏳ Focus + collage script...")
    pbcopy(content); w(0.3)
    page.keyboard.press("Escape"); w(0.3)

    selectors = [
        '.cm-content[contenteditable="true"]',
        '[class*="pine-editor"] .cm-content',
        '[class*="editor-section"] .cm-content',
        '.cm-editor .cm-content',
    ]
    focused = False
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(timeout=3000)
            el.click(); w(0.3)
            focused = True; log(f"  ✅ Focus ({sel})"); break
        except: continue

    if not focused:
        log("  ⚠️ Fallback: clic zone éditeur...")
        sz = page.viewport_size
        page.mouse.click(sz["width"] * 0.3, sz["height"] * 0.85); w(0.5)

    page.keyboard.press("Meta+a"); w(0.3)
    page.keyboard.press("Meta+v"); w(2)
    log("  ✅ Script collé")

def click_add_to_chart(page):
    log("  ⏳ Ajouter au chart...")
    for sel in [
        'button:has-text("Ajouter au chart")',
        '[data-name="pine-editor-add-to-chart-btn"]',
        'button[aria-label="Ajouter au chart"]',
    ]:
        try:
            btn = page.locator(sel).first
            btn.wait_for(timeout=3000)
            btn.click(); w(4); log(f"  ✅ Ajouté ({sel})"); return True
        except: continue

    log("  ⚠️ Bouton non trouvé → Shift+Enter")
    page.keyboard.press("Shift+Enter"); w(4)
    return True

def load_script(page, cfg):
    content = read_script(cfg["script"])
    log(f"  📋 Script: {cfg['script']} ({len(content)} chars)")
    open_pine_editor(page); w(1)
    focus_and_paste(page, content)
    shot(page, f"{cfg['coin']}_pasted")
    click_add_to_chart(page)
    shot(page, f"{cfg['coin']}_loaded_v61")
    log(f"  ✅ Script JP v6.1 chargé sur {cfg['coin']}")

# ─────────────────────────────────────────────────────────────
#  SUPPRIMER LES ALERTES EXISTANTES
# ─────────────────────────────────────────────────────────────
def delete_existing_alerts(page, coin):
    """Supprime toutes les alertes contenant 'SOL' ou 'ETH' dans leur nom/condition."""
    log(f"  🗑️ Suppression alertes existantes ({coin})...")
    try:
        # Ouvrir le gestionnaire d'alertes (icône cloche dans la barre droite)
        page.locator('[data-name="alerts-panel-button"], [aria-label*="lerte"], [title*="lerte"]').first.click(timeout=5000)
        w(2)
    except:
        # Raccourci Alt+A
        try:
            page.keyboard.press("Alt+a"); w(2)
        except:
            log("  ⚠️ Impossible d'ouvrir le gestionnaire d'alertes")
            return

    # Chercher alertes et les supprimer une par une
    try:
        alerts = page.locator('[class*="alert-item"], [data-name="alert-item"]').all()
        deleted = 0
        for alert in alerts:
            try:
                text = alert.inner_text()
                if coin in text.upper() or "JP V6" in text.upper() or "SOLUSDT" in text.upper() or "ETHUSDT" in text.upper():
                    # Clic droit → Supprimer
                    alert.click(button="right"); w(0.5)
                    page.locator('text=Supprimer').first.click(timeout=2000); w(0.5)
                    deleted += 1
            except: continue
        log(f"  ✅ {deleted} alerte(s) supprimée(s)")
    except:
        log("  ⚠️ Aucune alerte trouvée ou liste inaccessible")

    # Fermer le panneau alertes
    page.keyboard.press("Escape"); w(0.5)

# ─────────────────────────────────────────────────────────────
#  CRÉER UNE ALERTE WEBHOOK
# ─────────────────────────────────────────────────────────────
def create_alert(page, cfg):
    coin = cfg["coin"]
    log(f"\n  🔔 Création alerte webhook {coin}...")

    # Ouvrir dialog alerte via Alt+A ou icône
    try:
        page.keyboard.press("Alt+a"); w(2)
    except:
        page.locator('[data-name="alerts-panel-button"]').first.click(timeout=5000); w(2)

    # Bouton "Créer une alerte" / "+"
    try:
        page.locator('button:has-text("Créer une alerte"), button:has-text("Ajouter une alerte"), '
                     '[data-name="add-alert-button"], [aria-label*="Créer"]').first.click(timeout=5000)
        w(2)
    except:
        # Alt+A ouvre directement le dialog
        log("  ⚠️ Bouton Créer non trouvé")

    shot(page, f"{coin}_alert_dialog")

    # ── Sélectionner la condition : l'indicateur JP v6.1 ──
    try:
        # Premier dropdown (indicateur/symbole)
        cond_select = page.locator('[class*="condition"] select, [data-name="condition-select"]').first
        cond_select.click(timeout=4000); w(0.5)
        # Chercher "JP v6.1" dans les options
        option = page.locator(f'option:has-text("JP v6.1")')
        if option.count() > 0:
            option.first.click(); w(0.5)
            log("  ✅ Indicateur JP v6.1 sélectionné")
        else:
            log("  ⚠️ Indicateur JP v6.1 non trouvé dans la liste, sélection manuelle requise")
    except Exception as e:
        log(f"  ⚠️ Sélection indicateur: {e}")

    # ── Sélectionner "Appel de fonction alert()" ──
    try:
        # Deuxième dropdown (type de condition)
        selects = page.locator('[class*="condition"] select, select[class*="select"]').all()
        for select in selects:
            try:
                options = select.locator('option').all()
                for opt in options:
                    txt = opt.inner_text()
                    if "alert()" in txt or "Appel" in txt or "function" in txt.lower():
                        select.select_option(label=txt); w(0.5)
                        log(f"  ✅ Condition: {txt}"); break
            except: continue
    except Exception as e:
        log(f"  ⚠️ Sélection condition: {e}")

    # ── Webhook URL ──
    try:
        # Chercher la checkbox/toggle Webhook
        webhook_toggle = page.locator(
            'label:has-text("Webhook"), '
            'input[type="checkbox"][id*="webhook"], '
            '[class*="webhook"] input[type="checkbox"]'
        ).first
        if not webhook_toggle.is_checked(timeout=3000):
            webhook_toggle.click(); w(0.5)
        log("  ✅ Webhook activé")

        # Remplir l'URL
        webhook_input = page.locator('input[placeholder*="http"], input[type="url"], [class*="webhook"] input[type="text"]').first
        webhook_input.fill(WEBHOOK, timeout=3000); w(0.3)
        log(f"  ✅ URL webhook: {WEBHOOK}")
    except Exception as e:
        log(f"  ⚠️ Webhook: {e}")

    # ── Message : {{alert.message}} ──
    try:
        msg_area = page.locator('textarea[placeholder*="message"], textarea[class*="message"], [data-name="alert-message"]').first
        msg_area.fill("{{alert.message}}", timeout=3000); w(0.3)
        log("  ✅ Message: {{alert.message}}")
    except Exception as e:
        log(f"  ⚠️ Message: {e}")

    # ── Nom de l'alerte ──
    try:
        name_input = page.locator('input[placeholder*="Nom"], input[placeholder*="Name"], [data-name="alert-name"]').first
        name_input.fill(f"TradeMolty {coin} v6.1", timeout=3000); w(0.3)
        log(f"  ✅ Nom: TradeMolty {coin} v6.1")
    except: pass

    # ── Expiration : Open-ended ──
    try:
        exp = page.locator('select[class*="expir"], [data-name="expiry-select"]').first
        exp.select_option(label="Ouverte"); w(0.3)
        log("  ✅ Expiration: Ouverte")
    except: pass

    shot(page, f"{coin}_alert_filled")

    # ── Créer ──
    try:
        page.locator(
            'button:has-text("Créer"), button:has-text("Save"), button:has-text("Enregistrer")'
        ).last.click(timeout=5000)
        w(3)
        log(f"  ✅ Alerte {coin} créée !")
        shot(page, f"{coin}_alert_done")
    except Exception as e:
        log(f"  ❌ Erreur création alerte: {e}")
        shot(page, f"{coin}_alert_error")

# ─────────────────────────────────────────────────────────────
#  PIPELINE COMPLET PAR CHART
# ─────────────────────────────────────────────────────────────
def process_chart(page, coin, skip_load=False):
    cfg = CHARTS[coin]
    log(f"\n{'='*55}")
    log(f"  {coin}  →  {cfg['url']}")
    log('='*55)

    page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30000)
    w(5)
    page.keyboard.press("Escape"); w(0.3)
    page.keyboard.press("Escape"); w(0.3)
    shot(page, f"{coin}_nav")

    # Régler le timeframe 29M
    set_timeframe_29m(page)
    w(2)
    shot(page, f"{coin}_29m")

    # Charger le script Pine v6.1
    if not skip_load:
        load_script(page, cfg)
        w(2)

    # Supprimer alertes existantes
    delete_existing_alerts(page, coin)
    w(1)

    # Créer la nouvelle alerte
    create_alert(page, cfg)
    w(2)

    log(f"\n  🏁 {coin} terminé !")

# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--chart", choices=["SOL", "ETH", "ALL"], default="ALL",
                   help="Chart(s) à traiter")
    p.add_argument("--skip-load", action="store_true",
                   help="Ne pas recharger le script Pine (juste alertes)")
    args = p.parse_args()

    charts = ["SOL", "ETH"] if args.chart == "ALL" else [args.chart]
    log(f"\n🚀 TV Automation v2 — charts: {charts}  skip-load: {args.skip_load}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--start-maximized"], slow_mo=80)
        ctx  = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        login(page); w(3)

        for coin in charts:
            try:
                process_chart(page, coin, skip_load=args.skip_load)
            except Exception as e:
                log(f"\n❌ Erreur sur {coin}: {e}")
                shot(page, f"{coin}_crash")

        log("\n✅ Tout terminé ! Vérifie les charts TradingView.")
        log("Appuie sur Entrée pour fermer le navigateur...")
        input()
        browser.close()

if __name__ == "__main__":
    main()
