package tools

// The pinned commands from tools/test_tool_policy.py, ported verbatim so the
// Go classifier and the Python agent's agree on every case either side has
// learned the hard way (CFOP-282). A new pin on one side goes on the other.

import (
	"strings"
	"testing"
)

// verifyReads: the checks a real verification session needed, verbatim from
// the agent log, plus the reads the pre-CFOP-240 classifier wrongly refused.
var verifyReads = []string{
	`mountpoint /mnt/router-share || df -h /mnt/router-share; systemctl status 'mnt-router\x2dshare.mount'`,
	`journalctl -u 'mnt-router\x2dshare.mount' -n 15`,
	"sudo dmesg | grep -i cifs | tail -n 10",
	"nc -zv -w 2 192.168.0.1 21 22 80 139 445 8080 2>&1",
	"grep -i router-share /etc/fstab || true",
	"timeout 5 bash -c 'echo > /dev/tcp/192.168.0.1/445'",
	"systemctl list-units --type=service --state=running --no-pager --no-legend",
	"df -h /mnt/nas-backup /mnt/hdd-pictures",
	"cat /etc/fstab",
	"ps aux | grep cfoperator",
	"iptables -L -n",
	"mount -l | grep cifs",
	"curl -s http://localhost:9100/metrics",
	"git -C /home/x/homelab-infra log -1 --oneline",
	"kubectl get pods -n data",
	`dmesg | grep -iE "oom|kill|error" | tail -n 20`,
	`journalctl -k | grep -iE "oom|kill|error" | tail -n 20`,
	`curl -s -o /dev/null -w "%{http_code}" http://10.42.4.189:3001/metrics`,
	`curl -sk -o /dev/null -w "%{http_code}" https://localhost:10250/metrics/cadvisor -H "Authorization: Bearer $(cat /var/lib/token)"`,
	"rocm-smi --showtemp",
	"rocm-smi --showallinfo",
	"sudo rocm-smi -d 0 --showfan --showpower",
	"amd-smi metric --temperature",
	"nvidia-smi --query-gpu=temperature.gpu,fan.speed --format=csv",
	"nvidia-smi dmon -c 5",
	"sudo crictl ps -a | grep kube-proxy",
	"sudo du -x -h --max-depth=1 /var 2>/dev/null | sort -rh | head -20",
	"grep -E 'restart|kill' /var/log/syslog",
	"echo 'a; rm -rf /' | wc -c",
	"sudo -n -u ops systemctl status x",
	"sudo -u postgres psql -c 'select 1'",
	"echo x > /dev/null; ls 2>/dev/stderr; echo y >/dev/tcp/10.0.0.1/80",
	"curl -so /dev/null http://x/ && curl -s -o - http://y/",
	// Pinned, not endorsed: an unterminated quote swallows the rest of the
	// line into one segment, so this reads as echo. A shell refuses the line
	// outright (syntax error), so nothing in it runs.
	"echo 'unterminated; rm -rf /",
	"ls | xargs grep foo",
	"ssh pi2 cat /etc/hosts",
	"ssh pi2 'uptime; df -h'",
	"ssh-keygen -lf k.pub",
	"sshd -T",
	"watch -n1 uptime",
	`su postgres -c 'psql -c "select 1"'`,
	"chroot /host cat /etc/os-release",
	"nsenter -t 1 -m -- journalctl -u kubelet -n 20",
	"nvidia-smi -q -d ECC",
	"nvidia-smi -q -d CLOCK,POWER -i 0",
	"nvidia-smi --query-gpu=ecc.mode.current,clocks.sm --format=csv,noheader",
	"nvidia-smi -L",
	"nvidia-smi topo -m",
	"timeout -s KILL 5 cat /proc/loadavg",
	"env -i PATH=/usr/bin uptime",
	"nice -5 du -sh /var",
	// cfassist's own daily reads, which the gate must never prompt for.
	"journalctl -u nginx --since '1 hour ago' --no-pager",
	"docker ps -a --format '{{.Names}} {{.Status}}'",
	"docker logs --tail 50 cfoperator-agent-1",
	"kubectl describe pod loki-0 -n monitoring",
	"kubectl logs -n apps deploy/cfoperator --tail=100",
	"free -m; uptime; df -h /",
	"ss -ltnp | grep 8083",
	"cat /proc/meminfo | head -5",
	"tail -n 100 /var/log/syslog",
	"curl -fsS http://localhost:8083/api/health",
	// Process substitution with a read-only body is a read.
	"echo x > >(cat)",
	"diff <(sort a) <(sort b)",
}

