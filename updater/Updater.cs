// DiscTray installer and updater, per user (no admin rights, nothing outside your profile).
//
// Downloads the latest release from GitHub, installs it to
// %LOCALAPPDATA%\Programs\DiscTray, adds a Start Menu shortcut and an entry under
// Settings > Apps for this user only, then starts DiscTray.
//
//     DiscTrayUpdater.exe               install, or update to the newest release
//     DiscTrayUpdater.exe --from-app    the same, started from DiscTray's tray menu
//     DiscTrayUpdater.exe --uninstall   remove DiscTray for this user
//
// A small native .NET Framework program (built by build.py with the csc.exe that ships with
// Windows), so there's no bundled runtime unpacking itself into %TEMP% for antivirus to trip on.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.IO.Pipes;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

static class Setup
{
    public const string Repo = "ASQUALL-hub/disctray";
    public const string App = "DiscTray";
    public const string AssetSuffix = "-win64.zip";
    public const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + App;
    public const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";

    public static readonly bool TestMode;   // tests: files only, no shortcut/registry
    public static readonly string InstallDir, Exe, UpdaterExe, VersionFile, Shortcut, SettingsDir, Pipe, Self;

    static Setup()
    {
        string over = Environment.GetEnvironmentVariable("DISCTRAY_INSTALL_DIR");
        TestMode = !string.IsNullOrEmpty(over);
        InstallDir = Path.GetFullPath(TestMode ? over : Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", App)).TrimEnd('\\');
        Exe = Path.Combine(InstallDir, "DiscTray.exe");
        UpdaterExe = Path.Combine(InstallDir, "DiscTrayUpdater.exe");
        VersionFile = Path.Combine(InstallDir, "version.txt");
        Shortcut = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), App + ".lnk");
        SettingsDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), App);
        Pipe = App + "-" + (Environment.GetEnvironmentVariable("USERNAME") ?? "user");   // DiscTray's single-instance server
        Self = Path.GetFullPath(Assembly.GetExecutingAssembly().Location);
    }

    public static bool SamePath(string a, string b)
    {
        return string.Equals(Path.GetFullPath(a).TrimEnd('\\'), Path.GetFullPath(b).TrimEnd('\\'),
                             StringComparison.OrdinalIgnoreCase);
    }

    public static bool RunningFromInstall()
    {
        return Self.StartsWith(InstallDir + "\\", StringComparison.OrdinalIgnoreCase);
    }

    // ----------------------------------------------------------------------- GitHub

    public static int Compare(string a, string b)
    {
        string[] pa = a.Trim().TrimStart('v', 'V').Split('.'), pb = b.Trim().TrimStart('v', 'V').Split('.');
        for (int i = 0; i < Math.Max(pa.Length, pb.Length); i++)
        {
            int x = i < pa.Length ? Digits(pa[i]) : 0, y = i < pb.Length ? Digits(pb[i]) : 0;
            if (x != y) return x.CompareTo(y);
        }
        return 0;
    }

    static int Digits(string piece)
    {
        var sb = new StringBuilder();
        foreach (char ch in piece) if (char.IsDigit(ch)) sb.Append(ch);
        int n;
        return int.TryParse(sb.ToString(), out n) ? n : 0;
    }

    /// <summary>Version, download url and size in bytes of the newest release's app zip.</summary>
    public static void LatestRelease(out string version, out string url, out long size)
    {
        var req = (HttpWebRequest)WebRequest.Create("https://api.github.com/repos/" + Repo + "/releases/latest");
        req.Accept = "application/vnd.github+json";
        req.UserAgent = App + "-updater";
        req.Timeout = 20000;
        string body;
        using (var r = req.GetResponse())
        using (var s = new StreamReader(r.GetResponseStream(), Encoding.UTF8))
            body = s.ReadToEnd();
        var json = new JavaScriptSerializer();
        json.MaxJsonLength = int.MaxValue;
        var data = (Dictionary<string, object>)json.DeserializeObject(body);
        object assets;
        if (data.TryGetValue("assets", out assets) && assets is object[])
        {
            foreach (object o in (object[])assets)
            {
                var a = (Dictionary<string, object>)o;
                if (((string)a["name"]).EndsWith(AssetSuffix, StringComparison.OrdinalIgnoreCase))
                {
                    version = ((string)data["tag_name"]).TrimStart('v', 'V');
                    url = (string)a["browser_download_url"];
                    size = a.ContainsKey("size") && a["size"] != null ? Convert.ToInt64(a["size"]) : 0;
                    return;
                }
            }
        }
        throw new Exception("The latest release has no Windows build attached.");
    }

    public static string InstalledVersion()
    {
        try { return File.Exists(Exe) ? File.ReadAllText(VersionFile, Encoding.UTF8).Trim() : null; }
        catch (IOException) { return null; }
        catch (UnauthorizedAccessException) { return null; }
    }

    public static void Download(string url, string dest, Action<long, long> progress)
    {
        var req = (HttpWebRequest)WebRequest.Create(url);
        req.UserAgent = App + "-updater";
        req.Timeout = 30000;
        req.ReadWriteTimeout = 30000;
        using (var r = req.GetResponse())
        using (var s = r.GetResponseStream())
        using (var f = File.Create(dest))
        {
            long total = r.ContentLength > 0 ? r.ContentLength : 0, done = 0;
            var buf = new byte[256 * 1024];
            int n;
            while ((n = s.Read(buf, 0, buf.Length)) > 0)
            {
                f.Write(buf, 0, n);
                done += n;
                progress(done, total);
            }
        }
    }

    // ----------------------------------------------------------------------- running copies

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, EntryPoint = "QueryFullProcessImageNameW", SetLastError = true)]
    static extern bool QueryFullProcessImageName(IntPtr h, uint flags, StringBuilder name, ref uint size);
    [DllImport("kernel32.dll")]
    static extern bool TerminateProcess(IntPtr h, uint code);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr h);
    const uint PROCESS_TERMINATE = 0x0001, PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;

    /// <summary>Handles of processes whose exe lives in the install folder, except us.</summary>
    static List<IntPtr> InstalledProcesses()
    {
        var found = new List<IntPtr>();
        int me = Process.GetCurrentProcess().Id;
        string root = InstallDir + "\\";
        foreach (Process p in Process.GetProcesses())
        {
            int pid = p.Id;
            p.Dispose();
            if (pid == 0 || pid == me) continue;
            IntPtr h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE, false, pid);
            if (h == IntPtr.Zero) continue;
            var buf = new StringBuilder(1024);
            uint size = 1024;
            if (QueryFullProcessImageName(h, 0, buf, ref size) &&
                buf.ToString().StartsWith(root, StringComparison.OrdinalIgnoreCase))
                found.Add(h);
            else
                CloseHandle(h);
        }
        return found;
    }

    /// <summary>Ask DiscTray to quit (it saves its settings), and only force it if it won't.</summary>
    public static void CloseRunning()
    {
        try
        {
            using (var pipe = new NamedPipeClientStream(".", Pipe, PipeDirection.Out))
            {
                pipe.Connect(1000);
                byte[] msg = Encoding.ASCII.GetBytes("quit");
                pipe.Write(msg, 0, msg.Length);
            }
        }
        catch (Exception) { }   // not running (or an old build without the quit command)
        var end = DateTime.UtcNow.AddSeconds(6);
        while (DateTime.UtcNow < end)
        {
            var procs = InstalledProcesses();
            foreach (IntPtr h in procs) CloseHandle(h);
            if (procs.Count == 0) return;
            Thread.Sleep(200);
        }
        foreach (IntPtr h in InstalledProcesses())
        {
            TerminateProcess(h, 0);
            CloseHandle(h);
        }
        Thread.Sleep(500);
    }

    // ----------------------------------------------------------------------- install / uninstall

    static void Remove(string path)
    {
        for (int attempt = 0; attempt < 25; attempt++)   // files can stay locked for a moment after a process exits
        {
            try
            {
                if (Directory.Exists(path)) Directory.Delete(path, true);
                else if (File.Exists(path)) File.Delete(path);
                return;
            }
            catch (IOException) { Thread.Sleep(200); }
            catch (UnauthorizedAccessException) { Thread.Sleep(200); }
        }
        throw new Exception("Couldn't replace " + Path.GetFileName(path) + " (is it open somewhere?)");
    }

    public static void InstallZip(string zip, string version)
    {
        Directory.CreateDirectory(InstallDir);
        string staging = Path.Combine(Path.GetDirectoryName(InstallDir), "disctray-" + Path.GetRandomFileName());
        try
        {
            ZipFile.ExtractToDirectory(zip, staging);
            foreach (string child in Directory.GetFileSystemEntries(InstallDir))
            {
                if (SamePath(child, Self)) continue;   // can't delete the updater that is running; it's replaced below if needed
                Remove(child);
            }
            foreach (string child in Directory.GetFileSystemEntries(staging))
            {
                string dest = Path.Combine(InstallDir, Path.GetFileName(child));
                if (Directory.Exists(child)) Directory.Move(child, dest);
                else File.Move(child, dest);
            }
        }
        finally
        {
            try { Directory.Delete(staging, true); } catch (Exception) { }
        }
        if (!SamePath(Self, UpdaterExe))
            File.Copy(Self, UpdaterExe, true);   // keep a copy for "Check for updates…" and uninstall
        File.WriteAllText(VersionFile, version);
    }

    /// <summary>Start Menu shortcut, Settings > Apps entry, and an existing autostart pointed at the new exe.</summary>
    public static void Register(string version)
    {
        if (TestMode) return;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Shortcut));
            object shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            dynamic lnk = ((dynamic)shell).CreateShortcut(Shortcut);
            lnk.TargetPath = Exe;
            lnk.WorkingDirectory = InstallDir;
            lnk.IconLocation = Exe + ",0";
            lnk.Description = App;
            lnk.Save();
            Marshal.FinalReleaseComObject(lnk);
            Marshal.FinalReleaseComObject(shell);
        }
        catch (Exception) { }   // no shortcut is no reason to fail the install
        long bytes = 0;
        foreach (string f in Directory.GetFiles(InstallDir, "*", SearchOption.AllDirectories))
            bytes += new FileInfo(f).Length;
        using (RegistryKey k = Registry.CurrentUser.CreateSubKey(UninstallKey))
        {
            k.SetValue("DisplayName", App);
            k.SetValue("DisplayVersion", version);
            k.SetValue("Publisher", Repo.Split('/')[0]);
            k.SetValue("DisplayIcon", Exe);
            k.SetValue("InstallLocation", InstallDir);
            k.SetValue("UninstallString", "\"" + UpdaterExe + "\" --uninstall");
            k.SetValue("URLInfoAbout", "https://github.com/" + Repo);
            k.SetValue("EstimatedSize", (int)(bytes / 1024), RegistryValueKind.DWord);
            k.SetValue("NoModify", 1, RegistryValueKind.DWord);
            k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        }
        using (RegistryKey k = Registry.CurrentUser.OpenSubKey(RunKey, true))
        {   // "Start with Windows" was on (maybe from a source checkout): point it at the installed exe
            if (k != null && k.GetValue(App) != null)
                k.SetValue(App, "\"" + Exe + "\"");
        }
    }

    public static void Launch()
    {
        Process.Start(new ProcessStartInfo(Exe) { WorkingDirectory = InstallDir, UseShellExecute = false }).Dispose();
    }

    public static void Uninstall(bool removeSettings)
    {
        CloseRunning();
        if (!TestMode)
        {
            using (RegistryKey k = Registry.CurrentUser.OpenSubKey(RunKey, true))
                if (k != null) k.DeleteValue(App, false);
            Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false);
            try { File.Delete(Shortcut); } catch (Exception) { }
            if (removeSettings)
                try { Directory.Delete(SettingsDir, true); } catch (Exception) { }
        }
        if (Directory.Exists(InstallDir))
            Remove(InstallDir);   // Main moved us out of this folder first, so nothing in it is running
    }

    /// <summary>
    /// Uninstalling from the installed copy: run a temporary copy instead, so the whole folder
    /// can be deleted once this process has exited.
    /// </summary>
    public static void RelaunchFromTemp(string[] args)
    {
        string copy = Path.Combine(Path.GetTempPath(), "DiscTrayUninstall-" + Path.GetRandomFileName() + ".exe");
        File.Copy(Self, copy);
        var cmd = new StringBuilder("--relaunched");
        foreach (string a in args) cmd.Append(" \"").Append(a).Append('"');
        Process.Start(new ProcessStartInfo(copy, cmd.ToString()) { UseShellExecute = false }).Dispose();
    }

    /// <summary>Leftover temporary uninstall copies from earlier runs.</summary>
    public static void CleanTemp()
    {
        try
        {
            foreach (string f in Directory.GetFiles(Path.GetTempPath(), "DiscTrayUninstall-*.exe"))
                if (!SamePath(f, Self))
                    try { File.Delete(f); } catch (Exception) { }
        }
        catch (Exception) { }
    }
}

