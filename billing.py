#!/usr/bin/env python3
"""Start Billing & Order Management, on any operating system.

    python billing.py              start it and open it in its own window
    python billing.py --tab        ... in a tab of the everyday browser instead
    python billing.py --stop       stop a server this script started
    python billing.py --status     say whether it is running
    python billing.py --shortcut   put a launcher on the Desktop
    python billing.py --setup      first run on a new PC: everything above

This is the one place that knows how to start the app. run.sh, run.bat and the
Desktop launchers all come through here, so a fix lands everywhere at once.

Nothing outside the standard library is needed to run the app. The printed
stationery additionally wants reportlab; the app says so when it is missing.
"""
import argparse
import os
import socket
import subprocess
import sys
import urllib.request
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, 'app', 'server.py')
PIDFILE = os.path.join(HERE, 'app', 'data', 'billing.pid')
LOG = os.path.join(HERE, 'app', 'data', 'server.log')
ICON = os.path.join(HERE, 'app', 'static', 'billing.ico')
# Built on a Windows PC by make_exe.bat; the shortcuts point at it when it is
# there, so the icon people click is a program of its own.
LAUNCHER = os.path.join(HERE, 'Billing.exe')
DEFAULT_PORT = 8800
MIN_PYTHON = (3, 8)
WINDOWS = os.name == 'nt'


# ------------------------------------------------------------ Windows only

def quietly(argv, **kw):
    """Run a helper program without flashing a console window at the user.

    Windows gives a console program its own window when the process starting
    it has none -- which is exactly the case here, since the server runs
    detached. Every tasklist and taskkill below would blink a black rectangle
    on screen without this flag.
    """
    if WINDOWS:
        kw['creationflags'] = kw.get('creationflags', 0) | \
            getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
    return subprocess.run(argv, **kw)


def windowless_python():
    """pythonw.exe where there is one, so the server never owns a window.

    Started from python.exe the server would keep a console open behind the
    browser for as long as the app runs, and closing that window by mistake
    would take the books offline mid-save. pythonw.exe is the same interpreter
    with no console at all; it sits beside python.exe in every python.org
    install. Output still reaches the log file, which is where it was going.
    """
    exe = sys.executable
    if not WINDOWS or not exe:
        return exe
    folder, name = os.path.split(exe)
    if name.lower().startswith('pythonw'):
        return exe
    quiet = os.path.join(folder, name.lower().replace('python', 'pythonw', 1))
    return quiet if os.path.isfile(quiet) else exe


def say(text, title='Billing', bad=False):
    """Tell the user something, whether or not there is a console to say it in.

    Double-clicked from a Desktop shortcut there is no console: a message
    printed here would go nowhere and the app would look like it had simply
    failed to start. A message box is the only thing that reaches them.
    """
    stream = sys.stderr if bad else sys.stdout
    try:
        stream.write(text + '\n')
        stream.flush()
        has_console = True
    except (OSError, AttributeError, ValueError):
        has_console = False
    if WINDOWS and not has_console:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, text, title, 0x10 if bad else 0x40)
        except Exception:               # no user32, or a service session
            pass


def _program_folders():
    """The folders holding the program, never the books: app\\data is left
    out, and so is Python's own cache."""
    data = os.path.normcase(os.path.join(HERE, 'app', 'data'))
    for folder, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d != '__pycache__' and
                   os.path.normcase(os.path.join(folder, d)) != data]
        yield folder, files


def unmark():
    """Take Windows' "came from the internet" mark off the program's files.

    A Billing folder that came by download, e-mail or WhatsApp carries it on
    every file in it. Windows then puts up a security warning each time one
    of its .bat files is double-clicked, and .NET refuses the WebView2 files
    Billing's own window is drawn with. The mark is a stream kept beside each
    file (Zone.Identifier); taking it off changes nothing in the file itself.
    """
    for folder, files in _program_folders():
        for name in files:
            try:
                os.remove(os.path.join(folder, name) + ':Zone.Identifier')
            except OSError:
                pass                    # not marked -- nearly always


