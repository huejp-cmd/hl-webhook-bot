#!/usr/bin/env python3
"""
TradingView - Connexion + Création alertes SOL & ETH
Attend le code SMS dans /tmp/tv_sms_code.txt
"""
import time, os, subprocess, pyotp
from playwright.sync_api import sync_playwright

TV_EMAIL = "hue.jp@hotmail.fr"
TV_PASS  = "Sebastienhue1*@"
TV_TOTP  = "hkGDPPzw"
WEBHOOK  = "https://hl-webhook-bot-production.up.railway.app/webhook/jp_bot_secret_2026"
WORK     = os.path.expanduser("~/.openclaw/workspace/trading")
SMS_FILE = "/tmp/tv_sms_code.txt"

def log(m): print(m, flush=True)
def w(s):   time.sleep(s)
def shot(page, n): page.screenshot(path=f"{WORK}/tv_alert_{n}.png")
def pbcopy(t):
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(t.encode())

def wait_for_sms():
    """Attend que /tmp/tv_sms_code.txt contienne le code SMS."""
    if os.path.exists(SMS_FILE):
        os.remove(SMS_FILE)
    log("⏳ En attente du code SMS de JP...")
    for _ in range(120):  # max 2 min
        if os.path.exists(SMS_FILE):
            code = open(SMS_FILE).read().strip()
            if code:
                log(f"  ✅ Code SMS reçu: {code}")
                return code
        time.sleep(1)
    raise TimeoutError("Code SMS non reçu dans les 2 minutes")

def login(page):
    log("🔐 Connexion TradingView...")
    page.goto("https://www.tradingview.com/", wait_until="domcontentloaded", timeout=30000)
    w(3)

    # Déjà connecté ?
    try:
        page.locator('[data-name="header-user-menu-button"]').wait_for(timeout=4000)
        log("  ✅ Déjà connecté !"); return True
    except: pass

    # Cliquer "Se connecter"
    for sel in ['[data-name="header-user-menu-sign-in"]',
                'button:has-text("Se connecter")', 'a:has-text("Se connecter")']:
        try: page.locator(sel).first.click(timeout=3000); w(2); break
        except: continue

    # Choisir Email
    for sel in ['button:has-text("Email")', '[name="Email"]', 'text=Email']:
        try: page.locator(sel).first.click(timeout=3000); w(1); break
        except: continue

    shot(page, "login_form")

    # Remplir le formulaire
    try:
        page.locator('input[name="username"]').fill(TV_EMAIL, timeout=5000); w(0.3)
        page.locator('input[name="password"]').fill(TV_PASS); w(0.3)
        page.locator('button[type="submit"]').click(); w(4)
    except Exception as e:
        log(f"  ⚠️ Formulaire: {e}"); shot(page, "login_error"); return False

    shot(page, "after_submit")

    # SMS 2FA
    try:
        sms_input = page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]')
        if sms_input.is_visible(timeout=5000):
            log("  📱 Code SMS requis !")
            code = wait_for_sms()
            sms_input.fill(code); w(1)
            page.locator('button[type="submit"]').click(); w(3)
    except Exception as e:
        log(f"  ⚠️ 2FA: {e}")

    # TOTP fallback
    try:
        otp = page.locator('input[inputmode="numeric"]')
        if otp.is_visible(timeout=2000):
            c = pyotp.TOTP(TV_TOTP).now()
            otp.fill(c); w(2)
            page.locator('button[type="submit"]').click(); w(3)
    except: pass

    # Vérifier connexion
    try:
        page.locator('[data-name="header-user-menu-button"]').wait_for(timeout=8000)
        log("  ✅ Connecté !"); shot(page, "logged_in"); return True
    except:
        log("  ❌ Connexion échouée"); shot(page, "login_fail"); return False

