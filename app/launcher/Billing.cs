// Billing.exe -- the one thing to double-click on a Windows PC.
//
// Built on the PC itself by make_exe.bat, with the C# compiler every Windows
// 10 and 11 machine already carries as part of .NET Framework 4. Nothing is
// downloaded to build it, and a program made on the PC has no "came from the
// internet" mark for SmartScreen to stop it on.
//
// It holds none of the app. It finds a real Python, sets the PC up the first
// time, and has billing.py start the server (or find it already running).
// billing.py answers in a message box when anything goes wrong, since there
// is no console to read.
//
// **The screens are shown in Billing.exe's own window** (5 Oct 2026: "Don't
// run it on chrome or any other browser"). They are drawn by WebView2, the
// part of Windows 10 and 11 that programs such as Outlook and Teams draw
// their screens with -- Edge's engine, but inside this program: no browser
// opens, and the taskbar shows Billing. The three files it takes to reach
// WebView2 are Microsoft's, from its Microsoft.Web.WebView2 package
// (1.0.4258.31), kept in app\launcher\webview2 with their licence.
// make_exe.bat builds the window in (WEBVIEW) when they are there. Should the
// window not open on a PC -- no WebView2 there, or anything else -- Billing
// opens in the browser's app window as before, so it always opens.
//
// 6 Oct 2026, "The software isn't running on Windows" -- an error message on
// the PC. What could stop the window there and not on paper: a folder that
// came by download carries Windows' "came from the internet" mark, and .NET
// will not LoadFrom a marked file (UnsafeLoadFrom will); a message box opened
// behind other windows and held the browser's window back until it was
// answered; a second click while the first was still starting could stop the
// server the first had just started. Now one Billing.exe does the opening
// while any other waits for its window, the browser's window opens before
// anything is said, messages come up in front, and every step is written to
// %LOCALAPPDATA%\Billing\launch-log.txt. make_exe.bat runs
// "Billing.exe --window-check" after building, and builds it without its
// window if WebView2 does not draw on that PC.
//
// Written for the C# 5 compiler that ships with Windows: no string
// interpolation, no ?. and no => members, no await inside catch or finally.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
#if WEBVIEW
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
#endif

[assembly: AssemblyTitle("Billing")]
[assembly: AssemblyDescription("Starts Billing & Order Management")]
[assembly: AssemblyProduct("Billing & Order Management")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]
// Read by check.bat (as plain bytes): this Billing.exe knows --window-check.
[assembly: AssemblyInformationalVersion("1.0 billing-window-check")]

static class Launcher
{
    const string Title = "Billing";

    // Asked of every Python found: where it really is, whether the printing
    // library is there, and whether it is new enough. Single quotes only --
    // the whole line goes inside double quotes on the command line.
    const string Check =
        "import sys, importlib.util as u; print(sys.executable); " +
        "print(1 if u.find_spec('reportlab') else 0); " +
        "sys.exit(0 if sys.version_info[:2] >= (3, 8) else 3)";

    class Found
    {
        public string Console;      // python.exe, for setup in a window
        public string Windowless;   // pythonw.exe, for everything else
        public bool HasReportlab;
    }

    // Held for as long as this Billing.exe is opening Billing or showing it,
    // so a second click waits for this one's window instead of starting the
    // server a second time. A static field: a Mutex left to the garbage
    // collector would let go of it mid-way.
    static Mutex opening;

    [STAThread]
    static int Main(string[] args)
    {
        AppDomain.CurrentDomain.UnhandledException +=
            delegate(object sender, UnhandledExceptionEventArgs e)
            {
                Log("stopped by an error: " + e.ExceptionObject);
            };
        try
        {
            return Start(args);
        }
        catch (Exception e)
        {
            Log("failed: " + e);
            Tell("Billing could not be started.\n\n" + e.Message + Help(), true);
            return 1;
        }
    }