# What earlier packages put in the Billing folder and Billing does not use:
# samples and notes from building it, the Mac's launchers, and maintenance
# scripts from before the SHREE update screen. Asked on 6 Oct 2026: "only
# keep the files which are required from the software, and delete the
# rest". Extracting a newer package over the folder never takes anything
# away, so they are taken out here -- these names only, never the books.
RETIRED = (
    'README.md', 'sample_gst.xlsx', 'shree-gap-register.html',
    'gt211einvoice-b.json', 'gt231einvoice-b.json', 'gt232einvoice-b.json',
    'run.sh', 'stop.sh', 'make_desktop_app.sh',
    'app/check_customers.py', 'app/check_gst.py', 'app/import_shree.py',
    'app/remove_firm.py', 'app/restore_user_docs.py', 'app/setup_firms.py',
    'app/shree_history.py',
    'bst-pdf-generator/.gitignore', 'bst-pdf-generator/README.md',
    'bst-pdf-generator/check_ink.py', 'bst-pdf-generator/generate.py',
    'bst-pdf-generator/requirements.txt',
    'bst-pdf-generator/data/challan_f80.json',
    'bst-pdf-generator/data/invoice_gt184.json',
    'bst-pdf-generator/assets/fonts/.gitkeep',
    # Billing is kept to this computer for now (10 Oct 2026).
    'anywhere.bat',
)
RETIRED_FOLDERS = ('bst-pdf-generator/data', 'bst-pdf-generator/assets/fonts')


def tidy():
    """Remove RETIRED, and any copy an edit left behind (name.before-...),
    from the program's folders. Windows only: on the computer Billing is
    written on these are still in use."""
    gone = 0
    paths = [os.path.join(HERE, *p.split('/')) for p in RETIRED]
    for folder, files in _program_folders():
        paths += [os.path.join(folder, n) for n in files if '.before-' in n]
    for path in paths:
        try:
            os.remove(path)
            gone += 1
        except OSError:
            pass                        # already gone
    for name in RETIRED_FOLDERS:
        try:
            os.rmdir(os.path.join(HERE, *name.split('/')))   # only if empty
        except OSError:
            pass
    if gone:
        print('Removed %d file%s Billing does not use.'
              % (gone, '' if gone == 1 else 's'))


# --------------------------------------------------------------- is it up?

