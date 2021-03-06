#!/usr/bin/env python
# -*- coding: utf-8 -*-
#import resource
#resource.setrlimit(resource.RLIMIT_CPU, (60*2, -1))
import os
import sys
import io
import gzip
import MySQLdb
import config
import urllib2
import requests
import hashlib
import time
import chardet
import socket
import progressbar
import threading
import collections
from threading import Thread
import Queue as queue

import scraper
from btdht import utils
from replication import Replicator

from crawler import get_id, HashToIgnore
hash_to_ignore = HashToIgnore()

def on_torrent_announce(hash, url):
    global hash_to_ignore
    return
    #print("doing %s" % hash)
    if not url.startswith("http://torcache.net/"):
        print("not on torcache")
        return
    if len(hash) != 40:
        print("hash len invalid")
        return
    try:
        hash.decode("hex")
    except TypeError:
        print("hash is not hex")
        return
    hash = hash.lower()
    if hash.decode("hex") in hash_to_ignore:
        return
    def load_url(url, hash):
        try:
            response = urllib2.urlopen(url)
            torrentz = response.read()
            if torrentz:
                try:
                    bi = io.BytesIO(torrentz)
                    torrent = gzip.GzipFile(fileobj=bi, mode="rb").read()
                    if torrent:
                        open("%s/%s.torrent" % (config.torrents_new, hash), 'wb+').write(torrent)
                        hash_to_ignore.add(hash.decode("hex"))
                    else:
                        print("Got empty response from torcache %s" % url)
                except (gzip.zlib.error, OSError) as e:
                    print("%r" % e)
            else:
                print("Got empty response from torcache %s" % url)
        except (urllib2.HTTPError, socket.error, urllib2.URLError):
            pass
        except (EOFError,) as e:
            print("Error on %s: %r" % (hash, e))
    load_url(url, hash)
    
#replicator = Replicator(config.public_ip, pub_port=config.replication_tcp_port, priv_port=config.replication_udp_port,
#                        dht_port=config.replication_dht_port, on_torrent_announce=on_torrent_announce, dht_id=get_id("feed.id"),
#                        bootstrap_port=config.bootstrap_port)

def announce(hash, url=None):
    if url is None:
        url = "http://torcache.net/torrent/%s.torrent" % hash.upper()
    hash_to_ignore.add(hash.decode("hex"))
socket.setdefaulttimeout(3)

def widget(what=""):
    padding = 30
    return [progressbar.ETA(), ' ', progressbar.Bar('='), ' ', progressbar.SimpleProgress(), ' ' if what else "", what]

def cancelable(f):
    def func(*arg, **kwargs):
        try:
            return f(*arg, **kwargs)
        except KeyboardInterrupt:
            print("\r")
    return func

def last_timestamp(self):
    global last_get
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(created))    

