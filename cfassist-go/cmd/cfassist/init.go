package main

import (
	"fmt"
	"os"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/spf13/cobra"
)

// newInitCmd writes ~/.cfassist/config.yaml when it is missing.
//
// The install script is the path that made this a verb: it used to confirm
// the binary with `--version`, which returns before EnsureDirectories, so
// deleting the config and reinstalling left the operator with a binary and
// nothing to edit. `init` is that write, without starting a session.
func newInitCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "init",
		Short: "Write ~/.cfassist/config.yaml if it is missing",
		Long: `Scaffold ~/.cfassist and write the default config.yaml if it does
not already exist. An existing file is left alone — init is not a reset.

The install one-liner runs this after placing the binary. Interactive
cfassist does the same on first run; init exists so reinstalling is
enough after deleting the file.`,
		Args: cobra.NoArgs,
		RunE: runInit,
	}
}

func runInit(cmd *cobra.Command, _ []string) error {
	path := config.DefaultConfigPath()
	_, statErr := os.Stat(path)
	missing := os.IsNotExist(statErr)

	cfg, err := config.Load("")
	if err != nil {
		return fmt.Errorf("config: %w", err)
	}
	if err := config.EnsureDirectories(cfg); err != nil {
		return fmt.Errorf("directories: %w", err)
	}

	if missing {
		fmt.Fprintf(cmd.OutOrStdout(), "Wrote %s\n", path)
	} else {
		fmt.Fprintf(cmd.OutOrStdout(), "Already exists: %s\n", path)
	}
	return nil
}
