#!/usr/bin/env python3
"""
TradingView Automation — JP
Charge les scripts Pine sur SOL et ETH via Chromium visible.
"""

import time, os, subprocess, pyotp
from playwright.sync_api import sync_playwright

TV_EMAIL = "hue.jp@hotmail.fr"
TV_PASS  = "Sebastien2"
TV_TOTP  = "hkGDPPzw"
WEBHOOK  = "https://hl-webhook-bot-production.up.railway.app/webhook/jp_bot_secret_2026"
DESKTOP  = os.path.expanduser("~/Desktop")
WORK     = os.path.expanduser("~/.openclaw/workspace/trading")

CHARTS = {
    "SOL": {"url": "https://fr.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDC.P", "script": "JP_v6_tradingview.pine"},
    "ETH": {"url": "https://fr.tradingview.com/chart/?symbol=BINANCE%3AETHUSDC.P", "script": "JP_v53_ETH_tradingview.pine"},
}

def log(m): print(m, flush=True)
def w(s): time.sleep(s)
def shot(page, n): page.screenshot(path=os.path.join(WORK, f"tv_{n}.png")); log(f"  📸 tv_{n}.png")

def get_totp(): return pyotp.TOTP(TV_TOTP).now()

def pbcopy(text):
    p = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
    p.communicate(text.encode('utf-8'))

def read_script(fn):
    for d in [DESKTOP, WORK]:
        path = os.path.join(d, fn)
        if os.path.exists(path):
            return open(path).read()
    raise FileNotFoundError(fn)

def login(page):
    log("\n🔐 Connexion...")
    page.goto("https://fr.tradingview.com/", wait_until="domcontentloaded", timeout=30000)
    w(3)
    # Déjà connecté ?
    try:
        page.locator('[data-name="header-user-menu-button"]').wait_for(timeout=3000)
        log("  ✅ Déjà connecté")
        return
    except: pass

    # Cliquer Se connecter
    for sel in ['button:has-text("Se connecter")', 'a:has-text("Se connecter")']:
        try: page.locator(sel).first.click(timeout=3000); w(1.5); break
        except: continue

    # Email
    try: page.locator('text=Email').first.click(timeout=3000); w(1)
    except: pass

    # Formulaire
    try:
        page.locator('input[name="username"]').fill(TV_EMAIL, timeout=5000); w(0.2)
        page.locator('input[name="password"]').fill(TV_PASS); w(0.2)
        page.locator('button[type="submit"]').click(); w(4)
    except Exception as e:
        log(f"  ⚠️ Formulaire: {e}"); shot(page, "login_fail"); return

    # TOTP
    try:
        otp = page.locator('input[inputmode="numeric"]')
        if otp.is_visible(timeout=5000):
            code = get_totp()
            log(f"  🔑 TOTP: {code}")
            otp.fill(code); w(2)
            try: page.locator('button[type="submit"]').click(); w(3)
            except: pass
    except: pass
    log("  ✅ Login OK")

def open_pine_editor(page):
    """Ouvre le panneau Pine Editor en bas."""
    log("  ⏳ Ouverture Pine Editor...")

    # Méthode 1 : cliquer l'onglet Pine Editor en bas
    try:
        page.locator('[data-name="bottom-area"] button, [class*="bottomBar"] button').filter(has_text="Pine Editor").click(timeout=4000)
        w(2); log("  ✅ Pine Editor (onglet bas)"); return True
    except: pass

    # Méthode 2 : data-name
    try:
        page.locator('[data-name="pine-editor-activate-btn"]').click(timeout=3000)
        w(2); log("  ✅ Pine Editor (data-name)"); return True
    except: pass

    # Méthode 3 : chercher bouton texte "Pine"
    try:
        page.get_by_role("tab", name="Pine Editor").click(timeout=3000)
        w(2); log("  ✅ Pine Editor (tab)"); return True
    except: pass

    # Méthode 4 : Échapper d'abord (quitter tout mode dessin)
    page.keyboard.press("Escape"); w(0.5)
    page.keyboard.press("Escape"); w(0.5)

    # Méthode 5 : Alt+P
    page.keyboard.press("Alt+p"); w(2)
    log("  ✅ Pine Editor (Alt+P)")
    return True