def listening(port, host='127.0.0.1', timeout=1.0):
    """True when something holds the port -- anything at all."""
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def answering(port, host='127.0.0.1', timeout=3.0):
    """True when the app on that port is actually serving.

    **Holding the port is not the same as working.** A server can stay up
    with the port bound and still be unable to read its own files -- macOS
    withdrawing access from a long-lived process does exactly that, and it
    was left answering every request with a 500 while `start` looked at the
    open socket and said "already running". Asking it something only the real
    app can answer is the difference.
    """
    if not listening(port, host, min(timeout, 1.0)):
        return False
    # Never through a proxy. urllib takes Windows' Internet settings, and a
    # PC with a proxy set there -- by an office network or a web-filtering
    # antivirus -- that does not exempt 127.0.0.1 sent this question to the
    # proxy, which cannot see this PC's server: Billing was said not to have
    # started while it sat there running.
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with direct.open('http://%s:%d/api/meta' % (host, port),
                         timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# Long enough for the first start after an update on a slow PC, with a virus
# scanner reading each new file as Python opens it; a server that is up is
# found within a quarter of a second of answering.
START_SECONDS = 60
BUSY_SECONDS = 15


def wait_until_up(port, seconds=START_SECONDS):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if answering(port):
            return True
        time.sleep(0.25)
    return False


# ------------------------------------------------------------------ pidfile

def read_pid():
    try:
        with open(PIDFILE, encoding='utf-8') as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def write_pid(pid):
    os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
    with open(PIDFILE, 'w', encoding='utf-8') as fh:
        fh.write(str(pid))


def clear_pid():
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def alive(pid):
    """True when that process still exists. Works on Windows and Unix."""
    if not pid:
        return False
    if WINDOWS:
        out = quietly(['tasklist', '/FI', 'PID eq %d' % pid],
                      capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)                 # signal 0 only tests for existence
        return True
    except OSError:
        return False


# ------------------------------------------------- whoever holds the port
#
# The pidfile names only a server this folder's billing.py started. On 9 Oct
# 2026 the PC had a Billing server on 8800 that no pidfile named -- its record
# lost, or started from another Billing folder -- and it had stopped
# answering: every start said "Something is on port 8800, but this script did
# not start it" and Billing would not open. The port itself says who it is.

def _output(argv, timeout=30):
    try:
        return quietly(argv, capture_output=True, text=True, errors='replace',
                       timeout=timeout).stdout or ''
    except (OSError, subprocess.SubprocessError):
        return ''


def port_owner(port):
    """The process listening on `port` on this computer, or None."""
    if WINDOWS:
        for line in _output(['netstat', '-ano']).splitlines():
            # TCP  127.0.0.1:8800  0.0.0.0:0  LISTENING  4321. The state is
            # written in the PC's own language; a listening row is told by
            # its far end, which is nobody yet.
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == 'TCP' and \
                    parts[1].endswith(':%d' % port) and \
                    parts[2] in ('0.0.0.0:0', '[::]:0') and \
                    parts[-1].isdigit() and parts[-1] != '0':
                return int(parts[-1])
        return None
    for word in _output(['lsof', '-nP', '-iTCP:%d' % port, '-sTCP:LISTEN',
                         '-t']).split():
        if word.isdigit():
            return int(word)
    return None


def program_of(pid):
    """(program name, whole command line) of a process; '' where not known."""
    if WINDOWS:
        row = _output(['tasklist', '/FI', 'PID eq %d' % pid, '/FO', 'CSV',
                       '/NH']).strip()
        name = row.split('","')[0].strip('"') if row.startswith('"') else ''
        line = _output(['powershell', '-NoProfile', '-NonInteractive',
                        '-Command', "(Get-CimInstance Win32_Process -Filter "
                        "'ProcessId=%d').CommandLine" % pid]).strip()
        return name, line
    line = _output(['ps', '-o', 'command=', '-p', str(pid)]).strip()
    return os.path.basename(line.split(' ')[0]) if line else '', line


def is_billing(name, line):
    """A Billing server -- this folder's or another Billing folder's."""
    if line:
        return 'app/server.py' in line.replace('\\', '/').lower()
    # No command line to be had (Windows 7, or a server started as
    # administrator): Python on Billing's port is Billing.
    return name.lower().startswith('python')


# ------------------------------------------------------------------ actions

LOG_LIMIT = 10 * 1024 * 1024


def rotate_log():
    """Start a fresh log once the old one passes 10 MB, keeping one before.

    The server writes a line for every request, and it is left running for
    months; unchecked, the log would grow for as long as the app is used.
    """
    try:
        if os.path.getsize(LOG) > LOG_LIMIT:
            os.replace(LOG, LOG + '.1')
    except OSError:
        pass


# ------------------------------------------------------- the app's window
#
# Asked on 5 Oct 2026: "Can the software run as its own tab and not on
# chrome?" The app opens as a window of its own -- no tabs, no address bar,
# its own button on the taskbar -- rather than as one more tab among the
# everyday browser's. The page is still drawn by Chrome's engine (Edge is the
# same engine), which no window of its own can avoid; what it leaves behind
# is everything else in that browser: its add-ons, which watch every change
# on every page, and its other tabs. So the window keeps settings of its own,
# apart from the browser used for everything else.

def window_profile(exe):
    """Where the app's window keeps its settings: the size it was left at,
    and the screens remembered in it. Per person, not in the Billing folder,
    so an update over the folder never touches it -- and one for Edge apart
    from Chrome's, since neither reads the other's."""
    if WINDOWS:
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = (os.environ.get('XDG_DATA_HOME')
                or os.path.expanduser('~/.local/share'))
    edge = 'edge' in os.path.basename(exe).lower()
    return os.path.join(base, 'Billing', 'window-edge' if edge else 'window')


def app_browser():
    """Chrome, else Edge -- a browser that can open a page as a window of its
    own. Every Windows 10 and 11 PC has Edge. None when there is neither."""
    if WINDOWS:
        def registered(exe):
            # Where the installer said it put it, for this person or for all.
            try:
                import winreg
            except ImportError:
                return []
            found = []
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(root, 'SOFTWARE\\Microsoft\\Windows\\'
                                        'CurrentVersion\\App Paths\\' + exe) as k:
                        found.append(winreg.QueryValue(k, None))
                except OSError:
                    pass
            return found
        roots = [os.environ.get(v) for v in
                 ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')]
        candidates = (
            registered('chrome.exe')
            + [os.path.join(r, 'Google', 'Chrome', 'Application', 'chrome.exe')
               for r in roots if r]
            + registered('msedge.exe')
            + [os.path.join(r, 'Microsoft', 'Edge', 'Application', 'msedge.exe')
               for r in roots if r])
    elif sys.platform == 'darwin':
        candidates = [os.path.join(top, name + '.app', 'Contents', 'MacOS', name)
                      for name in ('Google Chrome', 'Microsoft Edge', 'Chromium')
                      for top in ('/Applications',
                                  os.path.expanduser('~/Applications'))]
    else:
        import shutil
        candidates = [shutil.which(n) for n in
                      ('google-chrome', 'google-chrome-stable', 'chromium',
                       'chromium-browser', 'microsoft-edge')]
    for exe in candidates:
        exe = (exe or '').strip().strip('"')
        if exe and os.path.isfile(exe):
            return exe
    return None


def exe_has_window():
    """Was Billing.exe built with its own window (make_exe.bat, WEBVIEW)?
    Such a one names Microsoft's WebView2 files inside it; one built before
    5 Oct 2026, or without them, does not."""
    try:
        with open(LAUNCHER, 'rb') as f:
            return b'Microsoft.Web.WebView2.Core' in f.read(4 * 1024 * 1024)
    except OSError:
        return False


def open_window(url, tab=False):
    """Open the app in its own window -- or, with `tab` or no browser that can,
    in the everyday browser as before."""
    # On Windows the window is Billing.exe's own, with no browser at all
    # (app/launcher/Billing.cs) -- so run.bat and install.bat open it too.
    # Never back and forth: Billing.exe says when it is the one asking, and
    # what is handed to it is marked, so an older Billing.exe -- one with no
    # window, which only calls billing.py -- gets the browser's window here.
    if (WINDOWS and not tab and exe_has_window()
            and not os.environ.get('BILLING_FROM_EXE')
            and not os.environ.get('BILLING_FROM_PY')):
        port = url.rstrip('/').rsplit(':', 1)[-1]
        env = dict(os.environ, BILLING_FROM_PY='1')
        try:
            subprocess.Popen([LAUNCHER, '--port', port], cwd=HERE, env=env,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP |
                                            getattr(subprocess, 'DETACHED_PROCESS', 0)))
            return
        except OSError:
            pass                        # the browser's window, rather than nothing
    exe = None if tab else app_browser()
    if exe:
        profile = window_profile(exe)
        # No "Restore pages?" bubble after the PC was switched off with
        # Billing open: there is nothing to restore, only Billing again.
        argv = [exe, '--app=' + url, '--user-data-dir=' + profile,
                '--no-first-run', '--no-default-browser-check',
                '--hide-crash-restore-bubble']
        # Full screen the first time; after that it opens the size it was
        # left at, which the browser remembers.
        if not os.path.isdir(profile):
            argv.append('--start-maximized')
        kwargs = {}
        if WINDOWS:
            kwargs['creationflags'] = (subprocess.CREATE_NEW_PROCESS_GROUP |
                                       getattr(subprocess, 'DETACHED_PROCESS', 0))
        else:
            kwargs['start_new_session'] = True
        try:
            os.makedirs(profile, exist_ok=True)
            subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kwargs)
            return
        except OSError:
            pass                        # the tab, rather than nothing
    webbrowser.open(url)


