#!/usr/bin/env python3
"""
TradingView — Recrée les alertes SOL + ETH pour JP v6.2 Labouchere
"""
import time, os, re, pyotp
from playwright.sync_api import sync_playwright

TV_EMAIL = "hue.jp@hotmail.fr"
TV_PASS  = "dp0G3dSMcrHxhWp"
TV_TOTP  = "hkGDPPzw"
WEBHOOK  = "https://hl-webhook-bot-production.up.railway.app/webhook/jp_bot_secret_2026"
WORK     = os.path.expanduser("~/.openclaw/workspace/trading")
SMS_FILE = "/tmp/tv_sms_code.txt"

def log(m): print(m, flush=True)
def w(s):   time.sleep(s)
def shot(page, n): page.screenshot(path=f"{WORK}/fix_{n}.png")

def wait_sms():
    if os.path.exists(SMS_FILE): os.remove(SMS_FILE)
    log("⏳ En attente du code SMS (max 3 min)...")
    for _ in range(180):
        if os.path.exists(SMS_FILE):
            code = open(SMS_FILE).read().strip()
            if code: log(f"  ✅ SMS reçu: {code}"); return code
        time.sleep(1)
    raise TimeoutError("SMS non reçu")

def login(page):
    log("🔐 Connexion TradingView...")
    page.goto("https://www.tradingview.com/", wait_until="domcontentloaded", timeout=30000)
    w(3)
    # Déjà connecté ?
    try:
        page.locator('[data-name="header-user-menu-button"]').wait_for(timeout=4000)
        log("  ✅ Déjà connecté"); return True
    except: pass

    for sel in ['[data-name="header-user-menu-sign-in"]',
                'button:has-text("Se connecter")', 'a:has-text("Se connecter")']:
        try: page.locator(sel).first.click(timeout=3000); w(2); break
        except: continue

    for sel in ['button:has-text("Email")', '[name="Email"]']:
        try: page.locator(sel).first.click(timeout=3000); w(1); break
        except: continue

    try:
        page.locator('input[name="username"]').fill(TV_EMAIL, timeout=5000); w(0.3)
        page.locator('input[name="password"]').fill(TV_PASS); w(0.3)
        page.locator('button[type="submit"]').click(); w(5)
    except Exception as e:
        log(f"  ❌ Formulaire: {e}"); shot(page, "login_err"); return False

    # 2FA SMS
    try:
        sms = page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]')
        if sms.is_visible(timeout=5000):
            log("  📱 Code SMS requis !")
            code = wait_sms()
            sms.fill(code); w(1)
            page.locator('button[type="submit"]').click(); w(3)
    except: pass

    # TOTP fallback
    try:
        otp = page.locator('input[inputmode="numeric"]')
        if otp.is_visible(timeout=2000):
            c = pyotp.TOTP(TV_TOTP).now()
            otp.fill(c); w(2)
            page.locator('button[type="submit"]').click(); w(3)
    except: pass

    try:
        page.locator('[data-name="header-user-menu-button"]').wait_for(timeout=10000)
        log("  ✅ Connecté !"); return True
    except:
        log("  ❌ Échec connexion"); shot(page, "login_fail"); return False

def delete_alerts_panel(page, coin):
    """Supprimer toutes les alertes dans le panneau alertes pour ce coin."""
    log(f"  🗑️ Suppression alertes existantes {coin}...")
    try:
        # Ouvrir panneau alertes
        for sel in ['[data-name="alerts"]', 'button[aria-label="Alertes"]',
                    '[data-name="base-toolbar"] button:has-text("Alertes")']:
            try:
                b = page.locator(sel).first
                if b.is_visible(timeout=2000): b.click(); w(1); break
            except: continue
        w(1)
        # Chercher les alertes actives et les supprimer
        deleted = 0
        for _ in range(10):
            try:
                # Chercher le bouton supprimer (poubelle) sur la première alerte
                del_btn = page.locator('[data-name="alert-item"] [data-name="delete"], [class*="alert"] button[aria-label*="suppr"], [class*="alert"] button[aria-label*="delet"]').first
                if del_btn.is_visible(timeout=1500):
                    del_btn.click(); w(0.5)
                    deleted += 1
                else:
                    break
            except: break
        log(f"  ✅ {deleted} alerte(s) supprimée(s)")
    except Exception as e:
        log(f"  ⚠️ Suppression: {e}")

