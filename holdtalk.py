#!/usr/bin/python3
"""holdtalk: hold a key, speak, release; the transcript is typed into the focused window.

  holdtalk.py run               the daemon (normally started by the systemd user unit)
  holdtalk.py detect            print the name of every key you press (to pick a trigger key)
  holdtalk.py transcribe FILE   send one audio file to the API and print the text
  holdtalk.py type TEXT         type TEXT into the focused window after 3 s (tests injection)

Settings: ~/.config/holdtalk/config.toml (restart the service after editing).
Key events are read only to see the trigger key and to notice chords; nothing typed is
stored or logged. The microphone is open only while the trigger key is held.
"""
import array, glob, json, os, queue, selectors, shutil, signal, subprocess, sys, threading, time
import tomllib, urllib.error, urllib.request, uuid, wave

import evdev
from evdev import ecodes as E

CONFIG = os.path.expanduser('~/.config/holdtalk/config.toml')
STATE = os.path.expanduser('~/.local/state/holdtalk')
RUNTIME = os.environ.get('XDG_RUNTIME_DIR', '/tmp')
VKBD = 'holdtalk-vkbd'
DEFAULTS = {
    'key': ['KEY_FN', 'KEY_RIGHTCTRL'],  # one evdev key name or a list; see `holdtalk.py detect`
    'devices': 'auto',           # 'auto' or a list of device-name substrings
    'min_hold_ms': 350,          # shorter presses are ignored
    'tail_ms': 200,              # keep recording this long after release
    'max_seconds': 180,
    'cancel_on_other_key': True, # trigger + another key = a normal shortcut, not dictation
    'model': 'gpt-4o-transcribe',
    'language': 'en',            # '' = auto-detect
    'prompt': '',                # optional spelling hints (names, jargon)
    'inject': 'auto',            # auto | wtype | paste | file:/path
    'type_delay_ms': 2,
    'trailing_space': True,
    'notify': 'all',             # all | errors | none
    'source': '',                # PipeWire source name; '' = system default
    'record_extra_args': [],     # extra pw-record arguments
    'silence_peak': 400,         # recordings quieter than this (0-32767) are not sent
    # API key: $OPENAI_API_KEY, else an OPENAI_API_KEY=... line in api_key_file, else 1Password (op_ref)
    'api_key_file': '~/.config/holdtalk/env',
    'op_ref': '',                # e.g. 'op://Vault/Item/credential' (needs the 1Password CLI)
    'op_token_file': '',         # optional env file holding OP_SERVICE_ACCOUNT_TOKEN for `op`
}
USD_PER_MIN = {'gpt-4o-transcribe': 0.006, 'gpt-4o-mini-transcribe': 0.003, 'whisper-1': 0.006}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def load_config(path=CONFIG):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, 'rb') as f:
            user = tomllib.load(f)
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise SystemExit(f'{path}: unknown setting(s): {", ".join(sorted(unknown))}')
        cfg.update(user)
    if isinstance(cfg['key'], str):
        cfg['key'] = [cfg['key']]
    for k in cfg['key']:
        if k not in E.ecodes:
            raise SystemExit(f"{path}: key {k!r} is not an evdev key name (run: holdtalk.py detect)")
    return cfg


# ---------- desktop helpers ----------

def wayland_env():
    env = dict(os.environ)
    wd = env.get('WAYLAND_DISPLAY')
    if not (wd and os.path.exists(os.path.join(RUNTIME, wd))):
        socks = sorted(p for p in glob.glob(os.path.join(RUNTIME, 'wayland-[0-9]*')) if not p.endswith('.lock'))
        if socks:
            env['WAYLAND_DISPLAY'] = os.path.basename(socks[-1])
    return env


def running(name):
    return subprocess.run(['pgrep', '-x', name], capture_output=True).returncode == 0