def start(port, host, open_browser=True, tab=False):
    # A server that is busy rather than stuck -- an update putting the books
    # in place, a long report -- is given a few seconds to answer before it
    # is taken for wedged.
    if answering(port) or (listening(port) and
                           wait_until_up(port, BUSY_SECONDS)):
        print('Billing is already running on port %d.' % port)
        if open_browser:
            open_window('http://127.0.0.1:%d/' % port, tab)
        return 0

    # The port is held but nothing behind it is answering: a wedged server.
    # Take it down before starting a new one, or the new one cannot bind and
    # the user is left looking at the broken one.
    if listening(port):
        print('Something is holding port %d but not answering. '
              'Stopping it first.' % port)
        if stop(port):
            return 1                    # not Billing's: stop() has said whose
        for _ in range(20):
            if not listening(port):
                break
            time.sleep(0.25)
        if listening(port):
            say('Port %d is held by something that will not stop and will '
                'not answer.\n\nClose whatever is using it, or start the '
                'app on another port:\n\n    python3 billing.py --port %d'
                % (port, port + 1), bad=True)
            return 1

    if not os.path.isfile(SERVER):
        say('Cannot find %s.\nIs billing.py still next to the app folder?'
            % SERVER, bad=True)
        return 1

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    rotate_log()
    argv = [windowless_python(), SERVER, '--port', str(port),
            '--host', host, '--no-browser']

    # Detach so closing the window that started us does not kill the server.
    kwargs = {}
    if WINDOWS:
        kwargs['creationflags'] = (subprocess.CREATE_NEW_PROCESS_GROUP |
                                   getattr(subprocess, 'DETACHED_PROCESS', 0))
    else:
        kwargs['start_new_session'] = True

    with open(LOG, 'a', encoding='utf-8') as log:
        proc = subprocess.Popen(argv, cwd=HERE, stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, **kwargs)
    write_pid(proc.pid)

    if not wait_until_up(port):
        clear_pid()
        say('Billing did not start within %d seconds.\n\n' % START_SECONDS +
            'The last lines of the log say:\n\n%s\nThe whole log is at\n%s'
            % (tail(LOG, 8), LOG), bad=True)
        return 1

    url = 'http://127.0.0.1:%d/' % port
    print('Billing is running at %s' % url)
    print('Stop it with:        %s billing.py --stop'
          % ('python' if os.name == 'nt' else 'python3'))
    if open_browser:
        open_window(url, tab)
    return 0


