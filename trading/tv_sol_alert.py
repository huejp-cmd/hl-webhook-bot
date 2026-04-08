#!/usr/bin/env python3
"""
TradeMolty — Crée l'alerte SOL sur le chart sauvegardé de JP
Utilise TOTP pour éviter SMS / backup codes
"""
import time, os, pyotp
from playwright.sync_api import sync_playwright

TV_EMAIL = "hue.jp@hotmail.fr"
TV_PASS  = "dp0G3dSMcrHxhWp"
TV_TOTP  = "hkGDPPzw"
WEBHOOK  = "https://hl-webhook-bot-production.up.railway.app/webhook/jp_bot_secret_2026"
WORK     = os.path.expanduser("~/.openclaw/workspace/trading")

def log(m): print(m, flush=True)
def w(s):   time.sleep(s)
def shot(page, n): page.screenshot(path=f"{WORK}/sol_alert_{n}.png"); log(f"  📸 {n}.png")

def login(page):
    log("🔐 Connexion TradingView...")
    page.goto("https://www.tradingview.com/", wait_until="networkidle", timeout=30000)
    w(4)
    shot(page, "home")

    # Déjà connecté ?
    try:
        # Si connecté, l'icône user a aria-label différent (pas "Open user menu")
        # On tente de détecter un élément spécifique aux comptes connectés
        connected = page.evaluate("""() => {
            const btn = document.querySelector('button[aria-label="Open user menu"]');
            if (!btn) return false;
            // Vérifier s'il y a un avatar ou initiales (compte connecté)
            return btn.querySelector('img, [class*="avatar"], [class*="initials"]') !== null;
        }""")
        if connected:
            log("  ✅ Déjà connecté"); return True
    except: pass

    # Ouvrir le menu utilisateur
    try:
        page.locator('button[aria-label="Open user menu"]').first.click(timeout=5000)
        w(2)
        shot(page, "user_menu_open")
        log("  ✅ Menu user ouvert")
    except Exception as e:
        log(f"  ❌ Menu user: {e}"); return False

    # Cliquer "Sign in" dans le dropdown
    for sel in ['a:has-text("Sign in")', 'button:has-text("Sign in")',
                'a:has-text("Se connecter")', 'button:has-text("Se connecter")']:
        try:
            b = page.locator(sel).first
            if b.is_visible(timeout=2000):
                b.click(); w(2); log(f"  ✅ Sign in cliqué"); break
        except: continue

    w(2)
    shot(page, "after_signin_click")

    # Choisir Email dans la modale de connexion
    for sel in ['button:has-text("Email")', 'a:has-text("Email")',
                'span:has-text("Sign in with email")', 'button:has-text("Continue with Email")']:
        try:
            b = page.locator(sel).first
            if b.is_visible(timeout=3000):
                b.click(); w(1.5); log("  ✅ Email choisi"); break
        except: continue

    w(2)
    shot(page, "after_email_click")

    # Si on atterrit sur le formulaire inscription → cliquer "Already have an account? Sign in"
    # Utilise JS pour trouver et cliquer le lien "Sign in"
    w(2)
    try:
        clicked_signin = page.evaluate("""() => {
            // Chercher tous les liens/boutons avec "sign in" ou "se connecter"
            const all = document.querySelectorAll('a, button, span');
            for (const el of all) {
                const txt = el.innerText?.trim().toLowerCase() || '';
                if (txt === 'sign in' || txt === 'se connecter' || txt === 'connexion') {
                    el.click();
                    return el.innerText;
                }
            }
            return null;
        }""")
        if clicked_signin:
            log(f"  ✅ JS click: '{clicked_signin}'")
        else:
            log("  ⚠️ 'Sign in' non trouvé par JS")
        w(2)
    except Exception as e:
        log(f"  ⚠️ JS signin: {e}")

    shot(page, "after_signin_link")

    # Remplir email + password
    try:
        page.locator('input[name="username"]').fill(TV_EMAIL, timeout=5000); w(0.3)
        page.locator('input[name="password"]').fill(TV_PASS); w(0.3)
        shot(page, "form_filled")
        page.locator('button[type="submit"]').click(); w(5)
    except Exception as e:
        log(f"  ❌ Formulaire: {e}"); shot(page, "form_error"); return False

    shot(page, "after_submit")

    # TOTP 2FA (on génère un code frais)
    try:
        otp_inp = page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]').first
        if otp_inp.is_visible(timeout=5000):
            code = pyotp.TOTP(TV_TOTP).now()
            log(f"  🔑 TOTP: {code}")
            otp_inp.fill(code); w(1)
            page.locator('button[type="submit"]').click(); w(4)
            shot(page, "after_totp")
    except: pass

    # Vérif connexion
    try:
        page.locator('[data-name="header-user-menu-button"]').wait_for(timeout=10000)
        log("  ✅ Connecté !"); return True
    except:
        log("  ❌ Connexion échouée"); shot(page, "login_fail"); return False

