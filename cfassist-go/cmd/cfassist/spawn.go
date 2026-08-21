package main

// `cfassist attach <id> --spawn`: cockpit tier 1 (CFOP-35).
//
// Two steps, and the split between them is the security design:
//
//  1. POST /api/cockpit/spawn — the AGENT builds the Job, mints the pod's
//     short-lived session token, and puts it in a Secret. Central because the
//     guards (dedupe, concurrency cap, audit, TTL) have to hold for the console
//     button in CFOP-59 too, not just for whoever typed this.
//  2. The attach — the OPERATOR's own binary and the OPERATOR's own
//     credentials, kubectl in the cluster and ssh outside it (CFOP-36). No
//     service account in this system holds pods/attach or pods/exec, and this
//     feature deliberately does not add one: someone spawning a cockpit from a
//     laptop already has that access, so widening a service identity would buy
//     nothing and lose the property that no long-lived identity in the cluster
//     can open a shell.
//
// The client does not know which runtime it is attaching to. The agent decides
// the tier and answers with argv; running it is all that happens here. That is
// why the ladder could grow two rungs without this file learning what docker or
// systemd-run are.

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/cfoperator"
	"github.com/spf13/cobra"
)

// Overridable so tests can assert the argv without a cluster (and without
// kubectl installed on the runner).
var (
	kubectlBinary = "kubectl"

	runKubectlCapture = func(args ...string) (string, error) {
		out, err := exec.Command(kubectlBinary, args...).Output()
		return string(out), err
	}

	// Interactive: stdin/stdout/stderr are the operator's terminal, which is
	// the whole point — this call is the cockpit.
	//
	// argv straight to exec.Command, never through a shell: the argv came over
	// the wire from the agent, and `sh -c` on it would make a confused agent a
	// local-execution primitive on the operator's laptop.
	runAttachInteractive = func(argv ...string) error {
		cmd := exec.Command(argv[0], argv[1:]...)
		cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
		return cmd.Run()
	}

	spawnPollInterval = 2 * time.Second
	spawnReadyTimeout = 3 * time.Minute
)

// runSpawn launches a cockpit for the investigation and attaches to it.
func runSpawn(cmd *cobra.Command, investigationID int, url, token, question string) error {
	if flagAttachPrint {
		return errors.New("--print and --spawn are opposites: --print renders the briefing here, " +
			"--spawn runs the session in a pod on the cluster")
	}
	if question != "" {
		// A one-shot question would spawn a pod, answer, and destroy it — an
		// expensive way to do what plain attach does locally.
		return errors.New("--spawn starts an interactive session; ask the question inside it, " +
			"or drop --spawn for a one-shot answer")
	}

	ttl, _ := cmd.Flags().GetDuration("session-ttl")
	tier, _ := cmd.Flags().GetString("tier")
	host, _ := cmd.Flags().GetString("host")
	cockpit, err := cfoperator.NewSpawnClient(url, token, 60*time.Second).
		Spawn(investigationID, ttl, tier, host)
	if err != nil {
		return formatAPIError(err)
	}

	verb := "spawned"
	if cockpit.Status == "existing" {
		verb = "already running"
	}
	fmt.Printf("cockpit %s: %s (%s)\n", verb, cockpit.Where(), cockpit.Placement.Note)
	if cockpit.HostProvenance != "" {
		// Where the session landed is a decision the operator did not make and
		// has to be able to check mid-incident — "the wrong box" is otherwise
		// only discoverable by looking around once you are inside it.
		fmt.Printf("  target: %s\n", cockpit.HostProvenance)
	}
	if cockpit.TTLSeconds > 0 {
		fmt.Printf("session token %s… — session and token expire in %s\n",
			cockpit.TokenPrefix, (time.Duration(cockpit.TTLSeconds) * time.Second))
	}
	if isolation := tierIsolationWarning(cockpit.Tier); isolation != "" {
		// Honest degradation is only honest if it is said out loud, at the
		// moment it applies. Below the container tier there is no boundary
		// between this session and the host it runs on.
		fmt.Println(isolation)
	}

	// Only tier 1 has a pod to become Running: the container is started by the
	// spawn call itself, and the process tiers have nothing running until this
	// attach starts it.
	if cockpit.Tier == cfoperator.TierPod {
		if err := waitForCockpit(cockpit); err != nil {
			// The Job exists and will clean itself up; tell the operator how to
			// get in by hand rather than leaving them with a pod they cannot find.
			return fmt.Errorf("%w\n  hint: the cockpit is still starting — attach with: %s",
				err, cockpit.AttachCommand)
		}
	}

	fmt.Printf("attaching (%s) — %s\n", cockpit.AttachCommand, detachHint(cockpit.Tier))
	return runAttachInteractive(cockpit.AttachArgv...)
}