def stop(port):
    pid = read_pid()
    if not alive(pid):
        clear_pid()
        # `listening`, not `answering`: a server that has wedged still holds
        # the port, and saying "not running" of it helps nobody.
        if not listening(port):
            print('Billing is not running.')
            return 0
        # Not one this script started: a Billing server is stopped all the
        # same, anything else is named and left alone.
        pid = port_owner(port)
        name, line = program_of(pid) if pid else ('', '')
        if not pid or not is_billing(name, line):
            say(('Port %d is in use by %s (process %d), which is not '
                 'Billing.\n\nClose that program, or end it in Task Manager '
                 '(Windows) / Activity Monitor (Mac).' % (port, name, pid))
                if pid and name else
                ('Something is on port %d and could not be stopped from '
                 'here.\n\nRestart the computer, or end pythonw.exe in Task '
                 'Manager (Windows) / Activity Monitor (Mac).' % port),
                bad=True)
            return 1
        print('Billing is running with no record of it here (pid %d).' % pid)

    if WINDOWS:
        quietly(['taskkill', '/PID', str(pid), '/T', '/F'],
                capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    for _ in range(40):                 # give it 10s to close the socket
        if not alive(pid):
            break
        time.sleep(0.25)
    if not WINDOWS and alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)    # wedged past hearing SIGTERM
        except OSError:
            pass
        for _ in range(20):
            if not alive(pid):
                break
            time.sleep(0.25)
    clear_pid()
    print('Stopped Billing (pid %d).' % pid)
    return 0


def close_windows():
    """Close Billing.exe's own windows, as stopping should.

    A window left open on a stopped server can only say it cannot reach it,
    and it keeps Billing.exe running -- so make_exe.bat, the next step after
    stop.bat in every update, could not write a new one over it. Only this
    folder's Billing.exe, and asked to close as its X would ask, so the place
    and zoom it was left at are kept.
    """
    if not WINDOWS or not os.path.isfile(LAUNCHER):
        return
    script = ("Get-Process -Name Billing -ErrorAction SilentlyContinue | "
              "Where-Object { $_.Path -eq '%s' } | "
              "ForEach-Object { [void]$_.CloseMainWindow() }"
              % LAUNCHER.replace("'", "''"))
    try:
        quietly(['powershell', '-NoProfile', '-NonInteractive',
                 '-ExecutionPolicy', 'Bypass', '-Command', script],
                capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def status(port):
    pid = read_pid()
    up = answering(port)
    held = up or listening(port)
    print('Billing: %s' % ('running' if up else
                           'HELD BUT NOT ANSWERING' if held else 'not running'))
    print('Port:    %d %s' % (port, '(answering)' if up else
                              '(held, but not serving -- start again to '
                              'replace it)' if held else '(quiet)'))
    if pid:
        print('Pid:     %d %s' % (pid, '' if alive(pid) else '(stale pidfile)'))
    print('Python:  %s' % sys.version.split()[0])
    print('Data:    %s' % os.path.join(HERE, 'app', 'data', 'billing.db'))
    return 0 if up else 1


def tail(path, lines=12):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return ''.join(fh.readlines()[-lines:])
    except OSError:
        return '(no log)\n'


# ---------------------------------------------------------------- shortcuts

def windows_desktop():
    """The real Desktop folder, which is not always under the home folder.

    With OneDrive Backup switched on -- which it is by default on a new PC --
    the Desktop lives inside the OneDrive folder and ~/Desktop either does not
    exist or is an old empty one. The registry is the only thing that knows.
    """
    home = os.path.join(os.path.expanduser('~'), 'Desktop')
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Explorer'
            r'\User Shell Folders')
        with key:
            raw, _kind = winreg.QueryValueEx(key, 'Desktop')
        real = os.path.expandvars(raw)
        if os.path.isdir(real):
            return real
    except Exception:
        pass
    return home


