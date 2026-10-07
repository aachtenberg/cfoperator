package tools

// Read-only classification for the bash tool (CFOP-282).
//
// A port of the Python agent's ssh_mutation_reason (tools/ssh.py, CFOP-124 and
// CFOP-240), which gates every ssh_execute the agent runs unattended. The two
// have to agree, and tools/test_tool_policy.py's pinned commands are ported
// into mutation_test.go so a divergence fails a test rather than running on a
// host.
//
// It is a denylist of write shapes, not a proof of harmlessness, and the
// Python file says the same of itself: an interpreter one-liner, a database
// client, or a script by its path is not classified and counts as a read.
// What the gate is for is the common case — a model deciding to restart,
// delete, install or edit — and asking the operator first.
//
// Token-based, not a regex port: Go's regexp is RE2, with no lookahead or
// lookbehind, and the Python patterns lean on both. Each segment of the
// command line is split into shell words, the launchers in front of the
// program (sudo, env, timeout, xargs, ssh, …) are stripped by a table that
// knows which of their options take a value, and the program word is then
// matched against a table of (program, args) → reason.

import (
	"fmt"
	"path"
	"strings"
)

// maxNesting is how many command-in-a-string layers are unwrapped
// (bash -c "su -c '...'"). Real commands use two or three; past this the
// command is refused rather than recursed into.
const maxNesting = 8

// MutationReason reports why a shell command is not read-only, or "" when
// nothing known matched. Every pipeline segment, subshell and `sh -c` body is
// unwrapped and its program word checked; output redirected to a file counts
// as a write (`2>&1`, `/dev/null` and `/dev/tcp` probes do not).
func MutationReason(command string) string {
	return mutationReason(command, 0)
}

func mutationReason(command string, depth int) string {
	if depth > maxNesting {
		return "the command nests too deeply to classify"
	}
	for _, raw := range segments(command) {
		words := unwrap(shellWords(raw))
		if len(words) == 0 {
			continue
		}
		// A command handed over as a string runs its body; the body is what
		// gets classified.
		if body, ok := shellBody(words); ok {
			if reason := mutationReason(body, depth+1); reason != "" {
				return reason
			}
			continue
		}
		if reason := matchMutator(words); reason != "" {
			return reason
		}
		if redirectsToFile(raw) {
			return "output is redirected to a file"
		}
	}
	return ""
}

// ---------------------------------------------------------------------------
// Splitting
// ---------------------------------------------------------------------------

// segments splits a command line into the simple commands a shell would run.
//
// Quote-aware: a separator inside single quotes is text, and so is one inside
// double quotes — except `$(` and a backtick, which the shell still executes
// there. `sh -c '...'` stays one segment so its body is recursed into whole.
// Separators: `;`, `|`, `||`, `&&`, a background `&` (not `2>&1` / `&>`),
// newline, parentheses, `$(` and backticks. A port of the Python scanner.
func segments(text string) []string {
	var out []string
	var buf strings.Builder
	var state byte // 0, '\'' or '"'
	type frame struct {
		closer  byte
		restore byte
	}
	var stack []frame

	cut := func() {
		if seg := strings.TrimSpace(buf.String()); seg != "" {
			out = append(out, seg)
		}
		buf.Reset()
	}

	runes := []byte(text)
	for i := 0; i < len(runes); i++ {
		c := runes[i]
		var nxt byte
		if i+1 < len(runes) {
			nxt = runes[i+1]
		}
		switch {
		case c == '\\' && state != '\'':
			buf.WriteByte(c)
			if i+1 < len(runes) {
				buf.WriteByte(nxt)
				i++
			}
		case state == '\'':
			if c == '\'' {
				state = 0
			}
			buf.WriteByte(c)
		case state == '"' && c == '"':
			state = 0
			buf.WriteByte(c)
		case c == '$' && nxt == '(':
			cut()
			stack = append(stack, frame{')', state})
			state = 0
			i++
		case c == '`':
			cut()
			if state == 0 && len(stack) > 0 && stack[len(stack)-1].closer == '`' {
				state = stack[len(stack)-1].restore
				stack = stack[:len(stack)-1]
			} else {
				stack = append(stack, frame{'`', state})
				state = 0
			}
		case state == '"':
			buf.WriteByte(c)
		case c == '\'' || c == '"':
			state = c
			buf.WriteByte(c)
		case c == '(':
			cut()
			stack = append(stack, frame{')', 0})
		case c == ')':
			cut()
			if len(stack) > 0 && stack[len(stack)-1].closer == ')' {
				state = stack[len(stack)-1].restore
				stack = stack[:len(stack)-1]
			}
		case c == ';' || c == '\n':
			cut()
		case c == '|':
			cut()
			if nxt == '|' {
				i++
			}
		case c == '&':
			prev := byte(0)
			if s := buf.String(); s != "" {
				prev = s[len(s)-1]
			}
			switch {
			case nxt == '&':
				cut()
				i++
			case nxt == '>' || (nxt >= '0' && nxt <= '9') || prev == '<' || prev == '>':
				buf.WriteByte(c)
			default:
				cut()
			}
		default:
			buf.WriteByte(c)
		}
	}
	cut()
	return out
}