    static int Start(string[] args)
    {
        // Before any window: drawn sharp at 125% and 150%, not stretched.
        DpiAware();
        // No "There is no disk in the drive" box while Python is looked for
        // along the PATH -- a card reader's empty slot puts one up -- nor in
        // billing.py or the server, which inherit this.
        try { SetErrorMode(0x0001 | 0x8000); } catch (Exception) { }
        string home = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        Log("Billing.exe" + Forward(args) + "  in " + home);
        if (Has(args, "--window-check"))
            return WindowCheck(home);
        string script = Path.Combine(home, "billing.py");
        if (!File.Exists(script))
        {
            Tell("Billing.exe has to stay in the Billing folder, next to " +
                 "billing.py. It is now in\n\n" + home + "\n\nMove it back " +
                 "into that folder. To start Billing from somewhere else, " +
                 "make a shortcut to it instead.", true);
            return 1;
        }

        Found py = FindPython();
        if (py == null)
        {
            Log("no Python 3.8 or newer found");
            if (Ask("Python 3 is not installed on this PC, and Billing is " +
                    "written in it.\n\n" +
                    "1.  Download it from python.org (the big yellow button).\n" +
                    "2.  On the FIRST screen of the installer, tick\n" +
                    "        Add python.exe to PATH\n" +
                    "3.  Click Install Now, then open Billing again.\n\n" +
                    "Open the download page now?"))
                Open("https://www.python.org/downloads/");
            return 1;
        }
        Log("Python " + py.Console + (py.HasReportlab ? "" : "  (no reportlab)"));

        // The first time on a PC -- no books yet, or no printing library and
        // setup never run here -- setup runs in a window that can be read: it
        // installs reportlab, makes the books and puts Billing on the Desktop
        // and in the Start menu. It leaves .setup-done-<this computer> behind
        // (billing.py, setup_mark), so a PC with no internet is not shown the
        // window at every start. A failure keeps the window open until a key
        // is pressed.
        string data = Path.Combine(home, "app", "data");
        bool noBooks = !File.Exists(Path.Combine(data, "billing.db"));
        bool setUp = File.Exists(Path.Combine(data,
            ".setup-done-" + Environment.MachineName.ToUpperInvariant()));
        if ((noBooks || (!py.HasReportlab && !setUp)) && !Has(args, "--stop"))
        {
            Log("setting this PC up first");
            int code = RunInWindow(py.Console, Quote(script) + " --setup", home);
            Log("setup ended with " + code);
            if (code != 0)
                return code;
        }

        // Billing's own window -- unless what was asked is only billing.py's
        // to do: stopping it, a browser tab, setting up.
        if (!WindowWanted(args))
            return InBrowser(py, script, args, home);

        int port = PortOf(args);
        // Another Billing.exe already opening Billing, or showing it: its
        // window is brought to the front once it is up, and nothing is
        // started twice. Should that one end without a window, this one
        // carries on in its place.
        bool mine;
        opening = new Mutex(true, MutexName(port), out mine);
        if (!mine && WaitForOther(port))
            return 0;

        int up = RunAndWait(py.Windowless,
                            Quote(script) + " --no-browser" + Forward(args), home);
        if (up != 0)
            return up;                  // billing.py has said why
        string fault = TryWindow(home, port);
        if (fault == null)
        {
            Log("window closed");
            return 0;                   // the window was open, and is closed
        }

        // The browser's window first, so Billing is open whatever is said
        // next -- and a message nobody has seen yet holds nothing up.
        Log("own window did not open: " + fault);
        Let(opening);
        int opened = InBrowser(py, script, args, home);
        if (fault == Missing)
        {
            if (Ask("Billing shows its screens with Microsoft's WebView2, " +
                    "which is part of Windows 10 and 11 -- but is missing " +
                    "on this PC.\n\nIt is free from Microsoft: on the page, " +
                    "under \"Evergreen Bootstrapper\", click Download, run " +
                    "it, then open Billing again.\n\nUntil then Billing " +
                    "opens in a browser window.\n\nOpen the download page now?"))
                Open("https://developer.microsoft.com/microsoft-edge/webview2/");
        }
        else if (fault != NotBuilt && opened == 0)
        {
            Tell("Billing has opened in a browser window, because its own " +
                 "window could not open:\n\n" + fault + "\n\nDouble-click " +
                 "make_exe.bat in the Billing folder once -- it checks the " +
                 "window on this PC and builds Billing.exe to suit." + Help(), true);
        }
        return opened;
    }

    // billing.py, to start Billing (or find it running) and open it in the
    // browser's app window: no window of this program's own.
    static int InBrowser(Found py, string script, string[] args, string home)
    {
        ProcessStartInfo run = new ProcessStartInfo(py.Windowless,
                                                    Quote(script) + Forward(args));
        run.WorkingDirectory = home;
        run.UseShellExecute = false;
        run.CreateNoWindow = true;
        // So billing.py opens the browser and does not hand back to us.
        run.EnvironmentVariables["BILLING_FROM_EXE"] = "1";
        try
        {
            Process.Start(run);
            Log("handed to billing.py for the browser's window");
        }
        catch (Exception e)
        {
            Log("billing.py would not start: " + e);
            Tell("Billing could not be started.\n\n" + e.Message + Help(), true);
            return 1;
        }
        return 0;
    }

    // Wait for the other Billing.exe: true once its window has been brought
    // to the front; false when it ended without one and this one now holds
    // `opening` -- or after two and a half minutes, when this one opens
    // Billing anyway rather than leave the click unanswered.
    static bool WaitForOther(int port)
    {
        Log("another Billing.exe is opening Billing; waiting for its window");
        DateTime until = DateTime.Now.AddSeconds(150);
        while (DateTime.Now < until)
        {
            if (Bring())
            {
                Log("brought its window to the front");
                return true;
            }
            try
            {
                if (opening.WaitOne(500))
                    return false;
            }
            catch (AbandonedMutexException)
            {
                return false;           // it ended without letting go: ours now
            }
        }
        Log("no window from the other one in 150 s; opening Billing here");
        return false;
    }