// --------------------------------------------------------------------------- window

class Window : Form
{
    readonly Label title, detail;
    readonly ProgressBar bar;
    readonly FlowLayoutPanel buttons;
    readonly List<string> argv;

    public Window(List<string> argv)
    {
        this.argv = argv;
        SuspendLayout();
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        Font = new Font("Segoe UI", 9F);
        Text = Setup.App + " Updater";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(400, 150);
        try { Icon = Icon.ExtractAssociatedIcon(Setup.Self); } catch (Exception) { }
        title = new Label { Text = "Checking for updates…", Font = new Font("Segoe UI Semibold", 11F),
                            Location = new Point(16, 14), Size = new Size(368, 24), AutoEllipsis = true };
        detail = new Label { Text = "", ForeColor = Color.FromArgb(0x55, 0x55, 0x55),
                             Location = new Point(16, 40), Size = new Size(368, 20), AutoEllipsis = true };
        bar = new ProgressBar { Location = new Point(16, 66), Size = new Size(368, 18),
                                Style = ProgressBarStyle.Marquee, MarqueeAnimationSpeed = 30 };
        buttons = new FlowLayoutPanel { Location = new Point(16, 102), Size = new Size(368, 34),
                                        FlowDirection = FlowDirection.RightToLeft, WrapContents = false };
        Controls.AddRange(new Control[] { title, detail, bar, buttons });
        ResumeLayout(false);
        PerformLayout();
    }