class Notifier:
    def __init__(self, mode):
        self.mode, self.nid = mode, '0'

    def show(self, text, ms, error=False):
        if self.mode == 'none' or (self.mode == 'errors' and not error):
            return
        try:
            r = subprocess.run(['notify-send', '-a', 'holdtalk', '-p', '-r', self.nid, '-t', str(ms),
                                '-u', 'critical' if error else 'low', 'holdtalk', text],
                               capture_output=True, text=True, timeout=3)
            self.nid = r.stdout.strip() or self.nid
        except Exception:
            pass

    def clear(self):
        if self.mode == 'all' and self.nid != '0':
            self.show('done', 1)


# ---------- API ----------

class Transcriber:
    def __init__(self, cfg):
        self.cfg, self._key = cfg, None

    def key(self, refresh=False):
        if self._key and not refresh:
            return self._key
        self._key = self._load_key()
        return self._key

    @staticmethod
    def _env_file(path):
        out = {}
        path = os.path.expanduser(path)
        if path and os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip().removeprefix('export ')
                    if '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        out[k.strip()] = v.strip().strip('"\'')
        return out

    def _load_key(self):
        key = os.environ.get('OPENAI_API_KEY') or self._env_file(self.cfg['api_key_file']).get('OPENAI_API_KEY')
        if key:
            return key
        if not self.cfg['op_ref']:
            raise RuntimeError(f"no API key: put OPENAI_API_KEY=... in {self.cfg['api_key_file']} (chmod 600) or set op_ref")
        env = dict(os.environ, **self._env_file(self.cfg['op_token_file']))
        r = subprocess.run(['op', 'read', self.cfg['op_ref']], capture_output=True, text=True, env=env, timeout=30)
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError('could not read the API key from 1Password: ' + r.stderr.strip()[:200])
        return r.stdout.strip()

    def transcribe(self, path):
        fields = {'model': self.cfg['model'], 'response_format': 'json'}
        if self.cfg['language']:
            fields['language'] = self.cfg['language']
        if self.cfg['prompt']:
            fields['prompt'] = self.cfg['prompt']
        b = uuid.uuid4().hex
        body = b''
        for k, v in fields.items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        with open(path, 'rb') as f:
            body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{os.path.basename(path)}"\r\n'
                     'Content-Type: application/octet-stream\r\n\r\n').encode() + f.read() + f'\r\n--{b}--\r\n'.encode()
        for attempt in (0, 1):
            req = urllib.request.Request('https://api.openai.com/v1/audio/transcriptions', data=body, headers={
                'Authorization': 'Bearer ' + self.key(refresh=attempt == 1),
                'Content-Type': f'multipart/form-data; boundary={b}'})
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    return json.load(r).get('text', '')
            except urllib.error.HTTPError as e:
                msg = e.read().decode('utf-8', 'replace')[:300].replace(self._key or '\0', '<key>')
                if e.code == 401 and attempt == 0:
                    continue
                raise RuntimeError(f'API error {e.code}: {msg}')


# ---------- typing the result ----------

