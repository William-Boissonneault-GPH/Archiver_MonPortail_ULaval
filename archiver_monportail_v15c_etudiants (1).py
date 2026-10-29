"""monPortail V15c — archivage automatique des cours et sessions. Python 3.11+, Playwright.

  py -m pip install --user playwright
  py -m playwright install chromium
  py archiver_monportail_v15c_etudiants.py

Le script ne modifie pas les travaux. Il ne clique jamais sur Supprimer / Déposer.
L'interface monPortail n'est pas testable hors connexion : vérifiez le rapport après l'essai.
"""
from __future__ import annotations
import json, re, time, hashlib, traceback, zipfile, sys
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
PROFILE = Path.home() / '.monportail_archiver' / 'profil_chromium'  # réutilise l'authentification v3
START_URL = 'https://monportail.ulaval.ca/'
MAX_COURSES = 9999
MAX_MODULES = 100
MAX_SUBTABS = 25
MAX_EVALS = 120
DL_TIMEOUT = 7000
ARCHIVE = None
STATE = None
LOG = []
NAV_LANDING = None  # page d’entrée fiable du dernier cours ouvert


def clean(s, limit=130):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', (s or '').strip())
    s = re.sub(r'\s+', ' ', s).strip(' ._')
    return (s or 'Sans_nom')[:limit].rstrip(' .')


def ask_destination():
    root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
    selected = filedialog.askdirectory(title='Étape 1/3 — Choisissez le disque ou le dossier où sauvegarder les cours', parent=root)
    root.destroy()
    if not selected:
        raise SystemExit('Aucun dossier sélectionné. Rien téléchargé.')
    return Path(selected) / 'MonPortail_Archive'


def ask_skip_semesters(total):
    """Choose how many newest-listed semesters to skip (does not alter archives)."""
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    value = simpledialog.askinteger(
        'Étape 3/3 — Sessions à ignorer (0 = tout sauvegarder)',
        f'{total} sessions trouvées dans monPortail.\n\n'
        'Combien de sessions souhaitez-vous IGNORER au début ?\n'
        'Laissez 0 pour une sauvegarde complète (recommandé).\n'
        'Exemple : 2 ignore les deux premières sessions de la liste.\n\n'
        'Les fichiers déjà sauvegardés sont normalement réutilisés.',
        initialvalue=0,
        minvalue=0,
        maxvalue=total,
        parent=root,
    )
    root.destroy()
    if value is None:
        raise SystemExit('Sélection annulée. Aucun téléchargement lancé.')
    return value


def load_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text(encoding='utf-8'))
        except Exception: pass
    return {}


def save_state(state):
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    tmp=STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(STATE)


def log(msg):
    print(msg, flush=True)
    LOG.append(f'{datetime.now().isoformat(timespec="seconds")} {msg}')
    with (ARCHIVE / 'journal_execution.txt').open('a', encoding='utf-8') as f: f.write(LOG[-1]+'\n')


def safe_text(locator):
    try: return locator.first.inner_text(timeout=1300).strip()
    except Exception: return ''


def fingerprint(page):
    try:
        return page.evaluate('''() => location.href+'|'+(document.body.innerText||'').replace(/\\s+/g,' ').slice(0,18000)''')
    except Exception: return page.url


def wait_changed(page, before, ms=9000):
    try:
        page.wait_for_function('''old => location.href+'|'+(document.body.innerText||'').replace(/\\s+/g,' ').slice(0,18000) !== old''', arg=before, timeout=ms, polling=350)
        page.wait_for_timeout(450); return True
    except PWTimeout: return fingerprint(page) != before


def course_info(page):
    body = safe_text(page.locator('body'))
    m = re.search(r'\b[A-Z]{2,5}-\d{4}\b', body)
    term = re.search(r'\b(Automne|Hiver|Été|Ete|Printemps)\s+20\d{2}\b', body, re.I)
    # En général le titre du cours apparaît à côté du code dans l'en-tête.
    match = re.search(r'([^\n]{5,100})\n\s*([A-Z]{2,5}-\d{4})\s*\(', body)
    title = match.group(1).strip() if match else (safe_text(page.locator('h1')) or safe_text(page.locator('h2')) or 'Cours')
    return (clean(term.group(0)) if term else 'Session_inconnue', m.group(0) if m else 'CODE_inconnu', clean(title))


def choose_link(page, label, exact=True, scope=None):
    target = scope if scope is not None else page
    locator = target.get_by_text(label, exact=exact)
    try:
        for n in range(min(locator.count(), 10)):
            el = locator.nth(n)
            if el.is_visible(): el.click(timeout=5000); return True
    except Exception: pass
    return False


def menu_courses_visible(page):
    """Read visible links inside the actual ADF popover launched by #lienListeCoursId."""
    return page.evaluate(r"""() => {
      const toggle=document.getElementById('lienListeCoursId');
      if(!toggle) return [];
      const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>2&&r.height>2&&s.display!=='none'&&s.visibility!=='hidden'};
      const tr=toggle.getBoundingClientRect();
      // ADF may render the popup elsewhere in the DOM, sometimes in a portal.
      const candidates=[...document.querySelectorAll('div,section,ul,[role="dialog"],[role="menu"]')].filter(e=>{
        if(!visible(e)) return false;
        const r=e.getBoundingClientRect();
        return r.width>=180&&r.width<=650&&r.height>=65&&r.height<=950&&
          r.left>tr.left-380&&r.left<tr.right+80&&r.top>=tr.top-15&&r.top<tr.bottom+130;
      });
      const panels=candidates.map(el=>({el,links:[...el.querySelectorAll('a,[role="link"]')].filter(visible).map(a=>({name:(a.innerText||a.textContent||'').replace(/\s+/g,' ').trim(),href:a.href||'',id:a.id||''})).filter(x=>x.name&&!/^Liste des cours$/i.test(x.name))})).filter(x=>x.links.length>0);
      // Prefer smallest area with >=2 links and visible semester selector.
      panels.sort((a,b)=>{
        const score=p=>(p.el.querySelector('select,[role="combobox"]')?1000:0)+Math.min(p.links.length,30)*20-p.el.getBoundingClientRect().width*p.el.getBoundingClientRect().height/20000;
        return score(b)-score(a);
      });
      return panels.length?panels[0].links:[];
    }""")