// word is one shell word with its quoting known: `quoted` means the whole
// word was a single quoted string, which is how `watch '...'` and `ssh host
// '...'` hand over a command to run.
type word struct {
	text   string
	quoted bool
}

// shellWords splits one segment into words, stripping the quotes and
// resolving backslash escapes. An unterminated quote runs to the end, as the
// segment scanner already treated it.
func shellWords(seg string) []word {
	var words []word
	var buf strings.Builder
	inWord, quotedSpans, plainChars := false, 0, 0
	var state byte
	flush := func() {
		if inWord {
			words = append(words, word{text: buf.String(), quoted: quotedSpans == 1 && plainChars == 0})
		}
		buf.Reset()
		inWord, quotedSpans, plainChars = false, 0, 0
	}
	for i := 0; i < len(seg); i++ {
		c := seg[i]
		switch {
		case state == '\'':
			if c == '\'' {
				state = 0
			} else {
				buf.WriteByte(c)
			}
		case state == '"':
			switch {
			case c == '"':
				state = 0
			case c == '\\' && i+1 < len(seg):
				i++
				buf.WriteByte(seg[i])
			default:
				buf.WriteByte(c)
			}
		case c == '\'' || c == '"':
			state = c
			inWord = true
			quotedSpans++
		case c == '\\' && i+1 < len(seg):
			i++
			buf.WriteByte(seg[i])
			inWord = true
			plainChars++
		case c == ' ' || c == '\t':
			flush()
		default:
			buf.WriteByte(c)
			inWord = true
			plainChars++
		}
	}
	flush()
	return words
}

// ---------------------------------------------------------------------------
// Launchers: what stands in front of the program
// ---------------------------------------------------------------------------

// launcher says how to step over one program that runs its arguments as a
// command. valueShort lists the single-letter options that take a value (the
// next word, or the rest of the cluster: `-u root`, `-uroot`, `-nu root`);
// valueLong the long options that do (`--user root`, `--user=root`);
// positionals how many bare words follow the options before the command
// (timeout's duration, flock's file, chroot's root, ssh's host).
type launcher struct {
	valueShort  string
	valueLong   []string
	positionals int
	// noCluster: a short cluster ending in `c` is not an option to step over
	// but a command string (flock -c, runuser -c); leave it for shellBody.
	noCluster bool
	// dashDash: a `--` ends the options.
	dashDash bool
}

var launchers = map[string]launcher{
	"sudo":    {valueShort: "ugUhpCDRrtT", valueLong: []string{"user", "group", "host", "prompt", "chdir", "chroot", "role", "type", "command-timeout", "close-from", "other-user", "login-class"}, dashDash: true},
	"doas":    {valueShort: "u"},
	"env":     {valueShort: "uCP", valueLong: []string{"unset", "chdir"}},
	"nice":    {valueShort: "n", valueLong: []string{"adjustment"}},
	"ionice":  {},
	"timeout": {valueShort: "sk", valueLong: []string{"signal", "kill-after"}, positionals: 1},
	"command": {}, "exec": {}, "nohup": {}, "time": {}, "stdbuf": {}, "setsid": {}, "unshare": {},
	"xargs":   {valueShort: "InPLsdEa"},
	"watch":   {valueShort: "ng", valueLong: []string{"interval"}},
	"flock":   {valueShort: "wE", positionals: 1, noCluster: true},
	"chroot":  {positionals: 1},
	"nsenter": {valueShort: "tSG", valueLong: []string{"target", "setuid", "setgid"}, dashDash: true},
	"runuser": {valueShort: "ugG", noCluster: true, dashDash: true},
	"ssh":     {valueShort: "BbcDEeFIiJLlmOoPpQRSWw", positionals: 1},
}

