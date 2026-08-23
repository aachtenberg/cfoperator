package main

// `cfassist attach` write-back (CFOP-37): the session says what it learned
// before it stops existing.
//
// Ordering is the whole design here. This runs *after* the TUI exits and
// *before* the session token is revoked, because the token is what authorises
// the write — a `defer` that revoked first would leave every session unable to
// record itself. The attach flow arranges that by calling this explicitly on
// the way out rather than deferring it, so the order is visible rather than
// dependent on the LIFO order of two defers written pages apart.
//
// Nothing here can fail the exit. An operator who has just finished an incident
// does not need cfassist to return non-zero because a summariser had a bad day
// — but they do need to be told, because a write-back they believe happened and
// did not is worse than none at all.

import (
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/cfoperator"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/spf13/cobra"
)

// Overridable so tests can drive the flow without an LLM or an agent.
var (
	summarizeSession = cfoperator.Summarize
)

// writeBackSession distils a finished session and records it.
//
// url/token are the session's own — the credential minted for this
// investigation, which carries the `investigate` scope both write endpoints
// take. Using the operator's standing token instead would work and would
// quietly undo the property CFOP-32 exists for.
func writeBackSession(cmd *cobra.Command, investigationID int, url, token string,
	llm *client.LLMClient, messages []client.Message, started time.Time,
	tier, host string) {

	exchanges := cfoperator.SessionExchanges(messages)
	if exchanges == 0 {
		// Attached, read the briefing, left. There is nothing to distil, and a
		// record saying "a human looked and said nothing" is noise in a place
		// that has to stay worth reading.
		return
	}

	if skip, _ := cmd.Flags().GetBool("no-writeback"); skip {
		// Loud, not silent: the flag has a cost and the operator should see it
		// in the units they just spent.
		fmt.Fprintf(os.Stderr,
			"session not recorded (--no-writeback): %d exchanges on investigation #%d "+
				"discarded\n", exchanges, investigationID)
		return
	}

	record := &cfoperator.SessionRecord{
		InvestigationID: investigationID,
		Exchanges:       exchanges,
		DurationSeconds: int(time.Since(started).Seconds()),
		Tier:            tier,
		Host:            host,
	}
	if llm != nil {
		record.Model = llm.Model
	}

	var learning *cfoperator.Learning
	summary, err := summarizeSession(cmd.Context(), llm, messages)
	if err != nil {
		// The issue's own instruction: store the raw tail rather than nothing.
		// A session's only trace should not depend on a local model having a
		// good day — but it is marked, so nobody reads a transcript fragment
		// as a conclusion.
		fmt.Fprintf(os.Stderr, "warning: could not summarize the session (%v) — "+
			"recording the raw tail instead\n", err)
		record.Outcome = "inconclusive"
		record.Summary = cfoperator.RawTail(messages, 4000)
		record.Degraded = true
	} else {
		record.Outcome = summary.Outcome
		record.Summary = summary.Summary
		record.Commands = summary.Commands
		learning = summary.Learning
		if summary.DroppedLearning != "" {
			// The model tried to teach something and could not say when it
			// applies. Saying so beats the silence that reads as "this session
			// had nothing to teach" — a different fact, and a wrong one.
			fmt.Fprintf(os.Stderr, "warning: the session's learning was not stored: "+
				"%s — a learning with no trigger condition would be auto-deprecated "+
				"and never retrieved\n", summary.DroppedLearning)
		}
	}

	wb := cfoperator.NewWriteBackClient(url, token, 30*time.Second)

	// Learning first, so the session record can cite it. The other order needs
	// a second write to backfill the id.
	if learning != nil {
		learning.Source = "cockpit"
		if id, lerr := wb.RecordLearning(learning); lerr != nil {
			fmt.Fprintf(os.Stderr, "warning: the session's learning was not stored: %v\n",
				formatAPIError(lerr))
		} else {
			record.LearningID = id
			fmt.Printf("learning #%d stored: %s\n", id, learning.Title)
		}
	}

	if err := wb.RecordSession(record); err != nil {
		fmt.Fprintf(os.Stderr, "warning: the session was NOT recorded on investigation "+
			"#%d: %v\n", investigationID, formatAPIError(err))
		return
	}
	fmt.Printf("session recorded on investigation #%d: %s (%d exchanges, %s)\n",
		investigationID, record.Outcome, exchanges,
		time.Duration(record.DurationSeconds)*time.Second)
}

// writeBackTarget reports where this session ran, for the session record.
//
// A plain attach runs on the operator's own machine, which is a fact worth
// recording: "resolved from a laptop" and "resolved on the affected Pi" are
// different claims about how the fix was verified.
func writeBackTarget() (tier, host string) {
	tier = strings.TrimSpace(os.Getenv("CFOP_COCKPIT_TIER"))
	host = strings.TrimSpace(os.Getenv("CFOP_COCKPIT_HOST"))
	if tier == "" {
		tier = "local"
	}
	if host == "" {
		host, _ = os.Hostname()
	}
	return tier, host
}