def open_course_menu(page):
    """Known exact selector from Chrome DevTools; don't use text or screen coordinates."""
    button=page.locator('#lienListeCoursId')
    try:
        button.wait_for(state='visible',timeout=12000)
        if len(menu_courses_visible(page))>=1:
            return True
        button.click(timeout=8000)
        for _ in range(12):
            page.wait_for_timeout(250)
            if len(menu_courses_visible(page))>=1:
                return True
    except Exception as exc:
        log('[MENU] '+str(exc)[:180])
    try:
        page.screenshot(path=str(ARCHIVE/'diagnostic_menu.png'))
        (ARCHIVE/'diagnostic_menu.html').write_text(page.content(),encoding='utf-8')
        log('[DIAGNOSTIC] diagnostic_menu.png et diagnostic_menu.html enregistrés')
    except Exception: pass
    return False

def return_to_introduction(page):
    """Return to the actual course Introduction link in the LEFT navigation.

    Avoid relying on page.url: modules and evaluations have separate addresses,
    and an ADF redirect can leave an incorrect landing URL behind.
    """
    # This short explicit operation does not browse browser history.
    candidates = page.get_by_text('Introduction', exact=True)
    for i in range(min(candidates.count(), 20)):
        try:
            node = candidates.nth(i)
            if not node.is_visible():
                continue
            rect = node.bounding_box()
            if not rect or rect['x'] >= 480 or rect['y'] < 100:
                continue
            # Check that this is a navigation link, not a course-content heading.
            if not node.evaluate("el => !!el.closest('a,[role=link],li,nav,.af_panelList')"):
                continue
            previous = page.url
            node.click(timeout=4500)
            page.wait_for_timeout(600)
            log('[NAVIGATION] Introduction du cours ouverte'+(' (URL actualisée)' if page.url != previous else ''))
            return True
        except Exception:
            continue
    log('[NAVIGATION] Lien Introduction du menu gauche introuvable')
    return False


def ensure_course_menu(page):
    """Prefer real Introduction navigation before invoking the course dropdown."""
    global NAV_LANDING
    # First try the current page; most sections keep the header visible.
    try:
        if page.locator('#lienListeCoursId').is_visible() and open_course_menu(page):
            return True
    except Exception:
        pass
    # On some ADF pages the course header/menu is not accessible. Prefer
    # the course's own Introduction link instead of a stale entry URL.
    if return_to_introduction(page):
        NAV_LANDING = page.url
        if open_course_menu(page):
            return True
    if NAV_LANDING:
        try:
            page.goto(NAV_LANDING, wait_until='domcontentloaded', timeout=12000)
            if return_to_introduction(page):
                NAV_LANDING = page.url
            if open_course_menu(page):
                return True
        except Exception as exc:
            log('[NAVIGATION] URL de secours inaccessible: '+str(exc)[:150])
    log('[ÉCHEC NAVIGATION] Impossible de retrouver Introduction et Liste des cours')
    return False

def get_courses_from_menu(page):
    """Return link texts for the current semester only, never the nav sidebar."""
    raw=menu_courses_visible(page)
    result=[]; seen=set()
    for item in raw:
        title=re.sub(r'\s+',' ',item.get('name','')).strip()
        if not title or title in seen:continue
        if re.match(r'^(Automne|Hiver|Été|Ete|Printemps)\s+20\d\d$',title,re.I):continue
        if title.lower() in ('liste des cours','cours','archives','plan de cours'):continue
        result.append({'name':title,'href':item.get('href',''),'id':item.get('id','')})
        seen.add(title)
    return result

def click_menu_course(page,name):
    global NAV_LANDING
    if not ensure_course_menu(page):
        log('  [ÉCHEC] Le menu des cours ne s’ouvre pas');return False
    entries=get_courses_from_menu(page)
    match=next((x for x in entries if x['name']==name),None)
    if not match:
        log('  [ÉCHEC] Cours absent du menu : '+name);return False
    before=fingerprint(page)
    # Use the actual ADF link ID when present, otherwise exact visible anchor text.
    try:
        if match.get('id'):
            target=page.locator('[id="'+match['id'].replace('"','')+'"]')
            if target.count():target.first.click(timeout=10000)
            else:raise ValueError('identifiant absent')
        else:
            links=page.locator('a,[role="link"]').filter(has_text=re.compile(r'^\s*'+re.escape(name)+r'\s*$',re.I))
            matchloc=next((links.nth(i) for i in range(min(links.count(),25)) if links.nth(i).is_visible()),None)
            if matchloc is None:raise ValueError('lien visible absent')
            matchloc.click(timeout=10000)
        if not wait_changed(page,before,16000):
            log('  [ÉCHEC] Aucun changement de page détecté pour '+name)
            return False
        NAV_LANDING = page.url
        return True
    except Exception as exc:
        log('  [ÉCHEC] Navigation vers '+name+' : '+str(exc)[:180])
        return False

def unique_target(folder, filename):
    folder.mkdir(parents=True, exist_ok=True)
    target=folder / clean(filename,170)
    k=2
    while target.exists():
        target=folder / (clean(Path(filename).stem,140)+f'_{k}'+Path(filename).suffix)
        k+=1
    return target


def download_action(page, action, folder, key, state, label):
    previous=state.get(key)
    if previous and Path(previous.get('path','')).is_file():
        log('    [déjà] '+label); return True
    try:
        folder.mkdir(parents=True,exist_ok=True)
        with page.expect_download(timeout=DL_TIMEOUT) as handle: action()
        item=handle.value
        target=unique_target(folder,item.suggested_filename)
        item.save_as(str(target))
        if not target.is_file() or target.stat().st_size==0: raise ValueError('Téléchargement vide')
        if target.suffix.lower()=='.zip' and not zipfile.is_zipfile(target): raise ValueError('ZIP invalide')
        state[key]={'path':str(target), 'size':target.stat().st_size,'source':page.url,'date':datetime.now().isoformat(timespec='seconds')}
        save_state(state)
        log('    [OK] '+label+' : '+target.name)
        return True
    except Exception as e:
        log('    [À VÉRIFIER] '+label+' : '+str(e)[:160])
        return False