def focus_and_paste(page, content):
    """Focus sur l'éditeur CodeMirror et colle le contenu."""
    log("  ⏳ Focus + collage...")
    pbcopy(content)
    w(0.3)

    # D'abord Escape pour quitter tout mode dessin
    page.keyboard.press("Escape"); w(0.3)

    # Chercher le div éditable du Pine Editor
    selectors = [
        '.cm-content[contenteditable="true"]',
        '[class*="pine-editor"] .cm-content',
        '[class*="editor-section"] .cm-content',
        '.tv-pine-editor .cm-content',
        '[data-name="pine-editor-section"] .cm-content',
        '.cm-editor .cm-content',
    ]

    focused = False
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(timeout=3000)
            el.click(); w(0.3)
            focused = True
            log(f"  ✅ Focus ({sel})")
            break
        except: continue

    if not focused:
        log("  ⚠️  Sélecteurs échoués, clic au centre bas-gauche...")
        # Cliquer manuellement dans la zone éditeur (bas de l'écran)
        size = page.viewport_size
        page.mouse.click(size['width'] * 0.3, size['height'] * 0.85)
        w(0.5)

    # Ctrl+A pour tout sélectionner
    page.keyboard.press("Meta+a"); w(0.3)
    # Coller
    page.keyboard.press("Meta+v"); w(1.5)
    log("  ✅ Code collé (Cmd+V)")

def click_add_to_chart(page):
    """Clique le bouton Ajouter au chart."""
    log("  ⏳ Ajouter au chart...")

    selectors = [
        'button:has-text("Ajouter au chart")',
        '[data-name="add-to-chart"]',
        '[data-name="pine-editor-add-to-chart-btn"]',
        'button[aria-label="Ajouter au chart"]',
        '.tv-pine-editor-header button:first-child',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            btn.wait_for(timeout=3000)
            btn.click(); w(3)
            log(f"  ✅ Ajouté ({sel})")
            return True
        except: continue

    # Raccourci : Shift+Enter dans l'éditeur
    log("  ⚠️  Bouton non trouvé, Shift+Enter...")
    page.keyboard.press("Shift+Enter"); w(3)
    return True

def process(page, chart):
    cfg = CHARTS[chart]
    log(f"\n{'='*55}")
    log(f"  {chart}  |  {cfg['url']}")
    log('='*55)

    # Aller sur le chart
    page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30000)
    w(5)

    # Escape x2 (sortir du mode dessin si actif)
    page.keyboard.press("Escape"); w(0.3)
    page.keyboard.press("Escape"); w(0.3)
    shot(page, f"{chart}_loaded")

    # Ouvrir Pine Editor
    open_pine_editor(page)
    w(1)
    shot(page, f"{chart}_editor")

    # Lire script
    try:
        content = read_script(cfg["script"])
        log(f"  📋 Script lu ({len(content)} chars)")
    except FileNotFoundError as e:
        log(f"  ❌ {e}"); return

    # Focus + coller
    focus_and_paste(page, content)
    shot(page, f"{chart}_pasted")

    # Ajouter au chart
    click_add_to_chart(page)
    shot(page, f"{chart}_done")
    log(f"  ✅ {chart} terminé !")

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--chart", choices=["SOL","ETH","ALL"], default="ALL")
    args = p.parse_args()
    charts = ["SOL","ETH"] if args.chart == "ALL" else [args.chart]

    log(f"🚀 TV Automation — {charts}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--start-maximized"], slow_mo=60)
        ctx  = browser.new_context(viewport={"width":1440,"height":900})
        page = ctx.new_page()

        login(page); w(2)
        for chart in charts:
            process(page, chart)
            w(3)

        log("\n✅ Terminé ! Vérifie les charts.")
        log("Appuie sur Entrée pour fermer...")
        input()
        browser.close()

if __name__ == "__main__":
    main()
