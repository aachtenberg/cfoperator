//go:build !unix

package tools

import (
	"os"
	"syscall"
)

func detachedProcessAttrs() *syscall.SysProcAttr {
	return nil
}

// killProcessGroup has no process group to reach for off unix, so it kills what
// it can. See the unix build for why the distinction matters.
func killProcessGroup(p *os.Process) error {
	return p.Kill()
}