def make_lnk(path, target, args='', workdir='', icon='', description=''):
    """Write a Windows .lnk, using the shell that is already on the machine.

    A .lnk is what makes the launcher behave like an installed program: it can
    be pinned to Start or the taskbar, it carries the icon, and pointing it at
    pythonw.exe means no console window ever appears. Building the binary
    format by hand would be a lot of code for something WScript.Shell has done
    since Windows 98; PowerShell is how you reach it without pywin32.
    """
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath = '%s';"
        "$s.Arguments = '%s';"
        "$s.WorkingDirectory = '%s';"
        "$s.Description = '%s';"
        % tuple(x.replace("'", "''") for x in
                (path, target, args, workdir, description)))
    if icon and os.path.isfile(icon):
        script += "$s.IconLocation = '%s';" % icon.replace("'", "''")
    script += '$s.Save()'
    done = quietly(['powershell', '-NoProfile', '-NonInteractive',
                    '-ExecutionPolicy', 'Bypass', '-Command', script],
                   capture_output=True, text=True)
    if done.returncode != 0 or not os.path.isfile(path):
        raise OSError((done.stderr or '').strip()
                      or 'PowerShell would not write %s' % path)


def shortcut():
    """Put a double-clickable launcher on the Desktop for this platform."""
    if WINDOWS:
        return windows_shortcuts()

    desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
    if not os.path.isdir(desktop):
        say('No Desktop folder at %s.' % desktop, bad=True)
        return 1

    if sys.platform == 'darwin':
        maker = os.path.join(HERE, 'make_desktop_app.sh')
        if os.path.isfile(maker):
            return subprocess.run(['/bin/sh', maker]).returncode
        say('make_desktop_app.sh is missing.', bad=True)
        return 1

    target = os.path.join(desktop, 'billing.desktop')     # Linux
    with open(target, 'w', encoding='utf-8') as fh:
        fh.write('[Desktop Entry]\nType=Application\nName=Billing\n'
                 'Exec=%s %s\nPath=%s\nTerminal=false\nCategories=Office;\n'
                 % (sys.executable, os.path.abspath(__file__), HERE))
    os.chmod(target, 0o755)
    print('Created %s' % target)
    return 0


def windows_shortcuts():
    """Billing on the Desktop and in the Start menu, with its icon.

    Both point at Billing.exe where make_exe.bat has built it, and otherwise
    at pythonw.exe rather than at run.bat -- either way there is no console
    window to flash on the way in or to sit behind the browser afterwards.
    """
    if os.path.isfile(LAUNCHER):
        exe, here = LAUNCHER, ''
    else:
        exe, here = windowless_python(), os.path.abspath(__file__)
    made, failed = [], []
    places = [(windows_desktop(), 'Desktop')]
    start = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows',
                         'Start Menu', 'Programs')
    if os.path.isdir(start):
        places.append((start, 'Start menu'))

    for folder, label in places:
        if not os.path.isdir(folder):
            failed.append('%s (no such folder: %s)' % (label, folder))
            continue
        target = os.path.join(folder, 'Billing.lnk')
        try:
            make_lnk(target, exe, '"%s"' % here if here else '', HERE, ICON,
                     'Billing & Order Management')
            made.append(target)
        except OSError as e:
            failed.append('%s -- %s' % (label, e))

    if not made:
        say('Could not create a shortcut.\n\n' + '\n'.join(failed)
            + '\n\nYou can still start Billing by double-clicking run.bat '
              'in\n%s' % HERE, bad=True)
        return 1
    for path in made:
        print('Created %s' % path)
    for note in failed:
        print('Skipped %s' % note)
    print('Right-click the Start menu entry > Pin to Start, or drag the '
          'Desktop one to the taskbar.')
    return 0


# -------------------------------------------------------------------- setup

def have_reportlab():
    try:
        import reportlab                        # noqa: F401
        return True
    except ImportError:
        return False