    static void Let(Mutex m)
    {
        try { m.ReleaseMutex(); }
        catch (Exception) { }           // not held after all
    }

    // ------------------------------------------------------ its own window

    internal const string Missing = "missing";
    internal const string NotBuilt = "not built";

    // What only billing.py does, in a console or not at all, goes straight
    // to it. --port, --lan and --host still open the window.
    static bool WindowWanted(string[] args)
    {
        string[] theirs = { "--stop", "--status", "--tab", "--setup",
                            "--shortcut", "--set-passcode",
                            "--set-delete-password", "--no-browser",
                            "--help", "-h" };
        foreach (string a in theirs)
            if (Has(args, a))
                return false;
        return true;
    }

    // The port billing.py will be on: --port N or --port=N, else
    // BILLING_PORT, else 8800 -- as billing.py itself decides.
    static int PortOf(string[] args)
    {
        int n;
        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i];
            if (a == "--port" && i + 1 < args.Length && int.TryParse(args[i + 1], out n))
                return n;
            if (a.StartsWith("--port=") && int.TryParse(a.Substring(7), out n))
                return n;
        }
        if (int.TryParse(Environment.GetEnvironmentVariable("BILLING_PORT") ?? "", out n))
            return n;
        return 8800;
    }

    static string MutexName(int port)
    {
        return "Local\\Billing.Window." + port;
    }

    // Is another Billing.exe's window up? Then it is brought to the front.
    static bool Bring()
    {
        Process me = Process.GetCurrentProcess();
        foreach (Process p in Process.GetProcessesByName(me.ProcessName))
        {
            try
            {
                IntPtr h = p.MainWindowHandle;
                if (p.Id == me.Id || h == IntPtr.Zero)
                    continue;
                if (IsIconic(h))
                    ShowWindow(h, 9);                       // SW_RESTORE
                SetForegroundWindow(h);
                return true;
            }
            catch (Exception) { }       // another person's, or just gone
        }
        return false;   // its window is not up yet, or is going: open ours
    }

    // billing.py, waited for: it starts the server, or finds it running.
    static int RunAndWait(string exe, string arguments, string dir)
    {
        ProcessStartInfo psi = new ProcessStartInfo(exe, arguments);
        psi.WorkingDirectory = dir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.EnvironmentVariables["BILLING_FROM_EXE"] = "1";
        try
        {
            Process p = Process.Start(psi);
            if (!p.WaitForExit(120000))
            {
                Log("billing.py still starting after 120 s");
                Tell("Billing is taking unusually long to start. Try again " +
                     "in a minute." + Help(), true);
                return 1;
            }
            Log("billing.py --no-browser ended with " + p.ExitCode);
            return p.ExitCode;
        }
        catch (Exception e)
        {
            Log("billing.py would not start: " + e);
            Tell("Billing could not be started.\n\n" + e.Message + Help(), true);
            return 1;
        }
    }

    // The window. Its code needs Microsoft's files, which are found only
    // once Resolve is in place -- so it sits behind a call of its own, and
    // nothing above names it.
    static string TryWindow(string home, int port)
    {
#if WEBVIEW
        windowHome = home;
        windowPort = port;
        AppDomain.CurrentDomain.AssemblyResolve += Resolve;
        try
        {
            return OpenWindow();
        }
        catch (Exception e)
        {
            return Explain(e);
        }
#else
        return NotBuilt;
#endif
    }

    // "Billing.exe --window-check", run by make_exe.bat and check.bat: open
    // a window off the screen for a moment and see WebView2 draw a page in
    // it. 0 it does; 2 WebView2 is not installed; 3 anything else went wrong;
    // 4 no answer in time; 5 built without the window. What was found is
    // also written to %LOCALAPPDATA%\Billing\window-check.txt, to be shown.
    static int WindowCheck(string home)
    {
        string said;
        int code;
#if WEBVIEW
        windowHome = home;
        AppDomain.CurrentDomain.AssemblyResolve += Resolve;
        try
        {
            code = CheckWindow(out said);
        }
        catch (Exception e)
        {
            code = 3;
            said = Explain(e);
        }
#else
        code = 5;
        said = "Billing.exe was built without its own window.";
#endif
        Log("window check: " + code + "  " + said);
        try
        {
            Directory.CreateDirectory(LocalFolder());
            File.WriteAllText(Path.Combine(LocalFolder(), "window-check.txt"),
                              said + Environment.NewLine);
        }
        catch (Exception) { }
        return code;
    }

    // An exception as one readable line, with what lay under it -- the inner
    // exception is often the part that says why.
    internal static string Explain(Exception e)
    {
        string text = e.GetType().Name + ": " + e.Message;
        for (Exception inner = e.InnerException; inner != null; inner = inner.InnerException)
            text += "\n  " + inner.GetType().Name + ": " + inner.Message;
        return text;
    }