def download_page_files(page, folder, prefix, state):
    """ADF: wait for either a direct download or its multi-file confirmation popup."""
    matches = page.get_by_text('Télécharger les fichiers', exact=False)
    btn = None
    for i in range(min(matches.count(), 8)):
        try:
            if matches.nth(i).is_visible():
                btn = matches.nth(i)
                break
        except Exception:
            pass
    if btn is None:
        return False

    key = prefix + '|bouton_fichiers'
    if state.get(key) and Path(state[key].get('path', '')).is_file():
        return True

    received = []
    def record(download):
        received.append(download)

    # ADF popups are not necessarily ARIA dialogs. Their button ID contains
    # popupTelechCnt and cmdOk:BtnPrincipal (the prefix varies by course).
    popup_ok = page.locator('button[id*="popupTelechCnt"][id*="cmdOk"]')
    fallback_ok = page.locator('button[id*="cmdOk"][id*="BtnPrincipal"]')
    popup_title = page.get_by_text('Télécharger les fichiers de la page', exact=False)

    def visible_ok():
        for options in (popup_ok, fallback_ok):
            try:
                for i in range(min(options.count(), 6)):
                    candidate = options.nth(i)
                    if candidate.is_visible() and candidate.is_enabled():
                        return candidate
            except Exception:
                pass
        return None

    page.on('download', record)
    try:
        btn.click(timeout=6500)
        confirmation = None
        # 15 s is for popup appearance / download START, not file completion.
        # Polling prevents a 7 s stall when an ADF popup opens immediately.
        for _ in range(150):
            if received:
                break
            confirmation = visible_ok()
            if confirmation is not None:
                break
            page.wait_for_timeout(100)

        if received:
            dl = received[0]
        elif confirmation is not None:
            # Listener is already active: even a very fast download is captured.
            confirmation.click(timeout=6500)
            for _ in range(150):
                if received:
                    break
                page.wait_for_timeout(100)
            if not received:
                raise TimeoutError('Aucun téléchargement démarré dans les 15 s après OK')
            dl = received[0]
        else:
            if popup_title.count() and popup_title.first.is_visible():
                log('    [À VÉRIFIER] Fenêtre de téléchargement visible, bouton OK introuvable')
            else:
                log('    [À VÉRIFIER] Ni téléchargement direct ni fenêtre ADF détectée')
            return False

        destination = unique_target(folder, dl.suggested_filename)
        dl.save_as(str(destination))  # waits for the complete file, including large ZIPs
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ValueError('Téléchargement vide')
        if destination.suffix.lower() == '.zip' and not zipfile.is_zipfile(destination):
            raise ValueError('ZIP invalide')
        state[key] = {'path': str(destination), 'size': destination.stat().st_size,
                      'source': page.url}
        save_state(state)
        log('    [OK] ' + destination.name)
        return True
    except Exception as exc:
        log('    [À VÉRIFIER] téléchargement fichiers : ' + str(exc)[:180])
        return False
    finally:
        page.remove_listener('download', record)

def fetch_file_links(page, folder, prefix, state, restrict_to_main=True):
    """Download recognized document links, including monPortail's extensionless file route."""
    raw=page.evaluate(r"""() => {
      return [...document.querySelectorAll('a[href]')].filter(a=>{
        const t=(a.innerText||'').trim(), h=a.getAttribute('href')||'',cl=a.className||'';
        const r=a.getBoundingClientRect();if(r.width<1||r.height<1)return false;
        return /\.(pdf|zip|docx?|xlsx?|pptx?|txt|csv|tex|ipynb)(?:[?#]|$)/i.test(h) ||
          /\.(pdf|zip|docx?|xlsx?|pptx?|txt|csv|tex|ipynb)$/i.test(t) ||
          /FichierFCK|ExtensionPDF|ExtensionDOC|ExtensionXLS|ExtensionPPT/i.test(cl) ||
          /\/analytique\/evenement\/fichier/i.test(h);
      }).map(a=>({text:(a.innerText||'').trim(),href:a.href,domId:a.id||'',className:a.className||''}));
    }""")
    seen=set();done=0; external=[]
    for item in raw[:250]:
        href=item['href'];label=item['text'] or Path(href.split('?')[0]).name
        if href in seen:continue
        seen.add(href)
        # External reference material (e.g. ULaval policy pages) must not trigger a
        # browser download wait. It is listed for later manual review instead.
        from urllib.parse import urlparse
        parsed=urlparse(href)
        if parsed.netloc.lower() not in ('sitescours.monportail.ulaval.ca', 'monportail.ulaval.ca'):
            external.append({'titre':label,'url':href,'raison':'référence externe non téléchargée'})
            continue
        # Only download actual file URLs, never generic page navigation links.
        if not (re.search(r'\.(pdf|zip|docx?|xlsx?|pptx?|txt|csv|tex|ipynb)(?:[?#]|$)',href,re.I)
                or re.search(r'\.(pdf|zip|docx?|xlsx?|pptx?|txt|csv|tex|ipynb)$',label,re.I)
                or '/analytique/evenement/fichier' in href.lower()
                or re.search(r'FichierFCK|ExtensionPDF|ExtensionDOC|ExtensionXLS|ExtensionPPT',item['className'],re.I)):
            continue
        key=prefix+'|file|'+hashlib.sha256(href.encode()).hexdigest()[:22]
        if state.get(key) and Path(state[key].get('path','')).is_file():continue
        # Download via authenticated request. This also captures PDFs opened inline in browser.
        try:
            response=page.context.request.get(href,timeout=5000)
            ct=response.headers.get('content-type','').lower()
            data=response.body()
            if response.ok and data and not any(t in ct for t in ('text/html','application/json')) and len(data)<400_000_000:
                cd=response.headers.get('content-disposition','')
                match=re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)',cd,re.I)
                filename=(match.group(1) if match else '') or Path(href.split('?')[0]).name
                filename=filename.split('/')[-1]
                if not Path(filename).suffix:
                    if Path(label).suffix:filename=label
                    elif 'pdf' in ct or 'ExtensionPDF' in item['className']:filename=clean(label)+'.pdf'
                    elif 'zip' in ct:filename=clean(label)+'.zip'
                    else:filename=clean(label)+'.bin'
                dest=unique_target(folder,filename)
                dest.write_bytes(data)
                if 'pdf' in ct and dest.read_bytes()[:4]!=b'%PDF':
                    dest.unlink();raise ValueError('Réponse PDF incorrecte')
                state[key]={'path':str(dest),'size':len(data),'source':href};save_state(state)
                log('    [OK] fichier '+dest.name);done+=1;continue
        except Exception:pass
        try:
            if item['domId']:
                anchor=page.locator('[id="'+item['domId'].replace('"','')+'"]')
            else:
                anchor=page.locator('a[href]').filter(has_text=item['text']).first if item['text'] else None
            if anchor is not None and anchor.count() and anchor.first.is_visible():
                if download_action(page,lambda:anchor.first.click(timeout=3500),folder,key,state,'lien '+label):done+=1;continue
        except Exception:pass
        log('    [À VÉRIFIER] fichier non récupéré : '+label[:100]+' ('+href[:100]+')')
    if external:
        folder.mkdir(parents=True,exist_ok=True)
        fp=folder/'references_externes.json'
        previous=[]
        if fp.exists():
            try: previous=json.loads(fp.read_text(encoding='utf-8'))
            except Exception: pass
        by_url={e['url']:e for e in previous if isinstance(e,dict) and e.get('url')}
        by_url.update({e['url']:e for e in external})
        fp.write_text(json.dumps(list(by_url.values()),ensure_ascii=False,indent=2),encoding='utf-8')
        log(f'    [INFO] {len(external)} références externes consignées (sans attente de téléchargement)')
    return done