    void Post(Action a)
    {
        if (IsDisposed) return;
        try { BeginInvoke(a); } catch (InvalidOperationException) { }   // window already closed
    }

    void SetButtons(params KeyValuePair<string, Action>[] specs)
    {
        buttons.Controls.Clear();
        for (int i = specs.Length - 1; i >= 0; i--)   // right to left, so the last one sits at the edge
        {
            Action cmd = specs[i].Value;
            var b = new Button { Text = specs[i].Key, AutoSize = true, MinimumSize = new Size(88, 28) };
            b.Click += delegate { cmd(); };
            buttons.Controls.Add(b);
        }
        if (specs.Length > 0) AcceptButton = (Button)buttons.Controls[buttons.Controls.Count - 1];
    }

    static KeyValuePair<string, Action> Btn(string text, Action cmd)
    {
        return new KeyValuePair<string, Action>(text, cmd);
    }

    void Progress(double percent)
    {
        bar.Style = ProgressBarStyle.Continuous;
        bar.Value = Math.Max(0, Math.Min(100, (int)percent));
    }

    void Busy()
    {
        bar.Style = ProgressBarStyle.Marquee;
    }

    void Done(bool ok)
    {
        bar.Style = ProgressBarStyle.Continuous;
        bar.Value = ok ? 100 : 0;
    }