#if WEBVIEW
    static int windowPort;
    static string windowHome;

    static string WebViewDir()
    {
        return Path.Combine(Path.Combine(Path.Combine(windowHome, "app"), "launcher"), "webview2");
    }

    // UnsafeLoadFrom, not LoadFrom. A Billing folder that came by download,
    // e-mail or WhatsApp carries Windows' "came from the internet" mark on
    // every file in it, and .NET Framework refuses to LoadFrom a marked
    // assembly ("Operation is not supported", 0x80131515). These three are
    // Microsoft's own, shipped in the folder on purpose.
    static Assembly Resolve(object sender, ResolveEventArgs e)
    {
        string name = new AssemblyName(e.Name).Name;
        string path = Path.Combine(WebViewDir(), name + ".dll");
        return File.Exists(path) ? Assembly.UnsafeLoadFrom(path) : null;
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    static string OpenWindow()
    {
        return BillingWindow.Run(WebViewDir(), windowPort);
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    static int CheckWindow(out string said)
    {
        return BillingWindow.Check(WebViewDir(), out said);
    }
#endif

    // -------------------------------------------------------------- Win32

    [DllImport("user32.dll")]
    static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")]
    static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")]
    static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")]
    static extern bool IsIconic(IntPtr hWnd);
    [DllImport("kernel32.dll")]
    static extern uint SetErrorMode(uint mode);

    static void DpiAware()
    {
        try
        {
            // Per monitor, version 2 (Windows 10 1703 and later).
            if (SetProcessDpiAwarenessContext(new IntPtr(-4)))
                return;
        }
        catch (Exception) { }
        try { SetProcessDPIAware(); } catch (Exception) { }
    }

    // ------------------------------------------------------ finding Python

    static Found FindPython()
    {
        HashSet<string> tried = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        // 1. The py launcher, which knows every Python installed.
        Found f = Probe("py", "-3 ", tried);
        if (f != null)
            return f;

        // 2. python.exe on the PATH -- but never the stub in WindowsApps,
        //    which opens the Microsoft Store instead of running anything.
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string entry in path.Split(';'))
        {
            string dir = entry.Trim().Trim('"');
            if (dir.Length == 0 ||
                dir.IndexOf("WindowsApps", StringComparison.OrdinalIgnoreCase) >= 0)
                continue;
            string exe;
            try { exe = Path.Combine(dir, "python.exe"); }
            catch (ArgumentException) { continue; }
            if (!File.Exists(exe))
                continue;
            f = Probe(exe, "", tried);
            if (f != null)
                return f;
        }

        // 3. Where the python.org installer puts it when the PATH box was not
        //    ticked -- which is most of the time. Newest first.
        string local = Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? "";
        string[] roots = {
            local.Length > 0 ? Path.Combine(local, "Programs", "Python") : "",
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86)
        };
        foreach (string root in roots)
        {
            if (root.Length == 0 || !Directory.Exists(root))
                continue;
            List<string> dirs = new List<string>();
            try { dirs.AddRange(Directory.GetDirectories(root, "Python3*")); }
            catch (Exception) { continue; }
            dirs.Sort(delegate(string a, string b) { return MinorOf(b).CompareTo(MinorOf(a)); });
            foreach (string dir in dirs)
            {
                string exe = Path.Combine(dir, "python.exe");
                if (!File.Exists(exe))
                    continue;
                f = Probe(exe, "", tried);
                if (f != null)
                    return f;
            }
        }
        return null;
    }

    // "Python312" -> 12, "Python39" -> 9, "Python3.11" -> 11.
    static int MinorOf(string dir)
    {
        string name = Path.GetFileName(dir) ?? "";
        string digits = "";
        for (int i = Math.Min(7, name.Length); i < name.Length; i++)
        {
            char c = name[i];
            if (char.IsDigit(c))
                digits += c;
            else if (digits.Length > 0)
                break;
        }
        int n;
        return int.TryParse(digits, out n) ? n : 0;
    }

    static Found Probe(string exe, string prefix, HashSet<string> tried)
    {
        if (!tried.Add(exe))
            return null;
        string output;
        if (Capture(exe, prefix + "-c \"" + Check + "\"", out output) != 0)
            return null;
        string[] lines = output.Split(new char[] { '\r', '\n' },
                                      StringSplitOptions.RemoveEmptyEntries);
        if (lines.Length < 2)
            return null;
        string console = lines[0].Trim();
        if (!File.Exists(console))
            return null;
        string quiet = Path.Combine(Path.GetDirectoryName(console), "pythonw.exe");
        Found found = new Found();
        found.Console = console;
        found.Windowless = File.Exists(quiet) ? quiet : console;
        found.HasReportlab = lines[1].Trim() == "1";
        return found;
    }

    // Run a program without a window and answer its exit code, or -1 if it
    // could not be run or did not finish in time.
    static int Capture(string exe, string arguments, out string output)
    {
        output = "";
        ProcessStartInfo psi = new ProcessStartInfo(exe, arguments);
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = Encoding.UTF8;
        // A path with a letter outside the PC's code page must come back
        // whole, or the Python it names would not be found.
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        StringBuilder text = new StringBuilder();
        Process p;
        try
        {
            p = Process.Start(psi);
        }
        catch (Win32Exception) { return -1; }
        catch (InvalidOperationException) { return -1; }
        if (p == null)
            return -1;
        p.OutputDataReceived += delegate(object s, DataReceivedEventArgs e)
        {
            if (e.Data != null)
                lock (text) { text.AppendLine(e.Data); }
        };
        p.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e) { };
        p.BeginOutputReadLine();
        p.BeginErrorReadLine();
        if (!p.WaitForExit(30000))
        {
            try { p.Kill(); } catch (Exception) { }
            return -1;
        }
        p.WaitForExit();            // lets the last lines of output arrive
        lock (text) { output = text.ToString(); }
        return p.ExitCode;
    }

    // -------------------------------------------------------------- helpers

    // A console window the user can read. Should setup fail, the window stays
    // open until a key is pressed and the exit code says so.
    static int RunInWindow(string exe, string arguments, string dir)
    {
        string cmd = Environment.GetEnvironmentVariable("ComSpec");
        if (string.IsNullOrEmpty(cmd))
            cmd = "cmd.exe";
        // cmd /c drops the first and last quote of what follows, so the whole
        // line is wrapped in one more pair.
        ProcessStartInfo psi = new ProcessStartInfo(cmd,
            "/c \"" + Quote(exe) + " " + arguments + " || (pause & exit /b 1)\"");
        psi.WorkingDirectory = dir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = false;
        try
        {
            Process p = Process.Start(psi);
            p.WaitForExit();
            return p.ExitCode;
        }
        catch (Exception e)
        {
            Tell("Setting Billing up could not be started.\n\n" + e.Message, true);
            return 1;
        }
    }

    static string Quote(string s)
    {
        return "\"" + s + "\"";
    }

    // Whatever Billing.exe was given goes on to billing.py: --stop, --lan ...
    static string Forward(string[] args)
    {
        StringBuilder sb = new StringBuilder();
        foreach (string a in args)
            sb.Append(' ').Append(Quote(a.Replace("\"", "")));
        return sb.ToString();
    }

    static bool Has(string[] args, string wanted)
    {
        foreach (string a in args)
            if (string.Equals(a, wanted, StringComparison.OrdinalIgnoreCase))
                return true;
        return false;
    }

    // Messages come up in front of every other window. Billing.exe has no
    // window of its own while it starts, and a box opened behind SHREE or
    // the browser waited there, unseen, holding Billing back until answered.
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr owner, string text, string caption, uint type);

    const uint MbYesNo = 0x4, MbError = 0x10, MbWarning = 0x30, MbInfo = 0x40,
               MbForeground = 0x10000, MbTopmost = 0x40000;

    static void Tell(string text, bool bad)
    {
        Log("said: " + text.Replace("\n", " "));
        MessageBoxW(IntPtr.Zero, text, Title,
                    (bad ? MbError : MbInfo) | MbForeground | MbTopmost);
    }

    static bool Ask(string text)
    {
        Log("asked: " + text.Replace("\n", " "));
        return MessageBoxW(IntPtr.Zero, text, Title,
                           MbYesNo | MbWarning | MbForeground | MbTopmost) == 6;   // IDYES
    }

    static string Help()
    {
        return "\n\nIf it says this again, double-click check.bat in the " +
               "Billing folder and send what it shows.";
    }

    // Where Billing keeps what is this person's own, outside the Billing
    // folder: the window's place and its engine's files, and this log.
    internal static string LocalFolder()
    {
        string local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (string.IsNullOrEmpty(local))
            local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return Path.Combine(local, "Billing");
    }

    // Every step of opening Billing, one line each, so a PC that will not
    // open it can say where it stopped (check.bat shows the last lines).
    internal static void Log(string line)
    {
        try
        {
            string folder = LocalFolder();
            Directory.CreateDirectory(folder);
            string file = Path.Combine(folder, "launch-log.txt");
            FileInfo info = new FileInfo(file);
            if (info.Exists && info.Length > 256 * 1024)
                File.Copy(file, file + ".old", true);
            if (info.Exists && info.Length > 256 * 1024)
                File.Delete(file);
            File.AppendAllText(file, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") +
                               "  [" + Process.GetCurrentProcess().Id + "] " + line +
                               Environment.NewLine);
        }
        catch (Exception) { }           // never the reason Billing does not open
    }

    static void Open(string url)
    {
        try { Process.Start(url); } catch (Exception) { }
    }
}