def scrape(db=None):
    if db is None:
        db = MySQLdb.connect(**config.mysql)
    if config.scrape_interval <= 0:
        return
    cur = db.cursor()
    query = "SELECT COUNT(id) FROM torrents WHERE created_at IS NOT NULL AND (scrape_date IS NULL OR scrape_date <= DATE_SUB(NOW(), INTERVAL %d MINUTE))" % config.scrape_interval
    if config.scape_limit > 0:
        query += " LIMIT %d" % config.scape_limit
    cur.execute(query)
    ret = [r[0] for r in cur]
    if config.scape_limit > 0:
        count=min(ret[0], config.scape_limit)
    else:
        count=ret[0]
    qhashs = queue.Queue()
    i=0
    n_count = 0

    if count <= 0:
        return
    query = "SELECT hash FROM torrents WHERE created_at IS NOT NULL AND (scrape_date IS NULL OR scrape_date <= DATE_SUB(NOW(), INTERVAL %d MINUTE))" % config.scrape_interval
    if config.scape_limit > 0:
        query += " ORDER BY scrape_date ASC LIMIT %d" % config.scape_limit
    db2 = MySQLdb.connect(**config.mysql)
    cur2 = db2.cursor()
    cur2.execute(query)
    l = []
    i=0
    for r in cur2:
        l.append(r[0])
        i+=1
        if i >= 50:
            qhashs.put(l)
            l = []
            i=0
    qhashs.put(l)

    pbar = progressbar.ProgressBar(widgets=widget("torrents scraped"), maxval=count).start()    
    def scrape_thread(cur2, pbar, count, qhashs, nth, total, ips=["open.demonii.com"]):
        db = MySQLdb.connect(**config.mysql)
        cur = db.cursor()
        last_commit = time.time()
        errno=0
        nip = len(ips)
        banip=collections.defaultdict(int)
        i = nth
        try:
            l = qhashs.get(timeout=0)
            while True:
                try:
                    ip = ips[i%len(ips)]
                    i+=1
                    for hash, info in scraper.scrape("udp://%s:1337/announce" % ip, l).items():
                        cur.execute("UPDATE torrents SET scrape_date=NOW(), seeders=%s, leechers=%s, downloads_count=%s WHERE hash=%s", (info['seeds'], info['peers'], info['complete'], hash))
                    if time.time() - last_commit > 30:
                        db.commit()
                        last_commit = time.time()
                    pbar.update(min(pbar.currval + len(l), count))
                    l = qhashs.get(timeout=0)
                    errno=0
                except (socket.timeout, socket.gaierror, socket.error):
                    db.commit()
                    banip[ip]+=1
                    if banip[ip]>3:
                        try:ips.remove(ip)
                        except ValueError:
                            pass
                    if not ips:
                        raise ValueError("all ips failed")
                    time.sleep(0.1 * errno + 0.1)
                    errno+=1
                    if errno > nip*3:
                        raise
        except (queue.Empty, ZeroDivisionError):
            pass
        except (socket.timeout, socket.gaierror, socket.error):
            qhashs.put(l)
        except (ValueError, RuntimeError) as e:
            print e
        finally:
            db.commit()
            cur.close()
            db.close()

    try:
        threads = []
        total = 30
        ip = [ s[4][0] for s in socket.getaddrinfo("open.demonii.com", 0, socket.AF_INET, socket.SOCK_DGRAM)]
        mod = len(ip)
        for i in range(0, total):
            t = Thread(target=scrape_thread, args=(cur2, pbar, count, qhashs, i,total, ip))
            t.daemon = True
            t.setName("scrape-%02d" % i)
            t.start()
            threads.append(t)
        join(threads)
        pbar.finish()
    finally:
        cur2.close()
        db2.close()
        db.commit()
        cur.close()

def update_torrent_file(db=None):
    if db is None:
        db = MySQLdb.connect(**config.mysql)
    hashs = get_hash(db, insert_new=True, dir_only=True, name_null=True)
    update_db(db, hashs, quiet=True)

def insert(new_hashq, pbar):
        db = MySQLdb.connect(**config.mysql)
        cur = db.cursor()
        try:
            while True:
                hash = new_hashq.get(timeout=0)
                if is_torcache(hash):
                    cur.execute("INSERT INTO torrents (hash,dht_last_get,torcache) VALUES (%s,NOW(),%s) ON DUPLICATE KEY UPDATE hash=LOWER(VALUES(hash))",(hash, True))
                else:
                    cur.execute("INSERT INTO torrents (hash,dht_last_get) VALUES (%s,NOW()) ON DUPLICATE KEY UPDATE hash=LOWER(VALUES(hash))",(hash,))
                pbar.update(pbar.currval+1)
        except queue.Empty:
            pass
        finally:
            db.commit()
            cur.close()
            db.close()

def get_new_torrent():
    files = os.listdir(config.torrents_new)
    if not files:
        return
    progress = progressbar.ProgressBar(widgets=widget("new torrents"))
    for f in progress(files):
        f = "%s/%s" % (config.torrents_new, f)
        try:
            torrent = utils.bdecode(open(f, 'rb').read())    
            real_hash = hashlib.sha1(utils.bencode(torrent[b'info'])).hexdigest()
            hash_to_ignore.add(real_hash.decode("hex"))
            os.rename(f, "%s/%s.torrent" % (config.torrents_dir, real_hash))
        except utils.BcodeError as e:
            pass

