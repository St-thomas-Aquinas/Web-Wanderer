#!/usr/bin/env python3
"""
AUTONOMOUS WEB WANDERER - PRODUCTION CRAWLER
Runs forever on Render, syncs to GitHub Gist.
"""

import os, json, time, random, logging, signal, sys, hashlib, threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

# ==================== CONFIG ====================
CONFIG = {
    'data_dir': 'wanderer_data',
    'sync_interval': 600,          # Sync to Gist every 10 minutes
    'min_delay': 3,                # Min seconds between requests
    'max_delay': 8,                # Max seconds between requests
    'save_interval': 5,            # Local save every N pages
    'request_timeout': 15,
    'blocked_extensions': {'.pdf','.jpg','.jpeg','.png','.gif','.svg','.mp4','.mp3','.zip','.exe','.css','.js'},
    'blocked_domains': {'facebook.com','twitter.com','instagram.com','youtube.com','tiktok.com','linkedin.com','reddit.com'},
    'blocked_paths': {'/login','/admin','/wp-admin','/api/','/cdn/','/static/'},
    'user_agent': 'WebWanderer/1.0 (Autonomous Explorer)',
    'seed_urls': ['https://en.wikipedia.org/wiki/Main_Page','https://www.gutenberg.org/','https://archive.org/','https://example.com']
}

# ==================== LOGGING ====================
DATA_DIR = Path(CONFIG['data_dir'])
DATA_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.FileHandler(DATA_DIR/'crawler.log'), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ==================== STORAGE ====================
class Storage:
    def __init__(self):
        self.urls_file = DATA_DIR/'urls.json'
        self.stats_file = DATA_DIR/'stats.json'
        self.lock = threading.Lock()
        if not self.urls_file.exists(): self._write(self.urls_file, {'crawled':{},'queue':[]})
        if not self.stats_file.exists(): self._write(self.stats_file, {'started':datetime.now().isoformat(),'crawled':0,'errors':0})
    
    def _read(self,p): 
        with open(p) as f: return json.load(f)
    
    def _write(self,p,d):
        t = p.with_suffix('.tmp')
        with open(t,'w') as f: json.dump(d,f,indent=2,default=str); f.flush(); os.fsync(f.fileno())
        t.replace(p)
    
    def add(self,url,depth=0):
        with self.lock:
            d=self._read(self.urls_file)
            if url not in d['crawled'] and url not in [q['url'] for q in d['queue']]:
                d['queue'].append({'url':url,'domain':urlparse(url).netloc,'depth':depth,'added':datetime.now().isoformat()})
                self._write(self.urls_file,d)
    
    def get_next(self):
        with self.lock:
            d=self._read(self.urls_file)
            if d['queue']: 
                item = d['queue'].pop(0)
                self._write(self.urls_file,d)
                return item
        return None
    
    def mark_done(self,item,title,words,ext,hashv):
        with self.lock:
            d=self._read(self.urls_file)
            d['crawled'][item['url']]={**item,'status':'done','title':title[:200],'words':words,'ext':ext,'hash':hashv,'at':datetime.now().isoformat()}
            self._write(self.urls_file,d)
    
    def mark_err(self,item):
        with self.lock:
            d=self._read(self.urls_file)
            d['crawled'][item['url']]={**item,'status':'error','at':datetime.now().isoformat()}
            self._write(self.urls_file,d)
    
    def stats(self,c=0,e=0):
        with self.lock:
            s=self._read(self.stats_file)
            s['crawled']+=c; s['errors']+=e; s['up']=datetime.now().isoformat()
            self._write(self.stats_file,s)
    
    def payload(self): 
        return {'urls.json':self._read(self.urls_file),'stats.json':self._read(self.stats_file)}

# ==================== GIST SYNC ====================
class Gist:
    def __init__(self,token):
        self.token=token.strip()
        # Auto-detect token type
        hdr = f'Bearer {self.token}' if self.token.startswith('github_pat_') else f'token {self.token}'
        self.h={'Authorization':hdr,'Content-Type':'application/json','Accept':'application/vnd.github.v3+json'}
        self.id=None
        self._setup()
    
    def _setup(self):
        desc='web-wanderer-autonomous-crawler'
        # Search for existing gist
        for p in range(1,4):
            r=requests.get(f'https://api.github.com/gists?per_page=100&page={p}',headers=self.h,timeout=10)
            if r.status_code!=200:break
            for g in r.json():
                if g.get('description')==desc: 
                    self.id=g['id']
                    logger.info(f"✅ Found Gist: {g['html_url']}")
                    return
        # Create new gist
        p={'description':desc,'public':False,'files':{
            'urls.json':{'content':'{"crawled":{},"queue":[]}'},
            'stats.json':{'content':f'{{"started":"{datetime.now().isoformat()}"}}'}
        }}
        r=requests.post('https://api.github.com/gists',headers=self.h,json=p,timeout=10)
        if r.status_code in(200,201): 
            self.id=r.json()['id']
            logger.info(f"✅ Created Gist: {r.json()['html_url']}")
        else: 
            logger.error(f"❌ Gist create failed: {r.status_code} {r.text}")
    
    def sync(self,data):
        if not self.id: return False
        pl={'files':{f:{'content':json.dumps(c,indent=2,default=str)} for f,c in data.items()}}
        r=requests.patch(f'https://api.github.com/gists/{self.id}',headers=self.h,json=pl,timeout=10)
        return r.status_code==200