#if WEBVIEW
// ============================================================ the window
//
// One Form with Microsoft's WebView2 control filling it, on the app's own
// address. What would otherwise make it look like a browser is turned off:
// the address that shows at the foot as the pointer passes a link, the
// browser's own suggestions in the boxes (the app has its own lists), its
// developer tools. A page the app opens itself -- a PDF -- gets a window of
// the same kind; anything elsewhere (WhatsApp) goes to the PC's browser,
// since that is a website and not Billing.
static class BillingWindow
{
    public static CoreWebView2Environment Env;
    public static int Port;

    public static string Folder()
    {
        return Launcher.LocalFolder();
    }

    // The engine keeps its own files (its cache, the screens remembered)
    // per person, never in the Billing folder.
    public static string DataFolder()
    {
        return Path.Combine(Folder(), "webview2");
    }

    public static string Address()
    {
        return "http://127.0.0.1:" + Port + "/";
    }

    // Null once the window has been open and is closed; else why it could not.
    // Only one Billing.exe gets this far at a time (Launcher.opening).
    public static string Run(string dir, int port)
    {
        Port = port;
        string version = Version(dir);
        Launcher.Log("WebView2 " + (version ?? "not installed"));
        if (version == null)
            return Launcher.Missing;
        Prepare();
        Page main = new Page(true);
        main.Shown += async delegate { await main.Begin(Address()); };
        Application.Run(main);
        return main.Fault;
    }

