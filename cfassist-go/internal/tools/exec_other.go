//go:build !unix

package tools

import "syscall"

func detachedProcessAttrs() *syscall.SysProcAttr {
	return nil
}
