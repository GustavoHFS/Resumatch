"""Compiles the native Windows GUI launcher `Resumatch.exe` in the repository root.

Hardened against browser IPC handoff and port conflicts:
1. Validates `"project":"Resumatch"` + `"version":"2.2.0"` on `/api/health`:
   - Evicts stale older Resumatch processes on port 8000.
   - Falls back automatically to a free port in `8001..8010` if another non-Resumatch app uses port 8000.
2. Uses the operating system's default browser and monitors lifecycle status,
   never treating a browser process exit as a tab close.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ICON_PATH = REPO_ROOT / "assets" / "resumatch.ico"
OUTPUT_EXE = REPO_ROOT / "Resumatch.exe"

CSC_CANDIDATES = [
    Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"),
]

CSHARP_SOURCE = r"""
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyCopyright("Copyright (c) 2026 Gustavo Henrique Francisco da Silva")]

namespace ResumatchLauncher
{
    internal static class Program
    {
        private const string ExpectedVersionToken = "\"version\":\"2.2.0\"";

        private enum PortState
        {
            Free,
            CompatibleResumatch,
            StaleResumatch,
            ForeignApp
        }

        private static bool IsPortListening(int port)
        {
            try
            {
                using (TcpClient tcp = new TcpClient())
                {
                    IAsyncResult ar = tcp.BeginConnect("127.0.0.1", port, null, null);
                    bool ok = ar.AsyncWaitHandle.WaitOne(280);
                    if (!ok) return false;
                    tcp.EndConnect(ar);
                    return true;
                }
            }
            catch
            {
                return false;
            }
        }

