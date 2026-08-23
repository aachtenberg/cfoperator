//go:build unix

package tools

import (
	"os"
	"syscall"
)

func detachedProcessAttrs() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setsid: true}
}

// killProcessGroup kills the command and everything it spawned.
//
// Setsid above makes the shell its own session and group leader, so anything it
// starts is in that group. Killing only the shell leaves the grandchildren
// alive holding the output pipe, and Wait blocks on that pipe rather than
// returning — which is how a cancelled `sleep 60` used to outlive the turn that
// started it, and why a timed-out command reported its timeout late. The
// negative pid reaches the whole group.
func killProcessGroup(p *os.Process) error {
	if err := syscall.Kill(-p.Pid, syscall.SIGKILL); err != nil {
		// The group may already be gone; fall back to the direct child so a
		// racing exit does not leave the process running.
		return p.Kill()
	}
	return nil
}