# Folders that are copied to a server behind your back while you work,
# with the name to call each one when saying so.
SYNCED = (('onedrive', 'OneDrive'), ('dropbox', 'Dropbox'),
          ('google drive', 'Google Drive'), ('googledrive', 'Google Drive'),
          ('my drive', 'Google Drive'), ('icloud', 'iCloud Drive'),
          ('mobile documents', 'iCloud Drive'),
          ('creative cloud files', 'Creative Cloud'), ('box sync', 'Box'),
          ('pcloud', 'pCloud'), ('nextcloud', 'Nextcloud'),
          ('yandexdisk', 'Yandex Disk'), ('sync.com', 'Sync.com'),
          ('syncthing', 'Syncthing'))


def risky_location(folder):
    """Say why keeping the books here could lose them, or '' if it is fine.

    SQLite's write-ahead log is a second and third file beside the database,
    and the three have to stay in step. A sync client copies whichever one it
    notices first, and a network share can lose a lock without saying so. Both
    produce a database that looks fine for weeks and is unreadable the day you
    need last year's books. This is the single likeliest way to lose them on a
    Windows PC, so it is said out loud while the folder can still be moved.
    """
    # The UNC prefix is read off what was given, not off abspath, which only
    # recognises it as a path at all on Windows.
    raw = str(folder or '')
    if raw.startswith('\\\\') or raw.startswith('//'):
        return 'a network share'
    low = os.path.abspath(folder).replace('\\', '/').lower()
    for name, shown in SYNCED:
        if '/' + name in low or low.startswith(name):
            return shown
    # OneDrive relocates the whole Documents folder without renaming it, so
    # the path alone does not always show it. The environment does.
    for var in ('OneDrive', 'OneDriveCommercial', 'OneDriveConsumer'):
        root = os.environ.get(var)
        if root and low.startswith(os.path.abspath(root)
                                   .replace('\\', '/').lower() + '/'):
            return 'OneDrive'
    return ''


def setup():
    """Everything a new PC needs before the first bill: printing, the books,
    and a way to start it that is not a folder of files.

    Run again as often as you like -- each step checks before it acts, and the
    shortcuts are simply rewritten.
    """
    print('')
    print('  Setting Billing up on this machine')
    print('  ' + '-' * 46)
    print('  Python   %s' % sys.version.split()[0])
    print('  Folder   %s' % HERE)

    synced = risky_location(HERE)
    if synced:
        print('')
        print('  !! This folder is inside %s.' % synced)
        print('     Move the whole Billing folder onto the PC itself -- say')
        print('     C:\\Billing -- before you start entering bills. A synced')
        print('     or shared folder copies the database out from under')
        print('     SQLite mid-write, and the damage does not show up until')
        print('     the day you need an old year back.')
        print('')

    # 1. The printed stationery is the one thing that needs a library.
    if have_reportlab():
        print('  Printing ready')
    else:
        print('  Printing installing reportlab, which draws the invoice and')
        print('           challan stationery. Needs the internet; takes a minute.')
        quietly([sys.executable, '-m', 'pip', 'install', '--user', '--quiet',
                 '--disable-pip-version-check', 'reportlab'],
                capture_output=True)
        # pip installs into a folder this already-running interpreter may not
        # have on its path, so ask a fresh one rather than trusting the import
        # here, which would report a failure that is not real.
        fresh = quietly([sys.executable, '-c', 'import reportlab'],
                        capture_output=True)
        if fresh.returncode == 0:
            print('           installed')
        else:
            print('           could not install it. Everything except the')
            print('           printed PDFs will work. To try again later:')
            print('             %s -m pip install --user reportlab'
                  % os.path.basename(sys.executable))

    # 2. The books themselves, and the permissions on the folder holding them.
    sys.path.insert(0, os.path.join(HERE, 'app'))
    try:
        import db
        db.init()
        db.harden()
        print('  Books    %s' % db.DB_PATH)
    except Exception as e:                      # a broken install, a bad disk
        say('Billing could not prepare its database.\n\n%s' % e, bad=True)
        return 1

    # 3. Somewhere to click.
    if WINDOWS or sys.platform == 'darwin' or os.path.isdir(
            os.path.join(os.path.expanduser('~'), 'Desktop')):
        print('  Shortcut')
        shortcut()
    mark_setup_done()
    print('')
    return 0


def setup_mark():
    """The file that says setup has run on this computer. Named after the
    computer, so a folder copied from another one does not carry it over;
    Billing.exe looks for the same name (Environment.MachineName)."""
    name = os.environ.get('COMPUTERNAME') or socket.gethostname() or 'this'
    return os.path.join(HERE, 'app', 'data', '.setup-done-%s' % name.upper())