    // The WebView2 installed on this PC, or null when there is none.
    static string Version(string dir)
    {
        CoreWebView2Environment.SetLoaderDllFolderPath(Path.Combine(dir, Arch()));
        try
        {
            string v = CoreWebView2Environment.GetAvailableBrowserVersionString(null);
            return string.IsNullOrEmpty(v) ? null : v;
        }
        catch (WebView2RuntimeNotFoundException)
        {
            return null;
        }
    }

    static void Prepare()
    {
        // Anything unforeseen is written down, not put up as the .NET
        // "unhandled exception" box: %LOCALAPPDATA%\Billing\window-errors.txt
        try { Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException); }
        catch (InvalidOperationException) { }
        Application.ThreadException += delegate(object sender, ThreadExceptionEventArgs e)
        {
            Note(e.Exception);
        };
        Application.EnableVisualStyles();
        try { Application.SetCompatibleTextRenderingDefault(false); }
        catch (InvalidOperationException) { }    // a window came first
    }

    // --window-check: the same steps as the real window, in one off the
    // screen, on a page of its own rather than the app's -- so it says
    // whether this PC can show Billing's window, server running or not.
    public static int Check(string dir, out string said)
    {
        string version = Version(dir);
        if (version == null)
        {
            said = "Microsoft's WebView2 is not installed on this PC.";
            return 2;
        }
        Prepare();
        Trial trial = new Trial();
        Application.Run(trial);
        said = trial.Said + "  (WebView2 " + version + ")";
        return trial.Code;
    }

    public static void Note(Exception e)
    {
        try
        {
            Directory.CreateDirectory(Folder());
            File.AppendAllText(Path.Combine(Folder(), "window-errors.txt"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + e + Environment.NewLine);
        }
        catch (Exception) { }
    }

    // Which of Microsoft's three loaders this process can use.
    static string Arch()
    {
        string a = (Environment.GetEnvironmentVariable("PROCESSOR_ARCHITECTURE") ?? "")
            .ToUpperInvariant();
        if (a == "ARM64")
            return "arm64";
        return IntPtr.Size == 8 ? "x64" : "x86";
    }

    public static bool Ours(string uri)
    {
        if (string.IsNullOrEmpty(uri))
            return true;
        string u = uri.ToLowerInvariant();
        return u.StartsWith("http://127.0.0.1:" + Port + "/")
            || u.StartsWith("http://localhost:" + Port + "/")
            || u.StartsWith("about:") || u.StartsWith("data:") || u.StartsWith("blob:");
    }

    // A website -- WhatsApp's -- in the PC's own browser.
    public static void Outside(string uri)
    {
        string u = (uri ?? "").ToLowerInvariant();
        if (!(u.StartsWith("https://") || u.StartsWith("http://") || u.StartsWith("mailto:")))
            return;
        try { Process.Start(uri); } catch (Exception) { }
    }

    public static bool Answering()
    {
        try
        {
            using (System.Net.Sockets.TcpClient c = new System.Net.Sockets.TcpClient())
            {
                IAsyncResult r = c.BeginConnect("127.0.0.1", Port, null, null);
                if (!r.AsyncWaitHandle.WaitOne(1500))
                    return false;
                c.EndConnect(r);
                return c.Connected;
            }
        }
        catch (Exception)
        {
            return false;
        }
    }
}