def get_hash(db=None, insert_new=False, dir_only=False, name_null=False):
    if db is None:
        db = MySQLdb.connect(**config.mysql)          
    cur = db.cursor()
    files = os.listdir(config.torrents_dir)
    hashs = [h[:-8] for h in files if h.endswith(".torrent")]
    if insert_new:
        khashs = set()
        i=0
        while hashs[i:i+50]:
            query = "SELECT hash FROM torrents WHERE (%s)"  % " OR ".join("hash=%s" for hash in hashs[i:i+50])
            cur.execute(query, tuple(hashs[i:i+50]))
            ret = [r[0] for r in cur]
            khashs = khashs.union(ret)
            i+=50
        new_hash = set(hashs).difference(khashs)
        count = len(new_hash)
        if count > 0:
            done=0
            new_hashq = queue.Queue()
            [new_hashq.put(h) for h in new_hash]
            pbar = progressbar.ProgressBar(widgets=widget("inserting torrents in db"), maxval=count).start()

            try:
                threads = []
                for i in range(0, 20):
                    t = Thread(target=insert, args=(new_hashq, pbar))
                    t.setName("insert-%02d" % i)
                    t.daemon = True
                    t.start()
                    threads.append(t)
                join(threads)
            finally:
                pbar.finish()
    if dir_only:
        if name_null and hashs:
            query = "SELECT hash FROM torrents WHERE created_at IS NULL AND (%s)"  % " OR ".join("hash=%s" for hash in hashs)
            cur.execute(query, tuple(hashs))
            ret = [r[0] for r in cur]
            cur.close()
            return ret
        return hashs
    else:
        query = "SELECT hash FROM torrents WHERE created_at IS NULL AND (dht_last_get >= DATE_SUB(NOW(), INTERVAL 1 HOUR) OR dht_last_announce >= DATE_SUB(NOW(), INTERVAL 1 HOUR) OR %s)"  % " OR ".join("hash=%s" for hash in hashs)
    cur.execute(query, tuple(hashs))
    ret = [r[0] for r in cur]
    cur.close()
    return ret

def get_dir(db=None):
    if db is None:
        db = MySQLdb.connect(**config.mysql)
    files = os.listdir(config.torrents_dir)
    hashs = [h[:-8] for h in files if h.endswith(".torrent")]
    cur = db.cursor()
    cur.execute("SELECT hash FROM torrents WHERE created_at IS NULL AND (%s)"  % " OR ".join("hash=%s" for hash in hashs), tuple(hashs))
    ret = [r[0] for r in cur]
    new_hash = set(hashs).difference(ret)
    count = len(new_hash)
    done=0
    new_hashq = queue.Queue()
    [new_hashq.put(h) for h in new_hash]
    pbar = progressbar.ProgressBar(widgets=widget("inserting torrents in db"), maxval=count).start()
    cur.close()
    try:
        threads = []
        for i in range(0, 20):
            t = Thread(target=insert, args=(new_hashq, pbar))
            t.setName("insert-%02d" % i)
            t.daemon = True
            t.start()
            threads.append(t)
        join(threads)
    finally:
        print("")
        db.commit()

    cur.close()
    return ret + list(new_hash)

def clean(db, hashs=None):
    cur = db.cursor()
    print("Cleanning old db hash")
    query = "DELETE FROM torrents WHERE created_at IS NULL AND (dht_last_get < DATE_SUB(NOW(), INTERVAL 1 HOUR) OR dht_last_get IS NULL) AND (dht_last_announce < DATE_SUB(NOW(), INTERVAL 1 HOUR) OR dht_last_announce IS NULL)"
    cur.execute(query)
    db.commit()

def is_torcache(hash):
    return False
    resp = requests.head("http://torcache.net/torrent/%s.torrent" % hash.upper())
    return resp.status_code == 200