        private static string HttpGetJsonCompact(string url, int timeoutMs)
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
                req.Method = "GET";
                req.Timeout = timeoutMs;
                req.ReadWriteTimeout = timeoutMs;
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    if (resp.StatusCode != HttpStatusCode.OK) return null;
                    using (Stream stream = resp.GetResponseStream())
                    using (StreamReader reader = new StreamReader(stream, Encoding.UTF8))
                    {
                        return reader.ReadToEnd().Replace(" ", "").Replace("\n", "").Replace("\r", "");
                    }
                }
            }
            catch
            {
                return null;
            }
        }

        private static PortState ProbePort(int port)
        {
            if (!IsPortListening(port)) return PortState.Free;
            string compact = HttpGetJsonCompact("http://127.0.0.1:" + port + "/api/health", 850);
            if (compact == null) return PortState.ForeignApp;
            if (compact.Contains("\"project\":\"Resumatch\""))
            {
                if (compact.Contains(ExpectedVersionToken) && compact.Contains("\"ai_connected\":"))
                {
                    return PortState.CompatibleResumatch;
                }
                return PortState.StaleResumatch;
            }
            return PortState.ForeignApp;
        }

        private static void KillProcessTree(int pid)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo
                {
                    FileName = "taskkill.exe",
                    Arguments = "/PID " + pid + " /T /F",
                    CreateNoWindow = true,
                    UseShellExecute = false,
                    WindowStyle = ProcessWindowStyle.Hidden
                };
                using (Process killer = Process.Start(psi))
                {
                    if (killer != null) killer.WaitForExit(3000);
                }
            }
            catch
            {
            }
        }

        private static void EvictStaleListenersOnPort(int port)
        {
            try
            {
                int selfPid = Process.GetCurrentProcess().Id;
                ProcessStartInfo psi = new ProcessStartInfo
                {
                    FileName = "netstat.exe",
                    Arguments = "-ano -p tcp",
                    CreateNoWindow = true,
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    WindowStyle = ProcessWindowStyle.Hidden
                };
                string output = "";
                using (Process proc = Process.Start(psi))
                {
                    if (proc != null)
                    {
                        output = proc.StandardOutput.ReadToEnd();
                        proc.WaitForExit(3000);
                    }
                }

                HashSet<int> pids = new HashSet<int>();
                Regex rx = new Regex(@":" + port + @"\s+\S+\s+LISTENING\s+(\d+)", RegexOptions.IgnoreCase);
                foreach (Match m in rx.Matches(output))
                {
                    int pid;
                    if (int.TryParse(m.Groups[1].Value, out pid) && pid > 4 && pid != selfPid)
                    {
                        pids.Add(pid);
                    }
                }

                foreach (int pid in pids)
                {
                    KillProcessTree(pid);
                }

                for (int i = 0; i < 15; i++)
                {
                    if (!IsPortListening(port)) break;
                    Thread.Sleep(150);
                }
            }
            catch
            {
            }
        }

        private static int SelectPort(out bool alreadyHealthy)
        {
            alreadyHealthy = false;
            PortState s8000 = ProbePort(8000);
            if (s8000 == PortState.CompatibleResumatch)
            {
                alreadyHealthy = true;
                return 8000;
            }
            if (s8000 == PortState.StaleResumatch)
            {
                EvictStaleListenersOnPort(8000);
                if (!IsPortListening(8000)) return 8000;
            }
            if (s8000 == PortState.Free)
            {
                return 8000;
            }

            for (int p = 8001; p <= 8010; p++)
            {
                PortState sp = ProbePort(p);
                if (sp == PortState.CompatibleResumatch)
                {
                    alreadyHealthy = true;
                    return p;
                }
                if (sp == PortState.Free)
                {
                    return p;
                }
            }
            return 8000;
        }

        private static bool ShouldShutdown(int port)
        {
            string compact = HttpGetJsonCompact("http://127.0.0.1:" + port + "/api/lifecycle/status", 700);
            if (compact == null) return false;
            // Backend owns the 120s boot grace, 10s reload grace and 30min inactivity timeout.
            return compact.Contains("\"shutdown_ready\":true");
        }

        private static void MonitorViaHeartbeatUntilClosed(int port, Process backendProc)
        {
            // Never infer tab closure from a browser process: Firefox and Chrome may
            // hand the URL to an existing process and exit immediately.
            while (true)
            {
                if (backendProc != null && backendProc.HasExited) break;
                if (ShouldShutdown(port)) break;
                Thread.Sleep(1000);
            }
        }

        private static string FindPythonExecutable(string repoRoot)
        {
            string[] candidates = new string[]
            {
                Path.Combine(repoRoot, ".venv", "Scripts", "pythonw.exe"),
                Path.Combine(repoRoot, ".venv", "Scripts", "python.exe")
            };
            foreach (string c in candidates)
            {
                if (File.Exists(c)) return c;
            }
            return null;
        }

        [STAThread]
        private static int Main()
        {
            string repoRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\', '/');
            string startScript = Path.Combine(repoRoot, "scripts", "start_app.py");
            if (!File.Exists(startScript))
            {
                MessageBox.Show(
                    "Arquivo scripts\\start_app.py não encontrado na pasta do Resumatch.",
                    "Resumatch",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 1;
            }

            Process backendProc = null;
            bool startedBackendHere = false;

            try
            {
                bool alreadyHealthy;
                int port = SelectPort(out alreadyHealthy);
                string appUrl = "http://127.0.0.1:" + port;

                if (!alreadyHealthy)
                {
                    string pyExe = FindPythonExecutable(repoRoot);
                    if (pyExe == null)
                    {
                        MessageBox.Show(
                            "Ambiente Python (.venv) não encontrado. Execute a instalação inicial antes de abrir o Resumatch.",
                            "Resumatch",
                            MessageBoxButtons.OK,
                            MessageBoxIcon.Warning
                        );
                        return 1;
                    }

                    ProcessStartInfo backendPsi = new ProcessStartInfo
                    {
                        FileName = pyExe,
                        Arguments = "\"" + startScript + "\" --no-browser --port " + port,
                        WorkingDirectory = repoRoot,
                        CreateNoWindow = true,
                        UseShellExecute = false,
                        WindowStyle = ProcessWindowStyle.Hidden
                    };
                    backendProc = Process.Start(backendPsi);
                    startedBackendHere = backendProc != null;

                    for (int i = 0; i < 125; i++)
                    {
                        if (ProbePort(port) == PortState.CompatibleResumatch) break;
                        if (backendProc != null && backendProc.HasExited) break;
                        Thread.Sleep(200);
                    }
                }

                if (ProbePort(port) != PortState.CompatibleResumatch)
                {
                    MessageBox.Show(
                        "Não foi possível iniciar o servidor local do Resumatch na porta " + port + ".",
                        "Resumatch",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error
                    );
                    return 1;
                }

                Process.Start(new ProcessStartInfo
                {
                    FileName = appUrl + "/",
                    UseShellExecute = true
                });
                if (startedBackendHere)
                {
                    MonitorViaHeartbeatUntilClosed(port, backendProc);
                }
            }
            finally
            {
                if (startedBackendHere && backendProc != null)
                {
                    try
                    {
                        if (!backendProc.HasExited)
                        {
                            KillProcessTree(backendProc.Id);
                        }
                    }
                    catch
                    {
                    }
                    finally
                    {
                        backendProc.Dispose();
                    }
                }
            }

            return 0;
        }
    }
}
"""


def build_exe() -> Path:
    if sys.platform != "win32":
        raise RuntimeError("Native Resumatch.exe compilation requires Windows.")

    if not ICON_PATH.is_file():
        from generate_icons import main as gen_icons_main

        gen_icons_main()

    csc_path = next((p for p in CSC_CANDIDATES if p.is_file()), None)
    if csc_path is None:
        raise RuntimeError("Windows C# compiler (csc.exe) not found in .NET Framework v4.0.30319.")

    with tempfile.TemporaryDirectory(prefix="resumatch_csc_") as tmp_dir:
        cs_file = Path(tmp_dir) / "ResumatchLauncher.cs"
        cs_file.write_text(CSHARP_SOURCE.strip() + "\n", encoding="utf-8-sig")

        cmd = [
            str(csc_path),
            "/nologo",
            "/target:winexe",
            "/optimize+",
            f"/win32icon:{ICON_PATH}",
            f"/out:{OUTPUT_EXE}",
            "/r:System.dll",
            "/r:System.Windows.Forms.dll",
            str(cs_file),
        ]
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"csc.exe failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")

    if not OUTPUT_EXE.is_file():
        raise RuntimeError("Resumatch.exe was not created.")
    return OUTPUT_EXE


def main() -> int:
    exe_path = build_exe()
    print(f"Built native GUI launcher: {exe_path} ({exe_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