    void CloseAfter(int ms)
    {
        var t = new System.Windows.Forms.Timer { Interval = ms };
        t.Tick += delegate { t.Stop(); Close(); };
        t.Start();
    }

    void Fail(string what, Exception exc)
    {
        string reason = exc.Message;
        if (exc is WebException) reason = "Couldn't reach GitHub. Check your internet connection.";
        if (reason.Length > 180) reason = reason.Substring(0, 180);
        Post(delegate
        {
            title.Text = what;
            detail.Text = reason;
            Done(false);
            SetButtons(Btn("Close", Close));
        });
    }

    public void RunUpdate()
    {
        Shown += delegate { new Thread(WorkUpdate) { IsBackground = true }.Start(); };
    }

    public void RunUninstall(bool removeSettings)
    {
        title.Text = "Removing " + Setup.App + "…";
        Shown += delegate { new Thread(() => WorkUninstall(removeSettings)) { IsBackground = true }.Start(); };
    }

    void WorkUpdate()
    {
        try
        {
            string latest, url;
            long size;
            Setup.LatestRelease(out latest, out url, out size);
            string current = Setup.InstalledVersion();
            if (current != null && Setup.Compare(current, latest) >= 0)
            {
                Post(delegate
                {
                    title.Text = Setup.App + " " + current + " is up to date.";
                    detail.Text = "You already have the newest version.";
                    Done(true);
                    if (argv.Contains("--from-app")) SetButtons(Btn("Close", Close));
                    else SetButtons(Btn("Open DiscTray", delegate { Setup.Launch(); Close(); }), Btn("Close", Close));
                });
                return;
            }
            string verb = current != null ? "Updating" : "Installing";
            Post(delegate { title.Text = verb + " " + Setup.App + " " + latest + "…"; detail.Text = "Downloading…"; Progress(0); });
            string tmp = Path.Combine(Path.GetTempPath(), "DiscTray-" + latest + Setup.AssetSuffix);
            long lastShown = -1;
            Setup.Download(url, tmp, (done, total) =>
            {
                total = total > 0 ? total : (size > 0 ? size : 1);
                long pct = done * 100 / total;
                if (pct == lastShown && done != total) return;   // don't flood the window
                lastShown = pct;
                Post(delegate
                {
                    Progress(pct);
                    detail.Text = string.Format("Downloading… {0:0.0} of {1:0.0} MB", done / 1e6, total / 1e6);
                });
            });
            Post(delegate { detail.Text = "Closing DiscTray…"; Busy(); });
            Setup.CloseRunning();
            Post(delegate { detail.Text = "Installing…"; });
            Setup.InstallZip(tmp, latest);
            Setup.Register(latest);
            try { File.Delete(tmp); } catch (Exception) { }
            Post(delegate { detail.Text = "Starting DiscTray…"; });
            Setup.Launch();
            Post(delegate
            {
                title.Text = Setup.App + " " + latest + " is installed.";
                detail.Text = "Installed to " + Setup.InstallDir;
                Done(true);
                SetButtons(Btn("Close", Close));
                CloseAfter(2500);
            });
        }
        catch (Exception exc)   // show any failure instead of vanishing
        {
            Fail("Update failed", exc);
        }
    }