// tierIsolationWarning names what the chosen tier does not give you. Empty for
// the tiers that have a boundary.
func tierIsolationWarning(tier string) string {
	switch tier {
	case cfoperator.TierHost, cfoperator.TierSSH:
		return "  no isolation at this tier: the session runs directly on the host, " +
			"and the short-lived token is the security model"
	}
	return ""
}

// detachHint differs by runtime because the escape sequence does: ctrl-p ctrl-q
// is a docker/kubectl attach convention and means nothing to a plain ssh.
func detachHint(tier string) string {
	switch tier {
	case cfoperator.TierPod, cfoperator.TierContainer:
		return "detach with ctrl-p ctrl-q; exit ends the cockpit"
	}
	return "exit ends the cockpit; the TTL ends it either way"
}

// cockpitPhaseArgs asks for the phase of every pod THIS Job owns. Label
// selector, not the pod name: the pod is created by the Job, so its name is not
// known until it exists.
//
// The selector is Kubernetes' own `job-name`, not the cockpit's
// per-investigation label. The investigation label spans every cockpit ever
// spawned for that investigation, and a finished Job lingers for
// ttlSecondsAfterFinished — so on the ordinary re-run path (spawn, work, exit,
// spawn again) it matches yesterday's Succeeded pod alongside today's Pending
// one, and the wait below concludes the cockpit ended before it started. The
// attach already addresses `job/<name>`; the wait has to agree with it.
func cockpitPhaseArgs(c *cfoperator.Cockpit) []string {
	return []string{
		"get", "pods", "-n", c.Namespace, "-l", "job-name=" + c.JobName,
		"-o", "jsonpath={.items[*].status.phase}",
	}
}

// waitForCockpit blocks until the pod is Running.
//
// Attaching to a Pending pod fails outright, and a cockpit's first seconds are
// an image pull onto (frequently) a Pi — so the wait is the difference between
// "it works" and "run the command again a few times".
func waitForCockpit(c *cfoperator.Cockpit) error {
	deadline := time.Now().Add(spawnReadyTimeout)
	for {
		phases, err := runKubectlCapture(cockpitPhaseArgs(c)...)
		switch {
		// Running is checked before the terminal phases on purpose. A Job whose
		// pod was replaced (node lost, eviction) can show both at once, and a
		// live pod to attach to beats a dead one to report. Belt to the
		// selector's braces above.
		case err == nil && strings.Contains(phases, "Running"):
			return nil
		case err != nil:
			// Transient (the Job's pod not created yet) or fatal (no kubectl,
			// no cluster access). Both look the same here, so keep trying
			// until the deadline and report the last error then.
			if time.Now().After(deadline) {
				return fmt.Errorf("could not reach the cockpit pod with %s: %w", kubectlBinary, err)
			}
		case strings.Contains(phases, "Failed"), strings.Contains(phases, "Succeeded"):
			return fmt.Errorf("the cockpit pod ended before it could be attached to (%s)",
				strings.TrimSpace(phases))
		case time.Now().After(deadline):
			return fmt.Errorf("the cockpit pod was not running within %s (last phase: %q)",
				spawnReadyTimeout, strings.TrimSpace(phases))
		}
		time.Sleep(spawnPollInterval)
	}
}