class Page : Form
{
    public string Fault;
    readonly bool main;
    readonly WebView2 view;
    bool arrived;

    public Page(bool main)
    {
        this.main = main;
        Text = "Billing";
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
        catch (Exception) { }
        view = new WebView2();
        view.Dock = DockStyle.Fill;
        view.DefaultBackgroundColor = Color.White;
        Controls.Add(view);
        if (main)
        {
            Place.Restore(this);
            FormClosing += delegate { Place.Save(this, view); };
        }
        else
        {
            StartPosition = FormStartPosition.WindowsDefaultLocation;
            Size = new Size(1100, 820);
        }
    }

    public CoreWebView2 Core
    {
        get { return view.CoreWebView2; }
    }

    public async Task Ready()
    {
        if (BillingWindow.Env == null)
            BillingWindow.Env = await CoreWebView2Environment.CreateAsync(
                null, BillingWindow.DataFolder(), null);
        await view.EnsureCoreWebView2Async(BillingWindow.Env);
        Configure(view.CoreWebView2);
    }

    // The main window: set up, then the app. A failure closes it and says
    // why, and Billing opens in the browser instead.
    public async Task Begin(string url)
    {
        string fault = null;
        try
        {
            // A window left white for ever is worse than the browser's: the
            // engine gets 45 seconds to start, which it does in one or two.
            Task ready = Ready();
            if (await Task.WhenAny(ready, Task.Delay(45000)) != ready)
                throw new TimeoutException("Microsoft's WebView2 did not start " +
                                           "within 45 seconds.");
            await ready;
            view.ZoomFactor = Place.Zoom;
            view.ZoomFactorChanged += delegate { Place.Zoom = view.ZoomFactor; };
            view.CoreWebView2.Navigate(url);
            Launcher.Log("window open on " + url);
        }
        catch (Exception e)
        {
            fault = Launcher.Explain(e);
        }
        if (fault != null)
        {
            Fault = fault;
            Close();
        }
    }

    void Configure(CoreWebView2 core)
    {
        CoreWebView2Settings s = core.Settings;
        s.IsStatusBarEnabled = false;
        s.AreDevToolsEnabled = false;
        s.IsGeneralAutofillEnabled = false;
        s.IsPasswordAutosaveEnabled = false;

        core.DocumentTitleChanged += delegate
        {
            string t = core.DocumentTitle;
            Text = string.IsNullOrEmpty(t) || t.StartsWith("http") || t.StartsWith("data:")
                ? "Billing" : t;
        };
        core.WindowCloseRequested += delegate { Close(); };

        core.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs e)
        {
            if (BillingWindow.Ours(e.Uri))
                return;
            e.Cancel = true;
            BillingWindow.Outside(e.Uri);
        };

        // The server not answering is said in Billing's words, not the
        // engine's. Anything else that "fails" -- a download, a cancelled
        // link -- is left alone: the server answering is what is checked.
        core.NavigationCompleted += delegate(object sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            if (e.IsSuccess)
            {
                if (!arrived)
                {
                    arrived = true;
                    view.Focus();
                }
                return;
            }
            if (main && !BillingWindow.Answering())
                core.NavigateToString(NotRunning());
        };

        core.NewWindowRequested += async delegate(object sender, CoreWebView2NewWindowRequestedEventArgs e)
        {
            if (!BillingWindow.Ours(e.Uri))
            {
                e.Handled = true;
                BillingWindow.Outside(e.Uri);
                return;
            }
            CoreWebView2Deferral wait = e.GetDeferral();
            Page p = null;
            try
            {
                p = new Page(false);
                p.Show();
                await p.Ready();
                e.NewWindow = p.Core;
                e.Handled = true;
            }
            catch (Exception)
            {
                // WebView2 opens it in a window of its own instead.
                if (p != null)
                    p.Close();
            }
            finally
            {
                wait.Complete();
            }
        };

        core.ProcessFailed += delegate(object sender, CoreWebView2ProcessFailedEventArgs e)
        {
            if (e.ProcessFailedKind == CoreWebView2ProcessFailedKind.BrowserProcessExited)
            {
                BeginInvoke((MethodInvoker)delegate
                {
                    MessageBox.Show(this, "Billing's window stopped working. Open Billing " +
                                    "again from its icon -- nothing saved is lost.",
                                    "Billing", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    Close();
                });
                return;
            }
            // The page itself went: drawn again. (A page that is only busy --
            // a long list on a slow PC -- is left to finish.)
            if (e.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessExited)
                BeginInvoke((MethodInvoker)delegate
                {
                    try { core.Reload(); } catch (Exception) { }
                });
        };
    }