def fetch_torrent(db):
    cur = db.cursor()
    cur.execute("SELECT hash FROM torrents WHERE created_at IS NULL AND (torcache_notfound=%s OR torcache_notfound IS NULL)", (False,))
    hashs = queue.Queue()
    [hashs.put(r[0].lower()) for r in cur]
    notfound = set()
    cur = db.cursor()
    count = hashs.qsize()
    if count <= 0:
        return
    pbar = progressbar.ProgressBar(widgets=widget("torrents fetched"), maxval=count).start()
    counter = [0, 0, 0]
    def load_url(db, cur, hash, notfound, counter):
        if os.path.isfile("%s/%s.torrent" % (config.torrents_dir, hash)):
            update_db(db, hashs=[hash], quiet=True)
            counter[2]+=1
        else:
            cur.execute("UPDATE torrents SET torcache_notfound=%s WHERE hash=%s", (True, hash))
            notfound.add(hash)
            counter[1]+=1
        pbar.update(pbar.currval + 1)

    def process_hashs(qhashs, notfound, counter):
        db = MySQLdb.connect(**config.mysql)
        cur = db.cursor()
        try:
            while True:
                hash = qhashs.get(timeout=0)
                try:
                    load_url(db, cur, hash, notfound, counter)
                except (socket.timeout, urllib2.URLError):
                    qhashs.put(hash)
        except queue.Empty:
            pass
        except (socket.timeout,):
            print("socket timeout http://torcache.net/torrent/%s.torrent" % hash.upper())
            qhashs.put(hash)
        finally:
            db.commit()
            db.close()
    try:
        threads = []
        for i in range(0, 20):
            t = Thread(target=process_hashs, args=(hashs, notfound, counter))
            t.setName("fetch-%02d" % i)
            t.daemon = True
            t.start()
            threads.append(t)
        join(threads)
    finally:
        pbar.finish()
        db.commit()
    print("%s found, %s not found, %s already gotten" % (counter[0], counter[1], counter[2]))
    return notfound


def get_torrent(db, hash, base_path=config.torrents_dir):
    global downloading, failed
    if hash in failed:
        return None
    if os.path.isfile("%s/%s.torrent" % (base_path, hash)):
        torrent = open("%s/%s.torrent" % (base_path, hash), 'rb').read()
        try:
            torrent = utils.bdecode(torrent)
            if not b'info' in torrent:
                return {b'info':torrent}
            else:
                return torrent
        except utils.BcodeError as e:
            print("FAILED %s: %r" % (hash, e))
            failed.add(hash)
    else:
        return


    
def format_size(i):
    if i > 1024**4:
        return "%s TB" % round(i/(1024.0**4), 2)
    elif i > 1024**3:
        return "%s GB" % round(i/(1024.0**3), 2)
    elif i > 1024**2:
        return "%s MB" % round(i/(1024.0**2), 2)
    elif i > 1024**1:
        return "%s KB" % round(i/(1024.0**1), 2)
    else:
        return "%s B" % i

