//go:build windows

package daemon

import "os/exec"

func detachProcess(cmd *exec.Cmd) {
	// No-op on Windows — detachment handled differently; daemon spawn is best-effort.
}