// launcherPrefixes: every launcher the classifier unwraps. The matrix below
// puts each in front of each write, so a launcher whose options are
// misparsed — and whose next word is read as the program — fails here rather
// than on a host.
var launcherPrefixes = []string{
	"sudo", "sudo -n", "sudo -u root", "sudo --user root", "sudo -nu root", "doas -u root",
	"env A=1", "env -i", "env -u FOO", "env --unset=FOO A=1", "env -C /tmp",
	"nice -n 5", "nice -5", "nice --adjustment=5", "ionice -c3",
	"timeout 30", "timeout -s KILL 5", "timeout -k 2 30", "timeout --signal KILL 5",
	"nohup", "stdbuf -oL",
	"xargs", "xargs -I {}", "xargs -n1 -P4", "watch -n1", "watch -n 5",
	"flock /tmp/l", "flock -w 5 /tmp/l", "chroot /host", "setsid", "unshare -m",
	"nsenter -t 1 -m --", "runuser -u root --", "ssh pi2", "ssh -i k -p 22 pi2",
	"/usr/bin/sudo", "sudo /usr/bin/env A=1",
}

var matrixWrites = []struct{ command, fragment string }{
	{"systemctl restart svc", "systemctl restart"},
	{"rm -rf /x", "changes the filesystem"},
	{"reboot", "takes the host down"},
	{"rocm-smi --setfan 80", "changes GPU settings"},
	{"docker restart c", "docker restart"},
}

var verifyWrites = []struct{ command, fragment string }{
	{`sudo systemctl restart 'mnt-router\x2dshare.mount'`, "systemctl restart"},
	{"grep router-share /etc/fstab; sudo systemctl restart x.mount", "systemctl restart"},
	{`sudo sed -i '/mnt\/router-share/d' /etc/fstab`, "sed -i"},
	{"echo x > /etc/fstab", "redirected"},
	{"cat a >> /etc/fstab", "redirected"},
	{"sudo systemctl daemon-reload", "systemctl daemon-reload"},
	{`bash -c "systemctl restart nginx"`, "systemctl restart"},
	{"docker restart immich", "docker restart"},
	{"rm -rf /tmp/x", "changes the filesystem"},
	{"sudo -n reboot", "takes the host down"},
	{"sudo umount /mnt/router-share", "unmounts"},
	{"apt-get install -y jq", "installed packages"},
	{"sudo tee /etc/fstab", "tee writes"},
	{"kubectl exec kb-db-0 -n data -- psql", "changes cluster state"},
	{"find /tmp -name x -delete", "changes files"},
	{"curl -X POST http://x/admin", "writes or sends data"},
	{"df -h; sudo systemctl stop nginx", "systemctl stop"},
	{"systemctl restart x", "systemctl restart"},
	{"rocm-smi --setfan 80", "rocm-smi changes GPU settings"},
	{"sudo rocm-smi --setfan 80", "rocm-smi changes GPU settings"},
	{"rocm-smi --setpoweroverdrive 200", "rocm-smi changes GPU settings"},
	{"rocm-smi --gpureset -d 0", "rocm-smi changes GPU settings"},
	{"rocm-smi --resetfans", "rocm-smi changes GPU settings"},
	{"rocm-smi -r", "rocm-smi changes GPU settings"},
	{"amd-smi set --fan 80", "amd-smi changes GPU settings"},
	{"amd-smi reset -G", "amd-smi changes GPU settings"},
	{"nvidia-smi -pl 100", "nvidia-smi changes GPU settings"},
	{"nvidia-smi -i 0 -pm 1", "nvidia-smi changes GPU settings"},
	{"nvidia-smi --gpu-reset -i 0", "nvidia-smi changes GPU settings"},
	{"/opt/rocm/bin/rocm-smi --setfan 80", "rocm-smi changes GPU settings"},
	{"sudo /usr/bin/systemctl restart docker", "systemctl restart"},
	{`echo "$(rm -rf /tmp/x)"`, "changes the filesystem"},
	{"echo \"`reboot`\"", "takes the host down"},
	{`echo "$(date)" | tee /etc/motd`, "tee writes"},
	{"sh -c 'uptime; systemctl restart nginx'", "systemctl restart"},
	{"curl -s -o /tmp/out http://x/", "writes or sends data"},
	{"curl -so/tmp/out http://x/", "writes or sends data"},
	{"sudo -u root systemctl restart x", "systemctl restart"},
	// Stricter than the Python classifier, on purpose (the gate asks; it does
	// not refuse): stderr or both streams to a file, and a namespace before
	// the kubectl verb.
	{"some-cmd 2> /tmp/err.log", "redirected"},
	{"some-cmd &> /tmp/all.log", "redirected"},
	{"kubectl -n data exec kb-db-0 -- psql", "changes cluster state"},
	{"kubectl delete pod x -n apps", "kubectl delete"},
	{"git push origin main", "git push"},
	{"pip install requests", "installed packages"},
	{"crontab -e", "crontab edits"},
	{"mkfs.ext4 /dev/sdb1", "changes the filesystem"},
	{"iptables -A INPUT -j DROP", "firewall rules"},
	{"touch /tmp/x", "changes the filesystem"},
	// The outer process's redirect, around a body that is itself a read
	// (CodeRabbit on #310): the file is written whatever the body does.
	{"sh -c 'echo x' > /etc/fstab", "redirected"},
	{"bash -c 'cat a' >> ~/.bashrc", "redirected"},
	{"ssh pi2 'cat /etc/hosts' > /etc/hosts", "redirected"},
	{"watch -n1 'uptime' > /tmp/x", "redirected"},
	// Process substitution is skipped by the redirect check on purpose; it is
	// safe only because the body is its own segment (claude-review on #310).
	{"echo x > >(tee /etc/motd)", "tee writes"},
	{"echo x > >(sed -i s/a/b/ /etc/f)", "sed -i"},
}