def get_torrent_info(db, hash, torrent):
    if not torrent:
        return
    encoding = None
    if b'encoding' in torrent and torrent[b'encoding'].decode():
       encoding = torrent[b'encoding'].decode()
       if encoding in ['utf8 keys', 'mbcs']:
           encoding = "utf-8"
    else:
        if b'name' in torrent[b'info']:
            encoding = chardet.detect(torrent[b'info'][b'name'])['encoding']
        if not encoding:
            encoding = "utf-8"
        
        
    if b'name.utf-8' in torrent[b'info']:
        name = torrent[b'info'][b'name.utf-8'].decode("utf-8", 'ignore')
    elif b'name' in torrent[b'info']:
        try:
            name = torrent[b'info'][b'name'].decode(encoding)
        except UnicodeDecodeError as e:
            encoding = chardet.detect(torrent[b'info'][b'name'])['encoding']
            if not encoding:
                encoding = "utf-8"
            name = torrent[b'info'][b'name'].decode(encoding, 'ignore')
        except AttributeError:
            name = unicode(torrent[b'info'][b'name'])
    else:
        return
    try:
        created = int(torrent.get(b'creation date', int(time.time())))
    except ValueError as e:
        created = int(time.time())

    files_nb = len(torrent[b'info'][b'files']) if b'files' in torrent[b'info'] else 1
    size = sum([file[b'length'] for file in torrent[b'info'][b'files']]) if b'files' in torrent[b'info'] else torrent[b'info'][b'length']
    ppath = b"path"
    if b'files' in torrent[b'info']:
        description=[name + "\n\n"]
        description_size = len(name + "\n\n")
        description.append("<table>\n")
        description_size+=len("<table>\n")
        files = []
        for i in range(len(torrent[b'info'][b'files'])):
            file = torrent[b'info'][b'files'][i]
            ppath = b"path"
            if b'path.utf-8' in file:
                ppath = b'path.utf-8'
                encoding = 'utf-8'
            for j in range(len(file[ppath])):
                p = file[ppath][j]
                if isinstance(p, int):
                    file[ppath][j] = str(p).encode()
                elif not isinstance(p, bytes):
                    raise ValueError("path element sould not be of type %s" % type(p).__name__)
            if file[ppath]:
                files.append((os.path.join(*file[ppath]), file))
        files.sort(key=lambda x:x[0])
        for (path, file) in files:
            if description_size > 40000:
                description.append("<tr><td>...</td><td></td></tr>\n")
                break
            try:
                desc = "<tr><td>%s</td><td>%s</td></tr>\n" % (path.decode(encoding), format_size(file[b'length']))
                description_size+=len(desc)
                description.append(desc)
            except UnicodeDecodeError:
                encoding = chardet.detect(path)['encoding']
                if encoding is None:
                     encoding = "utf-8"
                desc = "<tr><td>%s</td><td>%s</td></tr>\n" % (path.decode(encoding, 'ignore'), format_size(file[b'length']))
                description_size+=len(desc)
                description.append(desc)
        description.append("</table>")
        description = "".join(description)
    else:
        description=""
    
    return (name, created, files_nb, size, description)


failed=set()
def update_db_torrent(db, cur, hash, torrent, id=None, torcache=None, quiet=False):
    global failed
    real_hash = hashlib.sha1(utils.bencode(torrent[b'info'])).hexdigest()
    if real_hash == hash:
        try:
            if not quiet:
                print("\rdoing %s" % hash)
            infos = get_torrent_info(db, hash, torrent)
            if not infos:
                return
            (name, created, files_nb, size, description) = infos
            if not config.generate_description:
                description = None
            try:
                if torcache is not None:
                    cur.execute("UPDATE torrents SET name=%s, description=%s, size=%s, files_count=%s, created_at=%s, visible_status=0, torcache=%s WHERE hash=%s", (name, description, size, files_nb, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(created)), torcache, hash))
                else:
                    cur.execute("UPDATE torrents SET name=%s, description=%s, size=%s, files_count=%s, created_at=%s, visible_status=0 WHERE hash=%s", (name, description, size, files_nb, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(created)), hash))
            except Exception as error:
                print "update_db_torrent:%r" % error
            if not quiet:
                print("\r  done %s" % name)
            return True
        except (KeyError, UnicodeDecodeError, LookupError, ValueError, TypeError) as e:
            print("\r: %r" % e)
            failed.add(hash)
            return False
    else:
        try:
            cur.execute("UPDATE torrents SET hash=%s WHERE hash=%s", (real_hash, hash))
        except MySQLdb.IntegrityError:
            cur.execute("DELETE FROM torrents WHERE hash=%s", (hash,))
        os.rename("%s/%s.torrent" % (config.torrents_dir, hash), "%s/%s.torrent" % (config.torrents_dir, real_hash))


def update_db(db=None, hashs=None, torcache=None, quiet=False):
    global failed
    if db is None:
        db = MySQLdb.connect(**config.mysql)
    cur = db.cursor()
    if hashs is None:
        quiet=True
        hashs = get_hash(db)
    if not hashs:
        return
    if quiet and len(hashs)>1:
        progress = progressbar.ProgressBar(widgets=widget("torrents"))
        hashs = progress(hashs)
        print("Update db")
    try:
        for hash in hashs:
            if os.path.isfile("%s/%s.torrent" % (config.torrents_dir, hash)) and not hash in failed:
                t = get_torrent(db, hash)
                if t is None:
                    continue
                update_db_torrent(db, cur, hash, t, id=None, torcache=torcache, quiet=quiet)
    finally:
        db.commit()