def save_metadata(page,folder,name):
    folder.mkdir(parents=True,exist_ok=True)
    try:
        text=page.locator('body').inner_text(timeout=4000)
        (folder / (clean(name)+'.txt')).write_text('URL: '+page.url+'\n\n'+text,encoding='utf-8')
    except Exception: pass


def click_section(page,name):
    before=fingerprint(page)
    # Plusieurs 'Évaluations et résultats' sur une page : préférer le menu de gauche.
    candidates=page.get_by_text(name,exact=True)
    for i in range(min(candidates.count(),12)):
        try:
            el=candidates.nth(i)
            if not el.is_visible(): continue
            rect=el.bounding_box()
            if rect and rect['x']<500:
                el.click(timeout=4500);wait_changed(page,before,8000);return True
        except Exception: pass
    return False


def next_arrow(page):
    script='''() => {
      const nodes=[...document.querySelectorAll('button,a,[role="button"]')];
      const choices=nodes.filter(el=>{
        const r=el.getBoundingClientRect();
        if(r.width<8||r.height<8||r.width>150||r.height>80||r.top<120||r.top>550||r.left<innerWidth*.48)return false;
        const meta=((el.getAttribute('aria-label')||'')+' '+(el.getAttribute('title')||'')+' '+(el.textContent||'')).trim().toLowerCase();
        return /^(>|›|»|suivant|next)$/.test(meta)||/suivant|next/.test(meta)||!!el.querySelector('svg,i[class*="chevron"],i[class*="arrow"]');
      });
      choices.sort((a,b)=>a.getBoundingClientRect().top-b.getBoundingClientRect().top || b.getBoundingClientRect().left-a.getBoundingClientRect().left);
      if(!choices.length)return false;
      choices[0].setAttribute('data-archive-next','yes');return true;
    }'''
    try:
        if not page.evaluate(script):return False
        old=fingerprint(page)
        page.locator('[data-archive-next="yes"]').first.click(timeout=5000)
        return wait_changed(page,old,8000)
    except Exception:return False


def discover_module_links(page):
    """Collect ONLY module links from Contenu et activités overview.

    Observed monPortail ADF links: <a class="af_goLink" href="/ena/site/module?idSite=...&idModule=...">
    Parent rows may be headings or module entries; only links with idModule count.
    """
    return page.evaluate(r"""() => {
      const found=[], seen=new Set();
      for(const a of document.querySelectorAll('a[href*="/ena/site/module"]')) {
        const href=a.href||'';
        const url=new URL(href,location.href);
        const id=url.searchParams.get('idModule');
        if (!id || seen.has(id)) continue;
        const rect=a.getBoundingClientRect();
        if(rect.width<2 || rect.height<2)continue;
        const name=(a.innerText||a.textContent||'').replace(/\s+/g,' ').trim();
        if(!name)continue;
        seen.add(id);
        found.push({id,name,href,domId:a.id||''});
      }
      return found;
    }""")


def archive_open_module(page, root, state, coursekey, i, name, idmodule):
    folder=root/'01 - Contenu et activités'/f'{i:02d} - {clean(name)}'
    log(f'  [MODULE {i}] {name} (idModule={idmodule})')
    # Always save base page, even when it also has tabs.
    save_metadata(page,folder,'Page_du_module')
    tabs=module_subtabs(page)
    if tabs:
        log('    [SOUS-ONGLETS] '+', '.join(t['name'] for t in tabs))
        for ti,tab in enumerate(tabs[:MAX_SUBTABS],1):
            if not click_subtab(page,tab):
                log('    [À VÉRIFIER] sous-onglet non ouvert '+tab['name'])
                continue
            subfolder=folder/f'{ti:02d} - {clean(tab["name"])}'
            subkey=coursekey+f'|module|{idmodule}|onglet|{tab["name"]}'
            download_page_files(page,subfolder,subkey,state)
            fetch_file_links(page,subfolder/'Fichiers_individuels',subkey,state)
            save_metadata(page,subfolder,'Contenu_du_sous_onglet')
    else:
        key=coursekey+f'|module|{idmodule}'
        download_page_files(page,folder,key,state)
        fetch_file_links(page,folder/'Fichiers_individuels',key,state)
        save_metadata(page,folder,'Contenu_du_module')


def find_module_section(page):
    """Locate a course's module index even when its menu label is customized.

    Only accepts a sidebar section after seeing genuine idModule links.
    Doesn't click course-policy or reference links in the body.
    """
    # Start with the names observed on monPortail; this is a bounded scan.
    names=['Contenu et activités','Feuille de route','Modules','Parcours',
           'Planification','Calendrier','Contenu du cours','Séances','Cours et activités']
    # Also gather names from the COURSE sidebar, excluding fixed administrative sections.
    excluded={'Introduction','Plan de cours','Informations générales',
              'Description du cours','Évaluations et résultats','Matériel didactique',
              'Médiagraphie et annexes','Bibliographie','Outils',
              'Envoi de courriel','Ma réussite','Forums','Questionnaires',
              'Classes virtuelles','Liste des cours','Sommaire des résultats'}
    try:
        nav=page.evaluate(r"""() => [...document.querySelectorAll('a,[role="link"]')]
          .filter(a=>{let r=a.getBoundingClientRect();return r.width>2&&r.height>2
            &&r.left>190&&r.left<490&&r.top>130})
          .map(a=>(a.innerText||a.textContent||'').replace(/\s+/g,' ').trim())
          .filter(s=>s&&s.length<65)""")
        for name in nav:
            if name not in excluded and name not in names and not name.lower().startswith('plan de cours'):
                names.append(name)
    except Exception: pass
    for name in names[:22]:
        try:
            if not click_section(page,name): continue
            page.wait_for_timeout(250)
            links=discover_module_links(page)
            if links:
                log(f'  [SECTION MODULES] {name} — {len(links)} modules')
                return name
        except Exception as exc:
            log(f'  [SECTION MODULES] essai {name} : {str(exc)[:80]}')
    return None


