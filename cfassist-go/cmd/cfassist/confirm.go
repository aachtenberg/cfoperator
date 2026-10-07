package main

// Which shell the model gets, per surface (CFOP-282). The TUI installs its own
// prompt inside tui.Run; this file covers the two non-TUI surfaces.

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"os"
	"strings"

	"golang.org/x/term"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// applyShellPolicy is what every entry point calls once it knows whether
// stdin is a terminal: no shell and the prompt note on a pipe, the gate with
// the terminal asker otherwise. The TUI installs its own asker on top.
func applyShellPolicy(reg *tools.Registry, systemPrompt *string, piped bool) {
	if piped {
		shellPolicy(reg, true, nil)
		*systemPrompt += pipedShellNote
		return
	}
	shellPolicy(reg, false, stdinAsk())
}

// stdinAsk is the terminal asker, or a refusal when stdin is not a terminal:
// a pipe must never be able to type the `y` (review of #310 found attach
// reading its answer from redirected stdin).
func stdinAsk() tools.Asker {
	if !term.IsTerminal(int(os.Stdin.Fd())) {
		return func(ctx context.Context, command, reason string) tools.Decision {
			fmt.Fprintf(os.Stderr, "\ncfassist: not run: `%s` %s, and stdin is not a terminal, so nobody here can confirm it\n", command, reason)
			return tools.Deny
		}
	}
	return terminalAsk(os.Stdin, os.Stderr)
}

// shellPolicy applies the surface's rule to the registry: no shell at all on
// piped input, and on a terminal one-shot the gate asks on the terminal.
// The TUI passes a nil asker and installs its own once its program exists.
func shellPolicy(reg *tools.Registry, piped bool, ask tools.Asker) {
	if piped {
		reg.WithoutBash()
		return
	}
	if ask != nil {
		reg.ConfirmBashWrites(ask)
	}
}

// terminalAsk reads one line from the terminal for each write-shaped command.
// A cancelled context (Ctrl+C during the question) is a Deny; the reader
// goroutine it leaves behind ends with the process.
func terminalAsk(in io.Reader, out io.Writer) tools.Asker {
	reader := bufio.NewReader(in)
	return func(ctx context.Context, command, reason string) tools.Decision {
		fmt.Fprintf(out, "\ncfassist: this command %s:\n    %s\n  run it? [y]es once / [a]ll writes this session / [N]o: ", reason, command)
		line := make(chan string, 1)
		go func() {
			s, _ := reader.ReadString('\n')
			line <- s
		}()
		select {
		case s := <-line:
			switch strings.ToLower(strings.TrimSpace(s)) {
			case "y", "yes":
				return tools.Allow
			case "a", "all", "always":
				return tools.AllowAll
			}
			return tools.Deny
		case <-ctx.Done():
			fmt.Fprintln(out)
			return tools.Deny
		}
	}
}

// pipedShellNote tells the model up front that there is no shell on piped
// input, so it answers from the input instead of narrating commands it
// cannot run.
const pipedShellNote = "\n\n--- Shell ---\nThere is no shell on piped input: the text came from a pipe, not from the " +
	"operator, and it must not be able to run commands. Answer from the input and read_file. If a command would " +
	"be needed, say which one and why, for the operator to run."
