"""Run one command and record its exit code in a file.

    python3 -m peerlink.wrap EXIT_FILE STDOUT STDERR STDIN|"" -- CMD...

peerlink starts this in its own session (process group), so the run survives a
peerlink restart and can still be killed as a group; the exit file lets a restarted
peerlink learn how the run ended.
"""
import os
import signal
import subprocess
import sys


def main(argv):
    exit_file, out, err, stdin = argv[:4]
    assert argv[4] == "--"
    cmd = argv[5:]
    fin = open(stdin, "rb") if stdin else subprocess.DEVNULL
    with open(out, "ab") as fo, open(err, "ab") as fe:
        try:
            p = subprocess.Popen(cmd, stdin=fin, stdout=fo, stderr=fe)
        except OSError as e:
            fe.write(f"peerlink: cannot start {cmd[0]}: {e}\n".encode())
            code = 127
        else:
            def forward(sig, _frame):
                try:
                    p.send_signal(sig)
                except OSError:
                    pass
            signal.signal(signal.SIGTERM, forward)
            signal.signal(signal.SIGINT, forward)
            code = p.wait()
    tmp = exit_file + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(code))
    os.replace(tmp, exit_file)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