def create_alert(page, coin, url):
    log(f"\n{'='*50}\n  🔔 Création alerte {coin}\n{'='*50}")

    page.goto(url, wait_until="domcontentloaded", timeout=40000)
    w(6)
    page.keyboard.press("Escape"); w(0.5)
    shot(page, f"{coin}_chart")

    # Ouvrir dialog alerte
    page.keyboard.press("Alt+a"); w(3)
    shot(page, f"{coin}_alert_open")

    # Fermer popup upsell
    for sel in ['button:has-text("×")', '[class*="close"]', 'button[aria-label="Close"]',
                'button[aria-label="Fermer"]']:
        try:
            b = page.locator(sel).first
            if b.is_visible(timeout=1000): b.click(); w(0.3)
        except: pass

    # Vérifier que le dialog est là
    try:
        page.locator('[data-name="alerts-create-edit-dialog"], div[role="dialog"]').first.wait_for(timeout=6000)
        log("  ✅ Dialog alerte ouvert")
    except:
        shot(page, f"{coin}_no_dialog"); log("  ❌ Dialog inaccessible"); return False

    shot(page, f"{coin}_dialog")

    # === SÉLECTION CONDITION ===
    try:
        selects = page.locator('div[role="dialog"] select, [data-name="alerts-create-edit-dialog"] select').all()
        log(f"  {len(selects)} select(s) trouvés")

        # 1er select : choisir le bon indicateur (Labouchere v6.2)
        if selects:
            opts = selects[0].locator("option").all()
            target = None
            for opt in opts:
                txt = opt.inner_text()
                if "labouchere" in txt.lower() or "labouch" in txt.lower() or "v6.2" in txt.lower() or "v62" in txt.lower():
                    target = txt; break
            if not target:
                # Prendre le premier qui contient le coin
                for opt in opts:
                    txt = opt.inner_text()
                    if coin.upper() in txt.upper():
                        target = txt; break
            if target:
                selects[0].select_option(label=target); w(0.5)
                log(f"  ✅ Indicateur: {target}")
            else:
                log(f"  ℹ️ Options dispo: {[o.inner_text() for o in opts[:6]]}")

        # 2e select : "Any alert() function call"
        if len(selects) >= 2:
            opts2 = selects[1].locator("option").all()
            for opt in opts2:
                txt = opt.inner_text()
                if "alert()" in txt or "appel" in txt.lower() or "function" in txt.lower() or "any" in txt.lower() or "tout" in txt.lower():
                    selects[1].select_option(label=txt); w(0.5)
                    log(f"  ✅ Condition: {txt}"); break
            else:
                log(f"  ℹ️ Cond. options: {[o.inner_text() for o in opts2[:6]]}")
    except Exception as e:
        log(f"  ⚠️ Selects: {e}")

    # === WEBHOOK ===
    try:
        # Onglet Notifications
        for tab_sel in ['button:has-text("Notifications")', 'button:has-text("notification")',
                        '[role="tab"]:has-text("Notification")']:
            try:
                t = page.locator(tab_sel).first
                if t.is_visible(timeout=1500): t.click(); w(1); break
            except: pass

        # Toggle webhook
        for wh_sel in ['input[id*="webhook"]', 'input[id*="Webhook"]',
                       'label:has-text("Webhook") input', '[class*="webhook"] input[type="checkbox"]',
                       'label:has-text("URL webhook") input']:
            try:
                cb = page.locator(wh_sel).first
                if cb.is_visible(timeout=2000):
                    if not cb.is_checked(): cb.click(); w(0.5)
                    log("  ✅ Webhook activé"); break
            except: continue

        # URL
        for u_sel in ['input[placeholder*="http"]', 'input[placeholder*="URL"]',
                      '[class*="webhook"] input[type="text"]', 'input[id*="hook"]']:
            try:
                inp = page.locator(u_sel).first
                if inp.is_visible(timeout=2000):
                    inp.fill(WEBHOOK); w(0.3)
                    log("  ✅ URL webhook OK"); break
            except: continue
    except Exception as e:
        log(f"  ⚠️ Webhook: {e}")

    # === MESSAGE ===
    try:
        for m_sel in ['textarea[placeholder*="essage"]', 'textarea', '[data-name="alert-message"]']:
            try:
                ta = page.locator(m_sel).first
                if ta.is_visible(timeout=2000):
                    ta.triple_click(); ta.fill("{{alert.message}}"); w(0.3)
                    log("  ✅ Message: {{alert.message}}"); break
            except: continue
    except Exception as e:
        log(f"  ⚠️ Message: {e}")

    # === NOM ===
    try:
        for n_sel in ['input[placeholder*="Nom"]', 'input[placeholder*="Name"]',
                      'input[placeholder*="lerte"]', 'input[placeholder*="Alert"]']:
            try:
                inp = page.locator(n_sel).first
                if inp.is_visible(timeout=1500):
                    inp.triple_click()
                    inp.fill(f"TradeMolty {coin} v6.2 Labouchere"); w(0.3)
                    log(f"  ✅ Nom: TradeMolty {coin} v6.2 Labouchere"); break
            except: continue
    except: pass

    shot(page, f"{coin}_filled")

    # === CRÉER ===
    try:
        for btn_sel in ['button:has-text("Créer")', 'button:has-text("Save")',
                        'button:has-text("Enregistrer")', 'button[type="submit"]']:
            try:
                btn = page.locator(btn_sel).last
                if btn.is_visible(timeout=2000):
                    btn.click(); w(4)
                    log(f"  ✅ Alerte {coin} CRÉÉE !")
                    shot(page, f"{coin}_done")
                    return True
            except: continue
    except Exception as e:
        log(f"  ❌ Créer: {e}")

    shot(page, f"{coin}_stuck")
    return False

def main():
    log("🚀 TradeMolty — Fix alertes SOL + ETH")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False, slow_mo=80,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"])
        ctx  = browser.new_context(viewport={"width":1440,"height":900})
        page = ctx.new_page()

        if not login(page):
            log("❌ Connexion impossible")
            input("Entrée pour fermer...")
            browser.close(); return

        w(2)

        charts = [
            ("SOL", "https://fr.tradingview.com/chart/?symbol=BYBIT%3ASOLUSDT.P"),
            ("ETH", "https://fr.tradingview.com/chart/?symbol=BYBIT%3AETHUSDT.P"),
        ]
        results = {}
        for coin, url in charts:
            ok = create_alert(page, coin, url)
            results[coin] = "✅" if ok else "❌"
            w(3)

        log(f"\n🏁 Résultat : SOL={results.get('SOL','?')}  ETH={results.get('ETH','?')}")
        shot(page, "final")
        input("Entrée pour fermer le navigateur...")
        browser.close()

if __name__ == "__main__":
    main()
