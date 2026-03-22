#!/usr/bin/env python3
"""
TradingView Automation v3 — JP v6.1
Utilise le profil Chrome existant (cookies session TradingView inclus).
- Charge JP v6.1 sur BYBIT:SOLUSDT (29M) et BYBIT:ETHUSDT (29M)
- Crée les alertes webhook → Railway bot
"""

import time, os, subprocess, argparse, shutil, tempfile
from playwright.sync_api import sync_playwright

WEBHOOK  = "https://hl-webhook-bot-production.up.railway.app/webhook/jp_bot_secret_2026"
WORK     = os.path.expanduser("~/.openclaw/workspace/trading")
CHROME_PROFILE = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")

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
    path = os.path.join(WORK, f"v3_{n}.png")
    page.screenshot(path=path)
    log(f"  📸 {n}")

def pbcopy(text):
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf-8"))

def read_script(fn):
    path = os.path.join(WORK, fn)
    if os.path.exists(path):
        return open(path).read()
    raise FileNotFoundError(f"Script introuvable : {path}")

def dismiss_popups(page):
    """Ferme les popups/bandeaux courants de TradingView."""
    for sel in [
        'button:has-text("Compris")',
        'button:has-text("Got it")',
        'button:has-text("Accept")',
        'button:has-text("Accepter")',
        '[class*="close-btn"]',
        'button[aria-label="Close"]',
        '[data-name="close"]',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click(); w(0.4)
                log(f"  ✅ Popup fermé ({sel})")
        except: pass

def set_timeframe(page, coin):
    """Tente de passer en 29 minutes."""
    log("  ⏳ Réglage timeframe 29M...")
    try:
        # Cliquer sur le bouton timeframe actuel (ex: "1D", "30")
        tf_btn = page.locator('[data-name="time-interval-button"]').first
        tf_btn.wait_for(timeout=5000)
        tf_btn.click(); w(1)

        # Chercher champ de saisie custom
        inp = page.locator('input[placeholder*="minute"], input[placeholder*="Minute"], '
                           '[class*="interval"] input').first
        if inp.is_visible(timeout=2000):
            inp.fill("29"); w(0.3)
            page.keyboard.press("Enter"); w(1)
            log("  ✅ 29M (input)")
            return

        # Sinon taper "29" directement
        page.keyboard.type("29"); w(0.5)
        page.keyboard.press("Enter"); w(1)
        log("  ✅ 29M (type)")
    except Exception as e:
        log(f"  ⚠️ Timeframe: {e} — à régler manuellement")
    shot(page, f"{coin}_tf")

def open_pine_editor(page):
    log("  ⏳ Pine Editor...")
    page.keyboard.press("Escape"); w(0.3)

    # Méthode 1 : onglet bas
    for sel in [
        'button[class*="bottom"][class*="tab"]:has-text("Pine")',
        '[data-name="pine-editor-activate-btn"]',
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.click(); w(2); log(f"  ✅ Pine Editor ({sel})"); return
        except: pass

    # Méthode 2 : tab role
    try:
        page.get_by_role("tab", name="Pine Editor").click(timeout=3000); w(2)
        log("  ✅ Pine Editor (tab role)"); return
    except: pass

    # Méthode 3 : Alt+P
    page.keyboard.press("Alt+p"); w(2)
    log("  ✅ Pine Editor (Alt+P)")

def paste_script(page, content, coin):
    log("  ⏳ Collage script...")
    pbcopy(content); w(0.3)
    page.keyboard.press("Escape"); w(0.2)

    # Focus éditeur CodeMirror
    for sel in [
        '.cm-content[contenteditable="true"]',
        '.cm-editor .cm-content',
        '[class*="editor"] .cm-content',
    ]:
        try:
            el = page.locator(sel).first
            el.wait_for(timeout=4000)
            el.click(); w(0.3)
            log(f"  ✅ Focus ({sel})")
            break
        except: continue
    else:
        # Fallback: clic dans zone éditeur bas-gauche
        sz = page.viewport_size
        page.mouse.click(sz["width"] * 0.25, sz["height"] * 0.82); w(0.4)

    page.keyboard.press("Meta+a"); w(0.3)
    page.keyboard.press("Meta+v"); w(2)
    log("  ✅ Script collé")
    shot(page, f"{coin}_pasted")

def add_to_chart(page, coin):
    log("  ⏳ Ajouter au chart...")
    # Bouton "Ajouter au chart" dans l'en-tête de l'éditeur Pine
    for sel in [
        'button:has-text("Ajouter au chart")',
        'button:has-text("Add to chart")',
        '[data-name="add-to-chart-button"]',
        '[data-name="pine-editor-add-to-chart-btn"]',
        'button[class*="add-button"]',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                btn.click(); w(4)
                log(f"  ✅ Ajouté ({sel})")
                shot(page, f"{coin}_added")
                return
        except: pass

    # Fallback Shift+Enter (compile + ajoute)
    page.keyboard.press("Shift+Enter"); w(4)
    log("  ✅ Ajouté (Shift+Enter)")
    shot(page, f"{coin}_added")

def create_alert(page, coin):
    log(f"\n  🔔 Création alerte {coin}...")

    # ── Ouvrir le dialog "Créer une alerte" ──
    # Raccourci clavier Alt+A
    page.keyboard.press("Escape"); w(0.3)
    page.keyboard.press("Alt+a"); w(2)
    shot(page, f"{coin}_alert_open")

    # Fermer les popups éventuels
    dismiss_popups(page)

    # Vérifier si le dialog est ouvert
    dialog_open = False
    for sel in [
        '[data-name="alerts-create-edit-dialog"]',
        '[class*="dialog"][class*="alert"]',
        'div[role="dialog"]',
    ]:
        try:
            page.locator(sel).first.wait_for(timeout=3000)
            dialog_open = True
            log(f"  ✅ Dialog ouvert ({sel})")
            break
        except: pass

    if not dialog_open:
        # Essayer via le bouton "Alerte" dans la barre du chart
        try:
            page.locator('[data-name="alerts-toolbar-button"], button:has-text("Alerte")').first.click(timeout=3000)
            w(1)
            page.locator('button:has-text("Créer une alerte"), button:has-text("+")')  .first.click(timeout=3000)
            w(2)
        except:
            log("  ❌ Dialog alerte inaccessible"); shot(page, f"{coin}_alert_fail"); return

    shot(page, f"{coin}_alert_dialog")

    # ── Sélectionner l'indicateur JP v6.1 dans le 1er dropdown ──
    try:
        # Les selects de condition sont les premiers <select> dans le dialog
        selects = page.locator('div[role="dialog"] select, [class*="dialog"] select').all()
        log(f"  ℹ️ {len(selects)} select(s) dans le dialog")

        if len(selects) >= 1:
            # 1er select : source (indicateur ou prix)
            opts = selects[0].locator("option").all()
            for opt in opts:
                txt = opt.inner_text()
                if "JP v6" in txt or "v6.1" in txt or "Bybit" in txt.lower():
                    selects[0].select_option(label=txt); w(0.5)
                    log(f"  ✅ Indicateur: {txt}"); break
            else:
                log(f"  ⚠️ JP v6.1 non trouvé dans les options ({[o.inner_text() for o in opts[:5]]})")

        if len(selects) >= 2:
            # 2e select : type (alert function, crossing, etc.)
            opts2 = selects[1].locator("option").all()
            for opt in opts2:
                txt = opt.inner_text()
                if "alert()" in txt or "Appel" in txt or "function" in txt.lower():
                    selects[1].select_option(label=txt); w(0.5)
                    log(f"  ✅ Condition: {txt}"); break
    except Exception as e:
        log(f"  ⚠️ Selects: {e}")

    # ── Webhook ──
    try:
        # Chercher la checkbox ou toggle webhook
        for wh_sel in [
            'input[id*="webhook"]',
            'input[type="checkbox"][id*="ookUrl"]',
            '[class*="webhook"] input',
            'label:has-text("URL") input[type="checkbox"]',
        ]:
            try:
                cb = page.locator(wh_sel).first
                if cb.is_visible(timeout=2000) and not cb.is_checked():
                    cb.click(); w(0.5)
                    log("  ✅ Webhook checkbox activée"); break
            except: continue

        # Remplir l'URL webhook
        for url_sel in [
            'input[placeholder*"http"]',
            'input[placeholder*="URL"]',
            '[class*="webhook"] input[type="text"]',
            'input[id*="webhook"]',
        ]:
            try:
                inp = page.locator(url_sel).first
                if inp.is_visible(timeout=2000):
                    inp.fill(WEBHOOK); w(0.3)
                    log(f"  ✅ URL: {WEBHOOK}"); break
            except: continue
    except Exception as e:
        log(f"  ⚠️ Webhook: {e}")

    # ── Message ──
    try:
        for msg_sel in [
            'textarea[placeholder*="essage"]',
            '[class*="message"] textarea',
            'textarea',
        ]:
            try:
                ta = page.locator(msg_sel).first
                if ta.is_visible(timeout=2000):
                    ta.fill("{{alert.message}}"); w(0.3)
                    log("  ✅ Message: {{alert.message}}"); break
            except: continue
    except Exception as e:
        log(f"  ⚠️ Message: {e}")

    # ── Nom ──
    try:
        for name_sel in ['input[placeholder*="Nom"]', 'input[placeholder*="Name"]', 'input[placeholder*="lerte"]']:
            try:
                inp = page.locator(name_sel).first
                if inp.is_visible(timeout=1500):
                    inp.fill(f"TradeMolty {coin} v6.1"); w(0.3)
                    log(f"  ✅ Nom: TradeMolty {coin} v6.1"); break
            except: continue
    except: pass

    shot(page, f"{coin}_alert_filled")

    # ── Créer ──
    try:
        for btn_sel in [
            'button:has-text("Créer")',
            'button:has-text("Save")',
            'button:has-text("Enregistrer")',
            'button[class*="submit"]',
            'button[type="submit"]',
        ]:
            try:
                btn = page.locator(btn_sel).last
                if btn.is_visible(timeout=2000):
                    btn.click(); w(3)
                    log(f"  ✅ Alerte créée ({btn_sel})")
                    shot(page, f"{coin}_alert_done")
                    return
            except: continue
        log("  ❌ Bouton Créer non trouvé")
        shot(page, f"{coin}_alert_stuck")
    except Exception as e:
        log(f"  ❌ Créer: {e}")

def process_chart(page, coin, skip_load=False):
    cfg = CHARTS[coin]
    log(f"\n{'='*55}\n  {coin}  |  BYBIT → Hyperliquid\n{'='*55}")

    page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30000)
    w(5)
    dismiss_popups(page)
    page.keyboard.press("Escape"); w(0.3)
    shot(page, f"{coin}_nav")

    # Timeframe 29M
    set_timeframe(page, coin)
    w(1)

    # Charger script Pine v6.1
    if not skip_load:
        try:
            content = read_script(cfg["script"])
            log(f"  📋 {cfg['script']} ({len(content)} chars)")
            open_pine_editor(page); w(1)
            paste_script(page, content, coin)
            add_to_chart(page, coin)
            w(2)
        except Exception as e:
            log(f"  ❌ Chargement script: {e}")

    # Créer alerte
    create_alert(page, coin)
    w(2)
    log(f"\n  🏁 {coin} terminé !")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--chart", choices=["SOL", "ETH", "ALL"], default="ALL")
    p.add_argument("--skip-load", action="store_true", help="Ne pas recharger le script Pine")
    args = p.parse_args()
    charts = ["SOL", "ETH"] if args.chart == "ALL" else [args.chart]

    log(f"\n🚀 TV Automation v3 (profil Chrome) — {charts}")
    log(f"   Profil : {CHROME_PROFILE}")

    # Copier le profil Chrome dans un dossier temp (Chrome ne peut pas tourner en parallèle)
    tmp_profile = tempfile.mkdtemp(prefix="tv_chrome_")
    log(f"   Profil tmp : {tmp_profile}")
    try:
        shutil.copytree(CHROME_PROFILE, os.path.join(tmp_profile, "Default"))
    except Exception as e:
        log(f"  ⚠️ Copie profil partielle: {e}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=tmp_profile,
            headless=False,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            slow_mo=80,
            viewport={"width": 1440, "height": 900},
            channel="chrome",   # utilise Chrome installé
        )
        page = browser.pages[0] if browser.pages else browser.new_page()

        log("\n⏳ Chargement TradingView...")
        page.goto("https://fr.tradingview.com/", wait_until="domcontentloaded", timeout=30000)
        w(4)
        shot(page, "start")

        # Vérifier connexion
        try:
            page.locator('[data-name="header-user-menu-button"]').wait_for(timeout=5000)
            log("  ✅ Connecté à TradingView")
        except:
            log("  ⚠️ Pas connecté — la création d'alertes risque d'échouer")
            shot(page, "not_logged_in")

        for coin in charts:
            try:
                process_chart(page, coin, skip_load=args.skip_load)
            except Exception as e:
                log(f"\n❌ Erreur {coin}: {e}")
                shot(page, f"{coin}_crash")

        log("\n✅ Terminé ! Vérification...")
        shot(page, "final")
        log("Appuie sur Entrée pour fermer...")
        input()
        browser.close()

    # Nettoyage
    try: shutil.rmtree(tmp_profile)
    except: pass

if __name__ == "__main__":
    main()