// shellLaunchers run a quoted body given with -c.
var shellLaunchers = map[string]bool{
	"sh": true, "bash": true, "zsh": true, "dash": true, "ksh": true, "ash": true,
	"su": true, "runuser": true, "flock": true,
}

func isEnvAssign(s string) bool {
	eq := strings.IndexByte(s, '=')
	if eq < 1 {
		return false
	}
	for i, c := range s[:eq] {
		if !(c == '_' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || (i > 0 && c >= '0' && c <= '9')) {
			return false
		}
	}
	return true
}

// programName is the word by its name, not its path: /usr/bin/systemctl
// restart and /opt/rocm/bin/rocm-smi --setfan are the same writes.
func programName(w string) string {
	w = strings.TrimPrefix(w, `\`)
	if strings.Contains(w, "/") {
		return path.Base(w)
	}
	return w
}

// unwrap strips env assignments and launcher prefixes until the first word is
// the program. Quoted words are never a launcher or an option.
func unwrap(words []word) []word {
	for len(words) > 0 {
		first := words[0]
		if first.quoted {
			return words
		}
		if isEnvAssign(first.text) {
			words = words[1:]
			continue
		}
		name := programName(first.text)
		l, ok := launchers[name]
		if !ok {
			words[0].text = name
			return words
		}
		rest := words[1:]
		// Options, each possibly consuming the next word.
		for len(rest) > 0 && !rest[0].quoted && strings.HasPrefix(rest[0].text, "-") && rest[0].text != "-" {
			opt := rest[0].text
			if opt == "--" {
				if l.dashDash {
					rest = rest[1:]
				}
				break
			}
			if strings.HasPrefix(opt, "--") {
				long := strings.TrimPrefix(opt, "--")
				takesValue := false
				for _, v := range l.valueLong {
					if long == v {
						takesValue = true
					}
				}
				rest = rest[1:]
				if takesValue && len(rest) > 0 {
					rest = rest[1:]
				}
				continue
			}
			cluster := opt[1:]
			if l.noCluster && strings.HasSuffix(cluster, "c") {
				break // flock -c / runuser -c: a command string follows
			}
			rest = rest[1:]
			// A value-taking letter consumes the rest of the cluster, or the
			// next word when it is last.
			for i := 0; i < len(cluster); i++ {
				if strings.IndexByte(l.valueShort, cluster[i]) < 0 {
					continue
				}
				if i == len(cluster)-1 && len(rest) > 0 {
					rest = rest[1:]
				}
				break
			}
		}
		for n := 0; n < l.positionals && len(rest) > 0; n++ {
			rest = rest[1:]
		}
		words = rest
	}
	return words
}

// shellBody returns the command string a launcher is about to run, if the
// words are one: `sh -c '...'`, `su user -c '...'`, or a wholly quoted word
// left behind by `watch '...'` / `ssh host '...'`.
func shellBody(words []word) (string, bool) {
	if len(words) == 1 && words[0].quoted {
		return words[0].text, true
	}
	if !shellLaunchers[words[0].text] {
		return "", false
	}
	for i := 1; i < len(words)-1; i++ {
		w := words[i].text
		if !words[i].quoted && strings.HasPrefix(w, "-") && !strings.HasPrefix(w, "--") && strings.HasSuffix(w, "c") {
			return words[i+1].text, true
		}
	}
	return "", false
}

// ---------------------------------------------------------------------------
// Mutators: the program word and its arguments
// ---------------------------------------------------------------------------

func set(items ...string) map[string]bool {
	m := make(map[string]bool, len(items))
	for _, s := range items {
		m[s] = true
	}
	return m
}

var (
	systemctlVerbs = set("restart", "stop", "start", "reload", "reload-or-restart", "try-restart",
		"enable", "disable", "mask", "unmask", "kill", "daemon-reload", "daemon-reexec",
		"reset-failed", "isolate", "set-default", "set-property", "revert", "edit")
	hostDown     = set("reboot", "shutdown", "poweroff", "halt")
	processEnd   = set("kill", "pkill", "killall")
	filesystem   = set("rm", "rmdir", "mv", "cp", "dd", "truncate", "shred", "ln", "chmod", "chown", "chgrp", "touch", "mkdir", "fdisk", "parted", "wipefs", "swapoff", "swapon")
	packageTools = set("apt", "apt-get", "dnf", "yum", "zypper", "pacman", "snap", "pip", "pip3", "npm", "brew")
	packageVerbs = set("install", "remove", "purge", "upgrade", "update", "dist-upgrade", "autoremove", "uninstall", "refresh")
	dockerVerbs  = set("restart", "stop", "start", "rm", "rmi", "kill", "run", "exec", "up", "down", "prune",
		"pull", "create", "rename", "update", "pause", "unpause", "cp")
	kubectlVerbs = set("apply", "delete", "patch", "edit", "scale", "exec", "cp", "drain", "cordon", "uncordon",
		"taint", "label", "annotate", "create", "replace", "rollout", "set", "expose", "run")
	accounts   = set("useradd", "userdel", "usermod", "passwd", "chpasswd", "groupadd", "groupdel", "groupmod", "visudo")
	gitVerbs   = set("push", "commit", "reset", "checkout", "switch", "rebase", "merge", "clean", "stash", "pull", "rm", "mv", "add", "tag", "restore")
	kernelNet  = set("modprobe", "rmmod", "insmod")
	schedulers = set("systemd-run", "at", "batch")
	nmcliObj   = set("con", "connection", "dev", "device")
	nmcliVerbs = set("up", "down", "mod", "modify", "del", "delete", "add")
	ipObj      = set("link", "addr", "address", "route", "neigh")
	ipVerbs    = set("add", "del", "delete", "set", "flush", "change", "replace")
	curlSend   = set("-d", "--json", "-F", "-T", "--upload-file", "-O", "--remote-name")
	nvidiaRead = set("dmon", "pmon", "topo")
	nvidiaSet  = set("-pl", "-ac", "-rac", "-r", "-pm", "-c", "-e", "-lgc", "-rgc", "-lmc", "-rmc", "-cgi", "-dgi", "-cci", "-dci",
		"--power-limit", "--applications-clocks", "--reset-applications-clocks", "--persistence-mode", "--compute-mode",
		"--ecc-config", "--gpu-reset", "--lock-gpu-clocks", "--reset-gpu-clocks", "--lock-memory-clocks", "--reset-memory-clocks")
	devSinks = []string{"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/fd/", "/dev/tcp/", "/dev/udp/"}
)

func texts(words []word) []string {
	out := make([]string, len(words))
	for i, w := range words {
		out[i] = w.text
	}
	return out
}

// firstBare is the first argument that is not an option (`-x`, `--x`).
func firstBare(args []string) string {
	for _, a := range args {
		if !strings.HasPrefix(a, "-") {
			return a
		}
	}
	return ""
}

func anyArg(args []string, want map[string]bool) string {
	for _, a := range args {
		if want[a] {
			return a
		}
	}
	return ""
}

func hasPrefixAny(s string, prefixes ...string) bool {
	for _, p := range prefixes {
		if strings.HasPrefix(s, p) {
			return true
		}
	}
	return false
}

func isDevSink(target string) bool {
	if target == "" || target == "-" {
		return true
	}
	for _, s := range devSinks {
		if target == s || (strings.HasSuffix(s, "/") && strings.HasPrefix(target, s)) {
			return true
		}
	}
	return false
}

func matchMutator(words []word) string {
	prog := words[0].text
	args := texts(words[1:])

	switch {
	case prog == "systemctl":
		if v := firstBare(args); systemctlVerbs[v] {
			return fmt.Sprintf("systemctl %s changes service state", v)
		}
	case hostDown[prog]:
		return prog + " takes the host down"
	case prog == "init" && (firstBare(args) == "0" || firstBare(args) == "6"):
		return "init takes the host down"
	case processEnd[prog]:
		return prog + " ends processes"
	case filesystem[prog] || strings.HasPrefix(prog, "mkfs"):
		return prog + " changes the filesystem"
	case prog == "sed":
		for _, a := range args {
			if a == "--in-place" || strings.HasPrefix(a, "--in-place=") {
				return "sed -i edits a file in place"
			}
			if strings.HasPrefix(a, "-") && !strings.HasPrefix(a, "--") && strings.Contains(a, "i") {
				return "sed -i edits a file in place"
			}
		}
	case prog == "tee":
		return "tee writes a file"
	case packageTools[prog]:
		if v := firstBare(args); packageVerbs[v] {
			return fmt.Sprintf("%s %s changes installed packages", prog, v)
		}
	case prog == "umount":
		return "umount unmounts a filesystem"
	case prog == "mount":
		if len(args) > 0 && args[0] != "-l" {
			return "mount with arguments mounts something"
		}
	case prog == "docker":
		rest := args
		if len(rest) > 0 && rest[0] == "compose" {
			rest = rest[1:]
		}
		if v := firstBare(rest); dockerVerbs[v] {
			return fmt.Sprintf("docker %s changes container state", v)
		}
	case prog == "kubectl" || prog == "oc" || (prog == "k3s" && firstBare(args) == "kubectl"):
		// Any verb word anywhere, not only first: `kubectl -n data exec …`
		// puts the namespace before it.
		if v := anyArg(args, kubectlVerbs); v != "" {
			return fmt.Sprintf("kubectl %s changes cluster state", v)
		}
	case prog == "iptables" || prog == "ip6tables":
		if anyArg(args, set("-L", "-S", "-C")) == "" {
			return prog + " without -L changes firewall rules"
		}
	case prog == "nft":
		if firstBare(args) != "list" {
			return "nft changes firewall rules"
		}
	case prog == "ufw":
		if firstBare(args) != "status" {
			return "ufw changes firewall rules"
		}
	case prog == "crontab":
		if len(args) == 0 || args[0] != "-l" {
			return "crontab edits scheduled jobs"
		}
	case accounts[prog]:
		return prog + " changes accounts"
	case prog == "git":
		rest := args
		for len(rest) > 0 && strings.HasPrefix(rest[0], "-") {
			if rest[0] == "-C" && len(rest) > 1 {
				rest = rest[2:]
				continue
			}
			rest = rest[1:]
		}
		if len(rest) > 0 && gitVerbs[rest[0]] {
			return fmt.Sprintf("git %s changes the working tree or remote", rest[0])
		}
	case prog == "sysctl":
		for _, a := range args {
			if a == "-w" || (!strings.HasPrefix(a, "-") && strings.Contains(a, "=")) {
				return "sysctl changes network or kernel state"
			}
		}
	case kernelNet[prog]:
		return prog + " changes network or kernel state"
	case (prog == "hostnamectl" || prog == "timedatectl") && strings.HasPrefix(firstBare(args), "set"):
		return prog + " changes network or kernel state"
	case prog == "nmcli":
		bare := bareArgs(args)
		if len(bare) >= 2 && nmcliObj[bare[0]] && nmcliVerbs[bare[1]] {
			return "nmcli changes network or kernel state"
		}
	case prog == "ip":
		bare := bareArgs(args)
		if len(bare) >= 2 && ipObj[bare[0]] && ipVerbs[bare[1]] {
			return "ip changes network or kernel state"
		}
	case schedulers[prog]:
		return prog + " schedules work on the host"
	case prog == "journalctl":
		for _, a := range args {
			if hasPrefixAny(a, "--vacuum", "--rotate", "--flush") {
				return "journalctl --vacuum/--rotate changes the journal"
			}
		}
	case prog == "find":
		for i, a := range args {
			if a == "-delete" {
				return "find -delete/-exec changes files"
			}
			if (a == "-exec" || a == "-execdir" || a == "-ok") && i+1 < len(args) {
				switch args[i+1] {
				case "rm", "mv", "chmod", "chown":
					return "find -delete/-exec changes files"
				case "sed":
					if i+2 < len(args) && strings.Contains(args[i+2], "i") && strings.HasPrefix(args[i+2], "-") {
						return "find -delete/-exec changes files"
					}
				}
			}
		}
	case prog == "curl":
		if curlWrites(args) {
			return "curl that writes or sends data"
		}
	case prog == "wget":
		stdout := false
		for i, a := range args {
			if a == "--spider" || a == "-O-" || a == "-qO-" || (a == "-O" && i+1 < len(args) && args[i+1] == "-") {
				stdout = true
			}
			if strings.HasPrefix(a, "-") && !strings.HasPrefix(a, "--") && strings.HasSuffix(a, "O-") {
				stdout = true
			}
		}
		if !stdout {
			return "wget writes a file"
		}
	case prog == "rocm-smi":
		for _, a := range args {
			if a == "-r" || hasPrefixAny(a, "--set", "--reset", "--gpureset", "--load", "--save", "--autorespond", "--rasenable", "--rasdisable", "--rasinject") {
				return "rocm-smi changes GPU settings"
			}
		}
	case prog == "amd-smi":
		if v := firstBare(args); v == "set" || v == "reset" {
			return "amd-smi changes GPU settings"
		}
	case prog == "nvidia-smi":
		if nvidiaRead[firstBare(args)] {
			return ""
		}
		for _, a := range args {
			if nvidiaSet[a] || nvidiaSet[strings.SplitN(a, "=", 2)[0]] {
				return "nvidia-smi changes GPU settings"
			}
		}
	}
	return ""
}

func bareArgs(args []string) []string {
	var out []string
	for _, a := range args {
		if !strings.HasPrefix(a, "-") {
			out = append(out, a)
		}
	}
	return out
}

// curlWrites: curl writes when it sends data or saves the body to a file.
// -o/--output to a file counts, in any short-flag cluster (-so file, -sko
// file, -ofile); -o /dev/null and -o - (stdout) do not, since a status probe
// that discards its body is the commonest investigation read there is.
func curlWrites(args []string) bool {
	for i, a := range args {
		switch {
		case a == "--request" || a == "-X":
			if i+1 < len(args) && isMutatingMethod(args[i+1]) {
				return true
			}
		case strings.HasPrefix(a, "-X") && isMutatingMethod(a[2:]):
			return true
		case strings.HasPrefix(a, "--request=") && isMutatingMethod(a[len("--request="):]):
			return true
		case curlSend[a] || strings.HasPrefix(a, "--data") || strings.HasPrefix(a, "--form"):
			return true
		case a == "--output":
			if i+1 < len(args) && !isDevSink(args[i+1]) {
				return true
			}
		case strings.HasPrefix(a, "--output="):
			if !isDevSink(a[len("--output="):]) {
				return true
			}
		case strings.HasPrefix(a, "-") && !strings.HasPrefix(a, "--"):
			cluster := a[1:]
			if o := strings.IndexByte(cluster, 'o'); o >= 0 {
				target := cluster[o+1:]
				if target == "" && i+1 < len(args) {
					target = args[i+1]
				}
				if !isDevSink(target) {
					return true
				}
			}
		}
	}
	return false
}

func isMutatingMethod(m string) bool {
	switch strings.ToUpper(strings.TrimSpace(m)) {
	case "POST", "PUT", "DELETE", "PATCH":
		return true
	}
	return false
}

// ---------------------------------------------------------------------------
// Redirects
// ---------------------------------------------------------------------------

// redirectsToFile reports a `>` or `>>` outside quotes whose target is a file:
// not `&1`/`&2`, not the bit bucket, not the standard streams, not a /dev/tcp
// probe. Stricter than the Python pattern in two places, on purpose: `2> file`
// and `&> file` are writes here.
func redirectsToFile(seg string) bool {
	plain := blankQuotes(seg)
	for i := 0; i < len(plain); i++ {
		if plain[i] != '>' {
			continue
		}
		if i > 0 && plain[i-1] == '<' { // <> or <<
			continue
		}
		j := i + 1
		if j < len(plain) && plain[j] == '>' {
			j++
		}
		if j < len(plain) && plain[j] == '|' {
			j++
		}
		for j < len(plain) && (plain[j] == ' ' || plain[j] == '\t') {
			j++
		}
		if j < len(plain) && plain[j] == '&' { // >&1, >&2
			i = j
			continue
		}
		if j < len(plain) && plain[j] == '(' { // >(process substitution)
			i = j
			continue
		}
		k := j
		for k < len(plain) && plain[k] != ' ' && plain[k] != '\t' {
			k++
		}
		if !isDevSink(plain[j:k]) {
			return true
		}
		i = k
	}
	return false
}

// blankQuotes replaces the inside of quoted spans with spaces so operators
// inside them are not seen; the span keeps its width so offsets line up.
func blankQuotes(seg string) string {
	out := []byte(seg)
	var state byte
	for i := 0; i < len(out); i++ {
		c := out[i]
		switch {
		case state != 0:
			if c == state {
				state = 0
			} else if c == '\\' && state == '"' && i+1 < len(out) {
				out[i], out[i+1] = ' ', ' '
				i++
			} else {
				out[i] = ' '
			}
		case c == '\\' && i+1 < len(out):
			out[i], out[i+1] = ' ', ' '
			i++
		case c == '\'' || c == '"':
			state = c
		}
	}
	return string(out)
}