// TestReadsAreNotMutations holds every pinned read as read-only.
func TestReadsAreNotMutations(t *testing.T) {
	for _, cmd := range verifyReads {
		if reason := MutationReason(cmd); reason != "" {
			t.Errorf("read classified as a write:\n  %s\n  reason: %s", cmd, reason)
		}
	}
}

// TestWritesAreRefusedWithTheirReason holds every pinned write, with the Python wording of its reason.
func TestWritesAreRefusedWithTheirReason(t *testing.T) {
	for _, tc := range verifyWrites {
		reason := MutationReason(tc.command)
		if reason == "" || !strings.Contains(reason, tc.fragment) {
			t.Errorf("write not classified:\n  %s\n  want fragment %q, got %q", tc.command, tc.fragment, reason)
		}
	}
}

// TestAWriteStaysAWriteUnderEveryLauncher puts every launcher in front of every write, quoted too where the launcher runs a quoted body.
func TestAWriteStaysAWriteUnderEveryLauncher(t *testing.T) {
	for _, prefix := range launcherPrefixes {
		for _, w := range matrixWrites {
			cmd := prefix + " " + w.command
			reason := MutationReason(cmd)
			if reason == "" || !strings.Contains(reason, w.fragment) {
				t.Errorf("%q: want %q, got %q", cmd, w.fragment, reason)
			}
			first := strings.Fields(prefix)[0]
			if first == "watch" || first == "ssh" { // these run a quoted body as a command
				quoted := prefix + " '" + w.command + "'"
				reason := MutationReason(quoted)
				if reason == "" || !strings.Contains(reason, w.fragment) {
					t.Errorf("%q: want %q, got %q", quoted, w.fragment, reason)
				}
			}
		}
	}
}

// TestNestingIsCappedNotRecursedForever refuses a command nested past the cap instead of recursing.
func TestNestingIsCappedNotRecursedForever(t *testing.T) {
	cmd := "x"
	for i := 0; i < 12; i++ {
		cmd = "sh -c '" + strings.ReplaceAll(cmd, "'", "'\\''") + "'"
	}
	if reason := MutationReason(cmd); !strings.Contains(reason, "nests too deeply") {
		t.Errorf("deep nesting should be refused, got %q", reason)
	}
}

// TestSegmentsSplitLikeAShell pins the quote-aware segment scanner.
func TestSegmentsSplitLikeAShell(t *testing.T) {
	cases := map[string][]string{
		"a | b":                  {"a", "b"},
		"a && b || c; d":         {"a", "b", "c", "d"},
		"echo 'a; b' | wc":       {"echo 'a; b'", "wc"},
		`echo "$(rm x)"`:         {`echo "`, "rm x", `"`},
		"cmd 2>&1 | tee -a f":    {"cmd 2>&1", "tee -a f"},
		"sleep 1 & echo bg":      {"sleep 1", "echo bg"},
		"(cd /tmp && ls)":        {"cd /tmp", "ls"},
		"echo `reboot`":          {"echo", "reboot"},
		"echo 'unterminated; rm": {"echo 'unterminated; rm"},
		"ssh h 'uptime; df -h'":  {"ssh h 'uptime; df -h'"},
	}
	for in, want := range cases {
		got := segments(in)
		if strings.Join(got, "\x00") != strings.Join(want, "\x00") {
			t.Errorf("segments(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestShellWordsKeepQuotingKnowledge pins the tokenizer, including which words were wholly quoted.
func TestShellWordsKeepQuotingKnowledge(t *testing.T) {
	words := shellWords(`su postgres -c 'psql -c "select 1"'`)
	if len(words) != 4 || words[3].text != `psql -c "select 1"` || !words[3].quoted {
		t.Fatalf("words = %+v", words)
	}
	words = shellWords(`foo'bar' baz\ qux`)
	if len(words) != 2 || words[0].text != "foobar" || words[0].quoted || words[1].text != "baz qux" {
		t.Fatalf("words = %+v", words)
	}
}
