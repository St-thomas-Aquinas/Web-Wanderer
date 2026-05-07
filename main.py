#!/usr/bin/env python3
"""
AUTONOMOUS WEB WANDERER - GITHUB GIST EDITION
Runs 24/7, syncs crawled data to a private GitHub Gist automatically.
"""

import os
import json
import time
import random
import logging
import signal
import sys
import hashlib
import threading
import fcntl
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

# ==================== CONFIGURATION ====================
CONFIG = {
    'data_dir': 'wanderer_data',
    'sync_interval': 600,          # Sync to Gist every 10 minutes (seconds)
    'min_delay': 3,                # Min seconds between requests
    'max_delay': 8,                # Max seconds between requests
    'save_interval': 5,            # Local save every N pages
    'request_timeout': 15,
    'blocked_extensions': {'.pdf', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.mp4', '.mp3', '.zip', '.exe', '.css', '.js'},
    'blocked_domains': {'facebook.com', 'twitter.com', 'instagram.com', 'youtube.com', 'tiktok.com', 'linkedin.com', 'reddit.com'},
    'blocked_paths': {'/login', '/admin', '/wp-admin', '/api/', '/cdn/', '/static/'},
    'user_agent': 'WebWanderer/1.0 (Autonomous Explorer)',
    'seed_urls': [
        'https://en.wikipedia.org/wiki/Main_Page',
        'https://www.gutenberg.org/',
        'https://archive.org/',
        'https://example.com'
    ]
}

# ==================== LOGGING ====================
DATA_DIR = Path(CONFIG['data_dir'])
DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(DATA_DIR / 'crawler.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ==================== LOCAL JSON STORAGE ====================
class JSONStorage:
    def __init__(self):
        self.urls_file = DATA_DIR / 'urls.json'
        self.stats_file = DATA_DIR / 'stats.json'
        self.lock = threading.Lock()
        self._init_files()

    def _init_files(self):
        if not self.urls_file.exists():
            self._write(self.urls_file, {'crawled': {}, 'queue': []})
        if not self.stats_file.exists():
            self._write(self.stats_file, {
                'started_at': datetime.now().isoformat(),
                'pages_crawled': 0,
                'errors': 0,
                'last_sync': None
            })
        logger.info("✅ Local JSON storage initialized")

    def _read(self, filepath):
        with open(filepath, 'r') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _write(self, filepath, data):
        tmp = filepath.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        tmp.replace(filepath)

    def add_url(self, url, depth=0):
        with self.lock:
            data = self._read(self.urls_file)
            if url not in data['crawled'] and url not in data['queue']:
                data['queue'].append({
                    'url': url,
                    'domain': urlparse(url).netloc,
                    'depth': depth,
                    'added_at': datetime.now().isoformat()
                })
                self._write(self.urls_file, data)

    def get_next_url(self):
        with self.lock:
            data = self._read(self.urls_file)
            if data['queue']:
                item = data['queue'].pop(0)
                self._write(self.urls_file, data)
                return item
            return None

    def mark_crawled(self, url_item, title, word_count, ext_links, content_hash):
        with self.lock:
            data = self._read(self.urls_file)
            data['crawled'][url_item['url']] = {
                **url_item,
                'status': 'crawled',
                'crawled_at': datetime.now().isoformat(),
                'title': title[:200],
                'word_count': word_count,
                'external_links': ext_links,
                'content_hash': content_hash
            }
            self._write(self.urls_file, data)

    def mark_error(self, url_item):
        with self.lock:
            data = self._read(self.urls_file)
            data['crawled'][url_item['url']] = {
                **url_item,
                'status': 'error',
                'crawled_at': datetime.now().isoformat()
            }
            self._write(self.urls_file, data)

    def update_stats(self, crawled=0, errors=0):
        with self.lock:
            stats = self._read(self.stats_file)
            stats['pages_crawled'] += crawled
            stats['errors'] += errors
            stats['last_updated'] = datetime.now().isoformat()
            self._write(self.stats_file, stats)

    def mark_synced(self):
        with self.lock:
            stats = self._read(self.stats_file)
            stats['last_sync'] = datetime.now().isoformat()
            self._write(self.stats_file, stats)

    def get_sync_payload(self):
        """Return data ready for Gist upload"""
        return {
            'urls.json': self._read(self.urls_file),
            'stats.json': self._read(self.stats_file)
        }

# ==================== GITHUB GIST SYNC ====================
class GitHubGistSync:
    def __init__(self, token):
        self.token = token
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/vnd.github.v3+json'
        }
        self.gist_id = None
        self._setup_gist()

    def _setup_gist(self):
        """Find existing gist or create new one"""
        desc = "web-wanderer-autonomous-crawler"
        
        # Search existing gists
        page = 1
        while True:
            resp = requests.get(f"https://api.github.com/gists?per_page=100&page={page}", headers=self.headers)
            gists = resp.json()
            if not gists or resp.status_code != 200:
                break
            for g in gists:
                if g.get('description') == desc:
                    self.gist_id = g['id']
                    logger.info(f"✅ Found existing Gist: {g['html_url']}")
                    return
            page += 1

        # Create new gist
        payload = {
            "description": desc,
            "public": False,
            "files": {
                "urls.json": {"content": '{"crawled": {}, "queue": []}'},
                "stats.json": {"content": '{"started_at": "' + datetime.now().isoformat() + '"}'}
            }
        }
        resp = requests.post("https://api.github.com/gists", headers=self.headers, json=payload)
        if resp.status_code in (200, 201):
            self.gist_id = resp.json()['id']
            logger.info(f"✅ Created new Gist: {resp.json()['html_url']}")
        else:
            logger.error(f"❌ Failed to create Gist: {resp.status_code} {resp.text}")

    def sync(self, files_data):
        """Upload files to Gist"""
        if not self.gist_id:
            logger.warning("⚠️ Gist ID not set. Skipping sync.")
            return False

        payload = {"files": {}}
        for fname, content in files_data.items():
            payload["files"][fname] = {"content": json.dumps(content, indent=2, default=str)}

        try:
            resp = requests.patch(
                f"https://api.github.com/gists/{self.gist_id}",
                headers=self.headers,
                json=payload,
                timeout=10
            )
            if resp.status_code == 200:
                logger.info(f"📤 Synced to Gist successfully")
                return True
            else:
                logger.error(f"❌ Gist sync failed: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"❌ Gist network error: {e}")
            return False

# ==================== AUTONOMOUS CRAWLER ====================
class AutonomousWebWanderer:
    def __init__(self):
        token = os.getenv("GITHUB_TOKEN")
        if not token:
            raise EnvironmentError(" GITHUB_TOKEN environment variable not set!")
        
        self.storage = JSONStorage()
        self.gist = GitHubGistSync(token)
        self.running = True
        self.batch_counter = 0
        self.last_sync_time = time.time()
        
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
        
        logger.info("🤖 Autonomous Web Wanderer (Gist Edition) initialized")
        logger.info(f"📂 Auto-sync to GitHub Gist every {CONFIG['sync_interval']}s")

    def shutdown(self, signum, frame):
        logger.info("\n🛑 Shutdown signal received. Final sync...")
        self._force_sync()
        sys.exit(0)

    def _force_sync(self):
        try:
            payload = self.storage.get_sync_payload()
            self.gist.sync(payload)
            self.storage.mark_synced()
            logger.info("✅ Final sync complete. Goodbye!")
        except Exception as e:
            logger.error(f"❌ Final sync failed: {e}")

    def is_valid_url(self, url):
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ['http', 'https']: return False
            path = parsed.path.lower()
            if any(path.endswith(ext) for ext in CONFIG['blocked_extensions']): return False
            domain = parsed.netloc.lower()
            if any(b in domain for b in CONFIG['blocked_domains']): return False
            if any(b in parsed.path for b in CONFIG['blocked_paths']): return False
            return True
        except: return False

    def fetch_page(self, url):
        try:
            resp = requests.get(url, headers={'User-Agent': CONFIG['user_agent']}, 
                                timeout=CONFIG['request_timeout'], allow_redirects=True)
            if 'text/html' not in resp.headers.get('Content-Type', ''): 
                return None, "Not HTML"
            return resp.text, None
        except Exception as e: 
            return None, str(e)

    def extract_links(self, html, base_url):
        links = set()
        try:
            soup = BeautifulSoup(html, 'html.parser')
            for a in soup.find_all('a', href=True):
                href = urljoin(base_url, a['href']).split('#')[0]
                if self.is_valid_url(href): links.add(href)
        except: pass
        return links

    def crawl_page(self, url_item):
        url = url_item['url']
        logger.info(f"🕸️  Crawling: {url}")
        
        html, error = self.fetch_page(url)
        if error:
            logger.warning(f"  ⚠️  Failed: {error}")
            self.storage.mark_error(url_item)
            return []
        
        soup = BeautifulSoup(html, 'html.parser')
        title = (soup.title.string or "No title").strip() if soup.title else "No title"
        word_count = len(soup.get_text().split())
        links = self.extract_links(html, url)
        ext_links = sum(1 for l in links if urlparse(l).netloc != urlparse(url).netloc)
        content_hash = hashlib.md5(html.encode()).hexdigest()
        
        self.storage.mark_crawled(url_item, title, word_count, ext_links, content_hash)
        logger.info(f"  ✅ Success | Title: {title[:50]}... | Words: {word_count} | Links: {len(links)}")
        
        self.batch_counter += 1
        if self.batch_counter >= CONFIG['save_interval']:
            self.storage.update_stats(crawled=self.batch_counter)
            stats = self.storage.get_sync_payload()['stats.json']
            logger.info(f"📊 STATS | Crawled: {stats['pages_crawled']} | Errors: {stats['errors']}")
            self.batch_counter = 0
        
        return links

    def check_sync(self):
        if time.time() - self.last_sync_time >= CONFIG['sync_interval']:
            logger.info("⏰ Triggering Gist sync...")
            payload = self.storage.get_sync_payload()
            if self.gist.sync(payload):
                self.storage.mark_synced()
            self.last_sync_time = time.time()

    def run(self):
        logger.info("="*60)
        logger.info("🚀 STARTING AUTONOMOUS WEB WANDERER")
        logger.info("="*60)
        
        # Seed initial URLs
        for url in CONFIG['seed_urls']:
            self.storage.add_url(url, depth=0)
        
        while self.running:
            try:
                self.check_sync()
                
                url_item = self.storage.get_next_url()
                if not url_item:
                    logger.info("⏳ Queue empty. Waiting 60s...")
                    time.sleep(60)
                    continue
                
                new_links = self.crawl_page(url_item)
                for link in new_links:
                    self.storage.add_url(link, depth=url_item['depth'] + 1)
                
                time.sleep(random.uniform(CONFIG['min_delay'], CONFIG['max_delay']))
                
            except Exception as e:
                logger.error(f" Loop error: {e}")
                time.sleep(10)

if __name__ == "__main__":
    try:
        wanderer = AutonomousWebWanderer()
        wanderer.run()
    except Exception as e:
        logger.critical(f"💥 Fatal error: {e}")
        sys.exit(1)