    static string NotRunning()
    {
        return "<!doctype html><html><head><meta charset=\"utf-8\"><title>Billing</title></head>" +
            "<body style=\"font:16px 'Segoe UI',sans-serif;margin:64px;color:#1f2937\">" +
            "<h2 style=\"font-weight:600;margin:0 0 12px\">Billing is not running</h2>" +
            "<p>Close this window and open Billing again from its icon on the Desktop " +
            "or in the Start menu.</p>" +
            "<p><a href=\"" + BillingWindow.Address() + "\">Try again</a></p></body></html>";
    }
}

// The window --window-check opens: off the screen, out of the taskbar, gone
// in a second or two. It answers 0 once a page has been drawn in it.
class Trial : Form
{
    public int Code = 4;
    public string Said = "WebView2 did not draw a page within 45 seconds.";
    readonly WebView2 view = new WebView2();

    public Trial()
    {
        Text = "Billing";
        ShowInTaskbar = false;
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        Bounds = new Rectangle(-30000, -30000, 320, 240);
        view.Dock = DockStyle.Fill;
        Controls.Add(view);
        Shown += async delegate
        {
            await Go();
            Close();
        };
    }

    async Task Go()
    {
        try
        {
            TaskCompletionSource<bool> drawn = new TaskCompletionSource<bool>();
            Task all = Steps(drawn);
            if (await Task.WhenAny(all, Task.Delay(45000)) != all)
                return;                 // Code 4: no answer in time
            await all;
            if (await drawn.Task)
            {
                Code = 0;
                Said = "Billing's own window opens on this PC.";
            }
            else
            {
                Code = 3;
                Said = "WebView2 started but could not draw a page.";
            }
        }
        catch (Exception e)
        {
            Code = 3;
            Said = Launcher.Explain(e);
        }
    }

    async Task Steps(TaskCompletionSource<bool> drawn)
    {
        CoreWebView2Environment env = await CoreWebView2Environment.CreateAsync(
            null, BillingWindow.DataFolder(), null);
        await view.EnsureCoreWebView2Async(env);
        view.CoreWebView2.NavigationCompleted +=
            delegate(object sender, CoreWebView2NavigationCompletedEventArgs e)
            {
                drawn.TrySetResult(e.IsSuccess);
            };
        view.CoreWebView2.NavigateToString(
            "<!doctype html><title>Billing</title><p>Billing window check</p>");
        await drawn.Task;
    }
}

// Where the window was left, and how far it was zoomed: opened the same
// way next time. Full screen the first time.
static class Place
{
    public static double Zoom = 1.0;

    static string Where()
    {
        return Path.Combine(BillingWindow.Folder(), "window.txt");
    }

    public static void Restore(Form f)
    {
        try
        {
            string[] p = File.ReadAllText(Where()).Trim().Split(' ');
            System.Globalization.CultureInfo inv = System.Globalization.CultureInfo.InvariantCulture;
            Rectangle r = new Rectangle(int.Parse(p[0], inv), int.Parse(p[1], inv),
                                        int.Parse(p[2], inv), int.Parse(p[3], inv));
            bool max = p[4] == "1";
            double z = double.Parse(p[5], inv);
            bool seen = false;
            foreach (Screen sc in Screen.AllScreens)
                if (sc.WorkingArea.IntersectsWith(r))
                    seen = true;
            if (seen && r.Width >= 400 && r.Height >= 300)
            {
                f.StartPosition = FormStartPosition.Manual;
                f.Bounds = r;
                f.WindowState = max ? FormWindowState.Maximized : FormWindowState.Normal;
                if (z >= 0.25 && z <= 5)
                    Zoom = z;
                return;
            }
        }
        catch (Exception) { }
        f.StartPosition = FormStartPosition.WindowsDefaultLocation;
        f.Size = new Size(1280, 800);
        f.WindowState = FormWindowState.Maximized;
    }

    public static void Save(Form f, WebView2 v)
    {
        try
        {
            Rectangle r = f.WindowState == FormWindowState.Normal ? f.Bounds : f.RestoreBounds;
            System.Globalization.CultureInfo inv = System.Globalization.CultureInfo.InvariantCulture;
            Directory.CreateDirectory(BillingWindow.Folder());
            File.WriteAllText(Where(), string.Join(" ", new string[] {
                r.X.ToString(inv), r.Y.ToString(inv), r.Width.ToString(inv), r.Height.ToString(inv),
                f.WindowState == FormWindowState.Maximized ? "1" : "0",
                Zoom.ToString("0.###", inv) }));
        }
        catch (Exception) { }
    }
}
#endif