def clean_files(db=None):
    if db is None:
        db = MySQLdb.connect(**config.mysql)
    files = os.listdir(config.torrents_dir)
    hashs = [h[:-8] for h in files if h.endswith(".torrent")]
    cur = db.cursor()
    i=0
    step = 500
    count = len(hashs)
    if count <= 0:
        return
    pbar = progressbar.ProgressBar(widgets=widget("files cleaned"), maxval=count).start()
    while hashs[i:i+step]:
        query = "SELECT hash, torcache FROM torrents WHERE created_at IS NOT NULL AND (%s)" % " OR ".join("hash=%s" for hash in hashs[i:i+step])
        cur.execute(query, tuple(hashs[i:i+step]))
        db_hashs = [(r[0], r[1]) for r in cur]
        c=i
        for hash, torcache in db_hashs:
            if torcache == 1:
                announce(hash)
            os.rename("%s/%s.torrent" % (config.torrents_dir, hash), "%s/%s.torrent" % (config.torrents_done, hash))
            c+=1
            pbar.update(c)
        i=min(i + step, count)
        pbar.update(i)
    pbar.finish()


def compact_torrent(db=None):
    """SUpprime les info des block du torrent"""
    if db is None:
        db = MySQLdb.connect(**config.mysql)
    files = os.listdir(config.torrents_done)
    hashs = [h[:-8] for h in files if h.endswith(".torrent")]
    cur = db.cursor()
    i=0
    step=500
    archive_path = "%s/%s/" % (config.torrents_archive, time.strftime("%Y-%m-%d"))
    try: os.mkdir(archive_path)
    except OSError as e:
        if e.errno != 17: # file exist
            raise
    count = len(hashs)
    if count <= 0:
        return
    pbar = progressbar.ProgressBar(widgets=widget("files archived"), maxval=count).start()
    while hashs[i:i+step]:
        query = "SELECT hash, torcache FROM torrents WHERE created_at IS NOT NULL AND (%s)" % " OR ".join("hash=%s" for hash in hashs[i:i+step])
        cur.execute(query, tuple(hashs[i:i+step]))
        db_hashs = dict((r[0], r[1]) for r in cur)
        c=i
        for hash, torcache in db_hashs.items():
            if config.compact_archived_torrents and torcache == 1:
                torrent = get_torrent(db, hash, base_path=config.torrents_done)
                torrent[b'info'][b'pieces'] = b''
                with open("%s%s.torrent" % (archive_path, hash), 'wb+') as f:
                    f.write(utils.bencode(torrent))
                os.remove("%s/%s.torrent" % (config.torrents_done, hash))
            else:
                os.rename("%s/%s.torrent" % (config.torrents_done, hash), "%s%s.torrent" % (archive_path, hash))
            c+=1
            pbar.update(c)
        i=min(i + step, count)
        pbar.update(i)
    pbar.finish()
    
def loop():
    last_clean = time.time()
    sql_error = False
    db = MySQLdb.connect(**config.mysql)
    db.commit()
    db.close()
    last_loop = 0
    loop_interval = 300
    try:
        while True:
            try:
                last_loop = time.time()
                print("\n\n\nNEW LOOP")
                get_new_torrent()
                db = MySQLdb.connect(**config.mysql)
                update_torrent_file(db)
                notfound = fetch_torrent(db)
                feed_torcache(db)
                scrape(db)
                db.commit()
                db.close()
                widgets = [progressbar.Bar('>'), ' ', progressbar.ETA(), ' ', progressbar.ReverseBar('<')]
                now = time.time()
                maxval = int(max(0, loop_interval - (now - last_loop)))
                if maxval > 0:
                    print("Now spleeping until the next loop")
                    pbar = progressbar.ProgressBar(widgets=widgets, maxval=maxval).start()
                    for i in range(0, maxval):
                        time.sleep(1)
                        pbar.update(i+1)
                    pbar.finish()
                db = MySQLdb.connect(**config.mysql)
                clean_files(db)
                compact_torrent(db)
                if time.time() - last_clean > 60*15:
                    clean(db)
                    last_clean = time.time()
                db.commit()
                sql_error = False
            except socket.timeout:
                if sql_error:
                    raise
                time.sleep(10)
                sql_error = True
            finally:
                try:
                    db.close()
                except:
                    pass
    finally:
        print("exiting")