    void WorkUninstall(bool removeSettings)
    {
        try
        {
            Setup.Uninstall(removeSettings);
            Post(delegate
            {
                title.Text = Setup.App + " was removed.";
                detail.Text = "Thanks for listening.";
                Done(true);
                SetButtons(Btn("Close", Close));
                CloseAfter(2500);
            });
        }
        catch (Exception exc)
        {
            Fail("Uninstall failed", exc);
        }
    }
}

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072;   // TLS 1.2, which GitHub requires
        var argv = new List<string>(args);
        bool relaunched = argv.Remove("--relaunched");
        if (!relaunched) Setup.CleanTemp();
        bool uninstalling = argv.Contains("--uninstall") || argv.Contains("--uninstall-silent");
        try
        {
            if (uninstalling && Setup.RunningFromInstall())
            {
                Setup.RelaunchFromTemp(argv.ToArray());
                return 0;
            }
            int i = argv.IndexOf("--zip");
            if (i >= 0)   // developer/test path: install a local build instead of downloading
            {
                Setup.CloseRunning();
                Setup.InstallZip(argv[i + 1], argv[i + 2]);
                Setup.Register(argv[i + 2]);
                if (!argv.Contains("--no-launch")) Setup.Launch();
                return 0;
            }
            if (argv.Contains("--uninstall-silent"))
            {
                Setup.Uninstall(false);
                return 0;
            }
        }
        catch (Exception exc)
        {
            Console.Error.WriteLine(exc);
            return 1;
        }

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        var window = new Window(argv);
        if (argv.Contains("--uninstall"))
        {
            string caption = "Uninstall " + Setup.App;
            if (MessageBox.Show("Remove " + Setup.App + " from this PC (for your user)?", caption,
                                MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                return 0;
            bool keep = MessageBox.Show("Keep your settings (last track, style, volume) in case you install it again?",
                                        caption, MessageBoxButtons.YesNo, MessageBoxIcon.Question) == DialogResult.Yes;
            window.RunUninstall(!keep);
        }
        else
        {
            window.RunUpdate();
        }
        Application.Run(window);
        return 0;
    }
}