def create_alert(page, coin, symbol_url):
    log(f"\n{'='*50}\n  Création alerte {coin}\n{'='*50}")

    # Naviguer sur le bon chart
    page.goto(symbol_url, wait_until="domcontentloaded", timeout=30000)
    w(5)
    page.keyboard.press("Escape"); w(0.3)
    shot(page, f"{coin}_chart")

    # Ouvrir dialog alerte : Alt+A
    page.keyboard.press("Alt+a"); w(3)
    shot(page, f"{coin}_alert_open")

    # Fermer popup upsell si présent
    for sel in ['button:has-text("×")', '[class*="close"]', 'button[aria-label="Close"]']:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500): btn.click(); w(0.5)
        except: pass

    # Vérifier si le vrai dialog est ouvert
    try:
        page.locator('[data-name="alerts-create-edit-dialog"], div[role="dialog"]').first.wait_for(timeout=5000)
        log("  ✅ Dialog alerte ouvert")
    except:
        # Essayer bouton "+" dans le panneau alertes
        try:
            page.locator('[data-name="add-alert-button"], button:has-text("+")').first.click(timeout=3000); w(2)
        except:
            log("  ❌ Dialog alerte inaccessible"); shot(page, f"{coin}_no_dialog"); return False

    shot(page, f"{coin}_dialog")

    # Sélectionner JP v6.1 dans le premier dropdown
    try:
        selects = page.locator('div[role="dialog"] select').all()
        log(f"  {len(selects)} select(s) trouvés")
        if selects:
            opts = selects[0].locator("option").all()
            for opt in opts:
                txt = opt.inner_text()
                if "v6.1" in txt or "Bybit" in txt.lower() or "JP v6" in txt:
                    selects[0].select_option(label=txt); w(0.5)
                    log(f"  ✅ Indicateur: {txt}"); break
            else:
                log(f"  Options dispo: {[o.inner_text() for o in opts[:8]]}")

        # 2e select : "Any alert() function call"
        if len(selects) >= 2:
            opts2 = selects[1].locator("option").all()
            for opt in opts2:
                txt = opt.inner_text()
                if "alert()" in txt or "Appel" in txt or "function" in txt.lower() or "any" in txt.lower():
                    selects[1].select_option(label=txt); w(0.5)
                    log(f"  ✅ Condition: {txt}"); break
    except Exception as e:
        log(f"  ⚠️ Selects: {e}")

    # Webhook URL
    try:
        # Toggle webhook
        for wh_sel in ['input[id*="webhook"], input[id*="Webhook"]',
                       'label:has-text("Webhook") input',
                       '[class*="webhook"] input[type="checkbox"]']:
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
                    log(f"  ✅ URL webhook OK"); break
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
                    log("  ✅ Message OK"); break
            except: continue
    except Exception as e:
        log(f"  ⚠️ Message: {e}")

    # Nom
    try:
        for n_sel in ['input[placeholder*="Nom"]', 'input[placeholder*="Name"]', 'input[placeholder*="lerte"]']:
            try:
                inp = page.locator(n_sel).first
                if inp.is_visible(timeout=1500):
                    inp.triple_click(); inp.fill(f"TradeMolty {coin} v6.1"); w(0.3)
                    log(f"  ✅ Nom: TradeMolty {coin} v6.1"); break
            except: continue
    except: pass

    # Expiration ouverte
    try:
        for e_sel in ['select[class*="expir"]', 'select[id*="expir"]']:
            try:
                sel = page.locator(e_sel).first
                if sel.is_visible(timeout=1500):
                    sel.select_option(index=0); w(0.3)  # première option = ouverte généralement
                    break
            except: continue
    except: pass

    shot(page, f"{coin}_filled")

    # Créer
    try:
        for btn_sel in ['button:has-text("Créer")', 'button:has-text("Save")',
                        'button:has-text("Enregistrer")', 'button[type="submit"]']:
            try:
                btn = page.locator(btn_sel).last
                if btn.is_visible(timeout=2000):
                    btn.click(); w(3)
                    log(f"  ✅ Alerte {coin} CRÉÉE !")
                    shot(page, f"{coin}_done")
                    return True
            except: continue
    except Exception as e:
        log(f"  ❌ Créer: {e}")

    shot(page, f"{coin}_stuck")
    return False

def main():
    log("🚀 Création alertes TradeMolty SOL + ETH")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=80,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"])
        ctx  = browser.new_context(viewport={"width":1440,"height":900})
        page = ctx.new_page()

        if not login(page):
            log("❌ Connexion impossible"); input("Entrée pour fermer..."); browser.close(); return

        w(2)

        charts = [
            ("SOL", "https://fr.tradingview.com/chart/?symbol=BYBIT%3ASOLUSDT"),
            ("ETH", "https://fr.tradingview.com/chart/?symbol=BYBIT%3AETHUSDT"),
        ]
        for coin, url in charts:
            create_alert(page, coin, url)
            w(3)

        log("\n✅ Terminé !")
        shot(page, "final")
        input("Entrée pour fermer le navigateur...")
        browser.close()

if __name__ == "__main__":
    main()