def upload_to_torcache(db, hash, quiet=False):
    return False
    files = {'torrent': open("%s/%s.torrent" % (config.torrents_dir, hash), 'rb')}
    r = requests.post('http://torcache.net/autoupload.php', files=files)
    cur = db.cursor()
    try:
        if 'X-Torrage-Error-Msg' in r.headers:
            cur.execute("UPDATE torrents SET torcache=%s WHERE hash=%s", (False, hash))
            if not r.headers['X-Torrage-Error-Msg'].startswith('Deleted torrent:'):
                print("\r%s" % r.headers['X-Torrage-Error-Msg'])
        elif r.headers.get('X-Torrage-Infohash', "").lower() == hash:
            cur.execute("UPDATE torrents SET torcache=%s WHERE hash=%s", (True, hash))
            if not quiet:
                print("Uploaded %s to torcache" % hash)
        elif 'X-Torrage-Infohash' in r.headers:
            print("Bad hash %s -> %s" % (hash, r.headers.get('X-Torrage-Infohash').lower()))
            update_db(db, [hash], quiet=True)
    finally:
        db.commit()
    return r


def feed_torcache(db, hashs=None):
    if hashs is None:
        cur = db.cursor()
        cur.execute("SELECT hash FROM torrents WHERE created_at IS NOT NULL AND torcache IS NULL")
        hashs = [r[0].lower() for r in cur]
    if not hashs:
        return

    count = len(hashs)
    hashsq = queue.Queue()
    [hashsq.put(h) for h in hashs]
    counter = [0, 0, 0]
    pbar = progressbar.ProgressBar(widgets=widget("torrents uploaded to torcache"), maxval=count).start()
    def upload(hashsq, counter):
        db = MySQLdb.connect(**config.mysql)
        cur = db.cursor()
        try:
            while True:
                hash = hashsq.get(timeout=0)
                tc = is_torcache(hash)
                if not tc and os.path.isfile("%s/%s.torrent" % (config.torrents_dir, hash)):
                    try:
                        upload_to_torcache(db, hash, quiet=True)
                        counter[2]+=1
                    except requests.ConnectionError:
                        hashsq.put(hash)
                elif tc:
                    cur.execute("UPDATE torrents SET torcache=%s WHERE hash=%s", (True, hash))
                    counter[0]+=1
                else:
                    cur.execute("UPDATE torrents SET torcache=%s WHERE hash=%s", (False, hash))
                    counter[1]+=1
                pbar.update(pbar.currval + 1)
        except queue.Empty:
            pass
        finally:
            db.commit()
            cur.close()
            db.close()

    cur.close()
    stdout = sys.stdout
    try:
        threads = []
        sys.stdout = ThreadWriter(stdout)
        for i in range(0, 20):
            t = Thread(target=upload, args=(hashsq, counter))
            t.setName("upload-%02d" % i)
            t.daemon = True
            t.start()
            threads.append(t)
        join(threads)
    finally:
        sys.stdout = stdout
        pbar.finish()
    print("%s uploaded, %s already upped, %s failed" % (counter[0], counter[2], counter[1]))


def join(tl):
    while [t for t in tl if t.isAlive()]:
        time.sleep(0.5)

class ThreadWriter(object):
    def __init__(self, stdout):
        self.stdout = stdout

    def write(self, value):
        if threading.current_thread().name == 'MainThread':
            self.stdout.write(value)




if __name__ == '__main__':
    try:
        loop()
    except KeyboardInterrupt:
        pass
    print("exit")