# ==================== CRAWLER ====================
class Wanderer:
    def __init__(self):
        tok=os.getenv('GITHUB_TOKEN')
        if not tok: raise RuntimeError('❌ GITHUB_TOKEN not set in environment')
        logger.info(f"🔑 Token: {tok[:10]}...{tok[-4:]}")
        self.st=Storage()
        self.gist=Gist(tok)
        self.run_forever=True
        self.batch=0
        self.last_sync=time.time()
        signal.signal(signal.SIGINT,self._stop)
        signal.signal(signal.SIGTERM,self._stop)
        logger.info("🤖 Web Wanderer started | Sync every 600s")
    
    def _stop(self,s,f): 
        logger.info("🛑 Shutdown signal received")
        self._sync_now()
        sys.exit(0)
    
    def _sync_now(self):
        try:
            if self.gist.sync(self.st.payload()): 
                self.st.stats()
                logger.info("📤 Synced to Gist")
        except Exception as e: logger.error(f"❌ Sync error: {e}")
    
    def valid(self,u):
        try:
            p=urlparse(u)
            if p.scheme not in['http','https']:return False
            if any(p.path.lower().endswith(x) for x in CONFIG['blocked_extensions']):return False
            if any(d in p.netloc.lower() for d in CONFIG['blocked_domains']):return False
            if any(x in p.path for x in CONFIG['blocked_paths']):return False
            return True
        except: return False
    
    def fetch(self,u):
        try:
            r=requests.get(u,headers={'User-Agent':CONFIG['user_agent']},timeout=CONFIG['request_timeout'],allow_redirects=True)
            if'text/html'not in r.headers.get('Content-Type',''):return None,'not html'
            return r.text,None
        except Exception as e: return None,str(e)
    
    def links(self,html,base):
        s=set()
        try:
            for a in BeautifulSoup(html,'html.parser').find_all('a',href=True):
                h=urljoin(base,a['href']).split('#')[0]
                if self.valid(h):s.add(h)
        except:pass
        return s
    
    def crawl(self,item):
        u=item['url']
        logger.info(f"🕸️ Crawling: {u}")
        html,err=self.fetch(u)
        if err: 
            logger.warning(f"⚠️ Failed: {err}")
            self.st.mark_err(item)
            return[]
        soup=BeautifulSoup(html,'html.parser')
        title=(soup.title.string or'No title').strip() if soup.title else'No title'
        words=len(soup.get_text().split())
        lnks=self.links(html,u)
        ext=sum(1 for l in lnks if urlparse(l).netloc!=urlparse(u).netloc)
        hsh=hashlib.md5(html.encode()).hexdigest()
        self.st.mark_done(item,title,words,ext,hsh)
        logger.info(f"✅ {title[:40]}... | {words} words | {len(lnks)} links")
        self.batch+=1
        if self.batch>=CONFIG['save_interval']: 
            self.st.stats(c=self.batch)
            self.batch=0
        return lnks
    
    def _check_sync(self):
        if time.time()-self.last_sync>=CONFIG['sync_interval']: 
            self._sync_now()
            self.last_sync=time.time()
    
    def run(self):
        logger.info("="*50)
        logger.info("🚀 WEB WANDERER RUNNING FOREVER")
        logger.info("="*50)
        # Seed initial URLs
        for u in CONFIG['seed_urls']: self.st.add(u)
        # Main loop - runs forever
        while self.run_forever:
            try:
                self._check_sync()
                item=self.st.get_next()
                if not item: 
                    logger.info("⏳ Queue empty, waiting 60s...")
                    time.sleep(60)
                    continue
                for l in self.crawl(item): 
                    self.st.add(l,item['depth']+1)
                time.sleep(random.uniform(CONFIG['min_delay'],CONFIG['max_delay']))
            except Exception as e: 
                logger.error(f"❌ Error: {e}")
                time.sleep(10)

# ==================== ENTRY POINT ====================
if __name__=='__main__':
    try: 
        Wanderer().run()  # ← This runs FOREVER
    except Exception as e: 
        logger.critical(f"💥 Fatal: {e}")
        sys.exit(1)