def mark_setup_done():
    try:
        with open(setup_mark(), 'w', encoding='utf-8') as fh:
            fh.write(time.strftime('%Y-%m-%d %H:%M:%S\n'))
    except OSError:
        pass


# --------------------------------------------------------------------- main

def set_passcode():
    """Choose the passcode at the computer itself.

    Anything reaching Billing from the network is asked for it, and it can
    only be chosen here -- whoever sits at this computer can read the books
    file anyway, so no old passcode is asked for.
    """
    import getpass
    sys.path.insert(0, os.path.join(HERE, 'app'))
    import auth
    import db
    try:
        first = getpass.getpass('New passcode (%d characters or more): '
                                % auth.MIN_LENGTH)
        again = getpass.getpass('The same again: ')
    except (EOFError, KeyboardInterrupt):
        print('\nNothing was changed.')
        return 1
    if first != again:
        say('The two did not match. Nothing was changed.', bad=True)
        return 1
    db.init()
    conn = db.connect()
    try:
        auth.set_passcode(conn, first, force=True)
        auth.end_all_sessions(conn)
    except ValueError as e:
        say(str(e), bad=True)
        return 1
    finally:
        conn.close()
    print('Passcode set. Phones and other PCs will ask for it; anyone signed '
          'in elsewhere has been signed out.')
    return 0


def set_delete_password():
    """Choose the delete password at the computer itself -- the way back
    when it is forgotten. No old one is asked for: whoever sits here can
    read the books file anyway."""
    import getpass
    sys.path.insert(0, os.path.join(HERE, 'app'))
    import auth
    import db
    try:
        first = getpass.getpass('New delete password (%d characters or more): '
                                % auth.DELETE_MIN)
        again = getpass.getpass('The same again: ')
    except (EOFError, KeyboardInterrupt):
        print('\nNothing was changed.')
        return 1
    if first != again:
        say('The two did not match. Nothing was changed.', bad=True)
        return 1
    db.init()
    conn = db.connect()
    try:
        auth.set_delete_password(conn, first, force=True)
    except ValueError as e:
        say(str(e), bad=True)
        return 1
    finally:
        conn.close()
    print('Delete password set. Deleting a challan, a bill or an account '
          'asks for it from now on.')
    return 0


def main():
    if sys.version_info < MIN_PYTHON:
        note = ('Billing needs Python %d.%d or newer -- this is %s.'
                % (MIN_PYTHON[0], MIN_PYTHON[1], sys.version.split()[0]))
        if WINDOWS:
            note += ('\n\nInstall it from https://www.python.org/downloads/ '
                     'and tick\n"Add python.exe to PATH" on the first screen '
                     'of the installer.')
        say(note, bad=True)
        return 1

    ap = argparse.ArgumentParser(
        description='Start Billing & Order Management.')
    ap.add_argument('--port', type=int,
                    default=int(os.environ.get('BILLING_PORT', DEFAULT_PORT)))
    ap.add_argument('--host', default=os.environ.get('BILLING_HOST',
                                                     '127.0.0.1'),
                    help='127.0.0.1 (default) keeps it on this machine')
    ap.add_argument('--set-passcode', action='store_true',
                    help='choose the passcode phones and other PCs are asked '
                         'for (run it in a console window)')
    ap.add_argument('--set-delete-password', action='store_true',
                    help='choose the password deleting a challan, bill or '
                         'account asks for -- also the way back if it is '
                         'forgotten (run it in a console window)')
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--tab', action='store_true',
                    default=os.environ.get('BILLING_TAB') == '1',
                    help='open in a tab of the everyday browser, not in a '
                         'window of its own')
    ap.add_argument('--stop', action='store_true')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--shortcut', action='store_true')
    ap.add_argument('--setup', action='store_true',
                    help='first run on a new machine: printing library, the '
                         'books folder, and a Desktop launcher')
    args = ap.parse_args()

    if WINDOWS:
        unmark()
        tidy()

    if args.stop:
        stopped = stop(args.port)
        close_windows()
        return stopped
    if args.status:
        return status(args.port)
    if args.setup:
        return setup()
    if args.shortcut:
        return shortcut()
    if args.set_passcode:
        return set_passcode()
    if args.set_delete_password:
        return set_delete_password()
    return start(args.port, args.host, not args.no_browser, args.tab)


if __name__ == '__main__':
    sys.exit(main())