def archive_modules(page,root,state,coursekey):
    section=find_module_section(page)
    if section is None:
        log('  [À VÉRIFIER] aucune section avec liens idModule détectée')
        return
    page.wait_for_timeout(650)
    listing_url=page.url
    links=discover_module_links(page)
    log(f'  [LISTE MODULES] {len(links)} liens de modules détectés')
    if links:
        # Return to the index for each module: do not use the next arrows here.
        for i,item in enumerate(links[:MAX_MODULES],1):
            try:
                # Re-enter index in case ADF updated the DOM while visiting a module.
                if i>1:
                    if not click_section(page,section):
                        page.goto(listing_url,wait_until='domcontentloaded',timeout=20000)
                    page.wait_for_timeout(400)
                current=discover_module_links(page)
                target=next((x for x in current if x['id']==item['id']),item)
                before=fingerprint(page)
                # Clicking the actual anchor lets ADF manage navigation correctly.
                # If it was not rendered, use its observed absolute href.
                loc=page.locator('a[href*="/ena/site/module"]').filter(has_text=item['name'])
                if target.get('domId') and page.locator('[id="'+target['domId'].replace('"','')+'"]').count():
                    page.locator('[id="'+target['domId'].replace('"','')+'"]').first.click(timeout=8500)
                elif loc.count():
                    loc.first.click(timeout=8500)
                else:
                    page.goto(item['href'],wait_until='domcontentloaded',timeout=20000)
                wait_changed(page,before,11000)
                # Some ADF calls update asynchronously; the visible content should
                # no longer be the overview table and ideally contain an idModule URL.
                if '/ena/site/module' not in page.url:
                    page.wait_for_timeout(950)
                if '/ena/site/module' not in page.url and fingerprint(page)==before:
                    log('  [À VÉRIFIER] module non ouvert : '+item['name'])
                    continue
                archive_open_module(page,root,state,coursekey,i,item['name'],item['id'])
            except Exception as exc:
                log('  [ERREUR MODULE] '+item['name']+' : '+str(exc)[:180])
        if len(links)>MAX_MODULES:
            log(f'  [À VÉRIFIER] {len(links)-MAX_MODULES} modules au-delà de la limite MAX_MODULES')
        return
    # Some course sites land directly on the first module instead of listing all.
    # Keep the previously working next-arrow method for those courses.
    log('  [INFO] Aucun lien idModule sur la page : navigation par flèches (méthode V5)')
    seen=set()
    for i in range(1,MAX_MODULES+1):
        sig=fingerprint(page)
        if sig in seen:break
        seen.add(sig)
        body=safe_text(page.locator('body'))
        match=re.search(r'Semaines?\s+\d+(?:\s+et\s+\d+)?[^\n]{0,75}',body,re.I)
        name=clean(match.group(0)) if match else f'Module_{i:02d}'
        archive_open_module(page,root,state,coursekey,i,name,f'fallback_{i}_{hashlib.sha256(page.url.encode()).hexdigest()[:8]}')
        if not next_arrow(page):break

def get_eval_entries(page):
    """Only genuine evaluation entries in assessment tables (not anchor menus).

    Real monPortail examples contain class 'evaluation-texte' and links to
    /ena/site/evaluation?idSite=...&idEvaluation=... .
    """
    return page.evaluate(r"""() => {
      const seen=new Set(), out=[];
      const links=[...document.querySelectorAll('a[class*="evaluation-texte"], a[href*="idEvaluation="]')];
      for(const a of links){
        const tr=a.closest('tr');
        if(!tr || !tr.closest('table'))continue;
        const title=(a.textContent||'').replace(/\s+/g,' ').trim();
        const href=a.getAttribute('href')||'';
        const id=href.match(/[?&]idEvaluation=(\d+)/i)?.[1] ||
                 (a.getAttribute('id')||'').match(/idEvaluation[^0-9]*(\d+)/i)?.[1];
        if(!title || (![...a.classList].some(c=>c.includes('evaluation-texte')) && !id))continue;
        const key=id||title;
        if(seen.has(key))continue;
        seen.add(key);
        out.push({title,id:id||'',href:a.href||'',domId:a.id||''});
      }
      return out;
    }""")


def click_eval_entry(page, item):
    """Navigate via exact evaluation ID when possible; otherwise click table entry."""
    try:
        if item.get('id'):
            loc=page.locator('table a[class*="evaluation-texte"][href*="idEvaluation='+item['id']+'"]')
            if not loc.count():loc=page.locator('table a[href*="idEvaluation='+item['id']+'"]')
        elif item.get('domId'):
            loc=page.locator('[id="'+item['domId'].replace('"','')+'"]')
        else:
            loc=page.locator('table a[class*="evaluation-texte"]').filter(has_text=item['title'])
        if not loc.count():return False
        before=fingerprint(page)
        loc.first.click(timeout=8000)
        wait_changed(page,before,10000)
        return ('idEvaluation=' in page.url or item['title'].lower() in safe_text(page.locator('body')).lower())
    except Exception as e:
        log('  [À VÉRIFIER] clic évaluation '+item['title']+': '+str(e)[:110]);return False


def module_subtabs(page):
    """Detect active clickable ADF tabs nested in module pages."""
    return page.evaluate(r"""() => {
      const isVisible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>5&&r.height>5&&s.visibility!=='hidden'&&s.display!=='none'};
      const result=[],seen=new Set();
      for(const a of document.querySelectorAll('a.ul_customizablePanelTabbed_tab-link, .ul_customizablePanelTabbed_tab a')){
        if(!isVisible(a))continue;
        const name=(a.textContent||'').replace(/\s+/g,' ').trim();
        if(!name || seen.has(name))continue;
        seen.add(name);result.push({name,id:a.id||''});
      }
      return result.slice(0,25);
    }""")


def click_subtab(page, item):
    try:
        if item.get('id'):
            loc=page.locator('[id="'+item['id'].replace('"','')+'"]')
        else:
            loc=page.locator('a.ul_customizablePanelTabbed_tab-link').filter(has_text=item['name'])
        if not loc.count():return False
        loc.first.click(timeout=6500)
        page.wait_for_timeout(600)
        return True
    except Exception as e:
        log('    [À VÉRIFIER] sous-onglet '+item['name']+': '+str(e)[:110]);return False