def find_sol_chart_url(page):
    """Cherche l'URL du chart SOL de JP dans ses layouts sauvegardés"""
    log("🔍 Recherche chart SOL sauvegardé...")
    try:
        page.goto("https://www.tradingview.com/u/Sebastienhue1/#published-charts", 
                  wait_until="domcontentloaded", timeout=20000)
        w(3)
        shot(page, "profile")
    except: pass

    # Essayer la page des layouts
    try:
        page.goto("https://www.tradingview.com/chart/", 
                  wait_until="domcontentloaded", timeout=20000)
        w(3)
        url = page.url
        log(f"  Chart URL: {url}")
        return url
    except Exception as e:
        log(f"  ❌ {e}")
        return None

def create_sol_alert(page, chart_url):
    log(f"\n📊 Navigation vers chart SOL...")
    
    # Naviguer vers le chart
    page.goto(chart_url, wait_until="domcontentloaded", timeout=30000)
    w(5)
    shot(page, "chart_loaded")

    # Changer le symbole pour SOL si nécessaire
    try:
        # Chercher la barre de symbole
        sym_bar = page.locator('[data-name="legend-series-item"]').first
        current_sym = sym_bar.inner_text(timeout=3000) if sym_bar else ""
        log(f"  Symbole actuel: {current_sym}")
    except: pass

    # Ouvrir dialog alerte : Alt+A ou bouton
    log("  ⏰ Ouverture dialog alerte...")
    try:
        page.keyboard.press("Alt+a")
        w(3)
        shot(page, "after_alta")
    except: pass

    # Vérifier si le dialog est ouvert
    dialog_open = False
    for sel in ['[data-name="alerts-create-edit-dialog"]', 
                'div[role="dialog"]',
                '.tv-alert-dialog']:
        try:
            d = page.locator(sel).first
            if d.is_visible(timeout=3000):
                log(f"  ✅ Dialog ouvert ({sel})")
                dialog_open = True
                break
        except: continue

    if not dialog_open:
        # Essayer via bouton dans la toolbar
        for btn_sel in ['[data-name="add-alert-button"]', 
                        'button[aria-label*="lert"]',
                        'button:has-text("Alert")']:
            try:
                b = page.locator(btn_sel).first
                if b.is_visible(timeout=2000):
                    b.click(); w(3)
                    shot(page, "after_btn_click")
                    dialog_open = True
                    break
            except: continue

    if not dialog_open:
        log("  ❌ Impossible d'ouvrir le dialog alerte")
        shot(page, "no_dialog")
        return False

    # Sélectionner la condition : script Labouchere SOL
    log("  🎯 Sélection indicateur Labouchere SOL...")
    try:
        selects = page.locator('div[role="dialog"] select, [data-name="alerts-create-edit-dialog"] select').all()
        log(f"  {len(selects)} select(s) trouvés")

        if selects:
            opts = selects[0].locator("option").all()
            target = None
            for opt in opts:
                txt = opt.inner_text()
                log(f"    option: {txt}")
                if any(k in txt.lower() for k in ["labouch", "v6.2", "v62", "sol", "molty"]):
                    target = txt; break
            
            if target:
                selects[0].select_option(label=target); w(0.5)
                log(f"  ✅ Indicateur: {target}")
            else:
                log(f"  ⚠️ Labouchere SOL non trouvé dans les options")

        # 2e select : Any alert() function call
        if len(selects) >= 2:
            opts2 = selects[1].locator("option").all()
            for opt in opts2:
                txt = opt.inner_text()
                if any(k in txt.lower() for k in ["alert()", "appel", "function", "any", "tout"]):
                    selects[1].select_option(label=txt); w(0.5)
                    log(f"  ✅ Condition: {txt}"); break
    except Exception as e:
        log(f"  ⚠️ Selects: {e}")

    shot(page, "condition_set")

    # Onglet Notifications + Webhook
    log("  🔔 Configuration webhook...")
    try:
        for tab_sel in ['button:has-text("Notifications")', 
                        '[role="tab"]:has-text("Notif")',
                        'button:has-text("notification")']:
            try:
                t = page.locator(tab_sel).first
                if t.is_visible(timeout=2000): t.click(); w(1); break
            except: continue

        # Activer webhook
        for wh_sel in ['input[id*="webhook"]', 'input[id*="Webhook"]',
                       'label:has-text("Webhook") input',
                       '[class*="webhook"] input[type="checkbox"]',
                       'label:has-text("URL webhook") input']:
            try:
                cb = page.locator(wh_sel).first
                if cb.is_visible(timeout=2000):
                    if not cb.is_checked(): cb.click(); w(0.5)
                    log("  ✅ Webhook activé"); break
            except: continue

        # URL webhook
        for u_sel in ['input[placeholder*="http"]', 'input[placeholder*="URL"]',
                      '[class*="webhook"] input[type="text"]']:
            try:
                inp = page.locator(u_sel).first
                if inp.is_visible(timeout=2000):
                    inp.triple_click(); inp.fill(WEBHOOK); w(0.3)
                    log(f"  ✅ URL: {WEBHOOK}"); break
            except: continue
    except Exception as e:
        log(f"  ⚠️ Webhook: {e}")

    # Message
    try:
        for m_sel in ['textarea[placeholder*="essage"]', 'textarea', '[data-name="alert-message"]']:
            try:
                ta = page.locator(m_sel).first
                if ta.is_visible(timeout=2000):
                    ta.triple_click(); ta.fill("{{alert.message}}"); w(0.3)
                    log("  ✅ Message: {{alert.message}}"); break
            except: continue
    except: pass

    # Nom alerte
    try:
        for n_sel in ['input[placeholder*="Nom"]', 'input[placeholder*="Name"]',
                      'input[placeholder*="lerte"]']:
            try:
                inp = page.locator(n_sel).first
                if inp.is_visible(timeout=1500):
                    inp.triple_click()
                    inp.fill("TradeMolty SOL v6.2 Labouchere"); w(0.3)
                    log("  ✅ Nom: TradeMolty SOL v6.2 Labouchere"); break
            except: continue
    except: pass

    shot(page, "before_create")

    # Créer l'alerte
    try:
        for btn_sel in ['button:has-text("Créer")', 'button:has-text("Save")',
                        'button:has-text("Enregistrer")', 'button[type="submit"]']:
            try:
                btn = page.locator(btn_sel).last
                if btn.is_visible(timeout=2000):
                    btn.click(); w(4)
                    log("  🎉 Alerte SOL CRÉÉE !")
                    shot(page, "done")
                    return True
            except: continue
    except Exception as e:
        log(f"  ❌ Créer: {e}")

    shot(page, "stuck")
    return False

def main():
    log("🚀 TradeMolty — Création alerte SOL")
    log("=" * 50)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False, slow_mo=80,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"])
        ctx  = browser.new_context(viewport={"width":1440,"height":900})
        page = ctx.new_page()

        if not login(page):
            log("❌ Connexion impossible — arrêt")
            input("Entrée pour fermer...")
            browser.close(); return

        w(2)

        # Aller directement sur le chart avec symbole SOL
        # URL générique mais on changera le symbole
        sol_url = "https://fr.tradingview.com/chart/?symbol=BYBIT%3ASOLUSDT.P"
        ok = create_sol_alert(page, sol_url)

        if ok:
            log("\n✅ SUCCÈS — Alerte SOL active sur TradingView !")
        else:
            log("\n❌ ÉCHEC — Screenshot disponible dans trading/")

        input("\nEntrée pour fermer le navigateur...")
        browser.close()

if __name__ == "__main__":
    main()