class Injector:
    def __init__(self, cfg):
        self.cfg, self.ui = cfg, None

    def backend(self):
        b = self.cfg['inject']
        if b != 'auto':
            return b
        return 'paste' if running('kwin_wayland') else 'wtype'

    def inject(self, text):
        b = self.backend()
        if b.startswith('file:'):
            with open(b[5:], 'a') as f:
                f.write(text + '\n')
        elif b == 'wtype':
            r = subprocess.run(['wtype', '-d', str(self.cfg['type_delay_ms']), '--', text],
                               env=wayland_env(), capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError('wtype failed: ' + r.stderr.strip()[:200])
        elif b == 'paste':   # KWin has no virtual-keyboard protocol: clipboard + Ctrl+V through uinput
            subprocess.run(['wl-copy', '--', text], env=wayland_env(), check=True, timeout=10)
            if self.ui is None:
                self.ui = evdev.UInput({E.EV_KEY: [E.KEY_LEFTCTRL, E.KEY_V]}, name=VKBD)
                time.sleep(0.6)
            for code, val in ((E.KEY_LEFTCTRL, 1), (E.KEY_V, 1), (E.KEY_V, 0), (E.KEY_LEFTCTRL, 0)):
                self.ui.write(E.EV_KEY, code, val)
                self.ui.syn()
                time.sleep(0.02)
        else:
            raise RuntimeError(f'unknown inject backend {b!r}')
        return b


# ---------- recording ----------

class Recorder:
    def __init__(self, cfg):
        self.cfg, self.proc, self.path, self.t0 = cfg, None, None, 0.0

    def start(self):
        self.path = os.path.join(RUNTIME, f'holdtalk-{os.getpid()}-{int(time.time() * 1000)}.wav')
        cmd = ['pw-record', '--rate', '16000', '--channels', '1', '--format', 's16']
        if self.cfg['source']:
            cmd += ['--target', self.cfg['source']]
        self.proc = subprocess.Popen(cmd + list(self.cfg['record_extra_args']) + [self.path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.t0 = time.monotonic()

    def stop(self, keep=True):
        """Stop the recording; returns (path, held_seconds) or None when discarded."""
        proc, path, held = self.proc, self.path, time.monotonic() - self.t0
        self.proc = self.path = None
        if proc is None:
            return None

        def finish():
            if keep:
                time.sleep(self.cfg['tail_ms'] / 1000)
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if not keep and path and os.path.exists(path):
                os.unlink(path)
        if keep:
            finish()
            return path, held
        threading.Thread(target=finish, daemon=True).start()
        return None


def wav_stats(path):
    with wave.open(path, 'rb') as w:
        n, rate = w.getnframes(), w.getframerate()
        a = array.array('h', w.readframes(n))
    return (n / rate, max(max(a), -min(a)) if a else 0)


# ---------- daemon ----------

class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.codes = {E.ecodes[k] for k in cfg['key']}
        self.active = None   # the trigger key that started the current recording
        self.sel = selectors.DefaultSelector()
        self.devs, self.down = {}, set()
        self.rec, self.tx, self.inj = Recorder(cfg), Transcriber(cfg), Injector(cfg)
        self.note = Notifier(cfg['notify'])
        self.jobs = queue.Queue()
        self.announced = False
        os.makedirs(STATE, exist_ok=True)

    def wanted(self, dev):
        if dev.name == VKBD:
            return False
        keys = dev.capabilities().get(E.EV_KEY, [])
        if self.cfg['devices'] == 'auto':
            return bool(self.codes & set(keys)) or E.KEY_A in keys
        return any(s.lower() in dev.name.lower() for s in self.cfg['devices'])

    def rescan(self):
        for path in evdev.list_devices():
            if path in self.devs:
                continue
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            if self.wanted(dev):
                self.devs[path] = dev
                self.sel.register(dev, selectors.EVENT_READ)
                log('listening on', dev.name, '[has trigger key]' if self.codes & set(dev.capabilities().get(E.EV_KEY, [])) else '')
            else:
                dev.close()

    def drop(self, dev):
        self.sel.unregister(dev)
        self.devs.pop(dev.path, None)
        self.down = {k for k in self.down if k[0] != dev.path}
        log('lost', dev.name)
        try:
            dev.close()
        except OSError:
            pass

    def on_key(self, dev, code, val):
        if val == 2:
            return
        ident = (dev.path, code)
        if val == 1:
            others = bool(self.down)
            self.down.add(ident)
            if self.rec.proc is None:
                if code in self.codes and not (others and self.cfg['cancel_on_other_key']):
                    self.rec.start()
                    self.active = code
                    self.announced = False
            elif code != self.active and self.cfg['cancel_on_other_key']:
                self.rec.stop(keep=False)
                self.note.clear()
                log('cancelled (another key was pressed)')
        else:
            self.down.discard(ident)
            if code == self.active and self.rec.proc is not None:
                self.finish()

    def finish(self):
        held = time.monotonic() - self.rec.t0
        if held * 1000 < self.cfg['min_hold_ms']:
            self.rec.stop(keep=False)
            return
        self.note.show('Transcribing…', 20000)
        self.jobs.put(self.rec.stop(keep=True))

    def tick(self):
        if self.rec.proc is None:
            return
        held = time.monotonic() - self.rec.t0
        if not self.announced and held * 1000 >= self.cfg['min_hold_ms']:
            self.announced = True
            self.note.show('Listening…', self.cfg['max_seconds'] * 1000)
        if held > self.cfg['max_seconds']:
            log('max length reached')
            self.finish()

    def worker(self):
        while True:
            path, held = self.jobs.get()
            try:
                secs, peak = wav_stats(path)
                if secs < 0.2 or peak < self.cfg['silence_peak']:
                    log(f'nothing heard ({secs:.1f}s, peak {peak})')
                    self.note.show('Nothing heard', 2000)
                    continue
                t = time.monotonic()
                text = ' '.join(self.tx.transcribe(path).split())
                api = time.monotonic() - t
                if not text:
                    log(f'empty transcript ({secs:.1f}s)')
                    self.note.clear()
                    continue
                how = self.inj.inject(text + (' ' if self.cfg['trailing_space'] else ''))
                self.note.clear()
                log(f'typed {len(text)} chars via {how} (audio {secs:.1f}s, api {api:.1f}s)')
                rate = USD_PER_MIN.get(self.cfg['model'])
                with open(os.path.join(STATE, 'usage.jsonl'), 'a') as f:
                    f.write(json.dumps({'ts': int(time.time()), 'model': self.cfg['model'], 'audio_s': round(secs, 1),
                                        'chars': len(text), 'est_usd': round(secs / 60 * rate, 5) if rate else None}) + '\n')
            except Exception as e:
                log('ERROR', e)
                self.note.show(str(e)[:200], 6000, error=True)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def run(self):
        for tool in ['pw-record', 'wtype', 'wl-copy', 'notify-send']:
            if not shutil.which(tool):
                log('warning: missing tool', tool)
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=lambda: self._prefetch(), daemon=True).start()
        log(f"holdtalk ready: hold {' or '.join(self.cfg['key'])}, model {self.cfg['model']}, inject {self.cfg['inject']}")
        last_scan = 0.0
        while True:
            if time.monotonic() - last_scan > 3:
                self.rescan()
                last_scan = time.monotonic()
            for sk, _ in self.sel.select(timeout=0.1 if self.rec.proc else 3):
                dev = sk.fileobj
                try:
                    for ev in dev.read():
                        if ev.type == E.EV_KEY:
                            self.on_key(dev, ev.code, ev.value)
                except OSError:
                    self.drop(dev)
            self.tick()

    def _prefetch(self):
        try:
            self.tx.key()
            log('API key loaded')
        except Exception as e:
            log('ERROR', e)


def detect():
    devs = []
    for p in evdev.list_devices():
        try:
            d = evdev.InputDevice(p)
        except OSError:
            continue
        if E.EV_KEY in d.capabilities():
            devs.append(d)
    sel = selectors.DefaultSelector()
    for d in devs:
        sel.register(d, selectors.EVENT_READ)
    print('Press the key you want to use (Ctrl+C to stop). A key that prints nothing cannot be used.', flush=True)
    while True:
        for sk, _ in sel.select():
            for ev in sk.fileobj.read():
                if ev.type == E.EV_KEY and ev.value == 1:
                    name = E.bytype[E.EV_KEY].get(ev.code, ev.code)
                    print(f'{name if isinstance(name, str) else name[0]:<20} on {sk.fileobj.name}', flush=True)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    cfg_path = os.environ.get('HOLDTALK_CONFIG', CONFIG)
    if cmd == 'detect':
        return detect()
    cfg = load_config(cfg_path)
    if cmd == 'run':
        Daemon(cfg).run()
    elif cmd == 'transcribe':
        print(Transcriber(cfg).transcribe(sys.argv[2]))
    elif cmd == 'type':
        time.sleep(3)
        print('typed via', Injector(cfg).inject(sys.argv[2]))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