def save_result_summary(page,folder):
    """Compact grade record, not a full dump of the monPortail interface."""
    folder.mkdir(parents=True,exist_ok=True)
    body=safe_text(page.locator('body'))
    # Find the active results panel, if available.
    pos=body.rfind('Correction par l’enseignant')
    if pos<0:pos=body.rfind("Correction par l'enseignant")
    result_pos=body.find('Statistiques du groupe')
    if result_pos<0:result_pos=body.find('Note :')
    portion=body[max(0,result_pos-100):] if result_pos>=0 else body
    grade=re.search(r'\bNote\s*:\s*(\d+(?:[,.]\d+)?)\s*%',portion,re.I)
    if not grade:
        grade=re.search(r'\bNote\s*:\s*(\d+(?:[,.]\d+)?)\s*%',body,re.I)
    teacher=body[pos:] if pos>=0 else ''
    points=re.search(r'\bNote\s*:\s*(\d+(?:[,.]\d+)?)\s*/\s*(\d+(?:[,.]\d+)?)',teacher,re.I)
    mean=re.search(r'\bMoyenne\s*:\s*(\d+(?:[,.]\d+)?)\s*%',portion,re.I)
    median=re.search(r'\bMédiane\s*:\s*(\d+(?:[,.]\d+)?)\s*%',portion,re.I)
    std=re.search(r'\bÉcart-type\s*:\s*(\d+(?:[,.]\d+)?)',portion,re.I)
    payload={'url':page.url,'note_pourcent':grade.group(1) if grade else None,
             'note_correcteur':(points.group(1)+' / '+points.group(2)) if points else None,
             'moyenne_groupe_pourcent':mean.group(1) if mean else None,
             'mediane_groupe_pourcent':median.group(1) if median else None,
             'ecart_type_groupe':std.group(1) if std else None}
    (folder/'resultats.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    log('    [NOTE] '+str(payload['note_pourcent'] or payload['note_correcteur'] or 'non affichée'))


def archive_evaluation(page,root,state,coursekey,index,title):
    base=root/'03 - Évaluations et résultats'/f'{index:02d} - {clean(title)}'
    log('  [ÉVALUATION] '+title)
    for tab,folder_name in [('Description','01 - Énoncé'),('Boîte de dépôt','02 - Ma soumission'),('Résultats','03 - Correction et résultats')]:
        if not choose_link(page,tab,exact=True):
            log('    [À VÉRIFIER] onglet absent : '+tab);continue
        page.wait_for_timeout(450)
        folder=base/folder_name
        if tab=='Résultats':save_result_summary(page,folder)
        else:save_metadata(page,folder,tab)
        key=coursekey+f'|eval|{index}|{title}|{tab}'
        if tab=='Description': download_page_files(page,folder,key,state)
        fetch_file_links(page,folder,key,state)
        # Les liens AJAX sans extension peuvent représenter une soumission ou une correction.
        # Capture leurs intitulés pour signaler ceux qu'il faudra vérifier.
        if tab!='Description':
            try:
                all_links=page.evaluate('''() => [...document.querySelectorAll('a')].map(a=>({label:(a.innerText||'').trim(),href:a.href||''})).filter(a=>/\\.(pdf|docx?|zip|xlsx?)/i.test(a.label))''')
                (folder/'liens_documents.json').write_text(json.dumps(all_links,ensure_ascii=False,indent=2),encoding='utf-8')
            except Exception:pass


def archive_evaluations(page,root,state,coursekey):
    if not click_section(page,'Évaluations et résultats'):
        log('  [À VÉRIFIER] section Évaluations et résultats inaccessible');return
    base_url=page.url
    base_sig=fingerprint(page)
    save_metadata(page,root/'03 - Évaluations et résultats','Liste_des_evaluations')
    entries=get_eval_entries(page)
    entries=[e for e in entries if e['title']]
    unique=[];seen=set()
    for e in entries:
        pair=(e['title'],e['href'])
        if pair not in seen: unique.append(e);seen.add(pair)
    log(f'  [ÉVALUATIONS REPÉRÉES] {len(unique)}')
    if not unique: log('  [À VÉRIFIER] aucune évaluation repérée — vérifier le journal et la page')
    for i,e in enumerate(unique[:MAX_EVALS],1):
        try:
            if not click_section(page,'Évaluations et résultats'):
                page.goto(base_url,wait_until='domcontentloaded',timeout=20000)
            page.wait_for_timeout(500)
            before=fingerprint(page)
            if not click_eval_entry(page,e):
                log('  [À VÉRIFIER] entrée évaluation introuvable '+e['title']);continue
            wait_changed(page,before,6500)
            archive_evaluation(page,root,state,coursekey,i,e['title'])
        except Exception as exc:
            log('  [ERREUR ÉVALUATION] '+e['title']+' : '+str(exc)[:160])


def archive_course(page,state):
    term,code,title=course_info(page)
    root=ARCHIVE/term/clean(f'{code} - {title}')
    root.mkdir(parents=True,exist_ok=True)
    key=f'{term}|{code}|{title}'
    log(f'\n===== COURS {term} / {code} / {title} =====')
    save_metadata(page,root/'00 - Introduction','Introduction')
    for section,folder in [('Plan de cours','02 - Plan de cours'),('Matériel didactique','04 - Matériel didactique'),('Médiagraphie et annexes','05 - Médiagraphie et annexes'),('Informations générales','06 - Autres pages'),('Description du cours','06 - Autres pages')]:
        try:
            if click_section(page,section):
                path=root/folder
                save_metadata(page,path,section)
                download_page_files(page,path,key+'|section|'+section,state)
                fetch_file_links(page,path,key+'|section|'+section,state)
            else: log('  [À VÉRIFIER] section absente '+section)
        except Exception as exc:log('  [ERREUR SECTION] '+section+': '+str(exc)[:150])
    archive_modules(page,root,state,key)
    archive_evaluations(page,root,state,key)
    log('[FIN COURS] '+code+' (voir les mentions À VÉRIFIER)')


def semester_menu_info(page):
    """Inspect semester picker in the visible course popover (native SELECT or ADF combo)."""
    return page.evaluate(r"""() => {
      const btn=document.getElementById('lienListeCoursId');if(!btn)return {terms:[],current:''};
      const b=btn.getBoundingClientRect();
      const visible=e=>{let r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.visibility!=='hidden'&&s.display!=='none'};
      const term=/^(Automne|Hiver|Été|Ete|Printemps)\s+20\d{2}$/i;
      let candidates=[...document.querySelectorAll('select,[role="combobox"],input[role="combobox"],.af_selectOneChoice')].filter(visible).filter(e=>{let r=e.getBoundingClientRect();return Math.abs(r.left-b.left)<500&&r.top>=b.top-20&&r.top<b.bottom+500});
      candidates.sort((a,c)=>Math.abs(a.getBoundingClientRect().top-(b.bottom+25))-Math.abs(c.getBoundingClientRect().top-(b.bottom+25)));
      const el=candidates[0];if(!el)return {terms:[],current:'',id:''};
      let opts=[...el.querySelectorAll('option')].map(e=>(e.textContent||'').trim()).filter(x=>term.test(x));
      let cur=el.tagName==='SELECT'?el.options[el.selectedIndex]?.textContent.trim():(el.value||el.innerText||'').trim();
      return {terms:opts,current:cur,id:el.id||'',tag:el.tagName};
    }""")


def discover_semesters(page):
    if not ensure_course_menu(page):return []
    info=semester_menu_info(page)
    terms=info.get('terms',[])
    if not terms:
        # ADF's popup might have to be expanded to reveal the semester choices.
        try:
            sel=page.locator('[id="'+info['id']+'"]') if info.get('id') else page.locator('select,[role="combobox"],.af_selectOneChoice')
            sel.first.click(timeout=4000)
            page.wait_for_timeout(350)
            texts=page.evaluate(r"""() => {
              const term=/^(Automne|Hiver|Été|Ete|Printemps)\s+20\d{2}$/i;
              return [...document.querySelectorAll('option,[role="option"],li')]
                .filter(e=>{let r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.display!=='none'})
                .map(e=>(e.innerText||e.textContent||'').trim()).filter(x=>term.test(x));
            }""")
            terms=texts
        except Exception as exc:log('[SESSIONS] ouverture du sélecteur: '+str(exc)[:130])
    if not terms:
        # Current session only is NOT a successful full-degree run.
        log('[ÉCHEC] Impossible de lire toutes les sessions dans le sélecteur.')
        try:
            page.screenshot(path=str(ARCHIVE/'diagnostic_sessions.png'))
            (ARCHIVE/'diagnostic_sessions.html').write_text(page.content(),encoding='utf-8')
        except Exception:pass
        return []
    terms=list(dict.fromkeys(terms))
    log('[SESSIONS] '+', '.join(terms))
    return terms


def select_semester(page,term):
    """Switch semester using visible menu picker and re-read course list."""
    if not ensure_course_menu(page):return False
    info=semester_menu_info(page)
    current=re.sub(r'\s+',' ',info.get('current','')).strip()
    if current==term:return True
    before=[x['name'] for x in get_courses_from_menu(page)]
    try:
        if info.get('tag')=='SELECT' and info.get('id'):
            page.locator('[id="'+info['id']+'"]').select_option(label=term,timeout=8500)
        else:
            if info.get('id'):
                picker=page.locator('[id="'+info['id']+'"]')
            else:
                picker=page.locator('[role="combobox"],.af_selectOneChoice').first
            picker.click(timeout=6000)
            option=page.get_by_role('option',name=term,exact=True)
            if option.count() and option.first.is_visible():option.first.click(timeout=6000)
            else:
                visible=page.get_by_text(term,exact=True)
                found=False
                for i in range(min(visible.count(),30)):
                    if visible.nth(i).is_visible() and not visible.nth(i).evaluate('el => !!el.closest("header")'):
                        visible.nth(i).click(timeout=6000);found=True;break
                if not found:raise RuntimeError('option non visible : '+term)
        page.wait_for_timeout(900)
        if not ensure_course_menu(page):raise RuntimeError('menu disparu')
        refreshed=semester_menu_info(page)
        now=re.sub(r'\s+',' ',refreshed.get('current','')).strip()
        if now!=term:
            raise RuntimeError('sélection non confirmée : '+repr(now))
        log('[SESSION ACTIVÉE] '+term+' ('+str(len(get_courses_from_menu(page)))+' liens)')
        return True
    except Exception as exc:
        log('[ÉCHEC SESSION] '+term+' : '+str(exc)[:180])
        try:
            page.screenshot(path=str(ARCHIVE/('diagnostic_session_'+clean(term)+'.png')))
        except Exception:pass
        return False

def save_inventory(inventory):
    path=ARCHIVE/'inventaire_cours.json'
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(inventory,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(path)


def try_introduction_url(page):
    """Return to a real course page without using the generic portal root."""
    if page.locator('#lienListeCoursId').count():
        return True
    if return_to_introduction(page) and page.locator('#lienListeCoursId').count():
        return True
    return False


def discover_all_courses(page, semesters):
    """Build and persist an inventory BEFORE archiving. ADF links often have href='#',
    so visit the actual course link to capture the resulting URL. Never trust '#'.
    """
    inventory={'schema':1,'created':datetime.now().isoformat(), 'semesters':[], 'errors':[]}
    for index,term in enumerate(semesters,1):
        log(f'\n[INVENTAIRE SESSION {index}/{len(semesters)}] {term}')
        record={'session':term,'courses':[],'complete':False}
        inventory['semesters'].append(record)
        save_inventory(inventory)
        if not select_semester(page,term):
            msg='sélecteur de session inaccessible'
            log('[INVENTAIRE ÉCHEC] '+term+' : '+msg)
            inventory['errors'].append({'session':term,'error':msg})
            save_inventory(inventory)
            continue
        entries=get_courses_from_menu(page)
        # Only the actual links inside the menu, never the other course sections.
        entries=list({x['name']:x for x in entries if x.get('name')}.values())
        log(f'[INVENTAIRE] {len(entries)} cours trouvés')
        for item in entries:
            name=item['name']
            row={'name':name,'url':None,'status':'pending'}
            record['courses'].append(row)
            save_inventory(inventory)
            # Direct href is valid only if it is a real course URL, not ADF '#'.
            href=item.get('href') or ''
            if 'sitescours.monportail.ulaval.ca/ena/site/' in href and 'idSite=' in href:
                row.update(url=href,status='ready')
                log('[INVENTAIRE URL] '+name+' : '+href)
                save_inventory(inventory)
                continue
            if not select_semester(page,term):
                row['status']='failed';row['reason']='menu de session non disponible'
                save_inventory(inventory);continue
            if not click_menu_course(page,name):
                row['status']='failed';row['reason']='impossible d’ouvrir le cours'
                save_inventory(inventory);continue
            # Wait for course-specific page information; a popup closing alone
            # is not proof that ADF finished navigating to the selected course.
            for _ in range(30):
                page.wait_for_timeout(350)
                observed=course_info(page)
                header=page.evaluate("() => (document.querySelector('#lienListeCoursId')?.closest('header')?.innerText||'')")
                if (observed[0].casefold()==term.casefold() and
                    name.casefold() in observed[2].casefold() and
                    page.url.startswith('https://sitescours.monportail.ulaval.ca/ena/site/')):
                    break
            url=page.url
            displayed=course_info(page)
            if ('sitescours.monportail.ulaval.ca/ena/site/' in url and
                'idSite=' in url and displayed[0].casefold()==term.casefold() and
                name.casefold() in displayed[2].casefold()):
                return_to_introduction(page)
                url=page.url
                row.update(url=url,status='ready',code=displayed[1])
                log('[INVENTAIRE OK] '+term+' / '+name+' -> '+url)
            else:
                row.update(status='failed',reason='URL non vérifiée : '+url)
                log('[INVENTAIRE À VÉRIFIER] '+term+' / '+name+' : '+url)
            save_inventory(inventory)
        record['complete']=len(record['courses'])==len(entries) and all(x['status']=='ready' for x in record['courses'])
        save_inventory(inventory)
    log('\n[INVENTAIRE] Enregistré dans '+str(ARCHIVE/'inventaire_cours.json'))
    return inventory


def direct_open_course(page,row,semester):
    url=row.get('url') or ''
    if not url.startswith('https://sitescours.monportail.ulaval.ca/ena/site/'):
        return False
    try:
        page.goto(url,wait_until='domcontentloaded',timeout=20000)
        page.wait_for_timeout(650)
        info=course_info(page)
        if info[0].casefold()!=semester.casefold():
            log('[SÉCURITÉ] mauvaise session après navigation directe : '+str(info))
            return False
        if row.get('code') and row['code']!=info[1]:
            log('[SÉCURITÉ] mauvais cours après navigation directe : '+str(info))
            return False
        return_to_introduction(page)
        log('[OUVERTURE DIRECTE] '+row['name'])
        return True
    except Exception as exc:
        log('[ÉCHEC OUVERTURE DIRECTE] '+row['name']+' : '+str(exc)[:190])
        return False


def find_open_course_page(context, preferred=None):
    """Select the actual course tab, not the initial monPortail homepage tab."""
    pages = [p for p in context.pages if not p.is_closed()]
    if preferred in pages:
        pages.remove(preferred)
        pages.append(preferred)
    for candidate in reversed(pages):
        try:
            candidate.wait_for_load_state('domcontentloaded', timeout=4000)
        except Exception:
            pass
        try:
            candidate.locator('#lienListeCoursId').wait_for(state='visible', timeout=3500)
            return candidate
        except Exception:
            pass
    return None


def main():
    global ARCHIVE,STATE,NAV_LANDING
    ARCHIVE=ask_destination()
    ARCHIVE.mkdir(parents=True,exist_ok=True)
    STATE=ARCHIVE/'journal.json'
    state=load_state()
    log('=== MONPORTAIL V15c — SAUVEGARDE DES COURS ===')
    log('Archive : '+str(ARCHIVE))
    log('Profil Chromium : '+str(PROFILE))
    with sync_playwright() as p:
        context=p.chromium.launch_persistent_context(str(PROFILE),headless=False,accept_downloads=True,viewport={'width':1440,'height':900})
        page=context.pages[0] if context.pages else context.new_page()
        if page.url in ('','about:blank'):page.goto(START_URL,wait_until='domcontentloaded')
        print('\nÉTAPE 2/3 — CONNEXION À MONPORTAIL')
        print('1. Dans la fenêtre Chromium qui vient de s’ouvrir, connectez-vous à monPortail avec votre compte ULaval.')
        print('2. Allez dans Études > Cours, puis ouvrez N’IMPORTE QUEL cours accessible.')
        print('3. Assurez-vous de voir la page Introduction du cours et le bouton « Liste des cours » en haut à droite.')
        print('4. Revenez à cette fenêtre noire, puis appuyez sur Entrée.')
        print('IMPORTANT : utilisez le navigateur ouvert par ce programme, pas votre navigateur habituel.')
        input('\nAppuyez sur Entrée lorsque le cours est affiché... ')
        course_page = find_open_course_page(context, page)
        if course_page is None:
            log('[DIAGNOSTIC] Onglets ouverts :')
            for candidate in context.pages:
                try: log('  - '+candidate.url)
                except Exception: pass
            log('[ARRÊT] Aucun onglet avec « Liste des cours » détecté. Ouvre un cours dans le navigateur Chromium lancé par le script (pas dans ton Chrome habituel).')
            input('Entrée pour fermer... ');context.close();return
        page = course_page
        try: page.bring_to_front()
        except Exception: pass
        log('[NAVIGATION] Onglet du cours sélectionné : '+page.url)
        NAV_LANDING=page.url
        semesters=discover_semesters(page)
        if not semesters:
            log('[ARRÊT] Sessions introuvables.');input('Entrée pour fermer... ');context.close();return
        skip_count=ask_skip_semesters(len(semesters))
        log(f'[REPRISE] {skip_count} session(s) à ignorer pour le téléchargement.')
        log('[PHASE 1/2] Recherche de toutes les sessions et de tous les cours (pas encore de téléchargement).')
        inventory=discover_all_courses(page,semesters)
        ready=sum(sum(r['status']=='ready' for r in t['courses']) for t in inventory['semesters'])
        failures=[(t['session'],r['name']) for t in inventory['semesters'] for r in t['courses'] if r['status']!='ready']
        log(f'[INVENTAIRE] {ready} URL(s) vérifiée(s), {len(failures)} échec(s).')
        for term,name in failures:log('[INVENTAIRE À VÉRIFIER] '+term+' / '+name)
        log('[PHASE 2/2] Téléchargement des documents de cours sur votre disque.')
        done=0;unavailable=0
        for si,term in enumerate(inventory['semesters']):
            semester=term['session']
            if si<skip_count:
                log('[SESSION IGNORÉE] '+semester);continue
            log(f'\n========== SESSION {si+1}/{len(semesters)} : {semester} ==========')
            for n,row in enumerate(term['courses'],1):
                log(f'[COURS {n}/{len(term["courses"])}] '+row['name'])
                if row['status']!='ready' or not direct_open_course(page,row,semester):
                    unavailable+=1
                    log('[À VÉRIFIER] cours non archivé : '+semester+' / '+row['name'])
                    continue
                try:
                    archive_course(page,state)
                    done+=1
                except Exception as exc:
                    unavailable+=1
                    log('[ERREUR COURS] '+row['name']+' : '+str(exc)[:200])
        log(f'\n[FIN] {done} cours traités; {unavailable} cours à vérifier.')
        log('[IMPORTANT] Vérifier journal_execution.txt ET inventaire_cours.json : un cours visité ne garantit pas tous les fichiers.')
        input('Appuie sur Entrée pour fermer le navigateur... ')
        context.close()


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt: print('\nInterruption utilisateur : téléchargements déjà terminés conservés.')
    except Exception as exc:
        print('\nERREUR :',repr(exc));traceback.print_exc()
        input('Appuie sur Entrée pour fermer... ')
